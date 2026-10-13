"""Verified WorkBuddy desktop adapter using isolated, owned workers."""
from __future__ import annotations

import re
from pathlib import Path
import threading
import time
import uuid
from urllib.parse import unquote

from . import __version__
from .compatibility import SUPPORTED_VERSIONS
from .discovery import (WORKER_PREFIX, WorkBuddyError, app_info, auth_status,
                        check_endpoint, discover, find_sockets, rpc)
from .transport import AcpClient
from .owned_runtime import OwnedRuntime, gui_identity, recover_owned


def health() -> dict:
    try:
        desktop = discover()
        supported = desktop.version in SUPPORTED_VERSIONS
        return {"available": True, "read_only": True, "bridge_version": __version__, "version": desktop.version,
                "ready_for_task": supported, "runtime_ready": True, "execution_mode": "sidecar",
                "protocol": desktop.protocol, "authenticated": True,
                "supported": supported}
    except WorkBuddyError as error:
        if error.kind == "cold_start":
            try:
                gui = gui_identity()
                return {"available": True, "ready_for_task": True, "runtime_ready": False,
                        "read_only": True, "bridge_version": __version__, "version": gui["version"], "protocol": None,
                        "authenticated": None, "supported": True, "execution_mode": "owned_cold_start"}
            except WorkBuddyError:
                pass
        return {"available": False, "read_only": True, "bridge_version": __version__, "error": error.public_message}


def recover_runtime(runtime: dict) -> dict:
    """Stop a persisted owned worker, never resume or re-submit its prompt."""
    if runtime.get("launcher") == "owned":
        return recover_owned(runtime)
    worker = str(runtime.get("worker_id", ""))
    task_id = str(runtime.get("task_id", ""))
    try:
        valid_id = str(uuid.UUID(task_id)) == task_id or uuid.UUID(task_id).hex == task_id
    except ValueError:
        valid_id = False
    if not valid_id or worker != WORKER_PREFIX + task_id:
        return {"cleaned": False, "reason": "Worker identity does not belong to this task"}
    path = Path(str(runtime.get("socket_path", "")))
    if (path, runtime.get("sidecar_pid")) not in find_sockets():
        return {"cleaned": False, "reason": "Original sidecar is no longer running"}
    sessions = rpc(path, "session.list") or []
    entry = next((item for item in sessions if item.get("sessionId") == worker), None)
    if entry and runtime.get("pid") and entry.get("pid") != runtime["pid"]:
        return {"cleaned": False, "reason": "Worker PID no longer matches"}
    identity = runtime.get("runtime_id")
    # A create timeout may have spawned a worker without returning its identity.
    # Reconnect only that cryptographically unique task worker to obtain identity.
    if not identity or entry is None:
        try:
            reconnected = rpc(path, "session.reconnect", {"sessionId": worker}) or {}
        except WorkBuddyError as error:
            if error.kind == "not_found":
                return {"cleaned": True, "reason": "Worker already stopped"}
            raise
        actual_identity = reconnected.get("runtimeId")
        if identity and identity != actual_identity:
            return {"cleaned": False, "reason": "Runtime identity no longer matches"}
        identity = actual_identity
    if not identity:
        return {"cleaned": False, "reason": "Runtime identity unavailable"}
    rpc(path, "session.kill", {"sessionId": worker, "expectedRuntimeId": identity})
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        remaining = rpc(path, "session.list") or []
        if not any(item.get("sessionId") == worker for item in remaining):
            return {"cleaned": True}
        time.sleep(0.1)
    return {"cleaned": False, "reason": "Worker termination not yet confirmed"}


class WorkBuddyAdapter:
    def __init__(self, task_id: str, cwd: str, on_event, on_permission,
                 model: str = "deepseek-v4.1-flash", context_tokens: int | None = None):
        try:
            valid_id = str(uuid.UUID(task_id)) == task_id or uuid.UUID(task_id).hex == task_id
        except ValueError:
            valid_id = False
        if not valid_id:
            raise WorkBuddyError("任务 ID 必须是桥接器生成的唯一 ID")
        self.task_id = task_id
        self.model = model
        self.context_tokens = context_tokens
        self.cwd = str(Path(cwd).resolve())
        self.on_event, self.on_permission = on_event, on_permission
        self.runtime: dict = {}
        self.client: AcpClient | None = None
        self.owned: OwnedRuntime | None = None
        self._cancel = threading.Event()
        self._close_lock = threading.Lock()

    def run(self, prompt: str) -> dict:
        try:
            desktop = discover(for_write=True)
        except WorkBuddyError as error:
            if error.kind != "cold_start":
                raise
            desktop = None
        info = app_info()
        if not info.get("cli_entry_present", info["cli"].is_file()):
            raise WorkBuddyError("WorkBuddy 内部运行入口缺失，请重新安装应用")
        if self._cancel.is_set():
            raise WorkBuddyError("任务已取消")
        worker = WORKER_PREFIX + self.task_id
        worker_args = [str(info["cli"]), "--serve", "--setting-sources", "user",
                       "--model", self.model,
                       "--permission-mode", "default", "--permission-mode-before-plan", "default"]
        if self.context_tokens is not None:
            # This controls compaction in this worker only. It does not increase
            # the advertised model budget, which is checked before prompting.
            worker_args += ["--autocompact", str(self.context_tokens)]
        if desktop is None:
            # In a fresh standalone runtime --model initializes a stale model
            # snapshot before WorkBuddy's product provider is ready. Select the
            # advertised model through ACP below, still before any prompt.
            cold_args = list(worker_args)
            index = cold_args.index("--model")
            del cold_args[index:index + 2]
            self.owned = OwnedRuntime(self.task_id, self.cwd, cold_args, self.on_event, model=self.model)
            endpoint, password = self.owned.start(self._cancel)
            self.runtime = dict(self.owned.runtime)
        else:
            endpoint, password = self._start_sidecar(desktop, info, worker, worker_args)
        self.client = AcpClient(endpoint, password, self.on_event, self.on_permission)
        self.client.connect()
        session_id = self.client.new_session(self.cwd)
        self.on_event({"type": "session", "session_id": session_id})
        if self._cancel.is_set():
            raise WorkBuddyError("任务已取消")
        self.client.request("session/set_config_option", {
            "sessionId": session_id, "configId": "mode", "value": "default"})
        selected = self.client.select_model(self.model)
        selected.update(self.client.select_context(self.context_tokens, selected))
        selected["auto_compact_window_requested"] = self.context_tokens
        self.on_event({"type": "model", "model": selected})
        self.on_event({"type": "running"})
        scoped = (f"你正在执行由 Codex 委派的独立任务。工作目录：{self.cwd}\n"
                  "需要生成文件时放在这个目录中；涉及其他目录或敏感操作时保留权限请求。"
                  "完成后说明生成文件的路径。\n\n" + prompt)
        if self._cancel.is_set():
            raise WorkBuddyError("任务已取消")
        if self.owned and (self.owned.lost.is_set() or self.owned._closed.is_set()):
            raise WorkBuddyError("WorkBuddy 桌面运行时已中断", status="uncertain")
        result = self.client.prompt(scoped)
        if self.owned and self.owned.lost.is_set():
            raise WorkBuddyError("WorkBuddy 桌面已退出，任务结果不确定", status="uncertain")
        return result

    def _start_sidecar(self, desktop, info, worker, worker_args):
        self.runtime = {"task_id": self.task_id, "worker_id": worker,
                        "socket_path": str(desktop.socket_path), "sidecar_pid": desktop.sidecar_pid,
                        "version": desktop.version}
        self.on_event({"type": "runtime", "runtime": dict(self.runtime)})
        created = rpc(desktop.socket_path, "session.create", {
            "sessionId": worker, "command": str(info["executable"]),
            "args": worker_args,
            "cwd": self.cwd, "port": 0,
            "env": {"ELECTRON_RUN_AS_NODE": "1", "CODEBUDDY_DISABLE_IDE": "1",
                    "CODEBUDDY_FORCE_LITE_WB_BUNDLE": "1",
                    "CODEBUDDY_CODE_DISABLE_TERMINAL_TITLE": "1",
                    "CODEBUDDY_CONFIG_DIR": str(Path.home() / ".workbuddy")},
            "cols": 80, "rows": 24}, timeout=200)
        if not isinstance(created, dict) or created.get("sessionId") != worker or not created.get("runtimeId"):
            raise WorkBuddyError("WorkBuddy worker 启动回复缺少身份信息")
        self.runtime.update({"runtime_id": str(created["runtimeId"]),
                             "pid": int(created["pid"]), "endpoint": str(created["acpEndpoint"])})
        check_endpoint(self.runtime["endpoint"])
        self.on_event({"type": "runtime", "runtime": dict(self.runtime)})
        if self._cancel.is_set():
            raise WorkBuddyError("任务已取消")
        password = ""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self._cancel.is_set():
                raise WorkBuddyError("任务已取消")
            captured = rpc(desktop.socket_path, "session.capture", {"sessionId": worker, "lines": 300}) or {}
            match = re.search(r"[?&]password=([^\s\x00-\x1f&]+)", str(captured.get("text", "")))
            password = unquote(match.group(1)) if match else ""
            try:
                status = auth_status(self.runtime["endpoint"], password)
                if status.get("auth_enabled") is not True or status.get("authenticated") is not True:
                    raise WorkBuddyError("WorkBuddy worker 尚未启用本机认证")
                break
            except WorkBuddyError:
                self._cancel.wait(0.25)
        else:
            raise WorkBuddyError("WorkBuddy worker 未完成本机认证")
        return self.runtime["endpoint"], password

    def cancel(self) -> None:
        self._cancel.set()
        if self.client:
            try:
                self.client.cancel()
            except WorkBuddyError:
                pass
        if self.owned:
            self.owned.stop()
        elif self.runtime.get("runtime_id"):
            recover_runtime(dict(self.runtime))

    def close(self) -> dict:
        with self._close_lock:
            if self.client:
                self.client.close()
                self.client = None
            if self.owned:
                return self.owned.stop()
            elif self.runtime:
                return recover_runtime(dict(self.runtime))
            return {"cleaned": True}
