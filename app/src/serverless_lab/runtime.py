"""Immutable process configuration, independent of analysis and HTTP models."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    instance_id: str
    revision: str

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> RuntimeSettings:
        return cls(
            instance_id=_safe_identity(environment.get("HOSTNAME"), "instance"),
            revision=_safe_identity(environment.get("K_REVISION"), "revision"),
        )


def _safe_identity(value: str | None, prefix: str) -> str:
    if value is not None and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value):
        return value
    return f"{prefix}-{uuid4().hex}"
