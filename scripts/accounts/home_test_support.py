"""Keep an accounts test's HOME and import-time registry paths together."""

import os
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from scripts.production.showrunner_registry_test_support import scratch_showrunner_registry


@contextmanager
def scratch_account_home(home: Path, environment: Mapping[str, str], *, clear: bool = True) -> Generator[None]:
    """Use the supplied scratch home for environment and registry lookups."""
    with (patch.dict(os.environ, {**environment, "HOME": str(home)}, clear=clear),
          scratch_showrunner_registry(home)):
        yield
