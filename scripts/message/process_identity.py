"""Read the operating system's process start identity for session and relay lifetimes."""

import os
import subprocess
import sys
from pathlib import Path


def process_start(pid: int) -> str | None:
    """The process identity stored in session records, in this platform's native units."""
    if sys.platform == "darwin":
        # Claude writes procStart under this locale and zone; other text never matches.
        try:
            result = subprocess.run(
                ("ps", "-o", "lstart=", "-p", str(pid)),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        started = result.stdout.strip()
        return started if result.returncode == 0 and started else None
    try:
        # The kernel can truncate the process name inside a UTF-8 character.
        _, separator, fields = Path(f"/proc/{pid}/stat").read_bytes().rpartition(b")")
        if not separator:
            return None
        started = fields.split()[19].decode("ascii")
        return started if started.isdigit() else None
    except (OSError, IndexError, UnicodeDecodeError):
        return None
