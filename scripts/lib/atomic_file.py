"""Replace a text file without exposing a partly written file to readers."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path


def replace_text(path: Path, text: str) -> None:
    """Write UTF-8 text beside its destination, then replace it in one operation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    name = re.compile(rf"\.{re.escape(path.name)}\.(\d+)\.tmp")
    for sibling in path.parent.iterdir():
        match = name.fullmatch(sibling.name)
        if match is None:
            continue
        try:
            os.kill(int(match.group(1)), 0)
        except ProcessLookupError:
            sibling.unlink(missing_ok=True)
        except PermissionError:
            continue
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
        with temporary.open("w", encoding="utf-8") as output:
            _ = output.write(text)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
