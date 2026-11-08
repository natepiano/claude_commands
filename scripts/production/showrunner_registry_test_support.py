"""One scratch showrunner registry for tests whose code reaches `registered_showrunners`."""

import os
from contextlib import ExitStack, contextmanager
from collections.abc import Generator
from pathlib import Path
from typing import cast
from unittest.mock import patch

from scripts.production import showrunners


@contextmanager
def scratch_showrunner_registry(home: Path) -> Generator[None]:
    """Point the registry at folders under `home`."""
    with ExitStack() as stack:
        _ = cast(object, stack.enter_context(patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(home / "sessions")})))
        for field, folder in (("NOTIFIER_STATE_DIR", "notifier"),
                              ("SHOWRUNNER_STATE_DIR", "showrunner")):
            _ = stack.enter_context(patch.object(showrunners, field, home / folder))
        yield
