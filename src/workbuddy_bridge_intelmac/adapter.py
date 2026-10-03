"""WorkBuddy 5.6.2 adapter. Use only this bridge's isolated desktop workers."""
from __future__ import annotations

import re
from pathlib import Path
import threading
import time
import uuid
from urllib.parse import unquote

from .discovery import (WORKER_PREFIX, WorkBuddyError, app_info, auth_status,
                        check_endpoint, discover, find_sockets, rpc)
from .transport import AcpClient


def health() -> dict:
    try:
        desktop = discover()
        return {"available": True, "read_only": True, "version": desktop.version,
                "protocol": desktop.protocol, "authenticated": True,
                "supported": desktop.version == "5.6.2"}
    except WorkBuddyError as error:
        return {"available": False, "read_only": True, "error": error.public_message}


def recover_runtime(runtime: dict) -> dict:
    """Stop a persisted owned worker, never resume or re-submit its prompt."""
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
    remaining = rpc(path, "session.list") or []
    return {"cleaned": not any(item.get("sessionId") == worker for item in remaining)}


class WorkBuddyAdapter:
    def __init__(self, task_id: str, cwd: str, on_event, on_permission):
        try:
            valid_id = str(uuid.UUID(task_id)) == task_id or uuid.UUID(task_id).hex == task_id
        except ValueError:
            valid_id = False
        if not valid_id:
            raise WorkBuddyError("任务 ID 必须是桥接器生成的唯一 ID")
        self.task_id = task_id
        self.cwd = str(Path(cwd).resolve())
        self.on_event, self.on_permission = on_event, on_permission
        self.runtime: dict = {}
        self.client: AcpClient | None = None
        self._cancel = threading.Event()
        self._close_lock = threading.Lock()

    def run(self, prompt: str) -> dict:
        desktop = discover(for_write=True)
        info = app_info()
        if not info.get("cli_entry_present", info["cli"].is_file()):
            raise WorkBuddyError("WorkBuddy 内部运行入口缺失，请重新安装应用")
        if self._cancel.is_set():
            raise WorkBuddyError("任务已取消")
        worker = WORKER_PREFIX + self.task_id
        self.runtime = {"task_id": self.task_id, "worker_id": worker,
                        "socket_path": str(desktop.socket_path), "sidecar_pid": desktop.sidecar_pid,
                        "version": desktop.version}
        self.on_event({"type": "runtime", "runtime": dict(self.runtime)})
        created = rpc(desktop.socket_path, "session.create", {
            "sessionId": worker, "command": str(info["executable"]),
            "args": [str(info["cli"]), "--serve", "--setting-sources", "user",
                     "--permission-mode", "default", "--permission-mode-before-plan", "default"],
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
                auth_status(self.runtime["endpoint"], password)
                break
            except WorkBuddyError:
                self._cancel.wait(0.25)
        else:
            raise WorkBuddyError("WorkBuddy worker 未完成本机认证")
        self.client = AcpClient(self.runtime["endpoint"], password, self.on_event, self.on_permission)
        self.client.connect()
        session_id = self.client.new_session(self.cwd)
        self.on_event({"type": "session", "session_id": session_id})
        if self._cancel.is_set():
            raise WorkBuddyError("任务已取消")
        # Strictly use the known safe mode; unknown modes can normalize to bypass.
        self.client.request("session/set_config_option", {
            "sessionId": session_id, "configId": "mode", "value": "default"})
        self.on_event({"type": "running"})
        scoped = (f"你正在执行由 Codex 委派的独立任务。工作目录：{self.cwd}\n"
                  "需要生成文件时放在这个目录中；涉及其他目录或敏感操作时保留权限请求。"
                  "完成后说明生成文件的路径。\n\n" + prompt)
        return self.client.prompt(scoped)

    def cancel(self) -> None:
        self._cancel.set()
        if self.client:
            try:
                self.client.cancel()
            except WorkBuddyError:
                pass
        if self.runtime.get("runtime_id"):
            recover_runtime(dict(self.runtime))

    def close(self) -> None:
        with self._close_lock:
            if self.client:
                self.client.close()
                self.client = None
            if self.runtime:
                recover_runtime(dict(self.runtime))
