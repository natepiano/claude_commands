"""Sync notices are best effort; lint records describe the exact requested tree."""

from __future__ import annotations

import errno
import fcntl
import io
import importlib
import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from contextlib import closing, redirect_stderr, redirect_stdout, suppress
from pathlib import Path
from typing import TypedDict, cast, final, override
from unittest import mock

import style_sync
import scratch
import style_ledger
import style_rules
import showrunners
from scripts.direct import test_verify_lint_scope as verify_fixture


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

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
LINT_DRIVER = f'''import sys
from pathlib import Path
sys.path.insert(0, {str(Path(style_sync.__file__).resolve().parent)!r})
import style_sync
style_sync.VERIFY = Path(sys.argv[1])
style_sync.LINT_TEMP_PARENT = Path(sys.argv[2])
style_sync.CHILD_STOP_SECONDS = 0.5
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
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    return True


def kill_group(group: int) -> None:
    with suppress(ProcessLookupError):
        os.killpg(group, signal.SIGKILL)


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

    @override
    def setUp(self) -> None:
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
            "STYLE_HISTORY_NATE_STYLE_DIR": str(rule_root),
            "STYLE_LEDGER_STATE_DIR": str(self.root / "state"),
            "SCRATCH_HOLDS": str(self.root / "scratch-holds.json"),
            "SCRATCH_ROOT": str(self.root / "scratch"),
            "SCRATCH_COPIES_ROOT": str(self.root / "copies"),
            "SCRATCH_VISIBLE_ROOT": str(self.root / "repro"),
        })))
        _ = self.enterContext(mock.patch.object(style_rules, "NATE_STYLE_DIR", rule_root))
        sender = self.root / "send.py"
        _ = sender.write_text(
            "import json,sys\nfrom pathlib import Path\n"
            + f"Path({str(self.root / 'sent.json')!r}).write_text(json.dumps({{'arguments':sys.argv[1:]}}))\n"
            + "raise SystemExit(0)\n",
        )
        address = self.root / "address.py"
        _ = address.write_text("print('session:fake-style')\n")
        _ = self.enterContext(mock.patch.object(style_sync, "SEND", sender))
        _ = self.enterContext(mock.patch.object(style_sync, "SHOWRUNNERS", address))
        _ = self.enterContext(mock.patch.object(showrunners, "production_of", return_value=None))
        self.lint_temp = self.root / "tmp"
        self.lint_temp.mkdir()
        _ = self.enterContext(mock.patch.object(style_sync, "LINT_TEMP_PARENT", self.lint_temp))

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *arguments],
                              capture_output=True, text=True, check=True).stdout.strip()

    def production(self, branch: str = "main") -> None:
        self.doc.parent.mkdir(parents=True)
        _ = self.doc.write_text(f"# Style\n- **Branch point:** {branch}\n")

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
            ledger.ticket_add("modules-unit-1", "modules-unit", "read", "modules", ".",
                              ["src/lib.rs"], [rule])
            ledger.ticket_assign("modules-unit-1", "main")
            blob = ledger.pinned_rule_texts("modules-unit-1")[0].blob
            ledger.record(rule, "src/lib.rs", checkpoint, blob, "clean", ticket="modules-unit-1", worker="test")
            ledger.ticket_checkpoint("modules-unit-1", checkpoint)

    def waiting_units(self, *units: str) -> None:
        with style_ledger.open_ledger(self.repo) as ledger:
            for unit in units:
                ledger.wait_for_sync(unit)

    def remaining_waiters(self) -> list[str]:
        with style_ledger.open_ledger(self.repo) as ledger, closing(sqlite3.connect(ledger.database)) as connection:
            rows = cast(list[tuple[str]], connection.execute("SELECT unit FROM sync_waits ORDER BY unit").fetchall())
        return [unit for (unit,) in rows]

    def wake_roster(self, units: tuple[str, ...] = ("names-unit", "types-unit")) -> None:
        entries: list[dict[str, str]] = [
            {"role": "unit director", "production": "sample-style", "unit": unit,
             "name": unit.removesuffix("-unit"), "address": "session:fake-" + unit}
            for unit in units
        ]
        entries.extend([
            {"role": "unit director", "production": "another-style", "unit": "names-unit",
             "name": "names", "address": "session:wrong-production"},
            {"role": "worker", "production": "sample-style", "unit": "names-unit",
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
            sys, "argv", ["style_ledger.py", "ticket", "merge", "--repo", str(self.repo), "--id", "modules-unit-1"],
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

    def test_ticket_merge_wakes_two_waiters_once_and_clears_their_rows(self) -> None:
        self.production()
        self.checkpoint_for_wake()
        self.waiting_units("names-unit", "types-unit")
        self.wake_sender()
        self.assertEqual(self.merge_for_wake(), (0, "", ""))
        self.assertEqual(self.recorded_wakes(), [
            ["--to", "session:fake-" + unit, "--from", "sample-style", "--text",
             "From the showrunner of sample-style: ticket modules-unit-1 merged; refill.", "--timeout", "10"]
            for unit in ("names-unit", "types-unit")
        ])
        self.assertEqual(self.remaining_waiters(), [])
        self.assertEqual((self.root / "roster-args").read_text(), "--json")
        self.assertEqual(self.merge_for_wake(), (0, "", ""))
        self.assertEqual(len(self.recorded_wakes()), 2)

    def test_a_send_is_given_send_pys_whole_run_before_it_counts_as_timed_out(self) -> None:
        """A wake delivered late was stopped at 15 s and reported "timed out" (cargo-liner-style, 2026-10-17)."""
        sys.path.insert(0, str(style_sync.SCRIPTS / "message"))
        kill_grace = cast(int, getattr(importlib.import_module("send"), "KILL_GRACE"))
        self.assertGreater(style_sync.SEND_RUN_SECONDS, style_sync.SEND_SECONDS + kill_grace)

    def test_an_older_in_flight_wake_cannot_erase_the_units_new_wait(self) -> None:
        self.production()
        self.waiting_units("names-unit")
        self.wake_sender()
        with style_ledger.open_ledger(self.repo) as ledger:
            first = ledger.sync_waits()[0]
        interleaved = False

        def refill_during_send(arguments: list[str], **options: object) -> object:
            nonlocal interleaved
            if (len(arguments) > 1 and arguments[1] == str(style_sync.SEND)
                    and options.get("timeout") == style_sync.SEND_RUN_SECONDS and not interleaved):
                interleaved = True
                style_sync.wake_waiters(self.repo, "modules-unit-2")
                with style_ledger.open_ledger(self.repo) as ledger:
                    self.assertEqual(ledger.sync_waits(), ())
                    # Equal timestamps ensure that only a wait identity protects the new row.
                    with mock.patch.object(style_ledger, "_now", return_value=first.since):
                        ledger.wait_for_sync("names-unit")
            return cast(object, mock.DEFAULT)

        with mock.patch.object(subprocess, "run", wraps=subprocess.run, side_effect=refill_during_send):
            style_sync.wake_waiters(self.repo, "modules-unit-1")
        self.assertTrue(interleaved)
        self.assertEqual(self.remaining_waiters(), ["names-unit"])
        with style_ledger.open_ledger(self.repo) as ledger:
            second = ledger.sync_waits()[0]
        self.assertEqual((second.unit, second.since), (first.unit, first.since))
        self.assertNotEqual(second.token, first.token)
        self.assertEqual([call[5] for call in self.recorded_wakes()], [
            "From the showrunner of sample-style: ticket modules-unit-2 merged; refill.",
            "From the showrunner of sample-style: ticket modules-unit-1 merged; refill.",
        ])

    def test_failed_wake_keeps_only_that_waiter_and_an_already_merged_ticket_retries_it(self) -> None:
        self.production()
        self.checkpoint_for_wake()
        self.waiting_units("names-unit", "types-unit")
        self.wake_sender("names-unit")
        self.assertEqual(self.merge_for_wake(), (0, "style_ledger: wake names-unit: FAILED: unavailable\n", ""))
        self.assertEqual(self.remaining_waiters(), ["names-unit"])
        with style_ledger.open_ledger(self.repo) as ledger, closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(connection.execute("SELECT state FROM tickets WHERE id='modules-unit-1'").fetchone(), ("merged",))
        self.wake_sender()
        self.assertEqual(self.merge_for_wake(), (0, "", ""))
        self.assertEqual(self.remaining_waiters(), [])
        self.assertEqual([arguments[1] for arguments in self.recorded_wakes()], ["session:fake-names-unit", "session:fake-types-unit", "session:fake-names-unit"])

    def test_merge_without_waiters_or_a_standing_production_sends_nothing(self) -> None:
        self.checkpoint_for_wake()
        self.waiting_units()
        self.production()
        self.wake_sender()
        self.assertEqual(self.merge_for_wake(), (0, "", ""))
        self.doc.unlink()
        self.waiting_units("names-unit")
        self.assertEqual(self.merge_for_wake(), (0, "", ""))
        self.assertEqual(self.recorded_wakes(), [])
        self.assertEqual(self.remaining_waiters(), ["names-unit"])

    def test_director_without_an_address_does_not_block_other_waiters(self) -> None:
        self.production()
        self.checkpoint_for_wake()
        self.waiting_units("names-unit", "types-unit")
        self.wake_sender()
        roster = self.root / "roster.py"
        _ = roster.write_text(roster.read_text().replace("session:fake-names-unit", ""))
        self.assertEqual(self.merge_for_wake(), (0, "style_ledger: wake names-unit: no live director\n", ""))
        self.assertEqual(self.recorded_wakes(), [[
            "--to", "session:fake-types-unit", "--from", "sample-style", "--text",
            "From the showrunner of sample-style: ticket modules-unit-1 merged; refill.", "--timeout", "10",
        ]])
        self.assertEqual(self.remaining_waiters(), ["names-unit"])

    def test_merge_without_a_matching_director_keeps_the_wait_and_reports_why(self) -> None:
        self.production()
        self.checkpoint_for_wake()
        self.waiting_units("names-unit")
        self.wake_sender()
        self.wake_roster(())
        self.assertEqual(self.merge_for_wake(), (0, "style_ledger: wake names-unit: no live director\n", ""))
        self.assertEqual(self.recorded_wakes(), [])
        self.assertEqual(self.remaining_waiters(), ["names-unit"])

    def test_ambiguous_or_unreadable_roster_does_not_send_or_forget_the_wait(self) -> None:
        self.production()
        self.checkpoint_for_wake()
        self.waiting_units("names-unit")
        self.wake_sender()
        self.wake_roster(("names-unit", "names-unit"))
        self.assertEqual(self.merge_for_wake(), (0, "style_ledger: wake names-unit: 2 directors\n", ""))
        self.assertEqual(self.recorded_wakes(), [])
        self.assertEqual(self.remaining_waiters(), ["names-unit"])
        _ = (self.root / "roster.py").write_text("print('roster unavailable'); raise SystemExit(3)\n")
        self.assertEqual(self.merge_for_wake(), (0, "style_ledger: wake names-unit: roster: roster.py exited 3\n", ""))
        self.assertEqual(self.recorded_wakes(), [])
        self.assertEqual(self.remaining_waiters(), ["names-unit"])

    def test_refused_checkpoint_merge_does_not_wake_or_clear_waiters(self) -> None:
        self.production()
        self.checkpoint_for_wake(on_style_branch=False)
        self.waiting_units("names-unit")
        self.wake_sender()
        code, output, errors = self.merge_for_wake()
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("is not on refs/heads/style/main", errors)
        self.assertEqual(self.recorded_wakes(), [])
        self.assertEqual(self.remaining_waiters(), ["names-unit"])

    def test_no_style_production_and_another_branch_point_are_silent(self) -> None:
        self.assertEqual(self.notice(), (0, ""))
        self.production("init/catalyst")
        self.assertEqual(self.notice(), (0, ""))
        self.assertFalse((self.root / "sent.json").exists())

    def test_found_production_sends_the_keyed_sync_message_to_its_current_address(self) -> None:
        self.production()
        with mock.patch("style_sync.subprocess.run", wraps=subprocess.run) as calls:
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
        self.assertEqual(output, "style_sync: notify: FAILED: unavailable\n")

    def test_notify_lookup_refusal_prints_one_prefix_and_keeps_the_merge_successful(self) -> None:
        with mock.patch.object(style_sync, "standing_style_production",
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
        result = fixture.verify("lint", "--workspace", "--no-cache")
        self.assertEqual(fixture.calls("+nightly"), [
            ["fmt", "--version"], ["fmt", "--all"],
            ["fmt", "--version"], ["fmt", "--all"],
        ])
        self.assertEqual(fixture.calls("clippy"), [
            ["--fix", "--allow-dirty", "--allow-staged", "--allow-no-vcs",
             "--workspace", "--all-targets", "--all-features", "--keep-going", "--", "-D", "warnings"],
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
        result = fixture.invoke_verify("lint", "--workspace", "--check", "--no-cache", cwd=fixture.root)
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
            '#!/bin/bash\n[[ "$*" == "lint --workspace --check --no-cache" ]] || exit 2\n'
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
        _ = verify.write_text(f"#!/bin/bash\ncp ../owner.json {self.root / 'owner-seen'}\n"
                              + f"cp ../{style_sync.VERIFIER_FILE} {self.root / 'verifier-seen'}\n"
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
        _ = verify.write_text(f"#!/bin/bash\n{before}cat <<'OUTPUT'\n" + CLEAN_LINT + "OUTPUT\n")
        return verify

    def run_lint(self, before: str = "") -> tuple[int, str]:
        output = io.StringIO()
        with mock.patch.object(style_sync, "VERIFY", self.clean_verify(before)), redirect_stdout(output), \
                redirect_stderr(output):
            code = style_sync.main(["lint", "--repo", str(self.repo), "--checkout", str(self.repo), "--tip", self.tip])
        return code, output.getvalue()

    def stale_dir(self, name: str, owner: int | None, *, registered: bool, verifier_group: int = 0) -> Path:
        directory = self.lint_temp / f"{style_sync.LINT_PREFIX}{name}"
        directory.mkdir()
        if owner is not None:
            common_dir = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir"))
            style_sync.OwnerRecord(owner, common_dir).write(directory)
        if verifier_group:
            _ = (directory / style_sync.VERIFIER_FILE).write_text(str(verifier_group))
        if registered:
            _ = self.git("worktree", "add", "--detach", str(directory / "checkout"), self.tip)
        return directory

    def test_shared_target_survives_task_end_and_reuses_artifacts(self) -> None:
        common = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir"))
        target = style_sync.lint_target(common)
        code, output = self.run_lint('printf previous > target/artifact\n')
        self.assertEqual(code, 0, output)
        self.assertEqual(scratch.end("run:sample-style"), [])
        self.assertTrue(target.is_dir())
        self.assertEqual((target / "artifact").read_text(), "previous")
        code, output = self.run_lint('[[ $(cat target/artifact) == previous ]] || exit 2\n')
        self.assertEqual(code, 0, output)
        self.assertEqual(list(self.lint_temp.iterdir()), [])

    def test_lint_links_a_warm_target_and_cleanup_keeps_it_until_the_production_ends(self) -> None:
        target = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir")) / scratch.STYLE_TARGET_NAME
        code, output = self.run_lint('[[ -L target ]] || exit 2\nprintf warm > target/artifact\n')
        self.assertEqual(code, 0, output)
        self.assertEqual((target / "artifact").read_text(), "warm")
        self.assertEqual(list(self.lint_temp.iterdir()), [])
        self.assertNotIn(str(target), scratch.holds())
        # A later checkout uses the same artifacts. No Cargo environment variable is overridden.
        with mock.patch.dict(os.environ, {"CARGO_TARGET_DIR": "inherited-value"}):
            code, output = self.run_lint(
                '[[ -L target && $(cat target/artifact) == warm && $CARGO_TARGET_DIR == inherited-value ]] || exit 2\n'
            )
        self.assertEqual(code, 0, output)
        self.assertEqual(list(self.lint_temp.iterdir()), [])
        self.assertEqual(scratch.end("run:sample-style"), [])
        self.assertTrue(target.exists())
        self.assertEqual(scratch.holds(), {})

    def test_a_missing_or_swept_target_runs_cold(self) -> None:
        target = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir")) / scratch.STYLE_TARGET_NAME
        for state in ("missing", "swept"):
            with self.subTest(state=state):
                self.assertFalse(target.exists())
                code, output = self.run_lint(
                    '[[ -L target && -d target && ! -e target/artifact ]] || exit 2\nprintf fresh > target/artifact\n'
                )
                self.assertEqual(code, 0, output)
                self.assertEqual((target / "artifact").read_text(), "fresh")
                self.assertEqual(list(self.lint_temp.iterdir()), [])
                shutil.rmtree(target)

    def test_a_sync_waits_for_the_target_lock_before_creating_its_checkout(self) -> None:
        common_dir = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir"))
        target = style_sync.lint_target(common_dir)
        locked, attempted = threading.Event(), threading.Event()
        real_flock = fcntl.flock

        def first_sync() -> None:
            with (target / scratch.STYLE_LOCK_NAME).open("a") as lock:
                real_flock(lock, fcntl.LOCK_EX)
                locked.set()
                _ = attempted.wait(30)

        holder = threading.Thread(target=first_sync)
        holder.start()
        self.addCleanup(holder.join, TEST_HANG_GUARD_SECS)
        self.addCleanup(attempted.set)
        self.assertTrue(locked.wait(TEST_HANG_GUARD_SECS))

        def second_sync_lock(fd: int, operation: int) -> None:
            self.assertEqual(operation, fcntl.LOCK_EX)
            self.assertEqual(list(self.lint_temp.iterdir()), [])
            with self.assertRaises(BlockingIOError):
                real_flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            attempted.set()
            real_flock(fd, operation)
            # No thread remains when verify_lint uses preexec_fn.
            holder.join(TEST_HANG_GUARD_SECS)
            self.assertFalse(holder.is_alive())

        real_git = style_sync.git
        real_cleanup = style_sync.remove_lint_dir

        def assert_locked() -> None:
            with (target / scratch.STYLE_LOCK_NAME).open("a") as lock, self.assertRaises(BlockingIOError):
                real_flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def git(repo: Path, *arguments: str) -> str:
            if arguments[:2] == ("worktree", "add"):
                assert_locked()
            return real_git(repo, *arguments)

        def cleanup(repo: Path, directory: Path) -> bool:
            assert_locked()
            removed = real_cleanup(repo, directory)
            assert_locked()
            return removed

        with mock.patch.object(fcntl, "flock", side_effect=second_sync_lock), \
                mock.patch.object(style_sync, "git", side_effect=git), \
                mock.patch.object(style_sync, "remove_lint_dir", side_effect=cleanup):
            code, output = self.run_lint()
        self.assertEqual(code, 0, output)
        self.assertTrue(attempted.is_set())
        self.assertEqual(list(self.lint_temp.iterdir()), [])
        # Cleanup has completed and the next sync can acquire the same lock.
        with (target / scratch.STYLE_LOCK_NAME).open("a") as lock:
            real_flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_replacing_the_target_link_cannot_grant_successful_lint_coverage(self) -> None:
        code, output = self.run_lint('rm target\nmkdir target\n')
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
                    [sys.executable, str(driver), str(verify), str(self.lint_temp),
                     "lint", "--repo", str(self.repo), "--checkout", str(self.repo), "--tip", self.tip],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                try:
                    self.assertTrue(wait_until(lambda: pids.exists() or process.poll() is not None, 30))
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
                self.assertTrue(wait_until(lambda: not running(child) and not running(grandchild), 5))

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
        make_dir = tempfile.mkdtemp

        def make_dir_then_signal(prefix: str, dir: Path) -> str:
            made = make_dir(prefix=prefix, dir=dir)
            os.kill(os.getpid(), signal.SIGTERM)
            return made

        output = io.StringIO()
        with mock.patch.object(tempfile, "mkdtemp", make_dir_then_signal), \
                mock.patch.object(style_sync, "VERIFY", self.clean_verify()), \
                redirect_stdout(output), redirect_stderr(output):
            code = style_sync.main(["lint", "--repo", str(self.repo), "--checkout", str(self.repo), "--tip", self.tip])
        self.assertEqual(code, 128 + signal.SIGTERM, output.getvalue())
        self.assertEqual(list(self.lint_temp.iterdir()), [])

    def test_the_owner_record_exists_before_the_checkout_is_added(self) -> None:
        real_git = style_sync.git
        at_add: list[style_sync.OwnerRecord] = []

        def read_record_at_add(repo: Path, *arguments: str) -> str:
            if arguments[:2] == ("worktree", "add"):
                at_add.append(style_sync.OwnerRecord.read(Path(arguments[3]).parent))
            return real_git(repo, *arguments)

        with mock.patch.object(style_sync, "git", read_record_at_add):
            code, output = self.run_lint()
        self.assertEqual(code, 0, output)
        common_dir = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.assertEqual(at_add, [style_sync.OwnerRecord(os.getpid(), common_dir)])

    def test_a_locked_checkout_is_removed_with_its_registration(self) -> None:
        original_worktrees = self.git("worktree", "list", "--porcelain")
        code, output = self.run_lint('git worktree lock --reason initializing "$PWD"\n')
        self.assertEqual(code, 0, output)
        self.assertEqual(list(self.lint_temp.iterdir()), [])
        self.assertEqual(self.git("worktree", "list", "--porcelain"), original_worktrees)

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

    def test_a_git_failure_in_cleanup_keeps_the_record_and_the_next_sweep_removes_the_registration(self) -> None:
        original_worktrees = self.git("worktree", "list", "--porcelain")
        real_git = style_sync.git

        def git_launch_fails_in_cleanup(repo: Path, *arguments: str) -> str:
            if arguments[:2] == ("worktree", "list"):
                raise BlockingIOError(errno.EAGAIN, os.strerror(errno.EAGAIN), "git")
            return real_git(repo, *arguments)

        with mock.patch.object(style_sync, "git", git_launch_fails_in_cleanup):
            code, output = self.run_lint()
        self.assertEqual(code, 0, output)
        [kept] = list(self.lint_temp.iterdir())
        self.assertIn(f"style_sync: kept {kept} for the next lint run; worktree registration: "
                      + f"[Errno {errno.EAGAIN}] {os.strerror(errno.EAGAIN)}: 'git'", output.splitlines())
        self.assertFalse((kept / "checkout").exists())
        self.assertNotEqual(self.git("worktree", "list", "--porcelain"), original_worktrees)
        # The next lint run starts after this one's owner has exited.
        style_sync.OwnerRecord(ended_pid(), style_sync.OwnerRecord.read(kept).repo).write(kept)
        sweep_output = io.StringIO()
        with redirect_stdout(sweep_output):
            style_sync.sweep_stale_lint_dirs()
        self.assertIn(f"style_sync: removed stale lint checkout {kept}", sweep_output.getvalue())
        self.assertEqual(list(self.lint_temp.iterdir()), [])
        self.assertEqual(self.git("worktree", "list", "--porcelain"), original_worktrees)

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
                self.assertTrue(wait_until(started.exists, 30))
                os.kill(os.getpid(), signal.SIGTERM)
            return child

        output = io.StringIO()
        with mock.patch.object(subprocess, "Popen", side_effect=popen_then_signal), \
                mock.patch.object(style_sync, "VERIFY", self.clean_verify(f"sleep 300 &\ntouch {started}\nwait\n")), \
                redirect_stdout(output), redirect_stderr(output):
            code = style_sync.main(["lint", "--repo", str(self.repo), "--checkout", str(self.repo), "--tip", self.tip])
        self.assertEqual(code, 128 + signal.SIGTERM, output.getvalue())
        self.assertEqual(len(verifiers), 1)
        self.assertTrue(wait_until(lambda: not group_running(verifiers[0]), 5))
        self.assertEqual(list(self.lint_temp.iterdir()), [])

    def test_after_a_sigkill_of_the_owner_the_sweep_keeps_the_dir_until_its_verifier_ends(self) -> None:
        driver = self.root / "lint_driver.py"
        _ = driver.write_text(LINT_DRIVER)
        original_worktrees = self.git("worktree", "list", "--porcelain")
        verifier = self.root / "verifier"
        verify = self.clean_verify(f'sleep 300 &\necho "$$" > {verifier}.part && mv {verifier}.part {verifier}\nwait\n')
        process = subprocess.Popen(
            [sys.executable, str(driver), str(verify), str(self.lint_temp),
             "lint", "--repo", str(self.repo), "--checkout", str(self.repo), "--tip", self.tip],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            self.assertTrue(wait_until(lambda: verifier.exists() or process.poll() is not None, 30))
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
        self.assertTrue(wait_until(lambda: not group_running(group), 5))
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
        planted = self.lint_temp / f"{style_sync.LINT_PREFIX}planted"
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
        code, output = self.run_lint(shlex.join([sys.executable, "-c", probe]) + "\n")
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
            + " out.write(json.dumps(sys.argv[1:]) + '\\n')\n",
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
            code = style_sync.main(["send-handoff", "--repo", str(self.repo), "--slug", "sample-style",
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
            code = style_sync.main(["resume", "--repo", str(self.repo), "--slug", "sample-style",
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
        with mock.patch("style_sync.subprocess.run", wraps=real_run, side_effect=observe_send), redirect_stdout(output):
            code = style_sync.main(["resume", "--repo", str(self.repo), "--slug", "sample-style",
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
            code = style_sync.main(["send-handoff", "--repo", str(self.repo), "--slug", "sample-style",
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
            code = style_sync.main(["send-handoff", "--repo", str(self.repo), "--slug", "sample-style",
                                    "--checkpoint", checkpoint])
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue().strip(), f"root: merge {checkpoint[:7]} with --root-self")
        self.assertEqual(self.handoff_rows()[0][:3], (checkpoint, "pending", ""))
        self.assertEqual(self.handoff_notices(), [])

    def test_queued_send_handoff_is_sent_to_the_parent(self) -> None:
        """send.py queued a hand-off and the row stayed pending, so its merge was refused (cargo-liner, 2026-10-27)."""
        checkpoint = self.handoff_checkpoint()
        self.handoff_sender("QUEUED: relay timed out", 1)
        self.assertEqual(self.send_handoff(checkpoint), (0, f"hand-off {checkpoint[:7]}: queued for parent\n"))
        self.assertEqual(self.handoff_rows()[0][:3], (checkpoint, "sent", "parent"))
        self.assertEqual(len(self.handoff_notices()), 1)

    def test_undelivered_send_handoff_stays_pending(self) -> None:
        checkpoint = self.handoff_checkpoint()
        for outcome, code in (("FAILED: unavailable", 3), ("relay error", 1)):
            with self.subTest(outcome=outcome):
                self.handoff_sender(outcome, code)
                self.assertEqual(self.send_handoff(checkpoint), (1, f"style_sync: hand-off: {outcome}\n"))
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
                _ = style_sync.main(["send-handoff", "--repo", str(self.repo), "--slug", "sample-style",
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
