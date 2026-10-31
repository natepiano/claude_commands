"""One failed-stop injection shared by private unit-operation tests."""
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from unittest import mock

import rename_unit

from scripts.production.fake_unit_host import PaneShellFixture


@contextmanager
def failed_stop(pane: PaneShellFixture, stop: Callable[[], None]) -> Generator[None]:
    """Inject after the fake director exits; never signal a real process."""
    from scripts.shutdown import stop_resume
    from rename_state import RenameRefused

    def interrupt(*_arguments: str) -> None:
        pane.stopped()
        stop()
        raise RenameRefused("fixture failed stop after director exit")

    with mock.patch.object(stop_resume, "stop", side_effect=interrupt):
        yield


@dataclass
class PaneClosingOperation:
    """A generic unit stop closes its exact terminal after cleanup."""

    slug: str
    old: str
    new: str
    worktree_dir: Path
    account: str
    session_id: str
    pane_session: str
    pane_id: str
    terminal_id: str
    id_matches: bool = True
    stop_identity: rename_unit.StopIdentityEvidence = rename_unit.StopIdentityEvidence.RECORDED

    def worktree(self) -> Path:
        return self.worktree_dir
