"""One failed-stop injection shared by private unit-operation tests."""
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from unittest import mock

from scripts.production.fake_unit_host import PaneShellFixture


@contextmanager
def failed_stop(pane: PaneShellFixture, stop: Callable[[], None]) -> Generator[None]:
    """Inject after the fake director exits; never signal a real process."""
    import rename_unit
    from rename_state import RenameRefused

    def interrupt(*_arguments: str) -> None:
        pane.stopped()
        stop()
        raise RenameRefused("fixture failed stop after director exit")

    with mock.patch.object(rename_unit, "run_shutdown", side_effect=interrupt):
        yield
