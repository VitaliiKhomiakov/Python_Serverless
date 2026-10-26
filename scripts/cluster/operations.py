"""Install and operate only the cluster identified by the local ownership receipt."""

import http.client
import json
import os
import re
import shutil
import socket
import tempfile
import time
from pathlib import Path

from .artifacts import prepare
from .runtime import CLUSTER, NAMESPACE, SERVICE, Lab, LabError, Receipt, digest, run


def check_capacity(root: Path) -> None:
    available_kib = next(
        int(line.split()[1])
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith("MemAvailable:")
    )
    if available_kib < 6 * 1024 * 1024 or (os.cpu_count() or 0) < 6:
        raise LabError("A new single-node lab requires 6 available GiB RAM and 6 CPUs")
    if shutil.disk_usage(root).free < 30 * 1024**3:
        raise LabError("A new lab requires at least 30 GiB free disk space")
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", 8081))
        except OSError as error:
            raise LabError("Ingress port 127.0.0.1:8081 is unavailable") from error


def ensure_cluster(lab: Lab) -> None:
    if (lab.state / "ownership.txt").exists():
        lab.require_owned()
        return
    if lab.nodes() or CLUSTER in lab.kind("get", "clusters").splitlines():
        raise LabError("Existing unowned cluster; refusing to adopt or replace it")
    if lab.kubeconfig.exists():
        raise LabError(
            "Unrecorded kubeconfig exists; inspect incomplete setup manually"
        )
    check_capacity(lab.root)
    lab.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lab.kind(
        "create",
        "cluster",
        "--name",
        CLUSTER,
        "--config",
        str(lab.root / "infra/kind/cluster.yaml"),
        "--kubeconfig",
        str(lab.kubeconfig),
        "--wait",
        "180s",
        timeout=420,
    )
    lab.record_ownership()
    lab.require_owned()


def apply(lab: Lab, path: Path) -> None:
    # Upstream webhook controllers update fields in their installation manifests.
    # Use Knative's documented client-side apply so reruns preserve those updates.
    lab.kubectl(
        "apply",
        "--field-manager=serverless-lab",
        "-f",
        str(path),
        timeout=120,
    )


def wait_for_config_webhook(lab: Lab) -> None:
    deadline = time.monotonic() + 60
    while True:
        try:
            lab.kubectl(
                "-n",
                "knative-serving",
                "patch",
                "configmap",
                "config-network",
                "--dry-run=server",
                "--type=merge",
                "--patch-file",
                str(lab.root / "infra/k8s/platform/config-network.yaml"),
                timeout=10,
            )
            return
        except LabError as error:
            transient = "failed calling webhook" in str(error) and any(
                message in str(error)
                for message in ("connection refused", "no endpoints available")
            )
            if not transient or time.monotonic() >= deadline:
                raise
            time.sleep(1)


def setup(lab: Lab) -> None:
    prepare(lab)
    ensure_cluster(lab)
    print("Owned cluster confirmed; installing Serving and Kourier", flush=True)
    apply(lab, lab.local / "cache/serving-crds")
    lab.kubectl(
        "wait",
        "--for=condition=Established",
        "--timeout=60s",
        "-f",
        str(lab.local / "cache/serving-crds"),
        timeout=90,
    )
    apply(lab, lab.local / "cache/serving-core")
    lab.kubectl(
        "-n",
        "knative-serving",
        "wait",
        "deployment",
        "--all",
        "--for=condition=Available",
        "--timeout=240s",
        timeout=270,
    )
    wait_for_config_webhook(lab)
    apply(lab, lab.local / "cache/kourier")
    directory = lab.root / "infra/k8s/platform"
    for name in (
        "config-network",
        "config-domain",
        "config-deployment",
        "config-autoscaler",
        "config-features",
    ):
        lab.kubectl(
            "-n",
            "knative-serving",
            "patch",
            "configmap",
            name,
            "--type=merge",
            "--field-manager=serverless-lab",
            "--patch-file",
            str(directory / f"{name}.yaml"),
        )
    lab.kubectl(
        "-n",
        "kourier-system",
        "patch",
        "service",
        "kourier",
        "--type=merge",
        "--field-manager=serverless-lab",
        "--patch-file",
        str(directory / "kourier-service.yaml"),
    )
    lab.kubectl(
        "-n",
        "kourier-system",
        "wait",
        "deployment",
        "--all",
        "--for=condition=Available",
        "--timeout=240s",
        timeout=270,
    )
    print("Platform deployments available; deploy the application next")


def deploy(lab: Lab, image: str) -> None:
    lab.require_owned()
    image_id = run("docker", "image", "inspect", image, "--format", "{{.Id}}")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise LabError("Unexpected local Docker image identity")
    reference = f"dev.local/serverless-lab:{image_id.removeprefix('sha256:')}"
    rendered = render_service(lab.root / "infra/k8s/app/service.yaml", reference)
    run("docker", "tag", image_id, reference)
    lab.kind("load", "docker-image", reference, "--name", CLUSTER, timeout=240)
    apply(lab, lab.root / "infra/k8s/app/namespace.yaml")
    # Admission checks Knative's additional field/feature rules before mutation.
    for extra in (("--dry-run=server",), ()):
        lab.kubectl(
            "apply",
            "--server-side",
            "--field-manager=serverless-lab",
            *extra,
            "-f",
            "-",
            input_text=rendered,
            timeout=120,
        )
    lab.kubectl(
        "-n",
        NAMESPACE,
        "wait",
        f"ksvc/{SERVICE}",
        "--for=condition=Ready",
        "--timeout=240s",
        timeout=270,
    )
    revision = ready_revision(lab)
    print(f"Ready Revision: {revision}; image: {reference}")
    smoke(lab)


def render_service(path: Path, reference: str) -> str:
    if not re.fullmatch(r"dev\.local/serverless-lab:[0-9a-f]{64}", reference):
        raise LabError("Expected an image-ID-derived local tag")
    source = path.read_text()
    sentinel = "dev.local/serverless-lab:replace-at-deploy"
    if source.count(sentinel) != 1:
        raise LabError("Expected exactly one image placeholder in the Service manifest")
    return source.replace(sentinel, reference)


def ready_revision(lab: Lab) -> str:
    identity = lab.kubectl(
        "-n",
        NAMESPACE,
        "get",
        "ksvc",
        SERVICE,
        "-o",
        "jsonpath={.metadata.generation} {.status.observedGeneration} "
        "{.status.latestCreatedRevisionName} {.status.latestReadyRevisionName}",
    ).split()
    if len(identity) != 4 or identity[0] != identity[1] or identity[2] != identity[3]:
        raise LabError("Service generation/Revision is not fully reconciled")
    revision = identity[3]
    if not re.fullmatch(r"text-analysis-[a-z0-9-]+", revision):
        raise LabError("Expected an explicit Ready Revision")
    lab.kubectl(
        "-n",
        NAMESPACE,
        "wait",
        f"revision/{revision}",
        "--for=condition=Ready",
        "--timeout=60s",
        timeout=90,
    )
    return revision


def request(path: str, payload: dict[str, object] | None = None) -> tuple[int, object]:
    connection = http.client.HTTPConnection("127.0.0.1", 8081, timeout=45)
    headers = {"Host": "text-analysis.serverless-lab.lab.test"}
    body = None if payload is None else json.dumps(payload)
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        connection.request("GET" if body is None else "POST", path, body, headers)
        response = connection.getresponse()
        result: object = json.loads(response.read())
        return response.status, result
    finally:
        connection.close()


def smoke(lab: Lab) -> None:
    lab.require_owned()
    revision = ready_revision(lab)
    expected = {"characters": 8, "words": 2, "top_words": [{"word": "кот", "count": 2}]}
    if request("/health") != (200, {"status": "ok"}):
        raise LabError("Ingress health request failed")
    if request("/analyze", {"text": "Кот кот!", "top_k": 1}) != (200, expected):
        raise LabError("Ingress analysis response differs from the contract")
    review_expected = {
        "analysis": expected,
        "word_count_check": {"min_words": 3, "max_words": 5, "status": "too_short"},
    }
    if request(
        "/review-text", {"text": "Кот кот!", "min_words": 3, "max_words": 5}
    ) != (200, review_expected):
        raise LabError("Ingress review response differs from the contract")
    if request("/analyze", {"text": "PRIVATE_CHECK", "top_k": True}) != (
        422,
        {"detail": "Invalid request."},
    ):
        raise LabError("Expected safe input validation through ingress")
    if ready_revision(lab) != revision:
        raise LabError("Revision changed during the smoke check")
    print(f"PASS: health, analysis, review and validation through Kourier; {revision}")


def cleanup(lab: Lab) -> None:
    if (
        not (lab.state / "ownership.txt").exists()
        and not lab.kubeconfig.exists()
        and not lab.nodes()
    ):
        print("No project cluster or ownership state exists")
        return
    receipt = Receipt.read(lab.state / "ownership.txt")
    if lab.kubeconfig.exists() and digest(lab.kubeconfig) != receipt.kubeconfig_hash:
        raise LabError("Project kubeconfig changed; refusing cleanup")
    if lab.nodes():
        lab.require_owned(api=False)
        # kind edits kubeconfig before deleting nodes; preserve recovery state on error.
        with tempfile.TemporaryDirectory(prefix="cleanup-", dir=lab.state) as directory:
            disposable_config = Path(directory) / "kubeconfig"
            shutil.copy2(lab.kubeconfig, disposable_config)
            lab.kind(
                "delete",
                "cluster",
                "--name",
                CLUSTER,
                "--kubeconfig",
                str(disposable_config),
                timeout=120,
            )
        if lab.nodes():
            raise LabError("Cluster still exists; retaining ownership receipt")
    for name in ("kubeconfig", "ownership.txt"):
        (lab.state / name).unlink(missing_ok=True)
    print(f"Owned cluster removed ({receipt.node_id[:12]}); tools and images retained")
