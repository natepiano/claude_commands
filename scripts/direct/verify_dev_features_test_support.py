"""Private verify inputs and the real dev-feature parser."""

from __future__ import annotations

from scripts.lint.memory_test_support import write_pressure_reading

from scripts.lib.py_floor_test_support import seed_interpreter_cache

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
from typing import override

from scripts.direct import dev_features
from scripts.direct.verify_examples_test_support import SeededExampleWorkspaceFixture


VERIFY = Path(__file__).with_name("verify.sh")
DEV_LIST = '["bevy/dynamic_linking", "engine/dev", "bevy/dynamic_linking"]'
DEV_ARGUMENTS = "bevy/dynamic_linking,engine/dev"


def add_dev_features(root: Path, value: str = DEV_LIST) -> None:
    with (root / "Cargo.toml").open("a") as manifest:
        _ = manifest.write(f"\n[workspace.metadata.dev]\nfeatures = {value}\n")


def isolate_verify_inputs(environment: dict[str, str], root: Path) -> None:
    """Keep interpreter discovery and host pressure outside feature assertions."""
    seed_interpreter_cache(Path(environment["XDG_CACHE_HOME"]))
    _ = environment.pop("CLAUDE_PYTHON", None)
    pressure = root / "dev-feature-pressure"
    write_pressure_reading(pressure)
    cgroup = root / "dev-feature-cgroup"
    cgroup.mkdir(exist_ok=True)
    environment.update({
        "BUILDLOG_MEMORY_PRESSURE": str(pressure),
        "BUILDLOG_BUILDS_CGROUP": str(cgroup),
        "BUILDLOG_CI_CGROUP": str(cgroup),
        "BUILDLOG_ZRAM": str(root / "dev-feature-zram"),
    })


def parse_dev_features(manifest: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the real parser in this process with the CLI's environment and folder."""
    stdout, stderr = io.StringIO(), io.StringIO()
    directory = Path.cwd()
    try:
        os.chdir(manifest.parent)
        with (patch.dict(os.environ, environment, clear=True),
              redirect_stdout(stdout), redirect_stderr(stderr)):
            status = dev_features.main([str(manifest)])
    finally:
        os.chdir(directory)
    return subprocess.CompletedProcess([sys.executable, str(VERIFY.with_name("dev_features.py")), str(manifest)],
                                       status, stdout.getvalue(), stderr.getvalue())


class DevFeatureFixture(SeededExampleWorkspaceFixture):
    """Seed the private workspace once; retain real Bash and Cargo stub calls."""

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        builder = cls()
        _ = builder.package(root=cls.fixture_seed)
        cls.fixture_environment["NEXTEST_TEST_THREADS"] = "2"

    @override
    def setUp(self) -> None:
        super().setUp()
        isolate_verify_inputs(self.environment, self.root)

    def clear_calls(self) -> None:
        self.calls.unlink(missing_ok=True)
