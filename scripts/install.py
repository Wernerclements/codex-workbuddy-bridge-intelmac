#!/usr/bin/env python3
"""Install the source checkout and append only our owned Codex MCP table."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
import uuid

SERVER = "workbuddy_intelmac"
MODULE = "workbuddy_bridge_intelmac.server"
DEFAULT_APP = Path("/Applications/WorkBuddy.app")
BEGIN = re.compile(rb"(?m)^# workbuddy-intelmac:begin v1 sep=([012]) sha256=([a-f0-9]{64})\r?\n")
END = b"# workbuddy-intelmac:end v1"
ROOT = Path(__file__).resolve().parents[1]
# Installation starts before the package and its dependencies are available.
SUPPORTED_VERSIONS = runpy.run_path(
    str(ROOT / "src/workbuddy_bridge_intelmac/compatibility.py")
)["SUPPORTED_VERSIONS"]


class InstallError(RuntimeError):
    pass


def _header_path(line: bytes) -> tuple[str, ...] | None:
    """Parse table headings alone; do not inspect other configuration values."""
    if not line.lstrip().startswith(b"["):
        return None
    try:
        tree = tomllib.loads(line.decode("utf-8") + "\n__bridge_header_probe__ = true\n")
        path = []
        while isinstance(tree, dict) and len(tree) == 1:
            key, tree = next(iter(tree.items()))
            if key == "__bridge_header_probe__":
                return tuple(path)
            path.append(key)
            if isinstance(tree, list):
                tree = tree[0]
        return tuple(path)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, IndexError):
        raise InstallError("无法识别配置中的表标题；未修改配置。") from None


def owned_span(raw: bytes) -> tuple[int, int] | None:
    matches = list(BEGIN.finditer(raw))
    if not matches:
        if b"# workbuddy-intelmac:begin" in raw or END in raw:
            raise InstallError("桥接配置标记不完整；请先检查配置，未修改。")
        return None
    if len(matches) != 1:
        raise InstallError("存在多个桥接配置块；未修改配置。")
    marker = matches[0]
    end_at = raw.find(END, marker.end())
    if end_at == -1 or raw.count(END) != 1:
        raise InstallError("桥接配置标记不完整；未修改配置。")
    newline = b"\r\n" if raw[marker.end() - 2:marker.end()] == b"\r\n" else b"\n"
    finish = end_at + len(END)
    if raw[finish:finish + len(newline)] != newline:
        raise InstallError("桥接配置结束标记已改变；未修改配置。")
    body = raw[marker.end():end_at]
    if hashlib.sha256(body).hexdigest().encode() != marker.group(2):
        raise InstallError("桥接配置块经过手工修改；请先保存并核对，未覆盖。")
    separator = newline * int(marker.group(1))
    start = marker.start() - len(separator)
    if start < 0 or raw[start:marker.start()] != separator:
        raise InstallError("桥接配置块边界已改变；未修改配置。")
    if _header_path(body.splitlines()[0]) != ("mcp_servers", SERVER):
        raise InstallError("桥接配置块内容不匹配；未修改配置。")
    return start, finish + len(newline)


def _contains_server(raw: bytes) -> bool:
    section: tuple[str, ...] = ()
    for line in raw.splitlines():
        heading = _header_path(line)
        if heading is not None:
            section = heading
            if heading[:2] == ("mcp_servers", SERVER):
                return True
            continue
        # Inspect assignment key names alone, including quoted/dotted names.
        stripped = line.lstrip()
        if b"=" in stripped and not stripped.startswith(b"#"):
            key = stripped.split(b"=", 1)[0].strip()
            key_path = _header_path(b"[" + key + b"]")
            path = section + (key_path or ())
            if path == ("mcp_servers",) or path[:2] == ("mcp_servers", SERVER):
                return True
    return False


def add_config(raw: bytes, python: Path, app: Path = DEFAULT_APP) -> bytes:
    app = app.expanduser().absolute()
    span = owned_span(raw)
    if span:
        if _contains_server(raw[:span[0]] + raw[span[1]:]):
            raise InstallError("桥接配置块外另有同名服务；未修改配置。")
        existing_body = raw[BEGIN.search(raw).end():raw.find(END)]
        own = tomllib.loads(existing_body.decode())["mcp_servers"][SERVER]
        installed_app = own.get("env", {}).get("WORKBUDDY_APP", str(DEFAULT_APP))
        if own.get("command") == str(python) and own.get("args") == ["-m", MODULE] and installed_app == str(app):
            return raw
        raise InstallError("已安装的桥接服务使用另一个 Python 或 WorkBuddy 路径；请先卸载旧配置。")
    if _contains_server(raw):
        raise InstallError("已有同名 MCP 服务 workbuddy_intelmac；未覆盖。")
    nl = b"\r\n" if b"\r\n" in raw else b"\n"
    count = 0 if not raw or raw.endswith(nl + nl) else 1 if raw.endswith(nl) else 2
    app_env = "" if app == DEFAULT_APP else f"env = {{ WORKBUDDY_APP = {json.dumps(str(app), ensure_ascii=False)} }}\n"
    body = (
        f"[mcp_servers.{SERVER}]\n"
        f"command = {json.dumps(str(python), ensure_ascii=False)}\n"
        f'args = ["-m", "{MODULE}"]\n'
        "startup_timeout_sec = 20\n"
        "tool_timeout_sec = 70\n"
        + app_env
    ).encode("utf-8").replace(b"\n", nl)
    marker = f"# workbuddy-intelmac:begin v1 sep={count} sha256={hashlib.sha256(body).hexdigest()}".encode()
    return raw + nl * count + marker + nl + body + END + nl


def remove_config(raw: bytes) -> bytes:
    span = owned_span(raw)
    if span is None:
        if _contains_server(raw):
            raise InstallError("同名服务没有本安装器的标记；未删除用户配置。")
        return raw
    remaining = raw[:span[0]] + raw[span[1]:]
    if _contains_server(remaining):
        raise InstallError("桥接配置块外另有同名服务；请先核对配置，未删除。")
    return remaining


def read_config(path: Path) -> bytes:
    if path.is_symlink():
        raise InstallError("配置路径是符号链接；请指定实际配置文件，未修改。")
    if not path.exists():
        return b""
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 10_000_000:
        raise InstallError("配置文件类型、归属或大小不符合安装要求。")
    return path.read_bytes()


def write_config(path: Path, before: bytes, after: bytes) -> Path | None:
    if before == after:
        return None
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if read_config(path) != before:
        raise InstallError("配置在安装期间发生变化；未写入，请重试。")
    backup = None
    if path.exists():
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.workbuddy-intelmac-{timestamp}-{uuid.uuid4().hex[:8]}.bak")
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(before)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, temporary = tempfile.mkstemp(prefix=".workbuddy-intelmac-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(after)
            stream.flush()
            os.fsync(stream.fileno())
        if read_config(path) != before:
            raise InstallError("配置在写入前发生变化；未覆盖，请重试。")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return backup


def runtime_info(executable: Path) -> dict:
    try:
        result = subprocess.run(
            [str(executable), "-c", "import json,sys,platform; print(json.dumps({'version':list(sys.version_info[:3]),'machine':platform.machine(),'platform':sys.platform}))"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        raise InstallError("无法检查指定 Python 解释器。") from None


def find_python(requested: str | None = None) -> Path:
    candidates = [requested] if requested else [shutil.which("python3.12"), "/usr/local/bin/python3.12", sys.executable, shutil.which("python3")]
    for candidate in candidates:
        if not candidate:
            continue
        executable = Path(candidate).expanduser().absolute()
        if not executable.exists():
            continue
        info = runtime_info(executable)
        if info["version"] >= [3, 12, 0] and info["machine"] == "x86_64" and info["platform"] == "darwin":
            return executable
    raise InstallError("未找到 Intel macOS Python 3.12 或以上版本；请用 --python 指定已有解释器。")


def check_app(app: Path, allow_unverified: bool = False) -> str:
    if platform.system() != "Darwin" or platform.machine() != "x86_64":
        raise InstallError("本安装器要求 Intel macOS（x86_64）。")
    arm = subprocess.run(["/usr/sbin/sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True)
    if arm.stdout.strip() == "1":
        raise InstallError("当前为 Apple Silicon/Rosetta；本项目面向 Intel Mac。")
    try:
        with (app / "Contents/Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
        version = str(info["CFBundleShortVersionString"])
        binary = app / "Contents/MacOS" / info["CFBundleExecutable"]
        architectures = subprocess.run(["/usr/bin/lipo", "-archs", str(binary)], capture_output=True, text=True, check=True).stdout.split()
    except (OSError, KeyError, ValueError, plistlib.InvalidFileException, subprocess.SubprocessError):
        raise InstallError("无法检查 WorkBuddy 应用及其可执行文件。") from None
    if "x86_64" not in architectures:
        raise InstallError("WorkBuddy 应用不包含 Intel 可执行文件。")
    if version not in SUPPORTED_VERSIONS and not allow_unverified:
        raise InstallError(f"WorkBuddy {version} 尚未验证；需先做兼容诊断，或明确使用 --allow-unverified-version。")
    return version


def _run_step(command: list[str], label: str) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise InstallError(f"{label}失败（退出码 {result.returncode}）；未修改 Codex 配置。请检查网络、依赖或构建工具。")


def install_environment(python: Path, environment: Path, allow_source_build: bool) -> Path:
    target = environment / "bin/python"
    marker = environment / ".workbuddy-intelmac-environment"
    if environment.exists():
        if not marker.is_file() or marker.read_text().strip() != str(ROOT):
            raise InstallError("目标环境已存在且不属于本安装器；请指定一个新的 --venv 路径。")
        info = runtime_info(target)
        if info["version"] < [3, 12, 0] or info["machine"] != "x86_64":
            raise InstallError("现有安装环境的 Python 版本或架构不兼容。")
    else:
        environment.parent.mkdir(parents=True, exist_ok=True)
        _run_step([str(python), "-m", "venv", str(environment)], "创建 Python 环境")
        marker.write_text(str(ROOT) + "\n")
    flags = [] if allow_source_build else ["--only-binary=cryptography,cffi,pydantic_core,rpds-py"]
    if allow_source_build and any(shutil.which(tool) is None for tool in ("clang", "rustc", "pkg-config")):
        raise InstallError("允许原生编译需要 clang、Rust 和 pkg-config；未修改 Codex 配置。")
    _run_step([str(target), "-m", "pip", "install", *flags, "-r", str(ROOT / "requirements-test.lock")], "安装固定依赖（Intel 上部分版本可能没有预编译包）")
    _run_step([str(target), "-m", "pip", "install", "--no-deps", "--editable", str(ROOT)], "安装桥接源码")
    _run_step([str(target), "-m", "pip", "check"], "检查依赖")
    _run_step([str(target), "-c", f"import importlib; importlib.import_module('{MODULE}')"], "检查 MCP 服务模块")
    return target


def check_environment_path(environment: Path) -> None:
    if environment.is_symlink():
        raise InstallError("安装环境不能是符号链接。")
    if environment.exists():
        marker = environment / ".workbuddy-intelmac-environment"
        if not marker.is_file() or marker.read_text().strip() != str(ROOT):
            raise InstallError("目标环境已存在且不属于本安装器；请指定一个新的 --venv 路径。")


def default_config() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser() / "config.toml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装 Intel Mac WorkBuddy MCP 桥接器；不覆盖已有配置。")
    parser.add_argument("--python", help="已有的 Intel Python 3.12+ 解释器")
    parser.add_argument("--venv", type=Path, default=ROOT / ".venv", help="独立 Python 环境路径")
    parser.add_argument("--config", type=Path, default=default_config(), help="Codex 配置文件路径")
    parser.add_argument("--app", type=Path, default=DEFAULT_APP)
    parser.add_argument("--allow-source-build", action="store_true", help="允许安装原生依赖源码，需要现有构建工具")
    parser.add_argument("--allow-unverified-version", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="只检查与预览，不安装、不改配置")
    args = parser.parse_args(argv)
    try:
        app = args.app.expanduser().absolute()
        version = check_app(app, args.allow_unverified_version)
        python = find_python(args.python)
        config = args.config.expanduser().absolute()
        environment = args.venv.expanduser().absolute()
        check_environment_path(environment)
        before = read_config(config)
        after = add_config(before, environment / "bin/python", app)
        print(f"Intel macOS 检查通过；WorkBuddy {version}。")
        print(f"Python：{python}\n环境：{environment}\n配置：{config}")
        if args.dry_run:
            print("预览完成：配置无冲突；未安装依赖、未修改文件。原生依赖能否使用预编译包，将在实际安装时检查。")
            return 0
        install_environment(python, environment, args.allow_source_build)
        backup = write_config(config, before, after)
        print("桥接器已安装；请重新加载 Codex 的 MCP 服务。")
        if backup:
            print(f"原配置备份：{backup}")
        return 0
    except InstallError as error:
        print(f"安装未完成：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
