"""Download pinned tools/manifests without changing global installations."""

import platform
import re
import time
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .runtime import Lab, LabError, digest


@dataclass(frozen=True)
class Artifact:
    name: str
    url: str
    sha256: str

    @property
    def executable(self) -> bool:
        return self.name in {"kind", "kubectl"}

    def destination(self, lab: Lab) -> Path:
        return lab.local / ("bin" if self.executable else "cache") / self.name


def locked_artifacts(lab: Lab) -> list[Artifact]:
    with (lab.root / "infra/k8s/platform/artifacts.toml").open("rb") as stream:
        raw: object = tomllib.load(stream)
    if not isinstance(raw, dict):
        raise LabError("Invalid artifact lock")
    artifacts: list[Artifact] = []
    for name, fields in raw.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z-]+", name):
            raise LabError("Invalid artifact name")
        if not isinstance(fields, dict):
            raise LabError("Invalid artifact fields")
        url: object = fields.get("url")
        checksum: object = fields.get("sha256")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise LabError("Expected HTTPS artifact URL")
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise LabError("Expected SHA-256 artifact checksum")
        artifacts.append(Artifact(name, url, checksum))
    if {item.name for item in artifacts} != {
        "kind",
        "kubectl",
        "serving-crds",
        "serving-core",
        "kourier",
    }:
        raise LabError("Artifact lock has missing or unexpected entries")
    return artifacts


def fetch(artifact: Artifact, destination: Path) -> None:
    if destination.exists():
        if digest(destination) != artifact.sha256:
            raise LabError(
                f"Checksum mismatch: {destination}; remove that file before retrying"
            )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".download")
    deadline = time.monotonic() + 180
    try:
        with (
            urllib.request.urlopen(artifact.url, timeout=20) as response,
            temporary.open("wb") as out,
        ):
            total = 0
            while chunk := response.read1(1024 * 1024):
                total += len(chunk)
                if time.monotonic() > deadline or total > 150 * 1024 * 1024:
                    raise LabError(f"Download limit exceeded: {artifact.name}")
                out.write(chunk)
        if digest(temporary) != artifact.sha256:
            raise LabError(f"Checksum mismatch: {artifact.name}")
        temporary.chmod(0o755 if artifact.executable else 0o644)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def prepare(lab: Lab) -> None:
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise LabError("Pinned tools support Linux amd64 only")
    for artifact in locked_artifacts(lab):
        fetch(artifact, artifact.destination(lab))
    print("Tools and upstream manifests verified against the artifact lock")
