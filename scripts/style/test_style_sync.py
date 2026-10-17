"""Sync notices are best effort; lint records describe the exact requested tree."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from typing import TypedDict, cast, final, override
from unittest import mock

import style_sync
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


if __name__ == "__main__":
    _ = unittest.main()
