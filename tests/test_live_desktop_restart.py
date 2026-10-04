"""Protect the opt-in boundary of the real desktop restart acceptance script."""
import asyncio
from contextlib import redirect_stdout, asynccontextmanager
import importlib.util
import io
import json
import plistlib
import tempfile
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/live_desktop_restart.py"
spec = importlib.util.spec_from_file_location("live_desktop_restart", SCRIPT)
restart = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restart)


class DesktopRestartGuardTests(unittest.TestCase):
    def test_default_and_explicit_preview_do_not_touch_application(self):
        for arguments in ([], ["--dry-run"]):
            with self.subTest(arguments=arguments), \
                    patch.object(sys, "argv", [str(SCRIPT), *arguments]), \
                    patch.object(restart, "run") as run, \
                    patch.object(restart, "command") as command, \
                    patch.object(restart, "write_report") as write_report:
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(restart.main(), 0)
                self.assertTrue(json.loads(output.getvalue())["dry_run"])
                run.assert_not_called()
                command.assert_not_called()
                write_report.assert_not_called()

    def test_permission_in_text_test_cancels_without_granting(self):
        async def exercise():
            session = object()
            call = AsyncMock(side_effect=[{"status": "permission_required"}, {}])
            with patch.object(restart, "call", call):
                with self.assertRaisesRegex(RuntimeError, "未自动授权"):
                    await restart.finish(session, "owned-task", timeout=1)
            self.assertEqual([item.args[1] for item in call.await_args_list],
                             ["wait_task", "cancel_task"])
        asyncio.run(exercise())

    def test_identity_check_detects_same_task_being_restarted(self):
        state = {"task_id": "owned", "created_at": 1, "started_at": 2,
                 "runtime": {"worker_id": "worker"}}
        self.assertTrue(restart.same_task(state, dict(state, status="uncertain")))
        self.assertFalse(restart.same_task(state, dict(state, started_at=3)))
        self.assertFalse(restart.same_task(state, dict(state, runtime={"worker_id": "other"})))

    def test_permission_wait_accepts_execute_without_granting(self):
        async def exercise():
            pending = {"status": "permission_required", "permission_request": {"kind": "execute"}}
            mocked = AsyncMock(return_value=pending)
            with patch.object(restart, "call", mocked):
                self.assertEqual(await restart.wait_permission(object(), "owned", timeout=1), pending)
            self.assertEqual([item.args[1] for item in mocked.await_args_list], ["get_task"])
        asyncio.run(exercise())

    def test_permission_wait_rejects_terminal_before_gui_quit(self):
        async def exercise():
            mocked = AsyncMock(return_value={"status": "succeeded"})
            with patch.object(restart, "call", mocked):
                with self.assertRaisesRegex(RuntimeError, "未进入工具权限等待"):
                    await restart.wait_permission(object(), "owned", timeout=1)
            self.assertEqual([item.args[1] for item in mocked.await_args_list], ["get_task"])
        asyncio.run(exercise())

    def test_terminal_task_waits_for_verified_cleanup(self):
        async def exercise():
            states = [
                {"status": "uncertain", "cleanup_pending": True, "cleanup_confirmed": False},
                {"status": "uncertain", "cleanup_pending": False, "cleanup_confirmed": True},
                {"status": "uncertain", "partial": True},
            ]
            mocked = AsyncMock(side_effect=states)
            with patch.object(restart, "call", mocked), patch.object(restart.asyncio, "sleep", AsyncMock()):
                state, result = await restart.finish(object(), "owned-task", timeout=2)
            self.assertTrue(state["cleanup_confirmed"])
            self.assertTrue(result["partial"])
            self.assertEqual([item.args[1] for item in mocked.await_args_list],
                             ["wait_task", "wait_task", "get_result"])
        asyncio.run(exercise())

    def test_probe_uses_read_only_health_and_unique_gui_without_requiring_sidecar(self):
        async def exercise():
            cold = {"available": True, "ready_for_task": True, "authenticated": None,
                    "gui": {"pid": 10, "start": "old"}, "sidecar_pids": []}
            mocked = AsyncMock(return_value=json.dumps(cold))
            with patch.object(restart, "command", mocked):
                result = await restart.probe()
            self.assertEqual(result, cold)
            code = mocked.await_args.args[0][-1]
            self.assertIn("h=health()", code)
            self.assertIn("g=gui_identity()", code)
            self.assertNotIn("discover()", code)
            self.assertNotIn("OwnedRuntime", code)
        asyncio.run(exercise())

    def test_cleanup_failure_cannot_pass_acceptance(self):
        base = {"cleanup": {"reopen_command_succeeded": True, "owned_workers": 1,
                            "workers": [{"cleaned": True}]}}
        self.assertTrue(restart.cleanup_passed(base))
        self.assertFalse(restart.cleanup_passed(dict(base, mcp_cleanup_timed_out=True)))
        for entry in ({"cleaned": False}, {"cleaned": 1}, {"identity_available": False}):
            report = {"cleanup": dict(base["cleanup"], workers=[entry])}
            self.assertFalse(restart.cleanup_passed(report))

    def test_cold_restart_mock_records_authentication_only_after_owned_task_success(self):
        async def exercise():
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                app = root / "WorkBuddy.app"
                (app / "Contents").mkdir(parents=True)
                (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.test.workbuddy"}))
                live = root / "live"
                initial = {"ready_for_task": True, "runtime_ready": False, "authenticated": None,
                           "gui": {"pid": 10, "start": "old"}, "sidecar_pids": [], "version": "5.6.2"}
                restored = dict(initial, gui={"pid": 20, "start": "new"})
                old = {"task_id": "private-interrupted", "created_at": 1, "started_at": 2,
                       "runtime": {"launcher": "owned", "worker_id": "private-worker-1"},
                       "status": "uncertain", "cleanup_confirmed": True, "cleanup_pending": False}
                short = {"task_id": "private-short", "created_at": 3, "started_at": 4,
                         "runtime": {"launcher": "owned", "worker_id": "private-worker-2"},
                         "status": "succeeded", "cleanup_confirmed": True, "cleanup_pending": False}
                issued = set()
                tools = []

                async def tool(session, name, arguments=None):
                    tools.append(name)
                    arguments = arguments or {}
                    if name == "start_task":
                        state = old if arguments["request_id"].startswith("desktop-interruption-") else short
                        task_id = state["task_id"]
                        if task_id in issued:
                            return state
                        issued.add(task_id)
                        return {"task_id": task_id}
                    state = old if arguments["task_id"] == old["task_id"] else short
                    if name == "get_task" and len(issued) == 1 and tools.count("get_task") == 1:
                        return dict(old, status="running", prompt_accepted=True)
                    if name in {"wait_task", "get_task"}:
                        return state
                    if name == "get_result":
                        return {"partial": state is old, "summary": "" if state is old else "INTELMAC_DESKTOP_RESTART_OK"}
                    raise AssertionError("Unexpected MCP tool")

                @asynccontextmanager
                async def transport(parameters):
                    yield object(), object()

                class Session:
                    async def __aenter__(self): return self
                    async def __aexit__(self, *args): return False
                    async def initialize(self): return None

                async def cmd(arguments, **kwargs):
                    if arguments[0] == "open" or arguments[0] == "osascript":
                        return ""
                    return json.dumps({"cleaned": True})

                with patch.object(restart, "APP", app), patch.object(restart, "LIVE", live), \
                     patch.object(restart.sys, "platform", "darwin"), \
                     patch.object(restart, "probe", AsyncMock(side_effect=[initial, restored])), \
                     patch.object(restart, "gui_running", AsyncMock(side_effect=[True, False, True])), \
                     patch.object(restart, "process_identity", AsyncMock(side_effect=["old-identity", None])), \
                     patch.object(restart, "stdio_client", transport), patch.object(restart, "ClientSession", lambda *args: Session()), \
                     patch.object(restart, "call", tool), patch.object(restart, "command", cmd), redirect_stdout(io.StringIO()):
                    self.assertTrue(await restart.run(120))
                report = json.loads((live / "report.json").read_text())
                self.assertIsNone(report["initial"]["authenticated"])
                self.assertIsNone(report["cases"]["normal_quit"]["old_sidecar_exited"])
                self.assertEqual(report["cases"]["interrupted_task"]["status"], "uncertain")
                self.assertTrue(report["cases"]["reopen"]["authenticated"])
                self.assertEqual(report["cases"]["reopen"]["authentication_source"], "new_owned_worker_task")
                self.assertTrue(report["cleanup"]["passed"])
                self.assertNotIn("private-", json.dumps(report))
                self.assertNotIn("health", tools)
                self.assertEqual(len(issued), 2)
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
