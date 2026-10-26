"""Verify resource identity before interpreting Kubernetes observations."""

import json
import math
import re
import time
from dataclasses import dataclass

from scripts.cluster.runtime import NAMESPACE, SERVICE, Lab, LabError


def object_fields(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise LabError("Expected a JSON object from Kubernetes")
    return {key: item for key, item in value.items()}


def items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise LabError("Expected a Kubernetes items/conditions list")
    return [object_fields(item) for item in value]


def text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise LabError("Expected a nonempty Kubernetes string field")
    return value


def fields(resource: dict[str, object], name: str) -> dict[str, object]:
    return object_fields(resource[name])


def read(
    lab: Lab, resource: str, deadline: float, *args: str, namespace: str = NAMESPACE
) -> dict[str, object]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LabError("Kubernetes observation deadline exceeded")
    raw: object = json.loads(
        lab.kubectl(
            "-n",
            namespace,
            "get",
            resource,
            *args,
            "-o",
            "json",
            timeout=max(1, min(10, math.ceil(remaining))),
        )
    )
    return object_fields(raw)


def selector_text(labels: dict[str, object]) -> str:
    if not labels:
        raise LabError("Empty deployment selector would allow false zero")
    pairs: list[str] = []
    for key, value in sorted(labels.items()):
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", key) or not re.fullmatch(
            r"[A-Za-z0-9._-]+", text(value)
        ):
            raise LabError("Unsupported selector label")
        pairs.append(f"{key}={value}")
    return ",".join(pairs)


@dataclass(frozen=True)
class Identity:
    service_uid: str
    generation: int
    revision: str
    revision_uid: str
    deployment: str
    deployment_uid: str
    selector: str
    image: str


def service_revision(service: dict[str, object]) -> tuple[str, int, str]:
    meta, status = fields(service, "metadata"), fields(service, "status")
    generation = meta["generation"]
    revision = text(status["latestReadyRevisionName"])
    if type(generation) is not int or status.get("observedGeneration") != generation:
        raise LabError("Service generation not reconciled")
    if status.get("latestCreatedRevisionName") != revision:
        raise LabError("Service has a different pending Revision")
    if not any(
        item.get("type") == "Ready" and item.get("status") == "True"
        for item in items(status.get("conditions"))
    ):
        raise LabError("Service is not Ready")
    traffic = items(status.get("traffic"))
    if (
        len(traffic) != 1
        or traffic[0].get("percent") != 100
        or traffic[0].get("revisionName") != revision
    ):
        raise LabError("Experiment requires all traffic to one verified Revision")
    return text(meta["uid"]), generation, revision


def capture(lab: Lab, deadline: float) -> Identity:
    service = read(lab, f"ksvc/{SERVICE}", deadline)
    service_uid, generation, revision_name = service_revision(service)
    revision = read(lab, f"revision/{revision_name}", deadline)
    revision_uid = text(fields(revision, "metadata")["uid"])
    spec = fields(revision, "spec")
    app = next(item for item in items(spec["containers"]) if item.get("name") == "app")
    annotations = fields(fields(revision, "metadata"), "annotations")
    for suffix, expected in {
        "class": "kpa.autoscaling.knative.dev",
        "min-scale": "0",
        "max-scale": "3",
        "target": "5",
        "target-utilization-percentage": "100",
    }.items():
        if annotations.get(f"autoscaling.knative.dev/{suffix}") != expected:
            raise LabError(f"Unexpected autoscaler setting: {suffix}")
    if spec.get("containerConcurrency") != 10:
        raise LabError("Unexpected hard concurrency limit")
    deployments = items(
        read(
            lab,
            "deployments",
            deadline,
            "-l",
            f"serving.knative.dev/revision={revision_name}",
        )["items"]
    )
    if len(deployments) != 1:
        raise LabError("Expected one Deployment owned by the selected Revision")
    deployment = deployments[0]
    meta, selector = (
        fields(deployment, "metadata"),
        fields(fields(deployment, "spec"), "selector"),
    )
    if not any(
        owner.get("uid") == revision_uid
        and owner.get("kind") == "Revision"
        and owner.get("controller") is True
        for owner in items(meta.get("ownerReferences"))
    ):
        raise LabError("Deployment is not owned by the selected Revision")
    labels = fields(selector, "matchLabels")
    template_labels = fields(
        fields(fields(fields(deployment, "spec"), "template"), "metadata"), "labels"
    )
    identifies_revision = (
        labels.get("serving.knative.dev/revision") == revision_name
        or labels.get("serving.knative.dev/revisionUID") == revision_uid
    )
    if (
        selector.get("matchExpressions")
        or not identifies_revision
        or any(template_labels.get(key) != value for key, value in labels.items())
        or template_labels.get("serving.knative.dev/revision") != revision_name
        or template_labels.get("serving.knative.dev/service") != SERVICE
    ):
        raise LabError("Deployment selector does not identify the selected Revision")
    return Identity(
        service_uid,
        generation,
        revision_name,
        revision_uid,
        text(meta["name"]),
        text(meta["uid"]),
        selector_text(labels),
        text(app["image"]),
    )


@dataclass(frozen=True)
class Snapshot:
    elapsed_seconds: float
    revision_pods: int
    service_pods: int
    ready_pods: int
    names: tuple[str, ...]


def observe(lab: Lab, identity: Identity, started: float, deadline: float) -> Snapshot:
    current = capture(lab, deadline)
    if current != identity:
        raise LabError(
            "Service, Revision, image or deployment identity changed during experiment"
        )
    selected = items(read(lab, "pods", deadline, "-l", identity.selector)["items"])
    service_pods = items(
        read(lab, "pods", deadline, "-l", f"serving.knative.dev/service={SERVICE}")[
            "items"
        ]
    )
    names: list[str] = []
    ready = 0
    for pod in selected:
        meta = fields(pod, "metadata")
        labels = fields(meta, "labels")
        if (
            labels.get("serving.knative.dev/revision") != identity.revision
            or labels.get("serving.knative.dev/service") != SERVICE
        ):
            raise LabError("Pod labels contradict the verified selector")
        names.append(text(meta["name"]))
        status = fields(pod, "status")
        if not meta.get("deletionTimestamp") and any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in items(status.get("conditions", []))
        ):
            ready += 1
    return Snapshot(
        round(time.monotonic() - started, 3),
        len(selected),
        len(service_pods),
        ready,
        tuple(sorted(names)),
    )


def platform_status(lab: Lab, deadline: float) -> dict[str, int]:
    expected = {
        "knative-serving": {
            "activator",
            "autoscaler",
            "controller",
            "webhook",
            "net-kourier-controller",
        },
        "kourier-system": {"3scale-kourier-gateway"},
    }
    result: dict[str, int] = {}
    for namespace, names in expected.items():
        deployments = items(
            read(lab, "deployments", deadline, namespace=namespace)["items"]
        )
        for name in names:
            deployment = next(
                item
                for item in deployments
                if fields(item, "metadata").get("name") == name
            )
            available = fields(deployment, "status").get("availableReplicas", 0)
            if type(available) is not int or available < 1:
                raise LabError(f"Platform component unavailable: {namespace}/{name}")
            result[f"{namespace}/{name}"] = available
    return result
