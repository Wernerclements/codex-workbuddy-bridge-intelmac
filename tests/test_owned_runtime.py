"""Cold runtime boundaries; no desktop, network, or real process is operated."""
import os
from pathlib import Path
import re
import signal
import socket
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from workbuddy_bridge_intelmac import owned_runtime as owned
from workbuddy_bridge_intelmac.discovery import WORKER_PREFIX, WorkBuddyError


TASK = "11111111-2222-4333-8444-555555555555"
START = "Sun Oct 4 12:34:56 2026"
PID = 4321


def record(**changes):
    value = {"launcher": "owned", "task_id": TASK, "worker_id": WORKER_PREFIX + TASK,
             "pid": PID, "process_start": START}
    value.update(changes)
    return value


def identity(**changes):
    value = {"start": START, "command": (
        f"{owned.APP}/Contents/MacOS/Electron {owned.LAUNCHER} {TASK} 9876 "
        f"{owned.APP}/Contents/Resources/app.asar/cli/bin/codebuddy --serve --port 0")}
    value.update(changes)
    return value


class IdentityTests(unittest.TestCase):
    def setUp(self):
        home = patch.object(owned.Path, "home", return_value=Path("/synthetic/home"))
        metadata = patch.object(owned.Path, "lstat", return_value=SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600, st_uid=os.getuid(), st_size=1234))
        home.start()
        metadata.start()
        self.addCleanup(home.stop)
        self.addCleanup(metadata.stop)

    def test_process_identity_rejects_other_uid_zombie_and_malformed_output(self):
        rows = [f"{os.getuid() + 1} {START} S foreign",
                f"{os.getuid()} {START} Z defunct", "unexpected"]
        for row in rows:
            with self.subTest(row=row), patch.object(owned.subprocess, "run", return_value=
                    SimpleNamespace(returncode=0, stdout=row)):
                self.assertIsNone(owned.process_identity(PID))

    def test_invalid_pid_does_not_inspect_processes(self):
        with patch.object(owned.subprocess, "run") as run:
            for pid in [True, 0, 1, "4321", None]:
                self.assertIsNone(owned.process_identity(pid))
            run.assert_not_called()

    def test_uuid_start_and_wrapper_must_all_match(self):
        invalid = [record(task_id="not-a-uuid"), record(worker_id="desktop-host"),
                   record(process_start="reused PID start")]
        with patch.object(owned, "process_identity", return_value=identity()):
            self.assertEqual(owned.owned_identity(record(), PID), identity())
            for runtime in invalid:
                with self.subTest(runtime=runtime):
                    self.assertIsNone(owned.owned_identity(runtime, PID))
        for command in ["other-app --serve", identity()["command"].replace(TASK, "foreign"),
                        identity()["command"].replace("--serve", "--other")]:
            with self.subTest(command=command), patch.object(owned, "process_identity",
                    return_value=identity(command=command)):
                self.assertIsNone(owned.owned_identity(record(), PID))

    def test_gui_detection_excludes_daemon_worker_and_foreign_user(self):
        executable = owned.APP / "Contents/MacOS/Electron"
        lines = [f"{os.getuid()} 100 {executable}",
                 f"{os.getuid()} 101 {executable} --type=renderer",
                 f"{os.getuid()} 102 {executable} --serve",
                 f"{os.getuid()} 103 {executable} {owned.LAUNCHER} {TASK}",
                 f"{os.getuid()+1} 104 {executable}"]
        with patch.object(owned, "app_info", return_value={"version": "5.6.2",
                "intel_binary": True, "executable": executable}), \
                patch.object(owned.subprocess, "run", return_value=SimpleNamespace(stdout="\n".join(lines))), \
                patch.object(owned, "process_identity", return_value=identity()) as inspect:
            self.assertEqual(owned.gui_identity(), {"pid": 100, "start": START, "version": "5.6.2"})
        inspect.assert_called_once_with(100)

    def test_multiple_gui_instances_refuse_cold_launch(self):
        executable = owned.APP / "Contents/MacOS/Electron"
        with patch.object(owned, "app_info", return_value={"version": "5.6.2",
                "intel_binary": True, "executable": executable}), \
                patch.object(owned.subprocess, "run", return_value=SimpleNamespace(stdout=
                    f"{os.getuid()} 100 {executable}\n{os.getuid()} 101 {executable}")), \
                patch.object(owned, "process_identity", return_value=identity()), \
                self.assertRaises(WorkBuddyError):
            owned.gui_identity()

    def test_environment_does_not_forward_credentials_or_override_permissions(self):
        ambient = {"HOME": "/synthetic/home", "PATH": "/usr/bin", "LANG": "C",
                   "OPENAI_API_KEY": "ambient-secret", "ANTHROPIC_API_KEY": "ambient-secret",
                   "NODE_OPTIONS": "unsafe-node-options", "CODEBUDDY_GATEWAY_PASSWORD": "ambient-secret",
                   "WORKBUDDY_AT_REST_ENCRYPTION": "ambient-secret",
                   "CODEBUDDY_CREDENTIAL_BOOTSTRAP": "ambient-secret", "BROKER_LEASE": "ambient-secret"}
        with patch.dict(os.environ, ambient, clear=True):
            env = owned.launch_environment("local-gateway-secret", version="5.6.2")
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["CODEBUDDY_GATEWAY_AUTH"], "password")
        self.assertEqual(env["CODEBUDDY_GATEWAY_PASSWORD"], "local-gateway-secret")
        self.assertNotIn("ambient-secret", repr(env))
        for key in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "NODE_OPTIONS", "BROKER_LEASE",
                    "WORKBUDDY_AT_REST_ENCRYPTION", "CODEBUDDY_CREDENTIAL_BOOTSTRAP"]:
            self.assertNotIn(key, env)
        self.assertNotIn("bypassPermissions", repr(env))
        self.assertNotIn("fullAccess", repr(env))
        self.assertEqual(env["ACC_PRODUCT_CONFIG_PATH"],
                         "/synthetic/home/.workbuddy/cache/acc-product-config-v3.json")
        self.assertEqual(env["CLIENT_INFO_PLATFORM_VERSION"], "5.6.2")
        self.assertEqual(env["CLIENT_INFO_PRODUCT_NAME"], "WorkBuddy")
        self.assertNotIn("CODEBUDDY_DISABLE_PRODUCT_CACHE", env)

    def test_new_version_environment_is_exact_and_unknown_version_refuses(self):
        env = owned.launch_environment("synthetic-local-password", version="5.7.7")
        self.assertEqual(env["CODEBUDDY_DISABLE_PRODUCT_CACHE"], "1")
        for key in ("CLIENT_INFO_PLATFORM_VERSION", "CLIENT_INFO_PRODUCT_VERSION", "CLIENT_INFO_PLUGIN_VERSION"):
            self.assertEqual(env[key], "5.7.7")
        with patch.object(owned.Path, "lstat") as read, self.assertRaises(WorkBuddyError):
            owned.launch_environment("synthetic-local-password", version="99.0.0")
        read.assert_not_called()
        self.assertEqual(env["CODEBUDDY_HOST"], "workbuddy-desktop")

    def test_product_cache_path_is_passed_without_reading_or_writing_body(self):
        with patch.object(owned.Path, "read_text", side_effect=AssertionError("cache body read")), \
                patch.object(owned.Path, "read_bytes", side_effect=AssertionError("cache body read")), \
                patch.object(owned.Path, "write_text", side_effect=AssertionError("cache modified")), \
                patch.object(owned.Path, "write_bytes", side_effect=AssertionError("cache modified")):
            env = owned.launch_environment("synthetic-local-password", version="5.6.2")
        self.assertEqual(env["ACC_PRODUCT_CONFIG_PATH"],
                         "/synthetic/home/.workbuddy/cache/acc-product-config-v3.json")

    def test_missing_product_cache_refuses_launch_environment(self):
        with patch.object(owned.Path, "lstat", side_effect=FileNotFoundError), \
                self.assertRaises(WorkBuddyError):
            owned.launch_environment("synthetic-local-password", version="5.6.2")

    def test_unsafe_product_cache_metadata_refuses_launch_environment(self):
        cases = [(stat.S_IFLNK | 0o600, os.getuid(), 1234),
                 (stat.S_IFDIR | 0o700, os.getuid(), 1234),
                 (stat.S_IFREG | 0o600, os.getuid()+1, 1234),
                 (stat.S_IFREG | 0o620, os.getuid(), 1234),
                 (stat.S_IFREG | 0o602, os.getuid(), 1234),
                 (stat.S_IFREG | 0o600, os.getuid(), 0),
                 (stat.S_IFREG | 0o600, os.getuid(), 8_000_001)]
        for mode, uid, size in cases:
            with self.subTest(mode=mode, uid=uid, size=size), \
                    patch.object(owned.Path, "lstat", return_value=SimpleNamespace(
                        st_mode=mode, st_uid=uid, st_size=size)), self.assertRaises(WorkBuddyError):
                owned.launch_environment("synthetic-local-password", version="5.6.2")

    def test_listener_must_belong_to_worker_and_exact_loopback_address(self):
        for output in ["n*:3456\n", "n[::1]:3456\n", "n127.0.0.1:34567\n"]:
            with self.subTest(output=output), patch.object(owned.subprocess, "run",
                    return_value=SimpleNamespace(returncode=0, stdout=output)):
                self.assertFalse(owned.owns_listener(PID, "3456"))
        with patch.object(owned.subprocess, "run", return_value=
                SimpleNamespace(returncode=0, stdout="p4321\nn127.0.0.1:3456\n")) as run:
            self.assertTrue(owned.owns_listener(PID, "3456"))
            self.assertIn(str(PID), run.call_args.args[0])


class RecoveryTests(unittest.TestCase):
    def test_invalid_task_identity_never_inspects_or_signals_process(self):
        with patch.object(owned, "process_identity") as inspect, \
                patch.object(owned.subprocess, "run") as lookup, patch.object(owned.os, "killpg") as kill:
            for runtime in [record(task_id=".*"), record(worker_id="foreign-desktop")]:
                self.assertFalse(owned.recover_owned(runtime)["cleaned"])
        inspect.assert_not_called()
        lookup.assert_not_called()
        kill.assert_not_called()

    def test_foreign_or_pid_reused_worker_is_never_signalled(self):
        for current in [identity(command="foreign application"), identity(start="PID reused")]:
            with self.subTest(current=current), \
                    patch.object(owned, "process_identity", return_value=current), \
                    patch.object(owned.os, "killpg") as kill:
                self.assertFalse(owned.recover_owned(record())["cleaned"])
                kill.assert_not_called()

    def test_worker_in_desktop_process_group_is_never_signalled(self):
        with patch.object(owned, "process_identity", return_value=identity()), \
                patch.object(owned.os, "getpgid", return_value=999), patch.object(owned.os, "killpg") as kill:
            self.assertFalse(owned.recover_owned(record())["cleaned"])
        kill.assert_not_called()

    def test_missing_wrapper_with_remaining_children_is_not_cleaned_or_killed(self):
        children = {PID + 1: {"uid": os.getuid(), "start": START}}
        with patch.object(owned, "process_identity", return_value=None), \
                patch.object(owned, "process_group_members", return_value=children), \
                patch.object(owned.os, "killpg") as kill:
            self.assertFalse(owned.recover_owned(record())["cleaned"])
        kill.assert_not_called()

    def test_already_empty_group_is_cleaned_without_signals(self):
        with patch.object(owned, "process_identity", return_value=None), \
                patch.object(owned, "process_group_members", return_value={}), \
                patch.object(owned.os, "killpg") as kill:
            self.assertTrue(owned.recover_owned(record())["cleaned"])
        kill.assert_not_called()

    def test_known_child_is_cleaned_even_if_wrapper_exits_first(self):
        child = {PID + 1: {"uid": os.getuid(), "start": START}}
        all_members = {PID: {"uid": os.getuid(), "start": START}, **child}
        with patch.object(owned, "process_identity", return_value=identity()), \
                patch.object(owned.os, "getpgid", return_value=PID), \
                patch.object(owned, "process_group_members", side_effect=[all_members, all_members, child, {}]), \
                patch.object(owned.time, "monotonic", side_effect=[0, 3, 4, 5]), \
                patch.object(owned.os, "killpg") as kill:
            self.assertTrue(owned.recover_owned(record())["cleaned"])
        self.assertEqual([call.args for call in kill.call_args_list],
                         [(PID, signal.SIGTERM), (PID, signal.SIGKILL)])

    def test_group_replacement_after_term_is_not_signalled_again(self):
        initial = {PID: {"uid": os.getuid(), "start": START}}
        replacement = {PID + 1: {"uid": os.getuid(), "start": "unknown start"}}
        with patch.object(owned, "process_identity", return_value=identity()), \
                patch.object(owned.os, "getpgid", return_value=PID), \
                patch.object(owned, "process_group_members", side_effect=[initial, initial, replacement]), \
                patch.object(owned.time, "monotonic", side_effect=[0, 1]), \
                patch.object(owned.os, "killpg") as kill:
            self.assertFalse(owned.recover_owned(record())["cleaned"])
        kill.assert_called_once_with(PID, signal.SIGTERM)

    def test_group_containing_foreign_uid_cannot_be_signalled(self):
        members = {PID: {"uid": os.getuid(), "start": START},
                   PID + 1: {"uid": os.getuid()+1, "start": START}}
        with patch.object(owned, "process_identity", return_value=identity()), \
                patch.object(owned.os, "getpgid", return_value=PID), \
                patch.object(owned, "process_group_members", return_value=members), \
                patch.object(owned.os, "killpg") as kill:
            self.assertFalse(owned.recover_owned(record())["cleaned"])
        kill.assert_not_called()

    def test_without_saved_pid_only_unique_owned_wrapper_is_recovered(self):
        runtime = record()
        runtime.pop("pid")
        members = {PID: {"uid": os.getuid(), "start": START}}
        with patch.object(owned.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=str(PID))) as lookup, \
                patch.object(owned, "process_identity", return_value=identity()), \
                patch.object(owned.os, "getpgid", return_value=PID), \
                patch.object(owned, "process_group_members", side_effect=[members, members, {}]), \
                patch.object(owned.time, "monotonic", side_effect=[0, 1]), \
                patch.object(owned.os, "killpg") as kill:
            self.assertTrue(owned.recover_owned(runtime)["cleaned"])
        args = lookup.call_args.args[0]
        self.assertEqual(args[:3], ["pgrep", "-u", str(os.getuid())])
        self.assertIn(re.escape(TASK), args[-1])
        kill.assert_called_once_with(PID, signal.SIGTERM)

    def test_ambiguous_wrapper_matches_are_not_killed(self):
        runtime = record()
        runtime.pop("pid")
        with patch.object(owned.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="4321 4322")), \
                patch.object(owned, "process_identity") as inspect, patch.object(owned.os, "killpg") as kill:
            self.assertFalse(owned.recover_owned(runtime)["cleaned"])
        inspect.assert_not_called()
        kill.assert_not_called()

    def test_failed_wrapper_search_does_not_claim_worker_absence(self):
        runtime = record()
        runtime.pop("pid")
        for returncode in [2, 3]:
            with self.subTest(returncode=returncode), \
                    patch.object(owned.subprocess, "run", return_value=SimpleNamespace(
                        returncode=returncode, stdout="")), \
                    patch.object(owned.os, "killpg") as kill, self.assertRaises(WorkBuddyError):
                owned.recover_owned(runtime)
            kill.assert_not_called()

    def test_no_wrapper_matches_with_successful_search_confirms_absence(self):
        runtime = record()
        runtime.pop("pid")
        with patch.object(owned.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="")), \
                patch.object(owned.os, "killpg") as kill:
            self.assertTrue(owned.recover_owned(runtime)["cleaned"])
        kill.assert_not_called()

    def test_wrapper_absent_but_owned_reaper_alive_cannot_confirm_cleanup(self):
        runtime = record()
        runtime.pop("pid")
        with patch.object(owned.subprocess, "run", side_effect=[
                SimpleNamespace(returncode=1, stdout=""), SimpleNamespace(returncode=0, stdout="5432")]) as lookup, \
                patch.object(owned.os, "killpg") as kill:
            self.assertFalse(owned.recover_owned(runtime)["cleaned"])
        self.assertIn("worker_reaper", lookup.call_args.args[0][-1])
        kill.assert_not_called()

    def test_failed_reaper_search_cannot_confirm_cleanup(self):
        runtime = record()
        runtime.pop("pid")
        with patch.object(owned.subprocess, "run", side_effect=[
                SimpleNamespace(returncode=1, stdout=""), SimpleNamespace(returncode=2, stdout="")]), \
                self.assertRaises(WorkBuddyError):
            owned.recover_owned(runtime)

    def test_group_inspection_includes_foreign_uid_but_excludes_zombies_and_other_groups(self):
        rows = [f"{os.getuid()} {PID} {PID} {START} S",
                f"{os.getuid()+1} {PID+1} {PID} {START} S",
                f"{os.getuid()} {PID+2} {PID} {START} Z",
                f"{os.getuid()} {PID+3} 999 {START} S"]
        with patch.object(owned.subprocess, "run", return_value=
                SimpleNamespace(returncode=0, stdout="\n".join(rows))):
            members = owned.process_group_members(PID)
        self.assertEqual(set(members), {PID, PID+1})
        self.assertEqual(members[PID+1]["uid"], os.getuid()+1)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        env = patch.object(owned, "launch_environment", return_value={"SYNTHETIC": "1"})
        env.start()
        self.addCleanup(env.stop)
        directory = patch.object(owned.tempfile, "mkdtemp", return_value="/synthetic/wbim-11111111-test")
        directory.start()
        self.addCleanup(directory.stop)

    def instance(self, callback=None):
        with patch.object(owned, "gui_identity", return_value={"pid": 100, "start": START, "version": "5.6.2"}):
            return owned.OwnedRuntime(TASK, "/tmp", ["synthetic-entry", "--serve"], callback or Mock())

    def test_stop_clears_in_memory_password_and_output(self):
        runtime = self.instance()
        runtime._buffer = "private-output"
        with patch.object(owned, "recover_owned", return_value={"cleaned": True}):
            self.assertTrue(runtime.stop()["cleaned"])
        self.assertEqual(runtime.password, "")
        self.assertEqual(runtime._buffer, "")
        self.assertTrue(runtime._closed.is_set())

    def test_stop_rechecks_delayed_reaper_without_overwriting_failure(self):
        for final in ({"cleaned": True}, {"cleaned": False, "reason": "still pending"}):
            runtime = self.instance()
            runtime.process = Mock()
            with self.subTest(final=final), patch.object(owned, "recover_owned", side_effect=[
                    {"cleaned": False, "reason": "reaper finishing"}, final]) as recover:
                self.assertEqual(runtime.stop(), final)
            runtime.process.wait.assert_called_once_with(timeout=2)
            self.assertEqual(recover.call_count, 2)

    def test_update_between_identity_and_spawn_refuses_worker(self):
        runtime = self.instance()
        with patch.object(owned, "app_info", return_value={"version": "5.7.7", "intel_binary": True}), \
                patch.object(owned.subprocess, "Popen") as launch, self.assertRaises(WorkBuddyError):
            runtime.start(threading.Event())
        launch.assert_not_called()

    def test_runtime_event_stop_before_spawn_cannot_launch_worker(self):
        runtime = self.instance()
        runtime.on_event = lambda event: runtime.stop()
        with patch.object(owned, "recover_owned", return_value={"cleaned": True}), \
                patch.object(owned, "app_info", return_value={"executable": "synthetic-app", "version": "5.6.2", "intel_binary": True}), \
                patch.object(owned.subprocess, "Popen") as launch, self.assertRaises(WorkBuddyError):
            runtime.start(threading.Event())
        launch.assert_not_called()

    def test_runtime_event_cancel_before_spawn_cannot_launch_worker(self):
        cancelled = threading.Event()
        runtime = self.instance(lambda event: cancelled.set())
        with patch.object(owned, "app_info", return_value={"executable": "synthetic-app", "version": "5.6.2", "intel_binary": True}), \
                patch.object(owned.subprocess, "Popen") as launch, self.assertRaises(WorkBuddyError):
            runtime.start(cancelled)
        launch.assert_not_called()

    def test_gui_exit_reports_loss_and_cleans_even_if_event_callback_raises(self):
        callback = Mock(side_effect=RuntimeError("synthetic callback failure"))
        runtime = self.instance(callback)
        runtime.process = Mock()
        runtime.process.poll.return_value = None
        runtime._closed = Mock()
        runtime._closed.wait.return_value = False
        runtime.stop = Mock()
        with patch.object(owned, "process_identity", return_value=None), \
                self.assertRaises(RuntimeError):
            runtime._watch()
        self.assertTrue(runtime.lost.is_set())
        callback.assert_called_once_with({"type": "runtime_lost", "diagnostics": {"reason": "gui_changed"}})
        runtime.stop.assert_called_once()

    def test_gui_pid_reuse_is_runtime_loss(self):
        runtime = self.instance()
        runtime.process = Mock()
        runtime._closed = Mock()
        runtime._closed.wait.return_value = False
        runtime.stop = Mock()
        with patch.object(owned, "process_identity", return_value=identity(start="new GUI start")):
            runtime._watch()
        self.assertTrue(runtime.lost.is_set())
        runtime.stop.assert_called_once()

    def test_gui_probe_timeout_does_not_stop_a_live_worker(self):
        runtime = self.instance()
        runtime.process = Mock()
        runtime.process.poll.return_value = None
        runtime._closed = Mock()
        runtime._closed.wait.side_effect = [False, False, True]
        runtime.stop = Mock()
        with patch.object(owned, "process_identity", side_effect=[
                owned.subprocess.TimeoutExpired("ps", 3), identity()]):
            runtime._watch()
        self.assertFalse(runtime.lost.is_set())
        runtime.stop.assert_not_called()

    def test_child_exit_is_detected_even_if_gui_probe_times_out(self):
        runtime = self.instance()
        runtime.process = Mock()
        runtime.process.poll.return_value = 3
        runtime._closed = Mock()
        runtime._closed.wait.return_value = False
        runtime.stop = Mock()
        with patch.object(owned, "process_identity", side_effect=owned.subprocess.TimeoutExpired("ps", 3)):
            runtime._watch()
        self.assertTrue(runtime.lost.is_set())
        runtime.stop.assert_called_once()

    def test_start_does_not_authenticate_another_process_listener(self):
        cancelled = threading.Event()
        runtime = self.instance()
        process = Mock(pid=PID)
        runtime._buffer = "INTELMAC_PROTECTED_RUNTIME_READY\nhttp://127.0.0.1:3456"
        with patch.object(owned, "app_info", return_value={"executable": "synthetic-app", "version": "5.6.2", "intel_binary": True}), \
                patch.object(owned.subprocess, "Popen", return_value=process), \
                patch.object(owned, "process_identity", return_value=identity()), \
                patch.object(owned.threading.Thread, "start"), \
                patch.object(owned, "owns_listener", return_value=False), \
                patch.object(owned, "auth_status") as authenticate, \
                patch.object(cancelled, "wait", side_effect=lambda timeout: cancelled.set()), \
                self.assertRaises(WorkBuddyError):
            runtime.start(cancelled)
        authenticate.assert_not_called()

    def test_auth_must_be_enabled_before_start_is_ready(self):
        for status in [{"auth_enabled": False, "authenticated": True},
                       {"auth_enabled": True, "authenticated": False}]:
            cancelled = threading.Event()
            runtime = self.instance()
            runtime._buffer = "INTELMAC_PROTECTED_RUNTIME_READY\nhttp://127.0.0.1:3456"
            with self.subTest(status=status), \
                    patch.object(owned, "app_info", return_value={"executable": "synthetic-app", "version": "5.6.2", "intel_binary": True}), \
                    patch.object(owned.subprocess, "Popen", return_value=Mock(pid=PID)), \
                    patch.object(owned, "process_identity", return_value=identity()), \
                    patch.object(owned.threading.Thread, "start"), \
                    patch.object(owned, "owns_listener", return_value=True), \
                    patch.object(owned, "auth_status", return_value=status), \
                    patch.object(cancelled, "wait", side_effect=lambda timeout: cancelled.set()), \
                    self.assertRaises(WorkBuddyError):
                runtime.start(cancelled)

    def test_authenticated_listener_without_native_bootstrap_marker_is_not_ready(self):
        cancelled = threading.Event()
        runtime = self.instance()
        runtime._buffer = "http://127.0.0.1:3456"
        with patch.object(owned, "app_info", return_value={"executable": "synthetic-app", "version": "5.6.2", "intel_binary": True}), \
                patch.object(owned.subprocess, "Popen", return_value=Mock(pid=PID)), \
                patch.object(owned, "process_identity", return_value=identity()), \
                patch.object(owned.threading.Thread, "start"), \
                patch.object(owned, "owns_listener", return_value=True), \
                patch.object(owned, "auth_status", return_value={"auth_enabled": True, "authenticated": True}) as auth, \
                patch.object(cancelled, "wait", side_effect=lambda timeout: cancelled.set()), \
                self.assertRaises(WorkBuddyError):
            runtime.start(cancelled)
        auth.assert_not_called()

    def test_protected_marker_owned_listener_and_authentication_all_make_runtime_ready(self):
        runtime = self.instance()
        runtime._buffer = "INTELMAC_PROTECTED_RUNTIME_READY\nhttp://127.0.0.1:3456"
        password = runtime.password
        with patch.object(owned, "app_info", return_value={"executable": "synthetic-app", "version": "5.6.2", "intel_binary": True}), \
                patch.object(owned.subprocess, "Popen", return_value=Mock(pid=PID)) as launch, \
                patch.object(owned, "process_identity", return_value=identity()), \
                patch.object(owned.threading.Thread, "start"), \
                patch.object(owned, "owns_listener", return_value=True), \
                patch.object(owned, "auth_status", return_value={"auth_enabled": True, "authenticated": True}):
            endpoint, returned_password = runtime.start(threading.Event())
        self.assertEqual(endpoint, "http://127.0.0.1:3456/api/v1/acp")
        self.assertEqual(returned_password, password)
        self.assertNotIn(password, repr(runtime.runtime))
        self.assertEqual(launch.call_args.kwargs["env"]["WORKBUDDY_BRIDGE_BOOTSTRAP_DIR"],
                         "/synthetic/wbim-11111111-test")


class StartupDirectoryTests(unittest.TestCase):
    def directory(self):
        path = Path(tempfile.mkdtemp(prefix="wbim-" + TASK[:8] + "-"))
        self.addCleanup(lambda: path.exists() and path.rmdir())
        return path

    def test_only_owned_startup_sockets_are_removed_after_process_cleanup(self):
        directory = self.directory()
        sockets = []
        for name in ["r.sock", "c.sock"]:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sockets.append(connection)
            self.addCleanup(connection.close)
            connection.bind(str(directory / name))
        runtime = record(startup_dir=str(directory))
        with patch.object(owned, "_recover_owned_processes", return_value={"cleaned": True}):
            self.assertTrue(owned.recover_owned(runtime)["cleaned"])
        self.assertFalse(directory.exists())

    def test_unconfirmed_process_cleanup_preserves_startup_directory(self):
        directory = self.directory()
        with patch.object(owned, "_recover_owned_processes", return_value={"cleaned": False}):
            self.assertFalse(owned.recover_owned(record(startup_dir=str(directory)))["cleaned"])
        self.assertTrue(directory.exists())

    def test_unexpected_file_is_preserved_and_prevents_confirmation(self):
        directory = self.directory()
        unexpected = directory / "user-file"
        unexpected.write_text("synthetic user content")
        self.addCleanup(unexpected.unlink)
        with patch.object(owned, "_recover_owned_processes", return_value={"cleaned": True}):
            self.assertFalse(owned.recover_owned(record(startup_dir=str(directory)))["cleaned"])
        self.assertEqual(unexpected.read_text(), "synthetic user content")

    def test_symlink_directory_does_not_delete_target(self):
        target = self.directory()
        alias = Path(tempfile.gettempdir()) / (target.name + "-alias")
        alias.symlink_to(target, target_is_directory=True)
        self.addCleanup(alias.unlink)
        self.assertFalse(owned.cleanup_startup_directory(record(startup_dir=str(alias))))
        self.assertTrue(target.exists())

    def test_socket_named_symlink_is_not_unlinked(self):
        directory = self.directory()
        with tempfile.NamedTemporaryFile(prefix="wb-user-file-", dir="/tmp") as external:
            target = Path(external.name)
            link = directory / "r.sock"
            link.symlink_to(target)
            self.addCleanup(link.unlink)
            self.assertFalse(owned.cleanup_startup_directory(record(startup_dir=str(directory))))
            self.assertTrue(link.is_symlink())
            self.assertTrue(target.exists())

    def test_foreign_owner_or_unsafe_directory_mode_is_not_deleted(self):
        directory = self.directory()
        for mode, uid in [(stat.S_IFDIR | 0o700, os.getuid()+1), (stat.S_IFDIR | 0o755, os.getuid())]:
            with self.subTest(mode=mode, uid=uid), patch.object(owned.Path, "lstat", return_value=
                    SimpleNamespace(st_mode=mode, st_uid=uid)):
                self.assertFalse(owned.cleanup_startup_directory(record(startup_dir=str(directory))))
        self.assertTrue(directory.exists())

    def test_unrelated_directory_name_is_not_deleted(self):
        with tempfile.TemporaryDirectory(prefix="other-runtime-") as path:
            self.assertFalse(owned.cleanup_startup_directory(record(startup_dir=path)))
            self.assertTrue(Path(path).exists())

    def test_replaced_socket_inode_is_not_deleted(self):
        directory = self.directory()
        path = directory / "r.sock"
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(connection.close)
        connection.bind(str(path))
        self.addCleanup(path.unlink)
        original = Path.lstat
        calls = 0
        def metadata(candidate):
            nonlocal calls
            value = original(candidate)
            if candidate == path:
                calls += 1
                if calls == 2:
                    return SimpleNamespace(st_mode=value.st_mode, st_uid=value.st_uid,
                        st_dev=value.st_dev, st_ino=value.st_ino + 1)
            return value
        with patch.object(Path, "lstat", metadata):
            self.assertFalse(owned.cleanup_startup_directory(record(startup_dir=str(directory))))
        self.assertTrue(path.exists())

    def test_file_arriving_after_validation_is_preserved(self):
        directory = self.directory()
        path = directory / "r.sock"
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(connection.close)
        connection.bind(str(path))
        foreign = directory / "late-user-file"
        self.addCleanup(lambda: foreign.unlink(missing_ok=True))
        unlink = Path.unlink
        def remove(candidate, *args, **kwargs):
            result = unlink(candidate, *args, **kwargs)
            if candidate == path:
                foreign.write_text("preserve this file")
            return result
        with patch.object(Path, "unlink", remove):
            self.assertFalse(owned.cleanup_startup_directory(record(startup_dir=str(directory))))
        self.assertEqual(foreign.read_text(), "preserve this file")


if __name__ == "__main__":
    unittest.main()
