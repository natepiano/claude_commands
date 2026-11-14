"""Give test runs maker folders and recover free folders before the next run."""
from __future__ import annotations

import atexit
from collections.abc import Generator
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile

from scripts.lib import build_folders


RUN_FOLDER_ENV = "DEVTOOLS_TEST_RUN_FOLDER"


@contextmanager
def test_run_folder() -> Generator[Path]:
    """Set TMPDIR for one run and restore the caller's temporary parent afterward."""
    previous = {name: os.environ.get(name) for name in ("TMPDIR", RUN_FOLDER_ENV)}
    old_tempdir = tempfile.tempdir
    inherited = build_folders.inherited_temporary_parent().resolve()
    parent = (inherited.parent if str(inherited) == previous[RUN_FOLDER_ENV]
              else inherited / "test-runs")
    parent.mkdir(parents=True, exist_ok=True)
    build_folders.sweep_temporary_build_folders(parent)
    request = build_folders.UniqueBuildFolder("test-run-", build_folders.ExplicitBuildParent(parent))
    with build_folders.temporary_build_folder(request) as path:
        os.environ["TMPDIR"] = str(path)
        os.environ[RUN_FOLDER_ENV] = str(path)
        tempfile.tempdir = None
        try:
            yield path
        finally:
            for name, value in previous.items():
                if value is None:
                    _ = os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            tempfile.tempdir = old_tempdir


def start_test_run() -> None:
    """Keep the bootstrap allocation until normal interpreter exit."""
    owner = os.getpid()
    lifetime = test_run_folder()
    _ = lifetime.__enter__()

    def finish() -> None:
        if os.getpid() == owner:
            _ = lifetime.__exit__(None, None, None)

    _ = atexit.register(finish)
