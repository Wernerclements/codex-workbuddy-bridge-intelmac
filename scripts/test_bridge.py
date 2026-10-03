"""Run this project's tests separately from the pinned Windows reference."""
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
loader = unittest.TestLoader()
suite = unittest.TestSuite()
for path in sorted((ROOT / "tests").glob("test_*.py")):
    if path.name != "test_macos_review_paths.py":
        suite.addTests(loader.discover(str(ROOT / "tests"), pattern=path.name))
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
