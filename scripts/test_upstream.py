"""Verify pinned upstream, apply our patch to a temporary copy, run all tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    source = ROOT / "reference/upstream"
    manifest = json.loads((ROOT / "reference/upstream-sha256.json").read_text())
    for name, expected in manifest.items():
        actual = hashlib.sha256((source / name).read_bytes()).hexdigest()
        if actual != expected:
            raise SystemExit(f"Pinned upstream changed: {name}")
    with tempfile.TemporaryDirectory(prefix="workbuddy-tests-") as directory:
        working = Path(directory)
        shutil.copytree(source, working, dirs_exist_ok=True)
        for patch in sorted((ROOT / "patches").glob("*.patch")):
            subprocess.run(
                ["patch", "--batch", "--forward", "-p1", "-i", str(patch)],
                cwd=working, check=True,
            )
        for test in (ROOT / "tests").glob("test_*.py"):
            shutil.copy2(test, working / "workbuddy_bridge" / test.name)
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s",
             "workbuddy_bridge", "-p", "test_*.py", "-v"],
            cwd=working, capture_output=True, text=True,
        )
        output = result.stdout + result.stderr
        print(output, end="")
        report = ROOT / ".local"
        report.mkdir(exist_ok=True)
        (report / "unit-tests.txt").write_text(output)
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
