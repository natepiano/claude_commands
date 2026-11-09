"""Prevent tests from starting the real session sender."""
from __future__ import annotations

import atexit
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import tempfile


@dataclass(frozen=True)
class SenderGuardNotInstalled:
    """The module has no sender refusal marker."""


@dataclass(frozen=True)
class SenderRefusalMarker:
    path: Path


refusal_marker: SenderGuardNotInstalled | SenderRefusalMarker = SenderGuardNotInstalled()


def forbid_real_sender() -> None:
    """Set one inherited marker path before any test can start the sender."""
    global refusal_marker
    if isinstance(refusal_marker, SenderGuardNotInstalled):
        marker = os.environ.get("TEST_SENDER_GUARD")
        if not marker:
            directory = tempfile.mkdtemp(prefix="sender-guard-")
            marker = str(Path(directory) / "refused")
            os.environ["TEST_SENDER_GUARD"] = marker
            _ = atexit.register(shutil.rmtree, directory)
        refusal_marker = SenderRefusalMarker(Path(marker))
