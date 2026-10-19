"""Cold-start owned WorkBuddy runtime, with no application database writes."""
from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid

from .discovery import APP, WORKER_PREFIX, WorkBuddyError, app_info, auth_status
from .compatibility import SUPPORTED_VERSIONS

LAUNCHER = Path(__file__).with_name("worker_launcher.cjs").resolve()


def process_identity(pid: int) -> dict | None:
    if type(pid) is not int or pid <= 1:
        return None
    result = subprocess.run(["ps", "-p", str(pid), "-o", "uid=,lstart=,stat=,command="],
                            capture_output=True, text=True, timeout=3)
    parts = result.stdout.strip().split(maxsplit=7)
    if result.returncode or len(parts) != 8 or parts[0] != str(os.getuid()) or parts[6].startswith("Z"):
        return None
    return {"start": " ".join(parts[1:6]), "command": parts[7]}


def gui_identity() -> dict:
    info = app_info()
    if info["version"] not in SUPPORTED_VERSIONS or not info["intel_binary"]:
        raise WorkBuddyError("当前应用版本或架构尚未通过冷启动适配")
    result = subprocess.run(["ps", "-axo", "uid=,pid=,args="],
                            capture_output=True, text=True, timeout=5)
    found = []
    executable = str(info["executable"])
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) != 3 or parts[0] != str(os.getuid()):
            continue
        command = parts[2]
        if command != executable and not command.startswith(executable + " --"):
            continue
        if any(flag in command for flag in ("--serve", "--type=", "--run-as-node", "--prewarm")):
            continue
        identity = process_identity(int(parts[1]))
        if identity:
            found.append({"pid": int(parts[1]), "start": identity["start"]})
    if len(found) != 1:
        raise WorkBuddyError("冷启动需要唯一的 WorkBuddy 桌面实例；请启动应用并登录")
    return {**found[0], "version": info["version"]}


def launch_environment(password: str, *, version: str) -> dict:
    if version not in SUPPORTED_VERSIONS:
        raise WorkBuddyError("当前应用版本尚未通过冷启动适配")
    # Inherit ordinary system paths, never ambient model keys, broker leases,
    # credential bootstrap or Node/Electron overrides from the Codex process.
    keys = ("HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env.update({"ELECTRON_RUN_AS_NODE": "1", "CODEBUDDY_DISABLE_IDE": "1",
                "CODEBUDDY_FORCE_LITE_WB_BUNDLE": "1",
                "CODEBUDDY_FORCE_HEADLESS_BUNDLE": "1",
                "CODEBUDDY_CODE_DISABLE_TERMINAL_TITLE": "1",
                "CODEBUDDY_CONFIG_DIR": str(Path.home() / ".workbuddy"),
                "CODEBUDDY_GATEWAY_AUTH": "password",
                "CODEBUDDY_GATEWAY_PASSWORD": password,
                "CODEBUDDY_GATEWAY_DISABLE_API_DOCS": "1",
                "WORKBUDDY_BRIDGE_APP_VERSION": version,
                "CLIENT_INFO_PLATFORM": "WorkBuddy",
                "CLIENT_INFO_PLATFORM_VERSION": version,
                "CLIENT_INFO_IDE_TYPE": "WorkBuddy",
                "CLIENT_INFO_PRODUCT_NAME": "WorkBuddy",
                "CLIENT_INFO_PRODUCT_VERSION": version,
                "CLIENT_INFO_PLUGIN_NAME": "workbuddy-desktop",
                "CLIENT_INFO_PLUGIN_VERSION": version,
                "CODEBUDDY_HOST": "workbuddy-desktop"})
    if version in {"5.7.7", "5.8.2", "5.8.3"}:
        # The CLI otherwise freezes ACP's initial model catalog from its
        # older localStorage cache before the GUI product snapshot arrives.
        # Skip that read for this worker; do not delete or rewrite the cache.
        env["CODEBUDDY_DISABLE_PRODUCT_CACHE"] = "1"
    # The GUI already maintains this product snapshot. Passing its path avoids
    # an older standalone product/model snapshot during early CLI argument setup.
    # The bridge neither reads its body nor copies or rewrites it.
    product = Path.home() / ".workbuddy/cache/acc-product-config-v3.json"
    try:
        metadata = product.lstat()
    except OSError:
        raise WorkBuddyError("WorkBuddy 产品配置尚未就绪；请在应用中登录并等待配置更新") from None
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o022 or not 0 < metadata.st_size <= 8_000_000):
        raise WorkBuddyError("WorkBuddy 产品配置文件归属或权限不符合要求")
    env["ACC_PRODUCT_CONFIG_PATH"] = str(product)
    return env


def owned_identity(runtime: dict, pid: int) -> dict | None:
    task = str(runtime.get("task_id", ""))
    try:
        if str(uuid.UUID(task)) != task:
            return None
    except ValueError:
        return None
    if runtime.get("worker_id") != WORKER_PREFIX + task:
        return None
    identity = process_identity(pid)
    if not identity or (runtime.get("process_start") and identity["start"] != runtime["process_start"]):
        return None
    command = identity["command"]
    executable = str(APP / "Contents/MacOS/Electron")
    entry = str(APP / "Contents/Resources/app.asar/cli/bin/codebuddy")
    if (not command.startswith(executable + " ") or str(LAUNCHER) + " " + task + " " not in command
            or entry + " --serve " not in command):
        return None
    return identity


def owns_listener(pid: int, port: str) -> bool:
    try:
        result = subprocess.run(["lsof", "-nP", "-a", "-p", str(pid), "-iTCP:" + port,
                                 "-sTCP:LISTEN", "-Fn"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and "n127.0.0.1:" + port in result.stdout.splitlines()


def process_group_members(group: int) -> dict[int, dict]:
    """Read live group identities, including foreign UIDs to reject them."""
    if type(group) is not int or group <= 1:
        return {}
    result = subprocess.run(["ps", "-axo", "uid=,pid=,pgid=,lstart=,stat="],
                            capture_output=True, text=True, timeout=3)
    if result.returncode:
        raise WorkBuddyError("无法确认 WorkBuddy 自有进程组的结束状态")
    members = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) != 9 or parts[2] != str(group) or parts[8].startswith("Z"):
            continue
        if not parts[0].isdigit() or not parts[1].isdigit():
            continue
        members[int(parts[1])] = {"uid": int(parts[0]), "start": " ".join(parts[3:8])}
    return members


def _recover_owned_processes(runtime: dict) -> dict:
    task = str(runtime.get("task_id", ""))
    try:
        valid = str(uuid.UUID(task)) == task
    except ValueError:
        valid = False
    if not valid or runtime.get("worker_id") != WORKER_PREFIX + task:
        return {"cleaned": False, "reason": "Invalid task identity"}
    pid = runtime.get("pid")
    if type(pid) is not int:
        # A crash between Popen and recording its PID: search only this UUID's
        # wrapper, then perform the same full identity check before signalling.
        task = str(runtime.get("task_id", ""))
        try:
            if str(uuid.UUID(task)) != task:
                return {"cleaned": False, "reason": "Invalid task identity"}
        except ValueError:
            return {"cleaned": False, "reason": "Invalid task identity"}
        result = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f",
                                 re.escape(str(LAUNCHER) + " " + task + " ")],
                                capture_output=True, text=True, timeout=3)
        if result.returncode not in {0, 1}:
            raise WorkBuddyError("无法确认自有运行时是否仍存在")
        candidates = [int(item) for item in result.stdout.split() if item.isdigit()]
        if not candidates:
            # The root may have exited before its PID was persisted, while its
            # UUID's reaper still holds the group during descendant shutdown.
            reaper = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f",
                                     re.escape(str(LAUNCHER.with_name("worker_reaper.cjs")) + " " + task + " ")],
                                    capture_output=True, text=True, timeout=3)
            if reaper.returncode not in {0, 1}:
                raise WorkBuddyError("无法确认自有清理进程是否仍存在")
            if reaper.returncode == 0:
                return {"cleaned": False, "reason": "Owned reaper still stopping descendants"}
            return {"cleaned": True, "reason": "Worker absent"}
        if len(candidates) != 1:
            return {"cleaned": False, "reason": "Ambiguous worker identity"}
        pid = candidates[0]
    current = process_identity(pid)
    if current is None:
        if process_group_members(pid):
            # Without the wrapper's identity we cannot establish ownership of
            # remaining members during recovery. Do not signal an unknown group.
            return {"cleaned": False, "reason": "Worker absent but process group remains"}
        return {"cleaned": True, "reason": "Worker already stopped"}
    if not owned_identity(runtime, pid):
        return {"cleaned": False, "reason": "Worker identity no longer matches"}
    # Each worker has its own session/process group, never a desktop group.
    try:
        group = os.getpgid(pid)
    except ProcessLookupError:
        return {"cleaned": not bool(process_group_members(pid))}
    if group != pid:
        return {"cleaned": False, "reason": "Worker process group mismatch"}
    known = process_group_members(group)
    if (pid not in known or known[pid]["start"] != current["start"]
            or any(member["uid"] != os.getuid() for member in known.values())):
        return {"cleaned": False, "reason": "Worker group identity could not be confirmed"}

    def confirmed_members():
        members = process_group_members(group)
        if any(member["uid"] != os.getuid() for member in members.values()):
            return members, False
        # An original, same-start member anchors group ownership even when the
        # wrapper exits first. If every known member is gone, do not infer that
        # an unrecognised remaining group still belongs to this task.
        anchored = any(member == known.get(member_pid) for member_pid, member in members.items())
        return members, anchored

    for sig, allowance in ((signal.SIGTERM, 3), (signal.SIGKILL, 2)):
        members, anchored = confirmed_members()
        if not members:
            return {"cleaned": True}
        if not anchored:
            return {"cleaned": False, "reason": "Remaining process group identity no longer matches"}
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            return {"cleaned": True}
        deadline = time.monotonic() + allowance
        while time.monotonic() < deadline:
            members, anchored = confirmed_members()
            if not members:
                return {"cleaned": True}
            if not anchored:
                return {"cleaned": False, "reason": "Remaining process group identity no longer matches"}
            time.sleep(0.1)
    return {"cleaned": False, "reason": "Worker termination not confirmed"}


def cleanup_startup_directory(runtime: dict) -> bool:
    if not runtime.get("startup_dir"):
        return True
    directory = Path(runtime["startup_dir"])
    prefix = "wbim-" + str(runtime.get("task_id", ""))[:8] + "-"
    if directory.parent.resolve() != Path(tempfile.gettempdir()).resolve() or not directory.name.startswith(prefix):
        return False
    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        return True
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700):
        return False
    verified = []
    for item in directory.iterdir():
        info = item.lstat()
        if item.name not in {"r.sock", "c.sock"} or not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            return False
        verified.append((item, info))
    latest_directory = directory.lstat()
    if (latest_directory.st_dev, latest_directory.st_ino) != (metadata.st_dev, metadata.st_ino):
        return False
    for item, info in verified:
        try:
            latest = item.lstat()
        except FileNotFoundError:
            continue
        if ((latest.st_dev, latest.st_ino) != (info.st_dev, info.st_ino)
                or not stat.S_ISSOCK(latest.st_mode) or latest.st_uid != os.getuid()):
            return False
        item.unlink()
    try:
        directory.rmdir()
    except OSError:
        return False
    return True


def recover_owned(runtime: dict) -> dict:
    result = _recover_owned_processes(runtime)
    if result.get("cleaned") and not cleanup_startup_directory(runtime):
        return {"cleaned": False, "reason": "Private startup directory cleanup not confirmed"}
    return result


class OwnedRuntime:
    def __init__(self, task_id: str, cwd: str, args: list[str], on_event, *, model: str | None = None):
        self.gui = gui_identity()
        self.runtime = {"launcher": "owned", "task_id": task_id,
                        "worker_id": WORKER_PREFIX + task_id,
                        "gui_pid": self.gui["pid"], "gui_start": self.gui["start"],
                        "version": self.gui["version"]}
        self.args, self.cwd, self.on_event = args, cwd, on_event
        self.model = model
        self.password = secrets.token_urlsafe(32)
        self.process = None
        self._buffer = ""
        self._lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._closed = threading.Event()
        self.lost = threading.Event()

    def _read(self):
        while not self._closed.is_set():
            data = self.process.stdout.read1(8192)
            if not data:
                return
            with self._lock:
                self._buffer = (self._buffer + data.decode(errors="replace"))[-1_000_000:]

    def _watch(self):
        while not self._closed.wait(0.5):
            gui_verified = True
            try:
                gui = process_identity(self.gui["pid"])
            except (OSError, subprocess.SubprocessError):
                # A busy desktop can time out ps while reopening. An
                # inconclusive probe is not evidence that the GUI exited.
                # Continue checking the owned child and retry the GUI probe.
                gui_verified = False
                gui = None
            child_exited = self.process.poll() is not None
            if child_exited or (gui_verified and (not gui or gui["start"] != self.gui["start"])):
                self.lost.set()
                diagnostics = {"reason": "worker_exited" if child_exited else "gui_changed"}
                if child_exited:
                    diagnostics["exit_code"] = self.process.returncode
                    with self._lock:
                        stages = re.findall(r"INTELMAC_STAGE_(product|native|handshake|cli|credential_response|credential_connect|credential_ack|cli_load)\n", self._buffer)
                    if stages:
                        diagnostics["startup_stage"] = stages[-1]
                try:
                    self.on_event({"type": "runtime_lost", "diagnostics": diagnostics})
                finally:
                    self.stop()
                return

    def start(self, cancelled: threading.Event) -> tuple[str, str]:
        if cancelled.is_set() or self._closed.is_set():
            raise WorkBuddyError("任务已取消")
        info = app_info()
        if info["version"] != self.runtime["version"] or not info["intel_binary"]:
            raise WorkBuddyError("应用在任务启动期间已更新或架构改变；任务未发送")
        with self._stop_lock:
            if cancelled.is_set() or self._closed.is_set():
                raise WorkBuddyError("任务已取消")
            self.runtime["startup_dir"] = tempfile.mkdtemp(prefix="wbim-" + self.runtime["task_id"][:8] + "-")
        self.on_event({"type": "runtime", "runtime": dict(self.runtime)})
        # Cancellation can happen while the initial runtime event is persisted.
        # Spawn and identity recording form one short transaction with stop().
        # Do not hold this lock while waiting for startup or invoking callbacks.
        with self._stop_lock:
            if cancelled.is_set() or self._closed.is_set():
                raise WorkBuddyError("任务已取消")
            password = self.password
            if not password:
                raise WorkBuddyError("WorkBuddy 自有运行时启动凭证不可用")
            environment = launch_environment(password, version=self.runtime["version"])
            if self.model is not None:
                environment["WORKBUDDY_BRIDGE_MODEL"] = self.model
            if len(os.fsencode(str(Path(self.runtime["startup_dir"]) / "c.sock"))) > 100:
                raise WorkBuddyError("WorkBuddy 私有启动通道路径过长")
            environment["WORKBUDDY_BRIDGE_BOOTSTRAP_DIR"] = self.runtime["startup_dir"]
            self.process = subprocess.Popen(
                [str(info["executable"]), str(LAUNCHER), self.runtime["task_id"], str(os.getpid()),
                 *self.args, "--port", "0", "--host", "127.0.0.1"], cwd=self.cwd,
                env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            self.runtime["pid"] = self.process.pid
            identity = process_identity(self.process.pid)
            if not identity:
                self.process.wait(timeout=3)
                raise WorkBuddyError("WorkBuddy 自有运行时未能启动")
            self.runtime["process_start"] = identity["start"]
        self.on_event({"type": "runtime", "runtime": dict(self.runtime)})
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if cancelled.is_set() or self._closed.is_set() or self.lost.is_set():
                raise WorkBuddyError("任务取消或桌面运行时中断", status="uncertain")
            with self._lock:
                ports = set(re.findall(r"http://127\.0\.0\.1:(\d+)", self._buffer))
                protected_ready = "INTELMAC_PROTECTED_RUNTIME_READY" in self._buffer
            if not protected_ready:
                cancelled.wait(0.15)
                continue
            for port in ports:
                if not owns_listener(self.process.pid, port):
                    continue
                endpoint = f"http://127.0.0.1:{port}/api/v1/acp"
                try:
                    status = auth_status(endpoint, password)
                    if status.get("auth_enabled") is not True or status.get("authenticated") is not True:
                        continue
                except WorkBuddyError:
                    continue
                self.runtime["endpoint"] = endpoint
                self.on_event({"type": "runtime", "runtime": dict(self.runtime)})
                if cancelled.is_set() or self._closed.is_set() or self.lost.is_set():
                    raise WorkBuddyError("任务取消或桌面运行时中断", status="uncertain")
                return endpoint, password
            cancelled.wait(0.15)
        raise WorkBuddyError("WorkBuddy 自有运行时启动超时")

    def stop(self):
        with self._stop_lock:
            self._closed.set()
            try:
                result = recover_owned(dict(self.runtime))
                if self.process:
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                # Descendant reaping can finish while wait() collects the
                # wrapper. Recheck with the same ownership guards before
                # persisting a stale pending result.
                if result.get("cleaned") is not True:
                    result = recover_owned(dict(self.runtime))
                return result
            finally:
                self.password = ""
                with self._lock:
                    self._buffer = ""
