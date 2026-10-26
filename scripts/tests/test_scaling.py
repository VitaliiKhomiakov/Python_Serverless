"""Offline checks for false-zero protection and bounded experiment failures."""

import asyncio
import json
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx2

from scripts.checks import knative_state as state
from scripts.checks import scaling
from scripts.cluster import __main__ as cluster_cli
from scripts.cluster.runtime import Lab, LabError

IDENTITY = state.Identity(
    "service-uid",
    1,
    "text-analysis-00001",
    "revision-uid",
    "deployment",
    "deployment-uid",
    "serving.knative.dev/revisionUID=revision-uid",
    "image",
)
ZERO = state.Snapshot(0, 0, 0, 0, ())


def resources() -> list[dict[str, object]]:
    revision_labels = {
        "serving.knative.dev/revision": IDENTITY.revision,
        "serving.knative.dev/revisionUID": IDENTITY.revision_uid,
        "serving.knative.dev/service": "text-analysis",
    }
    service: dict[str, object] = {
        "metadata": {"uid": IDENTITY.service_uid, "generation": 1},
        "status": {
            "observedGeneration": 1,
            "latestReadyRevisionName": IDENTITY.revision,
            "latestCreatedRevisionName": IDENTITY.revision,
            "conditions": [{"type": "Ready", "status": "True"}],
            "traffic": [{"revisionName": IDENTITY.revision, "percent": 100}],
        },
    }
    revision: dict[str, object] = {
        "metadata": {
            "uid": IDENTITY.revision_uid,
            "annotations": {
                f"autoscaling.knative.dev/{key}": value
                for key, value in {
                    "class": "kpa.autoscaling.knative.dev",
                    "min-scale": "0",
                    "max-scale": "3",
                    "target": "5",
                    "target-utilization-percentage": "100",
                }.items()
            },
        },
        "spec": {
            "containerConcurrency": 10,
            "containers": [
                {
                    "name": "app",
                    "image": "image",
                }
            ],
        },
    }
    deployment: dict[str, object] = {
        "metadata": {
            "name": IDENTITY.deployment,
            "uid": IDENTITY.deployment_uid,
            "ownerReferences": [
                {"uid": IDENTITY.revision_uid, "kind": "Revision", "controller": True}
            ],
        },
        "spec": {
            "selector": {
                "matchLabels": {
                    "serving.knative.dev/revisionUID": IDENTITY.revision_uid
                }
            },
            "template": {"metadata": {"labels": revision_labels}},
        },
    }
    return [service, revision, {"items": [deployment]}]


class StateTests(unittest.TestCase):
    def test_selector_must_match_owned_revision_and_pod_template(self) -> None:
        with patch.object(state, "read", side_effect=resources()):
            self.assertEqual(state.capture(Lab(), time.monotonic() + 10), IDENTITY)
        values = resources()
        deployment = state.items(values[2]["items"])[0]
        spec = state.fields(deployment, "spec")
        spec["selector"] = {"matchLabels": {"serving.knative.dev/revisionUID": "wrong"}}
        deployment["spec"] = spec
        values[2] = {"items": [deployment]}
        with (
            patch.object(state, "read", side_effect=values),
            self.assertRaisesRegex(LabError, "selector"),
        ):
            state.capture(Lab(), time.monotonic() + 10)

    def test_api_failure_and_identity_drift_never_mean_zero(self) -> None:
        for failure in (
            LabError("API unavailable"),
            replace(IDENTITY, revision_uid="replaced"),
        ):
            with (
                self.subTest(failure=failure),
                patch.object(
                    state,
                    "capture",
                    side_effect=failure if isinstance(failure, Exception) else None,
                    return_value=failure,
                ),
                patch.object(state, "read") as read,
            ):
                with self.assertRaises(LabError):
                    state.observe(Lab(), IDENTITY, 0, time.monotonic() + 10)
                read.assert_not_called()

    def test_other_service_pods_prevent_zero_even_with_empty_revision(self) -> None:
        with (
            patch.object(state, "capture", return_value=IDENTITY),
            patch.object(
                state,
                "read",
                side_effect=[
                    {"items": []},
                    {"items": [{"metadata": {"deletionTimestamp": "now"}}]},
                ],
            ),
        ):
            snapshot = state.observe(Lab(), IDENTITY, 0, time.monotonic() + 10)
        self.assertFalse(scaling.no_application_pods(snapshot))

    def test_terminating_pod_counts_as_existing_but_not_ready(self) -> None:
        pod = {
            "metadata": {
                "name": "pod",
                "deletionTimestamp": "now",
                "labels": {
                    "serving.knative.dev/revision": IDENTITY.revision,
                    "serving.knative.dev/service": "text-analysis",
                },
            },
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }
        with (
            patch.object(state, "capture", return_value=IDENTITY),
            patch.object(state, "read", return_value={"items": [pod]}),
        ):
            snapshot = state.observe(Lab(), IDENTITY, 0, time.monotonic() + 10)
        self.assertEqual((snapshot.revision_pods, snapshot.ready_pods), (1, 0))
        self.assertFalse(scaling.no_application_pods(snapshot))

    def test_removed_delay_option_is_rejected_before_cluster_access(self) -> None:
        with (
            patch("sys.argv", ["cluster", "deploy", "--demo-delay-ms", "500"]),
            patch("scripts.cluster.__main__.Lab") as lab,
            self.assertRaises(SystemExit) as caught,
        ):
            cluster_cli.main()
        self.assertEqual(caught.exception.code, 2)
        lab.assert_not_called()

    def test_correlation_selects_completion_from_mixed_server_logs(self) -> None:
        sample = scaling.ResponseSample(1.0, "pod-one", "request-one")
        context = {
            "request_id": sample.request_id,
            "instance_id": sample.instance_id,
            "revision": IDENTITY.revision,
        }
        access = {**context, "event": "http.access", "status": 200}
        completed = {
            **context,
            "event": "request.completed",
            "status": 200,
            "duration_ms": 1.0,
            "outcome": "completed",
        }
        logs = "\n".join(
            (
                "INFO uvicorn.error: Application startup complete.",
                "INFO uvicorn.access: HTTP/1.1 200",
                json.dumps(access),
                json.dumps(completed),
            )
        )
        with patch.object(Lab, "kubectl", return_value=logs):
            self.assertEqual(scaling.correlated_log(Lab(), sample, IDENTITY), completed)

    def test_diagnostics_refuses_unowned_api(self) -> None:
        with (
            patch.object(Lab, "require_owned", side_effect=LabError("unowned")),
            patch.object(Lab, "kubectl") as api,
        ):
            self.assertIn("unowned", scaling.diagnostics(Lab()))
            api.assert_not_called()


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_observation_never_sends_http(self) -> None:
        with (
            patch.object(scaling, "observe", return_value=ZERO),
            patch.object(scaling, "request") as request,
        ):
            result = await scaling.wait_for_pods(
                Lab(),
                IDENTITY,
                scaling.Report(),
                0,
                "idle",
                scaling.no_application_pods,
            )
            self.assertEqual(result, ZERO)
            request.assert_not_called()

    async def test_http_rejects_wrong_revision_and_request_timeout(self) -> None:
        async def wrong_revision(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200,
                headers={
                    "x-instance-id": "pod",
                    "x-revision": "other",
                    "x-request-id": request.headers["x-request-id"],
                },
                json={
                    "characters": 8,
                    "words": 2,
                    "top_words": [{"word": "кот", "count": 2}],
                },
            )

        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(wrong_revision)
        ) as client:
            with self.assertRaisesRegex(LabError, "different Revision"):
                await scaling.request(client, IDENTITY)
        cancelled = asyncio.Event()

        async def stall(request: httpx2.Request) -> httpx2.Response:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            raise AssertionError("unreachable")

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(stall)) as client:
            with self.assertRaises(TimeoutError):
                await scaling.request(client, IDENTITY, timeout=0.02)
        self.assertTrue(cancelled.is_set())

    async def test_load_failure_cancels_and_joins_other_workers(self) -> None:
        active = 0
        both_started = asyncio.Event()

        async def failing_request(
            client: httpx2.AsyncClient, identity: state.Identity
        ) -> scaling.ResponseSample:
            nonlocal active
            active += 1
            ordinal = active
            if active == 2:
                both_started.set()
            try:
                await both_started.wait()
                if ordinal == 1:
                    raise LabError("load failure")
                await asyncio.Event().wait()
                raise AssertionError("unreachable")
            finally:
                active -= 1

        async with httpx2.AsyncClient() as client:
            with (
                patch.object(scaling, "request", side_effect=failing_request),
                patch.object(scaling, "observe", return_value=ZERO),
                self.assertRaises(ExceptionGroup) as caught,
            ):
                await asyncio.wait_for(
                    scaling.run_load(
                        Lab(), IDENTITY, client, scaling.Report(), 0, 2, 10
                    ),
                    2,
                )
        self.assertIn("load failure", scaling.failure_message(caught.exception))
        self.assertEqual(active, 0)


if __name__ == "__main__":
    unittest.main()
