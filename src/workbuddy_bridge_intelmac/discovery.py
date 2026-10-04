"""macOS sidecar discovery and bounded JSON-RPC over a user-owned socket."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import plistlib
import re
import socket
import stat
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit

import httpx


APP = Path(os.environ.get("WORKBUDDY_APP", "/Applications/WorkBuddy.app"))
WORKER_PREFIX = "codex-intelmac-"
HEADERS = {"x-codebuddy-request": "1"}
SUPPORTED_VERSION = "5.6.2"
SUPPORTED_PROTOCOL = 6


class WorkBuddyError(RuntimeError):
    """An error with a public, credential-free description."""

    def __init__(self, message: str, *, status: str = "failed", kind: str = ""):
        super().__init__(message)
        self.public_message = message
        self.status = status
        self.kind = kind


def checked_socket(path: Path) -> None:
    try:
        info = path.lstat()
        parent = path.parent.stat()
    except OSError:
        raise WorkBuddyError("WorkBuddy 控制通道已失效；请确认应用正在运行") from None
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise WorkBuddyError("控制通道不是当前用户拥有的 Unix socket")
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise WorkBuddyError("控制通道目录归属或写入权限不符合要求")
    if not re.fullmatch(r"sidecar-[a-f0-9]+\.sock", path.name):
        raise WorkBuddyError("控制通道名称不符合已验证协议")


def rpc(path: Path, method: str, params: dict | None = None, *, timeout: float = 8) -> object:
    checked_socket(path)
    request = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        request["params"] = params
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(path))
            connection.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode())
            response = bytearray()
            deadline = time.monotonic() + timeout
            payload = None
            while payload is None:
                connection.settimeout(max(0.01, deadline - time.monotonic()))
                data = connection.recv(65536)
                if not data:
                    raise WorkBuddyError("WorkBuddy 控制通道在回复前关闭")
                response.extend(data)
                if len(response) > 4_000_000:
                    raise WorkBuddyError("WorkBuddy 控制通道回复过大")
                while b"\n" in response:
                    frame, _, rest = response.partition(b"\n")
                    response = bytearray(rest)
                    value = json.loads(frame)
                    # A long session.create emits lifecycle broadcasts before
                    # the response; do not mistake those for its RPC result.
                    if isinstance(value, dict) and value.get("id") == 1 and "method" not in value:
                        payload = value
                        break
                if time.monotonic() >= deadline and payload is None:
                    raise WorkBuddyError(f"WorkBuddy 控制请求超时：{method}")
    except (OSError, ValueError):
        raise WorkBuddyError(f"WorkBuddy 控制请求失败：{method}") from None
    if not isinstance(payload, dict) or payload.get("id") != 1:
        raise WorkBuddyError("WorkBuddy 控制回复无效")
    if "error" in payload:
        # Native errors can contain environment values or startup credentials.
        code = (payload.get("error") or {}).get("code", "unknown")
        code = code if type(code) is int else "unknown"
        native_message = str((payload.get("error") or {}).get("message", ""))
        kind = "not_found" if native_message.startswith("Session not found:") else ""
        raise WorkBuddyError(f"WorkBuddy 拒绝控制请求 {method}（{code}）", kind=kind)
    return payload.get("result")


def check_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.path != "/api/v1/acp" or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise WorkBuddyError("ACP 地址不是预期的本机端点")
    try:
        port = parsed.port
    except ValueError:
        raise WorkBuddyError("ACP 端口无效") from None
    if not port or not 1 <= port <= 65535:
        raise WorkBuddyError("ACP 端口无效")
    return f"http://127.0.0.1:{port}"


def app_info() -> dict:
    if sys.platform != "darwin":
        raise WorkBuddyError("真实桌面通信需要 macOS")
    try:
        info = plistlib.loads((APP / "Contents/Info.plist").read_bytes())
        executable = APP / "Contents/MacOS" / info["CFBundleExecutable"]
        architecture = subprocess.run(["file", str(executable)], capture_output=True, text=True, check=True).stdout
    except (OSError, KeyError, ValueError, subprocess.SubprocessError):
        raise WorkBuddyError("无法检查 WorkBuddy；请确认应用安装位置") from None
    return {"version": str(info.get("CFBundleShortVersionString", "")),
            "intel_binary": "x86_64" in architecture, "executable": executable,
            # Electron resolves a virtual ASAR entry to its unpacked launcher
            # while preserving relative imports into the packed dist bundle.
            "cli": APP / "Contents/Resources/app.asar/cli/bin/codebuddy",
            "cli_entry_present": (APP / "Contents/Resources/app.asar.unpacked/cli/bin/codebuddy").is_file()
                                 and (APP / "Contents/Resources/app.asar").is_file()}


def find_sockets() -> list[tuple[Path, int]]:
    try:
        processes = subprocess.run(["ps", "-axo", "uid=,pid=,comm="], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        raise WorkBuddyError("无法检查 WorkBuddy 进程") from None
    found: dict[Path, int] = {}
    prefix = str(APP / "Contents/MacOS") + "/"
    for line in processes.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) != 3 or parts[0] != str(os.getuid()) or not parts[2].startswith(prefix):
            continue
        try:
            pid = int(parts[1])
            output = subprocess.run(["lsof", "-a", "-p", str(pid), "-U", "-Fn"], capture_output=True, text=True, timeout=5).stdout
        except (ValueError, OSError, subprocess.SubprocessError):
            continue
        for entry in output.splitlines():
            if entry.startswith("n/"):
                path = Path(entry[1:])
                if re.fullmatch(r"sidecar-[a-f0-9]+\.sock", path.name):
                    try:
                        checked_socket(path)
                        found[path] = pid
                    except WorkBuddyError:
                        pass
    return sorted(found.items(), key=lambda item: str(item[0]))


@dataclass(repr=False)
class Desktop:
    socket_path: Path
    sidecar_pid: int
    version: str
    protocol: int
    host_id: str
    endpoint: str
    password: str = field(repr=False)
    runtime_id: str = ""


def auth_status(endpoint: str, password: str) -> dict:
    base = check_endpoint(endpoint)
    headers = dict(HEADERS)
    if password:
        headers["Authorization"] = "Bearer " + password
    try:
        with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
            response = client.get(base + "/api/v1/auth/status", headers=headers)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError):
        raise WorkBuddyError("WorkBuddy 本机认证失败或端点不可用") from None
    if data.get("authEnabled") and data.get("authenticated") is not True:
        raise WorkBuddyError("WorkBuddy 本机凭证已失效")
    return {"auth_enabled": bool(data.get("authEnabled")), "authenticated": bool(data.get("authenticated"))}


def discover(*, for_write: bool = False) -> Desktop:
    info = app_info()
    if for_write and (info["version"] != SUPPORTED_VERSION or not info["intel_binary"]):
        raise WorkBuddyError("当前 WorkBuddy 版本或架构尚未通过真实任务验收；请先更新兼容适配")
    candidates: list[Desktop] = []
    sockets = find_sockets()
    for path, pid in sockets:
        try:
            ping = rpc(path, "sidecar.ping") or {}
            protocol = int(ping.get("version", 0))
            if protocol != SUPPORTED_PROTOCOL:
                continue
            sessions = rpc(path, "session.list") or []
            for host in sessions:
                if not str(host.get("sessionId", "")).startswith("__workbuddy_cli_host__") or not host.get("acpEndpoint"):
                    continue
                endpoint = str(host["acpEndpoint"])
                check_endpoint(endpoint)
                captured = rpc(path, "session.capture", {"sessionId": host["sessionId"], "lines": 300}) or {}
                match = re.search(r"[?&]password=([^\s\x00-\x1f&]+)", str(captured.get("text", "")))
                password = unquote(match.group(1)) if match else ""
                status = auth_status(endpoint, password)
                if status.get("auth_enabled") is not True or status.get("authenticated") is not True:
                    continue
                candidates.append(Desktop(path, pid, info["version"], protocol,
                                          str(host["sessionId"]), endpoint, password,
                                          str(host.get("runtimeId", ""))))
        except (WorkBuddyError, TypeError, ValueError, AttributeError):
            continue
    if len(candidates) > 1:
        raise WorkBuddyError("发现多个 WorkBuddy 桌面实例；请保留一个实例后重试")
    if not candidates:
        raise WorkBuddyError("未发现可认证的 WorkBuddy 桌面通道；请启动应用并登录",
                            kind="unavailable" if sockets else "cold_start")
    return candidates[0]
