"""Exercise waiting decisions in disposable production repositories."""

from __future__ import annotations

from scripts.whoami.note_file import NumberedAccountLabel, parse_account_label, note_name

import fcntl
import io
import json
import os
import re
import select
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast, final, override
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import add_unit
import dailies_render
import merge_checkpoint
import update_registration
import waiting
from cli_fixture import invoke_cli
import unit_lookup
import herdr
from fake_herdr import CommandResponse, herdr_listener

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "whoami"))
import agent_accounts
import quota_alert
from scripts.accounts.accounts import CodexWorkersAccount


# Script completion deadlines only bound hangs; startup can be slow under load.
SCRIPT = Path(__file__).with_name("waiting.py")
CI_POINTS = SCRIPT.with_name("ci_points.py")
ALPHA = "alpha-unit"
BETA = "beta-unit"
GAMMA = "gamma-unit"
PHASE = "Phase 2 of 3: panel labels stay clear"
# Quota notices read no live account dirs: each test names the Claude accounts in use.
MAIN_CLAUDE = agent_accounts.NumberedAccount("claude2", Path.home() / ".claude", "main@example.invalid")
UNIT_CLAUDE = agent_accounts.NumberedAccount("claude3", Path("/unread/.claude3"), "unit@example.invalid")
ONE_CLAUDE_ACCOUNT = agent_accounts.ClaudeAccountsInUse(MAIN_CLAUDE, False, (), frozenset())
TWO_CLAUDE_ACCOUNTS = agent_accounts.ClaudeAccountsInUse(
    MAIN_CLAUDE, True,
    (agent_accounts.CheckedNamedClaudeAccount("claude3", Path("/unread/.claude3"), UNIT_CLAUDE),),
    frozenset((UNIT_CLAUDE.login,)))
PHASE_IDENTITY = "Phase 2 of 3"
BERTH_STUB = '''#!__PYTHON__
import os
from pathlib import Path
import sys

if sys.argv[1:] != ["board", "--json"]:
    raise SystemExit(99)
print(Path(os.environ["WAITING_TEST_BOARD"]).read_text(encoding="utf-8"))
raise SystemExit(int(os.environ.get("WAITING_TEST_BOARD_EXIT", "0")))
'''
NO_EXTERNAL_COMMAND = '''#!__PYTHON__
raise SystemExit(99)
'''
SEND_STUB = '''#!__PYTHON__
import json
import os
from pathlib import Path
import sys

with Path(os.environ["WAITING_TEST_SENDS"]).open("a", encoding="utf-8") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
'''
SEND_OUTCOME_STUB = '''#!__PYTHON__
import json
import os
from pathlib import Path
import sys

with Path(os.environ["WAITING_TEST_SENDS"]).open("a", encoding="utf-8") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
recipient = sys.argv[sys.argv.index("--to") + 1]
codes = {"alpha-unit": 0, "beta-unit": 1, "gamma-unit": 3}
print(f"delivery outcome for {recipient}")
raise SystemExit(codes[recipient])
'''


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


@final
class QuotaNote:
    path: Path
    tool: str
    values: dict[str, str]
    label: str

    def __init__(self, path: Path, tool: str, values: dict[str, str]) -> None:
        self.path = path
        self.tool = tool
        parsed = parse_account_label(path.stem)
        assert isinstance(parsed, NumberedAccountLabel)
        self.label = parsed.label
        self.values = values

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def get_list(self, key: str) -> tuple[str, ...]:
        value = self.get(key)
        return (value,) if value and value != "[]" else ()


@final
class WaitingTests(unittest.TestCase):
    root: Path = Path()
    checkout: Path = Path()
    alpha: Path = Path()
    beta: Path = Path()
    doc: Path = Path()
    log: Path = Path()
    state: Path = Path()
    board: Path = Path()
    sends: Path = Path()
    origin: Path = Path()
    bin_path: Path = Path()
    env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.checkout = self.root / "checkout"
        self.alpha = self.root / "alpha"
        self.beta = self.root / "beta"
        self.doc = self.checkout / "docs/plans/example-production.md"
        self.log = self.checkout / "production.log"
        self.state = self.root / "state"
        self.board = self.root / "board.json"
        self.sends = self.root / "sends.jsonl"
        self.state.mkdir()
        self.bin_path = self.root / "bin"
        self.bin_path.mkdir()
        berth = self.bin_path / "cargo-berth"
        _ = berth.write_text(BERTH_STUB.replace("__PYTHON__", sys.executable), encoding="utf-8")
        berth.chmod(0o755)
        for name in ("quota_alert.py", "quota", "claude", "tmux", "systemd-run", "ssh", "nix"):
            stub = self.bin_path / name
            _ = stub.write_text(NO_EXTERNAL_COMMAND.replace("__PYTHON__", sys.executable), encoding="utf-8")
            stub.chmod(0o755)
        send = self.bin_path / "send.py"
        _ = send.write_text(SEND_STUB.replace("__PYTHON__", sys.executable), encoding="utf-8")
        send.chmod(0o755)
        home = self.root / "home"
        home.mkdir()
        # Each unit has a tmux session marked as that unit, with a Claude running under the unit's id
        # as its name. Tests that call the tool in this process read the same stand-ins.
        (self.root / "sessions").mkdir()
        self.write(self.root / "showrunners.json", json.dumps({
            "threshold_percent": 2, "repeat_minutes": 30, "stall_minutes": 5, "faults_to": "natedev",
            "always": []}))
        self.write(self.root / "herdr-responses.json", json.dumps({
            "default": CommandResponse(stdout="", code=1),
        }))
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(home),
            "CODEX_HOME": str(home / "codex-fixture"),
            "UNIT_LOOKUP_TMUX": str(Path(__file__).with_name("fake_tmux.sh")),
            "FAKE_TMUX_STATE": str(self.root / "tmux.json"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "HERDR_BIN": str(SCRIPT.parents[1] / "herdr/fake_herdr.py"),
            "HERDR_PROC": str(self.root / "proc"),
            "FAKE_HERDR_LOG": str(self.root / "herdr.jsonl"),
            "FAKE_HERDR_RESPONSES": str(self.root / "herdr-responses.json"),
            "SHOWRUNNERS_CONFIG": str(self.root / "showrunners.json")}))
        _ = self.enterContext(mock.patch.object(quota_alert, "observe_workers_home", side_effect=AssertionError(
            "quota tests must supply Codex readings instead of resolving a live home")))
        self.write(self.root / "tmux.json", json.dumps({
            f"${index}": {"label": "any-label", "panes": [f"%{index}"],
                          "env": {"SHOWRUNNER_UNIT": "example", "SHOWRUNNER_UNIT_ID": unit}}
            for index, unit in enumerate((ALPHA, BETA, GAMMA), start=1)}))
        for index, unit in enumerate((ALPHA, BETA, GAMMA), start=1):
            self.claude(unit, f"%{index}")
        self.env = {**os.environ, "HOME": str(home), "WAITING_TEST_BOARD": str(self.board),
                    "WAITING_TEST_SENDS": str(self.sends),
                    "PATH": str(self.bin_path) + os.pathsep + os.environ.get("PATH", "")}
        self.board_ready()
        self.origin = self.root / "origin.git"
        _ = self.git("init", "--bare", str(self.origin), cwd=self.root)
        _ = self.git("init", "-b", "production", str(self.checkout), cwd=self.root)
        _ = self.git("config", "user.name", "Waiting Test")
        _ = self.git("config", "user.email", "waiting@example.invalid")
        _ = self.git("remote", "add", "origin", str(self.origin))
        self.write(self.checkout / "README.md", "base\n")
        self.write(self.checkout / "shared.txt", "base\n")
        self.production_doc()
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "initial production")
        _ = self.git("push", "-u", "origin", "production")
        _ = self.git("worktree", "add", "-b", "alpha-branch", str(self.alpha), "production")
        _ = self.git("worktree", "add", "-b", "beta-branch", str(self.beta), "production")
        self.write(self.log, "# Production log — example\n\n### STATE\n")

    def write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content, encoding="utf-8")

    def board_ready(self, overlaps: list[dict[str, object]] | None = None) -> None:
        self.write(self.board, json.dumps({
            "output_contract_version": 6,
            "verb": "board",
            "status": "board_ready",
            "exit_code": 0,
            "reservations": [],
            "blocked_by": [],
            "message": "The reservation board was read. Use `cargo-berth board --json` to inspect it.",
            "presentation": {"kind": "rendered_blocks", "blocks": []},
            "payload": {"kind": "board", "data": {
                "unresolved_overlaps": {"journal_position": {
                    "generation": 3, "journal_byte_offset": 2820,
                }, "entries": overlaps or []},
            }},
        }) + "\n")
        self.env["WAITING_TEST_BOARD_EXIT"] = "0"

    def unconfigured_board(self) -> None:
        self.write(self.board, json.dumps({
            "output_contract_version": 6,
            "verb": "board",
            "status": "unconfigured",
            "exit_code": 4,
            "reservations": [],
            "blocked_by": [],
            "message": "Cargo Berth is not configured for this repository.",
            "presentation": {"kind": "not_provided"},
            "payload": {"kind": "no_facts", "alerts": []},
        }) + "\n")
        self.env["WAITING_TEST_BOARD_EXIT"] = "4"

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", *args], cwd=cwd or self.checkout, env=self.env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, (args, result.stdout, result.stderr))
        return result.stdout.strip()

    def git_result(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=cwd or self.checkout, env=self.env,
                              capture_output=True, text=True, check=False)

    def refs(self, cwd: Path) -> str:
        return self.git("for-each-ref", "--format=%(refname) %(objectname)", cwd=cwd)

    def claude(self, name: str, pane: str) -> None:
        """Record a live Claude called `name` in `pane`: this test process stands in for it."""
        path = self.root / f"claude-{pane.removeprefix('%')}.sock"
        if not path.exists():
            held = socket.socket(socket.AF_UNIX)
            held.bind(str(path))
            self.addCleanup(held.close)
        self.write(self.root / "sessions" / f"{pane.removeprefix('%')}.json", json.dumps({
            "pid": os.getpid(), "sessionId": f"id-{name}", "name": name, "messagingSocketPath": str(path),
            "updatedAt": 1, "tmux": f"label-at-start:@1.{pane}"}))

    def production_doc(self) -> None:
        self.write(self.doc, "\n".join((
            "# Production — example", "", "> **Status: PRODUCTION — running.** Example.",
            "", "## Production Context", "", "- **Merge branch:** `production`",
            f"- **Showrunner checkout:** `{self.checkout}`", "- **Showrunner session:** showrunner-example",
            "- **Log:** `production.log`", "- **User zone:** America/Los_Angeles",
            "- **Updates:** every 15 minutes", "", "## Units", "",
            "| Unit | Plan | Worktree | Branch | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- |",
            f"| `{ALPHA}` | `docs/alpha.md` | `{self.alpha}` | `alpha-branch` | — | `shared.txt` |",
            f"| `{BETA}` | `docs/beta.md` | `{self.beta}` | `beta-branch` | — | `src/beta.py` |",
            "", "## Gates", "", "| Gate | Waiting | Waits on | Clears when |",
            "| --- | --- | --- | --- |",
            f"| G1 | {ALPHA} phase 2 | {BETA} phase 1 | beta code merged |",
            "", "## Production rules", "", "- Merge each phase and push the merge branch.", "",
        )))

    def add_gamma(self) -> None:
        content = self.doc.read_text(encoding="utf-8")
        row = f"| `{GAMMA}` | `docs/gamma.md` | `{self.root / 'gamma'}` | `gamma-branch` | — | — |\n"
        self.write(self.doc, content.replace("\n\n## Gates", f"\n{row}\n## Gates"))

    def alpha_session(self, session: str) -> None:
        """The user renamed alpha's session: only the live session record says so."""
        self.claude(session, "%1")

    def test_shell_tmux_rejects_malformed_calls_without_changing_the_fixture(self) -> None:
        path = self.root / "tmux.json"
        before = path.read_text()
        for arguments in (["list-panes"], ["capture-pane", "-p", "-t", "%1"],
                          ["show-environment", "-t", "$1", "EXTRA"],
                          ["set-environment", "-t", "$1", "MARK"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run([self.env["UNIT_LOOKUP_TMUX"], *arguments], env=self.env,
                                        capture_output=True, text=True, check=False, timeout=300)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("unexpected call", result.stderr)
                self.assertEqual(path.read_text(), before)

    def test_shell_tmux_names_missing_targets_and_preserves_state_on_every_targeted_call(self) -> None:
        path = self.root / "tmux.json"
        before = path.read_text()
        for target in ("missing session", "%404", "=$missing; π"):
            for arguments, kind in ((["show-environment", "-t", target], "session"),
                                    (["set-environment", "-t", target, "MARK", "value"], "session"),
                                    (["capture-pane", "-p", "-e", "-t", target], "pane"),
                                    (["send-keys", "-t", target, "Enter"], "pane")):
                with self.subTest(arguments=arguments):
                    result = subprocess.run([self.env["UNIT_LOOKUP_TMUX"], *arguments], env=self.env,
                                            capture_output=True, text=True, check=False, timeout=300)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(result.stderr, f"can't find {kind}: {target}\n")
                    self.assertEqual(path.read_text(), before)
                    self.assertEqual(list(self.root.glob("tmux.json.*.tmp")), [])

    def test_shell_tmux_preserves_session_marks_and_literal_key_arguments(self) -> None:
        tool = self.env["UNIT_LOOKUP_TMUX"]
        for arguments in (["set-environment", "-t", "$1", "TEST_MARK", "marked value"],
                          ["send-keys", "-t", "%1", "-l", "echo '$HOME'; π"],
                          ["send-keys", "-t", "%1", "Enter"]):
            result = subprocess.run([tool, *arguments], env=self.env, capture_output=True,
                                    text=True, check=False, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = subprocess.run([tool, "show-environment", "-t", "%1"], env=self.env,
                                capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TEST_MARK=marked value", result.stdout.splitlines())
        sessions = cast(dict[str, dict[str, object]], json.loads((self.root / "tmux.json").read_text()))
        self.assertEqual(sessions["$1"]["sent"], [["%1", "-l", "echo '$HOME'; π"], ["%1", "Enter"]])
        self.assertNotIn("sent", sessions["$2"])

    def run_waiting(self, action: str, *args: str) -> subprocess.CompletedProcess[str]:
        arguments = [action, *args, "--production", str(self.doc)]
        return invoke_cli(waiting, waiting.main, arguments, self.env, self.checkout, production_state=True)

    def commit(self, worktree: Path, relative: str, content: str) -> str:
        self.write(worktree / relative, content)
        _ = self.git("add", relative, cwd=worktree)
        _ = self.git("commit", "-m", f"change {relative}", cwd=worktree)
        return self.git("rev-parse", "HEAD", cwd=worktree)

    def worktrees(self) -> str:
        return self.git("worktree", "list", "--porcelain")

    def assert_scratch_clean(self, worktrees: str, unit_tip: str, merge_tip: str,
                             local_refs: str, origin_refs: str) -> None:
        self.assertEqual(self.worktrees(), worktrees)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.alpha), unit_tip)
        self.assertEqual(self.git("rev-parse", "HEAD"), merge_tip)
        self.assertEqual(self.refs(self.checkout), local_refs)
        self.assertEqual(self.refs(self.origin), origin_refs)
        self.assertEqual(self.git("status", "--porcelain", cwd=self.alpha), "")
        self.assertEqual(self.git("status", "--porcelain"), "?? production.log")
        for checkout in (self.checkout, self.alpha):
            self.assertNotEqual(self.git_result("rev-parse", "-q", "--verify", "MERGE_HEAD",
                                                cwd=checkout).returncode, 0)

    def test_named_unit_refuses_retired_row(self) -> None:
        content = self.doc.read_text(encoding="utf-8")
        self.write(self.doc, content.replace("`docs/alpha.md`", "(retired by the user)", 1))
        production = add_unit.read_production(self.doc)
        with self.assertRaisesRegex(waiting.WaitingFailure, f"unknown unit {ALPHA}"):
            _ = waiting.named_unit(production, ALPHA)

    def push_scratch_branches(self) -> None:
        _ = self.git("push", "origin", "production")
        _ = self.git("push", "-u", "origin", "alpha-branch", cwd=self.alpha)

    def scratch_log(self, output: str) -> Path:
        found = re.search(r"\(([^\n()]*scratch-test-alpha-unit-[^\n()]*\.log)\)", output)
        self.assertIsNotNone(found, output)
        path = Path(found.group(1)) if found else Path()
        self.assertTrue(path.is_file(), output)
        self.assertEqual(path.parent, self.state)
        return path

    def producer_launch_line(self, moment: datetime, *, session: str = ALPHA) -> str:
        production = add_unit.read_production(self.doc)
        producer_log = self.root / "launch-producer.log"
        _ = producer_log.unlink(missing_ok=True)
        request = add_unit.UnitLaunch(
            production=production._replace(log=producer_log),
            identity=add_unit.UnitIdentity(ALPHA, session),
            branch="alpha-branch",
            worktree=self.alpha,
            plan=add_unit.PlanGiven(self.root / "alpha-plan.md"),
            port=add_unit.OmittedCell(),
            owns=add_unit.OmittedCell(),
            session=add_unit.NewSession(),
            launch_kind=add_unit.NewWorkLaunch(),
            timeout=1.0,
        )
        with redirect_stdout(io.StringIO()):
            add_unit.record(request)
        produced = producer_log.read_text(encoding="utf-8").strip().partition(": ")[2]
        return f"- {moment:%H:%M %Z}: {produced}"

    def producer_merge_line(self, moment: datetime, unit_tip: str, merge_tip: str,
                            phase: str = "1") -> str:
        production = add_unit.read_production(self.doc)
        producer_log = self.root / "merge-producer.log"
        _ = producer_log.unlink(missing_ok=True)
        unit = merge_checkpoint.Unit(ALPHA, self.root / "alpha-plan.md", self.alpha,
                                     "alpha-branch", ("shared.txt",))
        request = merge_checkpoint.MergeRequest(
            production._replace(log=producer_log), unit, phase, unit_tip, (),
            merge_checkpoint.ShrinkCommit(), merge_checkpoint.ValidateAndPush(),
            merge_checkpoint.NoPromotion(), (), frozenset(), (unit,), (), self.root,
            (), merge_checkpoint.NoReviewTrial(), "", False, "", "", 0, 0, "",
        )
        with redirect_stdout(io.StringIO()):
            merge_checkpoint.record(request, merge_tip, "merge tests")
        produced = producer_log.read_text(encoding="utf-8").strip().partition(": ")[2]
        return f"- {moment:%H:%M %Z}: {produced}"

    def producer_state_block(self, moment: datetime, phases: dict[str, str]) -> str:
        production = add_unit.read_production(self.doc)
        judgments = tuple(update_registration.UnitJudgment(session, phase, "none")
                          for session, phase in phases.items())
        state = update_registration.ShowrunnerState(judgments, (), update_registration.KeepOutstanding())
        return update_registration.state_block(production, state, moment.strftime("%H:%M %Z"))

    def producer_eta_line(self, moment: datetime, phase: str, eta: datetime,
                          *, identifier: str = ALPHA) -> str:
        days = (eta.date() - moment.date()).days
        eta_clock = eta.strftime("%H:%M") + (f"+{days}" if days else "")
        fields: dict[str, object] = {
            "length": "simple",
            "zone": "America/Los_Angeles",
            "units": [{
                "unit": identifier,
                "label": "alpha",
                "project": "panels that stay readable",
                "phase": phase,
                "started": moment.replace(tzinfo=None).isoformat(timespec="minutes"),
                "held": None,
                "update": "checking the panel labels",
                "eta": {"time": eta_clock, "percent": 60},
                "then": ["nothing queued"],
            }],
            "topics": [],
        }
        holders = self.root / "producer-holders"
        holders.mkdir(exist_ok=True)
        with mock.patch.dict(os.environ, {
            "BUILD_HOLD_DIR": str(holders),
            "MAC_TEST_STATE_DIR": str(self.root / "mac-test"),
        }):
            report = dailies_render.parse_report(fields, "default")
        resolved = dailies_render.resolve_eta_moments(report, {}, moment)
        return dailies_render.log_line(report, resolved, moment, moment.tzname() or "PDT")

    def producer_held_eta_line(self, moment: datetime, phase: str, eta: datetime) -> str:
        eta_text = eta.strftime("%H:%M")
        fields: dict[str, object] = {
            "length": "simple",
            "zone": "America/Los_Angeles",
            "units": [{
                "unit": ALPHA,
                "label": "alpha",
                "project": "panels that stay readable",
                "phase": phase,
                "started": (moment - timedelta(hours=5)).replace(
                    tzinfo=None).isoformat(timespec="minutes"),
                "held": "the panel review is paused",
                "update": "checking the panel labels",
                "eta": {"time": eta_text, "percent": 60},
                "then": ["nothing queued"],
            }],
            "topics": [],
        }
        holders = self.root / "producer-holders"
        holders.mkdir(exist_ok=True)
        with mock.patch.dict(os.environ, {
            "BUILD_HOLD_DIR": str(holders),
            "MAC_TEST_STATE_DIR": str(self.root / "mac-test"),
        }):
            report = dailies_render.parse_report(fields, "default")
        previous = {ALPHA: dailies_render.LastUnitReport(
            phase,
            dailies_render.LastReportedEta(eta_text, eta),
            "the panel review is paused",
            eta,
        )}
        resolved = dailies_render.resolve_eta_moments(report, previous, moment)
        return dailies_render.log_line(report, resolved, moment, moment.tzname() or "PDT")

    def quota_note(self, name: str, tool: str = "codex", login: str = "") -> QuotaNote:
        return QuotaNote(self.root / note_name(name), tool, {
            "role": "worker" if tool == "codex" else "unit", "sessions": "0",
            "weekly_remaining_usage": "4",
            "resets": "2026-10-12 09:00 PDT",
            "weekly_usage_checked_at": "2026-10-07 02:00 PDT",
            **({"login": login} if login else {}),
        })

    def quota_message(self, note: QuotaNote,
                      selection: agent_accounts.ClaudeAccountsInUse = ONE_CLAUDE_ACCOUNT) -> str:
        config: quota_alert.Config = {
            "threshold_percent": 5.0,
            "repeat_minutes": 30.0,
            "stall_minutes": 5.0,
            "faults_to": "showrunner-example",
            "always": [],
        }
        home = self.root / "codex-fixture"
        readings = quota_alert.CodexAlertPassReadings(
            CodexWorkersAccount(note.label, home),
            agent_accounts.KnownCodexAccount(home, "codex@example.invalid", note.label),
            (),
        )
        return quota_alert.message(note, [], config, selection=selection, codex_readings=readings)

    def acknowledgement_message(self, account: str) -> str:
        return f'Quota alert acknowledged: the user silenced {account} in session "showrunner-example" at 2026-10-07T06:00:00+00:00.'

    def test_scratch_green_uses_detached_unit_tip_and_lift_accepts_its_log(self) -> None:
        unit_tip = self.commit(self.alpha, "alpha.txt", "unit work\n")
        merge_tip = self.commit(self.checkout, "merged.txt", "merge branch work\n")
        _ = self.commit(self.beta, "other-unit.txt", "unmerged work\n")
        self.push_scratch_branches()
        before = self.worktrees()
        local_refs, origin_refs = self.refs(self.checkout), self.refs(self.origin)
        probe = "from pathlib import Path; assert Path('alpha.txt').exists(); assert Path('merged.txt').exists(); assert not Path('other-unit.txt').exists(); print('scratch green marker')"
        result = self.run_waiting("scratch-test", ALPHA, "--tests",
                                  f"{shlex.quote(sys.executable)} -c {shlex.quote(probe)}", "--state-dir", str(self.state))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("scratch-test: ok — green", result.stdout)
        log = self.scratch_log(result.stdout)
        self.assertIn("scratch green marker", log.read_text(encoding="utf-8"))
        self.assert_scratch_clean(before, unit_tip, merge_tip, local_refs, origin_refs)
        lift = subprocess.run([sys.executable, str(CI_POINTS), "notice", "lift", "G1",
                               "--production", str(self.doc), "--log", str(log)],
                              cwd=self.checkout, env=self.env, capture_output=True, text=True, check=False)
        self.assertEqual(lift.returncode, 0, lift.stdout + lift.stderr)
        self.assertIn(str(log), lift.stdout)
        self.assertIn("G1 lifted", lift.stdout)

    def test_scratch_red_keeps_full_log_and_cleans_worktree_without_claiming_block(self) -> None:
        unit_tip = self.commit(self.alpha, "alpha.txt", "unit work\n")
        merge_tip = self.commit(self.checkout, "merged.txt", "merge branch work\n")
        self.push_scratch_branches()
        before = self.worktrees()
        local_refs, origin_refs = self.refs(self.checkout), self.refs(self.origin)
        probe = "import sys; print('red marker'); sys.exit(7)"
        result = self.run_waiting("scratch-test", ALPHA, "--tests",
                                  f"{shlex.quote(sys.executable)} -c {shlex.quote(probe)}", "--state-dir", str(self.state))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("scratch-test: failed — red:", result.stdout)
        self.assertNotIn("blocked", result.stdout.lower())
        self.assertIn("red marker", self.scratch_log(result.stdout).read_text(encoding="utf-8"))
        self.assert_scratch_clean(before, unit_tip, merge_tip, local_refs, origin_refs)

    def test_scratch_merge_conflict_cleans_worktree_without_running_tests(self) -> None:
        unit_tip = self.commit(self.alpha, "shared.txt", "alpha\n")
        merge_tip = self.commit(self.checkout, "shared.txt", "production\n")
        self.push_scratch_branches()
        before = self.worktrees()
        local_refs, origin_refs = self.refs(self.checkout), self.refs(self.origin)
        marker = self.root / "tests-ran"
        probe = f"from pathlib import Path; Path({str(marker)!r}).touch()"
        result = self.run_waiting("scratch-test", ALPHA, "--tests",
                                  f"{shlex.quote(sys.executable)} -c {shlex.quote(probe)}", "--state-dir", str(self.state))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("scratch-test: failed —", result.stdout)
        self.assertIn("conflict", result.stdout.lower())
        self.assertNotIn("red:", result.stdout)
        self.assertFalse(marker.exists())
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1, result.stdout)
        self.assertTrue(lines[0].startswith("scratch-test: failed — merge conflict:"), result.stdout)
        conflict_log = self.scratch_log(result.stdout).read_text(encoding="utf-8")
        self.assertGreater(len(conflict_log.splitlines()), 1)
        self.assertIn("CONFLICT", conflict_log)
        self.assert_scratch_clean(before, unit_tip, merge_tip, local_refs, origin_refs)

    def test_scratch_unstartable_command_cleans_worktree_and_keeps_log(self) -> None:
        unit_tip = self.commit(self.alpha, "alpha.txt", "unit work\n")
        merge_tip = self.commit(self.checkout, "merged.txt", "merge branch work\n")
        self.push_scratch_branches()
        before = self.worktrees()
        local_refs, origin_refs = self.refs(self.checkout), self.refs(self.origin)
        result = self.run_waiting("scratch-test", ALPHA, "--tests",
                                  "command-that-does-not-exist-waiting-test", "--state-dir", str(self.state))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("scratch-test: failed —", result.stdout)
        self.assertNotIn("green", result.stdout)
        self.assertTrue(self.scratch_log(result.stdout).read_text(encoding="utf-8"))
        self.assert_scratch_clean(before, unit_tip, merge_tip, local_refs, origin_refs)

    def test_scratch_log_preserves_interleaved_stream_order_and_red_tail(self) -> None:
        _ = self.commit(self.alpha, "alpha.txt", "unit work\n")
        _ = self.commit(self.checkout, "merged.txt", "merge branch work\n")
        self.push_scratch_branches()
        probe = ("import sys; print('stdout first', flush=True); "
                 "print('stderr second', file=sys.stderr, flush=True); "
                 "print('stdout third', flush=True); sys.exit(7)")
        result = self.run_waiting("scratch-test", ALPHA, "--tests",
                                  f"{shlex.quote(sys.executable)} -c {shlex.quote(probe)}",
                                  "--state-dir", str(self.state))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        log_text = self.scratch_log(result.stdout).read_text(encoding="utf-8")
        self.assertLess(log_text.index("stdout first"), log_text.index("stderr second"))
        self.assertLess(log_text.index("stderr second"), log_text.index("stdout third"))
        verdict = next(line for line in result.stdout.splitlines() if line.startswith("scratch-test:"))
        self.assertLess(verdict.index("stdout first"), verdict.index("stderr second"))
        self.assertLess(verdict.index("stderr second"), verdict.index("stdout third"))

    def test_scratch_cleanup_failure_prints_verdict_before_cleanup_failure(self) -> None:
        _ = self.commit(self.alpha, "alpha.txt", "unit work\n")
        _ = self.commit(self.checkout, "merged.txt", "merge branch work\n")
        self.push_scratch_branches()
        real_git = shutil.which("git", path=os.environ.get("PATH"))
        self.assertIsNotNone(real_git)
        wrapper = self.bin_path / "git"
        self.write(wrapper, '''#!__PYTHON__
import os
import sys

arguments = sys.argv[1:]
if "worktree" in arguments and arguments[arguments.index("worktree") + 1] == "remove":
    print("cleanup refused by test wrapper", file=sys.stderr)
    raise SystemExit(9)
os.execv(os.environ["WAITING_TEST_REAL_GIT"], [os.environ["WAITING_TEST_REAL_GIT"], *arguments])
'''.replace("__PYTHON__", sys.executable))
        wrapper.chmod(0o755)
        self.env["WAITING_TEST_REAL_GIT"] = real_git or "git"
        result = self.run_waiting("scratch-test", ALPHA, "--tests", "printf 'green marker\\n'",
                                  "--state-dir", str(self.state))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.startswith("scratch-test:")]
        self.assertEqual(len(lines), 2, result.stdout)
        self.assertIn("ok — green", lines[0])
        self.assertIn("failed — temporary worktree cleanup: cleanup refused by test wrapper", lines[1])

    def test_search_reports_other_unit_branch_and_uncommitted_worktree_sites(self) -> None:
        _ = self.commit(self.beta, "src/beta.py", "old_public_name()\n")
        self.write(self.beta / "src/draft.py", "old_public_name()\n")
        self.write(self.alpha / "alpha.txt", "old_public_name()\n")
        result = self.run_waiting("search", "old_public_name", "new_public_name")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("src/beta.py:1", result.stdout)
        self.assertIn("src/draft.py:1", result.stdout)
        self.assertIn(BETA, result.stdout)

    def test_eta_request_records_requested_alone_once_per_phase(self) -> None:
        seen_path = self.state / "eta_seen.json"
        original = json.dumps({
            f"{ALPHA}|{PHASE}": {
                "text": "ETA 14:00",
                "first_seen": "2026-10-07T01:00",
                "requested": False,
            },
        }, separators=(",", ":")) + "\n"
        self.write(seen_path, original)
        first = self.run_waiting("eta-request", ALPHA, "--phase", PHASE,
                                 "--state-dir", str(self.state))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn(f"request /unit:eta: {ALPHA}", first.stdout)
        seen = cast(dict[str, dict[str, object]], json.loads(seen_path.read_text()))
        self.assertEqual(seen, {f"{ALPHA}|{PHASE}": {"requested": True}})
        requested_bytes = seen_path.read_bytes()
        repeated = self.run_waiting("eta-request", ALPHA, "--phase", PHASE,
                                    "--state-dir", str(self.state))
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertNotIn("request /unit:eta:", repeated.stdout)
        self.assertEqual(seen_path.read_bytes(), requested_bytes)
        next_phase = self.run_waiting("eta-request", ALPHA, "--phase", "Phase 3: edges align",
                                      "--state-dir", str(self.state))
        self.assertEqual(next_phase.returncode, 0, next_phase.stdout + next_phase.stderr)
        self.assertIn(f"request /unit:eta: {ALPHA}", next_phase.stdout)

    def eta_request_process(self, phase: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, str(SCRIPT), "eta-request", ALPHA, "--phase", phase,
             "--production", str(self.doc), "--state-dir", str(self.state)],
            cwd=self.checkout, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_parallel_eta_requests_for_different_phases_keep_every_record(self) -> None:
        phases = [f"Phase {number}: parallel record" for number in range(1, 13)]
        processes = [self.eta_request_process(phase) for phase in phases]
        outputs = [process.communicate(timeout=300) for process in processes]
        for process, (stdout, stderr) in zip(processes, outputs):
            self.assertEqual(process.returncode, 0, stdout + stderr)
        seen = cast(dict[str, object], json.loads((self.state / "eta_seen.json").read_text()))
        self.assertEqual(set(seen), {f"{ALPHA}|{phase}" for phase in phases})
        self.assertFalse([path for path in self.state.iterdir() if path.suffix == ".tmp"])

    def test_parallel_eta_requests_for_one_phase_print_once(self) -> None:
        processes = [self.eta_request_process(PHASE) for _ in range(12)]
        outputs = [process.communicate(timeout=300) for process in processes]
        for process, (stdout, stderr) in zip(processes, outputs):
            self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(sum(stdout.count(f"request /unit:eta: {ALPHA}")
                             for stdout, _ in outputs), 1)
        seen = cast(dict[str, object], json.loads((self.state / "eta_seen.json").read_text()))
        self.assertEqual(seen, {f"{ALPHA}|{PHASE}": {"requested": True}})

    def test_eta_request_waits_for_its_state_lock(self) -> None:
        # The child proves it reached the held lock, rather than racing startup
        # against a short wait(). Keep the real blocking flock after the signal.
        program = """import fcntl, os, runpy, sys
ready = int(sys.argv.pop(1))
sys.argv.pop(0)
original = fcntl.flock
def observed_lock(fd, operation):
    if operation == fcntl.LOCK_EX:
        try:
            original(fd, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            os.write(ready, b'blocked')
        else:
            raise RuntimeError('expected the parent to hold the state lock')
    return original(fd, operation)
fcntl.flock = observed_lock
sys.path.insert(0, os.path.dirname(sys.argv[0]))
runpy.run_path(sys.argv[0], run_name='__main__')
"""
        lock_path = self.state / "eta_seen.json.lock"
        read_ready, write_ready = os.pipe()
        try:
            with lock_path.open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                process = subprocess.Popen(
                    [sys.executable, "-c", program, str(write_ready), str(SCRIPT),
                     "eta-request", ALPHA, "--phase", PHASE,
                     "--production", str(self.doc), "--state-dir", str(self.state)],
                    cwd=self.checkout, env=self.env, pass_fds=(write_ready,),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                os.close(write_ready)
                write_ready = -1
                try:
                    readable, _, _ = select.select([read_ready], [], [], 300)
                    self.assertEqual(readable, [read_ready], "child never reached the held lock")
                    if os.read(read_ready, 7) != b"blocked":
                        stdout, stderr = process.communicate(timeout=300)
                        self.fail("child exited before observing the state lock: " + stdout + stderr)
                    self.assertIsNone(process.poll())
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                    stdout, stderr = process.communicate(timeout=300)
                finally:
                    if process.poll() is None:
                        process.kill()
                    _ = process.communicate(timeout=300)
        finally:
            os.close(read_ready)
            if write_ready != -1:
                os.close(write_ready)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertIn(f"request /unit:eta: {ALPHA}", stdout)

    def test_waits_accepts_unconfigured_berth_and_still_reports_log_waits(self) -> None:
        now = datetime(2026, 10, 14, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        opened = now - timedelta(minutes=72)
        cleared = now - timedelta(minutes=50)
        self.write(self.log, "\n".join((
            "# Production log — example",
            f"- {opened:%H:%M %Z}: block: {ALPHA} on {BETA} (files: shared.txt), clears ~{(opened + timedelta(minutes=20)):%H:%M}",
            f"- {cleared:%H:%M %Z}: block: {BETA} on {ALPHA} (code: API), clears ~{(cleared + timedelta(minutes=20)):%H:%M}",
            f"- {(cleared + timedelta(minutes=5)):%H:%M %Z}: block cleared: {BETA} on {ALPHA}", "",
        )))
        self.unconfigured_board()
        output = io.StringIO()
        clock = mock.Mock(wraps=datetime, now=mock.Mock(return_value=now))
        with mock.patch.dict(os.environ, self.env), \
             mock.patch.object(waiting, "datetime", clock), \
             mock.patch.object(sys, "argv", [str(SCRIPT), "waits", "--production", str(self.doc)]), \
             redirect_stdout(output):
            code = waiting.main()
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("waits: ok — 1 open; berth not configured", output.getvalue())
        self.assertIn(f"{ALPHA} on {BETA}", output.getvalue())
        self.assertIn("open 72 minutes", output.getvalue())
        self.assertNotIn(f"{BETA} on {ALPHA}: open", output.getvalue())

    def test_waits_hide_retired_waiting_unit_but_keep_live_wait_on_it(self) -> None:
        content = self.doc.read_text(encoding="utf-8")
        self.write(self.doc, content.replace("`docs/alpha.md`", "retired after completion", 1))
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
        opened = now - timedelta(minutes=10)
        self.write(self.log, "\n".join((
            "# Production log — example",
            f"- {opened:%H:%M %Z}: block: {ALPHA} on {BETA} (files: shared.txt), clears ~23:00",
            f"- {opened:%H:%M %Z}: block: {BETA} on {ALPHA} (code: API), clears ~23:00",
            "",
        )))
        self.unconfigured_board()
        result = self.run_waiting("waits")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("waits: ok — 1 open; berth not configured", result.stdout)
        self.assertNotIn(f"{ALPHA} on {BETA}: open", result.stdout)
        self.assertIn(f"{BETA} on {ALPHA}: open", result.stdout)

    def test_waits_reads_unresolved_overlaps_from_board_payload(self) -> None:
        self.board_ready([{
            "declaration_event_id": "01a036fb-1629-7712-96b7-1699922daa50",
            "deferred": "01a036fb-1629-7712-96b7-1672b64a151f",
            "blocker": "01a036fa-b70a-7e72-89ae-0facf1976ed1",
            "scopes": [{"path": "crates/shared", "kind": "tree"},
                       {"path": "src/panel.py", "kind": "file"}],
            "reason": "the holder API must land first",
            "origin": "user_answer",
            "consequence": "both_integrations_held_until_sequence",
        }])
        result = self.run_waiting("waits")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("waits: ok — 0 open; 1 berth overlaps", result.stdout)
        self.assertIn("waits: ok — berth overlap: crates/shared, src/panel.py; blocker "
                      + "01a036fa-b70a-7e72-89ae-0facf1976ed1; the holder API must land first",
                      result.stdout)

    def test_waits_rejects_unknown_berth_status_and_non_json_output(self) -> None:
        cases = (
            (json.dumps({"status": "busy", "message": "the ledger is locked"}) + "\n", "4", "busy", "the ledger is locked"),
            ("not berth json\n", "1", "not berth json", ""),
        )
        for content, exit_code, expected, message in cases:
            with self.subTest(content=content):
                self.write(self.board, content)
                self.env["WAITING_TEST_BOARD_EXIT"] = exit_code
                result = self.run_waiting("waits")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("waits: failed —", result.stdout)
                self.assertIn(expected, result.stdout)
                if message:
                    self.assertIn(message, result.stdout)
                self.assertNotIn("Traceback", result.stdout + result.stderr)

    def agenda_log(self, start: datetime, phase: str, *, eta: datetime | None = None,
                   wait: datetime | None = None, eta_identifier: str = ALPHA,
                   ) -> None:
        events = [(start, self.producer_launch_line(start))]
        if eta is not None:
            eta_moment = start + timedelta(minutes=10)
            events.append((eta_moment, self.producer_eta_line(eta_moment, phase, eta,
                                                               identifier=eta_identifier)))
        if wait is not None:
            events.append((wait, f"- {wait:%H:%M %Z}: block: {ALPHA} on {BETA} "
                                 + "(files: shared.txt), clears ~23:00"))
        lines = ["# Production log — example", *(line for _, line in sorted(events))]
        lines.append(self.producer_state_block(datetime.now(start.tzinfo), {ALPHA: phase}))
        self.write(self.log, "\n".join((*lines, "")))

    def test_agenda_opens_phase_started_nine_hours_ago_only_once(self) -> None:
        start = datetime.now(ZoneInfo("America/Los_Angeles")) - timedelta(hours=9)
        self.agenda_log(start, PHASE)
        first = self.run_waiting("agenda", "--state-dir", str(self.state))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn(f"{ALPHA}|{PHASE_IDENTITY}", first.stdout)
        seen = cast(dict[str, object], json.loads((self.state / "agenda_seen.json").read_text()))
        self.assertIn(f"{ALPHA}|{PHASE_IDENTITY}", seen)
        repeated = self.run_waiting("agenda", "--state-dir", str(self.state))
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertNotIn(f"{ALPHA}|{PHASE_IDENTITY}", repeated.stdout)

    def test_agenda_opens_when_eta_is_nine_hours_after_phase_start(self) -> None:
        start = datetime.now(ZoneInfo("America/Los_Angeles")) - timedelta(hours=1)
        self.agenda_log(start, PHASE, eta=start + timedelta(hours=9))
        result = self.run_waiting("agenda", "--state-dir", str(self.state))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"{ALPHA}|{PHASE_IDENTITY}", result.stdout)

    def test_agenda_uses_previous_merge_time_in_log_instead_of_git_commit_time(self) -> None:
        unit_tip = self.commit(self.alpha, "alpha.txt", "first phase\n")
        _ = self.git("merge", "--no-ff", "-m",
                     f"Merge {ALPHA} phase 1 ({unit_tip[:7]}) into production", "alpha-branch")
        merge_tip = self.git("rev-parse", "HEAD")
        old = datetime.now(ZoneInfo("America/Los_Angeles")) - timedelta(hours=9)
        self.write(self.log, "\n".join((
            "# Production log — example",
            self.producer_merge_line(old, unit_tip, merge_tip),
            self.producer_state_block(datetime.now(old.tzinfo), {ALPHA: PHASE}), "",
        )))
        result = self.run_waiting("agenda", "--state-dir", str(self.state))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"{ALPHA}|{PHASE_IDENTITY}", result.stdout)

    def test_agenda_ignores_long_wait_when_phase_itself_is_short(self) -> None:
        start = datetime.now(ZoneInfo("America/Los_Angeles")) - timedelta(hours=1)
        old_wait = start - timedelta(hours=9)
        self.agenda_log(start, PHASE, eta=start + timedelta(hours=2), wait=old_wait)
        result = self.run_waiting("agenda", "--state-dir", str(self.state))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(f"{ALPHA}|{PHASE_IDENTITY}", result.stdout)
        self.assertFalse((self.state / "agenda_seen.json").exists())

    def test_agenda_uses_only_the_newest_state_block(self) -> None:
        zone = ZoneInfo("America/Los_Angeles")
        now = datetime.now(zone)
        start = now - timedelta(hours=10)
        old_state = self.producer_state_block(start + timedelta(minutes=5), {ALPHA: "Phase 3 of 3: old work"})
        eta = self.producer_eta_line(start + timedelta(minutes=10), "Phase 3 of 3: old work",
                                     start + timedelta(hours=12))
        done_state = self.producer_state_block(now, {ALPHA: "done"})
        self.write(self.log, "\n".join(("# Production log — example",
                                        self.producer_launch_line(start), old_state, eta, done_state, "")))
        result = self.run_waiting("agenda", "--state-dir", str(self.state))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("agenda: ok — 0 new items", result.stdout)
        self.assertFalse((self.state / "agenda_seen.json").exists())

    def test_agenda_does_not_inherit_an_eta_from_the_previous_phase(self) -> None:
        zone = ZoneInfo("America/Los_Angeles")
        now = datetime.now(zone)
        launched = now - timedelta(hours=12)
        phase_started = now - timedelta(hours=1)
        unit_tip = self.commit(self.alpha, "alpha.txt", "first phase\n")
        _ = self.git("merge", "--no-ff", "-m",
                     f"Merge {ALPHA} phase 1 ({unit_tip[:7]}) into production", "alpha-branch")
        merge_tip = self.git("rev-parse", "HEAD")
        old_phase = "Phase 1 of 3: old work"
        lines = (
            "# Production log — example",
            self.producer_launch_line(launched),
            self.producer_state_block(launched + timedelta(minutes=5), {ALPHA: old_phase}),
            self.producer_eta_line(launched + timedelta(minutes=10), old_phase,
                                   launched + timedelta(hours=10)),
            self.producer_merge_line(phase_started, unit_tip, merge_tip),
            self.producer_state_block(now, {ALPHA: PHASE}),
            "",
        )
        self.write(self.log, "\n".join(lines))
        result = self.run_waiting("agenda", "--state-dir", str(self.state))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("agenda: ok — 0 new items", result.stdout)
        self.assertFalse((self.state / "agenda_seen.json").exists())

    def test_agenda_matches_eta_segments_by_unit_or_session_name(self) -> None:
        session = "alpha-session"
        self.alpha_session(session)
        zone = ZoneInfo("America/Los_Angeles")
        for identifier in (ALPHA, session):
            with self.subTest(identifier=identifier):
                _ = (self.state / "agenda_seen.json").unlink(missing_ok=True)
                start = datetime.now(zone) - timedelta(hours=1)
                # The saved report state names a unit by its id; an ETA segment may use either name.
                self.agenda_log(start, PHASE, eta=start + timedelta(hours=9), eta_identifier=identifier)
                result = self.run_waiting("agenda", "--state-dir", str(self.state))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"{ALPHA}|{PHASE_IDENTITY}", result.stdout)

    def test_held_unchanged_eta_from_yesterday_stays_out_of_the_agenda(self) -> None:
        zone = ZoneInfo("America/Los_Angeles")
        start = datetime(2026, 10, 6, 18, 50, tzinfo=zone)
        first_report = datetime(2026, 10, 6, 19, 30, tzinfo=zone)
        eta = datetime(2026, 10, 6, 19, 35, tzinfo=zone)
        now = datetime(2026, 10, 7, 0, 10, tzinfo=zone)
        held_report = self.producer_held_eta_line(now, PHASE, eta)
        lines = (
            "# Production log — example",
            self.producer_launch_line(start),
            self.producer_eta_line(first_report, PHASE, eta),
            held_report,
            self.producer_state_block(now, {ALPHA: PHASE}),
            "",
        )
        self.assertIn(f"{ALPHA} {PHASE_IDENTITY} Tue 19:35 PDT", held_report)
        self.write(self.log, "\n".join(lines))

        class AgendaClock:
            @staticmethod
            def now(requested_zone: ZoneInfo) -> datetime:
                return now.astimezone(requested_zone)

        output = io.StringIO()
        with mock.patch.object(waiting, "datetime", AgendaClock), redirect_stdout(output):
            waiting.agenda(add_unit.read_production(self.doc), self.state)
        self.assertIn("agenda: ok — 0 new items", output.getvalue())
        self.assertFalse((self.state / "agenda_seen.json").exists())

    def test_future_weekday_eta_from_renderer_resolves_after_the_log_line(self) -> None:
        zone = ZoneInfo("America/Los_Angeles")
        report_at = datetime(2026, 10, 6, 8, 0, tzinfo=zone)
        eta = datetime(2026, 10, 8, 9, 0, tzinfo=zone)
        line = self.producer_eta_line(report_at, PHASE, eta)
        prefix = f"{ALPHA} {PHASE_IDENTITY} "
        eta_text = line.split("dailies ETAs: ", 1)[1].removeprefix(prefix)

        self.assertTrue(eta_text.startswith("Thu 09:00 PDT"), line)
        self.assertEqual(waiting.eta_moment(eta_text, report_at), waiting.EtaOnLog(eta))

    def test_log_timeline_spans_two_midnights_for_waits_and_first_phase(self) -> None:
        now = datetime.now(ZoneInfo("America/Los_Angeles")).replace(second=0, microsecond=0)
        moments = [now - timedelta(hours=hours) for hours in (50, 40, 26, 14, 2)]
        start = moments[0]
        lines = [
            "# Production log — example",
            self.producer_launch_line(start),
            f"- {start:%H:%M %Z}: block: {ALPHA} on {BETA} (files: shared.txt), clears ~23:00",
            *(f"- {moment:%H:%M %Z}: tick marker {index}" for index, moment in enumerate(moments[1:], 1)),
            self.producer_state_block(now, {ALPHA: PHASE}),
            "",
        ]
        self.write(self.log, "\n".join(lines))
        output = io.StringIO()
        clock = mock.Mock(wraps=datetime, now=mock.Mock(return_value=now))
        with mock.patch.dict(os.environ, self.env), mock.patch.object(waiting, "datetime", clock), redirect_stdout(output):
            waiting.waits(add_unit.read_production(self.doc))
        found = re.search(r"open (\d+) minutes", output.getvalue())
        self.assertIsNotNone(found, output.getvalue())
        age = int(found.group(1)) if found else 0
        self.assertEqual(age, 3000)
        agenda = self.run_waiting("agenda", "--state-dir", str(self.state))
        self.assertEqual(agenda.returncode, 0, agenda.stdout + agenda.stderr)
        self.assertIn(f"{ALPHA}|{PHASE_IDENTITY}", agenda.stdout)

    def test_quota_tracks_full_account_stems_through_acknowledgment_and_restore(self) -> None:
        first_note, second_note = self.quota_note("codex1"), self.quota_note("codex2")
        for note in (first_note, second_note):
            result = self.run_waiting("quota", "--notice", self.quota_message(note),
                                      "--state-dir", str(self.state))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        seen_path = self.state / "quota_seen.json"
        held = cast(dict[str, object], json.loads(seen_path.read_text()))
        self.assertEqual(set(held), {"codex1", "codex2"})

        acknowledged = self.run_waiting("quota", "--notice", self.acknowledgement_message("codex1"),
                                        "--state-dir", str(self.state))
        self.assertEqual(acknowledged.returncode, 0, acknowledged.stdout + acknowledged.stderr)
        held = cast(dict[str, object], json.loads(seen_path.read_text()))
        self.assertEqual(set(held), {"codex2"})

        restored_notes: list[quota_alert.AgentNote] = [second_note]
        restored_notice = quota_alert.restored_message(restored_notes, "the codex2 alert is closed", 5.0,
                                                       selection=ONE_CLAUDE_ACCOUNT)
        restored = self.run_waiting("quota", "--notice", restored_notice,
                                    "--state-dir", str(self.state))
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        self.assertEqual(json.loads(seen_path.read_text()), {})

    def test_quota_prints_each_held_alert_with_percent_left_and_reset_time(self) -> None:
        first_note, second_note = self.quota_note("codex1"), self.quota_note("codex2")
        first = self.run_waiting("quota", "--notice", self.quota_message(first_note),
                                 "--state-dir", str(self.state))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        second = self.run_waiting("quota", "--notice", self.quota_message(second_note),
                                  "--state-dir", str(self.state))
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        held = [line for line in second.stdout.splitlines() if line.startswith("held: ")]
        self.assertEqual(len(held), 2, second.stdout)
        for account in ("codex1", "codex2"):
            line = next(item for item in held if account in item)
            self.assertIn("4%", line)
            self.assertIn("resets 2026-10-12 09:00 PDT", line)

    def test_canonical_ack_clears_a_legacy_keyed_hold_without_losing_other_accounts(self) -> None:
        path = self.state / "quota_seen.json"
        legacy = self.quota_message(self.quota_note("codex1")).replace("codex1", "codex 1")
        other = self.quota_message(self.quota_note("codex2"))
        _ = path.write_text(json.dumps({"codex 1": legacy.splitlines()[0], "codex 2": other.splitlines()[0]}))

        result = self.run_waiting("quota", "--notice", self.acknowledgement_message("codex1"),
                                  "--state-dir", str(self.state))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(path.read_text()), {"codex2": other.splitlines()[0]})
        self.assertNotIn("held: Quota alert: codex 1", result.stdout)

    def test_reading_legacy_holds_normalizes_identity_and_text_without_rewriting_the_file(self) -> None:
        path = self.state / "quota_seen.json"
        canonical = self.quota_message(self.quota_note("codex2")).splitlines()[0]
        legacy = canonical.replace("codex2", "codex 2")
        original = json.dumps({"codex 2": legacy}, indent=4) + "\n"
        _ = path.write_text(original)

        held = waiting.read_quota_state(path)

        self.assertEqual(held, {"codex2": canonical})
        self.assertEqual(path.read_bytes(), original.encode())

    def test_legacy_ack_notice_matches_a_canonical_held_account(self) -> None:
        path = self.state / "quota_seen.json"
        _ = path.write_text(json.dumps({"codex1": self.quota_message(self.quota_note("codex1")).splitlines()[0]}))
        notice = self.acknowledgement_message("codex1").replace("codex1", "codex 1")

        result = self.run_waiting("quota", "--notice", notice, "--state-dir", str(self.state))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(path.read_text()), {})

    def test_claude_quota_notices_are_not_relayed_or_saved_as_waits(self) -> None:
        for notice in ("Quota alert: claude3 (unit) at 1% weekly usage left.",
                       "Quota alert: claude 3 (unit) at 1% weekly usage left.",
                       "Quota restored: work on claude3 can resume."):
            with self.subTest(notice=notice):
                result = self.run_waiting("quota", "--notice", notice, "--state-dir", str(self.state))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("send ", result.stdout)
                self.assertFalse(self.sends.exists())
                seen = self.state / "quota_seen.json"
                if seen.exists():
                    self.assertEqual(json.loads(seen.read_text()), {})

    def test_legacy_claude_waits_are_dropped_while_codex_waits_remain(self) -> None:
        path = self.state / "quota_seen.json"
        codex = self.quota_message(self.quota_note("codex2")).splitlines()[0]
        for notice in ("Quota restored: work on claude3 can resume.",
                       self.quota_message(self.quota_note("codex1"))):
            with self.subTest(notice=notice.splitlines()[0]):
                _ = path.write_text(json.dumps({
                    "claude 3": "Quota alert: claude 3 (unit) at 1% weekly usage left.",
                    "claude2": "Quota alert: claude2, the active Claude account, has 1% weekly usage left.",
                    "codex2": codex,
                }))
                self.assertEqual(waiting.read_quota_state(path), {"codex2": codex})
                result = self.run_waiting("quota", "--notice", notice, "--state-dir", str(self.state))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                held = cast(dict[str, object], json.loads(path.read_text()))
                self.assertNotIn("claude2", held)
                self.assertNotIn("claude3", held)
                self.assertNotIn("claude 3", held)
                self.assertEqual(held["codex2"], codex)
                self.assertNotIn("held: Quota alert: claude", result.stdout)

    def test_quota_tells_units_to_keep_delegating_on_codex(self) -> None:
        codex = self.run_waiting("quota", "--notice", self.quota_message(self.quota_note("codex1")),
                                 "--state-dir", str(self.state))
        self.assertEqual(codex.returncode, 0, codex.stdout + codex.stderr)
        self.assertIn(f"send {ALPHA}: From the showrunner: keep delegating on Codex:", codex.stdout)
        self.assertIn("tell the showrunner at once if Codex refuses work for quota", codex.stdout)
        self.assertNotIn("start no new", codex.stdout)

    def test_quota_reaches_a_live_herdr_unit_by_its_claude_name(self) -> None:
        native_name = "native-director"
        tmux = cast(dict[str, object], json.loads((self.root / "tmux.json").read_text()))
        del tmux["$1"]
        self.write(self.root / "tmux.json", json.dumps(tmux))
        self.write(self.root / "herdr-responses.json", json.dumps({
            "--session default session list --json": CommandResponse(
                code=0,
                stdout=json.dumps(herdr.SessionListResult(sessions=[herdr.SessionEntry(
                    name="native", running=True, default=False,
                    session_dir="/fixture", socket_path="/fixture/socket",
                )])),
            ),
        }))

        def process(pid: int, parent: int, argv: list[str], **environment: str) -> None:
            directory = self.root / "proc" / str(pid)
            directory.mkdir(parents=True)
            _ = (directory / "stat").write_text(f"{pid} (fixture) S {parent} 0 0")
            _ = (directory / "cmdline").write_bytes(("\0".join(argv) + "\0").encode())
            _ = (directory / "environ").write_bytes(
                ("\0".join(f"{key}={value}" for key, value in environment.items()) + "\0").encode()
            )
            (directory / "cwd").symlink_to(self.alpha)

        process(991000, 0, ["herdr", "--session", "native", "server"])
        herdr_listener(self.root / "proc", 991000, "native", self.env)
        process(os.getpid(), 991000, ["claude"], HERDR_PANE_ID="w1:p1",
                SHOWRUNNER_UNIT="example", SHOWRUNNER_UNIT_ID=ALPHA)
        path = self.root / "sessions/1.json"
        record = cast(dict[str, object], json.loads(path.read_text()))
        record.update(name=native_name, tmux="")
        self.write(path, json.dumps(record))
        with mock.patch.dict(os.environ, self.env):
            marked = unit_lookup.marked_units("example")[ALPHA]
        self.assertEqual(marked.place, herdr.HerdrPlace("native", "w1:p1"))
        place = subprocess.run(
            [sys.executable, str(SCRIPT.with_name("unit_lookup.py")), "pane", "example", ALPHA],
            env=self.env, capture_output=True, text=True, check=False,
        )
        self.assertEqual((place.returncode, place.stdout), (0, "herdr:native:w1:p1\n"), place.stderr)

        notice = self.quota_message(self.quota_note("codex1"))
        result = self.run_waiting("quota", "--notice", notice, "--state-dir", str(self.state))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [cast(list[str], json.loads(line)) for line in self.sends.read_text().splitlines()]
        self.assertEqual([call[call.index("--to") + 1] for call in calls], [native_name, BETA])
        self.assertIn(f"send {native_name}: From the showrunner: keep delegating on Codex:", result.stdout)
        native_call = calls[0]
        text = native_call[native_call.index("--text") + 1]
        self.assertTrue(text.startswith("From the showrunner: keep delegating on Codex:"), text)
        self.assertIn("tell the showrunner at once if Codex refuses work for quota", text)


    def test_quota_queues_one_unit_collects_failure_and_saves_held_alert(self) -> None:
        self.add_gamma()
        send = self.bin_path / "send.py"
        self.write(send, SEND_OUTCOME_STUB.replace("__PYTHON__", sys.executable))
        send.chmod(0o755)
        notice = self.quota_message(self.quota_note("codex1"))
        result = self.run_waiting("quota", "--notice", notice, "--state-dir", str(self.state))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        calls = [cast(list[str], json.loads(line)) for line in self.sends.read_text().splitlines()]
        self.assertEqual([call[call.index("--to") + 1] for call in calls], [ALPHA, BETA, GAMMA])
        self.assertIn(f"send {ALPHA}:", result.stdout)
        self.assertIn(f"queued {BETA}:", result.stdout)
        self.assertIn(f"quota: failed — {GAMMA}:", result.stdout)
        held = cast(dict[str, object], json.loads((self.state / "quota_seen.json").read_text()))
        self.assertEqual(set(held), {"codex1"})

    def test_quota_reports_a_unit_with_no_claude_running_and_tells_the_others(self) -> None:
        (self.root / "sessions" / "2.json").unlink()
        notice = self.quota_message(self.quota_note("codex1"))
        result = self.run_waiting("quota", "--notice", notice, "--state-dir", str(self.state))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        calls = [cast(list[str], json.loads(line)) for line in self.sends.read_text().splitlines()]
        self.assertEqual([call[call.index("--to") + 1] for call in calls], [ALPHA])
        self.assertIn(f"quota: failed — {BETA}: no Claude is running in its session, so it was not told",
                      result.stdout)

    def test_agenda_reports_missing_merge_branch_without_traceback(self) -> None:
        self.write(self.doc, self.doc.read_text(encoding="utf-8").replace(
            "- **Merge branch:** `production`", "- **Merge branch:** `missing-merge-branch`"))
        result = self.run_waiting("agenda", "--state-dir", str(self.state))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("agenda: failed —", result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_showrunner_and_dailies_use_one_state_directory_for_waiting_records(self) -> None:
        root = SCRIPT.parents[2]
        produce = (root / "commands/showrunner/produce.md").read_text(encoding="utf-8")
        dailies = (root / "commands/showrunner/dailies.md").read_text(encoding="utf-8")
        self.assertIn("`DAILIES_STATE_DIR` — `<SCRATCH>/dailies_input_state`", produce)
        calls = cast(list[str], re.findall(r"`([^`]*scripts/production/waiting\.py[^`]*)`", produce))
        for action in ("scratch-test", "agenda", "eta-request"):
            matches = [call for call in calls if f"waiting.py {action}" in call]
            self.assertTrue(matches, action)
            self.assertTrue(all("--state-dir DAILIES_STATE_DIR" in call for call in matches), action)
        builder = re.search(r"dailies_input\.py.*?--state-dir ([^\s\\]+)", dailies, re.S)
        self.assertIsNotNone(builder)
        self.assertEqual(builder.group(1) if builder else "", "<scratchpad>/dailies_input_state")

    def test_showrunner_says_an_unmeasured_eta_request_cannot_repeat(self) -> None:
        produce = (SCRIPT.parents[2] / "commands/showrunner/produce.md").read_text(encoding="utf-8")
        unmeasured = produce.split("**Unmeasured ETAs.**", 1)[1].split("- **Waiting on block.**", 1)[0]
        self.assertNotIn("Ask again only if", unmeasured)
        self.assertIn("the command will not repeat the request", unmeasured)

    def test_quota_alert_and_agenda_contracts_keep_every_user_surface(self) -> None:
        root = SCRIPT.parents[2]
        produce = (root / "commands/showrunner/produce.md").read_text(encoding="utf-8")
        dailies = (root / "commands/showrunner/dailies.md").read_text(encoding="utf-8")
        quota = produce.split("<QuotaAlert>", 1)[1].split("</QuotaAlert>", 1)[0]
        agenda = produce.split("<Agenda>", 1)[1].split("</Agenda>", 1)[0]
        agenda_words = " ".join(agenda.split())
        open_topics = dailies.split("5. **Open topics.**", 1)[1].split("6. **Review watch.**", 1)[0]

        self.assertIn("listed first in every Waiting on block", " ".join(quota.split()))
        self.assertIn("led by `quota: `", " ".join(quota.split()))
        nested = " ".join(produce.split("- **Nested showrunners.**", 1)[1].split("- **What reaches the user:**", 1)[0].split())
        # A lower showrunner's item lives only in its owner's list, so clearing it there is all it takes.
        self.assertIn("never copy a lower showrunner's item into your own `open_for_user`", nested)
        self.assertIn("The root's count drops by itself: send the root nothing to drop it.", nested)
        self.assertIn("held quota alerts", open_topics)
        self.assertIn("`- HH:MM <zone>: agenda: <item>`", agenda_words)
        self.assertEqual(agenda_words.count("`For discussion with you`"), 1)
        self.assertIn("`- HH:MM <zone>: agenda closed: <item>: <what was decided>`", agenda_words)


if __name__ == "__main__":
    _ = unittest.main()
