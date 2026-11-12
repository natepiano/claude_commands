"""Open plain and zstd transcripts through one seekable binary stream."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
import subprocess
import tempfile
from typing import BinaryIO


def uncompressed_path(path: Path) -> Path:
    return path.with_suffix("") if path.suffix == ".zst" else path


def transcript_paths(root: Path, pattern: str = "**/*.jsonl") -> list[Path]:
    """Find each logical transcript once, with the plain file preferred."""
    found: dict[Path, Path] = {}
    for suffix in (".zst", ""):
        for path in root.glob(pattern + suffix):
            found[uncompressed_path(path.resolve())] = path
    return sorted(found.values(), key=str)


@contextmanager
def open_transcript(path: Path) -> Generator[BinaryIO]:
    """Keep decompression outside memory and permit existing readers to seek."""
    source = path
    if source.suffix != ".zst" and not source.exists():
        compressed = source.with_suffix(source.suffix + ".zst")
        if compressed.exists():
            source = compressed
    if source.suffix != ".zst":
        with source.open("rb") as stream:
            yield stream
        return
    with tempfile.TemporaryFile() as stream:
        try:
            _ = subprocess.run(["zstd", "-q", "-d", "-c", "--", str(source)],
                               stdout=stream, stderr=subprocess.PIPE, check=True)
        except subprocess.CalledProcessError as error:
            if not source.exists():
                raise FileNotFoundError(source) from error
            raise
        _ = stream.seek(0)
        yield stream


def compress_transcript(path: Path, output: BinaryIO) -> int:
    """Write zstd data and return its size without removing the transcript."""
    _ = subprocess.run(["zstd", "-q", "-c", "--", str(path)],
                       stdout=output, stderr=subprocess.PIPE, check=True)
    return output.tell()
