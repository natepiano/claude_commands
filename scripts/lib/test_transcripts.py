"""Read plain and compressed transcripts through the same interface."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from scripts.lib.transcript_test_support import compressed_copy
from scripts.lib.transcripts import codex_transcript_homes, open_transcript, transcript_paths


class TranscriptTests(unittest.TestCase):
    def test_missing_transcript_raises_file_not_found_for_plain_and_compressed_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("missing.jsonl", "missing.jsonl.zst"):
                with self.subTest(name=name):
                    with self.assertRaises(FileNotFoundError):
                        with open_transcript(root / name):
                            self.fail("The reader opened a missing transcript.")

    def test_plain_and_compressed_streams_keep_bytes_offsets_and_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session.jsonl"
            content = b'{"message":"one"}\n{"message":"two"}\n'
            _ = path.write_bytes(content)
            for compressed in (False, True):
                with self.subTest(compressed=compressed):
                    source = compressed_copy(path) if compressed else path
                    self.assertEqual(transcript_paths(root), [source])
                    for name in {path, source}:
                        with open_transcript(name) as stream:
                            self.assertEqual(stream.read(3), content[:3])
                            _ = stream.seek(0)
                            self.assertEqual(list(stream), content.splitlines(keepends=True))
                            self.assertEqual(stream.tell(), len(content))
            user_home = root / "home"
            user_home.mkdir()
            numbered = user_home / ".codex2"
            numbered.mkdir()
            alias = user_home / ".codex7"
            alias.symlink_to(numbered, target_is_directory=True)
            different = root / "different-home"
            different.mkdir()
            linked = user_home / ".codex8"
            linked.symlink_to(different, target_is_directory=True)
            tenth = user_home / ".codex10"
            tenth.mkdir()
            (user_home / ".codex4").touch()
            (user_home / ".codex-cache").mkdir()
            extra = root / "staff-home"
            extra.mkdir()
            selected = root / "selected-home"
            selected.symlink_to(numbered, target_is_directory=True)
            default = user_home / ".codex"
            with patch.dict(os.environ, {"HOME": str(user_home), "CODEX_HOME": ""}):
                self.assertEqual(codex_transcript_homes(), [default, numbered, linked, tenth])
                self.assertEqual(codex_transcript_homes((extra, extra)), [extra, default, numbered, linked, tenth])
                with patch.dict(os.environ, {"CODEX_HOME": str(selected)}):
                    self.assertEqual(codex_transcript_homes((numbered, extra)), [selected, extra, default, linked, tenth])
