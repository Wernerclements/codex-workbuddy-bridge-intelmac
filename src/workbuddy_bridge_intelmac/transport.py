"""ACP HTTP/SSE client. Credentials never leave this in-memory object."""
from __future__ import annotations

import json
import threading
from typing import Callable

import httpx

from .discovery import HEADERS, WorkBuddyError, check_endpoint


def sse_messages(response):
    lines: list[str] = []
    size = 0
    for line in response.iter_lines():
        if not line:
            if lines:
                try:
                    value = json.loads("\n".join(lines))
                except ValueError:
                    raise WorkBuddyError("ACP 事件不是有效 JSON") from None
                if isinstance(value, dict):
                    yield value
                lines, size = [], 0
        elif line.startswith("data:"):
            piece = line[5:].lstrip(" ")
            size += len(piece)
            if size > 2_000_000:
                raise WorkBuddyError("ACP 单条事件过大")
            lines.append(piece)
        elif line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                raise WorkBuddyError("ACP 回复不是有效 JSON") from None
            if isinstance(value, dict):
                yield value
    if lines:
        try:
            value = json.loads("\n".join(lines))
        except ValueError:
            raise WorkBuddyError("ACP 事件被截断") from None
        if isinstance(value, dict):
            yield value


class AcpClient:
    def __init__(self, endpoint: str, password: str, on_event: Callable[[dict], None],
                 on_permission: Callable[[dict], str]):
        self.endpoint = endpoint
        self.base = check_endpoint(endpoint)
        self._password = password
        self._connection_id = ""
        self._session_token = ""
        self.session_id = ""
        self.on_event = on_event
        self.on_permission = on_permission
        self._http = httpx.Client(trust_env=False, follow_redirects=False,
                                  timeout=httpx.Timeout(30, connect=5))
        self._lock = threading.Lock()
        self._next = 0
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._listener: threading.Thread | None = None
        self._listener_error: str | None = None
        self._permission_lock = threading.Lock()
        self._permission_ids: set[str] = set()
        self.answer: list[str] = []
        self.stop_reason = ""

    def headers(self) -> dict:
        headers = {**HEADERS, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if self._password:
            headers["Authorization"] = "Bearer " + self._password
        if self._connection_id:
            headers["acp-connection-id"] = self._connection_id
        if self._session_token:
            headers["acp-session-token"] = self._session_token
        return headers

    def connect(self) -> None:
        try:
            response = self._http.post(self.base + "/api/v1/acp/connect", headers=self.headers(), timeout=10)
            response.raise_for_status()
            data = response.json()
            self._connection_id = data["connectionId"]
            self._session_token = data["sessionToken"]
        except (httpx.HTTPError, ValueError, KeyError):
            raise WorkBuddyError("无法建立 WorkBuddy ACP 连接") from None
        self.request("initialize", {"protocolVersion": 1,
            "clientInfo": {"name": "codex-workbuddy-bridge-intelmac", "version": "0.1.0"},
            "clientCapabilities": {}}, timeout=30)

    def new_session(self, cwd: str) -> str:
        result = self.request("session/new", {"cwd": cwd, "mcpServers": [],
            "_meta": {"codebuddy.ai": {"welcomeMode": "working", "isPlayground": True}}}, timeout=60)
        if not isinstance(result, dict) or not result.get("sessionId"):
            raise WorkBuddyError("WorkBuddy 未返回任务会话 ID")
        self.session_id = str(result["sessionId"])
        return self.session_id

    def start_listener(self) -> None:
        def listen():
            try:
                with self._http.stream("GET", self.endpoint, headers=self.headers(),
                                       timeout=httpx.Timeout(None, connect=5)) as response:
                    response.raise_for_status()
                    self._ready.set()
                    for event in sse_messages(response):
                        if self._stop.is_set():
                            break
                        # Prompt POST is the ordered, authoritative answer
                        # stream. GET carries broadcasts and can lag behind it.
                        if event.get("method") == "session/request_permission":
                            self._event(event)
                    if not self._stop.is_set():
                        self._listener_error = "UnexpectedEOF"
            except Exception as error:
                if not self._stop.is_set():
                    self._listener_error = type(error).__name__
                self._ready.set()
        self._listener = threading.Thread(target=listen, name="workbuddy-events", daemon=True)
        self._listener.start()
        if not self._ready.wait(10) or self._listener_error:
            raise WorkBuddyError("无法订阅 WorkBuddy 任务事件")

    def _event(self, event: dict) -> None:
        params = event.get("params") or {}
        if params.get("sessionId") != self.session_id:
            return
        if event.get("method") == "session/request_permission" and event.get("id") is not None:
            self._permission(event)
            return
        update = params.get("update") or {}
        kind = str(update.get("sessionUpdate", update.get("type", "")))
        if kind in {"agent_message_chunk", "assistant_message", "message_chunk"}:
            content = update.get("content") or {}
            text = content.get("text", "") if isinstance(content, dict) else ""
            if text:
                self.answer.append(text)
                self.on_event({"type": "answer", "text": text})
        elif kind == "tool_call" or kind == "tool_call_update":
            self.on_event({"type": "activity", "kind": kind,
                           "title": str(update.get("title", ""))[:300],
                           "status": str(update.get("status", ""))})

    def _permission(self, event: dict) -> None:
        key = str(event["id"])
        with self._permission_lock:
            if key in self._permission_ids:
                return
            self._permission_ids.add(key)
        params = event.get("params") or {}
        options = [{"optionId": str(item.get("optionId", "")), "kind": str(item.get("kind", "")),
                    "name": str(item.get("name", ""))} for item in params.get("options", []) if item.get("optionId")]
        tool = params.get("toolCall") or {}
        tool_name = str((tool.get("_meta") or {}).get("codebuddy.ai/toolName", ""))
        raw_input = tool.get("rawInput") or {}
        inputs = {key: value[:2000] for key, value in raw_input.items()
                  if key in {"file_path", "path", "command", "url", "description", "content"}
                  and isinstance(value, str)} if isinstance(raw_input, dict) else {}
        locations = tool.get("locations") or []
        if not locations and (inputs.get("file_path") or inputs.get("path")):
            locations = [{"path": inputs.get("file_path") or inputs["path"]}]
        kind = tool.get("kind") or {"Read": "read", "Write": "edit", "Edit": "edit",
            "Bash": "execute", "WebFetch": "fetch", "WebSearch": "search"}.get(tool_name, "unknown")
        selection = self.on_permission({"request_id": key, "session_id": self.session_id,
            "title": str(tool.get("title") or tool_name or "工具权限请求")[:500],
            "kind": str(kind), "tool_name": tool_name, "input": inputs, "options": options,
            "locations": locations})
        if selection == "cancelled" or self._stop.is_set():
            outcome = {"outcome": "cancelled"}
        else:
            if selection not in {item["optionId"] for item in options}:
                raise WorkBuddyError("权限选项无效")
            outcome = {"outcome": "selected", "optionId": selection}
        self._post_message({"jsonrpc": "2.0", "id": event["id"], "result": {"outcome": outcome}})

    def _post_message(self, message: dict) -> None:
        try:
            response = self._http.post(self.endpoint, headers=self.headers(), json=message, timeout=10)
            response.raise_for_status()
        except httpx.HTTPError:
            raise WorkBuddyError("WorkBuddy 控制消息发送失败") from None

    def request(self, method: str, params: dict, *, timeout: float = 30):
        with self._lock:
            self._next += 1
            request_id = self._next
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            with self._http.stream("POST", self.endpoint, headers=self.headers(), json=payload,
                                   timeout=httpx.Timeout(timeout, connect=5)) as response:
                response.raise_for_status()
                for event in sse_messages(response):
                    if event.get("id") == request_id and "method" not in event:
                        if event.get("error"):
                            code = (event["error"] or {}).get("code", "unknown")
                            code = code if type(code) is int else "unknown"
                            raise WorkBuddyError(f"ACP 拒绝请求 {method}（{code}）")
                        return event.get("result")
                    if event.get("method") == "session/request_permission":
                        self._event(event)
                    elif method == "session/prompt" or self._listener is None:
                        self._event(event)
        except httpx.HTTPError:
            raise WorkBuddyError(f"ACP 请求中断：{method}；请查询任务状态，不要自动重发",
                                 status="uncertain" if method == "session/prompt" else "failed") from None
        raise WorkBuddyError(f"ACP 未返回请求结果：{method}",
                             status="uncertain" if method == "session/prompt" else "failed")

    def prompt(self, text: str) -> dict:
        self.start_listener()
        result = self.request("session/prompt", {"sessionId": self.session_id,
            "prompt": [{"type": "text", "text": text}],
            "_meta": {"codebuddy.ai": {"isPlayground": True, "conversationId": self.session_id}}}, timeout=3600)
        if self._listener_error:
            raise WorkBuddyError("WorkBuddy 事件连接中断，结果不确定", status="uncertain")
        reason = str((result or {}).get("stopReason", ""))
        return {"session_id": self.session_id, "answer": "".join(self.answer).strip(), "stop_reason": reason}

    def cancel(self) -> None:
        if self.session_id:
            self._post_message({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": self.session_id}})

    def close(self) -> None:
        self._stop.set()
        if self._connection_id:
            try:
                self._http.delete(self.endpoint, headers=self.headers(), timeout=3)
            except (httpx.HTTPError, RuntimeError):
                pass
        self._http.close()
        self._password = self._session_token = ""
