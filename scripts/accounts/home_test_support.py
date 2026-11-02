"""Keep an accounts test's HOME and import-time registry paths together."""

import json
import os
import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from scripts.accounts import accounts
from scripts.production.showrunner_registry_test_support import scratch_showrunner_registry


def write_account_settings(
    path: Path, *, folders: tuple[str, ...] = (),
    retries: tuple[float, ...] = (0.2, 0.4), stale_seconds: float = 10.0,
) -> None:
    """A complete scratch configuration, independent of the repository settings."""
    _ = path.write_text(
        f"[trust]\nfolders = {json.dumps(folders)}\n"
        + f"[claude_config_lock]\nretries = {json.dumps(retries)}\nstale_seconds = {stale_seconds}\n"
    )


@contextmanager
def scratch_account_home(home: Path, environment: Mapping[str, str], *, clear: bool = True) -> Generator[None]:
    """Use the supplied scratch home for environment and registry lookups."""
    with tempfile.TemporaryDirectory(prefix="accounts-settings-") as temporary:
        settings = Path(temporary) / "accounts.toml"
        write_account_settings(settings)
        with (patch.object(accounts, "trust_settings_file", return_value=settings),
              patch.dict(os.environ, {**environment, "HOME": str(home)}, clear=clear),
              scratch_showrunner_registry(home),
              patch.object(accounts, "WORKERS_RUN_ROOTS", (home / "worker-runs",))):
            yield
