from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from tools.import_rizline_save import import_save


class RizlineSaveImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_imports_a_valid_export_with_restricted_permissions(self) -> None:
        source = self.root / "gameData.json"
        source.write_text(json.dumps({"username": "Alice", "myBest": [], "levelsRks": []}), encoding="utf-8")

        target = import_save(source, self.root / "saves", "alice")

        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["username"], "Alice")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_does_not_replace_an_existing_save_without_explicit_permission(self) -> None:
        source = self.root / "gameData.json"
        source.write_text(json.dumps({"myBest": []}), encoding="utf-8")
        target = import_save(source, self.root / "saves", "alice")

        with self.assertRaisesRegex(ValueError, "--replace"):
            import_save(source, self.root / "saves", "alice")
        self.assertTrue(target.is_file())
