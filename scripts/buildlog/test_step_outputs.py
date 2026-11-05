"""Read distinct step output facts from private indexes."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, final, override

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_index import use_test_log

use_test_log()

if TYPE_CHECKING:
    from .step_outputs import (
        AbsoluteRecordedRoot, RecordedRoot, RecordedStepOutputs, RecordedRootsUnavailable,
        RecordedStepOutput, RelativeRecordedRoot, TargetFolderOverride, UnknownRecordedRoot,
        recorded_output_roots,
    )
    from .step_outputs_test_support import create_step_index, record_root_attempt
else:
    from scripts.buildlog.step_outputs import (
        AbsoluteRecordedRoot, RecordedRoot, RecordedStepOutputs, RecordedRootsUnavailable,
        RecordedStepOutput, RelativeRecordedRoot, TargetFolderOverride, UnknownRecordedRoot,
        recorded_output_roots,
    )
    from scripts.buildlog.step_outputs_test_support import create_step_index, record_root_attempt


UNKNOWN_ROOT = UnknownRecordedRoot()


def output_row(worktree: RecordedRoot, target_directory: RecordedRoot, build_directory: RecordedRoot, *,
               cwd: RecordedRoot = UNKNOWN_ROOT, target_folder: RecordedRoot = UNKNOWN_ROOT,
               target_folder_override: TargetFolderOverride = TargetFolderOverride.ABSENT) -> RecordedStepOutput:
    return RecordedStepOutput(worktree, cwd, target_directory, build_directory, target_folder, target_folder_override)


@final
class RecordedOutputRootsTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.index = self.root / "steps?#.sqlite"

    @override
    def setUp(self) -> None:
        self.addCleanup(self.directory.cleanup)
        create_step_index(self.index)

    def rows(self) -> tuple[RecordedStepOutput, ...]:
        result = recorded_output_roots(self.index, "natedev")
        self.assertIsInstance(result, RecordedStepOutputs)
        assert isinstance(result, RecordedStepOutputs)
        return result.rows

    def test_every_step_kind_and_attempt_retains_its_workspace_and_root_pair(self) -> None:
        workspace = self.root / "checkout"
        expected: list[RecordedStepOutput] = []
        for number, step in enumerate(("build", "check", "clippy", "mend", "doc", "nextest", "test", "t")):
            target, build = self.root / f"target-{number}", self.root / f"build-{number}"
            record_root_attempt(self.index, str(workspace), str(target), str(build), step=step, call_id="same-call")
            expected.append(output_row(
                worktree=AbsoluteRecordedRoot(workspace),
                target_directory=AbsoluteRecordedRoot(target), build_directory=AbsoluteRecordedRoot(build)))
        record_root_attempt(self.index, str(workspace), "/wrong-host/target", "/wrong-host/build", host="other")
        fallback = self.root / "cwd"
        record_root_attempt(self.index, "unknown", str(fallback / "target"), str(fallback / "target"),
                            cwd=str(fallback), call_id="")
        expected.append(output_row(
            worktree=UnknownRecordedRoot(), cwd=AbsoluteRecordedRoot(fallback),
            target_directory=AbsoluteRecordedRoot(fallback / "target"),
            build_directory=AbsoluteRecordedRoot(fallback / "target")))
        before = self.index.read_bytes(), self.index.stat().st_mtime_ns
        self.assertCountEqual(self.rows(), expected)
        self.assertEqual((self.index.read_bytes(), self.index.stat().st_mtime_ns), before)

    def test_each_target_override_form_sets_the_override_marker(self) -> None:
        workspace, target = self.root / "workspace", self.root / "default-target"
        valid = self.root / "valid"
        record_root_attempt(self.index, str(valid), str(valid / "target"), str(valid / "target"))
        arguments = (["cargo", "build", "--target-dir", str(target)],
                     ["cargo", "build", f"--target-dir={target}"],
                     ["cargo", "build", "--config", f'build.target-dir="{target}"'],
                     ["cargo", "build", f'--config=build.target-dir="{target}"'])
        for argv in arguments:
            record_root_attempt(self.index, str(workspace), str(target), str(target), argv=argv)
        different = self.root / "different-target"
        record_root_attempt(self.index, str(workspace), str(target), str(target), target_folder=str(different))
        self.assertCountEqual(self.rows(), (
            output_row(
                worktree=AbsoluteRecordedRoot(valid),
                target_directory=AbsoluteRecordedRoot(valid / "target"),
                build_directory=AbsoluteRecordedRoot(valid / "target")),
            output_row(
                worktree=AbsoluteRecordedRoot(workspace),
                target_directory=AbsoluteRecordedRoot(target), build_directory=AbsoluteRecordedRoot(target),
                target_folder_override=TargetFolderOverride.PRESENT),
            output_row(
                worktree=AbsoluteRecordedRoot(workspace),
                target_directory=AbsoluteRecordedRoot(target), build_directory=AbsoluteRecordedRoot(target),
                target_folder=AbsoluteRecordedRoot(different)),
        ))

    def test_unknown_and_relative_roots_have_distinct_states(self) -> None:
        workspace = self.root / "workspace"
        fallback = self.root / "fallback"
        record_root_attempt(self.index, str(workspace), "unknown", "relative/build")
        record_root_attempt(self.index, "", "target", "unknown", cwd=str(fallback))
        self.assertCountEqual(self.rows(), (
            output_row(
                worktree=AbsoluteRecordedRoot(workspace),
                target_directory=UnknownRecordedRoot(), build_directory=RelativeRecordedRoot()),
            output_row(
                worktree=UnknownRecordedRoot(), cwd=AbsoluteRecordedRoot(fallback),
                target_directory=RelativeRecordedRoot(), build_directory=UnknownRecordedRoot()),
        ))

    def test_unavailable_index_has_a_named_state_and_creates_no_index(self) -> None:
        missing = self.root / "missing.sqlite"
        self.assertIsInstance(recorded_output_roots(missing, "natedev"), RecordedRootsUnavailable)
        self.assertFalse(missing.exists())
        _ = self.index.write_text("broken index")
        self.assertIsInstance(recorded_output_roots(self.index, "natedev"), RecordedRootsUnavailable)

    def test_repeated_rows_and_argv_only_changes_return_one_distinct_fact(self) -> None:
        workspace, cwd = self.root / "workspace", self.root / "workspace" / "member"
        target, build = self.root / "target", self.root / "build"
        for number, argv in enumerate(((), (), ("cargo", "build", "--release"), ("cargo", "check"))):
            record_root_attempt(self.index, str(workspace), str(target), str(build), cwd=str(cwd),
                                argv=argv, step="check" if number else "build", call_id=str(number))
        record_root_attempt(self.index, str(workspace), str(target), str(build), cwd=str(cwd),
                            argv=("cargo", "build", "--target-dir", str(self.root / "override")))
        self.assertCountEqual(self.rows(), [output_row(
            worktree=AbsoluteRecordedRoot(workspace), cwd=AbsoluteRecordedRoot(cwd),
            target_directory=AbsoluteRecordedRoot(target), build_directory=AbsoluteRecordedRoot(build),
            target_folder_override=marker)
            for marker in (TargetFolderOverride.ABSENT, TargetFolderOverride.PRESENT)])


if __name__ == "__main__":
    _ = unittest.main()
