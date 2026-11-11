"""Compress scratch transcripts through the production compressor."""
from pathlib import Path
import os
import stat

from scripts.lib.transcripts import compress_transcript


def compressed_copy(path: Path) -> Path:
    saved = path.stat()
    destination = path.with_suffix(path.suffix + ".zst")
    with destination.open("wb") as output:
        _ = compress_transcript(path, output)
        os.fchmod(output.fileno(), stat.S_IMODE(saved.st_mode))
    os.utime(destination, ns=(saved.st_atime_ns, saved.st_mtime_ns))
    path.unlink()
    return destination
