"""Sync notices are best effort; lint records describe the exact requested tree."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import TypedDict, cast, final, override
from unittest import mock

import style_sync
import style_ledger
import style_rules
import showrunners
from scripts.delegate import test_verify_lint_scope as verify_fixture


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


@final
class StyleSyncTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    style: Path = Path()
    doc: Path = Path()
    tip: str = ""

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
        _ = self.git("branch", "style/main")
        if not on_style_branch:
            _ = (self.repo / "src/lib.rs").write_text("fn changed() {}\n")
            _ = self.git("commit", "-qam", "Unit checkpoint")
        checkpoint = self.git("rev-parse", "HEAD")
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.set_tip(self.tip, "style/main")
            ledger.ticket_add("modules-unit-1", "modules-unit", "read", "modules", ".",
                              ["src/lib.rs"], ["global:rust/clippy.md"])
            ledger.ticket_assign("modules-unit-1", "main")
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
                    and options.get("timeout") == 15 and not interleaved):
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
        self.assertEqual(fixture.calls("+nightly"), [["fmt", "--version"], ["fmt", "--all"]])
        self.assertEqual(fixture.calls("clippy"), [
            ["--workspace", "--all-targets", "--all-features", "--", "-D", "warnings"],
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
        _ = verify.write_text("#!/bin/bash\ncat <<'OUTPUT'\n" + CLEAN_LINT + "OUTPUT\n")
        with mock.patch.object(style_sync, "VERIFY", verify), redirect_stdout(io.StringIO()):
            self.assertEqual(style_sync.main(["lint", "--repo", str(self.repo), "--checkout", str(self.repo),
                                              "--tip", self.tip]), 0)
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
