import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
import uuid

from workbuddy_bridge_intelmac.manager import Manager


class Controller:
    def __init__(self, behavior=None):
        self.behavior = behavior
        self.entered = threading.Event()
        self.release = threading.Event()
        self.instances = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def factory(self, **kwargs):
        controller = self

        class FakeAdapter:
            def __init__(self):
                self.cancelled = False
                self.closed = False
                self.task_id = kwargs["task_id"]

            def run(self, prompt):
                with controller.lock:
                    controller.active += 1
                    controller.max_active = max(controller.max_active, controller.active)
                try:
                    kwargs["on_event"]({"type": "runtime", "runtime": {"task_id": self.task_id,
                                            "worker_id": "project-worker", "password": "NEVER-PERSIST"}})
                    kwargs["on_event"]({"type": "activity"})
                    controller.entered.set()
                    if controller.behavior:
                        return controller.behavior(self, kwargs, prompt)
                    controller.release.wait(3)
                    if self.cancelled:
                        raise RuntimeError("private-error-with-secret")
                    return {"answer": prompt, "session_id": "session", "stop_reason": "end_turn"}
                finally:
                    with controller.lock:
                        controller.active -= 1

            def cancel(self):
                self.cancelled = True
                controller.release.set()

            def close(self):
                self.closed = True

        instance = FakeAdapter()
        self.instances.append(instance)
        return instance


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.cwd = self.root / "work"
        self.cwd.mkdir()
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            manager.close()
        self.temporary.cleanup()

    def manager(self, controller=None, recover=None):
        controller = controller or Controller()
        manager = Manager(self.root / "state", controller.factory, recover or (lambda runtime: {}))
        self.managers.append(manager)
        return manager, controller

    def test_wait_timeout_does_not_cancel_or_resubmit_and_tasks_are_serial(self):
        manager, controller = self.manager()
        first = manager.start_task("first", str(self.cwd))["task_id"]
        self.assertTrue(controller.entered.wait(1))
        second = manager.start_task("second", str(self.cwd))["task_id"]
        response = manager.wait_task(first, 0.02)
        self.assertTrue(response["wait_timed_out"])
        self.assertFalse(response["cancel_requested"])
        self.assertEqual(len(controller.instances), 1)
        self.assertEqual(manager.get_task(second)["status"], "queued")
        controller.release.set()
        self.assertEqual(manager.wait_task(first, 2)["status"], "succeeded")
        self.assertEqual(manager.wait_task(second, 2)["status"], "succeeded")
        self.assertEqual(controller.max_active, 1)

    def test_queued_cancel_never_constructs_adapter(self):
        manager, controller = self.manager()
        first = manager.start_task("first", str(self.cwd))["task_id"]
        controller.entered.wait(1)
        queued = manager.start_task("queued", str(self.cwd))["task_id"]
        self.assertEqual(manager.cancel_task(queued)["status"], "cancelled")
        controller.release.set()
        manager.wait_task(first, 2)
        self.assertEqual(len(controller.instances), 1)

    def test_cancel_running_and_unknown_id(self):
        manager, controller = self.manager()
        task_id = manager.start_task("first", str(self.cwd))["task_id"]
        controller.entered.wait(1)
        manager.cancel_task(task_id)
        self.assertEqual(manager.wait_task(task_id, 2)["status"], "cancelled")
        for method in (manager.get_task, manager.cancel_task, manager.get_result, manager.wait_task):
            with self.assertRaisesRegex(ValueError, "未知"):
                method("missing")

    def test_permission_requires_explicit_current_choice(self):
        choices = []

        def behavior(instance, callbacks, prompt):
            choices.append(callbacks["on_permission"]({"options": [{"optionId": "once"},
                                                                   {"optionId": "deny"}]}))
            return {"answer": "done", "stop_reason": "end_turn"}

        manager, controller = self.manager(Controller(behavior))
        task_id = manager.start_task("test", str(self.cwd))["task_id"]
        response = manager.wait_task(task_id, 2)
        self.assertEqual(response["status"], "permission_required")
        request_id = response["permission_request"]["request_id"]
        self.assertEqual(choices, [])
        with self.assertRaises(ValueError):
            manager.respond_permission(task_id, "stale", "once")
        with self.assertRaises(ValueError):
            manager.respond_permission(task_id, request_id, "forever")
        manager.respond_permission(task_id, request_id, "deny")
        self.assertEqual(manager.wait_task(task_id, 2)["status"], "succeeded")
        self.assertEqual(choices, ["deny"])
        with self.assertRaises(ValueError):
            manager.respond_permission(task_id, request_id, "once")

    def test_cancel_permission_unblocks_without_grant(self):
        choices = []

        def behavior(instance, callbacks, prompt):
            choices.append(callbacks["on_permission"]({"options": [{"optionId": "allow"}]}))
            return {"answer": "done", "stop_reason": "end_turn"}

        manager, controller = self.manager(Controller(behavior))
        task_id = manager.start_task("test", str(self.cwd))["task_id"]
        self.assertEqual(manager.wait_task(task_id, 2)["status"], "permission_required")
        manager.cancel_task(task_id)
        self.assertEqual(manager.wait_task(task_id, 2)["status"], "cancelled")
        self.assertEqual(choices, [])

    def test_results_persist_with_private_permissions_and_safe_file_manifest(self):
        outside = self.root / "outside"
        outside.write_text("outside")
        (self.cwd / "existing.txt").write_text("old")

        def behavior(instance, callbacks, prompt):
            (self.cwd / "existing.txt").write_text("changed")
            (self.cwd / "new.txt").write_text("new")
            (self.cwd / "escape").symlink_to(outside)
            return {"answer": "A" * 10000, "stop_reason": "end_turn"}

        manager, controller = self.manager(Controller(behavior))
        task_id = manager.start_task("test", str(self.cwd))["task_id"]
        self.assertEqual(manager.wait_task(task_id, 2)["status"], "succeeded")
        result = manager.get_result(task_id)
        self.assertEqual(len(result["summary"]), 2000)
        self.assertEqual(Path(result["answer_path"]).read_text(), "A" * 10000)
        self.assertEqual({Path(file["path"]).name for file in result["artifacts"]},
                         {"existing.txt", "new.txt"})
        record = self.root / "state" / task_id / "task.json"
        self.assertNotIn("NEVER-PERSIST", record.read_text())
        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(record.parent.stat().st_mode), 0o700)
        manager.close()
        restarted, unused = self.manager()
        self.assertEqual(restarted.get_result(task_id)["answer_path"], result["answer_path"])
        self.assertEqual(unused.instances, [])

    def test_restart_intermediate_task_is_uncertain_and_never_replayed(self):
        state = self.root / "state"
        task_id = str(uuid.uuid4())
        directory = state / task_id
        directory.mkdir(parents=True)
        (directory / "task.json").write_text(json.dumps({"task_id": task_id, "status": "running",
                                                          "runtime": {"worker_id": "own-worker"}}))
        recovered = []
        manager, controller = self.manager(recover=lambda runtime: recovered.append(runtime) or {"cleaned": True})
        self.assertEqual(manager.get_task(task_id)["status"], "uncertain")
        self.assertEqual(recovered, [{"worker_id": "own-worker"}])
        self.assertEqual(controller.instances, [])

    def test_single_instance_lock_prevents_false_recovery(self):
        manager, controller = self.manager()
        task_id = manager.start_task("test", str(self.cwd))["task_id"]
        controller.entered.wait(1)
        with self.assertRaisesRegex(RuntimeError, "已有"):
            self.manager()
        self.assertEqual(manager.get_task(task_id)["status"], "running")
        controller.release.set()
        manager.wait_task(task_id, 2)
        manager.close()
        replacement, unused = self.manager()
        self.assertEqual(replacement.get_task(task_id)["status"], "succeeded")

    def test_unexpected_exception_is_redacted(self):
        def behavior(instance, callbacks, prompt):
            raise ValueError("secret-token-query")
        manager, controller = self.manager(Controller(behavior))
        task_id = manager.start_task("test", str(self.cwd))["task_id"]
        self.assertEqual(manager.wait_task(task_id, 2)["status"], "failed")
        self.assertNotIn("secret", manager.get_result(task_id)["error"])

    def test_wait_bounds_and_invalid_workdir(self):
        manager, controller = self.manager()
        for value in (-1, 61, float("nan"), float("inf"), True, "2"):
            with self.assertRaises(ValueError):
                manager.wait_task("missing", value)
        with self.assertRaises(ValueError):
            manager.start_task("", str(self.cwd))
        with self.assertRaises(ValueError):
            manager.start_task("test", str(self.root / "missing"))

    def test_close_retains_lock_for_uncooperative_worker_and_fences_late_result(self):
        from unittest.mock import patch
        manager, controller = self.manager()
        task_id = manager.start_task("test", str(self.cwd))["task_id"]
        self.assertTrue(controller.entered.wait(1))
        controller.instances[0].cancel = lambda: None
        with patch.object(manager._worker, "join", lambda timeout: None):
            manager.close()
        self.assertEqual(manager.get_task(task_id)["status"], "uncertain")
        with self.assertRaises(RuntimeError):
            self.manager()
        controller.release.set()
        manager._worker.join(2)
        self.assertFalse(manager._worker.is_alive())
        self.assertEqual(manager.get_task(task_id)["status"], "uncertain")
        replacement, unused = self.manager()
        self.assertEqual(replacement.get_task(task_id)["status"], "uncertain")

    def test_stop_reason_and_empty_answer_do_not_claim_success(self):
        for reason, answer, expected in (("cancelled", "partial", "cancelled"),
                                         ("unexpected", "partial", "uncertain"),
                                         (None, "partial", "uncertain"),
                                         ("end_turn", "", "failed"),
                                         ("refusal", "cannot complete", "failed"),
                                         ("max_tokens", "partial", "uncertain"),
                                         ("max_turn_requests", "partial", "uncertain")):
            with self.subTest(reason=reason):
                controller = Controller(lambda instance, callbacks, prompt:
                                        {"answer": answer, "stop_reason": reason})
                manager, unused = self.manager(controller)
                task_id = manager.start_task("test", str(self.cwd))["task_id"]
                self.assertEqual(manager.wait_task(task_id, 2)["status"], expected)
                manager.close()

    def test_session_event_persists_and_scan_limits_are_explicit(self):
        from unittest.mock import patch

        def behavior(instance, callbacks, prompt):
            callbacks["on_event"]({"type": "session", "session_id": "new-session"})
            (self.cwd / "one.txt").write_text("one")
            (self.cwd / "two.txt").write_text("two")
            return {"answer": "done", "stop_reason": "end_turn"}

        manager, controller = self.manager(Controller(behavior))
        with patch("workbuddy_bridge_intelmac.manager.MAX_ARTIFACTS", 1):
            task_id = manager.start_task("test", str(self.cwd))["task_id"]
            manager.wait_task(task_id, 2)
            result = manager.get_result(task_id)
        self.assertEqual(len(result["artifacts"]), 1)
        self.assertTrue(result["scan_limited"])
        record = json.loads((self.root / "state" / task_id / "task.json").read_text())
        # Final adapter data may omit session_id; preserve the session event.
        self.assertEqual(record["session_id"], "new-session")

    def test_exception_preserves_partial_answer_and_changes_without_claiming_success(self):
        def behavior(instance, callbacks, prompt):
            callbacks["on_event"]({"type": "answer", "text": "partial answer"})
            (self.cwd / "partial.txt").write_text("partial file")
            raise ConnectionError("secret-token-query")

        manager, controller = self.manager(Controller(behavior))
        task_id = manager.start_task("test", str(self.cwd))["task_id"]
        self.assertEqual(manager.wait_task(task_id, 2)["status"], "uncertain")
        result = manager.get_result(task_id)
        self.assertTrue(result["partial"])
        self.assertEqual(Path(result["answer_path"]).read_text(), "partial answer")
        self.assertEqual(result["summary"], "partial answer")
        self.assertEqual([Path(item["path"]).name for item in result["artifacts"]], ["partial.txt"])
        self.assertNotIn("secret", result["error"])

    def test_exception_with_file_only_still_preserves_partial_manifest(self):
        def behavior(instance, callbacks, prompt):
            (self.cwd / "partial.txt").write_text("partial file")
            raise ConnectionError("private failure")

        manager, controller = self.manager(Controller(behavior))
        task_id = manager.start_task("test", str(self.cwd))["task_id"]
        self.assertEqual(manager.wait_task(task_id, 2)["status"], "uncertain")
        result = manager.get_result(task_id)
        self.assertTrue(result["partial"])
        self.assertIsNone(result["answer_path"])
        self.assertEqual(len(result["artifacts"]), 1)

    def test_fenced_shutdown_discards_late_exception_metadata(self):
        from unittest.mock import patch

        def behavior(instance, callbacks, prompt):
            callbacks["on_event"]({"type": "answer", "text": "early partial"})
            controller.release.wait(3)
            (self.cwd / "late.txt").write_text("late file")
            raise ConnectionError("private late exception")

        controller = Controller(behavior)
        manager, unused = self.manager(controller)
        task_id = manager.start_task("test", str(self.cwd))["task_id"]
        self.assertTrue(controller.entered.wait(1))
        controller.instances[0].cancel = lambda: None
        with patch.object(manager._worker, "join", lambda timeout: None):
            manager.close()
        record = self.root / "state" / task_id / "task.json"
        fenced_record = record.read_text()
        controller.release.set()
        manager._worker.join(2)
        self.assertFalse(manager._worker.is_alive())
        self.assertEqual(record.read_text(), fenced_record)
        self.assertFalse((record.parent / "answer.txt").exists())
        self.assertEqual(manager.get_task(task_id)["status"], "uncertain")
