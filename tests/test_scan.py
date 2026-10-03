import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workbuddy_bridge_intelmac.manager import _scan


class ScanTests(unittest.TestCase):
    def test_unreadable_directory_marks_manifest_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            def walk(root, *, followlinks, onerror):
                onerror(PermissionError("private"))
                return iter([])
            with patch("workbuddy_bridge_intelmac.manager.os.walk", side_effect=walk):
                files, limited = _scan(Path(directory))
            self.assertEqual(files, {})
            self.assertTrue(limited)

    def test_disappearing_file_marks_manifest_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("workbuddy_bridge_intelmac.manager.os.walk", return_value=iter([(directory, [], ["gone.txt"])])):
                files, limited = _scan(Path(directory))
            self.assertEqual(files, {})
            self.assertTrue(limited)
