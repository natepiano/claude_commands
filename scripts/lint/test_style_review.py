"""Style inputs come from one checked range; read-only consumers write no state."""
from __future__ import annotations

import os
import json
import threading
import fcntl
from concurrent.futures import ThreadPoolExecutor
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from scripts.agents import worker_lifecycle
from scripts.agents.worker_lifecycle import LockFileDescriptor, NO_LOCK_DEADLINE
from scripts.message import sessions
from scripts.lib.atomic_file import write_text_atomic
from unittest import mock

from scripts.lint import style_review as style
from scripts.lib.git_test_support import commit_repository
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


def file_snapshot(home: Path) -> dict[str, bytes]:
    return {str(path.relative_to(home)): path.read_bytes() for path in home.rglob("*") if path.is_file()}


def no_temp_environment(home: Path) -> dict[str, str]:
    """A regular file blocks temporary-directory creation, including under root."""
    home.mkdir(parents=True, exist_ok=True)
    blocked = home / "not-a-directory"
    _ = blocked.write_text("temporary files unavailable\n")
    return {**os.environ, "HOME": str(home), "TMPDIR": str(blocked),
            "TMPPREFIX": str(blocked / "zsh"), "XDG_CACHE_HOME": str(home / "cache"),
            "CLAUDE_PYTHON": "", "PYTHONDONTWRITEBYTECODE": "1"}


class PreparationTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    output: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.output = self.root / "input"
        _ = subprocess.run(["git", "init", "-q", str(self.repo)], check=True, capture_output=True)
        _ = (self.repo / "note.txt").write_text("old\n")
        _ = self.commit("baseline")
        config = self.root / "lint.conf"
        _ = config.write_text("style_review=on\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"LINT_CONFIG_FILE": str(config), "CONF_DIR": str(Path(config).parent)})))

    def commit(self, message: str) -> str:
        return commit_repository(self.repo, message)

    def style_start(self, name: str = "dispatch-1") -> Path:
        _ = (self.repo / "note.txt").write_text("new\n")
        prompt = self.root / "prompt.md"
        _ = prompt.write_text("Review the change.\n")
        return style.start(self.repo, "HEAD", self.root / "phase", prompt, name)

    def test_live_dispatch_refuses_a_second_style_owner_without_changing_artifacts(self) -> None:
        first = self.style_start()
        before = file_snapshot(first.parent.parent)
        with self.assertRaisesRegex(style.StyleReviewBusy, "in flight: dispatch-1"):
            _ = self.style_start("dispatch-101")
        self.assertEqual(file_snapshot(first.parent.parent), before)
        result = self.root / "reply"
        _ = result.write_text(f"{style.BEGIN}\nStyle review: Pass\n{style.END}\n")
        style.finish(first, result, 0)
        second = self.style_start("dispatch-101")
        self.assertNotEqual(first, second)

    def test_dead_owner_retry_keeps_its_state_when_old_capture_arrives(self) -> None:
        first = self.style_start()
        with mock.patch.object(sessions, "process_liveness", return_value=sessions.SessionNotLive()):
            second = self.style_start("dispatch-101")
        before = file_snapshot(second.parent.parent)
        result = self.root / "reply"
        _ = result.write_text(f"{style.BEGIN}\nStyle review: Pass\n{style.END}\n")
        style.finish(first, result, 0)
        self.assertEqual(file_snapshot(second.parent.parent), before)
        self.assertEqual(cast(dict[str, object], json.loads(
            (second.parent.parent / "style_review_state.json").read_text()))["state"], "owed")

    def test_exited_preparation_owner_does_not_block_retry(self) -> None:
        _ = (self.repo / "note.txt").write_text("new\n")
        prompt = self.root / "prompt"
        _ = prompt.write_text("Review.\n")
        directory_file = self.root / "directory"
        completed = subprocess.run([
            sys.executable, str(Path(style.__file__)), "prepare", "--working-dir", str(self.repo),
            "--phase-dir", str(self.root / "phase"), "--since", "HEAD", "--prompt", str(prompt),
            "--directory-file", str(directory_file), "--pass-name", "dead-dispatch",
        ], capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        first = Path(directory_file.read_text().strip())
        second = self.style_start("replacement-dispatch")
        self.assertNotEqual(first, second)

    def test_capture_keeps_the_phase_lock_through_publication_before_retry_can_prepare(self) -> None:
        first = self.style_start()
        result = self.root / "reply"
        _ = result.write_text(f"{style.BEGIN}\nStyle review: Pass\n{style.END}\n")
        publishing, release, retry_lock = threading.Event(), threading.Event(), threading.Event()
        original_write, original_lock = write_text_atomic, worker_lifecycle.acquire_flock

        def write(path: Path, text: str) -> None:
            if path == first.parent.parent / "phase_style_review.md" and "Style review: Pass" in text:
                publishing.set()
                if not release.wait(TEST_HANG_GUARD_SECS):
                    raise AssertionError("publication was not released")
            original_write(path, text)

        def lock(handle: int | LockFileDescriptor, *, mode: int = fcntl.LOCK_EX, deadline: float = NO_LOCK_DEADLINE) -> None:
            if threading.current_thread().name.startswith("retry"):
                retry_lock.set()
            original_lock(handle, mode=mode, deadline=deadline)

        with mock.patch.object(style, "write_text_atomic", side_effect=write), mock.patch.object(
                worker_lifecycle, "acquire_flock", side_effect=lock):
            with ThreadPoolExecutor(max_workers=1) as capture, ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="retry") as retry:
                completed = capture.submit(style.finish, first, result, 0)
                try:
                    self.assertTrue(publishing.wait(TEST_HANG_GUARD_SECS))
                    prepared = retry.submit(self.style_start, "dispatch-101")
                    self.assertTrue(retry_lock.wait(TEST_HANG_GUARD_SECS))
                    self.assertFalse(prepared.done())
                finally:
                    release.set()
                _ = completed.result(timeout=TEST_HANG_GUARD_SECS)
                second = prepared.result(timeout=TEST_HANG_GUARD_SECS)
        state = cast(dict[str, object], json.loads((second.parent.parent / "style_review_state.json").read_text()))
        self.assertEqual(state["input_directory"], str(second))
        self.assertEqual(state["state"], "owed")
        self.assertNotIn("Style review: Pass", (second.parent.parent / "phase_style_review.md").read_text())

    def test_output_inside_repository_is_refused_before_any_artifact_is_created(self) -> None:
        output = self.repo / "style-output"
        result = style.prepare(self.repo, "HEAD", output)
        self.assertIsInstance(result, style.PreparationError)
        if isinstance(result, style.PreparationError):
            self.assertIn("outside the reviewed repository", result.cause)
        self.assertFalse(output.exists())
        prompt = self.root / "prompt"
        _ = prompt.write_text("Review.\n")
        with self.assertRaisesRegex(ValueError, "outside the reviewed repository"):
            _ = style.start(self.repo, "HEAD", output, prompt, "manual")
        self.assertFalse(output.exists())

    def test_invalid_baseline_is_an_error_not_an_empty_review(self) -> None:
        result = style.prepare(self.repo, "missing-commit", self.output)
        self.assertIsInstance(result, style.PreparationError)
        if isinstance(result, style.PreparationError):
            self.assertIn("invalid baseline", result.cause)
        self.assertFalse((self.output / "style-review.diff").exists())

    def test_tracked_failure_does_not_produce_a_partial_input(self) -> None:
        original = style.git
        def failing(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
            if arguments[0] == "diff":
                return subprocess.CompletedProcess(list(arguments), 128, b"", b"fixture diff failure")
            return original(root, *arguments)
        with mock.patch.object(style, "git", side_effect=failing):
            result = style.prepare(self.repo, "HEAD", self.output)
        self.assertIsInstance(result, style.PreparationError)
        if isinstance(result, style.PreparationError):
            self.assertIn("tracked diff failed", result.cause)
        self.assertFalse(self.output.exists())

    def test_off_skips_even_an_invalid_baseline(self) -> None:
        _ = (self.root / "lint.conf").write_text("style_review=off\n")
        result = style.prepare(self.repo, "not-a-ref", self.output)
        self.assertEqual(result, style.Finished("Off"))

    def test_unchanged_worktree_reports_no_changes(self) -> None:
        result = style.prepare(self.repo, "HEAD", self.output)
        self.assertEqual(result, style.Finished("No changes to review"))
        self.assertEqual((self.output / "style-review.diff").read_bytes(), b"")

    def test_untracked_paths_with_spaces_are_added_with_real_headers(self) -> None:
        _ = (self.repo / "new file.rs").write_text("fn added() {}\n")
        result = style.prepare(self.repo, "HEAD", self.output)
        self.assertIsInstance(result, style.Ready)
        if isinstance(result, style.Ready):
            self.assertTrue(result.inputs.rust_changed)
            self.assertEqual(result.inputs.paths, ("new file.rs",))
            self.assertIn("+++ b/new file.rs", result.inputs.diff.read_text())
            self.assertEqual(result.inputs.additions.read_text(), "+fn added() {}\n")

    def test_nonrust_additions_keep_the_scanner_without_loading_rust(self) -> None:
        _ = (self.repo / "note.txt").write_text("new\n")
        result = style.prepare(self.repo, "HEAD", self.output)
        self.assertIsInstance(result, style.Ready)
        if isinstance(result, style.Ready):
            self.assertFalse(result.inputs.rust_changed)
            self.assertEqual(result.inputs.additions.read_text(), "+new\n")

    def test_deleted_cargo_file_still_requires_the_rust_checklist(self) -> None:
        manifest = self.repo / "Cargo.toml"
        _ = manifest.write_text('[package]\nname="fixture"\n')
        _ = self.commit("manifest")
        manifest.unlink()
        result = style.prepare(self.repo, "HEAD", self.output)
        self.assertIsInstance(result, style.Ready)
        if isinstance(result, style.Ready):
            self.assertTrue(result.inputs.rust_changed)
            self.assertEqual(result.inputs.additions.read_text(), "")

    def test_assignment_uses_the_runner_trees_tools_for_another_repository(self) -> None:
        _ = (self.repo / "note.txt").write_text("new\n")
        prompt = self.root / "prompt.md"
        _ = prompt.write_text("Review the change.\n")
        directory = style.start(self.repo, "HEAD", self.root / "phase", prompt, "review")
        assignment = (directory / "assignment.md").read_text()
        scripts = Path(style.__file__).resolve().parents[1]
        self.assertNotEqual(scripts.parent, self.repo)
        self.assertIn(f'zsh "{scripts}/rust_style/load-rust-style.sh"', assignment)
        self.assertIn(f'"{scripts}/lib/py" "{scripts}/hooks/banned_words_lib.py" --diff', assignment)
        self.assertNotIn("~/.claude/scripts", assignment)
        self.assertNotIn("$scripts", assignment)

    def test_committed_range_excludes_later_commits_dirty_and_untracked_work(self) -> None:
        baseline = style.git(self.repo, "rev-parse", "HEAD").stdout.decode().strip()
        _ = (self.repo / "note.txt").write_text("committed range\n")
        end = self.commit("range end")
        _ = (self.repo / "later.rs").write_text("fn later() {}\n")
        _ = self.commit("outside range")
        _ = (self.repo / "note.txt").write_text("dirty work\n")
        _ = (self.repo / "untracked.rs").write_text("fn untracked() {}\n")
        result = style.prepare(self.repo, baseline, self.output, style.CommitEnd(end))
        self.assertIsInstance(result, style.Ready)
        if isinstance(result, style.Ready):
            self.assertEqual(result.inputs.end, style.CommitEnd(end))
            self.assertEqual(result.inputs.paths, ("note.txt",))
            self.assertFalse(result.inputs.rust_changed)
            self.assertEqual(result.inputs.additions.read_text(), "+committed range\n")

    def test_invalid_committed_end_is_an_error_not_a_worktree_fallback(self) -> None:
        result = style.prepare(self.repo, "HEAD", self.output, style.CommitEnd("missing-end"))
        self.assertIsInstance(result, style.PreparationError)
        if isinstance(result, style.PreparationError):
            self.assertIn("invalid end commit", result.cause)
        self.assertFalse(self.output.exists())

    def test_diff_scanner_does_not_touch_counters(self) -> None:
        home = self.root / "scanner-home"
        environment = no_temp_environment(home)
        guide = home / "rust/nate_style/rust/forbidden-words.md"
        guide.parent.mkdir(parents=True)
        _ = guide.write_text('### "delve"\n')
        diff = "diff --git a/a.rs b/a.rs\n+++ b/a.rs\n@@ -0,0 +1 @@\n+// delve\n"
        counters = home / ".claude/state/forbidden-word-counts.json"
        counters.parent.mkdir(parents=True)
        _ = counters.write_text('{"delve": {"count": 3}}')
        before = file_snapshot(home)
        result = subprocess.run([sys.executable, str(style.ROOT / "scripts/hooks/banned_words_lib.py"), "--diff"],
                                input=diff, text=True, capture_output=True,
                                env=environment, check=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("a.rs:1:", result.stdout)
        self.assertEqual(file_snapshot(home), before)

    def test_guide_loader_does_not_populate_a_cold_interpreter_cache(self) -> None:
        home = self.root / "home"
        environment = no_temp_environment(home)
        lib = home / ".claude/scripts/lib"
        lib.mkdir(parents=True)
        _ = shutil.copy2(style.ROOT / "scripts/lib/py", lib / "py")
        rules = home / "rust/nate_style/rust"
        rules.mkdir(parents=True)
        _ = (rules / "fixture.md").write_text(
            '---\nsee_also:\n  - "[[other]]"\n---\n# Fixture rule\nKeep the fixture readable.\n')
        _ = (rules / "other.md").write_text("# Other rule\nKeep the related rule readable.\n")
        counters = home / ".claude/state/forbidden-word-counts.json"
        counters.parent.mkdir(parents=True)
        _ = counters.write_text('{"delve": {"count": 3}}')
        before = file_snapshot(home)
        result = subprocess.run(["zsh", "-c", 'TMPPREFIX="$1"; source "$2" --project-root "$3"', "fixture",
                                 environment["TMPPREFIX"], str(style.ROOT / "scripts/rust_style/load-rust-style.sh"),
                                 str(self.repo)],
                                cwd=self.repo, env=environment, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Rust style guide loaded", result.stdout)
        self.assertIn("Local forbidden-word counters: delve=3", result.stdout)
        self.assertIn("See also: Other rule", result.stdout)
        self.assertNotIn("temp file", result.stderr)
        self.assertEqual(file_snapshot(home), before)
