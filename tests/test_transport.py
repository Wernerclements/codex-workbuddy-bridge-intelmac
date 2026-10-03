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
        self.assertEqual([event["text"] for event in self.events], ["第一段", "第二段"])

    def test_get_broadcast_copy_does_not_duplicate_post_answer(self):
        fake = Http([chunk("answer"), {"id": 1, "result": {"stopReason": "end_turn"}}],
                    [chunk("answer")], hold_get=True)
        self.client._http = fake
        self.client.start_listener()
        self.assertTrue(fake.consumed.wait(1))
        self.client.request("session/prompt", {"sessionId": "own-session"})
        self.assertEqual("".join(self.client.answer), "answer")
        self.assertEqual(len(self.events), 1)

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

    def test_sse_ignores_heartbeats_and_rejects_invalid_json(self):
        response = Mock()
        response.iter_lines.return_value = iter([": heartbeat", "", 'data: {"id":1,"result":{}}', ""])
        self.assertEqual(list(sse_messages(response)), [{"id": 1, "result": {}}])
        response.iter_lines.return_value = iter(["data: broken", ""])
        with self.assertRaises(WorkBuddyError):
            list(sse_messages(response))


if __name__ == "__main__":
    unittest.main()
