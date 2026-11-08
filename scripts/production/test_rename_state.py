"""Write the rename journal atomically."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import override


from scripts.production import rename_state
from scripts.lib import atomic_file


class RenameStateTests(unittest.TestCase):
    root: Path = Path()
    scratch: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()

    def write_json(self, relative: str, value: object) -> Path:
        path = self.scratch / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        return path

    def test_journal_is_synced_before_replacement_and_its_folder_afterward(self) -> None:
        path = self.write_json("rename.json", {"step": 1})
        before = path.read_bytes()
        events: list[str] = []
        sync = os.fsync
        replace = os.replace

        def synced(descriptor: int) -> None:
            events.append("folder" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
            sync(descriptor)

        def replaced(source: str | Path, destination: str | Path) -> None:
            self.assertEqual(path.read_bytes(), before)
            self.assertIn("file", events)
            events.append("replace")
            replace(source, destination)

        with unittest.mock.patch.object(atomic_file, "write_text_atomic", wraps=atomic_file.write_text_atomic) as writer, \
                unittest.mock.patch.object(os, "fsync", side_effect=synced), \
                unittest.mock.patch.object(os, "replace", side_effect=replaced):
            rename_state.write_json(path, {"step": 2})
        writer.assert_called_once_with(path, '{\n  "step": 2\n}\n')
        self.assertEqual(events, ["file", "replace", "folder"])
        self.assertEqual(json.loads(path.read_text()), {"step": 2})

    def test_failed_journal_sync_keeps_the_previous_complete_request(self) -> None:
        path = self.write_json("rename.json", {"step": 1})
        before = path.read_bytes()
        with unittest.mock.patch.object(os, "fsync", side_effect=OSError("sync failed")), \
                self.assertRaisesRegex(OSError, "sync failed"):
            rename_state.write_json(path, {"step": 2})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])


if __name__ == "__main__":
    _ = unittest.main()
