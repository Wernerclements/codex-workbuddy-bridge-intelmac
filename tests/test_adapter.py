"""Ownership and recovery tests; every WorkBuddy operation is mocked."""
from pathlib import Path
import unittest
from unittest.mock import patch

from workbuddy_bridge_intelmac import adapter
from workbuddy_bridge_intelmac.discovery import WORKER_PREFIX, WorkBuddyError


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.task = "11111111-2222-4333-8444-555555555555"
        self.worker = WORKER_PREFIX + self.task
        self.path = Path("/tmp/sidecar-aabb.sock")
        self.runtime = {"task_id": self.task, "worker_id": self.worker,
                        "socket_path": str(self.path), "sidecar_pid": 123}
        self.socket_patch = patch.object(adapter, "find_sockets", return_value=[(self.path, 123)])
        self.socket_patch.start()
        self.addCleanup(self.socket_patch.stop)

    def test_pending_worker_hidden_from_list_is_reconnected_and_owned_killed(self):
        calls = []

        def rpc(path, method, params=None):
            calls.append((method, params))
            if method == "session.list":
                return []
            if method == "session.reconnect":
                return {"runtimeId": "pending-owned-runtime"}
            return {"ok": True}

        with patch.object(adapter, "rpc", side_effect=rpc):
            self.assertTrue(adapter.recover_runtime(self.runtime)["cleaned"])
        self.assertEqual([method for method, _ in calls],
                         ["session.list", "session.reconnect", "session.kill", "session.list"])
        self.assertEqual(calls[2][1], {"sessionId": self.worker,
                                      "expectedRuntimeId": "pending-owned-runtime"})

    def test_reconnected_runtime_mismatch_never_kills(self):
        self.runtime["runtime_id"] = "previous-runtime"
        with patch.object(adapter, "rpc", side_effect=[[], {"runtimeId": "replacement-runtime"}]) as rpc:
            result = adapter.recover_runtime(self.runtime)
        self.assertFalse(result["cleaned"])
        self.assertNotIn("session.kill", [call.args[1] for call in rpc.call_args_list])

    def test_pid_mismatch_never_kills(self):
        self.runtime.update(pid=456, runtime_id="previous-runtime")
        with patch.object(adapter, "rpc", return_value=[{"sessionId": self.worker, "pid": 789}]) as rpc:
            result = adapter.recover_runtime(self.runtime)
        self.assertFalse(result["cleaned"])
        self.assertEqual(rpc.call_count, 1)

    def test_worker_replacement_still_listed_is_not_declared_cleaned(self):
        self.runtime.update(pid=456, runtime_id="previous-runtime")
        entry = {"sessionId": self.worker, "pid": 456}
        with patch.object(adapter, "rpc", side_effect=[[entry], {"ok": True}, [entry]]) as rpc:
            result = adapter.recover_runtime(self.runtime)
        self.assertFalse(result["cleaned"])
        self.assertEqual(rpc.call_args_list[1].args[2]["expectedRuntimeId"], "previous-runtime")

    def test_not_found_reconnect_is_cleaned_without_kill(self):
        with patch.object(adapter, "rpc", side_effect=[[], WorkBuddyError("missing", kind="not_found")]) as rpc:
            result = adapter.recover_runtime(self.runtime)
        self.assertTrue(result["cleaned"])
        self.assertEqual(rpc.call_count, 2)

    def test_failed_reconnect_remains_an_error(self):
        with patch.object(adapter, "rpc", side_effect=[[], WorkBuddyError("control unavailable")]):
            with self.assertRaises(WorkBuddyError):
                adapter.recover_runtime(self.runtime)

    def test_foreign_worker_is_rejected_before_any_control_request(self):
        self.runtime["worker_id"] = "__workbuddy_cli_host__existing"
        with patch.object(adapter, "rpc") as rpc:
            self.assertFalse(adapter.recover_runtime(self.runtime)["cleaned"])
            rpc.assert_not_called()

    def test_original_sidecar_pid_mismatch_never_contacts_socket(self):
        self.runtime["sidecar_pid"] = 999
        with patch.object(adapter, "rpc") as rpc:
            self.assertFalse(adapter.recover_runtime(self.runtime)["cleaned"])
            rpc.assert_not_called()

    def test_adapter_accepts_manager_uuid_and_rejects_arbitrary_worker_id(self):
        instance = adapter.WorkBuddyAdapter(self.task, "/tmp", lambda event: None, lambda request: "deny")
        self.assertEqual(instance.task_id, self.task)
        with self.assertRaises(WorkBuddyError):
            adapter.WorkBuddyAdapter("another-desktop-session", "/tmp", lambda event: None,
                                     lambda request: "deny")


if __name__ == "__main__":
    unittest.main()
