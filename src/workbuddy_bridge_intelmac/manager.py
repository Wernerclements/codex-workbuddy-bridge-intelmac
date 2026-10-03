"""串行任务管理。只持久化桥接器自己的记录，不重放中断的请求。"""
from __future__ import annotations

import copy
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import uuid
from typing import Callable

TERMINAL = frozenset({"succeeded", "failed", "cancelled", "uncertain"})
MAX_SCAN_ENTRIES = 3000
MAX_ARTIFACTS = 100
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ANSWER_CHARS = 8 * 1024 * 1024


def _safe_runtime(value):
    """适配器不得持久化凭证；额外过滤常见敏感键。"""
    if isinstance(value, dict):
        return {str(k): _safe_runtime(v) for k, v in value.items()
                if not any(word in str(k).lower() for word in
                           ("password", "token", "credential", "authorization", "secret", "cookie"))}
    if isinstance(value, list):
        return [_safe_runtime(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("运行信息必须可序列化")


def _atomic_write(path: Path, text: str):
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _scan(cwd: Path):
    files = {}
    visited = 0
    limited = False
    def scan_error(error):
        nonlocal limited
        limited = True
    for root, directories, names in os.walk(cwd, followlinks=False, onerror=scan_error):
        visited += len(directories)
        if visited > MAX_SCAN_ENTRIES:
            return files, True
        directories[:] = sorted(name for name in directories if name != ".git"
                                and not (Path(root) / name).is_symlink())
        for name in sorted(names):
            visited += 1
            if visited > MAX_SCAN_ENTRIES:
                return files, True
            path = Path(root) / name
            try:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                    if info.st_size > MAX_FILE_BYTES:
                        limited = True
                    continue
                path.resolve().relative_to(cwd)
                files[str(path.relative_to(cwd))] = (info.st_mtime_ns, info.st_size)
            except (OSError, ValueError):
                limited = True
                continue
    return files, limited


class Manager:
    def __init__(self, state_dir=None, adapter_factory: Callable | None = None,
                 recover_runtime: Callable | None = None):
        if adapter_factory is None:
            from .adapter import WorkBuddyAdapter
            adapter_factory = WorkBuddyAdapter
        if recover_runtime is None:
            from .adapter import recover_runtime as recover
            recover_runtime = recover
        self.adapter_factory = adapter_factory
        self._recover = recover_runtime
        self.state_dir = Path(state_dir or os.environ.get("WORKBUDDY_BRIDGE_STATE_DIR") or
                              Path.home() / "Library/Application Support/codex-workbuddy-bridge-intelmac/tasks").expanduser()
        if self.state_dir.is_symlink():
            raise ValueError("任务记录目录不能是符号链接")
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir = self.state_dir.resolve()
        os.chmod(self.state_dir, 0o700)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        self._lock_fd = os.open(self.state_dir / ".manager.lock", flags, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._lock_fd)
            self._lock_fd = None
            raise RuntimeError("已有桥接服务使用此任务目录") from None
        self._condition = threading.Condition(threading.RLock())
        self._tasks = {}
        self._queue = []
        self._adapters = {}
        self._shutdown = False
        for directory in self.state_dir.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                if str(uuid.UUID(directory.name)) != directory.name:
                    continue
                record = directory / "task.json"
                if record.is_symlink():
                    continue
                task = json.loads(record.read_text(encoding="utf-8"))
                if task.get("task_id") != directory.name:
                    continue
                if task["status"] not in TERMINAL:
                    task["status"] = "uncertain"
                    task["error"] = "桥接服务曾中断；任务未重新提交。"
                    task["permission_request"] = None
                    if task.get("runtime"):
                        try:
                            task["recovery"] = _safe_runtime(self._recover(task["runtime"]))
                        except Exception as error:
                            task["recovery"] = {"error_type": type(error).__name__}
                    task["updated_at"] = time.time()
                self._tasks[directory.name] = task
                self._save(task)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        self._worker = threading.Thread(target=self._work, name="workbuddy-task-worker", daemon=True)
        self._worker.start()

    def _save(self, task):
        directory = self.state_dir / task["task_id"]
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise ValueError("任务目录不能是符号链接")
        os.chmod(directory, 0o700)
        _atomic_write(directory / "task.json", json.dumps({k: v for k, v in task.items() if k != "_chunks"}, ensure_ascii=False, indent=2))

    def _task(self, task_id):
        if task_id not in self._tasks:
            raise ValueError("未知任务 ID")
        return self._tasks[task_id]

    def _view(self, task):
        result = copy.deepcopy({k: v for k, v in task.items()
                              if k not in {"prompt", "baseline", "permission_choice", "_chunks"}})
        result["answer_chars"] = sum(map(len, task.get("_chunks", [])))
        return result

    def _change(self, task, **fields):
        task.update(fields)
        task["updated_at"] = time.time()
        self._save(task)
        self._condition.notify_all()

    def start_task(self, prompt: str, cwd: str):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 100000:
            raise ValueError("任务内容不能为空，且不超过 100000 个字符")
        directory = Path(cwd).expanduser().resolve()
        if not directory.is_dir():
            raise ValueError("工作目录不存在")
        baseline, limited = {}, False
        with self._condition:
            if self._shutdown:
                raise RuntimeError("桥接服务正在退出")
            now = time.time()
            task = {"task_id": str(uuid.uuid4()), "prompt": prompt, "cwd": str(directory),
                    "status": "queued", "created_at": now, "updated_at": now,
                    "cancel_requested": False, "permission_request": None,
                    "runtime": None, "baseline": baseline, "scan_limited": limited}
            self._tasks[task["task_id"]] = task
            self._save(task)
            self._queue.append(task["task_id"])
            self._condition.notify_all()
            return self._view(task)

    def get_task(self, task_id: str):
        with self._condition:
            return self._view(self._task(task_id))

    def wait_task(self, task_id: str, timeout_seconds: float = 30):
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) \
                or not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= 60:
            raise ValueError("等待时间必须在 0 到 60 秒之间")
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            task = self._task(task_id)
            while task["status"] not in TERMINAL and (task["status"] != "permission_required" or task["cancel_requested"]):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            result = self._view(task)
            result["wait_timed_out"] = task["status"] not in TERMINAL \
                and (task["status"] != "permission_required" or task["cancel_requested"]) and time.monotonic() >= deadline
            return result

    def cancel_task(self, task_id: str):
        with self._condition:
            task = self._task(task_id)
            if task["status"] in TERMINAL:
                return self._view(task)
            self._change(task, cancel_requested=True)
            adapter = self._adapters.get(task_id)
            if task["status"] == "queued":
                self._change(task, status="cancelled", permission_request=None)
        if adapter is not None:
            try:
                adapter.cancel()
            except Exception as error:
                with self._condition:
                    self._change(task, cancel_error_type=type(error).__name__)
        return self.get_task(task_id)

    def respond_permission(self, task_id: str, request_id: str, option_id: str):
        with self._condition:
            task = self._task(task_id)
            request = task.get("permission_request")
            if task["status"] != "permission_required" or not request or task["cancel_requested"]:
                raise ValueError("任务没有待处理的权限请求")
            if request.get("request_id") != request_id:
                raise ValueError("权限请求已过期或不匹配")
            allowed = {option.get("optionId") for option in request.get("options", [])}
            if option_id not in allowed:
                raise ValueError("无效的权限选项")
            self._change(task, permission_choice=option_id, status="running", permission_request=None)
            return self._view(task)

    def get_result(self, task_id: str):
        with self._condition:
            task = self._task(task_id)
            if task["status"] not in TERMINAL:
                raise ValueError("任务尚未结束")
            return copy.deepcopy({"task_id": task_id, "status": task["status"],
                                  "summary": task.get("summary", ""),
                                  "answer_path": task.get("answer_path"),
                                  "artifacts": task.get("artifacts", []),
                                  "scan_limited": task.get("scan_limited", False),
                                  "error": task.get("error"), "stop_reason": task.get("stop_reason"),
                                  "partial": task.get("partial", False)})

    def _event(self, task, event):
        with self._condition:
            if task["status"] in TERMINAL:
                return
            if event.get("type") == "runtime":
                self._change(task, runtime=_safe_runtime(event.get("runtime", {})))
            elif event.get("type") == "session":
                self._change(task, session_id=event.get("session_id"))
            elif event.get("type") in {"activity", "running"} and task["status"] == "connecting":
                self._change(task, status="running")
            elif event.get("type") == "answer":
                chunks = task.setdefault("_chunks", [])
                text = event.get("text", "")
                if not isinstance(text, str) or sum(map(len, chunks)) + len(text) > MAX_ANSWER_CHARS:
                    raise ValueError("返回正文超过桥接器大小限制")
                chunks.append(text)
                # Chunk buffers stay in memory; only completed answers are persisted.

    def _permission(self, task, request):
        with self._condition:
            if task["cancel_requested"] or self._shutdown:
                raise RuntimeError("任务已取消")
            request = copy.deepcopy(request)
            request["request_id"] = str(uuid.uuid4())
            self._change(task, status="permission_required", permission_request=request,
                         permission_choice=None)
            while task.get("permission_choice") is None and not task["cancel_requested"] \
                    and not self._shutdown:
                self._condition.wait()
            if task["cancel_requested"] or self._shutdown:
                raise RuntimeError("任务已取消")
            choice = task.pop("permission_choice")
            self._save(task)
            return choice

    def _artifact_changes(self, task):
        current, limited = _scan(Path(task["cwd"]))
        artifacts = [{"path": str(Path(task["cwd"]) / name), "size": info[1]}
                     for name, info in current.items()
                     if tuple(task["baseline"].get(name, ())) != tuple(info)
                     and not (Path(task["cwd"]) / name).is_relative_to(self.state_dir)]
        return artifacts[:MAX_ARTIFACTS], (task["scan_limited"] or limited or len(artifacts) > MAX_ARTIFACTS)

    def _work(self):
        while True:
            with self._condition:
                while not self._queue and not self._shutdown:
                    self._condition.wait()
                if self._shutdown:
                    self._release_lock()
                    return
                task = self._tasks[self._queue.pop(0)]
                if task["status"] == "cancelled":
                    continue
                self._change(task, status="connecting")
            adapter = None
            try:
                baseline, limited = _scan(Path(task["cwd"]))
                with self._condition:
                    if self._shutdown:
                        raise RuntimeError("桥接服务正在退出")
                    self._change(task, baseline=baseline, scan_limited=limited)
                adapter = self.adapter_factory(task_id=task["task_id"], cwd=task["cwd"],
                                               on_event=lambda event: self._event(task, event),
                                               on_permission=lambda request: self._permission(task, request))
                with self._condition:
                    self._adapters[task["task_id"]] = adapter
                    cancelled = task["cancel_requested"] or self._shutdown
                if cancelled:
                    adapter.cancel()
                    raise RuntimeError("任务已取消")
                result = adapter.run(task["prompt"])
                with self._condition:
                    if task["status"] == "uncertain":
                        raise RuntimeError("服务退出时任务状态已标记不确定")
                answer = result.get("answer", "".join(task.get("_chunks", [])))
                if not isinstance(answer, str) or len(answer) > MAX_ANSWER_CHARS:
                    raise ValueError("返回正文超过桥接器大小限制")
                artifacts, limited = self._artifact_changes(task)
                with self._condition:
                    task.pop("_chunks", None)
                    reason = result.get("stop_reason")
                    if task["status"] == "uncertain":
                        continue
                    if task["cancel_requested"] or reason == "cancelled":
                        status_value = "cancelled"
                    elif reason == "refusal":
                        status_value = "failed"
                    elif reason != "end_turn":
                        status_value = "uncertain"
                    elif not answer.strip() and not artifacts:
                        status_value = "failed"
                    else:
                        status_value = "succeeded"
                    _atomic_write(self.state_dir / task["task_id"] / "answer.txt", answer)
                    self._change(task, status=status_value,
                                 session_id=result.get("session_id") or task.get("session_id"), stop_reason=result.get("stop_reason"),
                                 summary=answer[:2000], answer_path=str(self.state_dir / task["task_id"] / "answer.txt"),
                                 artifacts=artifacts, permission_request=None,
                                 partial=status_value != "succeeded" and bool(answer or artifacts),
                                 error=("WorkBuddy 拒绝执行此任务，未完成请求。" if reason == "refusal"
                                        else "WorkBuddy 达到生成或请求次数限制，结果可能不完整。" if reason in {"max_tokens", "max_turn_requests"}
                                        else "WorkBuddy 返回空正文，未检测到文件变化。" if status_value == "failed"
                                        else "WorkBuddy 未返回可识别的结束状态。" if status_value == "uncertain" else None),
                                 scan_limited=limited)
            except Exception as error:
                with self._condition:
                    if task["status"] == "uncertain":
                        continue
                    answer = "".join(task.pop("_chunks", []))[:MAX_ANSWER_CHARS]
                artifacts, limited = self._artifact_changes(task)
                with self._condition:
                    # Shutdown fencing: never overwrite a previously fenced uncertain task.
                    if task["status"] == "uncertain":
                        continue
                    answer_path = None
                    if answer:
                        path = self.state_dir / task["task_id"] / "answer.txt"
                        _atomic_write(path, answer)
                        answer_path = str(path)
                    partial = bool(answer or artifacts)
                    status_value = ("cancelled" if task["cancel_requested"] else
                                    "uncertain" if partial or getattr(error, "status", None) == "uncertain" else "failed")
                    # Exception strings may contain authentication data; expose only safe adapter messages.
                    self._change(task, status=status_value,
                                 error=getattr(error, "public_message", None) or f"执行未完成（{type(error).__name__}）",
                                 permission_request=None, partial=partial, summary=answer[:2000],
                                 answer_path=answer_path, artifacts=artifacts, scan_limited=limited)
            finally:
                if adapter is not None:
                    try:
                        adapter.close()
                    except Exception as error:
                        with self._condition:
                            if task["status"] != "uncertain":
                                self._change(task, cleanup_error_type=type(error).__name__)
                with self._condition:
                    self._adapters.pop(task["task_id"], None)

    def close(self):
        with self._condition:
            if self._shutdown:
                return
            self._shutdown = True
            active = []
            for task in self._tasks.values():
                if task["status"] not in TERMINAL:
                    self._change(task, cancel_requested=True)
                    if task["status"] == "queued":
                        self._change(task, status="cancelled")
                    elif task["task_id"] in self._adapters:
                        active.append(self._adapters[task["task_id"]])
            self._condition.notify_all()
        for adapter in active:
            try:
                adapter.cancel()
            except Exception:
                pass
        self._worker.join(timeout=5)
        if self._worker.is_alive():
            with self._condition:
                for task in self._tasks.values():
                    if task["status"] not in TERMINAL:
                        self._change(task, status="uncertain", error="服务退出时未能确认任务结束。")

        if not self._worker.is_alive():
            with self._condition:
                self._release_lock()

    def _release_lock(self):
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None
