"""ACP regressions with deterministic synthetic HTTP streams."""
from contextlib import contextmanager
import json
import threading
import unittest
from unittest.mock import Mock

import httpx

from workbuddy_bridge_intelmac.discovery import WorkBuddyError
from workbuddy_bridge_intelmac.transport import AcpClient, sse_messages


def chunk(text, session="own-session"):
    return {"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": session, "update": {"sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": text}}}}


def permission(request_id=12):
    return {"jsonrpc": "2.0", "id": request_id, "method": "session/request_permission",
            "params": {"sessionId": "own-session", "toolCall": {"title": "Write test file", "kind": "edit"},
                       "options": [{"optionId": "once", "kind": "allow_once", "name": "Allow once"},
                                   {"optionId": "deny", "kind": "reject_once", "name": "Reject"}]}}


class Response:
    def __init__(self, events=(), release=None, consumed=None):
        self.events, self.release, self.consumed = events, release, consumed

    def raise_for_status(self):
        pass

    def iter_lines(self):
        for event in self.events:
            yield "data: " + json.dumps(event)
            yield ""
        if self.consumed:
            self.consumed.set()
        if self.release:
            self.release.wait(2)


class Http:
    def __init__(self, post_events=(), get_events=(), *, hold_get=False):
        self.post_events, self.get_events = post_events, get_events
        self.release = threading.Event() if hold_get else None
        self.consumed = threading.Event()

    @contextmanager
    def stream(self, method, *args, **kwargs):
        if method == "GET":
            yield Response(self.get_events, self.release, self.consumed)
        else:
            yield Response(self.post_events)

    def close(self):
        if self.release:
            self.release.set()


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.choose = Mock(return_value="deny")
        self.client = AcpClient("http://127.0.0.1:3456/api/v1/acp", "SYNTHETIC-TOKEN",
                                self.events.append, self.choose)
        self.client._http.close()
        self.client.session_id = "own-session"
        self.client._http = Http()

    def tearDown(self):
        self.client.close()
        if isinstance(self.client._listener, threading.Thread):
            self.client._listener.join(2)
            self.assertFalse(self.client._listener.is_alive())

    def test_post_is_complete_answer_authority_with_existing_listener(self):
        self.client._listener = object()
        self.client._http = Http([chunk("第一段"), chunk("other", "other-session"), chunk("第二段"),
                                 {"jsonrpc": "2.0", "id": 1, "result": {"stopReason": "end_turn"}}])
        result = self.client.request("session/prompt", {"sessionId": "own-session"})
        self.assertEqual(result["stopReason"], "end_turn")
        self.assertEqual("".join(self.client.answer), "第一段第二段")
        self.assertEqual([event["text"] for event in self.events if event["type"] == "answer"], ["第一段", "第二段"])
        self.assertEqual(self.events[0], {"type": "prompt_accepted"})

    def test_get_broadcast_copy_does_not_duplicate_post_answer(self):
        fake = Http([chunk("answer"), {"id": 1, "result": {"stopReason": "end_turn"}}],
                    [chunk("answer")], hold_get=True)
        self.client._http = fake
        self.client.start_listener()
        self.assertTrue(fake.consumed.wait(1))
        self.client.request("session/prompt", {"sessionId": "own-session"})
        self.assertEqual("".join(self.client.answer), "answer")
        self.assertEqual(len([event for event in self.events if event["type"] == "answer"]), 1)

    def test_get_normal_eof_is_an_uncertain_subscription(self):
        self.client._http = Http()
        # EOF may happen immediately before or after start_listener returns.
        try:
            self.client.start_listener()
        except WorkBuddyError:
            pass
        self.client._listener.join(1)
        self.assertEqual(self.client._listener_error, "UnexpectedEOF")

    def test_permissions_wait_for_choice_and_do_not_auto_allow(self):
        entered, release = threading.Event(), threading.Event()

        def human(request):
            entered.set()
            release.wait(1)
            return "deny"

        self.client.on_permission = human
        send = Mock()
        self.client._post_message = send
        failures = []

        def receive():
            try:
                self.client._event(permission())
            except Exception as error:
                failures.append(error)

        thread = threading.Thread(target=receive)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            send.assert_not_called()
        finally:
            release.set()
            thread.join(1)
        self.assertEqual(failures, [])
        self.assertEqual(send.call_args.args[0]["result"]["outcome"],
                         {"outcome": "selected", "optionId": "deny"})

    def test_invalid_permission_choice_is_not_sent(self):
        self.choose.return_value = "invented-choice"
        self.client._post_message = Mock()
        with self.assertRaises(WorkBuddyError):
            self.client._event(permission())
        self.client._post_message.assert_not_called()

    def test_sparse_write_permission_is_readable_and_still_waits_for_allow_once(self):
        event = permission(13)
        event["params"]["toolCall"] = {
            "rawInput": {"file_path": "/tmp/intelmac-synthetic/output.md", "content": "Test output",
                         "password": "SYNTHETIC-PRIVATE-VALUE"},
            "_meta": {"codebuddy.ai/toolName": "Write"}}
        entered, release = threading.Event(), threading.Event()
        requests, failures = [], []

        def human(request):
            requests.append(request)
            entered.set()
            release.wait(1)
            return "once"

        self.client.on_permission = human
        self.client._post_message = Mock()

        def receive():
            try:
                self.client._event(event)
            except Exception as error:
                failures.append(error)

        thread = threading.Thread(target=receive)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.client._post_message.assert_not_called()
            request = requests[0]
            self.assertEqual(request["title"], "Write")
            self.assertEqual(request["tool_name"], "Write")
            self.assertEqual(request["kind"], "edit")
            self.assertEqual(request["locations"], [{"path": "/tmp/intelmac-synthetic/output.md"}])
            self.assertEqual(request["input"], {"file_path": "/tmp/intelmac-synthetic/output.md",
                                                "content": "Test output"})
            self.assertNotIn("SYNTHETIC-PRIVATE-VALUE", repr(request))
            self.assertEqual(request["options"][0]["kind"], "allow_once")
        finally:
            release.set()
            thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.client._post_message.assert_called_once()
        self.assertEqual(self.client._post_message.call_args.args[0]["result"],
                         {"outcome": {"outcome": "selected", "optionId": "once"}})

    def test_duplicate_permission_id_is_handled_once(self):
        self.client._post_message = Mock()
        self.client._event(permission())
        self.client._event(permission())
        self.choose.assert_called_once()
        self.client._post_message.assert_called_once()

    def test_other_session_permission_is_ignored(self):
        event = permission()
        event["params"]["sessionId"] = "unrelated-session"
        self.client._post_message = Mock()
        self.client._event(event)
        self.choose.assert_not_called()
        self.client._post_message.assert_not_called()

    def test_cancelled_permission_sends_cancelled_outcome(self):
        self.choose.return_value = "cancelled"
        self.client._post_message = Mock()
        self.client._event(permission())
        self.assertEqual(self.client._post_message.call_args.args[0]["result"],
                         {"outcome": {"outcome": "cancelled"}})

    def test_prompt_transport_failure_is_uncertain_and_credential_free(self):
        class Interrupted(Http):
            @contextmanager
            def stream(self, *args, **kwargs):
                raise httpx.ReadError("SYNTHETIC-TOKEN must stay private")
                yield

        self.client._http = Interrupted()
        with self.assertRaises(WorkBuddyError) as caught:
            self.client.request("session/prompt", {})
        self.assertEqual(caught.exception.status, "uncertain")
        self.assertNotIn("SYNTHETIC-TOKEN", str(caught.exception))

    def test_native_error_message_and_non_numeric_code_are_redacted(self):
        self.client._http = Http([{"jsonrpc": "2.0", "id": 1, "error": {
            "code": "SYNTHETIC-PRIVATE-VALUE", "message": "SYNTHETIC-TOKEN"}}])
        with self.assertRaises(WorkBuddyError) as caught:
            self.client.request("session/prompt", {})
        self.assertNotIn("SYNTHETIC-PRIVATE-VALUE", str(caught.exception))
        self.assertNotIn("SYNTHETIC-TOKEN", str(caught.exception))

    def test_usage_preserves_integer_budget_without_cost_or_private_metadata(self):
        def usage(used, size):
            return {"method": "session/update", "params": {
                "sessionId": "own-session", "update": {"sessionUpdate": "usage_update",
                "used": used, "size": size, "cost": {"amount": 999, "currency": "PRIVATE"},
                "_meta": {"token": "SYNTHETIC-PRIVATE-CREDENTIAL", "usage": {"secret": "PRIVATE"}}}}}
        self.client._event(usage(123, 1000000))
        self.client._event(usage(0, 300000))
        self.assertEqual(self.events, [{"type": "usage", "used": 123, "context_size": 1000000},
                                       {"type": "usage", "used": 0, "context_size": 300000}])
        for used, size in ((True, 1000000), (123, True), (-1, 1000000),
                           (123, 0), (123.0, 1000000), (123, "1000000")):
            self.client._event(usage(used, size))
        self.assertEqual(len(self.events), 2)
        self.assertNotIn("PRIVATE", repr(self.events))
        self.assertNotIn("cost", repr(self.events))

    def test_tool_input_fragments_are_ignored_and_skill_activity_is_sanitized(self):
        def tool(update):
            return {"method": "session/update", "params": {
                "sessionId": "own-session", "update": update}}
        for fragment in ("part one", "part two", "part three"):
            self.client._event(tool({"sessionUpdate": "tool_call_update", "toolCallId": "synthetic-tool",
                                     "rawInput": {"arguments": fragment}}))
        self.assertEqual(self.events, [])
        self.client._event(tool({"sessionUpdate": "tool_call", "toolCallId": "synthetic-tool",
                                "title": "Load requested skill", "status": "pending",
                                "_meta": {"codebuddy.ai/toolName": "Skill", "token": "PRIVATE"},
                                "rawInput": {"skill": "bridge-test-skill", "credential": "PRIVATE",
                                             "arguments": "SYNTHETIC-PRIVATE-INPUT"}}))
        self.assertEqual(self.events, [{"type": "activity", "kind": "tool_call",
                                       "title": "Load requested skill", "tool_name": "Skill",
                                       "skill_name": "bridge-test-skill", "status": "pending"}])
        self.assertNotIn("PRIVATE", repr(self.events))
        self.assertNotIn("rawInput", repr(self.events))

    def test_completion_without_metadata_uses_call_identity_and_only_safe_evidence(self):
        def event(update):
            return {"method": "session/update", "params": {
                "sessionId": "own-session", "update": update}}
        self.client._event(event({"sessionUpdate": "tool_call", "toolCallId": "search-1",
            "status": "pending", "_meta": {"codebuddy.ai/toolName": "ToolSearch"}}))
        self.client._event(event({"sessionUpdate": "tool_call_update", "toolCallId": "search-1",
            "status": "completed", "rawOutput": {"type": "text", "text":
                "Found 1 tool(s). Use DeferExecuteTool to invoke them.\n\n"
                "## mcp__example__lookup\nPRIVATE credential and tool parameters"}}))
        self.assertEqual(self.events[-1]["tool_name"], "ToolSearch")
        self.assertEqual(self.events[-1]["evidence"], {"outcome": "found", "count": 1,
                         "tool_names": ["mcp__example__lookup"]})
        self.assertNotIn("PRIVATE", repr(self.events))

    def model_capabilities(self, current="deepseek-v4.1-flash", *, context=True):
        options = [{"id": "model", "currentValue": current,
                    "options": [{"value": "deepseek-v4.1-flash"}, {"value": "glm-5.3-flash"}]}]
        if context:
            options.append({"id": "context_window", "currentValue": "300000",
                            "options": [{"value": str(value)} for value in (300000, 600000, 1000000)]})
        return {"models": {"currentModelId": current, "availableModels": [
            {"modelId": model, "name": model, "_meta": {"maxInputTokens": 1000000}}
            for model in ("deepseek-v4.1-flash", "glm-5.3-flash")]}, "configOptions": options}

    def test_model_missing_from_advertised_catalog_is_rejected_before_request(self):
        self.client.session_capabilities = self.model_capabilities()
        self.client.request = Mock()
        with self.assertRaises(WorkBuddyError):
            self.client.select_model("invented-model")
        self.client.request.assert_not_called()

    def test_model_echo_mismatch_does_not_fallback_or_mark_selection_confirmed(self):
        self.client.session_capabilities = self.model_capabilities()
        self.client.request = Mock(return_value={"configOptions": [
            {"id": "model", "currentValue": "hy4-preview"}]})
        with self.assertRaises(WorkBuddyError):
            self.client.select_model("glm-5.3-flash")
        self.client.request.assert_called_once_with("session/set_config_option", {
            "sessionId": "own-session", "configId": "model", "value": "glm-5.3-flash"})

    def test_selected_model_refreshes_context_options_from_confirmed_response(self):
        self.client.session_capabilities = self.model_capabilities(context=False)
        response = self.model_capabilities(current="glm-5.3-flash")["configOptions"]
        self.client.request = Mock(return_value={"configOptions": response})
        result = self.client.select_model("glm-5.3-flash")
        self.assertEqual(result, {"id": "glm-5.3-flash", "name": "glm-5.3-flash",
                                  "max_input_tokens": 1000000, "selection_confirmed": True})
        self.assertEqual(self.client.session_capabilities["configOptions"], response)
        self.assertEqual(self.client.select_context(None, result)["context_tokens"], 300000)

    def test_context_300k_to_1m_requires_string_option_echo(self):
        self.client.session_capabilities = self.model_capabilities()
        result_options = self.model_capabilities()["configOptions"]
        result_options[1]["currentValue"] = "1000000"
        self.client.request = Mock(return_value={"configOptions": result_options})
        result = self.client.select_context(1000000, {"max_input_tokens": 1000000})
        self.assertEqual(result["context_tokens"], 1000000)
        self.assertEqual(result["context_choices"], [300000, 600000, 1000000])
        self.assertEqual(result["context_source"], "session_option")
        self.client.request.assert_called_once_with("session/set_config_option", {
            "sessionId": "own-session", "configId": "context_window", "value": "1000000"})

    def test_context_above_limit_or_not_supported_is_rejected_without_request(self):
        self.client.session_capabilities = self.model_capabilities()
        self.client.request = Mock()
        for requested in (1000001, 500000):
            with self.subTest(requested=requested), self.assertRaises(WorkBuddyError):
                self.client.select_context(requested, {"max_input_tokens": 1000000})
        self.client.request.assert_not_called()

    def test_context_echo_mismatch_is_rejected(self):
        self.client.session_capabilities = self.model_capabilities()
        self.client.request = Mock(return_value={"configOptions":
                                                 self.model_capabilities()["configOptions"]})
        with self.assertRaises(WorkBuddyError):
            self.client.select_context(1000000, {"max_input_tokens": 1000000})
        self.client.request.assert_called_once()

    def test_context_without_switch_option_accepts_only_advertised_limit(self):
        self.client.session_capabilities = self.model_capabilities(context=False)
        self.client.request = Mock()
        for requested in (None, 1000000):
            with self.subTest(requested=requested):
                result = self.client.select_context(requested, {"max_input_tokens": 1000000})
                self.assertEqual(result["context_tokens"], 1000000)
                self.assertEqual(result["context_source"], "advertised_model_limit")
        for requested, limit in ((300000, 1000000), (1000000, None)):
            with self.subTest(requested=requested, limit=limit), self.assertRaises(WorkBuddyError):
                self.client.select_context(requested, {"max_input_tokens": limit})
        self.client.request.assert_not_called()

    def test_sse_ignores_heartbeats_and_rejects_invalid_json(self):
        response = Mock()
        response.iter_lines.return_value = iter([": heartbeat", "", 'data: {"id":1,"result":{}}', ""])
        self.assertEqual(list(sse_messages(response)), [{"id": 1, "result": {}}])
        response.iter_lines.return_value = iter(["data: broken", ""])
        with self.assertRaises(WorkBuddyError):
            list(sse_messages(response))


if __name__ == "__main__":
    unittest.main()
