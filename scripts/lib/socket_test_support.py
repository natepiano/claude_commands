"""Private Unix socket paths whose length does not depend on TMPDIR."""
from __future__ import annotations

import socket
import tempfile
from pathlib import Path
from typing import override


def short_socket_directory() -> tempfile.TemporaryDirectory[str]:
    """Own a mode-0700 folder under /tmp; use its context or register cleanup."""
    return tempfile.TemporaryDirectory(prefix="test-sock-", dir="/tmp")


class ShortUnixSocket(socket.socket):
    """A socket that removes its private path and folder when closed."""

    def __init__(self) -> None:
        super().__init__(socket.AF_UNIX)
        self._directory: tempfile.TemporaryDirectory[str] = short_socket_directory()
        self.path: Path = Path(self._directory.name) / "s"

    @override
    def close(self) -> None:
        try:
            super().close()
        finally:
            self._directory.cleanup()

    def bind_socket(self) -> Path:
        self.bind(str(self.path))
        return self.path
