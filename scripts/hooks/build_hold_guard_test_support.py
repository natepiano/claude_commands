"""Seeded private hold state and the real guard entry point without process startup."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
from typing import TYPE_CHECKING, ClassVar, cast, override
from unittest import mock

from scripts.build_hold.test_hold_applies import TemporaryHoldTest

if TYPE_CHECKING:
    from ..build_hold import build_hold, hold_applies as hold_applies
else:
    import build_hold
    import hold_applies as hold_applies

HOOK = Path(__file__).with_name("pre-tool-use-build-hold-guard.py")
with mock.patch.object(sys, "path", sys.path.copy()):
    _main = cast(Callable[[], None], runpy.run_path(str(HOOK))["main"])


def invoke_hook(raw: str) -> str:
    """Run main, restoring the environment and import path as a child process would."""
    output, errors = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ), mock.patch.object(sys, "path", sys.path.copy()), \
            mock.patch.object(sys, "stdin", io.StringIO(raw)), \
            redirect_stdout(output), redirect_stderr(errors):
        _main()
    if errors.getvalue():
        raise AssertionError(errors.getvalue())
    return output.getvalue()


class TemporaryGuardTest(TemporaryHoldTest):
    seed: ClassVar[Path]
    case: ClassVar[Path]
    environment: ClassVar[dict[str, str]]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        folder = tempfile.TemporaryDirectory(dir=HOOK.resolve().parents[2], prefix="guard-test-")
        cls.addClassCleanup(folder.cleanup)
        cls.seed, cls.case = Path(folder.name) / "seed", Path(folder.name) / "case"
        # Capture the existing fixture's environment contract rather than duplicating it.
        builder = TemporaryHoldTest()
        before = dict(os.environ)
        try:
            builder.setUp()
            _ = shutil.copytree(builder.root, cls.seed)
            cls.environment = {key: value.replace(str(builder.root), str(cls.case))
                               for key, value in os.environ.items() if before.get(key) != value}
        finally:
            builder.doCleanups()

    @override
    def setUp(self) -> None:
        _ = shutil.copytree(self.seed, self.case)
        self.addCleanup(shutil.rmtree, self.case)
        self.root: Path = self.case
        self.holders: Path = self.case / "holders"
        self.enterContext(mock.patch.dict(os.environ, self.environment))

    @override
    def releasing(self, *keys: str) -> None:
        super().releasing(*keys)
        # A release removes its holder file before it sends any notice.
        with mock.patch.object(build_hold, "start_recorder"), mock.patch.object(build_hold, "notify_release", return_value=""):
            for key in keys:
                _ = build_hold.release_cycle(self.holders, key)
