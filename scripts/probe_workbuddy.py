"""Read-only WorkBuddy diagnostic. Never print or save credentials or messages."""
from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


APP = Path("/Applications/WorkBuddy.app")
READ_METHODS = {"sidecar.ping", "session.list", "session.capture"}


def rpc(path: Path, method: str, params=None):
    if method not in READ_METHODS:
        raise ValueError("Only read methods are allowed")
    info = path.stat()
    if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
        raise ValueError("Socket is not owned by the current user")
    request = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        request["params"] = params
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(4)
        connection.connect(str(path))
        connection.sendall((json.dumps(request) + "\n").encode())
        response = bytearray()
        while b"\n" not in response:
            chunk = connection.recv(65536)
            if not chunk:
                raise EOFError("Socket closed before its response")
            response.extend(chunk)
            if len(response) > 4_000_000:
                raise ValueError("Response size limit exceeded")
    payload = json.loads(response.split(b"\n", 1)[0])
    if "error" in payload:
        raise RuntimeError("Sidecar rejected the read request")
    return payload.get("result")


def find_sockets() -> list[Path]:
    processes = subprocess.run(
        ["ps", "-axo", "pid=,comm="], capture_output=True, text=True, check=True,
    ).stdout
    candidates = set()
    for line in processes.splitlines():
        if "/WorkBuddy.app/Contents/MacOS/" not in line:
            continue
        pid = line.split(maxsplit=1)[0]
        output = subprocess.run(
            ["lsof", "-a", "-p", pid, "-U", "-Fn"],
            capture_output=True, text=True,
        ).stdout
        for entry in output.splitlines():
            if entry.startswith("n/"):
                path = Path(entry[1:])
                if re.fullmatch(r"sidecar-[a-f0-9]+\.sock", path.name):
                    candidates.add(path)
    return sorted(candidates)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "Redirect refused", headers, fp)


def probe_auth(path: Path, host: dict) -> dict:
    endpoint = str(host.get("acpEndpoint", ""))
    parsed = urllib.parse.urlsplit(endpoint)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.path != "/api/v1/acp" or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("ACP endpoint is not a supported loopback address")
    captured = rpc(path, "session.capture", {"sessionId": host["sessionId"], "lines": 300}) or {}
    match = re.search(r"[?&]password=([^\s\x00-\x1f&]+)", str(captured.get("text", "")))
    password = urllib.parse.unquote(match.group(1)) if match else ""
    headers = {"x-codebuddy-request": "1"}
    if password:
        headers["Authorization"] = "Bearer " + password
    request = urllib.request.Request(endpoint.removesuffix("/api/v1/acp") + "/api/v1/auth/status", headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=4) as response:
        payload = json.loads(response.read(65536))
        return {"http_status": response.status,
                "auth_enabled": payload.get("authEnabled"),
                "authenticated": payload.get("authenticated")}


def main() -> int:
    if sys.platform != "darwin":
        print(json.dumps({"supported_platform": False, "reason": "Requires macOS"}))
        return 2
    report = {"read_only": True, "workbuddy_version": None,
              "sidecar_count": 0, "sidecars": [], "history_schema": {}}
    try:
        info = plistlib.loads((APP / "Contents/Info.plist").read_bytes())
        report["workbuddy_version"] = info.get("CFBundleShortVersionString")
        binary = APP / "Contents/MacOS" / info["CFBundleExecutable"]
        architecture = subprocess.run(["file", str(binary)], capture_output=True, text=True, check=True).stdout
        report["intel_binary_present"] = "x86_64" in architecture
        for path in find_sockets():
            try:
                ping = rpc(path, "sidecar.ping") or {}
                sessions = rpc(path, "session.list") or []
                host = next((item for item in sessions if str(item.get("sessionId", "")).startswith("__workbuddy_cli_host__")), None)
                entry = {"protocol_version": ping.get("version"), "session_count": len(sessions),
                         "desktop_host_present": host is not None,
                         "pid_file_present": (path.parent / "sidecar.pid").is_file()}
                if host:
                    entry["auth"] = probe_auth(path, host)
                report["sidecars"].append(entry)
            except Exception as error:
                report["sidecars"].append({"error_type": type(error).__name__})
        report["sidecar_count"] = len(report["sidecars"])
        database = Path.home() / ".workbuddy/workbuddy.db"
        if database.is_file():
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
                report["history_schema"] = {"sessions_table_present": bool(columns),
                    "has_transport": "transport" in columns,
                    "has_session_settings": "session_settings" in columns}
    except Exception as error:
        report["error_type"] = type(error).__name__
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if any(item.get("auth", {}).get("authenticated") is True
                    for item in report["sidecars"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
