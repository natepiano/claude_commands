"""Private verify inputs and the real embedded dev-feature parser."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import re
import subprocess
import sys
from unittest.mock import patch
from typing import override

from scripts.direct.verify_examples_test_support import SeededExampleWorkspaceFixture


VERIFY = Path(__file__).with_name("verify.sh")
DEV_LIST = '["bevy/dynamic_linking", "engine/dev", "bevy/dynamic_linking"]'
DEV_ARGUMENTS = "bevy/dynamic_linking,engine/dev"


def add_dev_features(root: Path, value: str = DEV_LIST) -> None:
    with (root / "Cargo.toml").open("a") as manifest:
        _ = manifest.write(f"\n[workspace.metadata.dev]\nfeatures = {value}\n")


def isolate_verify_inputs(environment: dict[str, str], root: Path) -> None:
    """Keep interpreter discovery and host pressure outside feature assertions."""
    cache = Path(environment["XDG_CACHE_HOME"]) / "claude" / "python3-3.11"
    cache.parent.mkdir(parents=True, exist_ok=True)
    _ = cache.write_text(sys.executable + "\n", encoding="utf-8")
    _ = environment.pop("CLAUDE_PYTHON", None)
    pressure = root / "dev-feature-pressure"
    _ = pressure.write_text("full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    cgroup = root / "dev-feature-cgroup"
    cgroup.mkdir(exist_ok=True)
    environment.update({
        "BUILDLOG_MEMORY_PRESSURE": str(pressure),
        "BUILDLOG_BUILDS_CGROUP": str(cgroup),
        "BUILDLOG_CI_CGROUP": str(cgroup),
        "BUILDLOG_ZRAM": str(root / "dev-feature-zram"),
    })


def parse_dev_features(manifest: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Execute the real parser with the CLI's private tool and file boundaries."""
    match = re.search(r"^DEV_FEATURES_PY='\n(.*?)\n'$", VERIFY.read_text(), re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError("verify.sh no longer defines DEV_FEATURES_PY")
    source = match[1]
    stdout, stderr = io.StringIO(), io.StringIO()
    status = 0
    directory = Path.cwd()
    try:
        os.chdir(manifest.parent)
        with (patch.dict(os.environ, environment, clear=True),
              patch.object(sys, "argv", ["-c", str(manifest)]),
              redirect_stdout(stdout), redirect_stderr(stderr)):
            try:
                exec(compile(source, str(VERIFY) + ":DEV_FEATURES_PY", "exec"), {})
            except SystemExit as stopped:
                if not isinstance(stopped.code, int):
                    raise AssertionError("dev-feature parser returned a nonnumeric status") from stopped
                status = stopped.code
    finally:
        os.chdir(directory)
    return subprocess.CompletedProcess([sys.executable, "-c", source, str(manifest)],
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
