"""Sync notices are best effort; lint records describe the exact requested tree."""

from __future__ import annotations

import errno
import fcntl
import io
import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from contextlib import closing, redirect_stderr, redirect_stdout, suppress
from pathlib import Path

from typing import TypedDict, cast, final, override
from unittest import mock

from scripts.style import style_sync
from scripts.style import style_plan
from scripts.style.style_child_test_support import unqueued_style_children
from scripts.style.add_style import NoStyleProduction, standing_style_production
from scripts.copies import copies
from scripts.lib import scratch
from scripts.style import style_ledger
from scripts.style import style_rules
from scripts.lib import build_folders
from scripts.lib.process_group import send_process_group
from scripts.production import showrunners, staff
from scripts.production.staff_test_support import fixture_unit_location, recorded_showrunner
from scripts.style.ledger_test_support import style_staff_fixture, style_unit_id, ticket_owner_id, write_style_units
from scripts.style.style_config import StyleUnitAssignment
from scripts.message import send_budget, session_send
from scripts.direct import test_verify_lint_scope as verify_fixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.cargo_output_test_support import fake_cargo_output_roots
from scripts.lib.py_floor_test_support import interpreter_cache_environment


class RecordedCall(TypedDict):
    arguments: list[str]


RECORDED_LINT = '''style-lint: mend start
+ env RUSTC_WRAPPER= cargo mend --all-targets --workspace --fail-on-warn
warning: import module instead of item
 --> src/lib.rs:1:1
  |
1 | use crate::values::Value;
  | ^^^^^^^^^^^^^^^^^^^^^^^^^
  = help: for further information visit https://github.com/natepiano/cargo-liner
style-lint: rustfmt start
+ cargo +nightly fmt --all --check
Diff in src/lib.rs:2:
style-lint: clippy start
+ cargo clippy --workspace --all-targets --all-features -- -D warnings
warning: prefer clearer names
 --> src/lib.rs:3:1
warning: prefer a type
 --> src/lib.rs:5:1
'''

CLEAN_LINT = ''.join(f"style-lint: {tool} start\nstyle-lint: {tool} passed\n"
                     for tool in ("mend", "rustfmt", "clippy"))

# Runs the lint in its own process, so a signal takes the real path through style_sync.
LINT_DRIVER = '''import sys
from pathlib import Path
from scripts.style import style_sync
style_sync.VERIFY = Path(sys.argv[1])
style_sync.LINT_TEMP_PARENT = Path(sys.argv[2])
style_sync.CHILD_STOP_SECONDS = 0.5
style_sync.gate_owner = lambda repo: "sample-style"
raise SystemExit(style_sync.main(sys.argv[3:]))
'''


def running(pid: int) -> bool:
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)
    return state.returncode == 0 and not state.stdout.strip().startswith("Z")


def wait_until(condition: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def group_running(group: int) -> bool:
    try:
        send_process_group(group, 0)
    except ProcessLookupError:
        return False
    return True


def kill_group(group: int) -> None:
    with suppress(ProcessLookupError):
        send_process_group(group, signal.SIGKILL)


def ended_pid() -> int:
    process = subprocess.Popen(["true"])
    _ = process.wait()
    return process.pid


@final
class StyleSyncTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    style: Path = Path()
    doc: Path = Path()
    tip: str = ""
    lint_temp: Path = Path()
    resolve_owner = staticmethod(style_sync.gate_owner)

    @override
    def setUp(self) -> None:
        _ = self.enterContext(fake_cargo_output_roots(children=True))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="style-sync-test-")))
        self.repo = self.root / "sample"
        self.style = self.root / "sample-style"
        self.repo.mkdir()
        _ = self.git("init", "-q", "--initial-branch=main")
        _ = self.git("config", "user.name", "Sync tests")
        _ = self.git("config", "user.email", "sync@example.invalid")
        (self.repo / "src").mkdir()
        _ = (self.repo / "Cargo.toml").write_text('[package]\nname="sample"\nversion="0.1.0"\n')
        _ = (self.repo / "src/lib.rs").write_text("fn value() {}\n")
        _ = self.git("add", "--all")
        _ = self.git("commit", "-qm", "Initial")
        self.tip = self.git("rev-parse", "HEAD")
        self.doc = self.style / "docs/sample-style-production.md"
        rule_root = self.root / "nate_style"
        (rule_root / "rust").mkdir(parents=True)
        for tool in ("mend", "clippy", "rustfmt"):
            _ = (rule_root / "rust" / f"{tool}.md").write_text(f"---\ntier: A\nmechanism: {tool}\n---\n# {tool}\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root / "gate-home"),
            **interpreter_cache_environment(self.root / "cache"),
            "STYLE_HISTORY_NATE_STYLE_DIR": str(rule_root),
            "STYLE_LEDGER_STATE_DIR": str(self.root / "state"),
            "SCRATCH_HOLDS": str(self.root / "scratch-holds.json"),
            "SCRATCH_ROOT": str(self.root / "scratch"),
            "SCRATCH_COPIES_ROOT": str(self.root / "copies"),
            "SCRATCH_VISIBLE_ROOT": str(self.root / "repro"),
        })))
        _ = os.environ.pop("CLAUDE_PYTHON", None)
        _ = self.enterContext(style_staff_fixture(self.root, self.repo))
        owner = next(row for row in staff.snapshot().find(kind="showrunner") if row.name == "sample-style")
        assert isinstance(owner.doc, staff.LiveDoc)
        _ = owner.doc.path.write_text("# Ticket owners\n")
        _ = self.enterContext(mock.patch.object(style_rules, "NATE_STYLE_DIR", rule_root))
        sender = self.root / "send.py"
        _ = sender.write_text(
            "import json,sys\nfrom pathlib import Path\n"
            + f"Path({str(self.root / 'sent.json')!r}).write_text(json.dumps({{'arguments':sys.argv[1:]}}))\n"
            + "print('SENT: fixture')\nraise SystemExit(0)\n",
        )
        address = self.root / "address.py"
        _ = address.write_text("print('session:fake-style')\n")
        _ = self.enterContext(mock.patch.object(session_send, "SEND_SCRIPT", sender))
        _ = self.enterContext(mock.patch.object(style_sync, "SHOWRUNNERS", address))
        _ = self.enterContext(mock.patch.object(showrunners, "own_name", return_value=""))
        self.lint_temp = self.root / "tmp"
        self.lint_temp.mkdir()
        _ = self.enterContext(mock.patch.object(style_sync, "LINT_TEMP_PARENT", self.lint_temp))
        _ = self.enterContext(mock.patch.object(style_sync, "gate_owner", return_value="sample-style"))

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *arguments],
                              capture_output=True, text=True, check=True).stdout.strip()

    def production(self, branch: str = "main") -> None:
        _ = self.git("worktree", "add", "-q", "--detach", str(self.style))
        self.doc.parent.mkdir(parents=True)
        _ = self.doc.write_text(f"# Style\n- **Kind:** style\n- **Branch point:** {branch}\n" +
                                f"- **Merge branch:** style/main\n- **Showrunner checkout:** {self.style}\n")
        write_style_units(self.doc)
        owner = next(row for row in staff.snapshot().find(kind="showrunner") if row.name == "sample-style")
        assert isinstance(owner.doc, staff.LiveDoc)
        owner.doc.path.unlink()
        _ = recorded_showrunner(owner.name, self.doc)

    def direct_production(self) -> None:
        self.doc = self.repo / ".git/style-production.md"
        _ = self.doc.write_text(
            f"# Production\n- **Kind:** product\n- **Merge branch:** main\n- **Showrunner checkout:** {self.repo}\n")
        assignments = (StyleUnitAssignment("names", ("names", "values")),
                       StyleUnitAssignment("types", ("types", "modules")))
        write_style_units(self.doc, assignments)
        owner = recorded_showrunner("sample-style", self.doc)
        for assignment in assignments:
            worktree = self.root / f"direct-{assignment.unit}"
            _ = self.git("worktree", "add", "-qb", f"unit-{assignment.unit}", str(worktree))
            _ = fixture_unit_location(owner, assignment.unit, worktree)

    def notice(self, repo: Path | None = None) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = style_sync.main(["notify", "--repo-dir", str(repo or self.repo),
                                    "--branch", "refs/heads/main", "--tip", self.tip])
        return code, output.getvalue()

    def checkpoint_for_wake(self, *, on_style_branch: bool = True) -> None:
        rule = "global:rust/read-modules.md"
        _ = (self.root / "nate_style/rust/read-modules.md").write_text(
            "---\ntier: C\nfamily: modules\ninput_scope: file\n---\n# Modules\nReview module names.\n"
        )
        _ = self.git("branch", "style/main")
        if not on_style_branch:
            _ = (self.repo / "src/lib.rs").write_text("fn changed() {}\n")
            _ = self.git("commit", "-qam", "Unit checkpoint")
        checkpoint = self.git("rev-parse", "HEAD")
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.set_tip(self.tip, "style/main")
            ledger.ticket_add("3001", ticket_owner_id("modules"), "read", "modules", ".",
                              ["src/lib.rs"], [rule])
            ledger.ticket_assign("3001", "main")
            blob = ledger.pinned_rule_texts("3001")[0].blob
            ledger.record(rule, "src/lib.rs", checkpoint, blob, "clean", ticket="3001", worker="test")
            ledger.ticket_checkpoint("3001", checkpoint)

    def waiting_units(self, *units: str) -> None:
        with style_ledger.open_ledger(self.repo) as ledger:
            for unit in units:
                ledger.wait_for_sync(ticket_owner_id(unit))

    def remaining_waiters(self) -> list[str]:
        with style_ledger.open_ledger(self.repo) as ledger, closing(sqlite3.connect(ledger.database)) as connection:
            rows = cast(list[tuple[str]], connection.execute("SELECT unit FROM sync_waits ORDER BY unit").fetchall())
        return [unit for (unit,) in rows]

    def wake_roster(self, units: tuple[str, ...] = ("names", "types")) -> None:
        entries: list[dict[str, str]] = [
            {"role": "unit director", "production": "sample-style", "unit": unit,
             "name": unit, "address": "session:fake-" + unit}
            for unit in units
        ]
        entries.extend([
            {"role": "unit director", "production": "another-style", "unit": "names",
             "name": "names", "address": "session:wrong-production"},
            {"role": "worker", "production": "sample-style", "unit": "names",
             "name": "names-impl", "address": "seat:wrong-worker"},
        ])
        roster = self.root / "roster.py"
        _ = roster.write_text(
            "import sys\nfrom pathlib import Path\n"
            + f"Path({str(self.root / 'roster-args')!r}).write_text(' '.join(sys.argv[1:]))\n"
            + f"print({json.dumps({'entries': entries, 'problems': []})!r})\n",
        )
        _ = self.enterContext(mock.patch.object(style_sync, "ROSTER", roster, create=True))

    def wake_sender(self, failed_unit: str = "") -> None:
        self.wake_roster()
        _ = (self.root / "send.py").write_text(
            "import json,sys\nfrom pathlib import Path\n"
            + f"with Path({str(self.root / 'wakes.jsonl')!r}).open('a') as output:\n"
            + "    output.write(json.dumps(sys.argv[1:])+'\\n')\n"
            + f"if sys.argv[sys.argv.index('--to')+1] == {('session:fake-' + failed_unit)!r}:\n"
            + "    print('FAILED: unavailable'); raise SystemExit(3)\n",
        )

    def recorded_wakes(self) -> list[list[str]]:
        path = self.root / "wakes.jsonl"
        return [cast(list[str], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def merge_for_wake(self) -> tuple[int, str, str]:
        # Keep the CLI's success/refusal routing real; replace only its process boundary.
        def wake_in_process(repo: Path, ticket: str) -> None:
            self.assertEqual(style_sync.main(["wake-waiters", "--repo", str(repo), "--ticket", ticket]), 0)

        output, errors = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(style_ledger, "_wake_sync_waiters", wake_in_process, create=True), mock.patch.object(
            sys, "argv", ["style_ledger.py", "ticket", "merge", "--repo", str(self.repo), "--id", "3001"],
        ), redirect_stdout(output), redirect_stderr(errors):
            try:
                style_ledger.main()
            except SystemExit as error:
                code = cast(int, error.code)
        return code, output.getvalue(), errors.getvalue()

    def test_own_handoff_syncs_the_merged_tip_without_lint(self) -> None:
        _ = self.git("checkout", "-qb", "style/main")
        _ = (self.repo / "src/lib.rs").write_text("fn corrected() {}\n")
        _ = self.git("commit", "-qam", "Style checkpoint")
        _ = self.git("checkout", "-q", "main")
        _ = self.git("merge", "--no-ff", "-qm", "Integrate style", "style/main")
        tip = self.git("rev-parse", "HEAD")
        self.assertNotEqual(tip, self.git("rev-parse", "style/main"))
        with mock.patch.object(style_sync, "lint") as lint, redirect_stdout(io.StringIO()):
            for branch in ("style/main", "refs/heads/style/main"):
                self.assertEqual(style_sync.main([
                    "own-handoff", "--repo", str(self.repo), "--tip", tip, "--style-branch", branch,
                ]), 0)
            lint.assert_not_called()
        with style_ledger.open_ledger(self.repo) as ledger, closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(ledger.synced_commit(), tip)
            self.assertEqual(ledger.tip_seen(tip), "own hand-off")
            ledger.tip_done(tip)
            self.assertEqual(ledger.tip_seen(tip), "own hand-off")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM lint_runs").fetchone(), (0,))
        seen = subprocess.run([
            str(style_sync.PYTHON), str(style_sync.LEDGER), "tip", "seen", "--repo", str(self.repo), "--tip", tip,
        ], capture_output=True, text=True, check=True)
        self.assertEqual(seen.stdout.strip(), "seen (own hand-off: synced with no lint run)")

    def test_own_handoff_leaves_a_different_tree_unprocessed(self) -> None:
        _ = self.git("branch", "style/main")
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.set_tip(self.tip, "style/main")
        _ = (self.repo / "src/lib.rs").write_text("fn changed() {}\n")
        _ = self.git("commit", "-qam", "Other change")
        tip = self.git("rev-parse", "HEAD")
        self.assertEqual(style_sync.main([
            "own-handoff", "--repo", str(self.repo), "--tip", tip, "--style-branch", "style/main",
        ]), 1)
        with style_ledger.open_ledger(self.repo) as ledger:
            self.assertEqual(ledger.synced_commit(), self.tip)
            self.assertIsInstance(ledger.tip_seen(tip), style_ledger.UnseenTip)

    def test_direct_own_tree_comparison_uses_the_unit_branch_and_records_main(self) -> None:
        self.direct_production()
        _ = self.enterContext(unqueued_style_children())
        worktree = self.root / "direct-names"
        rule = "global:rust/names.md"
        _ = (self.root / "nate_style/rust/names.md").write_text("---\ntier: C\nfamily: names\ninput_scope: file\n---\n# Names\n")
        run = self.root / "run"
        journal = style_ledger.publication_path(worktree, "publication.json")

        def assign(ticket: str, source: str) -> None:
            with style_ledger.open_ledger(worktree) as ledger:
                ledger.ticket_add(ticket, ticket_owner_id("names"), "read", "names", ".", [source], [rule])
                ledger.ticket_assign(ticket, "unit-names")
                _ = ledger.ticket_base(ticket)
                head = style_sync.git(worktree, "rev-parse", "HEAD")
                blob = ledger.pinned_rule_texts(ticket)[0].blob
                ledger.record(rule, source, head, blob, "clean", ticket=ticket, worker="test")

        unlink = Path.unlink
        def stop_before_journal_removal(path: Path, missing_ok: bool = False) -> None:
            if path == journal:
                raise RuntimeError("publication stopped after checkpoint record")
            unlink(path, missing_ok=missing_ok)

        for case, interrupted in (("ordinary merge", False), ("stopped publication", True)):
            with self.subTest(case=case), mock.patch.object(
                    style_plan, "run_verification_step", return_value=subprocess.CompletedProcess([], 0)):
                with style_ledger.open_ledger(self.repo) as ledger:
                    ledger.set_tip(self.tip, "main")
                if interrupted:
                    assign("3001", "src/lib.rs")
                    with mock.patch.object(Path, "unlink", stop_before_journal_removal):
                        with self.assertRaisesRegex(RuntimeError, "publication stopped after checkpoint record"):
                            _ = style_plan.publish_checkpoint(worktree, "3001", run, "1", "First checkpoint\n")
                    self.assertTrue(journal.exists())
                    with style_ledger.open_ledger(worktree) as ledger:
                        self.assertEqual(ledger.ticket_get("3001")["state"], "checkpointed")
                    self.assertEqual(style_sync.git(worktree, "status", "--porcelain"), "")
                _ = (self.repo / "src/lib.rs").write_text(f"fn changed_{int(interrupted)}() {{}}\n")
                if interrupted:
                    _ = (self.repo / "src/later.rs").write_text("fn later() {}\n")
                    _ = self.git("add", "src/later.rs")
                _ = self.git("commit", "-qam", "Change main")
                tip = self.git("rev-parse", "HEAD")
                arguments = ["own-handoff", "--repo", str(self.repo), "--tip", tip,
                             "--style-branch", "main", "--unit", "names"]
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(style_sync.main(arguments), 0)
                with style_ledger.open_ledger(self.repo) as ledger, closing(sqlite3.connect(ledger.database)) as connection:
                    self.assertEqual(ledger.synced_commit(), tip)
                    self.assertEqual(connection.execute("SELECT style_branch FROM sync").fetchone(), ("refs/heads/main",))
                if interrupted:
                    self.assertEqual(style_sync.git(worktree, "merge-base", "HEAD", tip), tip)
                    self.assertFalse(journal.exists())
                    _ = self.git("merge", "--no-edit", "unit-names")
                    with style_ledger.open_ledger(worktree) as ledger:
                        _ = ledger.ticket_merge("3001")
                    assign("3002", "src/later.rs")
                    checkpoint = style_plan.publish_checkpoint(worktree, "3002", run, "2", "Later checkpoint\n")
                    self.assertEqual(style_sync.git(worktree, "rev-parse", "HEAD"), checkpoint)
                    with style_ledger.open_ledger(worktree) as ledger:
                        self.assertEqual(ledger.ticket_get("3002")["checkpoint"], checkpoint)

    def test_processed_tip_does_not_skip_a_second_direct_units_main_merge(self) -> None:
        self.direct_production()
        _ = (self.repo / "src/lib.rs").write_text("fn changed() {}\n")
        _ = self.git("commit", "-qam", "Change main")
        tip = self.git("rev-parse", "HEAD")
        _ = style_sync.git(self.root / "direct-names", "merge", "--no-ff", "-qm", "Sync main", "main")
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.set_tip(tip, "main")
            ledger.tip_done(tip)
        arguments = ["own-handoff", "--repo", str(self.repo), "--tip", tip,
                     "--style-branch", "main", "--unit", "types"]
        self.assertEqual(self.git("rev-parse", "unit-types"), self.tip)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(style_sync.main(arguments), 0)
        self.assertNotEqual(self.git("rev-parse", "unit-types"), self.tip)
        self.assertEqual(self.git("merge-base", "unit-types", tip), tip)

    def test_direct_merge_refuses_assigned_or_dirty_work_and_repeats_the_open_ticket_proof(self) -> None:
        self.direct_production()
        worktree = self.root / "direct-names"
        _ = (self.root / "nate_style/rust/names.md").write_text("---\ntier: C\nfamily: names\ninput_scope: file\n---\n# Names\n")
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.set_tip(self.tip, "main")
            rule = "global:rust/names.md"
            ledger.ticket_add("3001", ticket_owner_id("names"), "read", "names", ".", ["src/lib.rs"], [rule])
            ledger.ticket_assign("3001", "unit-names")
            _ = (self.repo / "main.txt").write_text("Main change.\n")
            _ = self.git("add", "main.txt")
            _ = self.git("commit", "-qm", "Change main")
            tip = self.git("rev-parse", "HEAD")
            arguments = ["own-handoff", "--repo", str(self.repo), "--tip", tip,
                         "--style-branch", "main", "--unit", "names"]
            self.assertEqual(style_sync.main(arguments), 4)
            self.assertEqual(self.git("rev-parse", "unit-names"), self.tip)
            blob = ledger.pinned_rule_texts("3001")[0].blob
            ledger.record(rule, "src/lib.rs", self.tip, blob, "clean", ticket="3001", worker="test")
            ledger.ticket_checkpoint("3001", self.tip)
            partial = worktree / "partial.rs"
            _ = partial.write_text("fn partial(\n")
            self.assertEqual(style_sync.main(arguments), 4)
            self.assertEqual(self.git("rev-parse", "unit-names"), self.tip)
            partial.unlink()
            pending = style_ledger.publication_path(worktree, "pending-sync")
            _ = pending.write_text(tip + "\n")
            original_proof = ledger.check_ticket_pipeline
            def prove(ticket: str) -> None:
                self.assertEqual(style_sync.git(worktree, "merge-base", "HEAD", tip), tip)
                with style_ledger.publication_path(worktree, "publication.lock").open("a") as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                original_proof(ticket)
            with mock.patch.object(style_ledger.Ledger, "check_ticket_pipeline", side_effect=prove) as proof, redirect_stdout(io.StringIO()):
                self.assertEqual(style_sync.main(arguments), 0)
            proof.assert_called_once_with("3001")
            self.assertFalse(pending.exists())

    def test_direct_startup_reconciles_main_without_parent_lookup_or_handoff_replay(self) -> None:
        self.direct_production()
        with (mock.patch.object(style_sync, "runner_value") as lookup,
              mock.patch.object(style_sync, "replay_handoffs") as replay,
              redirect_stdout(io.StringIO())):
            self.assertEqual(style_sync.main([
                "resume", "--repo", str(self.repo), "--production", "names",
                "--branch-point", "unused", "--style-branch", "unused",
            ]), 0)
        lookup.assert_not_called()
        replay.assert_not_called()
        with style_ledger.open_ledger(self.repo) as ledger, closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(ledger.synced_commit(), self.tip)
            self.assertEqual(connection.execute("SELECT branch_point FROM reconciliation").fetchone(), ("main",))
            self.assertEqual(connection.execute("SELECT style_branch FROM sync").fetchone(), ("refs/heads/main",))

    def test_send_handoff_uses_the_existing_path_without_route_discovery(self) -> None:
        self.direct_production()
        with (mock.patch.object(style_sync, "runner_value", return_value="") as lookup,
              mock.patch.object(style_sync, "sync_productions", side_effect=AssertionError("no route discovery")),
              redirect_stdout(io.StringIO())):
            self.assertEqual(style_sync.main([
                "send-handoff", "--repo", str(self.repo), "--production", "names", "--checkpoint", self.tip,
            ]), 0)
        lookup.assert_called_once_with("parent", "names")
        self.assertEqual(self.handoff_rows()[0][:3], (self.tip, "pending", ""))

    def test_unavailable_wait_owner_does_not_block_a_later_healthy_wait(self) -> None:
        self.production()
        self.waiting_units("names")
        self.wake_sender()
        with style_ledger.open_ledger(self.repo) as ledger, closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("INSERT INTO sync_waits (unit,since,token) VALUES ('missing','2000-01-01','bad-wait')")
        output = io.StringIO()
        with redirect_stdout(output):
            style_sync.wake_waiters(self.repo, "14")
        self.assertIn("style_ledger: wake missing:", output.getvalue())
        self.assertEqual(self.remaining_waiters(), ["missing"])
        self.assertEqual([arguments[1] for arguments in self.recorded_wakes()], ["session:fake-names"])

    def test_ticket_merge_wakes_two_waiters_once_and_clears_their_rows(self) -> None:
        self.production()
        self.checkpoint_for_wake()
        self.waiting_units("names", "types")
        self.wake_sender()
        self.assertEqual(self.merge_for_wake(), (0, "", ""))
        self.assertEqual(self.recorded_wakes(), [
            ["--to", "session:fake-" + unit, "--from", "sample-style", "--text",
             "From the showrunner of sample-style: ticket 3001 merged; refill.", "--timeout", "10"]
            for unit in ("names", "types")
        ])
        self.assertEqual(self.remaining_waiters(), [])
        self.assertEqual((self.root / "roster-args").read_text(), "--json")
        self.assertEqual(self.merge_for_wake(), (0, "", ""))
        self.assertEqual(len(self.recorded_wakes()), 2)

    def test_a_send_is_given_send_pys_whole_run_before_it_counts_as_timed_out(self) -> None:
        """The caller must allow delivery and relay cleanup before its timeout."""
        from scripts.lib import conf
        kill_grace = conf.read("send").number("kill_grace")
        self.assertGreater(send_budget.seconds(style_sync.SEND_SECONDS), style_sync.SEND_SECONDS + kill_grace)

    def test_handoff_budget_tracks_a_raised_cleanup_grace_without_reload(self) -> None:
        config = self.root / "send-budget-config"
        config.mkdir()
        for name in ("send", "postmaster", "codex_daemon"):
            source = Path(__file__).resolve().parents[2] / "config" / f"{name}.conf"
            _ = (config / source.name).write_text(source.read_text())
        postmaster_config = config / "postmaster.conf"
        _ = postmaster_config.write_text(postmaster_config.read_text().replace("claim_grace_seconds=15.0", "claim_grace_seconds=2.0"))
        with mock.patch.dict(os.environ, {"CONF_DIR": str(config)}):
            budgets: list[float] = []
            for grace in (10, 80):
                source = Path(__file__).resolve().parents[2] / "config/send.conf"
                _ = (config / "send.conf").write_text(source.read_text().replace("kill_grace=10", f"kill_grace={grace}"))
                with (mock.patch.object(style_sync, "runner_value", return_value="session:fixture"),
                      mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "SENT: fixture")) as sender):
                    self.assertEqual(style_sync.handoff_notice("style", "parent", "checkpoint"), "sent")
                budgets.append(cast(float, sender.call_args.kwargs["timeout"]))
            self.assertEqual(budgets, [150, 290])
            self.assertGreater(budgets[1], style_sync.SEND_SECONDS + 80)

    def test_handoff_budget_tracks_raised_postmaster_wait_and_claim_grace_without_reload(self) -> None:
        config = self.root / "postmaster-budget-config"
        config.mkdir()
        for name in ("send", "postmaster", "codex_daemon"):
            source = Path(__file__).resolve().parents[2] / "config" / f"{name}.conf"
            _ = (config / source.name).write_text(source.read_text())
        with mock.patch.dict(os.environ, {"CONF_DIR": str(config)}):
            budgets: list[float] = []
            for wait, grace in ((3, 2), (120, 40)):
                source = Path(__file__).resolve().parents[2] / "config/postmaster.conf"
                _ = (config / "postmaster.conf").write_text(source.read_text()
                    .replace("wait_seconds=3.0", f"wait_seconds={wait}")
                    .replace("claim_grace_seconds=15.0", f"claim_grace_seconds={grace}"))
                with (mock.patch.object(style_sync, "runner_value", return_value="session:fixture"),
                      mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "SENT: fixture")) as sender):
                    self.assertEqual(style_sync.handoff_notice("style", "parent", "checkpoint"), "sent")
                budgets.append(cast(float, sender.call_args.kwargs["timeout"]))
            self.assertEqual(budgets[1] - budgets[0], 155)
            self.assertGreater(budgets[1], 120 + 40 + style_sync.SEND_SECONDS)

    def test_an_older_in_flight_wake_cannot_erase_the_units_new_wait(self) -> None:
        self.production()
        self.waiting_units("names")
        self.wake_sender()
        with style_ledger.open_ledger(self.repo) as ledger:
            first = ledger.sync_waits()[0]
        interleaved = False

        def refill_during_send(arguments: list[str], **options: object) -> object:
            nonlocal interleaved
            if (len(arguments) > 1 and arguments[1] == str(session_send.SEND_SCRIPT)
                    and options.get("timeout") == send_budget.seconds(style_sync.SEND_SECONDS) and not interleaved):
                interleaved = True
                style_sync.wake_waiters(self.repo, "3002")
                with style_ledger.open_ledger(self.repo) as ledger:
                    self.assertEqual(ledger.sync_waits(), ())
                    # Equal timestamps ensure that only a wait identity protects the new row.
                    with mock.patch.object(style_ledger, "_now", return_value=first.since):
                        ledger.wait_for_sync(ticket_owner_id("names"))
            return cast(object, mock.DEFAULT)

        with mock.patch.object(subprocess, "run", wraps=subprocess.run, side_effect=refill_during_send):
            style_sync.wake_waiters(self.repo, "3001")
        self.assertTrue(interleaved)
        self.assertEqual(self.remaining_waiters(), [style_unit_id("names")])
        with style_ledger.open_ledger(self.repo) as ledger:
            second = ledger.sync_waits()[0]
        self.assertEqual((second.unit, second.since), (first.unit, first.since))
        self.assertNotEqual(second.token, first.token)
        self.assertEqual([call[5] for call in self.recorded_wakes()], [
            "From the showrunner of sample-style: ticket 3002 merged; refill.",
            "From the showrunner of sample-style: ticket 3001 merged; refill.",
        ])

    def test_failed_wake_keeps_only_that_waiter_and_an_already_merged_ticket_retries_it(self) -> None:
        self.production()
        self.checkpoint_for_wake()
        self.waiting_units("names", "types")
        self.wake_sender("names")
        self.assertEqual(self.merge_for_wake(), (0, f"style_ledger: wake {style_unit_id('names')}: send.py: stdout: FAILED: unavailable\n", ""))
        self.assertEqual(self.remaining_waiters(), [style_unit_id("names")])
        with style_ledger.open_ledger(self.repo) as ledger, closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(connection.execute("SELECT state FROM tickets WHERE id='3001'").fetchone(), ("merged",))
        self.wake_sender()
        self.assertEqual(self.merge_for_wake(), (0, "", ""))
        self.assertEqual(self.remaining_waiters(), [])
        self.assertEqual([arguments[1] for arguments in self.recorded_wakes()], ["session:fake-names", "session:fake-types", "session:fake-names"])

    def test_merge_without_waiters_or_a_standing_production_sends_nothing(self) -> None:
        self.checkpoint_for_wake()
        self.waiting_units()
        self.production()
        self.wake_sender()
        self.assertEqual(self.merge_for_wake(), (0, "", ""))
        self.doc.unlink()
        self.waiting_units("names")
        self.assertEqual(self.merge_for_wake(), (0, "", ""))
        self.assertEqual(self.recorded_wakes(), [])
        self.assertEqual(self.remaining_waiters(), [style_unit_id("names")])

    def test_configured_direct_route_wakes_waiters_without_a_style_showrunner(self) -> None:
        self.direct_production()
        self.waiting_units("names", "types")
        self.wake_sender()
        self.assertIsInstance(standing_style_production(self.repo), NoStyleProduction)
        style_sync.wake_waiters(self.repo, "3001")
        self.assertEqual([arguments[1] for arguments in self.recorded_wakes()],
                         ["session:fake-names", "session:fake-types"])
        self.assertEqual(self.remaining_waiters(), [])

    def test_main_move_notifies_only_configured_direct_units(self) -> None:
        self.direct_production()
        self.wake_roster(("names", "types", "modules"))
        self.handoff_sender("SENT: fixture", 0)
        with mock.patch.object(style_sync, "runner_value") as lookup:
            self.assertEqual(self.notice(), (0, ""))
        lookup.assert_not_called()
        notices = self.handoff_notices()
        self.assertEqual([notice[1] for notice in notices], ["session:fake-names", "session:fake-types"])
        self.assertEqual([notice[notice.index("--key") + 1] for notice in notices],
                         ["style-sync:names", "style-sync:types"])
        self.assertTrue(all(f"From style_sync: branch point main moved to {self.tip[:7]}; sync." in notice
                            for notice in notices))
        style_sync.notify(self.repo, "another", self.tip)
        self.assertEqual(self.handoff_notices(), notices)

    def test_director_without_an_address_does_not_block_other_waiters(self) -> None:
        self.production()
        self.checkpoint_for_wake()
        self.waiting_units("names", "types")
        self.wake_sender()
        roster = self.root / "roster.py"
        _ = roster.write_text(roster.read_text().replace("session:fake-names", ""))
        self.assertEqual(self.merge_for_wake(), (0, f"style_ledger: wake {style_unit_id('names')}: no live director\n", ""))
        self.assertEqual(self.recorded_wakes(), [[
            "--to", "session:fake-types", "--from", "sample-style", "--text",
            "From the showrunner of sample-style: ticket 3001 merged; refill.", "--timeout", "10",
        ]])
        self.assertEqual(self.remaining_waiters(), [style_unit_id("names")])

    def test_merge_without_a_matching_director_keeps_the_wait_and_reports_why(self) -> None:
        self.production()
        self.checkpoint_for_wake()
        self.waiting_units("names")
        self.wake_sender()
        self.wake_roster(())
        self.assertEqual(self.merge_for_wake(), (0, f"style_ledger: wake {style_unit_id('names')}: no live director\n", ""))
        self.assertEqual(self.recorded_wakes(), [])
        self.assertEqual(self.remaining_waiters(), [style_unit_id("names")])

    def test_ambiguous_or_unreadable_roster_does_not_send_or_forget_the_wait(self) -> None:
        self.production()
        self.checkpoint_for_wake()
        self.waiting_units("names")
        self.wake_sender()
        self.wake_roster(("names", "names"))
        self.assertEqual(self.merge_for_wake(), (0, f"style_ledger: wake {style_unit_id('names')}: 2 directors\n", ""))
        self.assertEqual(self.recorded_wakes(), [])
        self.assertEqual(self.remaining_waiters(), [style_unit_id("names")])
        _ = (self.root / "roster.py").write_text("print('roster unavailable'); raise SystemExit(3)\n")
        self.assertEqual(self.merge_for_wake(), (0, f"style_ledger: wake {style_unit_id('names')}: roster: roster.py exited 3\n", ""))
        self.assertEqual(self.recorded_wakes(), [])
        self.assertEqual(self.remaining_waiters(), [style_unit_id("names")])

    def test_refused_checkpoint_merge_does_not_wake_or_clear_waiters(self) -> None:
        self.production()
        self.checkpoint_for_wake(on_style_branch=False)
        self.waiting_units("names")
        self.wake_sender()
        code, output, errors = self.merge_for_wake()
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("is not on refs/heads/style/main", errors)
        self.assertEqual(self.recorded_wakes(), [])
        self.assertEqual(self.remaining_waiters(), [style_unit_id("names")])

    def test_no_style_production_and_another_branch_point_are_silent(self) -> None:
        for retired in (False, True):
            with self.subTest(retired=retired):
                if retired:
                    doc = self.repo / "retired-production.md"
                    _ = doc.write_text(
                        f"# Production\n- **Kind:** product\n- **Merge branch:** main\n"
                        + f"- **Showrunner checkout:** {self.repo}\n")
                    write_style_units(doc, (StyleUnitAssignment(
                        "sample (retired 2026-11-09)", ("names", "values", "types", "modules"), 0),))
                    _ = recorded_showrunner("sample-parent", doc)
                with mock.patch.object(style_sync, "_waiting_directors",
                                       side_effect=AssertionError("no roster read")) as roster:
                    self.assertIsInstance(style_sync.direct_route(self.repo), style_sync.NoDirectRoute)
                    self.assertEqual(self.notice(), (0, ""))
                roster.assert_not_called()
        self.production("init/catalyst")
        self.assertEqual(self.notice(), (0, ""))
        self.assertFalse((self.root / "sent.json").exists())

    def test_found_production_sends_the_keyed_sync_message_to_its_current_address(self) -> None:
        self.production()
        for retired in (False, True):
            with self.subTest(retired=retired):
                if retired:
                    write_style_units(self.doc, (StyleUnitAssignment(
                        "sample (retired 2026-11-09)", ("names", "values", "types", "modules"), 0),))
                (self.root / "sent.json").unlink(missing_ok=True)
                with mock.patch("scripts.style.style_sync.subprocess.run", wraps=subprocess.run) as calls:
                    self.assertEqual(self.notice(), (0, ""))
                call = cast(RecordedCall, json.loads((self.root / "sent.json").read_text()))
                arguments = call["arguments"]
                self.assertEqual(arguments, [
                    "--to", "session:fake-style", "--from", "style_sync", "--text",
                    f"From style_sync: branch point main moved to {self.tip[:7]}; sync.",
                    "--key", "style-sync:sample-style", "--timeout", "10",
                ])
                self.assertLess(int(arguments[-1]), cast(int, calls.call_args_list[-1].kwargs["timeout"]))

    def test_linked_worktree_notification_finds_the_same_production(self) -> None:
        self.production()
        unit = self.root / "unit"
        _ = self.git("worktree", "add", "-qb", "unit", str(unit))
        self.assertEqual(self.notice(unit), (0, ""))
        call = cast(RecordedCall, json.loads((self.root / "sent.json").read_text()))
        self.assertIn("style-sync:sample-style", call["arguments"])

    def test_send_failure_reports_once_and_does_not_fail_the_merge(self) -> None:
        self.production()
        _ = (self.root / "send.py").write_text("print('FAILED: unavailable')\nraise SystemExit(3)\n")
        code, output = self.notice()
        self.assertEqual(code, 0)
        self.assertEqual(output, "style_sync: notify: send.py: stdout: FAILED: unavailable\n")

    def test_notify_requires_a_confirmed_success_outcome(self) -> None:
        self.production()
        cases = ((0, "SENT: fixture", True), (0, "SKIPPED: repeated", True),
                 (0, "KEPT: silence", True), (0, "REPLACED: revision", True),
                 (1, "QUEUED: offline", False), (3, "FAILED: unavailable", False),
                 (2, "REFUSED: invalid", False), (4, "HELD: decision", False), (5, "PENDING: active", False), (6, "RETRY: active", False),
                 (0, "", False), (0, "QUEUED: inconsistent", False),
                 (1, "SENT: inconsistent", False), (0, "FAILED: inconsistent", False))
        for code, text, accepted in cases:
            with self.subTest(code=code, text=text):
                _ = (self.root / "send.py").write_text(f"print({text!r})\nraise SystemExit({code})\n")
                result, output = self.notice()
                self.assertEqual(result, 0)
                if accepted:
                    self.assertEqual(output, "")
                else:
                    self.assertIn("style_sync: notify: send.py:", output)

    def test_notify_lookup_refusal_prints_one_prefix_and_keeps_the_merge_successful(self) -> None:
        with mock.patch.object(style_sync, "sync_productions",
                               side_effect=ValueError("style_sync: invalid production")):
            self.assertEqual(self.notice(), (0, "style_sync: notify: invalid production\n"))
        self.assertFalse((self.root / "sent.json").exists())

    def test_recorded_workspace_output_is_counted_by_tool_and_by_file(self) -> None:
        records = {record.tool: record for record in style_sync.lint_records(RECORDED_LINT, self.repo)}
        self.assertEqual({tool: record.diagnostics for tool, record in records.items()},
                         {"mend": {"src/lib.rs": 1}, "rustfmt": {"src/lib.rs": 1}, "clippy": {"src/lib.rs": 2}})
        self.assertTrue(all(not record.passed for record in records.values()))
        with self.assertRaises(TypeError):
            cast(dict[str, int], records["mend"].diagnostics)["src/lib.rs"] = 99
        self.assertEqual(records["mend"].diagnostics["src/lib.rs"], 1)
        incomplete = style_sync.lint_records(CLEAN_LINT.removesuffix("style-lint: clippy passed\n"), self.repo)
        self.assertFalse(next(record.passed for record in incomplete if record.tool == "clippy"))
        self.assertTrue(all(not record.passed for record in style_sync.lint_records("checks skipped\n", self.repo)))

    def test_workspace_verify_covers_all_members_and_reports_completed_tools(self) -> None:
        fixture = verify_fixture.VerifyLintScopeTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        result = fixture.verify("lint", "--no-cache")
        self.assertEqual(fixture.calls("+nightly"), [
            ["fmt", "--version"], ["fmt", "--all"],
        ])
        self.assertEqual(fixture.calls("clippy"), [
            ["--workspace", "--all-targets", "--all-features", "--keep-going", "--", "-D", "warnings"],
        ])
        self.assertEqual(fixture.calls("mend"), [["--all-targets", "--workspace", "--fix"]])
        self.assertTrue(all(record.passed for record in style_sync.lint_records(result.stdout, fixture.root)))

    def test_workspace_check_reports_without_fixing_and_continues_after_a_tool_fails(self) -> None:
        fixture = verify_fixture.VerifyLintScopeTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        cargo = fixture.root / "bin/cargo"
        fixture.write_executable(cargo, verify_fixture.CARGO_STUB + '''
if arguments[:1] == ["mend"]:
    if "--fix" in arguments:
        Path("src/lib.rs").write_text("rewritten by mend")
    print("warning: import module instead of item\\n --> src/lib.rs:1:1")
    raise SystemExit(1)
if arguments[:2] == ["+nightly", "fmt"] and "--version" not in arguments:
    if "--check" not in arguments:
        Path("src/lib.rs").write_text("rewritten by rustfmt")
    print("Diff in src/lib.rs:2:")
    raise SystemExit(1)
''')
        source = fixture.root / "src/lib.rs"
        _ = source.write_text("pub fn value()->u8{1}\n")
        before = source.read_bytes()
        result = fixture.invoke_verify("lint", "--check", "--no-cache", cwd=fixture.root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(fixture.calls("mend"), [["--all-targets", "--workspace", "--fail-on-warn"]])
        self.assertEqual(fixture.calls("+nightly"), [["fmt", "--version"], ["fmt", "--all", "--check"]])
        self.assertEqual(fixture.calls("clippy"), [[
            "--workspace", "--all-targets", "--all-features", "--", "-D", "warnings",
        ]])
        records = {record.tool: record for record in style_sync.lint_records(result.stdout, fixture.root)}
        self.assertEqual(records["mend"].diagnostics, {"src/lib.rs": 1})
        self.assertEqual(records["rustfmt"].diagnostics, {"src/lib.rs": 1})
        self.assertFalse(records["mend"].passed)
        self.assertFalse(records["rustfmt"].passed)
        self.assertTrue(records["clippy"].passed)

    def test_lint_runs_workspace_gate_on_requested_tip_and_records_each_tool(self) -> None:
        verify = self.root / "verify.sh"
        _ = verify.write_text(
            '#!/bin/bash\n[[ "$*" == "lint --check --no-cache" ]] || exit 2\n'
            + f'git rev-parse HEAD > "{self.root / "checked-tip"}"\n'
            + "cat <<'OUTPUT'\n" + RECORDED_LINT + "OUTPUT\n",
        )
        # The style checkout can contain fixes that are not part of the requested synced tip.
        _ = (self.repo / "src/lib.rs").write_text("fn style_fix() {}\n")
        _ = self.git("commit", "-qam", "Style changes after tip")
        original_worktrees = self.git("worktree", "list", "--porcelain")
        with mock.patch.object(style_sync, "VERIFY", verify), redirect_stdout(io.StringIO()):
            self.assertEqual(style_sync.main(["lint", "--repo", str(self.repo), "--checkout", str(self.repo),
                                              "--tip", self.tip]), 1)
        self.assertEqual((self.root / "checked-tip").read_text().strip(), self.tip)
        self.assertEqual(self.git("worktree", "list", "--porcelain"), original_worktrees)
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            rows = cast(list[tuple[str, str, int, str]], connection.execute(
                "SELECT tool,tip,passed,diagnostics_json FROM lint_runs ORDER BY tool",
            ).fetchall())
        self.assertEqual([(tool, tip, passed) for tool, tip, passed, _ in rows],
                         [(tool, self.tip, 0) for tool in ("clippy", "mend", "rustfmt")])
        self.assertEqual([cast(dict[str, int], json.loads(diagnostics)) for _, _, _, diagnostics in rows],
                         [{"src/lib.rs": 2}, {"src/lib.rs": 1}, {"src/lib.rs": 1}])

    def test_clean_check_mode_records_all_tools_passing_and_returns_zero(self) -> None:
        verify = self.root / "verify.sh"
        metadata = self.lint_temp / f"{style_sync.lint_prefix()}*"
        _ = verify.write_text(f"#!/bin/bash\ncp {metadata}/owner.json {self.root / 'owner-seen'}\n"
                              + f"cp {metadata}/{style_sync.VERIFIER_FILE} {self.root / 'verifier-seen'}\n"
                              + f"echo $$ > {self.root / 'verify-pid'}\ncat <<'OUTPUT'\n" + CLEAN_LINT + "OUTPUT\n")
        with mock.patch.object(style_sync, "VERIFY", verify), redirect_stdout(io.StringIO()):
            self.assertEqual(style_sync.main(["lint", "--repo", str(self.repo), "--checkout", str(self.repo),
                                              "--tip", self.tip]), 0)
        self.assertEqual(cast(object, json.loads((self.root / "owner-seen").read_text())), {
            "owner": os.getpid(), "repo": self.git("rev-parse", "--path-format=absolute", "--git-common-dir"),
        })
        self.assertEqual((self.root / "verifier-seen").read_text(), (self.root / "verify-pid").read_text().strip())
        self.assertEqual(list(self.lint_temp.iterdir()), [])
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            rows = cast(list[tuple[str, int, str]], connection.execute(
                "SELECT tool,passed,diagnostics_json FROM lint_runs ORDER BY tool",
            ).fetchall())
        self.assertEqual(rows, [(tool, 1, "{}") for tool in ("clippy", "mend", "rustfmt")])

    def test_lint_rewrites_cannot_be_recorded_as_successful_coverage_of_the_original_tip(self) -> None:
        verify = self.root / "verify.sh"
        _ = verify.write_text("#!/bin/bash\necho 'fn rewritten() {}' > src/lib.rs\ncat <<'OUTPUT'\n"
                              + RECORDED_LINT + "OUTPUT\n")
        with mock.patch.object(style_sync, "VERIFY", verify), redirect_stdout(io.StringIO()):
            self.assertEqual(style_sync.main(["lint", "--repo", str(self.repo), "--checkout", str(self.repo),
                                              "--tip", self.tip]), 1)
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            rows = cast(list[tuple[int]], connection.execute("SELECT passed FROM lint_runs").fetchall())
        self.assertEqual(rows, [(0,), (0,), (0,)])
        self.assertEqual((self.repo / "src/lib.rs").read_text(), "fn value() {}\n")

    def clean_verify(self, before: str = "") -> Path:
        verify = self.root / "verify.sh"
        _ = verify.write_text(f"#!/bin/bash\nmkdir -p target\n{before}cat <<'OUTPUT'\n" + CLEAN_LINT + "OUTPUT\n")
        return verify

    def run_lint(self, before: str = "") -> tuple[int, str]:
        output = io.StringIO()
        with mock.patch.object(style_sync, "VERIFY", self.clean_verify(before)), redirect_stdout(output), \
                redirect_stderr(output):
            code = style_sync.main(["lint", "--repo", str(self.repo), "--checkout", str(self.repo), "--tip", self.tip])
        return code, output.getvalue()

    def stale_dir(self, name: str, owner: int | None, *, registered: bool, verifier_group: int = 0) -> Path:
        directory = self.lint_temp / f"{style_sync.lint_prefix()}{name}"
        directory.mkdir()
        if owner is not None:
            common_dir = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir"))
            style_sync.OwnerRecord(owner, common_dir).write(directory)
        if verifier_group:
            _ = (directory / style_sync.VERIFIER_FILE).write_text(str(verifier_group))
        if registered:
            _ = self.git("worktree", "add", "--detach", str(directory / "checkout"), self.tip)
        return directory

    def standing_target(self) -> Path:
        return self.root / "gate-home/worktrees/gates/sample/sample-style-gate/target"

    def test_standing_target_survives_task_end_and_reuses_artifacts(self) -> None:
        target = self.standing_target()
        code, output = self.run_lint('printf previous > target/artifact\n')
        self.assertEqual(code, 0, output)
        self.assertEqual(scratch.end("run:sample-style"), [])
        self.assertTrue(target.is_dir())
        self.assertEqual((target / "artifact").read_text(), "previous")
        code, output = self.run_lint('[[ $(cat target/artifact) == previous ]] || exit 2\n')
        self.assertEqual(code, 0, output)
        self.assertEqual(list(self.lint_temp.iterdir()), [])

    def test_lint_keeps_intermediates_with_the_standing_warm_target(self) -> None:
        target = self.standing_target()
        code, output = self.run_lint(
            '[[ "$CARGO_BUILD_BUILD_DIR" == "$(realpath target)/intermediates" ]] || exit 2\n'
            + 'printf warm > "$CARGO_BUILD_BUILD_DIR/intermediate"\n')
        self.assertEqual(code, 0, output)
        self.assertEqual((target / "intermediates/intermediate").read_text(), "warm")
        self.assertEqual(list(self.lint_temp.iterdir()), [])

    def test_lint_keeps_its_own_warm_target_after_metadata_cleanup(self) -> None:
        target = self.standing_target()
        code, output = self.run_lint('[[ -d target && -L target ]] || exit 2\nprintf warm > target/artifact\n')
        self.assertEqual(code, 0, output)
        self.assertEqual((target / "artifact").read_text(), "warm")
        self.assertEqual(list(self.lint_temp.iterdir()), [])
        self.assertNotIn(str(target), scratch.holds())
        # A later checkout uses the same artifacts. No Cargo environment variable is overridden.
        with mock.patch.dict(os.environ, {"CARGO_TARGET_DIR": "inherited-value"}):
            code, output = self.run_lint(
                '[[ -d target && -L target && $(cat target/artifact) == warm && $CARGO_TARGET_DIR == inherited-value ]] || exit 2\n'
            )
        self.assertEqual(code, 0, output)
        self.assertEqual(list(self.lint_temp.iterdir()), [])
        self.assertEqual(scratch.end("run:sample-style"), [])
        self.assertTrue(target.exists())
        self.assertEqual(scratch.holds(), {})

    def test_a_missing_or_swept_target_runs_cold(self) -> None:
        target = self.standing_target()
        for state in ("missing", "swept"):
            with self.subTest(state=state):
                self.assertFalse(target.exists())
                code, output = self.run_lint(
                    '[[ -d target && -L target && ! -e target/artifact ]] || exit 2\nprintf fresh > target/artifact\n'
                )
                self.assertEqual(code, 0, output)
                self.assertEqual((target / "artifact").read_text(), "fresh")
                self.assertEqual(list(self.lint_temp.iterdir()), [])
                shutil.rmtree(target)

    def test_lint_without_a_production_runs_in_the_configured_role_pool(self) -> None:
        observed = self.root / "fallback-checkout"
        with (mock.patch.object(style_sync, "gate_owner", self.resolve_owner),
              mock.patch.object(style_sync, "standing_style_production", return_value=NoStyleProduction(self.repo, "sample-style")),
              mock.patch.object(showrunners, "own_name", return_value=""),
              mock.patch.object(copies, "fallback_owner", return_value="configured-style")):
            code, output = self.run_lint(f"pwd -P > {observed}\n")
        self.assertEqual(code, 0, output)
        checkout = Path(observed.read_text().strip())
        self.assertEqual(checkout.name, "checkout")
        self.assertFalse(checkout.exists())
        self.assertTrue((copies.gates_root() / self.repo.name / "configured-style-gate/target").is_dir())
        self.assertEqual(list(self.lint_temp.iterdir()), [])

    def test_lint_takes_second_checkout_when_first_is_busy(self) -> None:
        observed = self.root / "checked-directory"
        with copies.standing("sample-style", self.repo, copies.CommittedSource(self.tip)) as first:
            probe = """import fcntl
from pathlib import Path
with (Path('target').resolve().parent / '.claude-busy').open('r+') as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    else:
        raise SystemExit('lint released its checkout while verifying')
"""
            code, output = self.run_lint(
                f"pwd -P > {observed}\n{shlex.quote(str(Path(__file__).resolve().parents[1] / "lib/py"))} -c {shlex.quote(probe)} || exit $?\n")
            self.assertEqual(code, 0, output)
            second = Path(observed.read_text().strip())
            self.assertNotEqual(first, second)
            self.assertEqual(second.name, "checkout")
            second_cache = self.standing_target().parent.with_name("sample-style-gate-2")
            self.assertTrue((second_cache / "target").is_dir())
            self.assertFalse(second.exists())
            self.assertTrue((first / ".git").is_dir())
            with (second_cache / ".claude-busy").open("r+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)
        with copies.standing("sample-style", self.repo, copies.CommittedSource(self.tip)) as reused:
            self.assertNotEqual(reused, first)
            self.assertEqual((reused / "target").resolve(), self.standing_target())
        self.assertEqual(list(self.lint_temp.iterdir()), [])

    def test_redirecting_the_target_cannot_grant_successful_lint_coverage(self) -> None:
        outside = self.root / "outside-target"
        outside.mkdir()
        code, output = self.run_lint(f'rm -f target\nln -s {outside} target\n')
        self.assertEqual(code, 1, output)
        self.assertIn("lint rewrote the checked tree", output)
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            self.assertEqual(connection.execute("SELECT passed FROM lint_runs").fetchall(), [(0,), (0,), (0,)])

    def running_group(self) -> int:
        group = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(group.wait)
        self.addCleanup(group.kill)
        return group.pid

    def test_a_stop_signal_during_verify_removes_the_checkout_stops_its_processes_and_exits_with_the_signal(
            self) -> None:
        driver = self.root / "lint_driver.py"
        _ = driver.write_text(LINT_DRIVER)
        original_worktrees = self.git("worktree", "list", "--porcelain")
        cases = ((signal.SIGTERM, ""), (signal.SIGHUP, ""), (signal.SIGTERM, "trap '' TERM\n"))
        for signum, trap in cases:
            with self.subTest(signal=signum.name, ignores_term=bool(trap)):
                pids = self.root / f"pids-{signum.name}-{bool(trap)}"
                verify = self.root / "verify.sh"
                _ = verify.write_text(f"#!/bin/bash\n{trap}sleep 300 &\n"
                                      + f'echo "$$ $!" > {pids}.part && mv {pids}.part {pids}\nwait\n')
                process = subprocess.Popen(
                    [str(Path(style_sync.__file__).resolve().parents[1] / "lib" / "py"), str(driver), str(verify), str(self.lint_temp),
                     "lint", "--repo", str(self.repo), "--checkout", str(self.repo), "--tip", self.tip],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                try:
                    self.assertTrue(wait_until(lambda: pids.exists() or process.poll() is not None, TEST_HANG_GUARD_SECS))
                    self.addCleanup(kill_group, int(pids.read_text().split()[0]))
                    process.send_signal(signum)
                    _, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                finally:
                    if process.poll() is None:
                        process.kill()
                        _ = process.communicate()
                self.assertEqual(process.returncode, 128 + signum, stderr)
                self.assertIn(f"lint stopped by {signum.name}", stderr)
                self.assertEqual(list(self.lint_temp.iterdir()), [])
                self.assertEqual(self.git("worktree", "list", "--porcelain"), original_worktrees)
                child, grandchild = (int(pid) for pid in pids.read_text().split())
                self.assertTrue(wait_until(lambda: not running(child) and not running(grandchild), TEST_HANG_GUARD_SECS))

    def test_a_stop_signal_during_cleanup_raises_after_the_cleanup_ends_and_handlers_are_restored(self) -> None:
        previous = signal.getsignal(signal.SIGTERM)
        cleanup_ended = False
        with self.assertRaises(style_sync.LintStopped) as stopped:
            with style_sync.stop_signals_raise(), style_sync.signal_mask(signal.SIG_BLOCK):
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(0.1)
                cleanup_ended = True
        self.assertTrue(cleanup_ended)
        self.assertEqual(stopped.exception.signum, signal.SIGTERM)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_a_stop_signal_right_after_the_dir_is_made_still_removes_it(self) -> None:
        make_dir = build_folders.create_build_folder

        def make_dir_then_signal(request: build_folders.BuildFolderRequest) -> Path:
            made = make_dir(request)
            os.kill(os.getpid(), signal.SIGTERM)
            return made

        output = io.StringIO()
        with mock.patch.object(build_folders, "create_build_folder", make_dir_then_signal), \
                mock.patch.object(style_sync, "VERIFY", self.clean_verify()), \
                redirect_stdout(output), redirect_stderr(output):
            code = style_sync.main(["lint", "--repo", str(self.repo), "--checkout", str(self.repo), "--tip", self.tip])
        self.assertEqual(code, 128 + signal.SIGTERM, output.getvalue())
        self.assertEqual(list(self.lint_temp.iterdir()), [])

    def test_standing_checkout_has_no_registration_in_the_source(self) -> None:
        original_worktrees = self.git("worktree", "list", "--porcelain")
        observed = self.root / "unregistered-checkout"
        code, output = self.run_lint(f"pwd -P > {observed}\n[[ -d .git ]] || exit 2\n")
        self.assertEqual(code, 0, output)
        checkout = Path(observed.read_text().strip())
        self.assertFalse(checkout.exists())
        self.assertTrue(self.standing_target().is_dir())
        self.assertFalse((self.standing_target().parent / ".git").exists())
        self.assertEqual(self.git("worktree", "list", "--porcelain"), original_worktrees)
        self.assertEqual(list(self.lint_temp.iterdir()), [])

    def test_sweep_removes_only_dead_runs_dirs_with_their_registrations(self) -> None:
        dead = ended_pid()
        live = self.stale_dir("live", os.getpid(), registered=True)
        verifier_running = self.stale_dir("verifier-running", dead, registered=True,
                                          verifier_group=self.running_group())
        unowned = self.stale_dir("unowned", None, registered=True)
        unreadable = self.stale_dir("unreadable", None, registered=False)
        _ = (unreadable / style_sync.OWNER_FILE).write_text("not a record\n")
        kept_worktrees = self.git("worktree", "list", "--porcelain")
        registered = self.stale_dir("registered", dead, registered=True, verifier_group=ended_pid())
        checkout_gone = self.stale_dir("checkout-gone", dead, registered=False)
        # A killed `worktree add`: its registration is still locked and the checkout has no `.git` yet.
        killed_add = self.stale_dir("killed-add", dead, registered=True)
        _ = self.git("worktree", "lock", "--reason", "initializing", str(killed_add / "checkout"))
        (killed_add / "checkout/.git").unlink()
        output = io.StringIO()
        with redirect_stdout(output):
            style_sync.sweep_stale_lint_dirs()
        self.assertEqual(output.getvalue().splitlines(), [
            f"style_sync: removed stale lint checkout {directory} (owner {dead} not running)"
            for directory in (checkout_gone, killed_add, registered)
        ])
        self.assertEqual(sorted(self.lint_temp.iterdir()), [live, unowned, unreadable, verifier_running])
        self.assertEqual(self.git("worktree", "list", "--porcelain"), kept_worktrees)

    def test_a_sweep_failure_prints_one_line_and_the_lint_run_continues(self) -> None:
        stuck = self.stale_dir("stuck", ended_pid(), registered=False)
        (stuck / "locked").mkdir()
        _ = (stuck / "locked/file").write_text("kept\n")
        (stuck / "locked").chmod(0o500)
        self.addCleanup((stuck / "locked").chmod, 0o700)
        code, output = self.run_lint()
        self.assertEqual(code, 0, output)
        sweep_lines = [line for line in output.splitlines() if line.startswith("style_sync: sweep ")]
        self.assertEqual(len(sweep_lines), 1, output)
        self.assertTrue(sweep_lines[0].startswith(f"style_sync: sweep {stuck}: [Errno 13] Permission denied"))
        self.assertTrue(stuck.exists())

    def test_a_git_launch_failure_in_the_sweep_keeps_the_record_and_the_lint_run_continues(self) -> None:
        stale = self.stale_dir("git-missing", None, registered=False)
        stale_repo = self.root / "stale-repo/.git"
        style_sync.OwnerRecord(ended_pid(), stale_repo).write(stale)
        real_git = style_sync.git

        def git_missing_for_stale_repo(repo: Path, *arguments: str) -> str:
            if repo == stale_repo:
                raise FileNotFoundError(2, "No such file or directory", "git")
            return real_git(repo, *arguments)

        with mock.patch.object(style_sync, "git", git_missing_for_stale_repo):
            code, output = self.run_lint()
        self.assertEqual(code, 0, output)
        self.assertIn(f"style_sync: kept {stale} for the next lint run; worktree registration: "
                      + "[Errno 2] No such file or directory: 'git'", output.splitlines())
        self.assertEqual(style_sync.OwnerRecord.read(stale).repo, stale_repo)

    def test_lint_folder_cleanup_needs_no_git_registration_read(self) -> None:
        original_worktrees = self.git("worktree", "list", "--porcelain")
        real_git = style_sync.git

        def git_launch_fails_in_cleanup(repo: Path, *arguments: str) -> str:
            if arguments[:2] == ("worktree", "list"):
                raise BlockingIOError(errno.EAGAIN, os.strerror(errno.EAGAIN), "git")
            return real_git(repo, *arguments)

        with mock.patch.object(style_sync, "git", git_launch_fails_in_cleanup):
            code, output = self.run_lint()
        self.assertEqual(code, 0, output)
        self.assertEqual(list(self.lint_temp.iterdir()), [])
        self.assertEqual(self.git("worktree", "list", "--porcelain"), original_worktrees)
        self.assertFalse((self.standing_target().parent / ".git").exists())
        self.assertTrue(self.standing_target().is_dir())

    def test_lint_folder_is_removed_when_verify_raises(self) -> None:
        with mock.patch.object(style_sync, "verify_lint", side_effect=RuntimeError("verify failed")):
            with self.assertRaisesRegex(RuntimeError, "verify failed"):
                _ = style_sync.lint(self.repo, self.repo, self.tip)
        self.assertEqual(list(self.lint_temp.iterdir()), [])

    def test_a_stop_signal_right_after_verify_starts_stops_it_and_cleans_up(self) -> None:
        real_popen = cast(Callable[..., subprocess.Popen[str]], subprocess.Popen)
        verifiers: list[int] = []
        started = self.root / "verify-started"

        def popen_then_signal(*arguments: object, **options: object) -> subprocess.Popen[str]:
            child = real_popen(*arguments, **options)
            if cast(list[str], arguments[0])[:1] == ["sh"]:
                verifiers.append(child.pid)
                self.addCleanup(kill_group, child.pid)
                # verify.sh is running, so only a stop removes its group.
                self.assertTrue(wait_until(started.exists, TEST_HANG_GUARD_SECS))
                os.kill(os.getpid(), signal.SIGTERM)
            return child

        output = io.StringIO()
        with mock.patch.object(subprocess, "Popen", side_effect=popen_then_signal), \
                mock.patch.object(style_sync, "VERIFY", self.clean_verify(f"sleep 300 &\ntouch {started}\nwait\n")), \
                redirect_stdout(output), redirect_stderr(output):
            code = style_sync.main(["lint", "--repo", str(self.repo), "--checkout", str(self.repo), "--tip", self.tip])
        self.assertEqual(code, 128 + signal.SIGTERM, output.getvalue())
        self.assertEqual(len(verifiers), 1)
        self.assertTrue(wait_until(lambda: not group_running(verifiers[0]), TEST_HANG_GUARD_SECS))
        self.assertEqual(list(self.lint_temp.iterdir()), [])

    def test_after_a_sigkill_of_the_owner_the_sweep_keeps_the_dir_until_its_verifier_ends(self) -> None:
        driver = self.root / "lint_driver.py"
        _ = driver.write_text(LINT_DRIVER)
        original_worktrees = self.git("worktree", "list", "--porcelain")
        verifier = self.root / "verifier"
        verify = self.clean_verify(f'sleep 300 &\necho "$$" > {verifier}.part && mv {verifier}.part {verifier}\nwait\n')
        process = subprocess.Popen(
            [str(Path(style_sync.__file__).resolve().parents[1] / "lib" / "py"), str(driver), str(verify), str(self.lint_temp),
             "lint", "--repo", str(self.repo), "--checkout", str(self.repo), "--tip", self.tip],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            self.assertTrue(wait_until(lambda: verifier.exists() or process.poll() is not None, TEST_HANG_GUARD_SECS))
        finally:
            process.kill()
            _ = process.wait()
        group = int(verifier.read_text())
        self.addCleanup(kill_group, group)
        [directory] = list(self.lint_temp.iterdir())
        with redirect_stdout(io.StringIO()):
            style_sync.sweep_stale_lint_dirs()
        self.assertTrue((directory / "checkout").exists())
        kill_group(group)
        self.assertTrue(wait_until(lambda: not group_running(group), TEST_HANG_GUARD_SECS))
        with redirect_stdout(io.StringIO()):
            style_sync.sweep_stale_lint_dirs()
        self.assertEqual(list(self.lint_temp.iterdir()), [])
        self.assertEqual(self.git("worktree", "list", "--porcelain"), original_worktrees)

    def test_a_verifier_wrapper_whose_owner_is_gone_exits_before_verify_starts(self) -> None:
        directory = self.root / "wrapper"
        directory.mkdir()
        started = self.root / "verify-started"
        with mock.patch.object(style_sync, "VERIFY", self.clean_verify(f"touch {started}\n")):
            ended = subprocess.run(style_sync.verifier_command(directory, ended_pid()), capture_output=True, check=False)
            self.assertNotEqual(ended.returncode, 0)
            self.assertTrue((directory / style_sync.VERIFIER_FILE).exists())
            self.assertFalse(started.exists())
            running_owner = subprocess.run(style_sync.verifier_command(directory, os.getpid()),
                                           capture_output=True, check=False)
        self.assertEqual(running_owner.returncode, 0)
        self.assertTrue(started.exists())

    def test_the_sweep_never_follows_a_checkout_link_or_a_lint_dir_link(self) -> None:
        dead = ended_pid()
        outside = self.root / "outside-checkout"
        outside.mkdir()
        _ = (outside / "kept").write_text("kept\n")
        linked = self.stale_dir("linked-checkout", dead, registered=False)
        (linked / "checkout").symlink_to(outside)
        planted_target = self.root / "outside-lint-dir"
        planted_target.mkdir()
        common_dir = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir"))
        style_sync.OwnerRecord(dead, common_dir).write(planted_target)
        _ = (planted_target / "kept").write_text("kept\n")
        planted = self.lint_temp / f"{style_sync.lint_prefix()}planted"
        planted.symlink_to(planted_target)
        with redirect_stdout(io.StringIO()):
            style_sync.sweep_stale_lint_dirs()
        self.assertEqual((outside / "kept").read_text(), "kept\n")
        self.assertEqual((planted_target / "kept").read_text(), "kept\n")
        self.assertTrue((planted_target / style_sync.OWNER_FILE).exists())
        self.assertEqual(list(self.lint_temp.iterdir()), [planted])

    def test_verify_starts_with_the_stop_signals_unblocked(self) -> None:
        mask = self.root / "sigblk"
        # Reading /proc/$$ from a pipeline samples Bash's temporary fork mask, not the
        # mask inherited by a lint tool. Query the actual tool child's own mask instead.
        probe = ("import signal; from pathlib import Path; "
                 "blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set()); "
                 f"Path({str(mask)!r}).write_text(str(sum(1 << (s - 1) for s in blocked)))")
        code, output = self.run_lint(shlex.join([str(Path(__file__).resolve().parents[1] / "lib/py"), "-c", probe]) + "\n")
        self.assertEqual(code, 0, output)
        held = sum(1 << (signum - 1) for signum in style_sync.HELD_SIGNALS)
        self.assertEqual(int(mask.read_text()) & held, 0)

    def handoff_checkpoint(self) -> str:
        _ = self.git("switch", "-qc", "style/main")
        _ = (self.repo / "src/lib.rs").write_text("fn corrected() {}\n")
        _ = self.git("commit", "-qam", "Style checkpoint")
        checkpoint = self.git("rev-parse", "HEAD")
        _ = self.git("switch", "-q", "main")
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.set_tip(self.tip, "style/main")
        _ = (self.root / "address.py").write_text(
            "import sys\nprint('parent' if sys.argv[1] == 'parent' else 'session:fake-parent')\n",
        )
        _ = (self.root / "send.py").write_text(
            "import json,sys\nfrom pathlib import Path\n"
            + f"with Path({str(self.root / 'notices.jsonl')!r}).open('a') as out:\n"
            + " out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            + "print('SENT: fixture')\n",
        )
        return checkpoint

    def handoff_sender(self, outcome: str, code: int) -> None:
        _ = (self.root / "send.py").write_text(
            "import json,sys\nfrom pathlib import Path\n"
            + f"with Path({str(self.root / 'notices.jsonl')!r}).open('a') as out:\n"
            + " out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            + f"print({outcome!r})\nraise SystemExit({code})\n",
        )

    def send_handoff(self, checkpoint: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = style_sync.main(["send-handoff", "--repo", str(self.repo), "--production", "sample-style",
                                    "--checkpoint", checkpoint])
        return code, output.getvalue()

    def handoff_rows(self) -> list[tuple[str, str, str, str, str]]:
        with style_ledger.open_ledger(self.repo) as ledger, closing(sqlite3.connect(ledger.database)) as connection:
            return cast(list[tuple[str, str, str, str, str]], connection.execute(
                "SELECT checkpoint,state,sent_to,created_at,updated_at FROM handoffs ORDER BY created_at,checkpoint",
            ).fetchall())

    def handoff_notices(self) -> list[list[str]]:
        path = self.root / "notices.jsonl"
        return [cast(list[str], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def resume_handoffs(self) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = style_sync.main(["resume", "--repo", str(self.repo), "--production", "sample-style",
                                    "--branch-point", "main", "--style-branch", "style/main"])
        return code, output.getvalue()

    def test_resume_reconciles_before_notice_and_sends_pending_handoff(self) -> None:
        checkpoint = self.handoff_checkpoint()
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.handoff_add(checkpoint)
            # This second row gives reconcile a visible transition before the notice.
            ledger.handoff_add(self.tip)
        output = io.StringIO()
        real_run = subprocess.run
        def observe_send(arguments: list[str], **options: object) -> object:
            if str(self.root / "send.py") in arguments:
                self.assertIn(f"hand-off {self.tip}: pending -> merged", output.getvalue())
                with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
                    self.assertEqual(connection.execute("SELECT branch_point FROM reconciliation").fetchone(), ("main",))
            self.assertTrue(options.get("capture_output"))
            return cast(object, mock.DEFAULT)
        with mock.patch("scripts.style.style_sync.subprocess.run", wraps=real_run, side_effect=observe_send), redirect_stdout(output):
            code = style_sync.main(["resume", "--repo", str(self.repo), "--production", "sample-style",
                                    "--branch-point", "main", "--style-branch", "style/main"])
        self.assertEqual(code, 0)
        rows = {row[0]: row for row in self.handoff_rows()}
        self.assertEqual(rows[checkpoint][1:3], ("sent", "parent"))
        notices = self.handoff_notices()
        self.assertEqual(len(notices), 1)
        self.assertIn(f"From the showrunner of sample-style: hand-off {checkpoint}", notices[0])
        self.assertIn("session:fake-parent", notices[0])

    def test_resume_replays_same_parent_without_touching_the_row(self) -> None:
        checkpoint = self.handoff_checkpoint()
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.handoff_add(checkpoint)
            ledger.handoff_send(checkpoint, "parent")
        before = self.handoff_rows()
        self.assertEqual(self.resume_handoffs()[0], 0)
        self.assertEqual(self.handoff_rows(), before)
        self.assertEqual(len(self.handoff_notices()), 1)

    def test_resume_reroutes_departed_parent_and_keeps_pending_age(self) -> None:
        checkpoint = self.handoff_checkpoint()
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.handoff_add(checkpoint)
            ledger.handoff_send(checkpoint, "departed")
        before = self.handoff_rows()[0]
        self.assertEqual(self.resume_handoffs()[0], 0)
        after = self.handoff_rows()[0]
        self.assertEqual(after[:4], (checkpoint, "sent", "parent", before[3]))
        self.assertNotEqual(after[4], before[4])
        self.assertEqual(len(self.handoff_notices()), 1)

    def test_resume_as_root_leaves_pending_until_branch_point_contains_it(self) -> None:
        checkpoint = self.handoff_checkpoint()
        _ = (self.root / "address.py").write_text("print('')\n")
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.handoff_add(checkpoint)
        code, output = self.resume_handoffs()
        self.assertEqual(code, 0)
        self.assertIn(f"root: merge {checkpoint[:7]} with --root-self", output)
        self.assertEqual(self.handoff_rows()[0][1], "pending")
        self.assertEqual(self.handoff_notices(), [])
        _ = self.git("merge", "--no-ff", "-qm", "Integrate root handoff", "style/main")
        code, output = self.resume_handoffs()
        self.assertEqual(code, 0)
        self.assertIn(f"hand-off {checkpoint}: pending -> merged", output)
        self.assertEqual(self.handoff_rows()[0][1], "merged")
        self.assertEqual(self.handoff_notices(), [])

    def test_failed_resume_notice_leaves_pending_and_sent_destinations_unchanged(self) -> None:
        checkpoint = self.handoff_checkpoint()
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.handoff_add(checkpoint)
        _ = (self.root / "send.py").write_text("print('FAILED: unavailable')\nraise SystemExit(3)\n")
        for destination in ("", "departed"):
            if destination:
                with style_ledger.open_ledger(self.repo) as ledger:
                    ledger.handoff_send(checkpoint, destination)
            before = self.handoff_rows()
            code, output = self.resume_handoffs()
            self.assertEqual(code, 1)
            self.assertEqual(len(output.splitlines()), 1)
            self.assertIn("FAILED: unavailable", output)
            self.assertEqual(self.handoff_rows(), before)

    def test_acknowledgment_requires_resume_to_establish_branch_point(self) -> None:
        checkpoint = self.handoff_checkpoint()
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.handoff_add(checkpoint)
            ledger.handoff_send(checkpoint, "parent")
        _ = self.git("merge", "--no-ff", "-qm", "Integrate handoff", "style/main")
        merge = self.git("rev-parse", "HEAD")
        def acknowledge() -> subprocess.CompletedProcess[str]:
            return subprocess.run([str(style_sync.PYTHON), str(style_sync.LEDGER), "handoff", "merged",
                                   "--repo", str(self.repo), "--checkpoint", checkpoint, "--merge-commit", merge],
                                  capture_output=True, text=True, check=False)
        refused = acknowledge()
        self.assertEqual(refused.returncode, 2)
        self.assertIn("reconcile", refused.stderr)
        self.assertEqual(self.resume_handoffs()[0], 0)
        accepted = acknowledge()
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(self.handoff_rows()[0][1], "merged")

    def test_send_handoff_sends_one_notice_and_records_the_current_parent(self) -> None:
        checkpoint = self.handoff_checkpoint()
        output = io.StringIO()
        with redirect_stdout(output):
            code = style_sync.main(["send-handoff", "--repo", str(self.repo), "--production", "sample-style",
                                    "--checkpoint", checkpoint])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(self.handoff_rows()[0][:3], (checkpoint, "sent", "parent"))
        notices = self.handoff_notices()
        self.assertEqual(len(notices), 1)
        self.assertIn("session:fake-parent", notices[0])
        self.assertIn(f"From the showrunner of sample-style: hand-off {checkpoint}", notices[0])

    def test_send_handoff_as_root_sends_nothing_and_leaves_the_row_pending(self) -> None:
        checkpoint = self.handoff_checkpoint()
        _ = (self.root / "address.py").write_text("print('')\n")
        output = io.StringIO()
        with redirect_stdout(output):
            code = style_sync.main(["send-handoff", "--repo", str(self.repo), "--production", "sample-style",
                                    "--checkpoint", checkpoint])
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue().strip(), f"root: merge {checkpoint[:7]} with --root-self")
        self.assertEqual(self.handoff_rows()[0][:3], (checkpoint, "pending", ""))
        self.assertEqual(self.handoff_notices(), [])

    def test_queued_send_handoff_is_sent_to_the_parent(self) -> None:
        """send.py queued a hand-off and the row stayed pending, so its merge was refused (cargo-liner, 2026-10-27)."""
        checkpoint = self.handoff_checkpoint()
        self.handoff_sender("checking recipient\nQUEUED: relay timed out", 1)
        self.assertEqual(self.send_handoff(checkpoint), (0, f"hand-off {checkpoint[:7]}: queued for parent\n"))
        self.assertEqual(self.handoff_rows()[0][:3], (checkpoint, "sent", "parent"))
        self.assertEqual(len(self.handoff_notices()), 1)

    def test_kept_handoff_notice_records_acceptance_and_reports_queued(self) -> None:
        checkpoint = self.handoff_checkpoint()
        self.handoff_sender("KEPT: until silence ends", 0)
        self.assertEqual(self.send_handoff(checkpoint), (0, f"hand-off {checkpoint[:7]}: queued for parent\n"))
        self.assertEqual(self.handoff_rows()[0][:3], (checkpoint, "sent", "parent"))
        self.assertEqual(len(self.handoff_notices()), 1)

    def test_handoff_without_a_confirmed_send_result_stays_pending(self) -> None:
        checkpoint = self.handoff_checkpoint()
        for output, code in (("", 0), ("unrelated output", 0), ("QUEUED: offline", 0),
                             ("SENT: apparent delivery", 1), ("FAILED: unavailable\nQUEUED: quoted result", 1),
                             ("REFUSED: action required", 2), ("HELD: review", 4), ("PENDING: active", 5)):
            with self.subTest(output=output, code=code):
                self.handoff_sender(output, code)
                result, diagnostic = self.send_handoff(checkpoint)
                self.assertEqual(result, 1)
                self.assertIn("style_sync: hand-off: send.py:", diagnostic)
                self.assertEqual(self.handoff_rows()[0][:3], (checkpoint, "pending", ""))

    def test_skipped_and_replaced_handoff_notices_record_parent_acceptance(self) -> None:
        checkpoint = self.handoff_checkpoint()
        for output in ("SKIPPED: acknowledged", "REPLACED: newer notice"):
            with self.subTest(output=output):
                self.handoff_sender(output, 0)
                self.assertEqual(self.send_handoff(checkpoint), (0, ""))
                self.assertEqual(self.handoff_rows()[0][:3], (checkpoint, "sent", "parent"))

    def test_undelivered_send_handoff_stays_pending(self) -> None:
        checkpoint = self.handoff_checkpoint()
        for outcome, code in (("FAILED: unavailable", 3), ("relay error", 1)):
            with self.subTest(outcome=outcome):
                self.handoff_sender(outcome, code)
                self.assertEqual(self.send_handoff(checkpoint), (1, f"style_sync: hand-off: send.py: stdout: {outcome}\n"))
                self.assertEqual(self.handoff_rows()[0][:3], (checkpoint, "pending", ""))

    def test_resume_records_a_queued_notice_as_sent(self) -> None:
        checkpoint = self.handoff_checkpoint()
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.handoff_add(checkpoint)
        self.handoff_sender("QUEUED: relay timed out", 1)
        code, output = self.resume_handoffs()
        self.assertEqual(code, 0, output)
        self.assertIn(f"hand-off {checkpoint[:7]}: queued for parent\n", output)
        self.assertEqual(self.handoff_rows()[0][:3], (checkpoint, "sent", "parent"))

    def test_send_handoff_interrupted_after_notice_is_replayed_on_resume(self) -> None:
        checkpoint = self.handoff_checkpoint()
        with mock.patch.object(style_ledger.Ledger, "handoff_send", side_effect=RuntimeError("interruption")):
            with self.assertRaisesRegex(RuntimeError, "interruption"):
                _ = style_sync.main(["send-handoff", "--repo", str(self.repo), "--production", "sample-style",
                                     "--checkpoint", checkpoint])
        self.assertEqual(len(self.handoff_notices()), 1)
        self.assertEqual(self.handoff_rows()[0][1], "pending")
        self.assertEqual(self.resume_handoffs()[0], 0)
        self.assertEqual(len(self.handoff_notices()), 2)
        self.assertEqual(self.handoff_notices()[0], self.handoff_notices()[1])
        self.assertEqual(self.handoff_rows()[0][1:3], ("sent", "parent"))

    def test_resume_sends_multiple_pending_handoffs_oldest_first(self) -> None:
        first = self.handoff_checkpoint()
        _ = self.git("switch", "-q", "style/main")
        _ = (self.repo / "src/lib.rs").write_text("fn another_correction() {}\n")
        _ = self.git("commit", "-qam", "Next style checkpoint")
        second = self.git("rev-parse", "HEAD")
        _ = self.git("switch", "-q", "main")
        with style_ledger.open_ledger(self.repo) as ledger:
            with mock.patch.object(style_ledger, "_now", return_value="2026-01-02T00:00:00+00:00"):
                ledger.handoff_add(second)
            with mock.patch.object(style_ledger, "_now", return_value="2026-01-01T00:00:00+00:00"):
                ledger.handoff_add(first)
        self.assertEqual(self.resume_handoffs()[0], 0)
        texts = [notice[notice.index("--text") + 1] for notice in self.handoff_notices()]
        self.assertEqual(texts, [f"From the showrunner of sample-style: hand-off {checkpoint}" for checkpoint in (first, second)])
        self.assertEqual({row[1:3] for row in self.handoff_rows()}, {("sent", "parent")})


if __name__ == "__main__":
    _ = unittest.main()
