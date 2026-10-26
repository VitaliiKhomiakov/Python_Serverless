"""Bounded ingress experiment; run with the app's locked development environment."""

import argparse
import asyncio
import fcntl
import json
import os
import platform
import re
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import httpx2

from scripts.checks.knative_state import (
    Identity,
    Snapshot,
    capture,
    observe,
    platform_status,
)
from scripts.cluster.runtime import NAMESPACE, Lab, LabError


@dataclass(frozen=True)
class ResponseSample:
    duration_ms: float
    instance_id: str
    request_id: str


@dataclass(frozen=True)
class LoadResult:
    requests: int
    instances: tuple[str, ...]
    maximum_ready_pods: int
    duration_seconds: float


@dataclass
class Report:
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    host: str = field(default_factory=platform.platform)
    python: str = field(default_factory=platform.python_version)
    cpus: int | None = field(default_factory=os.cpu_count)
    outcome: str = "running"
    identity: Identity | None = None
    snapshots: list[Snapshot] = field(default_factory=list)
    post_idle: ResponseSample | None = None
    warm: list[ResponseSample] = field(default_factory=list)
    load: LoadResult | None = None
    platform_at_zero: dict[str, int] = field(default_factory=dict)
    failure: str | None = None
    diagnostics: str | None = None
    request_log: dict[str, object] | None = None


async def request(
    client: httpx2.AsyncClient, identity: Identity, timeout: float = 30
) -> ResponseSample:
    request_id = f"load-{uuid.uuid4().hex}"
    started = time.monotonic()
    async with asyncio.timeout(timeout):
        async with client.stream(
            "POST",
            "http://127.0.0.1:8081/analyze",
            headers={
                "Host": "text-analysis.serverless-lab.lab.test",
                "X-Request-Id": request_id,
            },
            json={"text": "Кот кот!", "top_k": 1},
        ) as response:
            if response.status_code != 200:
                raise LabError(f"Ingress returned HTTP {response.status_code}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 16_384:
                    raise LabError("Unexpected oversized ingress response")
            payload: object = json.loads(body)
            expected = {
                "characters": 8,
                "words": 2,
                "top_words": [{"word": "кот", "count": 2}],
            }
            if payload != expected:
                raise LabError("Analysis contract changed during experiment")
            instance = response.headers.get("x-instance-id", "")
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", instance):
                raise LabError("Missing/invalid instance header")
            if response.headers.get("x-revision") != identity.revision:
                raise LabError("Ingress served a different Revision")
            if response.headers.get("x-request-id") != request_id:
                raise LabError("Request correlation ID was not preserved")
    return ResponseSample(
        round((time.monotonic() - started) * 1000, 3), instance, request_id
    )


async def wait_for_pods(
    lab: Lab,
    identity: Identity,
    report: Report,
    started: float,
    phase: str,
    condition: Callable[[Snapshot], bool],
    timeout: float = 240,
) -> Snapshot:
    deadline = time.monotonic() + timeout
    previous: tuple[int, int] | None = None
    while time.monotonic() < deadline:
        snapshot = await asyncio.to_thread(observe, lab, identity, started, deadline)
        report.snapshots.append(snapshot)
        counts = (snapshot.service_pods, snapshot.ready_pods)
        if counts != previous:
            print(
                f"{phase}: service Pods={counts[0]}, selected Ready={counts[1]}",
                flush=True,
            )
            previous = counts
        if condition(snapshot):
            return snapshot
        await asyncio.sleep(min(2, max(0, deadline - time.monotonic())))
    raise LabError(f"{phase}: condition not observed within {timeout}s")


async def run_load(
    lab: Lab,
    identity: Identity,
    client: httpx2.AsyncClient,
    report: Report,
    started: float,
    concurrency: int,
    duration: int,
) -> LoadResult:
    load_started = time.monotonic()
    deadline = load_started + duration
    samples: list[ResponseSample] = []
    maximum_ready = 0

    async def worker() -> None:
        while time.monotonic() < deadline:
            samples.append(await request(client, identity))

    async def monitor() -> None:
        nonlocal maximum_ready
        while time.monotonic() < deadline:
            snapshot = await asyncio.to_thread(
                observe, lab, identity, started, deadline + 10
            )
            report.snapshots.append(snapshot)
            if snapshot.ready_pods > maximum_ready:
                maximum_ready = snapshot.ready_pods
                print(f"load: Ready Pods={maximum_ready}", flush=True)
            await asyncio.sleep(min(2, max(0, deadline - time.monotonic())))

    # A failed worker/observer cancels siblings; TaskGroup joins before idle polling.
    async with asyncio.timeout(duration + 45), asyncio.TaskGroup() as group:
        for _ in range(concurrency):
            group.create_task(worker())
        group.create_task(monitor())
    if maximum_ready < 2 or not samples:
        raise LabError(
            "Load did not produce at least two Ready Pods with successful requests"
        )
    return LoadResult(
        len(samples),
        tuple(sorted({sample.instance_id for sample in samples})),
        maximum_ready,
        round(time.monotonic() - load_started, 3),
    )


def no_application_pods(snapshot: Snapshot) -> bool:
    return snapshot.revision_pods == 0 and snapshot.service_pods == 0


async def experiment(lab: Lab, report: Report, concurrency: int, duration: int) -> None:
    lab.require_owned()
    started = time.monotonic()
    identity = capture(lab, started + 40)
    report.identity = identity
    print(f"Selected {identity.revision}", flush=True)
    limits = httpx2.Limits(
        max_connections=concurrency, max_keepalive_connections=concurrency
    )
    async with httpx2.AsyncClient(timeout=30, limits=limits, trust_env=False) as client:
        await wait_for_pods(
            lab, identity, report, started, "initial idle", no_application_pods
        )
        report.post_idle = await request(client, identity)
        await wait_for_pods(
            lab,
            identity,
            report,
            started,
            "wake-up",
            lambda snapshot: snapshot.ready_pods >= 1,
            timeout=60,
        )
        for _ in range(3):
            report.warm.append(await request(client, identity))
        identities = Counter(
            sample.instance_id for sample in [report.post_idle, *report.warm]
        )
        if max(identities.values()) < 2:
            raise LabError("Instance reuse was not observed")
        report.request_log = await asyncio.to_thread(
            correlated_log, lab, report.warm[-1], identity
        )
        report.load = await run_load(
            lab, identity, client, report, started, concurrency, duration
        )
        await wait_for_pods(
            lab, identity, report, started, "final idle", no_application_pods
        )
    report.platform_at_zero = platform_status(lab, time.monotonic() + 30)
    # A final API-only observation also catches drift during the platform inspection.
    final = observe(lab, identity, started, time.monotonic() + 30)
    if not no_application_pods(final):
        raise LabError("Application received new traffic during final zero observation")
    report.snapshots.append(final)
    report.outcome = "passed"


def correlated_log(
    lab: Lab, sample: ResponseSample, identity: Identity
) -> dict[str, object]:
    raw = lab.kubectl(
        "-n",
        NAMESPACE,
        "logs",
        f"pod/{sample.instance_id}",
        "-c",
        "app",
        "--tail=200",
        timeout=10,
    )
    if "Кот кот!" in raw or "PRIVATE_CHECK" in raw:
        raise LabError("User text appeared in application logs")
    for line in raw.splitlines():
        if not line.startswith("{"):
            continue
        event: object = json.loads(line)
        if (
            isinstance(event, dict)
            and event.get("request_id") == sample.request_id
            and event.get("event") == "request.completed"
        ):
            if (
                event.get("revision") != identity.revision
                or event.get("instance_id") != sample.instance_id
            ):
                raise LabError("Log correlation identity mismatch")
            return {str(key): value for key, value in event.items()}
    raise LabError("No correlated structured request log found")


def failure_message(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(failure_message(item) for item in error.exceptions)[:2000]
    return str(error) or type(error).__name__


def diagnostics(lab: Lab) -> str:
    try:
        lab.require_owned()
        return lab.kubectl(
            "-n", NAMESPACE, "get", "ksvc,revision,pods", "-o", "wide", timeout=10
        )
    except (LabError, OSError) as error:
        return f"Diagnostics unavailable: {error}"


class Options(argparse.Namespace):
    concurrency: int
    duration: int


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Observe one Knative Revision scaling through ingress"
    )
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument(
        "--duration", type=int, default=45, help="load seconds (10..90)"
    )
    options = parser.parse_args(namespace=Options())
    if not 10 <= options.concurrency <= 30 or not 10 <= options.duration <= 90:
        parser.error("concurrency must be 10..30 and duration 10..90 seconds")
    lab, report = Lab(), Report()
    output = lab.local / "runs" / f"{report.run_id}.json"
    try:
        lab.local.mkdir(exist_ok=True, mode=0o700)
        with (lab.local / "cluster.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            asyncio.run(experiment(lab, report, options.concurrency, options.duration))
    except (Exception, KeyboardInterrupt) as error:
        report.outcome = "failed"
        report.failure = failure_message(error)
        report.diagnostics = diagnostics(lab)
        print(f"FAIL: {report.failure}\n{report.diagnostics}", file=sys.stderr)
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n"
        )
        print(f"Run report: {output}", flush=True)
    return 0 if report.outcome == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
