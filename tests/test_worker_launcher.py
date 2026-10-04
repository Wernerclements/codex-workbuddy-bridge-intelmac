"""Run the actual wrapper with original native doubles and local Unix sockets."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from workbuddy_bridge_intelmac.owned_runtime import LAUNCHER


NODE = shutil.which("node")
HARNESS = Path(__file__).parent / "fixtures/launcher_harness.cjs"


@unittest.skipUnless(NODE, "Node.js unavailable: native bootstrap socket harness requires Node")
class LauncherTests(unittest.TestCase):
    def run_harness(self, mode):
        # AF_UNIX names must remain short enough for Intel macOS.
        with tempfile.TemporaryDirectory(prefix="wb-launch-test-", dir="/tmp") as directory:
            result = subprocess.run([NODE, str(HARNESS), str(LAUNCHER), directory, mode],
                                    capture_output=True, text=True, timeout=8)
        self.assertNotIn("SYNTHETIC-PROTECTION-MATERIAL", result.stdout + result.stderr)
        lines = [line[len("HARNESS:"):] for line in result.stdout.splitlines() if line.startswith("HARNESS:")]
        self.assertEqual(len(lines), 1, result.stdout + result.stderr)
        return result, json.loads(lines[0])

    def test_fields_bootstrap_and_matching_ready_complete_without_secret_output(self):
        result, audit = self.run_harness("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(audit["getter_called"])
        self.assertIs(audit["create_if_missing"], False)
        self.assertTrue(audit["payload_valid"])
        self.assertTrue(audit["ready_ack"])
        self.assertTrue(audit["marker"])
        self.assertEqual(audit["disposed"], 1)
        self.assertTrue(audit["reaper_safe_env"])
        self.assertTrue(audit["inline_product"])
        self.assertTrue(audit["key_erased"])

    def test_wrong_credential_ack_cannot_report_protected_ready(self):
        result, audit = self.run_harness("wrong_ack")
        self.assertEqual(result.returncode, 3)
        self.assertFalse(audit["marker"])
        self.assertEqual(audit["disposed"], 1)
        self.assertIn("INTELMAC_CREDENTIAL_BOOTSTRAP_FAILED", result.stderr)

    def test_invalid_product_snapshot_does_not_start_cli(self):
        for mode in ["malformed_product", "wrong_product_auth"]:
            with self.subTest(mode=mode):
                result, audit = self.run_harness(mode)
                self.assertEqual(result.returncode, 3)
                self.assertFalse(audit["cli_started"])
                self.assertFalse(audit["marker"])

    def test_missing_developer_key_erases_already_acquired_symmetric_key(self):
        result, audit = self.run_harness("missing_public_key")
        self.assertEqual(result.returncode, 3)
        self.assertTrue(audit["key_erased"])
        self.assertFalse(audit["cli_started"])

    def test_native_dispose_error_is_private_and_fallback_erases_key(self):
        result, audit = self.run_harness("dispose_throws")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(audit["key_erased"])
        self.assertTrue(audit["marker"])
        self.assertEqual(audit["disposed"], 1)

    def test_wrong_ready_pid_cannot_report_protected_ready(self):
        result, audit = self.run_harness("wrong_pid")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(audit["payload_valid"])
        self.assertTrue(audit["ready_rejected"])
        self.assertFalse(audit["marker"])

    def test_wrong_ready_token_cannot_report_protected_ready(self):
        result, audit = self.run_harness("wrong_token")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(audit["payload_valid"])
        self.assertTrue(audit["ready_rejected"])
        self.assertFalse(audit["marker"])

    def test_missing_keyblob_is_not_created_and_cli_does_not_start(self):
        result, audit = self.run_harness("missing_keyblob")
        self.assertEqual(result.returncode, 3)
        self.assertIs(audit["create_if_missing"], False)
        self.assertFalse(audit["cli_started"])
        self.assertFalse(audit["marker"])


if __name__ == "__main__":
    unittest.main()
