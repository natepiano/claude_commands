"""Keep an accounts test's HOME and import-time registry paths together."""

import json
import os
import shutil
import tempfile
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from unittest.mock import patch

from scripts.accounts import accounts
from scripts.production.showrunner_registry_test_support import scratch_showrunner_registry
from scripts.whoami import note_file


def clear_notes_directory_cache() -> None:
    """Clear the notes folder when a test changes the configuration."""
    configured = cast(object, getattr(note_file, "_configured_notes_dir"))
    clear = cast(Callable[[], None], getattr(configured, "cache_clear"))
    clear()


def write_account_settings(
    path: Path, *, folders: tuple[str, ...] = (),
) -> None:
    """A complete scratch configuration, independent of the repository settings."""
    _ = path.write_text(
        f"[trust]\nfolders = {json.dumps(folders)}\n"
    )


@contextmanager
def scratch_accounts_config() -> Generator[Path]:
    """Use a scratch copy of the account configuration."""
    with tempfile.TemporaryDirectory(prefix="accounts-config-") as temporary:
        config = Path(temporary)
        _ = shutil.copyfile(Path(__file__).resolve().parents[2] / "config/accounts.conf", config / "accounts.conf")
        clear_notes_directory_cache()
        try:
            with patch.dict(os.environ, {"CONF_DIR": str(config)}):
                yield config
        finally:
            clear_notes_directory_cache()


@contextmanager
def scratch_account_home(home: Path, environment: Mapping[str, str], *, clear: bool = True) -> Generator[None]:
    """Use the supplied scratch home for environment and registry lookups."""
    with tempfile.TemporaryDirectory(prefix="accounts-settings-") as temporary:
        settings = Path(temporary) / "accounts.toml"
        write_account_settings(settings)
        with (scratch_accounts_config() as config,
              patch.object(accounts, "trust_settings_file", return_value=settings),
              patch.dict(os.environ, {**environment, "HOME": str(home), "CONF_DIR": str(config)}, clear=clear),
              scratch_showrunner_registry(home),
              patch.object(accounts, "WORKERS_RUN_ROOTS", (home / "worker-runs",))):
            yield
