"""Protocol and boundary tests using only synthetic local sockets."""
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from workbuddy_bridge_intelmac import discovery


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        # Keep AF_UNIX paths below the smaller macOS sun_path limit.
        self.temporary = tempfile.TemporaryDirectory(prefix="wb-test-", dir="/tmp")
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)
        self.path = self.root / "sidecar-a1b2.sock"
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.server.listen(1)
        self.server.settimeout(2)
        self.threads = []

    def tearDown(self):
        self.server.close()
        for thread in self.threads:
            thread.join(2)
        self.temporary.cleanup()

    def serve(self, frames):
        received = []

        def run():
            connection, _ = self.server.accept()
            with connection:
                connection.settimeout(2)
                incoming = bytearray()
                while b"\n" not in incoming:
                    incoming.extend(connection.recv(65536))
                received.append(json.loads(incoming.split(b"\n", 1)[0]))
                data = b"".join(json.dumps(frame).encode() + b"\n" for frame in frames)
                # The response can be fragmented independently of JSON frames.
                connection.sendall(data[:7])
                connection.sendall(data[7:])

        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        return received

    def test_lifecycle_broadcast_and_unrelated_reply_before_response(self):
        received = self.serve([
            {"jsonrpc": "2.0", "id": 1, "method": "session.created", "params": {}},
            {"jsonrpc": "2.0", "id": 99, "result": {"wrong": True}},
            {"jsonrpc": "2.0", "id": 1, "result": {"runtimeId": "own-runtime"}},
        ])
        self.assertEqual(discovery.rpc(self.path, "session.create", {"port": 0}),
                         {"runtimeId": "own-runtime"})
        self.assertEqual(received[0]["params"], {"port": 0})

    def test_native_error_is_classified_without_exposing_message(self):
        self.serve([{"jsonrpc": "2.0", "id": 1, "error": {
            "code": -32000, "message": "Session not found: SYNTHETIC-PRIVATE-VALUE"}}])
        with self.assertRaises(discovery.WorkBuddyError) as caught:
            discovery.rpc(self.path, "session.reconnect", {"sessionId": "missing"})
        self.assertEqual(caught.exception.kind, "not_found")
        self.assertNotIn("SYNTHETIC-PRIVATE-VALUE", str(caught.exception))

    def test_non_numeric_error_code_cannot_expose_private_values(self):
        self.serve([{"jsonrpc": "2.0", "id": 1, "error": {
            "code": "SYNTHETIC-PRIVATE-VALUE", "message": "native error"}}])
        with self.assertRaises(discovery.WorkBuddyError) as caught:
            discovery.rpc(self.path, "session.reconnect", {"sessionId": "missing"})
        self.assertNotIn("SYNTHETIC-PRIVATE-VALUE", str(caught.exception))

    def test_regular_file_is_not_a_control_socket(self):
        file = self.root / "sidecar-cccc.sock"
        file.write_text("not a socket")
        with self.assertRaises(discovery.WorkBuddyError):
            discovery.checked_socket(file)

    def test_socket_owned_by_another_user_is_rejected(self):
        info = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=os.getuid() + 1)
        with patch.object(Path, "lstat", return_value=info), self.assertRaises(discovery.WorkBuddyError):
            discovery.checked_socket(self.path)

    def test_writable_parent_directory_is_rejected(self):
        os.chmod(self.root, 0o777)
        try:
            with self.assertRaises(discovery.WorkBuddyError):
                discovery.checked_socket(self.path)
        finally:
            os.chmod(self.root, 0o700)

    def test_symbolic_socket_alias_is_rejected(self):
        alias = self.root / "sidecar-dddd.sock"
        alias.symlink_to(self.path)
        with self.assertRaises(discovery.WorkBuddyError):
            discovery.checked_socket(alias)

    def test_only_direct_loopback_acp_endpoint_is_accepted(self):
        self.assertEqual(discovery.check_endpoint("http://127.0.0.1:3456/api/v1/acp"),
                         "http://127.0.0.1:3456")
        for endpoint in ["https://127.0.0.1:3456/api/v1/acp",
                         "http://localhost:3456/api/v1/acp",
                         "http://proxy.example:3456/api/v1/acp",
                         "http://user:pass@127.0.0.1:3456/api/v1/acp",
                         "http://127.0.0.1:3456/api/v1/acp?password=test",
                         "http://127.0.0.1:3456/api/v1/acp#fragment",
                         "http://127.0.0.1:0/api/v1/acp"]:
            with self.subTest(endpoint=endpoint), self.assertRaises(discovery.WorkBuddyError):
                discovery.check_endpoint(endpoint)

    def test_unknown_version_cannot_enter_write_discovery(self):
        with patch.object(discovery, "app_info", return_value={"version": "99.0.0", "intel_binary": True}), \
                patch.object(discovery, "find_sockets") as sockets:
            with self.assertRaises(discovery.WorkBuddyError):
                discovery.discover(for_write=True)
            sockets.assert_not_called()

    def test_arm_only_binary_cannot_enter_write_discovery(self):
        with patch.object(discovery, "app_info", return_value={"version": "5.6.2", "intel_binary": False}), \
                patch.object(discovery, "find_sockets") as sockets:
            with self.assertRaises(discovery.WorkBuddyError):
                discovery.discover(for_write=True)
            sockets.assert_not_called()

    def test_cold_start_requires_no_existing_control_socket(self):
        with patch.object(discovery, "app_info", return_value={"version": "5.6.2", "intel_binary": True}), \
                patch.object(discovery, "find_sockets", return_value=[]), \
                self.assertRaises(discovery.WorkBuddyError) as caught:
            discovery.discover(for_write=True)
        self.assertEqual(caught.exception.kind, "cold_start")

    def test_incompatible_existing_protocol_cannot_trigger_cold_fallback(self):
        with patch.object(discovery, "app_info", return_value={"version": "5.6.2", "intel_binary": True}), \
                patch.object(discovery, "find_sockets", return_value=[(self.path, 123)]), \
                patch.object(discovery, "rpc", return_value={"version": 999}) as rpc, \
                self.assertRaises(discovery.WorkBuddyError) as caught:
            discovery.discover(for_write=True)
        self.assertNotEqual(caught.exception.kind, "cold_start")
        rpc.assert_called_once_with(self.path, "sidecar.ping")

    def test_existing_host_auth_failure_cannot_trigger_cold_fallback(self):
        replies = [{"version": 6}, [{"sessionId": "__workbuddy_cli_host__synthetic",
                    "acpEndpoint": "http://127.0.0.1:3456/api/v1/acp"}],
                   {"text": "http://127.0.0.1:3456/?password=SYNTHETIC-PRIVATE"}]
        with patch.object(discovery, "app_info", return_value={"version": "5.6.2", "intel_binary": True}), \
                patch.object(discovery, "find_sockets", return_value=[(self.path, 123)]), \
                patch.object(discovery, "rpc", side_effect=replies), \
                patch.object(discovery, "auth_status", side_effect=discovery.WorkBuddyError("auth failed")), \
                self.assertRaises(discovery.WorkBuddyError) as caught:
            discovery.discover(for_write=True)
        self.assertNotEqual(caught.exception.kind, "cold_start")
        self.assertNotIn("SYNTHETIC-PRIVATE", str(caught.exception))

    def test_known_idle_sidecar_without_host_allows_cold_start(self):
        for version in ("5.6.2", "5.7.7"):
            with self.subTest(version=version), \
                    patch.object(discovery, "app_info", return_value={"version": version, "intel_binary": True}), \
                    patch.object(discovery, "find_sockets", return_value=[(self.path, 123)]), \
                    patch.object(discovery, "rpc", side_effect=[{"version": 6}, [{"sessionId": "idle-runtime"}]]), \
                    self.assertRaises(discovery.WorkBuddyError) as caught:
                discovery.discover(for_write=True)
            self.assertEqual(caught.exception.kind, "cold_start")

    def test_unrecognised_endpoint_or_malformed_list_cannot_be_cold_start(self):
        for sessions in ([{"sessionId": "unknown-host", "acpEndpoint": "http://127.0.0.1:3456/api/v1/acp"}],
                         {}, None, ["unexpected"]):
            with self.subTest(sessions=sessions), \
                    patch.object(discovery, "app_info", return_value={"version": "5.7.7", "intel_binary": True}), \
                    patch.object(discovery, "find_sockets", return_value=[(self.path, 123)]), \
                    patch.object(discovery, "rpc", side_effect=[{"version": 6}, sessions]), \
                    self.assertRaises(discovery.WorkBuddyError) as caught:
                discovery.discover(for_write=True)
            self.assertEqual(caught.exception.kind, "unavailable")

    def test_multiple_idle_sidecars_cannot_be_cold_start(self):
        with patch.object(discovery, "app_info", return_value={"version": "5.7.7", "intel_binary": True}), \
                patch.object(discovery, "find_sockets", return_value=[(self.path, 123), (self.path, 456)]), \
                patch.object(discovery, "rpc", side_effect=[{"version": 6}, [], {"version": 6}, []]), \
                self.assertRaises(discovery.WorkBuddyError) as caught:
            discovery.discover(for_write=True)
        self.assertEqual(caught.exception.kind, "unavailable")


if __name__ == "__main__":
    unittest.main()
