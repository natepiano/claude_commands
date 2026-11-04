"""Private Unix socket paths whose length does not depend on TMPDIR."""
from __future__ import annotations

import socket
import os
from contextlib import ExitStack
from types import TracebackType

from scripts.lib import build_folders
from scripts.lint.memory_test_support import config_environment
from pathlib import Path
from typing import override


class ShortSocketDirectory:
    """Keep a short socket folder under the configured system root."""

    def __init__(self, prefix: str) -> None:
        if configuration := os.environ.get("CONF_DIR"):
            _ = config_environment(Path(configuration))
        self._lifetime: ExitStack = ExitStack()
        folder = self._lifetime.enter_context(build_folders.temporary_build_folder(
            build_folders.UniqueBuildFolder(prefix, build_folders.SystemTemporaryParent())))
        self.name: str = str(folder)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        self._lifetime.close()


def short_socket_directory(prefix: str = "test-sock-") -> ShortSocketDirectory:
    """Own a private socket folder; use its context or register cleanup."""
    return ShortSocketDirectory(prefix)


class ShortUnixSocket(socket.socket):
    """A socket that removes its private path and folder when closed."""

    def __init__(self) -> None:
        super().__init__(socket.AF_UNIX)
        self._directory: ShortSocketDirectory = short_socket_directory()
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
