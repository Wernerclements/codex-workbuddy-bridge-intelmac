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

    def test_idempotent_start_retries_do_not_create_another_model_task(self):
        manager, controller = self.manager()
        first = manager.start_task("test", str(self.cwd), "request-001")
        self.assertTrue(controller.entered.wait(1))
        retry = manager.start_task("test", str(self.cwd), "request-001")
        self.assertEqual(first["task_id"], retry["task_id"])
        self.assertEqual(len(controller.instances), 1)
        controller.release.set()
        self.assertEqual(manager.wait_task(first["task_id"], 2)["status"], "succeeded")
        ended = manager.start_task("test", str(self.cwd), "request-001")
        self.assertEqual(ended["status"], "succeeded")
        self.assertEqual(len(controller.instances), 1)
        manager.close()
        restarted, unused = self.manager()
        persisted = restarted.start_task("test", str(self.cwd), "request-001")
        self.assertEqual(persisted["task_id"], first["task_id"])
        self.assertEqual(persisted["status"], "succeeded")
        self.assertEqual(unused.instances, [])

    def test_request_id_conflicts_and_strict_identifier_validation(self):
        manager, controller = self.manager()
        manager.start_task("test", str(self.cwd), "stable")
        other = self.root / "other"
        other.mkdir()
        for prompt, directory in (("different", self.cwd), ("test", other)):
            with self.assertRaisesRegex(ValueError, "不同"):
                manager.start_task(prompt, str(directory), "stable")
        for request_id in ("", "a" * 129, "中文", "../escape", "a/b", "a\\b", "a\n", " space", "a\x00", 1, True):
            with self.assertRaisesRegex(ValueError, "ASCII"):
                manager.start_task("test", str(self.cwd), request_id)
        controller.release.set()

    def test_restart_request_id_returns_uncertain_record_without_replaying(self):
        from workbuddy_bridge_intelmac.manager import REQUEST_NAMESPACE
        request_id = "lost-response-001"
        task_id = str(uuid.uuid5(REQUEST_NAMESPACE, request_id))
        directory = self.root / "state" / task_id
        directory.mkdir(parents=True)
        (directory / "task.json").write_text(json.dumps({"task_id": task_id, "request_id": request_id,
                                                         "prompt": "test", "cwd": str(self.cwd.resolve()),
                                                         "requested_model": "deepseek-v4.1-flash",
                                                         "context_requested": None,
                                                         "status": "running", "runtime": None}))
        manager, controller = self.manager()
        retry = manager.start_task("test", str(self.cwd), request_id)
        self.assertEqual(retry["task_id"], task_id)
        self.assertEqual(retry["status"], "uncertain")
        self.assertEqual(controller.instances, [])

    def test_corrupt_idempotent_record_fails_closed_instead_of_resubmitting(self):
        from workbuddy_bridge_intelmac.manager import REQUEST_NAMESPACE
        request_id = "lost-response-002"
        directory = self.root / "state" / str(uuid.uuid5(REQUEST_NAMESPACE, request_id))
        directory.mkdir(parents=True)
        (directory / "task.json").write_text("corrupt")
        manager, controller = self.manager()
        with self.assertRaisesRegex(ValueError, "不能重复"):
            manager.start_task("test", str(self.cwd), request_id)
        self.assertEqual(controller.instances, [])

    def test_no_request_id_still_creates_distinct_tasks(self):
        manager, controller = self.manager()
        first = manager.start_task("test", str(self.cwd))
        second = manager.start_task("test", str(self.cwd))
        self.assertNotEqual(first["task_id"], second["task_id"])
        controller.release.set()
        manager.wait_task(first["task_id"], 2)
        manager.wait_task(second["task_id"], 2)
        self.assertEqual(len(controller.instances), 2)

    def test_requested_model_and_context_reach_adapter_without_default_fallback(self):
        observed = []

        def behavior(instance, callbacks, prompt):
            observed.append((callbacks["model"], callbacks["context_tokens"]))
            return {"answer": "done", "stop_reason": "end_turn"}

        manager, controller = self.manager(Controller(behavior))
        default = manager.start_task("default", str(self.cwd))
        self.assertEqual(manager.wait_task(default["task_id"], 2)["status"], "succeeded")
        chosen = manager.start_task("chosen", str(self.cwd), model="glm-5.3-flash", context_tokens=1000000)
        self.assertEqual(manager.wait_task(chosen["task_id"], 2)["status"], "succeeded")
        self.assertEqual(observed, [("deepseek-v4.1-flash", None), ("glm-5.3-flash", 1000000)])
        view = manager.get_task(chosen["task_id"])
        self.assertEqual(view["requested_model"], "glm-5.3-flash")
        self.assertEqual(view["context_requested"], 1000000)

    def test_request_id_conflicts_include_model_and_context_before_and_after_restart(self):
        manager, controller = self.manager()
        original = manager.start_task("test", str(self.cwd), "stable-model", "glm-5.3-flash", 1000000)
        self.assertTrue(controller.entered.wait(1))
        for model, context in (("deepseek-v4.1-flash", 1000000), ("glm-5.3-flash", 300000),
                               ("glm-5.3-flash", None)):
            with self.subTest(model=model, context=context), self.assertRaisesRegex(ValueError, "不同"):
                manager.start_task("test", str(self.cwd), "stable-model", model, context)
        controller.release.set()
        self.assertEqual(manager.wait_task(original["task_id"], 2)["status"], "succeeded")
        manager.close()
        restarted, unused = self.manager()
        retry = restarted.start_task("test", str(self.cwd), "stable-model", "glm-5.3-flash", 1000000)
        self.assertEqual(retry["task_id"], original["task_id"])
        for model, context in (("deepseek-v4.1-flash", 1000000), ("glm-5.3-flash", None)):
            with self.assertRaisesRegex(ValueError, "不同"):
                restarted.start_task("test", str(self.cwd), "stable-model", model, context)
        self.assertEqual(unused.instances, [])

    def test_model_capability_metadata_persists_with_token_budgets_and_redacts_credentials(self):
        metadata = {"id": "glm-5.3-flash", "name": "GLM-5.3-Flash", "selection_confirmed": True,
                    "max_input_tokens": 1000000, "context_tokens": 1000000,
                    "context_choices": [], "context_source": "advertised_model_limit",
                    "context_requested": 1000000, "auto_compact_window_requested": 1000000}

        def behavior(instance, callbacks, prompt):
            callbacks["on_event"]({"type": "model", "model": {**metadata,
                                                                 "authorization": "NEVER-PERSIST-MODEL",
                                                                 "api_token": "NEVER-PERSIST-MODEL"}})
            return {"answer": "done", "stop_reason": "end_turn"}

        manager, controller = self.manager(Controller(behavior))
        task_id = manager.start_task("test", str(self.cwd), model="glm-5.3-flash",
                                     context_tokens=1000000)["task_id"]
        self.assertEqual(manager.wait_task(task_id, 2)["status"], "succeeded")
        result = manager.get_result(task_id)
        self.assertEqual(result["requested_model"], "glm-5.3-flash")
        self.assertEqual(result["context_requested"], 1000000)
        self.assertEqual(result["model"], metadata)
        record = self.root / "state" / task_id / "task.json"
        self.assertEqual(json.loads(record.read_text())["model"], metadata)
        self.assertNotIn("NEVER-PERSIST-MODEL", record.read_text())
        manager.close()
        restarted, unused = self.manager()
        self.assertEqual(restarted.get_result(task_id)["model"], metadata)
        self.assertEqual(unused.instances, [])

    def test_old_alpha_record_without_model_cannot_retry_as_new_default(self):
        from workbuddy_bridge_intelmac.manager import REQUEST_NAMESPACE
        request_id = "old-alpha-default-model"
        task_id = str(uuid.uuid5(REQUEST_NAMESPACE, request_id))
        directory = self.root / "state" / task_id
        directory.mkdir(parents=True)
        (directory / "task.json").write_text(json.dumps({"task_id": task_id, "request_id": request_id,
                                                         "prompt": "test", "cwd": str(self.cwd.resolve()),
                                                         "status": "running", "runtime": None}))
        manager, controller = self.manager()
        self.assertEqual(manager.get_task(task_id)["status"], "uncertain")
        with self.assertRaisesRegex(ValueError, "不同"):
            manager.start_task("test", str(self.cwd), request_id)
        self.assertEqual(manager.get_result(task_id)["requested_model"], "auto")
        self.assertEqual(controller.instances, [])

    def test_invalid_model_and_context_rejected_before_queueing(self):
        manager, controller = self.manager()
        for model in ("", "glm flash", "a\n", "--unsafe", 1, True):
            with self.subTest(model=model), self.assertRaisesRegex(ValueError, "model"):
                manager.start_task("test", str(self.cwd), model=model)
        for context in (0, -1, 10000001, True, "1000000", 1.5):
            with self.subTest(context=context), self.assertRaisesRegex(ValueError, "context_tokens"):
                manager.start_task("test", str(self.cwd), context_tokens=context)
        self.assertEqual(manager._tasks, {})
        self.assertEqual(controller.instances, [])

    def test_runtime_loss_unblocks_permission_without_grant_and_preserves_partial(self):
        callbacks_holder = {}
        choices = []

        def behavior(instance, callbacks, prompt):
            callbacks_holder.update(callbacks)
            callbacks["on_event"]({"type": "answer", "text": "early partial"})
            choices.append(callbacks["on_permission"]({"options": [{"optionId": "allow"}]}))
            return {"answer": "late success", "stop_reason": "end_turn"}

        manager, controller = self.manager(Controller(behavior))
        task_id = manager.start_task("test", str(self.cwd))["task_id"]
        waiting = manager.wait_task(task_id, 2)
        self.assertEqual(waiting["status"], "permission_required")
        request_id = waiting["permission_request"]["request_id"]
        callbacks_holder["on_event"]({"type": "runtime_lost", "reason": "private-loss-detail"})
        with self.assertRaises(ValueError):
            manager.respond_permission(task_id, request_id, "allow")
        ended = manager.wait_task(task_id, 2)
        self.assertEqual(ended["status"], "uncertain")
        self.assertTrue(ended["runtime_lost"])
        self.assertFalse(ended["cancel_requested"])
        self.assertIsNone(ended["permission_request"])
        self.assertEqual(choices, [])
        result = manager.get_result(task_id)
        self.assertTrue(result["partial"])
        self.assertEqual(Path(result["answer_path"]).read_text(), "early partial")
        self.assertNotIn("private", result["error"])

    def test_runtime_loss_before_normal_return_cannot_claim_success(self):
        def behavior(instance, callbacks, prompt):
            callbacks["on_event"]({"type": "runtime_lost"})
            return {"answer": "late complete answer", "stop_reason": "end_turn"}

        manager, controller = self.manager(Controller(behavior))
        task_id = manager.start_task("test", str(self.cwd))["task_id"]
        ended = manager.wait_task(task_id, 2)
        self.assertEqual(ended["status"], "uncertain")
        self.assertFalse(ended["cancel_requested"])
        result = manager.get_result(task_id)
        self.assertTrue(result["partial"])
        self.assertEqual(Path(result["answer_path"]).read_text(), "late complete answer")

    def test_runtime_loss_without_partial_is_still_uncertain(self):
        def behavior(instance, callbacks, prompt):
            callbacks["on_event"]({"type": "runtime_lost"})
            raise ValueError("private unexpected exception")

        manager, controller = self.manager(Controller(behavior))
        task_id = manager.start_task("test", str(self.cwd))["task_id"]
        ended = manager.wait_task(task_id, 2)
        self.assertEqual(ended["status"], "uncertain")
        self.assertFalse(ended["cancel_requested"])
        self.assertFalse(manager.get_result(task_id)["partial"])

    def test_verified_close_clears_pending_without_changing_success(self):
        def behavior(instance, callbacks, prompt):
            instance.close = lambda: {"cleaned": True}
            return {"answer": "done", "stop_reason": "end_turn"}

        manager, controller = self.manager(Controller(behavior))
        task_id = manager.start_task("test", str(self.cwd))["task_id"]
        self.assertEqual(manager.wait_task(task_id, 2)["status"], "succeeded")
        manager.close()
        result = manager.get_result(task_id)
        self.assertTrue(result["cleanup_confirmed"])
        self.assertFalse(result["cleanup_pending"])
        recovered = []
        restarted, unused = self.manager(recover=lambda runtime: recovered.append(runtime) or {"cleaned": True})
        self.assertEqual(restarted.get_task(task_id)["status"], "succeeded")
        self.assertEqual(recovered, [])

    def test_failed_terminal_cleanup_is_recovered_on_restart_without_replay(self):
        def behavior(instance, callbacks, prompt):
            instance.close = lambda: {"cleaned": False}
            raise ValueError("private execution failure")

        manager, controller = self.manager(Controller(behavior))
        task_id = manager.start_task("test", str(self.cwd))["task_id"]
        self.assertEqual(manager.wait_task(task_id, 2)["status"], "failed")
        manager.close()
        before = manager.get_result(task_id)
        self.assertTrue(before["cleanup_pending"])
        self.assertFalse(before["cleanup_confirmed"])
        recovered = []
        restarted, unused = self.manager(recover=lambda runtime: recovered.append(runtime) or {"cleaned": True})
        after = restarted.get_result(task_id)
        self.assertEqual(after["status"], "failed")
        self.assertEqual(after["error"], before["error"])
        self.assertFalse(after["cleanup_pending"])
        self.assertTrue(after["cleanup_confirmed"])
        self.assertEqual(len(recovered), 1)
        self.assertEqual(unused.instances, [])

    def test_uncertain_close_exception_stays_pending_and_restart_preserves_partial(self):
        def behavior(instance, callbacks, prompt):
            def fail_close():
                raise OSError("private cleanup detail")
            instance.close = fail_close
            callbacks["on_event"]({"type": "runtime_lost"})
            return {"answer": "partial answer", "stop_reason": "end_turn"}

        manager, controller = self.manager(Controller(behavior))
        task_id = manager.start_task("test", str(self.cwd))["task_id"]
        self.assertEqual(manager.wait_task(task_id, 2)["status"], "uncertain")
        manager.close()
        before = manager.get_result(task_id)
        self.assertTrue(before["cleanup_pending"])
        self.assertEqual(manager.get_task(task_id)["cleanup_error_type"], "OSError")
        restarted, unused = self.manager(recover=lambda runtime: {"cleaned": True})
        after = restarted.get_result(task_id)
        self.assertEqual(after["status"], "uncertain")
        self.assertEqual(after["summary"], before["summary"])
        self.assertEqual(after["answer_path"], before["answer_path"])
        self.assertTrue(after["cleanup_confirmed"])
        self.assertFalse(after["cleanup_pending"])
        self.assertEqual(unused.instances, [])

    def test_legacy_close_none_never_claims_verified_cleanup(self):
        manager, controller = self.manager()
        controller.release.set()
        task_id = manager.start_task("test", str(self.cwd))["task_id"]
        manager.wait_task(task_id, 2)
        manager.close()
        result = manager.get_result(task_id)
        self.assertTrue(result["cleanup_pending"])
        self.assertFalse(result["cleanup_confirmed"])

    def test_failed_restart_cleanup_remains_pending_and_preserves_terminal_status(self):
        task_id = str(uuid.uuid4())
        directory = self.root / "state" / task_id
        directory.mkdir(parents=True)
        (directory / "task.json").write_text(json.dumps({"task_id": task_id, "status": "cancelled",
            "summary": "partial", "cleanup_pending": True, "runtime": {"worker_id": "own-worker"}}))
        def failed_recovery(runtime):
            raise PermissionError("private recovery detail")
        manager, controller = self.manager(recover=failed_recovery)
        task = manager.get_task(task_id)
        self.assertEqual(task["status"], "cancelled")
        self.assertTrue(task["cleanup_pending"])
        self.assertFalse(task["cleanup_confirmed"])
        self.assertEqual(task["cleanup_error_type"], "PermissionError")
        self.assertNotIn("private", json.dumps(task))
        self.assertEqual(controller.instances, [])
