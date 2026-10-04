"""ACP HTTP/SSE client. Credentials never leave this in-memory object."""
from __future__ import annotations

import json
import threading
from typing import Callable

import httpx

from .discovery import HEADERS, WorkBuddyError, check_endpoint
from . import __version__
from .tool_evidence import extract_tool_evidence


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
        self.session_capabilities: dict = {}
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
        self._tool_names: dict[str, str] = {}
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
            "clientInfo": {"name": "codex-workbuddy-bridge-intelmac", "version": __version__},
            "clientCapabilities": {}}, timeout=30)

    def new_session(self, cwd: str) -> str:
        result = self.request("session/new", {"cwd": cwd, "mcpServers": [],
            "_meta": {"codebuddy.ai": {"welcomeMode": "working", "isPlayground": True}}}, timeout=60)
        if not isinstance(result, dict) or not result.get("sessionId"):
            raise WorkBuddyError("WorkBuddy 未返回任务会话 ID")
        self.session_id = str(result["sessionId"])
        self.session_capabilities = {key: result[key] for key in ("models", "configOptions") if key in result}
        return self.session_id

    def select_model(self, model: str) -> dict:
        """Select an advertised model and verify its echoed session value before prompting."""
        models = (self.session_capabilities.get("models") or {}).get("availableModels", [])
        chosen = next((item for item in models if item.get("modelId") == model), None)
        if chosen is None:
            raise WorkBuddyError("指定模型未在 WorkBuddy 当前账号的可用模型中；任务未发送")
        result = self.request("session/set_config_option", {
            "sessionId": self.session_id, "configId": "model", "value": model})
        options = (result or {}).get("configOptions", []) if isinstance(result, dict) else []
        current = next((item.get("currentValue") for item in options if item.get("id") == "model"), None)
        if current != model:
            raise WorkBuddyError("WorkBuddy 未确认所选模型；任务未发送，未回退到默认模型")
        self.session_capabilities["configOptions"] = options
        metadata = chosen.get("_meta") or {}
        limit = metadata.get("maxInputTokens")
        return {"id": model, "name": str(chosen.get("name", model)),
                "max_input_tokens": limit if type(limit) is int and limit > 0 else None,
                "selection_confirmed": True}

    def select_context(self, tokens: int | None, model_info: dict) -> dict:
        """Change an advertised session budget; never manufacture a larger model limit."""
        option = next((item for item in self.session_capabilities.get("configOptions", [])
                       if item.get("id") == "context_window"), None)
        choices = [int(item["value"]) for item in (option or {}).get("options", [])
                   if str(item.get("value", "")).isdigit()]
        limit = model_info.get("max_input_tokens")
        current = (option or {}).get("currentValue")
        budget = int(current) if str(current).isdigit() else limit
        if tokens is not None:
            if limit is None or tokens > limit:
                raise WorkBuddyError("请求的上下文长度超过模型宣告上限或上限未知；任务未发送")
            if choices:
                if tokens not in choices:
                    raise WorkBuddyError("请求的上下文长度不是该模型支持的会话选项；任务未发送")
                result = self.request("session/set_config_option", {
                    "sessionId": self.session_id, "configId": "context_window", "value": str(tokens)})
                updated = next((item.get("currentValue") for item in (result or {}).get("configOptions", [])
                                if item.get("id") == "context_window"), None)
                if str(updated) != str(tokens):
                    raise WorkBuddyError("WorkBuddy 未确认上下文设置；任务未发送")
                budget = tokens
            elif tokens != limit:
                raise WorkBuddyError("该模型未提供上下文长度切换选项；任务未发送")
            else:
                budget = limit
        return {"context_tokens": budget, "context_choices": choices,
                "context_source": "session_option" if option else "advertised_model_limit",
                "context_requested": tokens}

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
            if kind == "tool_call_update" and not update.get("status"):
                return
            tool_name = str((update.get("_meta") or {}).get("codebuddy.ai/toolName", ""))
            tool_id = str(update.get("toolCallId", ""))[:200]
            if tool_id and tool_name:
                if len(self._tool_names) >= 500:
                    self._tool_names.clear()
                self._tool_names[tool_id] = tool_name
            elif tool_id:
                tool_name = self._tool_names.get(tool_id, "")
            raw = update.get("rawInput") or {}
            skill = raw.get("skill", "") if isinstance(raw, dict) and tool_name == "Skill" else ""
            activity = {"type": "activity", "kind": kind,
                           "title": str(update.get("title", ""))[:300],
                           "tool_name": tool_name[:200], "skill_name": str(skill)[:200],
                           "status": str(update.get("status", ""))[:100]}
            if activity["status"] in {"completed", "failed"}:
                output = update.get("rawOutput")
                if output is None:
                    output = {"content": "\n".join(str((item.get("content") or {}).get("text", ""))
                              for item in update.get("content", []) if isinstance(item, dict))}
                evidence = extract_tool_evidence(tool_name, output)
                if evidence:
                    activity["evidence"] = evidence
            self.on_event(activity)
        elif kind == "usage_update":
            used, size = update.get("used"), update.get("size")
            if type(used) is int and used >= 0 and type(size) is int and size > 0:
                self.on_event({"type": "usage", "used": used, "context_size": size})
        elif kind == "available_commands_update":
            skills = [{"name": str(item.get("name", ""))[:200],
                       "source": str((item.get("_meta") or {}).get("source", ""))[:100],
                       "is_built_in": bool((item.get("_meta") or {}).get("isBuiltIn", False))}
                      for item in update.get("availableCommands", [])
                      if (item.get("_meta") or {}).get("type") == "skill"]
            self.on_event({"type": "available_skills", "skills": skills[:500]})

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
                if method == "session/prompt":
                    self.on_event({"type": "prompt_accepted"})
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
