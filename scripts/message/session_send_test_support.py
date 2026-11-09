"""Prevent tests from starting the real session sender."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
import unittest


@dataclass(frozen=True)
class SenderGuardNotInstalled:
    """The module has no sender refusal marker."""


@dataclass(frozen=True)
class SenderRefusalMarker:
    path: Path


refusal_marker: SenderGuardNotInstalled | SenderRefusalMarker = SenderGuardNotInstalled()
real_sender_started = False


def real_sender_was_started() -> bool:
    """Report a refusal before or after the module cleanup."""
    return real_sender_started or (
        isinstance(refusal_marker, SenderRefusalMarker) and refusal_marker.path.is_file()
    )


def forbid_real_sender() -> None:
    """Set one inherited marker path before any test can start the sender."""
    global refusal_marker
    if isinstance(refusal_marker, SenderGuardNotInstalled):
        previous_marker = os.environ.get("TEST_SENDER_GUARD")
        marker = previous_marker
        if not marker:
            directory = unittest.enterModuleContext(tempfile.TemporaryDirectory(prefix="sender-guard-"))
            marker = str(Path(directory) / "refused")
            os.environ["TEST_SENDER_GUARD"] = marker
        installed_marker = SenderRefusalMarker(Path(marker))
        refusal_marker = installed_marker

        def finish_guard() -> None:
            global refusal_marker, real_sender_started
            real_sender_started = real_sender_was_started()
            if previous_marker is None:
                _ = os.environ.pop("TEST_SENDER_GUARD", None)
            else:
                os.environ["TEST_SENDER_GUARD"] = previous_marker
            refusal_marker = SenderGuardNotInstalled()

        # Module cleanups run in reverse order. Record the result before removing the folder.
        unittest.addModuleCleanup(finish_guard)
