"""One scratch showrunner registry for tests whose code reaches `registered_showrunners`."""

import sys
from contextlib import ExitStack, contextmanager
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import showrunners


@contextmanager
def scratch_showrunner_registry(home: Path) -> Generator[None]:
    """Point the registry at folders under `home`; its paths are fixed at import."""
    with ExitStack() as stack:
        for field, folder in (("NOTIFIER_STATE_DIR", "notifier"), ("SESSIONS_DIR", "sessions"),
                              ("SHOWRUNNER_STATE_DIR", "showrunner")):
            _ = stack.enter_context(patch.object(showrunners, field, home / folder))
        yield
