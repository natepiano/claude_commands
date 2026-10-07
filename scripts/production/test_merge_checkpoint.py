"""Exercise checkpoint merges against disposable repositories and command stubs."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import NamedTuple, cast, final, override

from merge_checkpoint import (CodeCheckpoint, LastMerged, NoMerge, ShrinkCommit,
                              merge_branch_history, merge_history, request_from)


SCRIPT = Path(__file__).with_name("merge_checkpoint.py")
STARTED = "2026-10-06T12:00:00-07:00"
TRIAL = ("review trial: ux 2 findings, code 3 findings, review-seat minutes 14, "
         "ux check minutes 4, ux repair minutes 1")
SIMPLE_OWNS = "`src/alpha.txt`, `crates/alpha/`, `docs/plans/alpha.md`, `docs/plans/alpha-next.md`"
REAL_OWNS = ("`docs/plans/build-followups-enh-showrunner.md`; `scripts/production/add_unit.py`, "
             "`test_add_unit.py`, `merge_checkpoint.py`, `test_merge_checkpoint.py`; "
             "`commands/showrunner/add_unit.md`; after stalls-unit Phase 6 merges: "
             "`commands/showrunner/produce.md`, `commands/showrunner/promote_unit.md`, "
             "`scripts/production/showrunners.py`, `scripts/production/stall_watch.py` and their tests "
             "(hub rows); from its Phase 5 (the user, 2026-10-06 15:25 PDT, the phase-end split): "
             "`commands/unit/delegate.md`, `commands/unit/checkpoint.md`, "
             "`docs/delegate/run_phase_review.md`, `docs/production_format.md`, "
             "`commands/unit/add_ons.md`, `commands/plan/shrink.md`, "
             "`commands/plan/phase_review.md`, `docs/delegate/phase_end.md`, "
             "`docs/delegate_plan_format.md`, `commands/unit/eta_breakdown.md`, "
             "`docs/delegate/final_gate_commit.md`")
SIMPLE_MERGE_TESTS = "- **Merge tests:** `checkpoint-test`"
REAL_MERGE_TESTS = ("- **Merge tests:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` "
                    "and `basedpyright scripts/buildlog` (pass is its `0 errors, 0 warnings` line: it exits 3 "
                    "because pyrightconfig names a `.venv` no checkout has), run in the showrunner checkout")
DELIVERS = "Delivers checkpoint merge behavior."
STUB = '''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

state = Path(os.environ["CHECKPOINT_TEST_STATE"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with (state / "events.jsonl").open("a") as output:
    output.write(json.dumps({"command": name, "args": args}) + "\\n")
if name == "ssh":
    print("rc=" + (state / "mac-rc").read_text().strip() if (state / "mac-rc").exists() else "rc=0")
    raise SystemExit(0)
if name == "verify.sh":
    key = "_".join(args).replace("/", "_")
    if args[:1] == ["test"]:
        package_key = "test-" + args[1] if len(args) > 1 else "test"
        package_marker = state / ("fail-once-" + package_key)
        if package_marker.exists():
            package_marker.unlink()
            raise SystemExit(1)
        if (state / ("fail-always-" + package_key)).exists():
            raise SystemExit(1)
        key = "test"
    marker = state / ("fail-once-" + key)
    if marker.exists():
        marker.unlink()
        raise SystemExit(1)
    if (state / ("fail-always-" + key)).exists():
        raise SystemExit(1)
if (state / ("fail-" + name)).exists():
    raise SystemExit(1)
if name == "basedpyright":
    print("0 errors, 0 warnings, 0 notes")
    raise SystemExit(3)
'''


class MergeResult(NamedTuple):
    process: subprocess.CompletedProcess[str]
    before: str


@final
class MergeCheckpointTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.origin = Path()
        self.checkout = Path()
        self.unit = Path()
        self.other = Path()
        self.promote = Path()
        self.doc = Path()
        self.log = Path()
        self.bin = Path()
        self.state = Path()
        self.env: dict[str, str] = {}
        self.push = "git"
        self.promotions = ""
        self.known_flakes = ""
        self.gate = True

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.origin = self.root / "origin.git"
        self.checkout = self.root / "merge"
        self.unit = self.root / "alpha"
        self.other = self.root / "beta"
        self.promote = self.root / "installed"
        self.doc = self.root / "example-production.md"
        self.log = self.checkout / "production.log"
        self.bin = self.root / "bin"
        self.state = self.root / "stub-state"
        self.bin.mkdir()
        self.state.mkdir()
        self.checkout.mkdir()
        _ = self.git("init", "--bare", str(self.origin), cwd=self.root)
        _ = self.git("init", "-b", "main", cwd=self.checkout)
        _ = self.git("config", "user.name", "Checkpoint Test", cwd=self.checkout)
        _ = self.git("config", "user.email", "checkpoint@example.invalid", cwd=self.checkout)
        _ = self.git("remote", "add", "origin", str(self.origin), cwd=self.checkout)
        self.write(self.checkout, "README.md", "base\n")
        self.write(self.checkout, ".gitignore", "production.log\n")
        self.write(self.checkout, "src/alpha.txt", "alpha base\n")
        self.write(self.checkout, "src/beta.txt", "beta base\n")
        _ = self.git("add", ".", cwd=self.checkout)
        _ = self.git("commit", "-m", "initial", cwd=self.checkout)
        _ = self.git("push", "-u", "origin", "main", cwd=self.checkout)
        _ = self.git("branch", "production", cwd=self.checkout)
        _ = self.git("checkout", "production", cwd=self.checkout)
        _ = self.git("push", "-u", "origin", "production", cwd=self.checkout)
        _ = self.git("worktree", "add", "-b", "alpha-branch", str(self.unit), "production", cwd=self.checkout)
        _ = self.git("push", "-u", "origin", "alpha-branch", cwd=self.unit)
        _ = self.git("worktree", "add", "-b", "beta-branch", str(self.other), "production", cwd=self.checkout)
        _ = self.git("push", "-u", "origin", "beta-branch", cwd=self.other)
        _ = self.git("worktree", "add", str(self.promote), "main", cwd=self.checkout)
        _ = self.git("config", "user.name", "Checkpoint Test", cwd=self.unit)
        _ = self.git("config", "user.email", "checkpoint@example.invalid", cwd=self.unit)
        _ = self.git("config", "user.name", "Checkpoint Test", cwd=self.other)
        _ = self.git("config", "user.email", "checkpoint@example.invalid", cwd=self.other)
        for name in ("verify.sh", "checkpoint-test", "validate_and_push.sh", "review_regime.py", "ssh"):
            self.executable(self.bin / name, STUB)
        home = self.root / "home"
        for relative, name in ((".claude/scripts/delegate", "verify.sh"),
                               (".claude/scripts/validate_and_push", "validate_and_push.sh"),
                               (".claude/scripts/production", "review_regime.py")):
            directory = home / relative
            directory.mkdir(parents=True)
            self.executable(directory / name, STUB)
        self.env = {**os.environ, "HOME": str(home),
                    "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                    "CHECKPOINT_TEST_STATE": str(self.state)}
        self.production_doc()

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", *args], cwd=cwd or self.checkout,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, (args, result.stdout, result.stderr))
        return result.stdout.strip()

    def write(self, checkout: Path, relative: str, content: str) -> None:
        path = checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content, encoding="utf-8")

    def executable(self, path: Path, content: str) -> None:
        content = content.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1)
        _ = path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def production_doc(self, *, owns_cell: str = SIMPLE_OWNS,
                       merge_tests_line: str | None = SIMPLE_MERGE_TESTS,
                       hub_files: bool = True) -> None:
        rows = [
            "# Production — example", "", "## Production Context", "",
            "- **Merge branch:** `production`",
            f"- **Showrunner checkout:** `{self.checkout}`",
            "- **Showrunner session:** test-showrunner",
            "- **Log:** `production.log`",
            "- **User zone:** America/Los_Angeles",
        ]
        if merge_tests_line is not None:
            rows.append(merge_tests_line)
        if self.push:
            rows.append(f"- **Push:** {self.push}")
        if self.promotions:
            rows.append(f"- **Promote:** {self.promotions}")
        if self.known_flakes:
            rows.append(f"- **Known flakes:** {self.known_flakes}")
        rows.extend([
            "", "## Units", "",
            "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            f"| `alpha-unit` | `docs/plans/alpha.md` | `{self.unit}` | `alpha-branch` | alpha | — | {owns_cell} |",
            f"| `beta-unit` | `docs/plans/beta.md` | `{self.other}` | `beta-branch` | beta | — | `src/beta.txt` |",
            "", "## Gates", "",
            "| Gate | Waiting | Waits on | Clears when |",
            "| --- | --- | --- | --- |",
        ])
        if hub_files:
            hub_start = rows.index("## Gates") - 1
            rows[hub_start:hub_start] = [
                "## Hub files", "",
                "| File | Owner unit | Other units that touch it |",
                "| --- | --- | --- |",
                "| `shared.txt` | beta-unit | alpha-unit |", "",
            ]
        if self.gate:
            rows.append("| G1 | beta-unit Phase 4 | alpha-unit Phase 2 | checkpoint merged |")
        rows.extend(["", "## Production rules", "", "- Merge each phase and push the merge branch."])
        _ = self.doc.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def checkpoint(self, relative: str = "src/alpha.txt", content: str = "alpha change\n",
                   *, subject: str = "alpha work", push: bool = True) -> str:
        self.write(self.unit, relative, content)
        _ = self.git("add", relative, cwd=self.unit)
        _ = self.git("commit", "-m", subject, cwd=self.unit)
        sha = self.git("rev-parse", "HEAD", cwd=self.unit)
        if push:
            _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
        return sha

    def run_checkpoint(self, sha: str, *extra: str, phase: str = "2",
                       review: bool = True, trial: str = TRIAL,
                       delivers: str | None = DELIVERS) -> MergeResult:
        before = self.git("rev-parse", "HEAD")
        arguments = ["--production", str(self.doc), "alpha-unit", phase, sha,
                     "--started", STARTED, "--scratch", str(self.root / "scratch"),
                     "--trailer", "Co-authored-by: Test <test@example.invalid>"]
        if review:
            arguments.extend(("--review-trial", trial))
        if delivers is not None and "--shrink" not in extra:
            arguments.extend(("--delivers", delivers))
        arguments.extend(extra)
        process = subprocess.run([sys.executable, str(SCRIPT), *arguments], cwd=self.checkout,
                                 env=self.env, capture_output=True, text=True, check=False, timeout=25)
        return MergeResult(process, before)

    def events(self, command: str) -> list[list[str]]:
        path = self.state / "events.jsonl"
        if not path.exists():
            return []
        rows = [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()]
        return [cast(list[str], row["args"]) for row in rows if row["command"] == command]

    def assert_stopped_before_merge(self, result: MergeResult, state: str, detail: str) -> None:
        self.assertIn(f": {state} —", result.process.stdout, result.process.stdout + result.process.stderr)
        self.assertIn(detail, result.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), result.before)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.events("checkpoint-test"), [])
        self.assertEqual(self.events("review_regime.py"), [])
        self.assertIn("send alpha-unit:", result.process.stdout)

    def test_missing_hash_and_unpushed_checkpoint_stop_at_ancestry(self) -> None:
        missing = self.run_checkpoint("0" * 40)
        self.assert_stopped_before_merge(missing, "failed", "commit")
        self.assertIn("push", missing.process.stdout)
        sha = self.checkpoint(push=False)
        unpushed = self.run_checkpoint(sha)
        self.assert_stopped_before_merge(unpushed, "held", "origin")
        self.assertIn("push", unpushed.process.stdout)

    def test_scope_holds_unowned_path_and_allows_explicit_also(self) -> None:
        sha = self.checkpoint("outside.txt", "unowned\n")
        held = self.run_checkpoint(sha)
        self.assert_stopped_before_merge(held, "held", "outside.txt")
        accepted = self.run_checkpoint(sha, "--also", "outside.txt")
        self.assertEqual(accepted.process.returncode, 0, accepted.process.stderr + accepted.process.stdout)
        self.assertIn("scope: ok", accepted.process.stdout)

    def assert_real_owns_path_allowed(self, relative: str) -> None:
        self.production_doc(owns_cell=REAL_OWNS, hub_files=False)
        sha = self.checkpoint(relative, relative + "\n", subject="own " + relative)
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertIn("scope: ok", result.process.stdout)

    def test_real_owns_cell_allows_produce_path_after_commentary(self) -> None:
        self.assert_real_owns_path_allowed("commands/showrunner/produce.md")

    def test_real_owns_cell_allows_explicit_add_unit_path_in_second_group(self) -> None:
        self.assert_real_owns_path_allowed("scripts/production/add_unit.py")

    def test_real_owns_cell_reads_a_bare_name_beside_the_path_before_it(self) -> None:
        self.assert_real_owns_path_allowed("scripts/production/merge_checkpoint.py")

    def test_real_owns_cell_reads_a_second_bare_name_beside_the_same_path(self) -> None:
        self.assert_real_owns_path_allowed("scripts/production/test_merge_checkpoint.py")

    def test_real_owns_cell_still_reads_a_bare_name_as_a_root_file(self) -> None:
        self.assert_real_owns_path_allowed("merge_checkpoint.py")

    def test_real_owns_cell_allows_delegate_path_in_last_group(self) -> None:
        self.assert_real_owns_path_allowed("commands/unit/delegate.md")

    def test_real_owns_cell_holds_an_outside_path(self) -> None:
        self.production_doc(owns_cell=REAL_OWNS, hub_files=False)
        outside = self.checkpoint("scripts/other/outside.py", "outside\n")
        held = self.run_checkpoint(outside)
        self.assertEqual(held.process.returncode, 1)
        self.assertIn("scope: held", held.process.stdout)
        self.assertIn("scripts/other/outside.py", held.process.stdout)

    def test_real_merge_tests_run_only_the_two_commands_before_commentary(self) -> None:
        self.production_doc(merge_tests_line=REAL_MERGE_TESTS)
        for name in ("python3", "basedpyright"):
            self.executable(self.bin / name, STUB)
        sha = self.checkpoint()
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertEqual(self.events("python3"), [["-m", "unittest", "discover", "-s", "scripts/buildlog",
                                                    "-p", "test_*.py"]])
        self.assertEqual(self.events("basedpyright"), [["scripts/buildlog"]])
        self.assertEqual(self.events("0"), [])
        all_args = [arg for event in (self.events("python3") + self.events("basedpyright")) for arg in event]
        self.assertFalse(any(".venv" in arg for arg in all_args))
        self.assertIn("test: ok", result.process.stdout)

    def test_missing_merge_test_executable_is_red_and_restores_unpushed_merge(self) -> None:
        self.production_doc(merge_tests_line="- **Merge tests:** `checkpoint-executable-that-does-not-exist`")
        sha = self.checkpoint()
        origin_before = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 1)
        self.assertIn("red: failed", result.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), result.before)
        merge_head = subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                                    cwd=self.checkout, capture_output=True, text=True, check=False)
        self.assertNotEqual(merge_head.returncode, 0)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), origin_before)
        self.assertIn("send alpha-unit:", result.process.stdout)
        log = self.root / "scratch" / f"merge_{sha[:7]}_test.log"
        self.assertIn("merge_test_1_EXIT=127", log.read_text(encoding="utf-8"))

    def test_merge_tests_field_is_required_but_dash_runs_no_merge_test(self) -> None:
        sha = self.checkpoint()
        self.production_doc(merge_tests_line=None)
        absent = self.run_checkpoint(sha)
        self.assertEqual(absent.process.returncode, 1)
        self.assertIn("input: failed", absent.process.stdout)
        self.assertIn("production doc lacks Merge tests", absent.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), absent.before)
        self.production_doc(merge_tests_line="- **Merge tests:** —")
        empty = self.run_checkpoint(sha)
        self.assertEqual(empty.process.returncode, 0, empty.process.stderr + empty.process.stdout)
        self.assertEqual(self.events("checkpoint-test"), [])
        self.assertIn("test: ok — 0 commands", empty.process.stdout)

    def test_later_phase_uses_last_unit_checkpoint_for_ancestry(self) -> None:
        first_sha = self.checkpoint()
        first = self.run_checkpoint(first_sha)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        self.assertIn(f"({first_sha[:7]})", self.git("log", "-1", "--format=%s"))
        next_sha = self.checkpoint(content="phase three\n")
        later = self.run_checkpoint(next_sha, phase="3")
        self.assertEqual(later.process.returncode, 0, later.process.stderr + later.process.stdout)
        self.assertIn("ancestry: ok", later.process.stdout)

    def test_code_checkpoint_requires_review_trial_before_merge(self) -> None:
        sha = self.checkpoint()
        result = self.run_checkpoint(sha, review=False)
        self.assertIn("failed", result.process.stdout)
        self.assertIn("--review-trial", result.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), result.before)
        self.assertEqual(self.events("checkpoint-test"), [])

    def test_alternate_review_trial_is_refused_and_canonical_numbers_reach_record(self) -> None:
        sha = self.checkpoint()
        alternate = self.run_checkpoint(sha, trial="review trial: UX 2, code 3, review 14 min, UX check 4 min, UX repair 1 min")
        self.assertEqual(alternate.process.returncode, 1)
        self.assert_stopped_before_merge(alternate, "failed", "review trial: ux <N> findings")
        self.assertIn("input: failed", alternate.process.stdout)
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        review = self.events("review_regime.py")
        self.assertEqual(len(review), 1)
        for flag, value in (("--ux-findings", "2"), ("--code-findings", "3"),
                            ("--review-minutes", "14"), ("--ux-check-minutes", "4"),
                            ("--ux-repair-minutes", "1")):
            self.assertEqual(review[0][review[0].index(flag) + 1], value)

    def test_conflict_holds_with_no_merge_left_behind(self) -> None:
        self.write(self.checkout, "src/alpha.txt", "main side\n")
        _ = self.git("add", "src/alpha.txt")
        _ = self.git("commit", "-m", "other change")
        sha = self.checkpoint(content="unit side\n")
        result = self.run_checkpoint(sha)
        self.assert_stopped_before_merge(result, "held", "conflict")
        self.assertIn("new hash", result.process.stdout)

    def test_other_unit_overlap_is_reported_as_data(self) -> None:
        self.write(self.other, "shared.txt", "beta branch\n")
        _ = self.git("add", "shared.txt", cwd=self.other)
        _ = self.git("commit", "-m", "beta shared edit", cwd=self.other)
        _ = self.git("push", "origin", "beta-branch", cwd=self.other)
        self.write(self.other, "shared.txt", "beta uncommitted\n")
        sha = self.checkpoint("shared.txt", "alpha shared\n")
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertIn("beta-unit", result.process.stdout)
        self.assertIn("shared.txt", result.process.stdout)

    def test_changed_example_gets_its_own_verify_call(self) -> None:
        self.write(self.unit, "crates/alpha/Cargo.toml", '[package]\nname = "alpha"\nversion = "0.1.0"\n')
        self.write(self.unit, "crates/alpha/examples/demo.rs", "fn main() {}\n")
        _ = self.git("add", "crates/alpha", cwd=self.unit)
        _ = self.git("commit", "-m", "add example", cwd=self.unit)
        sha = self.git("rev-parse", "HEAD", cwd=self.unit)
        _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        calls = self.events("verify.sh")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "test")
        self.assertEqual(calls[1][0], "example")
        self.assertIn("alpha", calls[1])
        self.assertIn("demo", calls[1])

    def test_default_validate_and_push_failure_undoes_unpushed_merge(self) -> None:
        self.push = ""
        self.production_doc()
        sha = self.checkpoint()
        _ = (self.state / "fail-validate_and_push.sh").touch()
        result = self.run_checkpoint(sha)
        self.assertIn("failed", result.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), result.before)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), result.before)
        self.assertEqual(self.events("validate_and_push.sh"), [[
            "--quick", "--to", "production", "--fix-commit",
            "ci(example): format fixes after alpha-unit phase 2",
        ]])
        self.assertEqual(self.events("review_regime.py"), [])

    def test_cancel_prior_reaches_validate_and_push_only_when_requested(self) -> None:
        self.push = "validate_and_push"
        self.production_doc()
        first = self.checkpoint()
        first_result = self.run_checkpoint(first)
        self.assertEqual(first_result.process.returncode, 0, first_result.process.stderr + first_result.process.stdout)
        second = self.checkpoint(content="second\n")
        second_result = self.run_checkpoint(second, "--cancel-prior", phase="3")
        self.assertEqual(second_result.process.returncode, 0,
                         second_result.process.stderr + second_result.process.stdout)
        calls = self.events("validate_and_push.sh")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("--cancel-prior", calls[0])
        self.assertIn("--cancel-prior", calls[1])

    def test_cancel_prior_with_git_push_is_refused_before_merge(self) -> None:
        sha = self.checkpoint()
        result = self.run_checkpoint(sha, "--cancel-prior")
        self.assertEqual(result.process.returncode, 1)
        self.assertIn("input: failed", result.process.stdout)
        self.assertIn("--cancel-prior applies only to Push: validate_and_push", result.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), result.before)
        self.assertEqual(self.events("checkpoint-test"), [])

    def test_absent_optional_fields_default_to_validate_push_without_promotion(self) -> None:
        self.push = ""
        self.production_doc()
        sha = self.checkpoint()
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertEqual(len(self.events("validate_and_push.sh")), 1)
        self.assertIn("promote: ok — no promotion configured", result.process.stdout)
        self.assertEqual(self.events("ssh"), [])

    def test_delivers_line_is_merge_body_before_trailer(self) -> None:
        sha = self.checkpoint()
        result = self.run_checkpoint(sha, delivers="Delivers one reusable checkpoint command.")
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        subject = f"Merge alpha-unit phase 2 ({sha[:7]}) into production"
        self.assertEqual(self.git("log", "-1", "--format=%B"),
                         subject + "\n\nDelivers one reusable checkpoint command.\n\n"
                         + "Co-authored-by: Test <test@example.invalid>")

    def test_missing_or_empty_delivers_is_refused_before_merge(self) -> None:
        sha = self.checkpoint()
        for delivers in (None, ""):
            with self.subTest(delivers=delivers):
                result = self.run_checkpoint(sha, delivers=delivers)
                self.assertEqual(result.process.returncode, 1)
                self.assertIn("input: failed", result.process.stdout)
                self.assertIn("--delivers", result.process.stdout)
                self.assertEqual(self.git("rev-parse", "HEAD"), result.before)
        self.assertEqual(self.events("checkpoint-test"), [])

    def test_git_push_rejection_restores_merge_branch(self) -> None:
        sha = self.checkpoint()
        self.executable(self.origin / "hooks/pre-receive", "#!/bin/sh\nexit 1\n")
        result = self.run_checkpoint(sha)
        self.assertIn("push: failed", result.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), result.before)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), result.before)
        self.assertEqual(self.events("review_regime.py"), [])

    def test_diverged_main_conflict_restores_clean_merge_branch(self) -> None:
        sha = self.checkpoint(content="unit side\n")
        main_writer = self.root / "main-writer"
        _ = self.git("clone", "-b", "main", str(self.origin), str(main_writer), cwd=self.root)
        _ = self.git("config", "user.name", "Checkpoint Test", cwd=main_writer)
        _ = self.git("config", "user.email", "checkpoint@example.invalid", cwd=main_writer)
        self.write(main_writer, "src/alpha.txt", "main side\n")
        _ = self.git("add", "src/alpha.txt", cwd=main_writer)
        _ = self.git("commit", "-m", "conflicting main edit", cwd=main_writer)
        _ = self.git("push", "origin", "main", cwd=main_writer)
        result = self.run_checkpoint(sha)
        self.assertIn("push: failed", result.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), result.before)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), result.before)

    def test_red_package_resets_merge_and_known_flake_continues(self) -> None:
        self.write(self.unit, "crates/alpha/Cargo.toml", '[package]\nname = "alpha"\nversion = "0.1.0"\n')
        self.write(self.unit, "crates/alpha/src/lib.rs", "pub fn value() -> i32 { 1 }\n")
        _ = self.git("add", "crates/alpha", cwd=self.unit)
        _ = self.git("commit", "-m", "add package", cwd=self.unit)
        sha = self.git("rev-parse", "HEAD", cwd=self.unit)
        _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
        _ = (self.state / "fail-always-test").touch()
        failed = self.run_checkpoint(sha)
        self.assertIn("test: ", failed.process.stdout)
        self.assertIn("red: failed", failed.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), failed.before)
        self.assertEqual(len(self.events("verify.sh")), 2)
        (self.state / "fail-always-test").unlink()
        _ = (self.state / "fail-once-test").touch()
        self.known_flakes = "alpha"
        self.production_doc()
        passed = self.run_checkpoint(sha)
        self.assertEqual(passed.process.returncode, 0, passed.process.stderr + passed.process.stdout)
        self.assertEqual(len(self.events("verify.sh")), 4)
        self.assertIn("flake", self.log.read_text().lower())

    def test_two_backticked_known_flakes_each_count_green_alone(self) -> None:
        for package in ("crate-a", "crate-b"):
            self.write(self.unit, f"crates/alpha/{package}/Cargo.toml",
                       f'[package]\nname = "{package}"\nversion = "0.1.0"\n')
            self.write(self.unit, f"crates/alpha/{package}/src/lib.rs", "pub fn value() {}\n")
            _ = (self.state / f"fail-once-test-{package}").touch()
        _ = self.git("add", "crates/alpha", cwd=self.unit)
        _ = self.git("commit", "-m", "two packages", cwd=self.unit)
        sha = self.git("rev-parse", "HEAD", cwd=self.unit)
        _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
        self.known_flakes = "`crate-a`, `crate-b`"
        self.production_doc()
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertIn("known flake crate-a", self.log.read_text(encoding="utf-8"))
        self.assertIn("known flake crate-b", self.log.read_text(encoding="utf-8"))
        self.assertEqual(len(self.events("verify.sh")), 4)

    def test_rerun_after_origin_main_moves_skips_merge_branch_push_and_promotes_pushed_tip(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        sha = self.checkpoint()
        first = self.run_checkpoint(sha)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        pushed = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        head = self.git("rev-parse", "HEAD")
        writer = self.root / "main-writer"
        _ = self.git("clone", "-b", "main", str(self.origin), str(writer), cwd=self.root)
        _ = self.git("config", "user.name", "Checkpoint Test", cwd=writer)
        _ = self.git("config", "user.email", "checkpoint@example.invalid", cwd=writer)
        self.write(writer, "later-main.txt", "main moved after push\n")
        _ = self.git("add", "later-main.txt", cwd=writer)
        _ = self.git("commit", "-m", "later main", cwd=writer)
        _ = self.git("push", "origin", "main", cwd=writer)
        rerun = self.run_checkpoint(sha)
        self.assertEqual(rerun.process.returncode, 0, rerun.process.stderr + rerun.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), pushed)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), pushed)
        self.assertIn(f"already pushed at {pushed}", rerun.process.stdout)

    def test_retry_pushes_main_after_initial_promotion_push_rejection(self) -> None:
        self.promotions = str(self.promote) + " — live checkout"
        self.production_doc()
        marker = self.root / "reject-main"
        _ = marker.touch()
        hook = ("#!/bin/sh\nwhile read old new ref; do\n"
                + f'  if [ "$ref" = "refs/heads/main" ] && [ -e "{marker}" ]; then exit 1; fi\n'
                + "done\nexit 0\n")
        self.executable(self.origin / "hooks/pre-receive", hook)
        sha = self.checkpoint()
        failed = self.run_checkpoint(sha)
        self.assertEqual(failed.process.returncode, 1)
        self.assertIn("promote: failed", failed.process.stdout)
        pushed = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), pushed)
        self.assertNotEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), pushed)
        marker.unlink()
        retried = self.run_checkpoint(sha)
        self.assertEqual(retried.process.returncode, 0, retried.process.stderr + retried.process.stdout)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), pushed)
        self.assertIn(f"already pushed at {pushed}", retried.process.stdout)

    def test_local_foreign_main_commit_never_reaches_origin(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        self.write(self.promote, "foreign-main.txt", "another session\n")
        _ = self.git("add", "foreign-main.txt", cwd=self.promote)
        _ = self.git("commit", "-m", "foreign main commit", cwd=self.promote)
        origin_main = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
        sha = self.checkpoint()
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 1)
        self.assertIn("promote: failed", result.process.stdout)
        self.assertIn(str(self.promote), result.process.stdout)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), origin_main)

    def test_rerun_never_pushes_a_main_that_holds_a_foreign_commit(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        sha = self.checkpoint()
        first = self.run_checkpoint(sha)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        pushed = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
        self.write(self.promote, "foreign-main.txt", "another session\n")
        _ = self.git("add", "foreign-main.txt", cwd=self.promote)
        _ = self.git("commit", "-m", "foreign main commit", cwd=self.promote)
        rerun = self.run_checkpoint(sha)
        self.assertEqual(rerun.process.returncode, 0, rerun.process.stderr + rerun.process.stdout)
        self.assertIn("was not pushed", rerun.process.stdout)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), pushed)

    def test_git_push_merges_diverged_main_promotes_pushed_tip_and_records(self) -> None:
        self.promotions = str(self.promote) + ", mac " + str(self.root / "mac-installed")
        self.production_doc()
        sha = self.checkpoint()
        main_writer = self.root / "main-writer"
        _ = self.git("clone", "-b", "main", str(self.origin), str(main_writer), cwd=self.root)
        _ = self.git("config", "user.name", "Checkpoint Test", cwd=main_writer)
        _ = self.git("config", "user.email", "checkpoint@example.invalid", cwd=main_writer)
        self.write(main_writer, "main-only.txt", "another session\n")
        _ = self.git("add", "main-only.txt", cwd=main_writer)
        _ = self.git("commit", "-m", "main moved", cwd=main_writer)
        _ = self.git("push", "origin", "main", cwd=main_writer)
        result = self.run_checkpoint(sha, "--holds", "2", "--merge-defects", "1",
                                     "--regime", "trial", "--excluded", "no merge design check")
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        tip = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), tip)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), tip)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), tip)
        self.assertEqual(self.git("merge-base", "--is-ancestor", sha, tip), "")
        self.assertIn("main-only.txt", self.git("ls-tree", "-r", "--name-only", tip))
        self.assertRegex(result.process.stdout, re.escape(f"into: {self.checkout} (production) — ") + ".*unblocks: G1: beta-unit Phase 4")
        self.assertRegex(result.process.stdout, re.escape(f"into: {self.promote} (main) — ") + ".*unblocks: the live gate of alpha-unit phase 2")
        self.assertIn("the merge branch collects every unit's checkpoints", result.process.stdout)
        self.assertIn("sessions on", result.process.stdout)
        self.assertIn("into: " + str(self.root / "mac-installed"), result.process.stdout)
        self.assertEqual(len(self.events("ssh")), 1)
        review = self.events("review_regime.py")
        self.assertEqual(len(review), 1)
        merged_at = review[0][review[0].index("--merged") + 1]
        self.assertRegex(merged_at, r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(review[0], [
            "add", "--unit", "alpha-unit", "--phase", "2", "--regime", "trial",
            "--started", STARTED, "--merged", merged_at, "--holds", "2",
            "--merge-defects", "1", "--ux-findings", "2", "--code-findings", "3",
            "--review-minutes", "14", "--ux-check-minutes", "4",
            "--ux-repair-minutes", "1", "--excluded", "no merge design check",
        ])
        self.assertRegex(self.log.read_text(), r"- \d\d:\d\d PDT: alpha-unit phase 2 \([0-9a-f]+\) merged as [0-9a-f]+; .*green; pushed; promoted")
        self.assertIn("send alpha-unit:", result.process.stdout)

    def test_promotion_refusal_keeps_pushed_merge_and_resume_reaches_only_missing_place(self) -> None:
        self.promotions = str(self.promote)
        self.gate = False
        self.production_doc()
        sha = self.checkpoint()
        self.write(self.promote, "src/alpha.txt", "local edit\n")
        failed = self.run_checkpoint(sha)
        self.assertIn("failed", failed.process.stdout)
        self.assertIn("src/alpha.txt", failed.process.stdout)
        pushed = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        self.assertEqual(self.git("rev-parse", "HEAD"), pushed)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), failed.before)
        self.assertIn(f"into: {self.checkout} (production)", failed.process.stdout)
        self.assertIn("unblocks: no gate", failed.process.stdout)
        self.assertNotIn(f"into: {self.promote} (main)", failed.process.stdout)
        _ = self.git("restore", "src/alpha.txt", cwd=self.promote)
        resumed = self.run_checkpoint(sha)
        self.assertEqual(resumed.process.returncode, 0, resumed.process.stderr + resumed.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), pushed)
        self.assertIn(f"into: {self.promote} (main)", resumed.process.stdout)
        self.assertNotIn("unblocks: G1", resumed.process.stdout)

    def test_mac_rc_failure_is_reported_after_local_promotion(self) -> None:
        self.promotions = str(self.promote) + ", mac " + str(self.root / "mac-installed")
        self.production_doc()
        _ = (self.state / "mac-rc").write_text("1\n")
        sha = self.checkpoint()
        result = self.run_checkpoint(sha)
        self.assertIn("failed", result.process.stdout)
        self.assertIn(f"into: {self.checkout} (production)", result.process.stdout)
        self.assertIn(f"into: {self.promote} (main)", result.process.stdout)
        self.assertNotIn("into: " + str(self.root / "mac-installed"), result.process.stdout)

    def test_record_failure_resumes_after_push_without_second_merge(self) -> None:
        sha = self.checkpoint()
        _ = (self.state / "fail-review_regime.py").touch()
        failed = self.run_checkpoint(sha)
        self.assertIn("record: failed", failed.process.stdout)
        pushed = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        self.assertNotEqual(pushed, failed.before)
        self.assertFalse(self.log.exists())
        (self.state / "fail-review_regime.py").unlink()
        resumed = self.run_checkpoint(sha)
        self.assertEqual(resumed.process.returncode, 0, resumed.process.stderr + resumed.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), pushed)
        self.assertEqual(len(self.events("checkpoint-test")), 1)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertIn("unblocks: no gate", resumed.process.stdout)

    def test_shrink_merges_only_plan_and_next_items_after_code_checkpoint(self) -> None:
        code = self.checkpoint()
        first = self.run_checkpoint(code)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        self.write(self.unit, "docs/plans/alpha.md", "short as built\n")
        self.write(self.unit, "docs/plans/alpha-next.md", "next items\n")
        _ = self.git("add", "docs/plans/alpha.md", "docs/plans/alpha-next.md", cwd=self.unit)
        _ = self.git("commit", "-m", "shrink", cwd=self.unit)
        sha = self.git("rev-parse", "HEAD", cwd=self.unit)
        _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
        result = self.run_checkpoint(sha, "--shrink", review=False)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertEqual(len(self.events("review_regime.py")), 1)
        self.assertNotIn("unblocks: G1", result.process.stdout)
        self.assertIn("docs/plans/alpha-next.md", self.git("ls-tree", "-r", "--name-only", "HEAD"))

    def test_reruns_report_their_own_code_and_shrink_merge_hashes(self) -> None:
        code = self.checkpoint()
        first = self.run_checkpoint(code)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        code_merge = self.git("rev-parse", "HEAD")
        shrink = self.checkpoint("docs/plans/alpha.md", "short as built\n", subject="shrink")
        shrink_result = self.run_checkpoint(shrink, "--shrink", review=False, delivers=None)
        self.assertEqual(shrink_result.process.returncode, 0,
                         shrink_result.process.stderr + shrink_result.process.stdout)
        self.assertIn("\n\nPhase plan shrink.\n\n", self.git("log", "-1", "--format=%B"))
        shrink_merge = self.git("rev-parse", "HEAD")
        self.assertNotEqual(code_merge, shrink_merge)
        code_rerun = self.run_checkpoint(code)
        self.assertEqual(code_rerun.process.returncode, 0,
                         code_rerun.process.stderr + code_rerun.process.stdout)
        self.assertIn(f"send alpha-unit: phase 2 merged and pushed as {code_merge}", code_rerun.process.stdout)
        shrink_rerun = self.run_checkpoint(shrink, "--shrink", review=False, delivers=None)
        self.assertEqual(shrink_rerun.process.returncode, 0,
                         shrink_rerun.process.stderr + shrink_rerun.process.stdout)
        self.assertIn(f"send alpha-unit: phase 2 merged and pushed as {shrink_merge}",
                      shrink_rerun.process.stdout)

    def test_shrink_holds_before_code_checkpoint(self) -> None:
        sha = self.checkpoint("docs/plans/alpha.md", "short as built\n")
        early = self.run_checkpoint(sha, "--shrink", review=False)
        self.assert_stopped_before_merge(early, "held", "phase 2")

    def test_shrink_holds_with_unrelated_staged_path(self) -> None:
        code = self.checkpoint(content="actual code\n")
        self.assertEqual(self.run_checkpoint(code).process.returncode, 0)
        shrink = self.checkpoint("docs/plans/alpha.md", "short as built\n")
        self.write(self.checkout, "unrelated.txt", "staged\n")
        _ = self.git("add", "unrelated.txt")
        before = self.git("rev-parse", "HEAD")
        staged = self.run_checkpoint(shrink, "--shrink", review=False)
        self.assertIn("scope: held", staged.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "unrelated.txt")

    def test_merge_branch_reader_reports_no_merge_before_checkpoints(self) -> None:
        history = merge_branch_history(self.checkout, "production")
        self.assertIsInstance(history.last_merge(), NoMerge)
        self.assertIsInstance(history.last_for_unit("alpha-unit"), NoMerge)
        self.assertIsInstance(history.last_code_for_unit("alpha-unit"), NoMerge)
        self.assertEqual(history.code_merge_count(), 0)
        self.assertFalse(history.has_code_merge("alpha-unit", "2"))

    def test_merge_reader_keeps_subject_from_earlier_branch_name(self) -> None:
        code = self.checkpoint()
        _ = self.git("merge", "--no-ff", "-m",
                     f"Merge alpha-unit phase 2 ({code[:7]}) into old-name", "alpha-branch")
        history = merge_branch_history(self.checkout, "production")
        entry = history.last_code_for_unit("alpha-unit")
        self.assertNotIsInstance(entry, NoMerge)
        if isinstance(entry, NoMerge):
            self.fail("merge under earlier branch name was not read")
        self.assertEqual((entry.unit, entry.phase, entry.short), ("alpha-unit", "2", code[:7]))
        self.assertIsInstance(entry.kind, CodeCheckpoint)
        self.assertTrue(history.has_code_merge("alpha-unit", "2"))

    def test_merge_history_uses_code_checkpoint_after_shrink(self) -> None:
        code = self.checkpoint()
        _ = self.git("merge", "--no-ff", "-m",
                     f"Merge alpha-unit phase 2 ({code[:7]}) into production", "alpha-branch")
        code_merge = self.git("rev-parse", "HEAD")
        shrink = self.checkpoint("docs/plans/alpha.md", "short as built\n", subject="shrink")
        _ = self.git("merge", "--no-ff", "-m",
                     f"Merge alpha-unit phase 2 shrink ({shrink[:7]}) into production", "alpha-branch")
        shrink_merge = self.git("rev-parse", "HEAD")
        history = merge_branch_history(self.checkout, "production")
        last_any = history.last_for_unit("alpha-unit")
        last_code = history.last_code_for_unit("alpha-unit")
        self.assertNotIsInstance(last_any, NoMerge)
        self.assertNotIsInstance(last_code, NoMerge)
        if isinstance(last_any, NoMerge) or isinstance(last_code, NoMerge):
            self.fail("checkpoint history was not read")
        self.assertEqual(last_any.merge_hash, shrink_merge)
        self.assertIsInstance(last_any.kind, ShrinkCommit)
        self.assertEqual(last_code.merge_hash, code_merge)
        self.assertIsInstance(last_code.kind, CodeCheckpoint)
        request = request_from(argparse.Namespace(
            production=str(self.doc), unit="alpha-unit", phase="2", hash=code,
            also=[], shrink=False, trailer=[], review_trial=TRIAL, delivers=DELIVERS,
            cancel_prior=False, started=STARTED, regime="after", holds=0,
            merge_defects=0, excluded=None, scratch=str(self.root / "scratch"),
        ))
        latest = merge_history(request)
        self.assertIsInstance(latest, LastMerged)
        if isinstance(latest, LastMerged):
            self.assertEqual(latest.hash, code)

    def test_merge_branch_reader_queries_last_any_last_unit_and_code_count(self) -> None:
        alpha_code = self.checkpoint(content="alpha phase two\n")
        _ = self.git("merge", "--no-ff", "-m",
                     f"Merge alpha-unit phase 2 ({alpha_code[:7]}) into production", "alpha-branch")
        alpha_merge = self.git("rev-parse", "HEAD")
        self.write(self.other, "src/beta.txt", "beta phase one\n")
        _ = self.git("add", "src/beta.txt", cwd=self.other)
        _ = self.git("commit", "-m", "beta phase one", cwd=self.other)
        beta_code = self.git("rev-parse", "HEAD", cwd=self.other)
        _ = self.git("merge", "--no-ff", "-m",
                     f"Merge beta-unit phase 1 ({beta_code[:7]}) into production", "beta-branch")
        beta_merge = self.git("rev-parse", "HEAD")
        shrink = self.checkpoint("docs/plans/alpha.md", "short as built\n", subject="shrink")
        _ = self.git("merge", "--no-ff", "-m",
                     f"Merge alpha-unit phase 2 shrink ({shrink[:7]}) into production", "alpha-branch")
        shrink_merge = self.git("rev-parse", "HEAD")

        history = merge_branch_history(self.checkout, "production")
        latest = history.last_merge()
        self.assertNotIsInstance(latest, NoMerge)
        if isinstance(latest, NoMerge):
            self.fail("shrink merge was not read")
        self.assertEqual((latest.unit, latest.phase, latest.short, latest.merge_hash),
                         ("alpha-unit", "2", shrink[:7], shrink_merge))
        self.assertIsInstance(latest.kind, ShrinkCommit)
        alpha = history.last_for_unit("alpha-unit")
        self.assertEqual(alpha, latest)
        alpha_code = history.last_code_for_unit("alpha-unit")
        self.assertNotIsInstance(alpha_code, NoMerge)
        if isinstance(alpha_code, NoMerge):
            self.fail("alpha code merge was not read")
        self.assertEqual(alpha_code.merge_hash, alpha_merge)
        beta = history.last_for_unit("beta-unit")
        self.assertNotIsInstance(beta, NoMerge)
        if isinstance(beta, NoMerge):
            self.fail("beta merge was not read")
        self.assertEqual((beta.unit, beta.merge_hash), ("beta-unit", beta_merge))
        self.assertIsInstance(beta.kind, CodeCheckpoint)
        self.assertEqual(history.code_merge_count(), 2)
        self.assertTrue(history.has_code_merge("alpha-unit", "2"))
        self.assertTrue(history.has_code_merge("beta-unit", "1"))
        self.assertFalse(history.has_code_merge("alpha-unit", "3"))
        self.assertEqual(history.entries[-1].merge_hash, alpha_merge)


if __name__ == "__main__":
    _ = unittest.main()
