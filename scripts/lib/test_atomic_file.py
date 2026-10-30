"""Atomic text replacement against private files and interrupted writes."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from scripts.lib import atomic_file


class AtomicFileTest(unittest.TestCase):
    def test_failed_replacement_keeps_original_bytes_and_removes_its_temporary_file(self) -> None:
        for interruption in (OSError("replace failed"), KeyboardInterrupt()):
            with self.subTest(interruption=type(interruption).__name__), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "questions.json"
                original = b'["earlier question"]\n'
                _ = path.write_bytes(original)

                def fail_replace(source: Path, destination: Path) -> None:
                    self.assertEqual(Path(destination), path)
                    self.assertEqual(Path(source), path.with_name(f".{path.name}.{os.getpid()}.tmp"))
                    self.assertEqual(Path(source).read_text(), '["new question"]\n')
                    self.assertEqual(list(path.parent.glob("*.json")), [path])
                    raise interruption

                with patch.object(os, "replace", side_effect=fail_replace), self.assertRaises(type(interruption)):
                    atomic_file.replace_text(path, '["new question"]\n')
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(path.parent.iterdir()), [path])

    def test_next_write_removes_dead_process_temporary_files_and_keeps_live_ones(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "change-review - demo.md"
            live_pid = os.getppid()
            dead_pid = os.getpid() + 1000000
            live = path.with_name(f".{path.name}.{live_pid}.tmp")
            dead = path.with_name(f".{path.name}.{dead_pid}.tmp")
            unrelated = path.with_name(f".another.md.{dead_pid}.tmp")
            for file in (live, dead, unrelated):
                _ = file.write_text("unfinished")
            real_kill = os.kill

            def process_exists(pid: int, signal: int) -> None:
                if pid == dead_pid:
                    raise ProcessLookupError(pid)
                real_kill(pid, signal)

            with patch.object(os, "kill", side_effect=process_exists):
                atomic_file.replace_text(path, "Reviewed\n")
            self.assertEqual(path.read_text(), "Reviewed\n")
            self.assertFalse(dead.exists())
            self.assertEqual(live.read_text(), "unfinished")
            self.assertEqual(unrelated.read_text(), "unfinished")

    def test_replacement_preserves_existing_mode(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "list.md"
            _ = path.write_text("before")
            path.chmod(0o640)
            atomic_file.replace_text(path, "after — updated\n")
            self.assertEqual(path.read_bytes(), "after — updated\n".encode())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_new_file_is_synced_before_replacement_and_has_default_mode(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "new.json"
            content = '["question — choices"]\n'
            synced: list[int] = []
            real_fsync, real_replace = os.fsync, os.replace

            def sync(descriptor: int) -> None:
                self.assertEqual(os.fstat(descriptor).st_size, len(content.encode()))
                real_fsync(descriptor)
                synced.append(descriptor)

            def replace(source: Path, destination: Path) -> None:
                self.assertEqual(len(synced), 1)
                self.assertFalse(path.exists())
                real_replace(source, destination)

            with patch.object(os, "fsync", side_effect=sync), patch.object(os, "replace", side_effect=replace):
                atomic_file.replace_text(path, content)
            self.assertEqual(path.read_bytes(), content.encode())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)


if __name__ == "__main__":
    _ = unittest.main()
