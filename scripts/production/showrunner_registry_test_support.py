"""One scratch showrunner registry for tests whose code reaches `registered_showrunners`."""

import sys
from importlib import import_module
from contextlib import ExitStack, contextmanager
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch


@contextmanager
def scratch_showrunner_registry(home: Path) -> Generator[None]:
    """Point every loaded copy of the registry at folders under `home`; its paths are fixed at import."""
    _ = import_module("scripts.production.showrunners")
    with ExitStack() as stack:
        for name in ("showrunners", "scripts.production.showrunners"):
            registry = sys.modules.get(name)
            if registry is None:
                continue
            for field, folder in (("NOTIFIER_STATE_DIR", "notifier"), ("SESSIONS_DIR", "sessions"),
                                  ("SHOWRUNNER_STATE_DIR", "showrunner")):
                _ = stack.enter_context(patch.object(registry, field, home / folder))
        yield
