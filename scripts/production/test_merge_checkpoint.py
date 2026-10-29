"""Exercise checkpoint merges against disposable repositories and command stubs."""

from __future__ import annotations

import argparse
from contextlib import closing, redirect_stdout
from io import StringIO
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import NamedTuple, cast, final, override

sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_checkpoint
from scripts.production import production_doc
from merge_checkpoint import (ChildHandoffEntry, CodeCheckpoint, LastMerged, MergeEntry, NoMerge, ShrinkCommit,
                              check_parallel, merge_branch_history, merge_history, promote, request_from)

# Importable once merge_checkpoint has put scripts/lib on the path.
import scratch


# Script completion deadlines only bound hangs; startup can be slow under load.
# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

SCRIPT = Path(__file__).with_name("merge_checkpoint.py")
STARTED = "2026-10-06T12:00:00-07:00"
TRIAL = ("review trial: ux 2 findings, code 3 findings, review-worker minutes 14, "
         "ux check minutes 4, ux repair minutes 1")
SIMPLE_OWNS = "`src/alpha.txt`, `crates/alpha/`, `docs/plans/alpha.md`, `docs/plans/alpha-next.md`"
REAL_OWNS = ("`docs/plans/build-followups-enh-showrunner.md`; `scripts/production/add_unit.py`, "
             "`test_add_unit.py`, `merge_checkpoint.py`, `test_merge_checkpoint.py`; "
             "`commands/showrunner/add_unit.md`; after stalls-unit Phase 6 merges: "
             "`commands/showrunner/produce.md`, `commands/showrunner/promote_unit.md`, "
             "`scripts/production/showrunners.py`, `scripts/production/stall_watch.py` and their tests "
             "(hub rows); from its Phase 5 (the user, 2026-10-06 15:25 PDT, the phase-end split): "
             "`commands/unit/direct.md`, `commands/unit/checkpoint.md`, "
             "`docs/direct/run_phase_review.md`, `docs/production_format.md`, "
             "`commands/unit/add_ons.md`, `commands/plan/shrink.md`, "
             "`commands/plan/phase_review.md`, `docs/direct/phase_end.md`, "
             "`docs/direct_plan_format.md`, `commands/unit/eta_breakdown.md`, "
             "`docs/direct/final_gate_commit.md`")
SIMPLE_MERGE_TESTS = "- **Merge tests:** `checkpoint-test`"
REAL_MERGE_TESTS = ("- **Merge tests:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` "
                    "and `basedpyright scripts/buildlog` (pass is its `0 errors, 0 warnings` line: it exits 3 "
                    "because pyrightconfig names a `.venv` no checkout has), run in the showrunner checkout")
DELIVERS = "Delivers checkpoint merge behavior."
PARALLEL = "Parallel: none — no phase is waiting"
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
if name == "style_sync.py":
    import subprocess
    repo = args[args.index("--repo-dir") + 1]
    branch = args[args.index("--branch") + 1]
    observed = subprocess.run(["git", "-C", repo, "ls-remote", "origin", "refs/heads/" + branch],
                              capture_output=True, text=True, check=True).stdout.split()[0]
    (state / "notified-tip").write_text(observed)
if name == "verify.sh":
    fail_rows = state / "fail-rows"
    if fail_rows.exists() and "--filter" not in args:
        print(fail_rows.read_text(), end="")
        raise SystemExit(1)
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
    print("0 errors, 0 warnings, 0 notes", flush=True)
    print("venv .venv subdirectory not found in venv path " + os.getcwd() + ".", file=sys.stderr)
    raise SystemExit(3)
'''


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them. The
    scratch folders a merge removes are in a temporary folder, never /tmp/claude/scratch or ~/rust/repro."""
    folder = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(folder.cleanup)
    unittest.enterModuleContext(mock.patch.dict(os.environ, {
        "USER_ZONE": "America/Los_Angeles", "SCRATCH_ROOT": f"{folder.name}/scratch",
        "SCRATCH_VISIBLE_ROOT": f"{folder.name}/repro"}))


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
        self.known_flaky_tests = ""
        self.gate = True

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.origin = self.root / "origin.git"
        self.checkout = self.root / "hana"
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
        for name in ("verify.sh", "checkpoint-test", "validate_and_push.sh", "review_regime.py", "ssh",
                     "style_sync.py", "style_ledger.py"):
            self.executable(self.bin / name, STUB)
        home = self.root / "home"
        for relative, name in ((".claude/scripts/direct", "verify.sh"),
                               (".claude/scripts/validate_and_push", "validate_and_push.sh"),
                               (".claude/scripts/production", "review_regime.py")):
            directory = home / relative
            directory.mkdir(parents=True)
            self.executable(directory / name, STUB)
        self.env = {**os.environ, "HOME": str(home),
                    "MERGE_CHECKPOINT_STYLE_SYNC": str(self.bin / "style_sync.py"),
                    "MERGE_CHECKPOINT_STYLE_LEDGER": str(self.bin / "style_ledger.py"),
                    "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                    "CHECKPOINT_TEST_STATE": str(self.state),
                    "MERGE_CHECKPOINT_SCRATCH": str(self.state),
                    "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
                    "SHOWRUNNER_STATE_DIR": str(self.root / "showrunners"),
                    "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "", "PLAN_DELEGATE_BOARD_DIR": "",
                    "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": "",
                    "MERGE_CHECKPOINT_RUN_ROOT": str(self.root / "unit-runs"),
                    "UNIT_HISTORY_DIR": str(self.root / "history"), "UNIT_SYNC_WRITES": "0",
                    "CLAUDE_PROBE_SESSION": ""}
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
                       hub_files: bool = True, change_review: Path | None = None) -> None:
        rows = [
            "# Production — example", "", "## Production Context", "",
            f"- **Repository:** `{self.checkout}`",
            "- **Merge branch:** `production`",
            *([f"- **Change review:** `{change_review}`"] if change_review else []),
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
        if self.known_flaky_tests:
            rows.append(f"- **Known flaky tests:** {self.known_flaky_tests}")
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
                       delivers: str | None = DELIVERS, parallel: str | None = PARALLEL) -> MergeResult:
        before = self.git("rev-parse", "HEAD")
        arguments = ["--production", str(self.doc), "alpha-unit", phase, sha,
                     "--started", STARTED, "--scratch", str(self.root / "scratch"),
                     "--trailer", "Co-authored-by: Test <test@example.invalid>"]
        if review:
            arguments.extend(("--review-trial", trial))
        if parallel is not None and "--shrink" not in extra and "--as-built" not in extra:
            arguments.extend(("--parallel", parallel))
        if delivers is not None and "--shrink" not in extra and "--as-built" not in extra:
            arguments.extend(("--delivers", delivers))
        arguments.extend(extra)
        process = subprocess.run([sys.executable, str(SCRIPT), *arguments], cwd=self.checkout,
                                 env=self.env, capture_output=True, text=True, check=False, timeout=300)
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

    def receipts(self) -> list[dict[str, object]]:
        path = self.root / "history" / "merge_receipts.jsonl"
        return [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def test_checkpoint_receipt_names_pushed_merge_and_source_once_on_retry(self) -> None:
        checkpoint = self.checkpoint()
        first = self.run_checkpoint(checkpoint)
        self.assertEqual(first.process.returncode, 0, first.process.stdout + first.process.stderr)
        merge = self.git("rev-parse", "HEAD")
        receipt, = self.receipts()
        self.assertEqual((receipt["project"], receipt["phase_id"], receipt["merge_commit"],
                          receipt["source_checkpoint"], receipt["target_branch"]),
                         ("hana", "2", merge, checkpoint, "production"))
        self.assertEqual(receipt["timestamp_epoch"], float(self.git("show", "-s", "--format=%ct", merge)))
        self.assertTrue(str(receipt["merged_at"]).endswith("+00:00"))
        retried = self.run_checkpoint(checkpoint)
        self.assertEqual(retried.process.returncode, 0, retried.process.stdout + retried.process.stderr)
        self.assertEqual(self.receipts(), [receipt])
        readback = subprocess.run([sys.executable, str(SCRIPT.parent.parent / "direct" / "progress_history.py"),
                                   "receipts", "--project", "hana", "--merge-commit", merge], cwd=self.checkout, env=self.env,
                                  capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(readback.returncode, 0, readback.stderr)
        report = cast(dict[str, object], json.loads(readback.stdout))
        self.assertEqual((report["covered_n"], report["missing_n"], report["receipts"]), (1, 0, [receipt]))

    def test_rolled_back_checkpoint_writes_no_receipt(self) -> None:
        checkpoint = self.checkpoint()
        _ = (self.state / "fail-checkpoint-test").touch()
        failed = self.run_checkpoint(checkpoint)
        self.assertNotEqual(failed.process.returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), failed.before)
        self.assertEqual(self.receipts(), [])

    def test_abbreviated_checkpoint_keeps_full_hash_wait_attribution_and_receipt(self) -> None:
        checkpoint = self.checkpoint()
        short = self.git("rev-parse", "--short", checkpoint, cwd=self.unit)
        session = self.start_history_run("abbreviated-checkpoint", self.unit)
        _ = self.record_history("start-phase", "--session-dir", str(session), "--phase-id", "2", "--phase-title", "Gate")
        _ = self.record_history("checkpoint", "--session-dir", str(session), "--phase", "2", "--commit", checkpoint)
        _ = self.record_history("finish-phase", "--session-dir", str(session), "--phase-id", "2", "--status", "completed")

        merged = self.run_checkpoint(short)
        self.assertEqual(merged.process.returncode, 0, merged.process.stdout + merged.process.stderr)
        report = self.record_history("waits")
        self.assertEqual(report["attribution_miss_n"], 0)
        wait, = cast(list[dict[str, object]], report["waits"])
        attribution = cast(dict[str, object], wait["wait_attribution"])
        self.assertEqual(attribution["state"], "CheckpointPhase")
        self.assertEqual(wait["checkpoint_commit"], checkpoint)
        phase, = cast(list[dict[str, object]], report["phases"])
        self.assertEqual(wait["phase_instance_id"], phase["phase_instance_id"])
        receipt, = self.receipts()
        self.assertEqual(receipt["source_checkpoint"], checkpoint)

        retried = self.run_checkpoint(checkpoint)
        self.assertEqual(retried.process.returncode, 0, retried.process.stdout + retried.process.stderr)
        self.assertEqual(self.receipts(), [receipt])

    def test_child_receipt_survives_retry_and_rebuilds_a_missed_write(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
        first = self.run_handoff(self.git("rev-parse", "--short", checkpoint, cwd=self.unit))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        merge = self.git("rev-parse", "HEAD")
        receipt, = self.receipts()
        self.assertEqual((receipt["merge_commit"], receipt["source_checkpoint"], receipt["issue"]),
                         (merge, checkpoint, "example-style hand-off"))
        retried = self.run_handoff(checkpoint)
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertEqual(self.receipts(), [receipt])
        (self.root / "history" / "merge_receipts.jsonl").unlink()
        recovered = self.run_handoff(checkpoint)
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assertEqual(self.receipts(), [receipt])

    def test_receipt_reader_names_a_merge_that_has_no_receipt(self) -> None:
        checkpoint = self.checkpoint()
        _ = self.git("merge", "--no-ff", "-m", "fixture merge without recording", checkpoint)
        merge = self.git("rev-parse", "HEAD")
        result = subprocess.run([sys.executable, str(SCRIPT.parent.parent / "direct" / "progress_history.py"),
                                 "receipts", "--merge-commit", merge], cwd=self.checkout, env=self.env,
                                capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = cast(dict[str, object], json.loads(result.stdout))
        self.assertEqual((report["covered_n"], report["missing_n"], report["missing"]),
                         (0, 1, [{"state": "ReceiptMissing", "merge_commit": merge}]))

    def test_gate_wait_uses_the_units_named_phase_and_closes_on_red(self) -> None:
        checkpoint = self.checkpoint()
        session = self.root / "unit-runs" / "alpha-run"
        recorder = SCRIPT.parent.parent / "direct" / "progress_history.py"
        for arguments in (
            ["start-run", "--session-dir", str(session), "--working-dir", str(self.unit),
             "--main-family", "codex", "--main-model", "fixture", "--main-effort", "high",
             "--main-session-id", "unit-director"],
            ["start-phase", "--session-dir", str(session), "--phase-id", "2", "--phase-title", "Gate"],
            ["checkpoint", "--session-dir", str(session), "--phase", "2", "--commit", checkpoint],
            ["start-phase", "--session-dir", str(session), "--phase-id", "3", "--phase-title", "Parallel"],
        ):
            result = subprocess.run([sys.executable, str(recorder), *arguments], cwd=self.unit, env=self.env,
                                    capture_output=True, text=True, check=False, timeout=300)
            self.assertEqual(result.returncode, 0, result.stderr)
        _ = (self.state / "fail-checkpoint-test").touch()
        failed = self.run_checkpoint(checkpoint)
        self.assertNotEqual(failed.process.returncode, 0)
        rows = [cast(dict[str, object], json.loads(line)) for line in
                (self.root / "history" / "runs" / "alpha-run.jsonl").read_text().splitlines()]
        rows.extend(cast(dict[str, object], json.loads(line)) for line in
                    (self.root / "history" / "wait_attribution.jsonl").read_text().splitlines())
        started, = [row for row in rows if row["event_type"] == "wait_started"]
        finished, = [row for row in rows if row["event_type"] == "wait_finished"]
        phase, = [row for row in rows if row["event_type"] == "phase_started" and row["phase_id"] == "2"]
        self.assertEqual((started["wait_kind"], started["phase_instance_id"], started["unit"], started["owner_session"]),
                         ("WaitingForMergeGate", phase["phase_instance_id"], "alpha-unit", "unit-director"))
        self.assertEqual(started["wait_id"], finished["wait_id"])
        self.assertEqual(cast(dict[str, object], finished["wait_end"])["state"], "Finished")

    def test_merge_after_phase_finish_records_the_checkpoint_instances_gate_wait(self) -> None:
        checkpoint = self.checkpoint()
        session = self.root / "unit-runs" / "finished-alpha"
        recorder = SCRIPT.parent.parent / "direct" / "progress_history.py"
        for arguments in (
            ["start-run", "--session-dir", str(session), "--working-dir", str(self.unit),
             "--main-family", "codex", "--main-model", "fixture", "--main-session-id", "unit-director"],
            ["start-phase", "--session-dir", str(session), "--phase-id", "2", "--phase-title", "Finished"],
            ["checkpoint", "--session-dir", str(session), "--phase", "2", "--commit", checkpoint],
            ["finish-phase", "--session-dir", str(session), "--phase-id", "2", "--status", "completed"],
        ):
            recorded = subprocess.run([sys.executable, str(recorder), *arguments], env=self.env, cwd=self.unit,
                                      capture_output=True, text=True, check=False, timeout=300)
            self.assertEqual(recorded.returncode, 0, recorded.stderr)
        merged = self.run_checkpoint(checkpoint)
        self.assertEqual(merged.process.returncode, 0, merged.process.stdout + merged.process.stderr)
        read = subprocess.run([sys.executable, str(recorder), "waits", "--session-dir", str(session)],
                              env=self.env, cwd=self.unit, capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(read.returncode, 0, read.stderr)
        report = cast(dict[str, object], json.loads(read.stdout))
        wait, = cast(list[dict[str, object]], report["waits"])
        phase, = cast(list[dict[str, object]], report["phases"])
        self.assertEqual((wait["wait_kind"], wait["phase_instance_id"], phase["wait_n"]),
                         ("WaitingForMergeGate", phase["phase_instance_id"], 1))
        self.assertEqual(cast(dict[str, object], wait["wait_end"])["state"], "Finished")

    def record_history(self, *arguments: str) -> dict[str, object]:
        recorder = SCRIPT.parent.parent / "direct" / "progress_history.py"
        result = subprocess.run([sys.executable, str(recorder), *arguments], cwd=self.unit, env=self.env,
                                capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        return cast(dict[str, object], json.loads(result.stdout)) if arguments[0] == "waits" else {}

    def start_history_run(self, name: str, worktree: Path) -> Path:
        session = self.root / "unit-runs" / name
        _ = self.record_history("start-run", "--session-dir", str(session), "--working-dir", str(worktree),
                                "--main-family", "codex", "--main-model", "fixture", "--main-session-id", "director")
        return session

    def assert_checkpoint_provenance(self, *, across_runs: bool, second_active: bool, checkpoint_events: int) -> None:
        checkpoint = self.checkpoint()
        first = self.start_history_run("first", self.unit)
        _ = self.record_history("start-phase", "--session-dir", str(first), "--phase-id", "2", "--phase-title", "First")
        if checkpoint_events:
            _ = self.record_history("checkpoint", "--session-dir", str(first), "--phase", "2", "--commit", checkpoint)
        _ = self.record_history("finish-phase", "--session-dir", str(first), "--phase-id", "2", "--status", "completed")
        if across_runs:
            _ = self.record_history("finish-run", "--session-dir", str(first), "--status", "completed")
        second = self.start_history_run("second", self.unit) if across_runs else first
        _ = self.record_history("start-phase", "--session-dir", str(second), "--phase-id", "2", "--phase-title", "Second")
        if checkpoint_events == 2:
            _ = self.record_history("checkpoint", "--session-dir", str(second), "--phase", "2", "--commit", checkpoint)
        if not second_active:
            _ = self.record_history("finish-phase", "--session-dir", str(second), "--phase-id", "2", "--status", "completed")
        merged = self.run_checkpoint(checkpoint)
        self.assertEqual(merged.process.returncode, 0, merged.process.stdout + merged.process.stderr)
        report = self.record_history("waits")
        phases = cast(list[dict[str, object]], report["phases"])
        waits = cast(list[dict[str, object]], report["waits"])
        self.assertEqual(len(phases), 2)
        if checkpoint_events == 1:
            self.assertEqual(len(waits), 1)
            self.assertEqual((waits[0]["run_id"], waits[0]["phase_instance_id"]),
                             (phases[0]["run_id"], phases[0]["phase_instance_id"]))
            self.assertEqual([phase["wait_n"] for phase in phases], [1, 0])
        else:
            self.assertEqual(waits, [])
            self.assertEqual(report["missing_record_n"], 1)
            for phase in phases:
                self.assertEqual(phase["state"], "UncertainCoverage")
                self.assertNotIn("wait_n", phase)
                self.assertTrue(phase["reason"])
                self.assertTrue(phase["candidates"])

    def test_checkpoint_event_selects_the_first_repeated_phase_instance(self) -> None:
        for across_runs in (False, True):
            for second_active in (False, True):
                with self.subTest(across_runs=across_runs, second_active=second_active):
                    # Each matrix cell owns a disposable repository and history root.
                    fixture = MergeCheckpointTests("runTest")
                    fixture.setUp()
                    try:
                        fixture.assert_checkpoint_provenance(across_runs=across_runs, second_active=second_active,
                                                             checkpoint_events=1)
                    finally:
                        self.assertTrue(fixture.doCleanups())

    def test_missing_or_duplicate_checkpoint_events_make_every_candidate_uncertain(self) -> None:
        for across_runs in (False, True):
            for second_active in (False, True):
                for checkpoint_events in (0, 2):
                    with self.subTest(across_runs=across_runs, second_active=second_active, checkpoint_events=checkpoint_events):
                        fixture = MergeCheckpointTests("runTest")
                        fixture.setUp()
                        try:
                            fixture.assert_checkpoint_provenance(across_runs=across_runs, second_active=second_active,
                                                                 checkpoint_events=checkpoint_events)
                        finally:
                            self.assertTrue(fixture.doCleanups())

    def test_child_handoff_wait_has_production_ownership_without_phase_coverage(self) -> None:
        _ = self.child_production()
        # A real child run may have phases; its branch checkpoint still belongs to the production.
        session = self.start_history_run("child", self.unit)
        _ = self.record_history("start-phase", "--session-dir", str(session), "--phase-id", "2", "--phase-title", "Child unit")
        checkpoint = self.checkpoint()
        merged = self.run_handoff(checkpoint)
        self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
        report = self.record_history("waits")
        wait, = cast(list[dict[str, object]], report["waits"])
        attribution = cast(dict[str, object], wait["wait_attribution"])
        self.assertEqual(attribution["state"], "ProductionHandoff")
        self.assertTrue(attribution["reason"])
        self.assertNotIn("phase_instance_id", wait)
        self.assertEqual(report["missing_record_n"], 0)
        phase, = cast(list[dict[str, object]], report["phases"])
        self.assertEqual((phase["state"], phase["wait_n"]), ("Covered", 0))

    def test_missing_hash_and_unpushed_checkpoint_stop_at_ancestry(self) -> None:
        missing = self.run_checkpoint("0" * 40)
        self.assert_stopped_before_merge(missing, "failed", "commit")
        self.assertIn("push", missing.process.stdout)
        sha = self.checkpoint(push=False)
        unpushed = self.run_checkpoint(sha)
        self.assert_stopped_before_merge(unpushed, "held", "origin")
        self.assertIn("push", unpushed.process.stdout)

    def test_never_pushed_unit_branch_is_held_like_an_unpushed_checkpoint(self) -> None:
        sha = self.checkpoint(push=False)
        unpushed = self.run_checkpoint(sha)
        _ = self.git("push", "origin", "--delete", "alpha-branch", cwd=self.unit)
        never_pushed = self.run_checkpoint(sha)
        self.assert_stopped_before_merge(never_pushed, "held", "ancestry: held — commit is not on origin")
        self.assertIn("send alpha-unit: push your branch to origin and resend the checkpoint",
                      never_pushed.process.stdout)
        self.assertEqual(never_pushed.process.stdout, unpushed.process.stdout)

    def test_unreachable_origin_still_fails_at_ancestry(self) -> None:
        sha = self.checkpoint()
        _ = self.git("remote", "set-url", "origin", str(self.root / "absent.git"))
        result = self.run_checkpoint(sha)
        self.assert_stopped_before_merge(result, "failed", "ancestry: failed")
        self.assertIn("send alpha-unit: the showrunner will inspect the failed command", result.process.stdout)

    def test_scope_holds_unowned_path_and_allows_explicit_also(self) -> None:
        sha = self.checkpoint("outside.txt", "unowned\n")
        held = self.run_checkpoint(sha)
        self.assert_stopped_before_merge(held, "held", "outside.txt")
        accepted = self.run_checkpoint(sha, "--also", "outside.txt")
        self.assertEqual(accepted.process.returncode, 0, accepted.process.stderr + accepted.process.stdout)
        self.assertIn("scope: ok", accepted.process.stdout)

    def test_a_code_checkpoint_removes_its_phases_scratch_folders(self) -> None:
        with mock.patch.dict(os.environ, self.env):
            done = [scratch.new("phase:alpha-unit/2", word) for word in ("gate", "copy")]
            later = scratch.new("phase:alpha-unit/3")
        result = self.run_checkpoint(self.checkpoint())
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertIn("scratch: ok — removed 2 folder(s) of phase:alpha-unit/2: ", result.process.stdout)
        self.assertFalse(any(path.exists() for path in done))
        self.assertTrue(later.is_dir())

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

    def test_real_owns_cell_allows_a_path_in_the_last_group(self) -> None:
        self.assert_real_owns_path_allowed("commands/unit/direct.md")

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
        self.assertEqual(self.events("python3"), [
            ["-m", "unittest", "discover", "-s", "scripts/buildlog", "-p", "test_*.py"],
            [str(self.bin / "style_sync.py"), "notify", "--repo-dir", str(self.checkout),
             "--branch", "production", "--tip", self.git("rev-parse", "HEAD")],
        ])
        self.assertEqual(self.events("basedpyright"), [["scripts/buildlog"]])
        self.assertEqual(self.events("0"), [])
        all_args = [arg for event in (self.events("python3") + self.events("basedpyright")) for arg in event]
        self.assertFalse(any(".venv" in arg for arg in all_args))
        self.assertIn("test: ok", result.process.stdout)

    def test_merge_tests_that_are_not_commands_are_refused_by_name_before_the_merge(self) -> None:
        """cargo-liner wrote packages and a crate directory where the line wants commands."""
        (self.checkout / "crates/tui_pane").mkdir(parents=True)
        sha = self.checkpoint()
        for entry, detail in (("crates/tui_pane", "crates/tui_pane is neither on PATH"),
                              ("tui-pane-package", "tui-pane-package is neither on PATH"),
                              ("checkpoint-executable-that-does-not-exist", "does-not-exist is neither on PATH"),
                              ("$MERGE_TEST_UNSET/bin/tool --all", "names $MERGE_TEST_UNSET, which is not set")):
            with self.subTest(entry=entry):
                self.production_doc(merge_tests_line=f"- **Merge tests:** `checkpoint-test`, `{entry}`")
                result = self.run_checkpoint(sha)
                self.assertEqual(result.process.returncode, 1)
                self.assert_stopped_before_merge(result, "failed", f"Merge tests entry `{entry}`")
                self.assertIn(detail, result.process.stdout)

    def test_merge_tests_expand_home_and_variables(self) -> None:
        """The commands run with no shell, so ~ and $VAR are expanded before they run."""
        tools = Path(self.env["HOME"]) / "tools"
        tools.mkdir()
        self.executable(tools / "merge-tool", STUB)
        self.env["MERGE_TEST_DIR"] = "scripts/buildlog"
        self.production_doc(merge_tests_line="- **Merge tests:** `~/tools/merge-tool $MERGE_TEST_DIR`, "
                                             + "`$HOME/tools/merge-tool ${MERGE_TEST_DIR}/one ~/two`")
        sha = self.checkpoint()
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertEqual(self.events("merge-tool"),
                         [["scripts/buildlog"], ["scripts/buildlog/one", str(tools.parent / "two")]])

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

    def test_code_checkpoint_requires_a_known_parallel_line(self) -> None:
        sha = self.checkpoint()
        for parallel, expected in (
            (None, "requires --parallel"),
            ("Parallel: unknown — Phase 5 has no **Depends on:** line; add one under each phase heading and rerun",
             "adds a **Depends on:** line"),
            ("Parallel: maybe later", "--parallel must read"),
        ):
            result = self.run_checkpoint(sha, parallel=parallel)
            self.assertEqual(result.process.returncode, 1)
            self.assert_stopped_before_merge(result, "failed", expected)
        for parallel in ("Parallel: none — Phase 4 needs Phase 3 (running)", "Parallel: Phase 4, Phase 6 could start"):
            with self.subTest(parallel=parallel):
                check_parallel(parallel)

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

    def dated_checkpoint(self, message: str) -> str:
        """A checkpoint(...) commit on the unit branch, dated after the Change-Review rule landed."""
        self.write(self.unit, "src/alpha.txt", message)
        _ = self.git("add", "src/alpha.txt", cwd=self.unit)
        _ = subprocess.run(["git", "commit", "-q", "-m", message], cwd=self.unit, check=True,
                           env={**os.environ, "GIT_COMMITTER_DATE": "2026-10-15T12:00:00-04:00"})
        _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
        return self.git("rev-parse", "HEAD", cwd=self.unit)

    def test_a_checkpoint_without_a_change_review_line_is_held_before_merge(self) -> None:
        sha = self.dated_checkpoint("checkpoint(alpha): grid fades")
        result = self.run_checkpoint(sha)
        self.assertNotEqual(result.process.returncode, 0)
        self.assert_stopped_before_merge(result, "held", "checkpoint(alpha): grid fades: no Change-Review line")
        self.assertIn("Change-Review-Covers", result.process.stdout)

    def test_a_merge_adds_a_row_per_visible_change_to_the_named_list(self) -> None:
        review = self.root / "change-review.md"
        _ = review.write_text("## To review\n\n| # | Change | Merged | On the Mac build | How to check |\n"
                              + "| --- | --- | --- | --- | --- |\n| C7 | old | 2026-10-14 | yes | - |\n", encoding="utf-8")
        self.production_doc(change_review=review)
        sha = self.dated_checkpoint("checkpoint(alpha): grid fades\n\n"
                                    + "Change-Review: The grid fades gently | Run `hana` in a natedev terminal | The grid lines fade out farther away\n"
                                    + "Change-Review: none — the merge script")
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        merged = self.git("rev-parse", "--short=8", "HEAD")
        self.assertRegex(review.read_text(encoding="utf-8").splitlines()[-1],
                         rf"^\| C8 \| The grid fades gently \| \d{{4}}-\d\d-\d\d · {merged} \| not yet \| Run `hana` in a natedev terminal\. You see: The grid lines fade out farther away \| example \|$")
        self.assertIn("change review: ok — 1 row(s) added", result.process.stdout)
        self.assertNotIn("change review: drift", result.process.stdout)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_a_production_without_a_list_writes_no_rows(self) -> None:
        sha = self.dated_checkpoint("checkpoint(alpha): grid fades\n\nChange-Review: none — the merge script")
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertIn("change review: ok — the production doc has no Change review line; no rows written",
                      result.process.stdout)

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
        self.assertEqual(self.events("style_sync.py"), [])

    def test_style_notification_observes_the_successfully_pushed_tip(self) -> None:
        result = self.run_checkpoint(self.checkpoint())
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        tip = self.git("rev-parse", "HEAD")
        self.assertEqual(self.events("style_sync.py"), [[
            "notify", "--repo-dir", str(self.checkout), "--branch", "production", "--tip", tip,
        ]])
        self.assertEqual((self.state / "notified-tip").read_text(), tip)

    def test_style_notification_failure_does_not_fail_a_pushed_checkpoint(self) -> None:
        _ = (self.state / "fail-style_sync.py").touch()
        result = self.run_checkpoint(self.checkpoint())
        self.assertEqual(result.process.returncode, 0, result.process.stdout + result.process.stderr)
        self.assertIn("style sync: exit 1", result.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.git("rev-parse", "production", cwd=self.origin))

    def test_notification_value_error_leaves_the_completed_checkpoint_successful(self) -> None:
        sha = self.checkpoint()
        original_notify = merge_checkpoint.notify_style

        def malformed_notification(checkout: Path, branch: str, tip: str) -> None:
            with mock.patch("merge_checkpoint.subprocess.run", side_effect=ValueError("invalid production")):
                original_notify(checkout, branch, tip)

        arguments = [str(SCRIPT), "--production", str(self.doc), "alpha-unit", "2", sha,
                     "--started", STARTED, "--scratch", str(self.root / "scratch"),
                     "--review-trial", TRIAL, "--delivers", DELIVERS, "--parallel", PARALLEL]
        output = StringIO()
        with mock.patch.dict(os.environ, self.env), mock.patch.object(sys, "argv", arguments), mock.patch.object(
            merge_checkpoint, "notify_style", side_effect=malformed_notification,
        ), redirect_stdout(output):
            code = merge_checkpoint.main()
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("style sync: invalid production", output.getvalue())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.git("rev-parse", "production", cwd=self.origin))

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

    def test_a_docs_path_under_a_workspace_root_runs_no_package_test(self) -> None:
        self.write(self.unit, "crates/alpha/Cargo.toml", '[workspace]\nmembers = ["crates/*"]\n')
        self.write(self.unit, "crates/alpha/docs/as-built/pane.md", "# Pane\n")
        _ = self.git("add", "crates/alpha", cwd=self.unit)
        _ = self.git("commit", "-m", "docs only", cwd=self.unit)
        sha = self.git("rev-parse", "HEAD", cwd=self.unit)
        _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
        _ = (self.state / "fail-always-test").touch()
        result = self.run_checkpoint(sha)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertEqual(self.events("verify.sh"), [])

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

    def test_known_flaky_tests_rerun_alone_and_an_unlisted_failure_stays_red(self) -> None:
        """cargo-liner-style: a whole verify.sh merge test where two known flaky tests failed."""
        reader = "shim_registration::reader_scenarios::reader_attributes_compiler_cache"
        locale = "shim_registration::reader_scenarios::reader_accepts_live_writer_under_different_locale"
        rows = "".join(f"        FAIL [  12.202s] ({count}/1067) cargo-tile::unit_tests {test}\n"
                       for count, test in ((1012, reader), (1013, locale)))
        # nextest prints each failure again after its Summary line.
        _ = (self.state / "fail-rows").write_text(rows + "     Summary [ 80.1s] 1067 tests run\n" + rows)
        sha = self.checkpoint()
        self.known_flaky_tests = f"`{reader}`"
        self.production_doc(merge_tests_line="- **Merge tests:** `verify.sh test cargo-tile`")
        failed = self.run_checkpoint(sha)
        self.assertIn("red: failed", failed.process.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), failed.before)
        self.assertEqual(self.events("verify.sh"), [["test", "cargo-tile"], ["test", "cargo-tile"]])
        (self.state / "events.jsonl").unlink()
        self.known_flaky_tests = f"`{reader}`, `{locale}` — red only under load"
        self.production_doc(merge_tests_line="- **Merge tests:** `verify.sh test cargo-tile`")
        passed = self.run_checkpoint(sha)
        self.assertEqual(passed.process.returncode, 0, passed.process.stderr + passed.process.stdout)
        self.assertEqual(self.events("verify.sh"), [
            ["test", "cargo-tile"],
            ["test", "cargo-tile", "--filter", locale, "--filter", reader],
        ])
        self.assertIn(f"known flaky tests {locale}, {reader}: red in merge test 1, green alone",
                      self.log.read_text(encoding="utf-8"))

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
        self.assertIn(f"promote: ok — {self.promote} already at {pushed}\n", retried.process.stdout)

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
        self.assertIn(f"promote: ok — {self.promote} main at {pushed}; "
                      + "main holds commits not on origin/production and was not pushed\n",
                      rerun.process.stdout)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), pushed)

    def test_promote_at_tip_with_foreign_remote_history_says_already_at_without_push(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        sha = self.checkpoint()
        first = self.run_checkpoint(sha)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        tip = self.git("rev-parse", "HEAD")
        base = self.git("rev-parse", "HEAD^1")
        _ = self.git("push", "--force", "origin", f"{base}:refs/heads/main")
        _ = self.git("push", "--force", "origin", f"{base}:refs/heads/production")
        with mock.patch.dict(os.environ, {"PATH": self.env["PATH"]}):  # Merge tests names a stub on the test PATH
            request = request_from(argparse.Namespace(
                production=str(self.doc), unit="alpha-unit", phase="2", hash=sha,
                also=[], shrink=False, as_built=False, trailer=[], review_trial=TRIAL, delivers=DELIVERS, parallel=PARALLEL,
                cancel_prior=False, started=STARTED, regime="after", holds=0,
                merge_defects=0, excluded=None, scratch=str(self.root / "scratch"),
            ))
        output = StringIO()
        with redirect_stdout(output):
            promote(request, tip)
        self.assertIn(f"promote: ok — {self.promote} already at {tip}; "
                      + "main holds commits not on origin/production and was not pushed\n", output.getvalue())
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), base)

    def test_promote_reports_already_at_tip_when_both_mains_match(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        sha = self.checkpoint()
        first = self.run_checkpoint(sha)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        tip = self.git("rev-parse", "refs/heads/main", cwd=self.origin)
        second = self.run_checkpoint(sha)
        self.assertEqual(second.process.returncode, 0, second.process.stderr + second.process.stdout)
        self.assertIn(f"promote: ok — {self.promote} already at {tip}\n", second.process.stdout)
        self.assertEqual(self.git("rev-parse", "refs/heads/main", cwd=self.origin), tip)

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
        self.assertIn(f"promote: ok — {self.promote} main at {tip}\n", result.process.stdout)
        self.assertIn(f"promote: ok — mac {self.root / 'mac-installed'} main pulled\n",
                      result.process.stdout)
        self.assertEqual(self.git("merge-base", "--is-ancestor", sha, tip), "")
        self.assertIn("main-only.txt", self.git("ls-tree", "-r", "--name-only", tip))
        receipts = self.receipts()
        self.assertEqual({row["merge_commit"] for row in receipts},
                         set(self.git("rev-list", "--first-parent", "--merges", "HEAD").splitlines()))
        integration, = [row for row in receipts if row["merge_commit"] == tip]
        self.assertEqual(integration["source_checkpoint"], self.git("rev-parse", "HEAD^2"))
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
            "add", "--unit", "alpha-unit", "--plan", "docs/plans/alpha.md", "--phase", "2", "--regime", "trial",
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

    def test_a_merge_killed_before_its_push_is_tested_before_a_rerun_pushes_it(self) -> None:
        # Killed after the merge and before its tests finished: nothing proves the merge green.
        sha = self.checkpoint()
        before = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        subject = f"Merge alpha-unit phase 2 ({sha[:7]}) into production"
        _ = self.git("merge", "--no-ff", "-q", "-m", subject, sha, cwd=self.checkout)
        _ = (self.state / "fail-checkpoint-test").touch()
        red = self.run_checkpoint(sha)
        self.assertEqual(red.process.returncode, 1, red.process.stderr + red.process.stdout)
        self.assertIn("red: failed", red.process.stdout)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), before)
        (self.state / "fail-checkpoint-test").unlink()
        green = self.run_checkpoint(sha)
        self.assertEqual(green.process.returncode, 0, green.process.stderr + green.process.stdout)
        self.assertNotEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), before)

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

    def test_shrink_after_criss_cross_merge_scopes_only_its_doc(self) -> None:
        code = self.checkpoint()
        self.write(self.checkout, "src/beta.txt", "beta on the merge branch\n")
        _ = self.git("commit", "-am", "beta lands first")
        _ = self.git("push", "origin", "production")
        _ = self.git("merge", "--no-ff", "-m", "merge production", "production", cwd=self.unit)
        _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
        first = self.run_checkpoint(code)
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        shrink = self.checkpoint("docs/plans/alpha.md", "short as built\n", subject="shrink")
        bases = self.git("merge-base", "--all", "production", shrink).splitlines()
        self.assertEqual(len(bases), 2, bases)
        result = self.run_checkpoint(shrink, "--shrink", review=False)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertIn("scope: ok — 1 changed paths are allowed", result.process.stdout)

    def test_promote_none_merges_and_records_without_promotion(self) -> None:
        self.promotions = "none"
        self.production_doc()
        result = self.run_checkpoint(self.checkpoint())
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertIn("promote: ok — no promotion configured", result.process.stdout)
        self.assertIn("record: ok", result.process.stdout)

    def test_a_missing_promote_checkout_is_refused_before_anything_is_pushed(self) -> None:
        self.promotions = str(self.unit / "no-such-checkout")
        self.production_doc()
        before = self.git("rev-parse", "refs/heads/production", cwd=self.origin)
        result = self.run_checkpoint(self.checkpoint())
        self.assertNotEqual(result.process.returncode, 0)
        self.assertIn("no-such-checkout does not exist", result.process.stdout + result.process.stderr)
        self.assertEqual(self.git("rev-parse", "refs/heads/production", cwd=self.origin), before)

    def as_built_commit(self, *extra: str) -> str:
        """Land the plan through a shrink, then delete it beside a new as-built doc."""
        shrink = self.checkpoint("docs/plans/alpha.md", "plan\n", subject="shrink")
        landed = self.run_checkpoint(shrink, "--shrink", review=False)
        self.assertEqual(landed.process.returncode, 0, landed.process.stderr + landed.process.stdout)
        _ = self.git("rm", "-q", "docs/plans/alpha.md", cwd=self.unit)
        self.write(self.unit, "docs/as-built/alpha.md", "as built\n")
        for path in extra:
            self.write(self.unit, path, "extra\n")
        _ = self.git("add", "docs/as-built/alpha.md", *extra, cwd=self.unit)
        _ = self.git("commit", "-m", "docs(alpha): as-built", cwd=self.unit)
        _ = self.git("push", "origin", "alpha-branch", cwd=self.unit)
        return self.git("rev-parse", "HEAD", cwd=self.unit)

    def test_as_built_merges_the_docs_and_the_plans_deletion_after_code_checkpoint(self) -> None:
        first = self.run_checkpoint(self.checkpoint())
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        result = self.run_checkpoint(self.as_built_commit(), "--as-built", review=False)
        self.assertEqual(result.process.returncode, 0, result.process.stderr + result.process.stdout)
        self.assertIn("test: ok — as-built has no package tests", result.process.stdout)
        tree = self.git("ls-tree", "-r", "--name-only", "HEAD")
        self.assertIn("docs/as-built/alpha.md", tree)
        self.assertNotIn("docs/plans/alpha.md", tree)
        self.assertRegex(self.git("log", "-1", "--format=%s"), r"^Merge alpha-unit phase \S+ as-built \(")

    def test_as_built_holds_a_commit_that_changes_code(self) -> None:
        first = self.run_checkpoint(self.checkpoint())
        self.assertEqual(first.process.returncode, 0, first.process.stderr + first.process.stdout)
        result = self.run_checkpoint(self.as_built_commit("src/alpha.rs"), "--as-built", review=False)
        self.assertNotEqual(result.process.returncode, 0)
        self.assertIn("an as-built commit carries only docs", result.process.stdout)

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

    def test_merge_branch_reader_ignores_checkpoint_subject_on_merged_side_branch(self) -> None:
        _ = self.checkpoint(subject="Merge alpha-unit phase 9 (abcdef0) into production")
        _ = self.git("merge", "--no-ff", "-m", "Bring side branch into production", "alpha-branch")

        history = merge_branch_history(self.checkout, "production")
        self.assertIsInstance(history.last_merge(), NoMerge)
        self.assertIsInstance(history.last_for_unit("alpha-unit"), NoMerge)
        self.assertIsInstance(history.last_code_for_unit("alpha-unit"), NoMerge)
        self.assertEqual(history.code_merge_count(), 0)
        self.assertFalse(history.has_code_merge("alpha-unit", "9"))

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
        with mock.patch.dict(os.environ, {"PATH": self.env["PATH"]}):  # Merge tests names a stub on the test PATH
            request = request_from(argparse.Namespace(
                production=str(self.doc), unit="alpha-unit", phase="2", hash=code,
                also=[], shrink=False, as_built=False, trailer=[], review_trial=TRIAL, delivers=DELIVERS, parallel=PARALLEL,
                cancel_prior=False, started=STARTED, regime="after", holds=0,
                merge_defects=0, excluded=None, scratch=str(self.root / "scratch"),
            ))
        latest = merge_history(request)
        self.assertIsInstance(latest, LastMerged)
        if isinstance(latest, LastMerged):
            self.assertEqual(latest.hash, code)

    def test_merge_history_follows_a_unit_rename_commit(self) -> None:
        code = self.checkpoint()
        _ = self.git("merge", "--no-ff", "-m",
                     f"Merge old-alpha-unit phase 2 ({code[:7]}) into production", "alpha-branch")
        _ = self.git("commit", "--allow-empty", "-m", "chore(example): rename unit old-alpha-unit to alpha-unit")
        history = merge_branch_history(self.checkout, "production")
        entry = history.last_code_for_unit("alpha-unit")
        if isinstance(entry, NoMerge):
            self.fail("the merge before the rename was not carried to the new name")
        self.assertEqual((entry.unit, entry.phase, entry.short), ("alpha-unit", "2", code[:7]))
        self.assertTrue(history.has_code_merge("alpha-unit", "2"))
        self.assertIsInstance(history.last_for_unit("old-alpha-unit"), NoMerge)
        with mock.patch.dict(os.environ, {"PATH": self.env["PATH"]}):  # Merge tests names a stub on the test PATH
            request = request_from(argparse.Namespace(
                production=str(self.doc), unit="alpha-unit", phase="3", hash=code,
                also=[], shrink=False, as_built=False, trailer=[], review_trial=TRIAL, delivers=DELIVERS, parallel=PARALLEL,
                cancel_prior=False, started=STARTED, regime="after", holds=0,
                merge_defects=0, excluded=None, scratch=str(self.root / "scratch"),
            ))
        latest = merge_history(request)
        if not isinstance(latest, LastMerged):
            self.fail("a renamed unit read as never merged, so its first-merge gate would fire again")
        self.assertEqual(latest.hash, code)

    def test_merge_history_follows_a_rename_chain_and_keeps_a_reused_name(self) -> None:
        for subject in (
            "Merge a-unit phase 1 (aaaaaaa) into production",
            "chore(example): rename unit a-unit to b-unit",
            "Merge b-unit phase 2 (bbbbbbb) into production",
            "chore(example): rename unit b-unit to c-unit",
            "Merge a-unit phase 1 (ccccccc) into production",
        ):
            _ = self.git("commit", "--allow-empty", "-m", subject)
        history = merge_branch_history(self.checkout, "production")
        self.assertEqual([(entry.unit, entry.short) for entry in history.entries if isinstance(entry, MergeEntry)],
                         [("a-unit", "ccccccc"), ("c-unit", "bbbbbbb"), ("c-unit", "aaaaaaa")])
        self.assertTrue(history.has_code_merge("c-unit", "1"))
        self.assertIsInstance(history.last_for_unit("b-unit"), NoMerge)
        self.assertEqual(history.code_merge_count(), 3)

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
        if not isinstance(latest, MergeEntry):
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


    def child_production(self, *, parent: str = "example") -> Path:
        doc = self.root / "example-style-production.md"
        _ = doc.write_text("\n".join([
            "# Style production", "- **Kind:** style", "- **Branch point:** production",
            "- **Merge branch:** alpha-branch", f"- **Showrunner checkout:** {self.unit}",
            "- **Log:** style.log", "", "## Units", "",
        ]))
        _ = (self.state / "child-parent").write_text(parent)
        registry = self.bin / "showrunners.py"
        self.executable(registry, "\n".join([
            "#!/usr/bin/env python3", "import sys", "from pathlib import Path",
            f"state = Path({str(self.state)!r})",
            "if sys.argv[1] == 'parent': print((state / 'child-parent').read_text())",
            f"elif sys.argv[1] == 'list': print('example-style\\t{doc}')",
            "elif sys.argv[1] == 'address': print('session:fake-child')",
            "else: raise SystemExit(2)", "",
        ]))
        self.executable(self.bin / "send.py", STUB)
        self.env["MERGE_CHECKPOINT_SHOWRUNNERS"] = str(registry)
        self.env["MERGE_CHECKPOINT_SEND"] = str(self.bin / "send.py")
        return doc

    def run_handoff(self, checkpoint: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([
            sys.executable, str(SCRIPT), "--from-child", "example-style", "--checkpoint", checkpoint,
            "--production", str(self.doc), "--started", STARTED, *extra,
        ], cwd=self.checkout, env=self.env, capture_output=True, text=True, check=False, timeout=300)

    def test_attached_child_handoff_merges_without_a_units_row_and_acknowledges_after_push(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        merge = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("log", "-1", "--format=%s"),
                         f"Merge example-style hand-off ({checkpoint[:7]}) into production")
        self.assertEqual(self.git("rev-parse", "HEAD^2"), checkpoint)
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/production").split()[0], merge)
        self.assertEqual((self.state / "notified-tip").read_text(), merge)
        self.assertEqual(self.events("send.py"), [[
            "--to", "session:fake-child", "--from", "example", "--text",
            f"From the showrunner of example: hand-off {checkpoint[:7]} merged as {merge[:7]}.", "--timeout", "10",
        ]])
        rows = [cast(dict[str, object], json.loads(line)) for line in (self.state / "events.jsonl").read_text().splitlines()]
        commands = [row["command"] for row in rows]
        self.assertLess(commands.index("style_sync.py"), commands.index("send.py"))
        self.assertLess(commands.index("style_sync.py"), commands.index("style_ledger.py"))
        self.assertLess(commands.index("style_ledger.py"), commands.index("send.py"))
        self.assertEqual(self.events("style_ledger.py"), [[
            "handoff", "merged", "--repo", str(self.unit), "--checkpoint", checkpoint,
            "--merge-commit", merge,
        ]])

    def prepare_handoff_ledger(self, checkpoint: str, *, add_handoff: bool = True) -> None:
        ledger = Path(__file__).resolve().parents[1] / "style/style_ledger.py"
        self.env["MERGE_CHECKPOINT_STYLE_LEDGER"] = str(ledger)
        self.env["STYLE_LEDGER_STATE_DIR"] = str(self.root / "style-state")
        catalog = self.root / "rule-catalog"
        catalog.mkdir()
        self.env["STYLE_HISTORY_NATE_STYLE_DIR"] = str(catalog)
        commands = [["reconcile", "--branch-point", "production", "--style-branch", "alpha-branch"]]
        if add_handoff:
            commands.extend([
                ["handoff", "add", "--checkpoint", checkpoint],
                ["handoff", "send", "--checkpoint", checkpoint, "--to", "example"],
            ])
        for arguments in commands:
            result = subprocess.run(
                [sys.executable, str(ledger), *arguments, "--repo", str(self.unit)],
                env=self.env, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def handoff_ledger_row(self, checkpoint: str) -> tuple[str, str]:
        database = self.root / "style-state" / self.checkout.name / "ledger.sqlite"
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            rows = cast(list[tuple[str, str]], connection.execute(
                "SELECT state,merge_commit FROM handoffs WHERE checkpoint=?", (checkpoint,),
            ).fetchall())
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_child_handoff_records_the_pushed_merge_in_the_style_ledger(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
        self.prepare_handoff_ledger(checkpoint)
        self.assertEqual(self.handoff_ledger_row(checkpoint), ("sent", ""))
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        merge = self.git("rev-parse", "HEAD")
        self.assertEqual(self.handoff_ledger_row(checkpoint), ("merged", merge))
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        self.assertEqual(len(self.events("send.py")), 1)
        self.assertNotIn("hand-off ledger:", result.stdout)
        duplicate = self.run_handoff(checkpoint)
        self.assertEqual((duplicate.returncode, duplicate.stdout, duplicate.stderr),
                         (0, f"hand-off {checkpoint[:7]} already merged as {merge[:7]}\n", ""))
        self.assertEqual(self.handoff_ledger_row(checkpoint), ("merged", merge))
        self.assertEqual(len(self.events("send.py")), 2)

    def test_child_handoff_ledger_failure_still_pushes_and_acknowledges(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
        self.prepare_handoff_ledger(checkpoint, add_handoff=False)
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        merge = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("rev-parse", "HEAD^2"), checkpoint)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        self.assertEqual(len(self.events("send.py")), 1)
        self.assertEqual([line for line in result.stdout.splitlines() if line.startswith("hand-off ledger:")],
                         [f"hand-off ledger: style_ledger: unknown hand-off {checkpoint}"])

    def test_duplicate_child_notice_records_a_ledger_write_missed_at_the_merge(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
        self.prepare_handoff_ledger(checkpoint)
        self.env["MERGE_CHECKPOINT_STYLE_LEDGER"] = str(self.bin / "style_ledger.py")
        self.assertEqual(self.run_handoff(checkpoint).returncode, 0)
        merge = self.git("rev-parse", "HEAD")
        self.assertEqual(self.handoff_ledger_row(checkpoint), ("sent", ""))
        self.env["MERGE_CHECKPOINT_STYLE_LEDGER"] = str(Path(__file__).resolve().parents[1] / "style/style_ledger.py")
        result = self.run_handoff(checkpoint)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, f"hand-off {checkpoint[:7]} already merged as {merge[:7]}\n", ""))
        self.assertEqual(self.handoff_ledger_row(checkpoint), ("merged", merge))
        self.assertEqual(self.git("rev-parse", "HEAD"), merge)
        self.assertEqual(len(self.events("send.py")), 2)

    def test_unattached_child_is_refused_before_merge_push_or_acknowledgment(self) -> None:
        _ = self.child_production(parent="someone-else")
        checkpoint = self.checkpoint()
        before = self.git("rev-parse", "HEAD")
        remote = self.git("ls-remote", "origin", "refs/heads/production")
        result = self.run_handoff(checkpoint)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("example-style is not attached under example", result.stdout)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/production"), remote)
        self.assertEqual(self.events("send.py"), [])
        self.assertEqual(self.events("style_sync.py"), [])

    def test_child_checkpoint_off_its_merge_branch_is_refused(self) -> None:
        _ = self.child_production()
        self.write(self.other, "src/beta.txt", "unrelated checkpoint\n")
        _ = self.git("commit", "-am", "Other work", cwd=self.other)
        checkpoint = self.git("rev-parse", "HEAD", cwd=self.other)
        before = self.git("rev-parse", "HEAD")
        result = self.run_handoff(checkpoint)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"checkpoint {checkpoint[:7]} is not on example-style's merge branch alpha-branch", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.events("send.py"), [])

    def test_child_checkpoint_must_descend_from_last_merged_handoff(self) -> None:
        _ = self.child_production()
        first = self.checkpoint()
        self.assertEqual(self.run_handoff(first).returncode, 0)
        self.write(self.other, "src/beta.txt", "divergent history\n")
        _ = self.git("commit", "-am", "Divergent child", cwd=self.other)
        divergent = self.git("rev-parse", "HEAD", cwd=self.other)
        _ = self.git("reset", "--hard", divergent, cwd=self.unit)
        before, notices = self.git("rev-parse", "HEAD"), self.events("send.py")
        result = self.run_handoff(divergent)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not descend from example-style's last merged hand-off", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.events("send.py"), notices)

    def test_root_style_handoff_merges_itself_without_an_attachment(self) -> None:
        _ = self.child_production(parent="")
        checkpoint = self.checkpoint()
        refused = self.run_handoff(checkpoint)
        self.assertNotEqual(refused.returncode, 0)
        result = self.run_handoff(checkpoint, "--root-self")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD^2"), checkpoint)
        self.assertEqual(self.events("send.py"), [])
        self.assertEqual(self.events("style_ledger.py"), [])
        self.assertIn(f"root: run style_ledger.py reconcile to mark {checkpoint[:7]} merged", result.stdout)

    def test_duplicate_child_notice_acknowledges_without_another_merge_push_or_notify(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        _ = self.child_production()
        checkpoint = self.checkpoint()
        self.assertEqual(self.run_handoff(checkpoint).returncode, 0)
        merge = self.git("rev-parse", "HEAD")
        count = self.git("rev-list", "--count", "HEAD")
        notifications = self.events("style_sync.py")
        self.write(self.promote, "src/alpha.txt", "local changes must not be promoted over\n")
        real_git = shutil.which("git")
        if real_git is None:
            self.fail("git is required for the checkpoint fixture")
        self.executable(self.bin / "git", "\n".join([
            "#!/usr/bin/env python3", "import os,sys", "from pathlib import Path",
            "if 'push' in sys.argv[1:]:",
            f" Path({str(self.state / 'unexpected-push')!r}).touch()",
            " raise SystemExit(91)", f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])", "",
        ]))
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), f"hand-off {checkpoint[:7]} already merged as {merge[:7]}")
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), count)
        self.assertEqual(self.git("rev-parse", "HEAD"), merge)
        self.assertEqual(self.events("style_sync.py"), notifications)
        self.assertFalse((self.state / "unexpected-push").exists())
        self.assertEqual((self.promote / "src/alpha.txt").read_text(), "local changes must not be promoted over\n")
        self.assertEqual(len(self.events("send.py")), 2)
        self.assertEqual(self.events("send.py")[0], self.events("send.py")[1])

    def test_failed_handoff_push_rolls_back_and_retry_acknowledges_only_after_push(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
        before = self.git("rev-parse", "HEAD")
        marker = self.state / "reject-once"
        marker.touch()
        self.executable(self.origin / "hooks/pre-receive", (
            "#!/bin/sh\nif [ -e '" + str(marker) + "' ]; then\n"
            + " rm '" + str(marker) + "'\n exit 1\nfi\ncat >/dev/null\n"
        ))
        failed = self.run_handoff(checkpoint)
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), before)
        self.assertEqual(self.events("send.py"), [])
        self.assertEqual(self.events("style_sync.py"), [])
        self.assertEqual(self.receipts(), [])
        retried = self.run_handoff(checkpoint)
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        merge = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("rev-parse", "HEAD^1"), before)
        self.assertEqual(self.git("rev-parse", "HEAD^2"), checkpoint)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        self.assertEqual((self.state / "notified-tip").read_text(), merge)
        self.assertEqual(len(self.events("send.py")), 1)
        self.assertLess(retried.stdout.index("push: ok"), retried.stdout.index("hand-off "))

    def test_locally_merged_handoff_is_pushed_before_acknowledgment_without_merging_again(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
        _ = self.git("merge", "--no-ff", "-m",
                     f"Merge example-style hand-off ({checkpoint[:7]}) into production", checkpoint)
        merge = self.git("rev-parse", "HEAD")
        count = self.git("rev-list", "--count", "HEAD")
        self.assertNotEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), merge)
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), count)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), merge)
        self.assertEqual((self.state / "notified-tip").read_text(), merge)
        self.assertEqual(len(self.events("send.py")), 1)
        self.assertNotIn("already merged", result.stdout)

    def test_handoff_runs_package_tests_and_writes_the_checkpoint_test_log(self) -> None:
        _ = self.child_production()
        self.write(self.unit, "crates/widget/Cargo.toml", '[package]\nname="widget"\nversion="0.1.0"\n')
        _ = self.git("add", "crates/widget/Cargo.toml", cwd=self.unit)
        checkpoint = self.checkpoint("crates/widget/src/lib.rs", "fn checked() {}\n")
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events("verify.sh"), [["test", "widget"]])
        log = self.state / f"merge_{checkpoint[:7]}_test.log"
        self.assertIn("package_widget_EXIT=0", log.read_text())
        self.assertIn("merge_test_1_EXIT=0", log.read_text())

    def test_red_handoff_package_test_restores_the_pre_merge_tip(self) -> None:
        _ = self.child_production()
        self.write(self.unit, "crates/widget/Cargo.toml", '[package]\nname="widget"\nversion="0.1.0"\n')
        _ = self.git("add", "crates/widget/Cargo.toml", cwd=self.unit)
        checkpoint = self.checkpoint("crates/widget/src/lib.rs", "fn checked() {}\n")
        before = self.git("rev-parse", "HEAD")
        (self.state / "fail-always-test-widget").touch()
        result = self.run_handoff(checkpoint)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events("verify.sh"), [["test", "widget"], ["test", "widget"]])
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), before)
        log = (self.state / f"merge_{checkpoint[:7]}_test.log").read_text()
        self.assertIn("package_widget_EXIT=1", log)
        self.assertIn("package_widget_RERUN_EXIT=1", log)
        self.assertEqual(self.events("send.py"), [])
        self.assertEqual(self.events("style_sync.py"), [])

    def test_root_self_refuses_a_child_that_still_reports_to_a_parent(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
        before = self.git("rev-parse", "HEAD")
        result = self.run_handoff(checkpoint, "--root-self")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.strip(),
                         "merge_checkpoint: example-style reports to example; --root-self needs a root")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.events("send.py"), [])

    def test_untracked_parent_file_does_not_refuse_a_handoff(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
        self.write(self.checkout, "untracked.txt", "unrelated local note\n")
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.checkout / "untracked.txt").read_text(), "unrelated local note\n")
        self.assertEqual(len(self.events("send.py")), 1)

    def test_staged_parent_file_refuses_a_handoff_before_merge_or_push(self) -> None:
        _ = self.child_production()
        checkpoint = self.checkpoint()
        self.write(self.checkout, "staged.txt", "unrelated staged change\n")
        _ = self.git("add", "staged.txt")
        before = self.git("rev-parse", "HEAD")
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 1)
        self.assertIn("staged paths in merge checkout: staged.txt", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), before)
        self.assertEqual(self.events("send.py"), [])

    def test_handoff_promotes_after_push_before_acknowledgment(self) -> None:
        self.promotions = str(self.promote)
        self.production_doc()
        _ = self.child_production()
        checkpoint = self.checkpoint()
        sender = (self.bin / "send.py").read_text()
        # Observe promotion at delivery time, before recording the acknowledgment.
        self.executable(self.bin / "send.py", sender.replace(
            'args = sys.argv[1:]',
            'args = sys.argv[1:]\nimport subprocess\n'
            + f'promoted = subprocess.check_output(["git", "-C", {str(self.promote)!r}, "rev-parse", "HEAD"], text=True).strip()\n'
            + f'remote = subprocess.check_output(["git", "-C", {str(self.origin)!r}, "rev-parse", "production"], text=True).strip()\n'
            + 'assert promoted == remote\n'
            + f'assert subprocess.call(["git", "-C", {str(self.promote)!r}, "merge-base", "--is-ancestor", {checkpoint!r}, promoted]) == 0',
        ))
        result = self.run_handoff(checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        tip = self.git("rev-parse", "HEAD")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.promote), tip)
        self.assertEqual(self.git("rev-parse", "production", cwd=self.origin), tip)
        self.assertEqual((self.state / "notified-tip").read_text(), tip)
        self.assertLess(result.stdout.index("push: ok"), result.stdout.index("promote: ok"))
        self.assertEqual(len(self.events("send.py")), 1)
        self.assertNotIn("hand-off acknowledgment:", result.stdout)

    def test_former_child_slug_resolves_to_the_canonical_doc_and_merge_subject(self) -> None:
        doc = self.child_production()
        _ = doc.write_text(doc.read_text().replace(
            "# Style production", "# Style production\n\n## Production Context\n- **Formerly:** `old-style`",
        ))
        timer = self.root / "notifier/showrunner-example-style/conf"
        timer.parent.mkdir(parents=True)
        _ = timer.write_text(f"TARGET=session:fake-child\nCHECK=zsh /unused/production_check.sh {doc}\n")
        checkpoint = self.checkpoint()
        result = subprocess.run([
            sys.executable, str(SCRIPT), "--from-child", "old-style", "--checkpoint", checkpoint,
            "--production", str(self.doc), "--started", STARTED,
        ], cwd=self.checkout, env=self.env, capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git("log", "-1", "--format=%s"),
                         f"Merge example-style hand-off ({checkpoint[:7]}) into production")
        entry = merge_branch_history(self.checkout, "production").last_merge()
        self.assertIsInstance(entry, ChildHandoffEntry)
        if not isinstance(entry, ChildHandoffEntry):
            self.fail("canonical child's hand-off was not recorded")
        self.assertEqual(entry.child, "example-style")
        self.assertEqual(len(self.events("send.py")), 1)
        self.assertNotIn("old-style", str(self.events("send.py")))

    def test_handoff_refuses_unit_only_hold_metadata(self) -> None:
        _ = self.child_production()
        before = self.git("rev-parse", "HEAD")
        result = self.run_handoff(self.checkpoint(), "--holds", "1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.strip(), "merge_checkpoint: --from-child is separate from unit checkpoint arguments")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.events("send.py"), [])

    def test_style_unit_none_owns_needs_explicit_ticket_file_allowance(self) -> None:
        self.production_doc(owns_cell="none — takes what the board leaves free")
        checkpoint = self.checkpoint(relative="src/lib.rs", content="fn fixed() {}\n")
        refused = self.run_checkpoint(checkpoint)
        self.assertNotEqual(refused.process.returncode, 0)
        self.assertIn("outside Owns: src/lib.rs", refused.process.stdout)
        permitted = self.run_checkpoint(checkpoint, "--also", "src/lib.rs")
        self.assertEqual(permitted.process.returncode, 0, permitted.process.stdout + permitted.process.stderr)
        self.assertIn("scope: ok", permitted.process.stdout)

    def test_mixed_history_keeps_child_identity_separate_from_renamed_unit_phases(self) -> None:
        subjects = (
            "Merge old-unit phase 1 (aaaaaaa) into production",
            "Merge old-unit hand-off (bbbbbbb) into production",
            "Merge old-unit phase 1 shrink (ccccccc) into production",
            "chore(example): rename unit old-unit to new-unit",
            "Merge style-child hand-off (ddddddd) into production",
            "Merge new-unit phase 2 (eeeeeee) into production",
            "Merge old-unit hand-off (fffffff) into production",
        )
        hashes: list[str] = []
        for subject in subjects:
            _ = self.git("commit", "--allow-empty", "-m", subject)
            hashes.append(self.git("rev-parse", "HEAD"))
        history = merge_branch_history(self.checkout, "production")
        self.assertEqual(history.entries, (
            ChildHandoffEntry("old-unit", "fffffff", "production", hashes[6]),
            MergeEntry("new-unit", "2", "eeeeeee", "production", hashes[5], CodeCheckpoint()),
            ChildHandoffEntry("style-child", "ddddddd", "production", hashes[4]),
            MergeEntry("new-unit", "1", "ccccccc", "production", hashes[2], ShrinkCommit()),
            ChildHandoffEntry("old-unit", "bbbbbbb", "production", hashes[1]),
            MergeEntry("new-unit", "1", "aaaaaaa", "production", hashes[0], CodeCheckpoint()),
        ))
        self.assertEqual(history.last_merge(), history.entries[0])
        self.assertEqual(history.last_for_unit("new-unit"), history.entries[1])
        self.assertEqual(history.last_code_for_unit("new-unit"), history.entries[1])
        self.assertEqual(history.last_for_unit("old-unit"), NoMerge())
        self.assertEqual(history.last_code_for_unit("style-child"), NoMerge())
        self.assertEqual(history.code_merge_count(), 2)
        self.assertTrue(history.has_code_merge("new-unit", "1"))
        self.assertFalse(history.has_code_merge("old-unit", "style hand-off"))
        self.assertEqual(history.last_handoff_for_child("old-unit"), history.entries[0])
        self.assertEqual(history.last_handoff_for_child("new-unit"), NoMerge())


@final
class TestPassedTests(unittest.TestCase):
    VENV = "venv .venv subdirectory not found in venv path /checkout.\n"

    def judge(self, status: int, stdout: str, stderr: str = "") -> bool:
        result = subprocess.CompletedProcess(["basedpyright"], status, stdout, stderr)
        return merge_checkpoint.test_passed(["/nix/bin/basedpyright", "scripts/buildlog"], result)

    def test_clean_basedpyright_passes_with_trailing_venv_line_on_stderr(self) -> None:
        self.assertTrue(self.judge(3, "0 errors, 0 warnings, 0 notes\n", self.VENV))

    def test_basedpyright_with_errors_fails(self) -> None:
        self.assertFalse(self.judge(1, "/checkout/a.py:1:1 - error: x\n2 errors, 0 warnings, 0 notes\n", self.VENV))
        self.assertFalse(self.judge(3, "2 errors, 0 warnings, 0 notes\n", self.VENV))

    def test_other_commands_pass_only_on_status_zero(self) -> None:
        result = subprocess.CompletedProcess(["python3"], 3, "0 errors, 0 warnings, 0 notes\n", "")
        self.assertFalse(merge_checkpoint.test_passed(["python3", "-m", "unittest"], result))
        self.assertTrue(merge_checkpoint.test_passed(["python3"], subprocess.CompletedProcess(["python3"], 0, "", "")))


@final
class MacPromotionPathTests(unittest.TestCase):
    def remote_command(self, promote_line: str) -> str:
        promotion = production_doc.promotion_from_doc([f"- **Promote:** {promote_line}"])
        assert isinstance(promotion, production_doc.PromoteTo)
        assert isinstance(promotion.mac, production_doc.MacCheckout)
        done = subprocess.CompletedProcess(["ssh"], 0, "rc=0\n", "")
        with mock.patch("merge_checkpoint.subprocess.run", return_value=done) as run:
            merge_checkpoint.pull_mac_checkout(promotion.mac.path, Path("/home/natepiano/.claude"))
        argv = cast(list[str], run.call_args.args[0])
        self.assertEqual(argv[:2], ["ssh", "mac"])
        return argv[2]

    def test_tilde_mac_path_is_expanded_by_the_mac_shell(self) -> None:
        remote = self.remote_command("mac `~/.claude`")
        self.assertTrue(remote.startswith('cd "$HOME"/.claude && git pull'), remote)
        self.assertNotIn(str(Path.home()) + "/.claude &&", remote)

    def test_absolute_mac_path_is_kept(self) -> None:
        remote = self.remote_command("/home/natepiano/.claude, mac /Users/someone/my repo")
        self.assertTrue(remote.startswith("cd '/Users/someone/my repo' && git pull"), remote)


if __name__ == "__main__":
    _ = unittest.main()
