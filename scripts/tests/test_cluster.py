"""Offline lifecycle safety checks: no Docker daemon or Kubernetes API calls."""

import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.cluster.artifacts import Artifact, fetch
from scripts.cluster.operations import (
    cleanup,
    deploy,
    ensure_cluster,
    ready_revision,
    render_service,
    wait_for_config_webhook,
)
from scripts.cluster.runtime import CLUSTER, Lab, LabError, Receipt, digest, run

NODE_ID = "a" * 64
SYSTEM_UID = "12345678-1234-1234-1234-123456789abc"


class OwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.lab = Lab(Path(self.directory.name))
        self.lab.state.mkdir(parents=True)

    def write_receipt(self) -> None:
        self.lab.kubeconfig.write_text("fixture kubeconfig")
        (self.lab.state / "ownership.txt").write_text(
            f"{NODE_ID}\n{digest(self.lab.kubeconfig)}\n{SYSTEM_UID}\n"
        )

    def test_existing_unowned_cluster_is_never_adopted(self) -> None:
        with (
            patch.object(Lab, "nodes", return_value=[NODE_ID]),
            patch.object(Lab, "kind") as kind,
        ):
            with self.assertRaisesRegex(LabError, "unowned"):
                ensure_cluster(self.lab)
            kind.assert_not_called()

    def test_owned_setup_rerun_does_not_create_a_cluster(self) -> None:
        self.write_receipt()
        with (
            patch.object(Lab, "nodes", return_value=[NODE_ID]),
            patch.object(Lab, "system_uid", return_value=SYSTEM_UID),
            patch.object(Lab, "kind") as kind,
        ):
            ensure_cluster(self.lab)
            kind.assert_not_called()

    def test_missing_receipt_refuses_cleanup_of_existing_cluster(self) -> None:
        with (
            patch.object(Lab, "nodes", return_value=[NODE_ID]),
            patch.object(Lab, "kind") as kind,
        ):
            with self.assertRaisesRegex(LabError, "receipt missing"):
                cleanup(self.lab)
            kind.assert_not_called()

    def test_replaced_or_extra_node_refuses_cleanup(self) -> None:
        self.write_receipt()
        for nodes in (["b" * 64], [NODE_ID, "b" * 64]):
            with (
                self.subTest(nodes=nodes),
                patch.object(Lab, "nodes", return_value=nodes),
                patch.object(Lab, "kind") as kind,
            ):
                with self.assertRaisesRegex(LabError, "identity changed"):
                    cleanup(self.lab)
                kind.assert_not_called()
                self.assertTrue(self.lab.kubeconfig.exists())

    def test_modified_kubeconfig_is_rejected_before_api_access(self) -> None:
        self.write_receipt()
        self.lab.kubeconfig.write_text("another cluster")
        with (
            patch.object(Lab, "nodes", return_value=[NODE_ID]),
            patch.object(Lab, "system_uid") as api,
        ):
            with self.assertRaisesRegex(LabError, "kubeconfig changed"):
                self.lab.require_owned()
            api.assert_not_called()

    def test_changed_api_identity_refuses_deploy_before_image_mutation(self) -> None:
        self.write_receipt()
        with (
            patch.object(Lab, "nodes", return_value=[NODE_ID]),
            patch.object(Lab, "system_uid", return_value="different-cluster"),
            patch("scripts.cluster.operations.run") as docker,
        ):
            with self.assertRaisesRegex(LabError, "Kubernetes cluster identity"):
                deploy(self.lab, "image:local")
            docker.assert_not_called()

    def test_cleanup_uses_owned_name_and_retains_unrelated_files(self) -> None:
        self.write_receipt()
        unrelated = self.lab.local / "keep.txt"
        unrelated.write_text("keep")
        with (
            patch.object(Lab, "nodes", side_effect=[[NODE_ID], [NODE_ID], []]),
            patch.object(Lab, "kind") as kind,
        ):
            cleanup(self.lab)
            self.assertEqual(kind.call_count, 1)
            self.assertEqual(
                kind.call_args.args[:5],
                ("delete", "cluster", "--name", CLUSTER, "--kubeconfig"),
            )
            self.assertEqual(kind.call_args.kwargs, {"timeout": 120})
        self.assertFalse((self.lab.state / "ownership.txt").exists())
        self.assertFalse(self.lab.kubeconfig.exists())
        self.assertEqual(unrelated.read_text(), "keep")

    def test_failed_delete_retains_recovery_state(self) -> None:
        self.write_receipt()
        with (
            patch.object(Lab, "nodes", return_value=[NODE_ID]),
            patch.object(Lab, "kind", side_effect=LabError("delete failed")),
            self.assertRaisesRegex(LabError, "delete failed"),
        ):
            cleanup(self.lab)
        self.assertTrue((self.lab.state / "ownership.txt").exists())

    def test_delete_uses_temporary_kubeconfig_and_preserves_state_on_failure(
        self,
    ) -> None:
        self.write_receipt()
        receipt = Receipt.read(self.lab.state / "ownership.txt")

        def fail_after_kubeconfig_update(*args: str, timeout: int = 60) -> str:
            self.assertIn("--kubeconfig", args)
            copy = Path(args[args.index("--kubeconfig") + 1])
            self.assertNotEqual(copy, self.lab.kubeconfig)
            self.assertEqual(copy.parent.parent, self.lab.state)
            self.assertEqual(digest(copy), receipt.kubeconfig_hash)
            copy.write_text(
                "kind removed its context before failing to delete the node"
            )
            raise LabError("partial deletion")

        with (
            patch.object(Lab, "nodes", return_value=[NODE_ID]),
            patch.object(Lab, "kind", side_effect=fail_after_kubeconfig_update),
            self.assertRaisesRegex(LabError, "partial deletion"),
        ):
            cleanup(self.lab)
        self.assertEqual(digest(self.lab.kubeconfig), receipt.kubeconfig_hash)
        self.assertEqual(Receipt.read(self.lab.state / "ownership.txt"), receipt)
        self.assertEqual(
            sorted(p.name for p in self.lab.state.iterdir()),
            ["kubeconfig", "ownership.txt"],
        )

    def test_missing_node_does_not_authorize_removing_changed_kubeconfig(self) -> None:
        self.write_receipt()
        self.lab.kubeconfig.write_text("changed")
        with (
            patch.object(Lab, "nodes", return_value=[]),
            self.assertRaisesRegex(LabError, "kubeconfig changed"),
        ):
            cleanup(self.lab)
        self.assertEqual(self.lab.kubeconfig.read_text(), "changed")

    def test_cleanup_rerun_without_resources_is_safe(self) -> None:
        with (
            patch.object(Lab, "nodes", return_value=[]),
            patch.object(Lab, "kind") as kind,
        ):
            cleanup(self.lab)
            kind.assert_not_called()

    def test_partial_creation_without_receipt_refuses_retry(self) -> None:
        self.lab.kubeconfig.write_text("partial")
        with (
            patch.object(Lab, "nodes", return_value=[]),
            patch.object(Lab, "kind", return_value="") as kind,
        ):
            with self.assertRaisesRegex(LabError, "Unrecorded kubeconfig"):
                ensure_cluster(self.lab)
            kind.assert_called_once_with("get", "clusters")

    def test_all_api_calls_choose_project_kubeconfig_and_context(self) -> None:
        with patch("scripts.cluster.runtime.run", return_value="ok") as command:
            self.lab.kubectl("get", "nodes")
        command.assert_called_once_with(
            str(self.lab.local / "bin/kubectl"),
            "--kubeconfig",
            str(self.lab.kubeconfig),
            "--context",
            "kind-serverless-lab",
            "--request-timeout=30s",
            "get",
            "nodes",
            timeout=60,
            input_text=None,
        )

    def test_corrupt_receipt_is_not_trusted(self) -> None:
        path = self.lab.state / "ownership.txt"
        for value in ("", "a\nb\nc\n", f"{NODE_ID}\n{NODE_ID}\nwrong\n"):
            path.write_text(value)
            with self.subTest(value=value), self.assertRaises(LabError):
                Receipt.read(path)


class ArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.destination = Path(self.directory.name) / "tool"
        self.artifact = Artifact(
            "kind",
            "https://example.invalid/tool",
            hashlib.sha256(b"trusted").hexdigest(),
        )

    def test_mismatched_download_is_not_installed(self) -> None:
        with (
            patch("urllib.request.urlopen", return_value=io.BytesIO(b"corrupt")),
            self.assertRaisesRegex(LabError, "Checksum mismatch"),
        ):
            fetch(self.artifact, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.destination.with_suffix(".download").exists())

    def test_valid_cache_avoids_network(self) -> None:
        self.destination.write_bytes(b"trusted")
        with patch("urllib.request.urlopen") as network:
            fetch(self.artifact, self.destination)
            network.assert_not_called()

    def test_corrupted_cache_fails_closed(self) -> None:
        self.destination.write_bytes(b"corrupt")
        with patch("urllib.request.urlopen") as network:
            with self.assertRaisesRegex(LabError, "Checksum mismatch"):
                fetch(self.artifact, self.destination)
            network.assert_not_called()

    def test_valid_download_is_atomically_installed_executable(self) -> None:
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"trusted")):
            fetch(self.artifact, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"trusted")
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o755)


class CommandTests(unittest.TestCase):
    def test_config_webhook_startup_refusal_is_retried(self) -> None:
        with (
            patch.object(
                Lab,
                "kubectl",
                side_effect=[
                    LabError("failed calling webhook: connect: connection refused"),
                    "ok",
                ],
            ) as command,
            patch("scripts.cluster.operations.time.sleep"),
        ):
            wait_for_config_webhook(Lab())
        self.assertEqual(command.call_count, 2)
        self.assertIn("--dry-run=server", command.call_args.args)

    def test_invalid_configuration_is_not_retried(self) -> None:
        with (
            patch.object(
                Lab, "kubectl", side_effect=LabError("admission denied invalid value")
            ) as command,
            self.assertRaisesRegex(LabError, "invalid value"),
        ):
            wait_for_config_webhook(Lab())
        self.assertEqual(command.call_count, 1)

    def test_config_webhook_wait_has_deadline(self) -> None:
        with (
            patch.object(
                Lab,
                "kubectl",
                side_effect=LabError("failed calling webhook: connection refused"),
            ),
            patch("scripts.cluster.operations.time.monotonic", side_effect=[0, 61]),
            self.assertRaisesRegex(LabError, "connection refused"),
        ):
            wait_for_config_webhook(Lab())

    def test_stale_ready_revision_is_not_current_deployment_success(self) -> None:
        for identity in (
            "2 1 text-analysis-00002 text-analysis-00001",
            "2 2 text-analysis-00002 text-analysis-00001",
            "",
        ):
            with (
                self.subTest(identity=identity),
                patch.object(Lab, "kubectl", return_value=identity),
                self.assertRaisesRegex(LabError, "not fully reconciled"),
            ):
                ready_revision(Lab())

    def test_command_timeout_is_reported_as_failure(self) -> None:
        with self.assertRaisesRegex(LabError, "exceeded 1s"):
            run(sys.executable, "-c", "import time; time.sleep(30)", timeout=1)

    def test_failed_command_is_not_empty_success(self) -> None:
        with self.assertRaisesRegex(LabError, "failed"):
            run(sys.executable, "-c", "raise SystemExit(2)")

    def test_invalid_image_tag_cannot_inject_manifest_fields(self) -> None:
        with self.assertRaisesRegex(LabError, "image-ID-derived"):
            render_service(Path("unused"), "image\nspec: malicious")


if __name__ == "__main__":
    unittest.main()
