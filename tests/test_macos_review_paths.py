from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from workbuddy_bridge.review_sessions import _transcript_mentions_target


class ReviewPathRegressionTests(unittest.TestCase):
    def test_accepts_original_and_canonical_names_for_the_same_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report with spaces.md"
            target.write_text("sample")
            self.assertTrue(_transcript_mentions_target(f"审查目标：{target}\n", str(target.resolve())))
            self.assertTrue(_transcript_mentions_target(f"审查目标：{target.resolve()}\n", str(target)))

    def test_rejects_longer_filename_with_the_same_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.md"
            target.write_text("sample")
            self.assertFalse(_transcript_mentions_target(f"审查目标：{target}.backup\n", str(target)))
            self.assertFalse(_transcript_mentions_target(f"审查目标：{target}/another\n", str(target)))

    def test_rejects_a_different_parent_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.md"
            target.write_text("sample")
            other = Path(directory) / "other" / target.name
            self.assertFalse(_transcript_mentions_target(f"审查目标：{other}\n", str(target)))


if __name__ == "__main__":
    unittest.main()
