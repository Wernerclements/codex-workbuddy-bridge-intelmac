"""Configuration preservation and installer preflight; no real user config."""
from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from scripts import install, uninstall


class InstallCompatibilityTests(unittest.TestCase):
    def make_app(self, root: Path, version: str) -> Path:
        app = root / "WorkBuddy.app"
        contents = app / "Contents"
        contents.mkdir(parents=True, exist_ok=True)
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleShortVersionString": version,
            "CFBundleExecutable": "WorkBuddy",
        }))
        return app

    def intel_commands(self, command, **kwargs):
        if command == ["/usr/sbin/sysctl", "-n", "hw.optional.arm64"]:
            return subprocess.CompletedProcess(command, 0, stdout="0\n")
        if command[:2] == ["/usr/bin/lipo", "-archs"]:
            return subprocess.CompletedProcess(command, 0, stdout="x86_64\n")
        self.fail(f"Unexpected preflight command: {command}")

    def test_supported_versions_pass_real_app_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            for version in ("5.6.2", "5.7.7", "5.8.2"):
                app = self.make_app(Path(temporary), version)
                with self.subTest(version=version), patch.object(install.platform, "system", return_value="Darwin"), patch.object(install.platform, "machine", return_value="x86_64"), patch.object(install.subprocess, "run", side_effect=self.intel_commands):
                    self.assertEqual(install.check_app(app), version)

    def test_unaccepted_versions_stop_before_install_or_config_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            original = b'model="keep"\n'
            config.write_bytes(original)
            for version in ("5.7.6", "5.8.1", "5.8.3", "99.0.0"):
                app = self.make_app(root, version)
                with self.subTest(version=version), patch.object(install.platform, "system", return_value="Darwin"), patch.object(install.platform, "machine", return_value="x86_64"), patch.object(install.subprocess, "run", side_effect=self.intel_commands), patch.object(install, "find_python") as python, patch.object(install, "install_environment") as environment, patch.object(install, "write_config") as write, redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    self.assertEqual(install.main(["--app", str(app), "--config", str(config), "--venv", str(root / "venv")]), 1)
                python.assert_not_called()
                environment.assert_not_called()
                write.assert_not_called()
                self.assertEqual(config.read_bytes(), original)
                self.assertFalse((root / "venv").exists())
                self.assertEqual(list(root.glob("*.bak")), [])

    def test_577_install_preserves_config_and_creates_exact_backup(self):
        self.assert_install_preserves_config("5.7.7")

    def test_582_install_preserves_config_and_creates_exact_backup(self):
        self.assert_install_preserves_config("5.8.2")

    def assert_install_preserves_config(self, version: str):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = self.make_app(root, version)
            config = root / "config.toml"
            environment = root / "venv"
            original = b'model="keep"\n'
            config.write_bytes(original)
            with patch.object(install.platform, "system", return_value="Darwin"), patch.object(install.platform, "machine", return_value="x86_64"), patch.object(install.subprocess, "run", side_effect=self.intel_commands), patch.object(install, "find_python", return_value=Path("/python3.12")), patch.object(install, "install_environment") as install_environment, redirect_stdout(StringIO()):
                self.assertEqual(install.main(["--app", str(app), "--config", str(config), "--venv", str(environment)]), 0)
            install_environment.assert_called_once_with(Path("/python3.12"), environment, False)
            installed = config.read_bytes()
            self.assertTrue(installed.startswith(original))
            own = tomllib.loads(installed.decode())["mcp_servers"][install.SERVER]
            self.assertEqual(own["env"]["WORKBUDDY_APP"], str(app))
            backups = list(root.glob("*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)

    def test_supported_version_does_not_bypass_rosetta_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary), "5.8.2")
            with patch.object(install.platform, "system", return_value="Darwin"), patch.object(install.platform, "machine", return_value="x86_64"), patch.object(install.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="1\n")) as run:
                with self.assertRaisesRegex(install.InstallError, "Apple Silicon/Rosetta"):
                    install.check_app(app)
            self.assertEqual(run.call_count, 1)

    def test_supported_version_requires_intel_app_binary(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = self.make_app(Path(temporary), "5.8.2")
            results = [subprocess.CompletedProcess([], 0, stdout="0\n"), subprocess.CompletedProcess([], 0, stdout="arm64\n")]
            with patch.object(install.platform, "system", return_value="Darwin"), patch.object(install.platform, "machine", return_value="x86_64"), patch.object(install.subprocess, "run", side_effect=results):
                with self.assertRaisesRegex(install.InstallError, "Intel 可执行文件"):
                    install.check_app(app)

    def test_installer_loads_shared_versions_without_installed_package(self):
        # -I removes checkout/PYTHONPATH imports; -S disables site packages.
        code = (
            "import runpy, sys; "
            "installer = runpy.run_path(sys.argv[1]); "
            "assert installer['SUPPORTED_VERSIONS'] == frozenset({'5.6.2', '5.7.7', '5.8.2'}); "
            "assert not any(name.startswith('workbuddy_bridge_intelmac') for name in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", code, str(install.ROOT / "scripts/install.py")],
            cwd=tempfile.gettempdir(), capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class InstallConfigTests(unittest.TestCase):
    def setUp(self):
        self.python = Path("/temporary/项目 space/.venv/bin/python")

    def test_round_trip_preserves_user_config_byte_for_byte(self):
        examples = [b"", b'model = "example"', b'model = "example"\n', b'model = "example"\n\n',
                    b'# retain comments\r\n[mcp_servers.other]\r\ncommand = "original"\r\nargs = ["secret-test-value"]\r\n']
        for original in examples:
            with self.subTest(original=original):
                changed = install.add_config(original, self.python)
                self.assertTrue(changed.startswith(original))
                parsed = tomllib.loads(changed.decode())
                self.assertEqual(parsed["mcp_servers"][install.SERVER]["command"], str(self.python))
                self.assertEqual(install.remove_config(changed), original)

    def test_second_install_is_idempotent(self):
        changed = install.add_config(b"", self.python)
        self.assertEqual(install.add_config(changed, self.python), changed)

    def test_existing_service_is_never_overwritten_or_removed(self):
        examples = [
            b'[mcp_servers.workbuddy_intelmac]\ncommand="original"\n',
            b'[mcp_servers."workbuddy_intelmac".env]\nTOKEN="test"\n',
            b'[mcp_servers]\nworkbuddy_intelmac={command="original"}\n',
            b'mcp_servers."workbuddy_intelmac".command="original"\n',
            b'mcp_servers={workbuddy_intelmac={command="original"}}\n',
        ]
        for original in examples:
            with self.subTest(original=original), self.assertRaises(install.InstallError):
                install.add_config(original, self.python)
            with self.subTest(original=original), self.assertRaises(install.InstallError):
                install.remove_config(original)

    def test_inline_table_for_other_server_rejected_before_invalid_toml_append(self):
        with self.assertRaises(install.InstallError):
            install.add_config(b'mcp_servers={other={command="original"}}\n', self.python)

    def test_modified_owned_block_is_preserved(self):
        changed = install.add_config(b"", self.python).replace(b"startup_timeout_sec = 20", b"startup_timeout_sec = 25")
        with self.assertRaises(install.InstallError):
            install.add_config(changed, self.python)
        with self.assertRaises(install.InstallError):
            install.remove_config(changed)

    def test_different_install_location_is_conflict(self):
        changed = install.add_config(b"", self.python)
        with self.assertRaises(install.InstallError):
            install.add_config(changed, Path("/another/bin/python"))

    def test_custom_app_path_is_forwarded_to_mcp_runtime(self):
        app = Path("/custom/应用 space/WorkBuddy.app")
        changed = install.add_config(b"# original\n", self.python, app)
        own = tomllib.loads(changed.decode())["mcp_servers"][install.SERVER]
        self.assertEqual(own["env"], {"WORKBUDDY_APP": str(app)})
        self.assertEqual(install.add_config(changed, self.python, app), changed)
        self.assertEqual(install.remove_config(changed), b"# original\n")

    def test_different_app_path_is_conflict(self):
        changed = install.add_config(b"", self.python, Path("/custom/WorkBuddy.app"))
        with self.assertRaises(install.InstallError):
            install.add_config(changed, self.python, Path("/another/WorkBuddy.app"))
        with self.assertRaises(install.InstallError):
            install.add_config(changed, self.python)
        default = install.add_config(b"", self.python)
        with self.assertRaises(install.InstallError):
            install.add_config(default, self.python, Path("/custom/WorkBuddy.app"))

    def test_main_passes_custom_app_path_to_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            app = Path(temporary) / "Custom WorkBuddy.app"
            environment = Path(temporary) / "venv"
            with patch.object(install, "check_app", return_value="5.6.2") as check, patch.object(install, "find_python", return_value=Path("/python3.12")), patch.object(install, "install_environment"), redirect_stdout(StringIO()):
                code = install.main(["--config", str(config), "--venv", str(environment), "--app", str(app)])
            self.assertEqual(code, 0)
            check.assert_called_once_with(app, False)
            own = tomllib.loads(config.read_text())["mcp_servers"][install.SERVER]
            self.assertEqual(own["env"]["WORKBUDDY_APP"], str(app))

    def test_unrelated_block_appended_after_install_is_preserved(self):
        original = b'model="example"\n'
        extra = b'\n[mcp_servers.other]\ncommand="original"\n'
        changed = install.add_config(original, self.python) + extra
        self.assertEqual(install.remove_config(changed), original + extra)

    def test_other_server_may_have_same_environment_key(self):
        original = b'[mcp_servers.other.env]\nworkbuddy_intelmac="unrelated"\n'
        self.assertEqual(install.remove_config(install.add_config(original, self.python)), original)

    def test_duplicate_and_partial_markers_are_rejected(self):
        valid = install.add_config(b"", self.python)
        for changed in (valid + valid, valid.replace(install.END, b""), b"# workbuddy-intelmac:begin broken\n"):
            with self.subTest(changed=changed), self.assertRaises(install.InstallError):
                install.remove_config(changed)

    def test_atomic_write_creates_private_exact_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            original = b'[mcp_servers.other.env]\nTOKEN="not-displayed"\n'
            config.write_bytes(original)
            backup = install.write_config(config, original, install.add_config(original, self.python))
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            installed = config.read_bytes()
            install.write_config(config, installed, install.remove_config(installed))
            self.assertEqual(config.read_bytes(), original)

    def test_concurrent_update_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            config.write_bytes(b'model="newer"\n')
            with self.assertRaises(install.InstallError):
                install.write_config(config, b'model="older"\n', b"replacement")
            self.assertEqual(config.read_bytes(), b'model="newer"\n')

    def test_symlink_is_not_modified(self):
        with tempfile.TemporaryDirectory() as temporary:
            actual = Path(temporary) / "actual.toml"
            actual.write_bytes(b"# original\n")
            alias = Path(temporary) / "alias.toml"
            alias.symlink_to(actual)
            with self.assertRaises(install.InstallError):
                install.read_config(alias)

    def test_new_config_is_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "new" / "config.toml"
            self.assertIsNone(install.write_config(config, b"", install.add_config(b"", self.python)))
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_dry_run_does_not_install_write_or_display_user_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            original = b'[mcp_servers.other.env]\nTOKEN="DO-NOT-DISPLAY"\n'
            config.write_bytes(original)
            output = StringIO()
            with patch.object(install, "check_app", return_value="5.6.2"), patch.object(install, "find_python", return_value=Path("/usr/bin/python3.12")), patch.object(install, "install_environment") as environment, redirect_stdout(output), redirect_stderr(output):
                result = install.main(["--dry-run", "--config", str(config), "--venv", str(Path(temporary) / "venv")])
            self.assertEqual(result, 0)
            self.assertEqual(config.read_bytes(), original)
            environment.assert_not_called()
            self.assertNotIn("DO-NOT-DISPLAY", output.getvalue())
            self.assertEqual(list(Path(temporary).iterdir()), [config])

    def test_uninstall_dry_run_preserves_everything(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            installed = install.add_config(b"# user config\n", self.python)
            config.write_bytes(installed)
            result = Path(temporary) / "result.txt"
            result.write_text("keep task output")
            with redirect_stdout(StringIO()):
                self.assertEqual(uninstall.main(["--config", str(config), "--dry-run"]), 0)
            self.assertEqual(config.read_bytes(), installed)
            self.assertEqual(result.read_text(), "keep task output")

    def test_missing_uninstall_config_creates_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "missing" / "config.toml"
            with redirect_stdout(StringIO()):
                self.assertEqual(uninstall.main(["--config", str(config)]), 0)
            self.assertFalse(config.parent.exists())

    def test_foreign_environment_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(install.InstallError):
                install.check_environment_path(Path(temporary))

    def test_reject_non_mac_platform(self):
        with patch.object(install.platform, "system", return_value="Linux"):
            with self.assertRaises(install.InstallError):
                install.check_app(Path("/irrelevant"))

    def test_codex_home_is_respected(self):
        with patch.dict(os.environ, {"CODEX_HOME": "/custom/codex"}):
            self.assertEqual(install.default_config(), Path("/custom/codex/config.toml"))

    def test_dependency_failure_leaves_config_and_creates_no_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            original = b'model="keep"\n'
            config.write_bytes(original)
            with patch.object(install, "check_app", return_value="5.6.2"), patch.object(install, "find_python", return_value=Path("/python3.12")), patch.object(install, "install_environment", side_effect=install.InstallError("native dependency unavailable")), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                code = install.main(["--config", str(config), "--venv", str(Path(temporary) / "venv")])
            self.assertEqual(code, 1)
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(list(Path(temporary).iterdir()), [config])

    def test_default_dependency_install_requires_native_wheels(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = Path(temporary)
            (environment / ".workbuddy-intelmac-environment").write_text(str(install.ROOT))
            with patch.object(install, "runtime_info", return_value={"version": [3, 12, 0], "machine": "x86_64"}), patch.object(install, "_run_step") as step:
                install.install_environment(Path("/python3.12"), environment, False)
            command = step.call_args_list[0].args[0]
            self.assertIn("--only-binary=cryptography,cffi,pydantic_core,rpds-py", command)

    def test_source_build_requires_existing_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = Path(temporary)
            (environment / ".workbuddy-intelmac-environment").write_text(str(install.ROOT))
            with patch.object(install, "runtime_info", return_value={"version": [3, 12, 0], "machine": "x86_64"}), patch.object(install.shutil, "which", return_value=None), patch.object(install, "_run_step") as step:
                with self.assertRaises(install.InstallError):
                    install.install_environment(Path("/python3.12"), environment, True)
            step.assert_not_called()


if __name__ == "__main__":
    unittest.main()
