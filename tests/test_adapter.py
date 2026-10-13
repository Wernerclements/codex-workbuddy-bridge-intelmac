"""Ownership and recovery tests; every WorkBuddy operation is mocked."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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
        def control(path, method, params=None):
            return [entry] if method == "session.list" else {"ok": True}

        with patch.object(adapter, "rpc", side_effect=control) as rpc, \
                patch.object(adapter.time, "monotonic", side_effect=range(10)), \
                patch.object(adapter.time, "sleep"):
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
        self.assertEqual(instance.model, "deepseek-v4.1-flash")
        self.assertIsNone(instance.context_tokens)
        with self.assertRaises(WorkBuddyError):
            adapter.WorkBuddyAdapter("another-desktop-session", "/tmp", lambda event: None,
                                     lambda request: "deny")

    def test_run_uses_lite_app_bundle_auto_port_and_explicit_safe_permissions(self):
        desktop = SimpleNamespace(socket_path=self.path, sidecar_pid=123, version="5.6.2")
        app = Path("/SyntheticApps/WorkBuddy.app")
        info = {"executable": app / "Contents/MacOS/Electron",
                "cli": app / "Contents/Resources/app.asar.unpacked/cli/bin/codebuddy",
                "cli_entry_present": True}
        events = []
        human_permission = Mock(return_value="deny")
        instance = adapter.WorkBuddyAdapter(self.task, "/tmp", events.append, human_permission,
                                             model="glm-5.3-flash", context_tokens=1000000)
        acp = Mock()
        acp.new_session.return_value = "own-acp-session"
        acp.select_model.return_value = {"id": "glm-5.3-flash", "name": "GLM-5.3-Flash",
                                         "max_input_tokens": 1000000, "selection_confirmed": True}
        acp.select_context.return_value = {"context_tokens": 1000000, "context_choices": [],
                                           "context_source": "advertised_model_limit",
                                           "context_requested": 1000000}
        acp.prompt.return_value = {"session_id": "own-acp-session", "answer": "done",
                                   "stop_reason": "end_turn"}
        calls = []

        def control(path, method, params=None, **kwargs):
            calls.append((path, method, params, kwargs))
            if method == "session.create":
                return {"sessionId": self.worker, "runtimeId": "owned-runtime", "pid": 456,
                        "acpEndpoint": "http://127.0.0.1:3456/api/v1/acp"}
            if method == "session.capture":
                return {"text": "http://127.0.0.1:3456/?password=SYNTHETIC-SECRET"}
            self.fail(f"Unexpected control method: {method}")

        with patch.object(adapter, "discover", return_value=desktop) as discover, \
                patch.object(adapter, "app_info", return_value=info), \
                patch.object(adapter, "rpc", side_effect=control), \
                patch.object(adapter, "auth_status", return_value={"authenticated": True, "auth_enabled": True}), \
                patch.object(adapter, "AcpClient", return_value=acp) as client:
            result = instance.run("Perform a synthetic task")

        self.assertEqual(result["stop_reason"], "end_turn")
        discover.assert_called_once_with(for_write=True)
        request = calls[0][2]
        self.assertEqual(calls[0][1], "session.create")
        self.assertEqual(request["command"], str(app / "Contents/MacOS/Electron"))
        self.assertEqual(request["args"][0], str(info["cli"]))
        self.assertEqual(request["args"][request["args"].index("--model") + 1], "glm-5.3-flash")
        self.assertEqual(request["args"][request["args"].index("--autocompact") + 1], "1000000")
        self.assertEqual(request["port"], 0)
        self.assertEqual(request["env"]["CODEBUDDY_FORCE_LITE_WB_BUNDLE"], "1")
        self.assertEqual(request["env"]["ELECTRON_RUN_AS_NODE"], "1")
        for option in ["--permission-mode", "--permission-mode-before-plan"]:
            self.assertEqual(request["args"][request["args"].index(option) + 1], "default")
        self.assertNotIn("fullAccess", request["args"])
        self.assertNotIn("bypassPermissions", request["args"])
        self.assertGreaterEqual(calls[0][3]["timeout"], 195)
        self.assertEqual(client.call_args.args[3], human_permission)
        acp.connect.assert_called_once()
        acp.new_session.assert_called_once_with(instance.cwd)
        acp.request.assert_called_once_with("session/set_config_option", {
            "sessionId": "own-acp-session", "configId": "mode", "value": "default"})
        acp.select_model.assert_called_once_with("glm-5.3-flash")
        acp.select_context.assert_called_once_with(1000000, acp.select_model.return_value)
        methods = [call[0] for call in acp.method_calls]
        self.assertLess(methods.index("select_model"), methods.index("select_context"))
        self.assertLess(methods.index("select_context"), methods.index("prompt"))
        model_event = next(event for event in events if event["type"] == "model")
        self.assertEqual(model_event["model"]["id"], "glm-5.3-flash")
        self.assertEqual(model_event["model"]["context_tokens"], 1000000)
        self.assertEqual(model_event["model"]["auto_compact_window_requested"], 1000000)
        self.assertNotIn("SYNTHETIC-SECRET", repr(events))
        human_permission.assert_not_called()


class ColdStartTests(unittest.TestCase):
    def cold_fixture(self):
        events = []
        instance = adapter.WorkBuddyAdapter("11111111-2222-4333-8444-555555555555",
                                           "/tmp", events.append, Mock(),
                                           model="glm-5.3-flash", context_tokens=1000000)
        info = {"executable": Path("/synthetic/WorkBuddy.app/Contents/MacOS/Electron"),
                "cli": Path("/synthetic/WorkBuddy.app/Contents/Resources/app.asar/cli/bin/codebuddy"),
                "cli_entry_present": True}
        runtime = Mock()
        runtime.runtime = {"launcher": "owned", "task_id": instance.task_id, "pid": 4321}
        runtime.start.return_value = ("http://127.0.0.1:3456/api/v1/acp", "SYNTHETIC-GATEWAY-SECRET")
        runtime.lost.is_set.return_value = False
        runtime._closed.is_set.return_value = False
        runtime.stop.return_value = {"cleaned": True}
        acp = Mock()
        acp.new_session.return_value = "synthetic-cold-session"
        acp.select_model.return_value = {"id": "glm-5.3-flash", "selection_confirmed": True,
                                         "max_input_tokens": 1000000}
        acp.select_context.return_value = {"context_tokens": 1000000}
        acp.prompt.return_value = {"stop_reason": "end_turn", "answer": "done"}
        return instance, events, info, runtime, acp

    def test_cold_launch_omits_early_model_and_selects_advertised_model_before_prompt(self):
        instance, events, info, runtime, acp = self.cold_fixture()
        with patch.object(adapter, "discover", side_effect=WorkBuddyError("cold", kind="cold_start")), \
                patch.object(adapter, "app_info", return_value=info), \
                patch.object(adapter, "OwnedRuntime", return_value=runtime) as launch, \
                patch.object(adapter, "AcpClient", return_value=acp), patch.object(adapter, "rpc") as rpc:
            result = instance.run("synthetic prompt")
            cleanup = instance.close()
        args = launch.call_args.args[2]
        self.assertEqual(launch.call_args.kwargs["model"], "glm-5.3-flash")
        self.assertEqual(args[0], str(info["cli"]))
        self.assertNotIn("--model", args)
        self.assertNotIn("glm-5.3-flash", args)
        self.assertEqual(args[args.index("--autocompact") + 1], "1000000")
        for option in ["--permission-mode", "--permission-mode-before-plan"]:
            self.assertEqual(args[args.index(option) + 1], "default")
        acp.request.assert_called_once_with("session/set_config_option", {
            "sessionId": "synthetic-cold-session", "configId": "mode", "value": "default"})
        acp.select_model.assert_called_once_with("glm-5.3-flash")
        acp.select_context.assert_called_once_with(1000000, acp.select_model.return_value)
        methods = [call[0] for call in acp.method_calls]
        self.assertLess(methods.index("request"), methods.index("select_model"))
        self.assertLess(methods.index("select_model"), methods.index("select_context"))
        self.assertLess(methods.index("select_context"), methods.index("prompt"))
        acp.prompt.assert_called_once()
        self.assertNotIn("SYNTHETIC-GATEWAY-SECRET", repr(events))
        self.assertEqual(result["stop_reason"], "end_turn")
        self.assertEqual(cleanup, {"cleaned": True})
        runtime.stop.assert_called_once()
        rpc.assert_not_called()

    def test_unadvertised_cold_model_never_submits_prompt(self):
        instance, events, info, runtime, acp = self.cold_fixture()
        acp.select_model.side_effect = WorkBuddyError("model not advertised")
        with patch.object(adapter, "discover", side_effect=WorkBuddyError("cold", kind="cold_start")), \
                patch.object(adapter, "app_info", return_value=info), \
                patch.object(adapter, "OwnedRuntime", return_value=runtime), \
                patch.object(adapter, "AcpClient", return_value=acp):
            with self.assertRaises(WorkBuddyError):
                instance.run("must not submit")
            self.assertEqual(instance.close(), {"cleaned": True})
        acp.select_context.assert_not_called()
        acp.prompt.assert_not_called()
        self.assertNotIn("running", [event["type"] for event in events])
        runtime.stop.assert_called_once()

    def test_unsupported_cold_context_never_submits_prompt(self):
        instance, events, info, runtime, acp = self.cold_fixture()
        acp.select_context.side_effect = WorkBuddyError("context exceeds advertised budget")
        with patch.object(adapter, "discover", side_effect=WorkBuddyError("cold", kind="cold_start")), \
                patch.object(adapter, "app_info", return_value=info), \
                patch.object(adapter, "OwnedRuntime", return_value=runtime), \
                patch.object(adapter, "AcpClient", return_value=acp):
            with self.assertRaises(WorkBuddyError):
                instance.run("must not submit")
            self.assertEqual(instance.close(), {"cleaned": True})
        acp.select_model.assert_called_once_with("glm-5.3-flash")
        acp.prompt.assert_not_called()
        self.assertNotIn("running", [event["type"] for event in events])
        runtime.stop.assert_called_once()

    def test_cancel_or_runtime_loss_in_running_callback_cannot_submit_prompt(self):
        for action in ["cancel", "lost"]:
            instance, events, info, runtime, acp = self.cold_fixture()
            def callback(event):
                events.append(event)
                if event["type"] == "running":
                    if action == "cancel":
                        instance._cancel.set()
                    else:
                        runtime.lost.is_set.return_value = True
            instance.on_event = callback
            with self.subTest(action=action), \
                    patch.object(adapter, "discover", side_effect=WorkBuddyError("cold", kind="cold_start")), \
                    patch.object(adapter, "app_info", return_value=info), \
                    patch.object(adapter, "OwnedRuntime", return_value=runtime), \
                    patch.object(adapter, "AcpClient", return_value=acp):
                with self.assertRaises(WorkBuddyError):
                    instance.run("must not submit")
                instance.close()
            acp.prompt.assert_not_called()

    def test_health_is_read_only_and_does_not_claim_cold_account_authentication(self):
        with patch.object(adapter, "discover", side_effect=WorkBuddyError("cold", kind="cold_start")), \
                patch.object(adapter, "gui_identity", return_value={"pid": 123, "start": "synthetic", "version": "5.7.7"}), \
                patch.object(adapter, "OwnedRuntime") as launch:
            result = adapter.health()
        self.assertTrue(result["available"])
        self.assertTrue(result["read_only"])
        self.assertTrue(result["ready_for_task"])
        self.assertFalse(result["runtime_ready"])
        self.assertIsNone(result["authenticated"])
        self.assertEqual(result["execution_mode"], "owned_cold_start")
        self.assertEqual(result["bridge_version"], adapter.__version__)
        self.assertEqual(result["version"], "5.7.7")
        launch.assert_not_called()

    def test_unavailable_or_unsupported_channel_cannot_fall_back_to_own_launcher(self):
        for kind in ["unavailable", ""]:
            with self.subTest(kind=kind), \
                    patch.object(adapter, "discover", side_effect=WorkBuddyError("guard", kind=kind)), \
                    patch.object(adapter, "gui_identity") as gui, \
                    patch.object(adapter, "OwnedRuntime") as launch:
                self.assertFalse(adapter.health()["available"])
                instance = adapter.WorkBuddyAdapter("11111111-2222-4333-8444-555555555555",
                                                   "/tmp", Mock(), Mock())
                with self.assertRaises(WorkBuddyError):
                    instance.run("synthetic task")
                gui.assert_not_called()
                launch.assert_not_called()

    def test_missing_gui_remains_unavailable_without_launching(self):
        with patch.object(adapter, "discover", side_effect=WorkBuddyError("cold", kind="cold_start")), \
                patch.object(adapter, "gui_identity", side_effect=WorkBuddyError("missing GUI")), \
                patch.object(adapter, "OwnedRuntime") as launch:
            self.assertFalse(adapter.health()["available"])
        launch.assert_not_called()

    def test_owned_recovery_delegates_to_identity_guarded_cleaner(self):
        runtime = {"launcher": "owned", "task_id": "synthetic"}
        with patch.object(adapter, "recover_owned", return_value={"cleaned": False}) as recover, \
                patch.object(adapter, "find_sockets") as sockets, patch.object(adapter, "rpc") as rpc:
            self.assertFalse(adapter.recover_runtime(runtime)["cleaned"])
        recover.assert_called_once_with(runtime)
        sockets.assert_not_called()
        rpc.assert_not_called()

    def test_owned_cleanup_result_is_preserved_and_client_closed_first(self):
        instance = adapter.WorkBuddyAdapter("11111111-2222-4333-8444-555555555555",
                                           "/tmp", Mock(), Mock())
        order = []
        instance.client = Mock()
        instance.client.close.side_effect = lambda: order.append("client")
        instance.owned = Mock()
        instance.owned.stop.side_effect = lambda: order.append("worker") or {"cleaned": False}
        self.assertEqual(instance.close(), {"cleaned": False})
        self.assertEqual(order, ["client", "worker"])
        self.assertIsNone(instance.client)


if __name__ == "__main__":
    unittest.main()
