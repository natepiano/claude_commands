"""Give server records a disposable process that tests can stop."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
from typing import cast

from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@contextmanager
def owned_server_record(path: Path) -> Generator[subprocess.Popen[bytes]]:
    """Replace a fixture record's pid and collect its process after the test."""
    record = cast(dict[str, object], json.loads(path.read_text()))
    process = subprocess.Popen(
        ["codex", "-c", "import sys; sys.stdin.buffer.read()", "app-server", "--listen",
         f"ws://127.0.0.1:{record['port']}"], executable=sys.executable,
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        record["pid"] = process.pid
        write_text_atomic(path, json.dumps(record))
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
        if process.stdin is not None:
            process.stdin.close()
