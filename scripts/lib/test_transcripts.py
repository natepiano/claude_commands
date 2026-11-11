"""Read plain and compressed transcripts through the same interface."""
from pathlib import Path
import tempfile
import unittest

from scripts.lib.transcript_test_support import compressed_copy
from scripts.lib.transcripts import open_transcript, transcript_paths


class TranscriptTests(unittest.TestCase):
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
