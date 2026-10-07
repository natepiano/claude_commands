"""Exercise waiting decisions in disposable production repositories."""

from __future__ import annotations

import fcntl
import io
import json
import os
import re
import shlex
import shutil
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

import add_unit
import dailies_render
import merge_checkpoint
import update_registration
import waiting

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "whoami"))
import quota_alert


SCRIPT = Path(__file__).with_name("waiting.py")
CI_POINTS = SCRIPT.with_name("ci_points.py")
ALPHA = "alpha-unit"
BETA = "beta-unit"
GAMMA = "gamma-unit"
PHASE = "Phase 2 of 3: panel labels stay clear"
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


@final
class QuotaNote:
    path: Path
    tool: str
    values: dict[str, str]

    def __init__(self, path: Path, tool: str, values: dict[str, str]) -> None:
        self.path = path
        self.tool = tool
        self.values = values

    def get(self, key: str) -> str | None:
        return self.values.get(key)


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

    def production_doc(self) -> None:
        self.write(self.doc, "\n".join((
            "# Production — example", "", "> **Status: PRODUCTION — running.** Example.",
            "", "## Production Context", "", "- **Merge branch:** `production`",
            f"- **Showrunner checkout:** `{self.checkout}`", "- **Showrunner session:** showrunner-example",
            "- **Log:** `production.log`", "- **User zone:** America/Los_Angeles",
            "- **Updates:** every 15 minutes", "", "## Units", "",
            "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            f"| `{ALPHA}` | `docs/alpha.md` | `{self.alpha}` | `alpha-branch` | `{ALPHA}` | — | `shared.txt` |",
            f"| `{BETA}` | `docs/beta.md` | `{self.beta}` | `beta-branch` | `{BETA}` | — | `src/beta.py` |",
            "", "## Gates", "", "| Gate | Waiting | Waits on | Clears when |",
            "| --- | --- | --- | --- |",
            f"| G1 | {ALPHA} phase 2 | {BETA} phase 1 | beta code merged |",
            "", "## Production rules", "", "- Merge each phase and push the merge branch.", "",
        )))

    def add_gamma(self) -> None:
        content = self.doc.read_text(encoding="utf-8")
        row = f"| `{GAMMA}` | `docs/gamma.md` | `{self.root / 'gamma'}` | `gamma-branch` | `{GAMMA}` | — | — |\n"
        self.write(self.doc, content.replace("\n\n## Gates", f"\n{row}\n## Gates"))

    def alpha_session(self, session: str) -> None:
        content = self.doc.read_text(encoding="utf-8")
        old = f"| `alpha-branch` | `{ALPHA}` |"
        self.assertIn(old, content)
        self.write(self.doc, content.replace(old, f"| `alpha-branch` | `{session}` |"))

    def run_waiting(self, action: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), action, *args,
                               "--production", str(self.doc)], cwd=self.checkout, env=self.env,
                              capture_output=True, text=True, check=False, timeout=25)

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
            timeout=1.0,
        )
        with (mock.patch.object(add_unit, "registry_has_unit", return_value=True),
              mock.patch.object(add_unit, "update_old_prompt"), redirect_stdout(io.StringIO())):
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

    def quota_note(self, name: str, tool: str = "codex") -> QuotaNote:
        return QuotaNote(self.root / f"{name}.md", tool, {
            "state": "active",
            "weekly_remaining_usage": "4",
            "resets": "2026-10-12 09:00 PDT",
            "weekly_usage_checked_at": "2026-10-07 02:00 PDT",
        })

    def quota_message(self, note: QuotaNote) -> str:
        config: quota_alert.Config = {
            "threshold_percent": 5.0,
            "repeat_minutes": 30.0,
            "stall_minutes": 5.0,
            "faults_to": "showrunner-example",
            "always": [],
            "showrunners": [],
        }
        return quota_alert.message(note, [], config)

    def acknowledgement_message(self, account: str) -> str:
        state_path = self.root / "quota-alert-state.json"
        _ = state_path.write_text(json.dumps({
            "episodes": {
                "codex 1": {"since": "2026-10-07T01:00:00+00:00", "last": {}},
                "codex 2": {"since": "2026-10-07T01:00:00+00:00", "last": {}},
            },
        }) + "\n", encoding="utf-8")
        notices: list[str] = []

        def capture(text: str, here: str | None) -> list[str]:
            del here
            notices.append(text)
            return []

        with (mock.patch.object(quota_alert, "STATE", state_path),
              mock.patch.object(quota_alert, "tell_others", side_effect=capture)):
            _ = quota_alert.acknowledge(account, here="showrunner-example",
                                        now=datetime(2026, 10, 7, 6, 0, tzinfo=ZoneInfo("UTC")))
        self.assertEqual(len(notices), 1)
        return notices[0]

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
        outputs = [process.communicate(timeout=25) for process in processes]
        for process, (stdout, stderr) in zip(processes, outputs):
            self.assertEqual(process.returncode, 0, stdout + stderr)
        seen = cast(dict[str, object], json.loads((self.state / "eta_seen.json").read_text()))
        self.assertEqual(set(seen), {f"{ALPHA}|{phase}" for phase in phases})
        self.assertFalse([path for path in self.state.iterdir() if path.suffix == ".tmp"])

    def test_parallel_eta_requests_for_one_phase_print_once(self) -> None:
        processes = [self.eta_request_process(PHASE) for _ in range(12)]
        outputs = [process.communicate(timeout=25) for process in processes]
        for process, (stdout, stderr) in zip(processes, outputs):
            self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(sum(stdout.count(f"request /unit:eta: {ALPHA}")
                             for stdout, _ in outputs), 1)
        seen = cast(dict[str, object], json.loads((self.state / "eta_seen.json").read_text()))
        self.assertEqual(seen, {f"{ALPHA}|{PHASE}": {"requested": True}})

    def test_eta_request_waits_for_its_state_lock(self) -> None:
        lock_path = self.state / "eta_seen.json.lock"
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            process = self.eta_request_process(PHASE)
            with self.assertRaises(subprocess.TimeoutExpired):
                _ = process.wait(timeout=0.5)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            stdout, stderr = process.communicate(timeout=25)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertIn(f"request /unit:eta: {ALPHA}", stdout)

    def test_waits_accepts_unconfigured_berth_and_still_reports_log_waits(self) -> None:
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
        opened = now - timedelta(minutes=72)
        cleared = now - timedelta(minutes=50)
        self.write(self.log, "\n".join((
            "# Production log — example",
            f"- {opened:%H:%M %Z}: block: {ALPHA} on {BETA} (files: shared.txt), clears ~{(opened + timedelta(minutes=20)):%H:%M}",
            f"- {cleared:%H:%M %Z}: block: {BETA} on {ALPHA} (code: API), clears ~{(cleared + timedelta(minutes=20)):%H:%M}",
            f"- {(cleared + timedelta(minutes=5)):%H:%M %Z}: block cleared: {BETA} on {ALPHA}", "",
        )))
        self.unconfigured_board()
        result = self.run_waiting("waits")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("waits: ok — 1 open; berth not configured", result.stdout)
        self.assertIn(f"{ALPHA} on {BETA}", result.stdout)
        self.assertRegex(result.stdout, r"open 7[12] minutes")
        self.assertNotIn(f"{BETA} on {ALPHA}: open", result.stdout)

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
                   state_session: str = ALPHA) -> None:
        events = [(start, self.producer_launch_line(start, session=state_session))]
        if eta is not None:
            eta_moment = start + timedelta(minutes=10)
            events.append((eta_moment, self.producer_eta_line(eta_moment, phase, eta,
                                                               identifier=eta_identifier)))
        if wait is not None:
            events.append((wait, f"- {wait:%H:%M %Z}: block: {ALPHA} on {BETA} "
                                 + "(files: shared.txt), clears ~23:00"))
        lines = ["# Production log — example", *(line for _, line in sorted(events))]
        lines.append(self.producer_state_block(datetime.now(start.tzinfo), {state_session: phase}))
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
                self.agenda_log(start, PHASE, eta=start + timedelta(hours=9),
                                eta_identifier=identifier, state_session=session)
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
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
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
        waits = self.run_waiting("waits")
        self.assertEqual(waits.returncode, 0, waits.stdout + waits.stderr)
        found = re.search(r"open (\d+) minutes", waits.stdout)
        self.assertIsNotNone(found, waits.stdout)
        age = int(found.group(1)) if found else 0
        self.assertLessEqual(abs(age - 3000), 2)
        agenda = self.run_waiting("agenda", "--state-dir", str(self.state))
        self.assertEqual(agenda.returncode, 0, agenda.stdout + agenda.stderr)
        self.assertIn(f"{ALPHA}|{PHASE_IDENTITY}", agenda.stdout)

    def test_quota_tracks_full_account_stems_through_acknowledgment_and_restore(self) -> None:
        first_note, second_note = self.quota_note("codex 1"), self.quota_note("codex 2")
        for note in (first_note, second_note):
            result = self.run_waiting("quota", "--notice", self.quota_message(note),
                                      "--state-dir", str(self.state))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        seen_path = self.state / "quota_seen.json"
        held = cast(dict[str, object], json.loads(seen_path.read_text()))
        self.assertEqual(set(held), {"codex 1", "codex 2"})

        acknowledged = self.run_waiting("quota", "--notice", self.acknowledgement_message("codex 1"),
                                        "--state-dir", str(self.state))
        self.assertEqual(acknowledged.returncode, 0, acknowledged.stdout + acknowledged.stderr)
        held = cast(dict[str, object], json.loads(seen_path.read_text()))
        self.assertEqual(set(held), {"codex 2"})

        restored_notes: list[quota_alert.AgentNote] = [second_note]
        restored_notice = quota_alert.restored_message(restored_notes, "the codex 2 alert is closed", 5.0)
        restored = self.run_waiting("quota", "--notice", restored_notice,
                                    "--state-dir", str(self.state))
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        self.assertEqual(json.loads(seen_path.read_text()), {})

    def test_quota_prints_each_held_alert_with_percent_left_and_reset_time(self) -> None:
        first_note, second_note = self.quota_note("codex 1"), self.quota_note("codex 2")
        first = self.run_waiting("quota", "--notice", self.quota_message(first_note),
                                 "--state-dir", str(self.state))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        second = self.run_waiting("quota", "--notice", self.quota_message(second_note),
                                  "--state-dir", str(self.state))
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        held = [line for line in second.stdout.splitlines() if line.startswith("held: ")]
        self.assertEqual(len(held), 2, second.stdout)
        for account in ("codex 1", "codex 2"):
            line = next(item for item in held if account in item)
            self.assertIn("4%", line)
            self.assertIn("resets 2026-10-12 09:00 PDT", line)

    def test_quota_queues_one_unit_collects_failure_and_saves_held_alert(self) -> None:
        self.add_gamma()
        send = self.bin_path / "send.py"
        self.write(send, SEND_OUTCOME_STUB.replace("__PYTHON__", sys.executable))
        send.chmod(0o755)
        notice = self.quota_message(self.quota_note("codex 1"))
        result = self.run_waiting("quota", "--notice", notice, "--state-dir", str(self.state))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        calls = [cast(list[str], json.loads(line)) for line in self.sends.read_text().splitlines()]
        self.assertEqual([call[call.index("--to") + 1] for call in calls], [ALPHA, BETA, GAMMA])
        self.assertIn(f"send {ALPHA}:", result.stdout)
        self.assertIn(f"queued {BETA}:", result.stdout)
        self.assertIn(f"quota: failed — {GAMMA}:", result.stdout)
        held = cast(dict[str, object], json.loads((self.state / "quota_seen.json").read_text()))
        self.assertEqual(set(held), {"codex 1"})

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
        self.assertIn("held quota alerts", open_topics)
        self.assertIn("`- HH:MM <zone>: agenda: <item>`", agenda_words)
        self.assertEqual(agenda_words.count("`For discussion with you`"), 1)
        self.assertIn("`- HH:MM <zone>: agenda closed: <item>: <what was decided>`", agenda_words)


if __name__ == "__main__":
    _ = unittest.main()
