"""Protect the opt-in boundary of the real desktop restart acceptance script."""
import asyncio
from contextlib import redirect_stdout
import importlib.util
import io
import json
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


if __name__ == "__main__":
    unittest.main()
