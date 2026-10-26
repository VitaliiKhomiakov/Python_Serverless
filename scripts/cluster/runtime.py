"""Bounded commands and explicit ownership of one local kind cluster."""

import hashlib
import os
import re
import signal
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Self

CLUSTER = "serverless-lab"
NAMESPACE = "serverless-lab"
SERVICE = "text-analysis"
ROOT = Path(__file__).resolve().parents[2]


class LabError(Exception):
    """An operation cannot safely proceed."""


def run(*args: str, timeout: int = 60, input_text: str | None = None) -> str:
    environment = dict(os.environ, KIND_EXPERIMENTAL_PROVIDER="docker")
    with subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        start_new_session=True,
    ) as process:
        try:
            output, errors = process.communicate(input_text, timeout=timeout)
        except BaseException as error:
            # kind may spawn Docker clients; terminate the owned process group too.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            if isinstance(error, subprocess.TimeoutExpired):
                raise LabError(
                    f"{args[0]} exceeded {timeout}s; inspect before retrying"
                ) from error
            raise
        if process.returncode:
            raise LabError(f"{args[0]} failed: {errors.strip()}")
        return output.strip()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass(frozen=True)
class Receipt:
    node_id: str
    kubeconfig_hash: str
    system_uid: str

    @classmethod
    def read(cls, path: Path) -> Self:
        if not path.is_file():
            raise LabError(
                "Ownership receipt missing; refusing to adopt/delete a cluster"
            )
        values = path.read_text().splitlines()
        if len(values) != 3 or not all(
            re.fullmatch(r"[0-9a-f]{64}", v) for v in values[:2]
        ):
            raise LabError("Invalid ownership receipt")
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", values[2]):
            raise LabError("Invalid kube-system UID")
        return cls(*values)


@dataclass(frozen=True)
class Lab:
    root: Path = ROOT

    @property
    def local(self) -> Path:
        return self.root / ".local"

    @property
    def state(self) -> Path:
        return self.local / CLUSTER

    @property
    def kubeconfig(self) -> Path:
        return self.state / "kubeconfig"

    def kind(self, *args: str, timeout: int = 60) -> str:
        return run(str(self.local / "bin/kind"), *args, timeout=timeout)

    def kubectl(
        self, *args: str, timeout: int = 60, input_text: str | None = None
    ) -> str:
        return run(
            str(self.local / "bin/kubectl"),
            "--kubeconfig",
            str(self.kubeconfig),
            "--context",
            f"kind-{CLUSTER}",
            "--request-timeout=30s",
            *args,
            timeout=timeout,
            input_text=input_text,
        )

    def nodes(self) -> list[str]:
        output = run(
            "docker",
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            f"label=io.x-k8s.kind.cluster={CLUSTER}",
        )
        return output.splitlines() if output else []

    def require_owned(self, *, api: bool = True) -> Receipt:
        receipt = Receipt.read(self.state / "ownership.txt")
        if self.nodes() != [receipt.node_id]:
            raise LabError("Cluster node identity changed; refusing mutation")
        if (
            not self.kubeconfig.is_file()
            or digest(self.kubeconfig) != receipt.kubeconfig_hash
        ):
            raise LabError("Project kubeconfig changed; refusing mutation")
        if api and self.system_uid() != receipt.system_uid:
            raise LabError("Kubernetes cluster identity changed; refusing mutation")
        return receipt

    def system_uid(self) -> str:
        return self.kubectl(
            "get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}"
        )

    def record_ownership(self) -> None:
        nodes = self.nodes()
        if len(nodes) != 1:
            raise LabError(
                "Expected one newly created kind node; ownership not recorded"
            )
        self.kubeconfig.chmod(0o600)
        receipt = self.state / "ownership.txt"
        temporary = self.state / "ownership.tmp"
        temporary.write_text(
            f"{nodes[0]}\n{digest(self.kubeconfig)}\n{self.system_uid()}\n"
        )
        temporary.chmod(0o600)
        Receipt.read(temporary)
        temporary.replace(receipt)
