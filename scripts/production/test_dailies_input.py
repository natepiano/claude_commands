"""Build dailies input from disposable production state and status output."""

from __future__ import annotations

import json
import runpy
from collections.abc import Sequence
from types import ModuleType
import dailies_input
import fake_unit_host
import fake_herdr
import dailies_render
from cli_fixture import ImmutableGitSeed, invoke_cli
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar, cast, override
from unittest import mock
from zoneinfo import ZoneInfo

from add_unit import read_production
from dailies_render import LENGTHS
from dailies_input import (Activity, BevyUpgradeUnavailable, Block, ClaudeNotRunning, Decision, FormWaiting,
                           Running, SessionGone, StillWaiting, TicksFailing, UnitRow,
                           bevy_upgrade_watch, status_blocks)
import unit_lookup
from unit_lookup import UnitState


# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300

SCRIPT = Path(__file__).with_name("dailies_input.py")
RENDERER = SCRIPT.with_name("dailies_render.py")
CI_POINTS = SCRIPT.with_name("ci_points.py")
WAITING = SCRIPT.with_name("waiting.py")
STAMP = "at 10:00 PDT / 17:00 UTC"
AT = "2026-10-06T17:00"
ALPHA = "alpha-unit"
BETA = "beta-unit"
NOTIFIER = '''#!__PYTHON__
import json
import os
from pathlib import Path
import sys

record = Path(os.environ["DAILIES_TEST_EVENTS"])
with record.open("a", encoding="utf-8") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
if Path(os.environ["DAILIES_TEST_FAIL"]).exists():
    raise SystemExit(1)
print(os.environ.get("DAILIES_TEST_NEXT_DUE", "next_due=1791334500 (2026-10-06 17:55 PDT)"))
'''
WATCH_STUB = '''#!__PYTHON__
import json
import os
from pathlib import Path
import sys

events = Path(os.environ["DAILIES_WATCH_EVENTS"])
with events.open("a", encoding="utf-8") as output:
    output.write(json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]]) + "\\n")
if Path(sys.argv[0]).name == "send.py":
    raise SystemExit(0)
if sys.argv[1:2] == ["watch"]:
    print("12 of 12 phases: report ready, waiting for your acknowledgment")
    raise SystemExit(3)
if sys.argv[1:2] == ["report"]:
    print("| phases merged | 12 |")
    raise SystemExit(0)
raise SystemExit(99)
'''
RACE_WATCH_STUB = '''#!__PYTHON__
import os
import subprocess
import sys

if sys.argv[1:2] != ["watch"]:
    raise SystemExit(99)
request = subprocess.run([
    sys.executable,
    os.environ["DAILIES_RACE_WAITING"],
    "eta-request",
    os.environ["DAILIES_RACE_UNIT"],
    "--phase",
    os.environ["DAILIES_RACE_PHASE"],
    "--production",
    os.environ["DAILIES_RACE_PRODUCTION"],
    "--state-dir",
    os.environ["DAILIES_RACE_STATE"],
], capture_output=True, text=True, check=False)
if request.returncode:
    print(request.stdout + request.stderr, file=sys.stderr)
raise SystemExit(request.returncode)
print("review threshold not reached")
'''
PHASE_TABLE_STUB = '''#!__PYTHON__
import json
import os
from pathlib import Path
import sys

events = Path(os.environ["DAILIES_PHASE_TABLE_EVENTS"])
with events.open("a", encoding="utf-8") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
command = sys.argv[1] if len(sys.argv) > 1 else ""
failure = os.environ.get("DAILIES_PHASE_TABLE_FAIL", "")
if command == failure:
    print(f"{command} could not read records", file=sys.stderr)
    raise SystemExit(1)
if command == "show":
    print(Path(os.environ["DAILIES_PHASE_TABLE_JSON"]).read_text(encoding="utf-8"))
    raise SystemExit(0)
raise SystemExit(2)
'''
BEVY_UPGRADE_STUB = '''
import json
import os
from pathlib import Path
import sys

Path(os.environ["DAILIES_BEVY_EVENTS"]).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
print(os.environ.get("DAILIES_BEVY_STDERR", ""), file=sys.stderr, flush=True)
print(os.environ.get("DAILIES_BEVY_OUTPUT", ""))
raise SystemExit(int(os.environ.get("DAILIES_BEVY_EXIT", "0")))
'''


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


class DailiesInputTests(unittest.TestCase):
    seed: ClassVar[ImmutableGitSeed]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        root = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix="DailiesInputTests-seed-")))
        cls.seed = ImmutableGitSeed.create(root, 'production', 'Dailies Test', 'dailies@example.invalid', {'README.md': 'base\n'})

    root: Path = Path()
    home: Path = Path()
    origin: Path = Path()
    checkout: Path = Path()
    doc: Path = Path()
    status: Path = Path()
    judgment: Path = Path()
    state: Path = Path()
    holders: Path = Path()
    output: Path = Path()
    notifier: Path = Path()
    events: Path = Path()
    notifier_failure: Path = Path()
    phase_table: Path = Path()
    phase_table_json: Path = Path()
    phase_table_events: Path = Path()
    showrunner_state: Path = Path()
    env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.origin = self.root / "origin.git"
        self.checkout = self.root / "merge"
        self.seed.copy_to(self.checkout, self.origin)
        self.doc = self.root / "example-production.md"
        self.status = self.root / "status.txt"
        self.judgment = self.root / "judgment.json"
        self.state = self.root / "state"
        self.state.mkdir()
        self.holders = self.root / "holders"
        self.holders.mkdir()
        self.output = self.root / "dailies_input.json"
        self.notifier = self.root / "notifier"
        self.events = self.root / "notifier-events.jsonl"
        self.notifier_failure = self.root / "notifier-fail"
        self.phase_table = self.root / "phase-table.py"
        self.phase_table_json = self.root / "phase-table.json"
        self.phase_table_events = self.root / "phase-table-events.jsonl"
        _ = self.notifier.write_text(NOTIFIER.replace("__PYTHON__", sys.executable), encoding="utf-8")
        self.notifier.chmod(0o755)
        _ = self.phase_table.write_text(PHASE_TABLE_STUB.replace("__PYTHON__", sys.executable), encoding="utf-8")
        self.phase_table.chmod(0o755)
        _ = self.phase_table_json.write_text("{}\n", encoding="utf-8")
        self.showrunner_state = self.root / "showrunner-state"
        upgrade = self.root / "bevy upgrade stub.py"
        _ = upgrade.write_text(BEVY_UPGRADE_STUB, encoding="utf-8")
        self.env = {**os.environ, "HOME": str(self.home), "BUILD_HOLD_DIR": str(self.holders),
                    "DAILIES_BEVY_UPGRADE": str(upgrade),
                    "DAILIES_BEVY_EVENTS": str(self.root / "bevy-events.json"),
                    "DAILIES_BEVY_OUTPUT": "", "DAILIES_BEVY_STDERR": "",
                    "DAILIES_BEVY_EXIT": "0",
                    "MAC_TEST_STATE_DIR": str(self.root / "mac-test"),
                    "DAILIES_TEST_EVENTS": str(self.events), "DAILIES_TEST_FAIL": str(self.notifier_failure),
                    "DAILIES_PHASE_TABLE": str(self.phase_table),
                    "DAILIES_PHASE_TABLE_JSON": str(self.phase_table_json),
                    "DAILIES_PHASE_TABLE_EVENTS": str(self.phase_table_events),
                    "PHASE_TABLE_VAULT": str(self.root / "vault"),
                    "UNIT_HISTORY_DIR": str(self.root / "history"),
                    "NOTIFIER_STATE_DIR": str(self.root / "notifier-state"),
                    "SHOWRUNNER_STATE_DIR": str(self.showrunner_state),
                    "STYLE_LEDGER_STATE_DIR": str(self.root / "style-state"),
                    "STYLE_HISTORY_NATE_STYLE_DIR": str(self.root / "nate_style"),
                    **fake_unit_host.environment(self.root),
                    "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions")}
        (self.root / "sessions").mkdir()
        self.production_doc()
        self.status_lines(f"== {ALPHA}", "● Checking panel labels", "ETA 23:40")
        self.judgment_file()
        real_run = subprocess.run

        def run(argv: Sequence[str], *, capture_output: bool, text: bool, check: bool,
                timeout: float | None = None, env: dict[str, str] | None = None,
                cwd: str | Path | None = None) -> subprocess.CompletedProcess[str]:
            environment = dict(os.environ) if env is None else env
            directory = Path.cwd() if cwd is None else Path(cwd)
            if argv[0] == self.env["HERDR_BIN"]:
                self.assertTrue(capture_output and text)
                self.assertFalse(check)
                self.assertIsNotNone(env, "herdr must supply its sanitized child environment")
                return invoke_cli(fake_herdr, lambda: fake_herdr.main(list(argv[1:])),
                                  list(argv[1:]), environment, directory)
            script = Path(argv[1] if argv[0] == sys.executable and len(argv) > 1 else argv[0])
            arguments = list(argv[2:] if argv[0] == sys.executable else argv[1:])
            # Execute the actual fixture scripts; their logs, mutations, status
            # and nested commands remain the evidence consumed by the tests.
            fixtures = {self.notifier, self.phase_table, self.root / "bevy upgrade stub.py",
                        self.root / "bin/review_regime.py", self.root / "race-review.py",
                        self.home / ".claude/scripts/message/send.py"}
            if script.exists() and (script in fixtures or script == SCRIPT.with_name("review_regime.py")):
                module = ModuleType("dailies_fixture_command")
                module.__file__ = str(script)

                def main() -> int:
                    _ = runpy.run_path(str(script), run_name="__main__")
                    return 0

                return invoke_cli(module, main, arguments, environment, directory)
            return real_run(argv, capture_output=capture_output, text=text, check=check,
                            timeout=timeout, env=env, cwd=cwd)

        _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=run))

    def git(self, *arguments: str) -> str:
        result = subprocess.run(["git", "-C", str(self.checkout), *arguments],
                                check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def production_doc(self, *, beta: bool = False) -> None:
        for name in ("alpha", "beta"):
            # A linked worktree, where `.git` is a file: its unit is never taken for a removed one.
            (self.root / name).mkdir(exist_ok=True)
            _ = (self.root / name / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        rows = [
            "# Production — example", "", "## Production Context", "",
            "- **Merge branch:** `production`",
            f"- **Showrunner checkout:** `{self.checkout}`",
            "- **Showrunner session:** test-showrunner",
            "- **Log:** `production.log`",
            "- **User zone:** America/Los_Angeles",
            "- **Updates:** every 15 minutes", "",
            "## Units", "",
            "| Unit | Plan | Worktree | Branch | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- |",
            f"| `{ALPHA}` | `docs/alpha.md` | `{self.root / 'alpha'}` | `alpha` | — | — |",
        ]
        if beta:
            rows.append(f"| `{BETA}` | `docs/beta.md` | `{self.root / 'beta'}` | `beta` | — | — |")
        rows.extend(["", "## Gates", ""])
        _ = self.doc.write_text("\n".join(rows), encoding="utf-8")

    def status_lines(self, *lines: str) -> None:
        _ = self.status.write_text("\n".join((STAMP, *lines)) + "\n", encoding="utf-8")

    def judgment_file(self, *, beta: bool = False, alpha: dict[str, object] | None = None) -> None:
        base: dict[str, object] = {
            "unit": ALPHA,
            "project": "panels that stay readable",
            "phase": "Phase 2 of 3: panel labels stay clear",
            "started": "2026-10-06T09:00",
            "held": None,
            "update": "checking the panel labels",
            "eta": {"percent": 60},
            "then": ["Phase 3: panel edges align"],
        }
        if alpha:
            base.update(alpha)
        units: list[dict[str, object]] = [base]
        if beta:
            units.append({
                "unit": BETA,
                "project": "buttons that stay readable",
                "phase": "Phase 1 of 2: button labels stay clear",
                "started": "2026-10-06T09:00",
                "held": None,
                "update": "checking the button labels",
                "then": ["Phase 2: button edges align"],
            })
        _ = self.judgment.write_text(json.dumps({"units": units, "topics": []}) + "\n", encoding="utf-8")

    def run_builder(self, *extra: str, include_render_state: bool = True, subprocess_cli: bool = False) -> subprocess.CompletedProcess[str]:
        state_args = (
            ("--render-state", str(self.root / "render-state.json"))
            if include_render_state and "--render-state" not in extra else ()
        )
        arguments = ["--production", str(self.doc),
             "--status", str(self.status), "--judgment", str(self.judgment),
             "--state-dir", str(self.state), "--out", str(self.output),
             "--holders", str(self.holders), "--notifier", str(self.notifier),
             "--at", AT, *state_args, *extra]
        if subprocess_cli:
            return subprocess.run([sys.executable, str(SCRIPT), *arguments], cwd=self.checkout,
                                  env=self.env, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        return invoke_cli(dailies_input, dailies_input.main, arguments, self.env, self.checkout, production_state=True)

    def report(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.output.read_text(encoding="utf-8")))

    def run_renderer(self, at: str) -> subprocess.CompletedProcess[str]:
        arguments = [str(self.output), "--at", at, "--state", str(self.root / "render-state.json"),
                     "--log", str(self.root / "dailies.log")]
        return invoke_cli(dailies_render, lambda: dailies_render.main(arguments), arguments,
                          self.env, self.checkout, production_state=True)

    def unit(self) -> dict[str, object]:
        return cast(dict[str, object], cast(list[object], self.report()["units"])[0])

    def notifier_events(self) -> list[list[str]]:
        if not self.events.exists():
            return []
        return [cast(list[str], json.loads(line)) for line in self.events.read_text(encoding="utf-8").splitlines()]

    def phase_table_calls(self) -> list[list[str]]:
        if not self.phase_table_events.exists():
            return []
        return [cast(list[str], json.loads(line))
                for line in self.phase_table_events.read_text(encoding="utf-8").splitlines()]

    def open_phase_record(
        self,
        *,
        phase: str = "2",
        started: str = "2026-10-06T15:30:00+00:00",
        percent: int | None = 60,
        eta: dict[str, object] | None = None,
        has_eta: bool = True,
        first: str | None = "2026-10-07T00:30:00+00:00",
        repair_rounds: int | None = 0,
        status: str = "running",
    ) -> None:
        current_eta: dict[str, object] | None = eta if eta is not None else {
            "time": "2026-10-07T00:40:00+00:00",
            "earliest": "2026-10-07T00:35:00+00:00",
            "latest": "2026-10-07T00:55:00+00:00",
            "source": "stated",
            "stated_at": "2026-10-06T23:45:00+00:00",
            "basis": "the checks are nearly done",
            "as_of": None,
        }
        if not has_eta:
            current_eta = None
        record: dict[str, object] = {
            "plan": str(self.root / "alpha/docs/alpha.md"),
            "updated": "2026-10-06T23:45:00+00:00",
            "plan_finish": "2026-10-07T01:00:00+00:00",
            "current": {
                "phase": phase,
                "of": 3,
                "title": "panel labels stay clear",
                "started": started,
                "percent": percent,
                "eta": current_eta,
                "first_stated_eta_target": first,
                "repair_rounds_since_first_stated_eta": (
                    repair_rounds if first is not None else None
                ),
            },
            "phases": [{
                "phase": phase,
                "title": "panel labels stay clear",
                "status": status,
                "start": started,
                "finish": None,
                "seconds": None,
            }],
        }
        _ = self.phase_table_json.write_text(json.dumps({ALPHA: record}) + "\n", encoding="utf-8")

    def watch_stubs(self) -> Path:
        bin_path = self.root / "bin"
        bin_path.mkdir()
        events = self.root / "watch-events.jsonl"
        for name in ("review_regime.py",):
            stub = bin_path / name
            _ = stub.write_text(WATCH_STUB.replace("__PYTHON__", sys.executable), encoding="utf-8")
            stub.chmod(0o755)
        send = self.home / ".claude/scripts/message/send.py"
        send.parent.mkdir(parents=True, exist_ok=True)
        _ = send.write_text(WATCH_STUB.replace("__PYTHON__", sys.executable), encoding="utf-8")
        send.chmod(0o755)
        self.env["DAILIES_WATCH_EVENTS"] = str(events)
        self.env["DAILIES_REVIEW_REGIME"] = str(bin_path / "review_regime.py")
        self.env["CI_POINTS_REVIEW_REGIME"] = str(bin_path / "review_regime.py")
        self.env["PATH"] = str(bin_path) + os.pathsep + os.environ.get("PATH", "")
        return events

    def watch_events(self, events: Path) -> list[list[str]]:
        return ([cast(list[str], json.loads(line)) for line in events.read_text().splitlines()]
                if events.exists() else [])

    def run_merge_watch(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(CI_POINTS), "watch", "--production", str(self.doc),
                               "--state-dir", str(self.state)], cwd=self.checkout, env=self.env,
                              capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

    def run_eta_request(self, phase: str = "Phase 2 of 3: panel labels stay clear") -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(WAITING), "eta-request", ALPHA,
                               "--phase", phase, "--production", str(self.doc),
                               "--state-dir", str(self.state)], cwd=self.checkout, env=self.env,
                              capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

    def rename_unit(self, name: str) -> None:
        for path in (self.doc, self.status, self.judgment):
            _ = path.write_text(path.read_text(encoding="utf-8").replace(ALPHA, name), encoding="utf-8")

    def parsed_block(self) -> tuple[Running | SessionGone | ClaudeNotRunning,
                                    tuple[FormWaiting | Decision | StillWaiting | Block | TicksFailing, ...],
                                    tuple[Activity, ...]]:
        block = status_blocks(self.status, (UnitRow(ALPHA, ALPHA, UnitState.RUNNING),))[0]
        return block.state, block.flags, block.activity

    def assert_refused_without_output(self, *extra: str, mention: str) -> str:
        _ = self.output.write_text("sentinel\n", encoding="utf-8")
        result = self.run_builder(*extra)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(mention, result.stdout + result.stderr)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "sentinel\n")
        return result.stdout + result.stderr

    def finished_run(self, worktree: Path) -> None:
        """Record a /unit:direct run in `worktree` that has finished, as the recorder would."""
        runs = self.root / "history/runs"
        runs.mkdir(parents=True, exist_ok=True)
        events: list[dict[str, object]] = [
            {"event_type": "run_started", "working_dir": str(worktree.resolve()), "run_started_at": 1.0},
            {"event_type": "run_finished"}]
        _ = (runs / f"{worktree.name}.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        self.env["UNIT_HISTORY_DIR"] = str(self.root / "history")

    def test_a_unit_whose_run_is_finished_needs_no_entry_and_is_left_out(self) -> None:
        self.production_doc(beta=True)
        self.status_lines(f"== {ALPHA}", "● Checking panel labels", f"== {BETA}", "● Standing by")
        self.judgment_file()
        self.finished_run(self.root / "beta")
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        units = cast(list[dict[str, object]], self.report()["units"])
        self.assertEqual([unit["unit"] for unit in units], [ALPHA])
        self.assertIn(f"left out, run finished and nothing waiting on the user: {BETA}", result.stdout)

    def test_units_from_doc_reads_run_records_once_for_several_units(self) -> None:
        self.production_doc(beta=True)
        self.finished_run(self.root / "beta")
        with (mock.patch.dict(os.environ, self.env),
              mock.patch.object(unit_lookup, "marked_units", return_value={}),
              mock.patch.object(unit_lookup, "read_run_states", wraps=unit_lookup.read_run_states) as reads):
            units = dailies_input.units_from_doc(self.doc.read_text(encoding="utf-8").splitlines(), "fixture")
            reads.assert_called_once_with()
        self.assertEqual([(unit.name, unit.run) for unit in units],
                         [(ALPHA, UnitState.STANDING_BY), (BETA, UnitState.RUN_FINISHED)])

    def test_a_finished_unit_that_shows_a_form_keeps_its_place_and_needs_its_entry(self) -> None:
        self.production_doc(beta=True)
        self.status_lines(f"== {ALPHA}", "● Checking panel labels",
                          f"== {BETA}", f"FORM WAITING on you in {BETA}: a question form is on its screen")
        self.judgment_file()
        self.finished_run(self.root / "beta")
        refused = self.run_builder()
        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertIn("units[1].project: required", refused.stdout + refused.stderr)
        self.assertNotIn("left out", refused.stdout)

    def test_a_finished_unit_whose_question_was_shown_before_keeps_its_place(self) -> None:
        self.production_doc(beta=True)
        self.status_lines(f"== {ALPHA}", "● Checking panel labels",
                          f"== {BETA}", f"STILL WAITING on you, {BETA}:  — gate: add-on review, item 1 of 2")
        self.judgment_file()
        self.finished_run(self.root / "beta")
        refused = self.run_builder()
        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertIn("units[1].project: required", refused.stdout + refused.stderr)
        self.assertNotIn("left out", refused.stdout)

    def test_each_length_produces_input_the_renderer_accepts(self) -> None:
        for length in ("gantt", "simple", "page", "elaborate"):
            with self.subTest(length=length):
                result = self.run_builder("--length", length, subprocess_cli=length == "gantt")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = self.report()
                self.assertEqual(report["length"], length)
                self.assertEqual(report["zone"], "America/Los_Angeles")
                self.assertEqual(self.unit()["unit"], ALPHA)
                self.assertNotIn("label", self.unit())
                self.assertEqual(cast(dict[str, object], self.unit()["eta"])["time"], "23:40")
                rendered = subprocess.run([sys.executable, str(RENDERER), str(self.output),
                                           "--at", "2026-10-06T17:00"],
                                          env=self.env, capture_output=True, text=True, check=False)
                self.assertEqual(rendered.returncode, 0, rendered.stderr)

    def test_record_supplies_start_stated_eta_range_percent_first_and_reason_without_screen_eta(self) -> None:
        self.open_phase_record(eta={
            "time": "2026-10-07T00:40:00+00:00",
            "earliest": "2026-10-07T00:35:00+00:00",
            "latest": "2026-10-07T00:55:00+00:00",
            "source": "stated",
            "stated_at": "2026-10-06T23:45:00+00:00",
            "basis": "checks\n  and   build remain",
            "as_of": None,
        })
        self.status_lines(f"== {ALPHA}", "● Checking panel labels")
        fields = cast(dict[str, object], json.loads(self.judgment.read_text(encoding="utf-8")))
        unit = cast(list[dict[str, object]], fields["units"])[0]
        del unit["started"]
        _ = self.judgment.write_text(json.dumps(fields), encoding="utf-8")

        built = self.run_builder()

        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        self.assertEqual(self.unit()["started"], "2026-10-06T08:30")
        self.assertEqual(self.unit()["eta"], {
            "time": "17:40", "earliest": "17:35", "latest": "17:55", "percent": 60,
            "first": "2026-10-06T17:30", "why": "checks and build remain",
            "fixes": 0, "stated": "2026-10-06T16:45",
        })
        # Pruning is the snapshot timer's (prune_phase_notes.py): a dailies only reads.
        self.assertEqual(self.phase_table_calls(), [
            ["show", "--production-doc", str(self.doc), "--json"],
        ])

    def test_real_phase_table_command_supplies_the_record_after_peer_handoff(self) -> None:
        plan = self.root / "alpha/docs/alpha.md"
        plan.parent.mkdir()
        _ = plan.write_text(
            "# Alpha plan\n\n"
            + "### Phase 1 — Earlier work  · status: done\n\n"
            + "### Phase 2 — Panel labels stay clear  · status: todo\n",
            encoding="utf-8",
        )
        utc = ZoneInfo("UTC")
        midnight = datetime(2026, 10, 6, tzinfo=utc)

        def epoch(hour: int, minute: int) -> int:
            return int((midnight + timedelta(hours=hour, minutes=minute)).timestamp())

        run = self.root / "history/runs/current.jsonl"
        run.parent.mkdir(parents=True)
        events = (
            {"event_type": "run_started", "working_dir": str(self.root / "alpha"),
             "plan_doc": "docs/alpha.md", "run_started_at": epoch(22, 0),
             "timestamp_epoch": epoch(22, 0)},
            {"event_type": "phase_started", "phase_id": "2", "phase_instance_id": "two",
             "phase_title": "Panel labels stay clear", "timestamp_epoch": epoch(22, 30)},
            {"event_type": "eta_stated", "phase_id": "2", "phase_instance_id": "two",
             "eta_at": epoch(24, 40), "basis": "two checks remain",
             "timestamp_epoch": epoch(23, 20)},
            {"event_type": "progress_reported", "phase_id": "2", "phase_instance_id": "two",
             "phase_percent": 60, "phase_elapsed_seconds": 3000, "phase_calibration": None,
             "timestamp_epoch": epoch(23, 30)},
        )
        _ = run.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        self.status_lines(f"== {ALPHA}", "● Checking panel labels")
        fields = cast(dict[str, object], json.loads(self.judgment.read_text(encoding="utf-8")))
        del cast(list[dict[str, object]], fields["units"])[0]["started"]
        _ = self.judgment.write_text(json.dumps(fields), encoding="utf-8")
        _ = self.env.pop("DAILIES_PHASE_TABLE")
        self.env["UNIT_NOW_EPOCH"] = str(epoch(23, 35))

        built = self.run_builder()

        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        self.assertEqual(self.unit()["started"], "2026-10-06T15:30")
        eta = cast(dict[str, object], self.unit()["eta"])
        self.assertEqual((eta["time"], eta["percent"], eta["why"], eta["stated"]),
                         ("17:40", 60, "two checks remain", "2026-10-06T16:20"))

    def test_record_projected_eta_uses_projection_reason_and_report_zone(self) -> None:
        self.open_phase_record(eta={
            "time": "2026-10-07T01:10:00+00:00",
            "earliest": "2026-10-07T01:00:00+00:00",
            "latest": "2026-10-07T01:25:00+00:00",
            "source": "projected",
            "stated_at": None,
            "basis": None,
            "as_of": "2026-10-06T23:50:00+00:00",
        })
        self.status_lines(f"== {ALPHA}", "● Checking panel labels")

        built = self.run_builder()

        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        self.assertEqual(self.unit()["eta"], {
            "time": "18:10", "earliest": "18:00", "latest": "18:25", "percent": 60,
            "first": "2026-10-06T17:30", "why": "it is now projected from 60% done",
            "fixes": 0, "stated": "2026-10-06T16:50",
        })

    def test_record_supplies_repair_rounds_and_the_renderer_names_them(self) -> None:
        self.open_phase_record(repair_rounds=2)
        self.status_lines(f"== {ALPHA}", "● Checking panel labels")

        built = self.run_builder("--length", "simple")

        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        eta = cast(dict[str, object], self.unit()["eta"])
        self.assertEqual(eta["fixes"], 2)
        rendered = self.run_renderer(AT)
        self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
        self.assertIn("2 fix rounds added", rendered.stdout)

    def test_record_eta_each_judgment_field_wins_and_a_deciding_value_keeps_record_out(self) -> None:
        self.open_phase_record(repair_rounds=2)
        self.status_lines(f"== {ALPHA}", "ETA 18:55")
        self.judgment_file(alpha={
            "started": "2026-10-06T09:12",
            "eta": {"earliest": "17:32", "percent": 72, "first": "2026-10-06T16:00",
                    "why": "the showrunner knows why", "fixes": 5},
        })
        filled = self.run_builder()
        self.assertEqual(filled.returncode, 0, filled.stdout + filled.stderr)
        self.assertEqual(self.unit()["started"], "2026-10-06T09:12")
        eta = cast(dict[str, object], self.unit()["eta"])
        self.assertEqual((eta["time"], eta["earliest"], eta["latest"]), ("17:40", "17:32", "17:55"))
        self.assertEqual((eta["percent"], eta["first"], eta["why"], eta["fixes"]),
                         (72, "2026-10-06T16:00", "the showrunner knows why", 5))

        self.judgment_file(alpha={"eta": {"none": "none measured"}})
        decided = self.run_builder()
        self.assertEqual(decided.returncode, 0, decided.stdout + decided.stderr)
        self.assertEqual(self.unit()["eta"], {"none": "none measured"})
        self.assertNotIn("request /unit:eta:", decided.stdout)

        self.judgment_file(alpha={"eta": {"time": "18:20", "percent": 44,
                                                   "why": "the showrunner set the target"}})
        timed = self.run_builder()
        self.assertEqual(timed.returncode, 0, timed.stdout + timed.stderr)
        self.assertEqual(self.unit()["eta"], {
            "time": "18:20", "percent": 44, "why": "the showrunner set the target",
        })
        self.assertNotIn("request /unit:eta:", timed.stdout)

    def test_record_with_no_percent_still_supplies_start_and_eta(self) -> None:
        self.open_phase_record(percent=None)
        self.status_lines(f"== {ALPHA}", "● Checking panel labels")
        self.judgment_file(alpha={"eta": {}})
        built = self.run_builder()
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        eta = cast(dict[str, object], self.unit()["eta"])
        self.assertEqual((eta["time"], eta["percent"]), ("17:40", None))

    def test_stale_record_eta_uses_its_source_time_and_requests_once(self) -> None:
        cases: tuple[tuple[str, dict[str, object]], ...] = (
            ("stated", {
                "time": "2026-10-07T01:40:00+00:00", "earliest": None, "latest": None,
                "source": "stated", "stated_at": "2026-10-06T22:30:00+00:00",
                "basis": "the panel pass remains", "as_of": None,
            }),
            ("projected", {
                "time": "2026-10-07T01:40:00+00:00", "earliest": "2026-10-07T01:30:00+00:00",
                "latest": "2026-10-07T01:50:00+00:00", "source": "projected",
                "stated_at": None, "basis": None, "as_of": "2026-10-06T22:30:00+00:00",
            }),
        )
        for source, eta_record in cases:
            with self.subTest(source=source):
                self.state.mkdir(exist_ok=True)
                for path in (self.state / "eta_seen.json", self.phase_table_events):
                    if path.exists():
                        path.unlink()
                self.open_phase_record(eta=eta_record)
                self.status_lines(f"== {ALPHA}", "● Checking panel labels")
                first = self.run_builder()
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                eta = cast(dict[str, object], self.unit()["eta"])
                self.assertEqual(eta["detail"], "set 15:30")
                self.assertEqual(eta["stated"], "2026-10-06T15:30")
                self.assertEqual(first.stdout.count(f"request /unit:eta: {ALPHA}"), 1)
                repeated = self.run_builder()
                self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
                self.assertNotIn("request /unit:eta:", repeated.stdout)

    def test_passed_record_eta_and_passed_first_target_each_request_once(self) -> None:
        self.open_phase_record(eta={
            "time": "2026-10-06T23:40:00+00:00", "earliest": None, "latest": None,
            "source": "stated", "stated_at": "2026-10-06T23:30:00+00:00",
            "basis": "the panel pass remained", "as_of": None,
        })
        self.status_lines(f"== {ALPHA}", "● Checking panel labels")
        passed = self.run_builder()
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertEqual(self.unit()["eta"], {"none": "none measured - requested"})
        self.assertEqual(passed.stdout.count(f"request /unit:eta: {ALPHA}"), 1)

        self.state.mkdir(exist_ok=True)
        (self.state / "eta_seen.json").unlink()
        self.open_phase_record(has_eta=False, first="2026-10-06T23:40:00+00:00")
        self.judgment_file(alpha={"held": "the panel review is paused", "eta": {}})
        first_target_passed = self.run_builder()
        self.assertEqual(first_target_passed.returncode, 0, first_target_passed.stdout + first_target_passed.stderr)
        self.assertEqual(self.unit()["eta"], {"none": "none measured - requested"})
        self.assertEqual(first_target_passed.stdout.count(f"request /unit:eta: {ALPHA}"), 1)
        repeated = self.run_builder()
        self.assertNotIn("request /unit:eta:", repeated.stdout)

    def test_record_with_no_eta_ever_stated_uses_none_without_request(self) -> None:
        self.open_phase_record(has_eta=False, first=None)
        self.status_lines(f"== {ALPHA}", "● Checking panel labels")
        built = self.run_builder()
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        self.assertEqual(self.unit()["eta"], {"none": "no ETA stated yet"})
        self.assertNotIn("fixes", cast(dict[str, object], self.unit()["eta"]))
        self.assertNotIn("request /unit:eta:", built.stdout)

    def test_malformed_record_repair_rounds_use_the_status_capture_path(self) -> None:
        for malformed in (-1, True, "2"):
            with self.subTest(malformed=malformed):
                self.open_phase_record()
                payload = cast(
                    dict[str, dict[str, object]],
                    json.loads(self.phase_table_json.read_text(encoding="utf-8")),
                )
                current = cast(dict[str, object], payload[ALPHA]["current"])
                current["repair_rounds_since_first_stated_eta"] = malformed
                _ = self.phase_table_json.write_text(
                    json.dumps(payload) + "\n", encoding="utf-8"
                )
                self.status_lines(f"== {ALPHA}", "● Checking panel labels", "ETA 23:40")

                built = self.run_builder()

                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                self.assertIn(
                    "phase tables: unavailable — "
                    + "phase tables.alpha-unit.current."
                    + "repair_rounds_since_first_stated_eta: "
                    + "expected a whole number from 0",
                    built.stdout,
                )
                eta = cast(dict[str, object], self.unit()["eta"])
                self.assertEqual(eta["time"], "23:40")

    def test_nonmatching_record_keeps_the_screen_capture_path(self) -> None:
        self.open_phase_record(phase="3")
        self.status_lines(f"== {ALPHA}", "● Held checkpoint remains", "ETA 18:15")
        self.judgment_file(alpha={"held": "the checkpoint awaits its merge"})
        built = self.run_builder()
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        self.assertEqual(self.unit()["started"], "2026-10-06T09:00")
        self.assertEqual(cast(dict[str, object], self.unit()["eta"])["time"], "18:15")

    def test_follow_up_judgment_keeps_the_screen_capture_path(self) -> None:
        self.open_phase_record()
        self.status_lines(f"== {ALPHA}", "ETA 18:15")
        self.judgment_file(alpha={
            "phase": "follow-up 1 of 1: urgent panel repair",
            "then": ["the plan at Phase 2: panel labels stay clear"],
        })
        built = self.run_builder()
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        self.assertEqual(cast(dict[str, object], self.unit()["eta"])["time"], "18:15")

    def test_no_open_or_unavailable_record_keeps_the_screen_capture_path(self) -> None:
        self.open_phase_record()
        payload = cast(dict[str, dict[str, object]], json.loads(self.phase_table_json.read_text()))
        for record in ({**payload[ALPHA], "current": None}, {"unavailable": "plan cannot be read"}):
            with self.subTest(record=record):
                _ = self.phase_table_json.write_text(json.dumps({ALPHA: record}), encoding="utf-8")
                self.status_lines(f"== {ALPHA}", "ETA 18:15")
                built = self.run_builder()
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                self.assertEqual(cast(dict[str, object], self.unit()["eta"])["time"], "18:15")

    def test_record_phase_outside_plan_and_non_numeric_phase_keep_the_screen_capture_path(self) -> None:
        for phase, status in (("2", "todo"), ("12a", "running")):
            with self.subTest(phase=phase, status=status):
                self.open_phase_record(phase=phase, status=status)
                self.status_lines(f"== {ALPHA}", "ETA 18:15")
                built = self.run_builder()
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                self.assertEqual(cast(dict[str, object], self.unit()["eta"])["time"], "18:15")

    def test_held_record_adopts_screen_text_without_requesting_again(self) -> None:
        self.open_phase_record(eta={
            "time": "2026-10-07T01:40:00+00:00", "earliest": None, "latest": None,
            "source": "stated", "stated_at": "2026-10-06T22:30:00+00:00",
            "basis": "the panel pass remains", "as_of": None,
        })
        key = f"{ALPHA}|Phase 2 of 3: panel labels stay clear"
        old = {key: {"text": "ETA 18:40", "first_seen": "2026-10-06T14:00", "requested": False}}
        _ = (self.state / "eta_seen.json").write_text(json.dumps(old), encoding="utf-8")
        self.status_lines(f"== {ALPHA}", "● Checking panel labels")
        self.judgment_file(alpha={"held": "the panel review is paused", "eta": {}})
        built = self.run_builder()
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        eta = cast(dict[str, object], self.unit()["eta"])
        self.assertNotIn("detail", eta)
        self.assertEqual(eta["stated"], "2026-10-06T15:30")
        self.assertNotIn("request /unit:eta:", built.stdout)
        saved = cast(dict[str, dict[str, object]], json.loads((self.state / "eta_seen.json").read_text()))
        self.assertEqual(saved[key]["text"], "2026-10-06T18:40")
        self.assertEqual(saved[key]["first_seen"], "2026-10-06T14:00")

    def test_held_record_target_passed_earlier_that_day_stays_overdue(self) -> None:
        self.open_phase_record(eta={
            "time": "2026-10-06T23:00:00+00:00",
            "earliest": "2026-10-06T22:50:00+00:00",
            "latest": "2026-10-06T23:10:00+00:00",
            "source": "projected", "stated_at": None,
            "basis": None, "as_of": "2026-10-06T22:30:00+00:00",
        }, first="2026-10-06T23:00:00+00:00")
        self.status_lines(f"== {ALPHA}", "● Checking panel labels")
        self.judgment_file(alpha={"held": "the panel review is paused", "eta": {}})
        first = self.run_builder("--at", "2026-10-06T15:50", "--length", "simple")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

        built = self.run_builder("--at", "2026-10-06T19:00", "--length", "simple")
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        eta = cast(dict[str, object], self.unit()["eta"])
        self.assertEqual((eta["time"], eta["stated"]), ("16:00", "2026-10-06T15:30"))
        self.assertEqual((eta["earliest"], eta["latest"]), ("15:50", "16:10"))
        rendered = self.run_renderer("2026-10-06T19:00")
        self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
        self.assertIn(
            "- eta: 16:00 PDT, 60% done (overdue; range 15:50–16:10)", rendered.stdout)
        self.assertNotIn("tomorrow", rendered.stdout)
        saved = cast(dict[str, dict[str, object]], json.loads(
            (self.root / "render-state.json").read_text(encoding="utf-8")))
        self.assertEqual(saved[ALPHA]["eta"], "2026-10-06T16:00:00")

    def test_held_record_target_from_an_earlier_day_stays_on_that_day(self) -> None:
        self.open_phase_record(
            started="2026-10-05T21:00:00+00:00",
            eta={
                "time": "2026-10-05T23:00:00+00:00",
                "earliest": "2026-10-05T22:50:00+00:00",
                "latest": "2026-10-05T23:10:00+00:00",
                "source": "projected", "stated_at": None,
                "basis": None, "as_of": "2026-10-05T22:30:00+00:00",
            },
            first="2026-10-05T23:00:00+00:00",
        )
        self.status_lines(f"== {ALPHA}", "● Checking panel labels")
        self.judgment_file(alpha={"held": "the panel review is paused", "eta": {}})
        first = self.run_builder("--at", "2026-10-05T15:50", "--length", "simple")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

        built = self.run_builder("--at", "2026-10-06T19:00", "--length", "simple")
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        eta = cast(dict[str, object], self.unit()["eta"])
        self.assertEqual((eta["time"], eta["stated"]), ("16:00", "2026-10-05T15:30"))
        self.assertEqual((eta["earliest"], eta["latest"]), ("15:50", "16:10"))
        rendered = self.run_renderer("2026-10-06T19:00")
        self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
        self.assertIn("overdue", rendered.stdout)
        self.assertIn("range Mon 15:50–Mon 16:10", rendered.stdout)
        self.assertNotIn("tomorrow", rendered.stdout)
        saved = cast(dict[str, dict[str, object]], json.loads(
            (self.root / "render-state.json").read_text(encoding="utf-8")))
        self.assertEqual(saved[ALPHA]["eta"], "2026-10-05T16:00:00")

    def test_record_range_that_resolves_to_its_own_minutes_is_kept(self) -> None:
        self.open_phase_record(eta={
            "time": "2026-10-06T23:00:00+00:00",
            "earliest": "2026-10-06T22:50:00+00:00",
            "latest": "2026-10-06T23:10:00+00:00",
            "source": "projected", "stated_at": None,
            "basis": None, "as_of": "2026-10-06T20:30:00+00:00",
        }, first="2026-10-06T23:00:00+00:00")
        self.status_lines(f"== {ALPHA}", "● Checking panel labels")
        self.judgment_file(alpha={"eta": {}})
        built = self.run_builder("--at", "2026-10-06T14:00", "--length", "simple")
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        eta = cast(dict[str, object], self.unit()["eta"])
        self.assertEqual((eta["earliest"], eta["latest"]), ("15:50", "16:10"))
        rendered = self.run_renderer("2026-10-06T14:00")
        self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
        self.assertIn("range 15:50–16:10", rendered.stdout)
        saved = cast(dict[str, dict[str, object]], json.loads(
            (self.root / "render-state.json").read_text(encoding="utf-8")))
        self.assertEqual(saved[ALPHA]["eta"], "2026-10-06T16:00:00")

    def test_record_range_more_than_a_day_from_eta_is_omitted(self) -> None:
        self.open_phase_record(eta={
            "time": "2026-10-07T23:00:00+00:00",
            "earliest": "2026-10-06T22:50:00+00:00",
            "latest": "2026-10-07T23:10:00+00:00",
            "source": "projected", "stated_at": None,
            "basis": None, "as_of": "2026-10-06T20:30:00+00:00",
        }, first="2026-10-07T23:00:00+00:00")
        self.status_lines(f"== {ALPHA}", "● Checking panel labels")
        self.judgment_file(alpha={"eta": {}})
        built = self.run_builder("--at", "2026-10-06T14:00", "--length", "simple")
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        eta = cast(dict[str, object], self.unit()["eta"])
        self.assertEqual(eta["time"], "16:00+1")
        self.assertNotIn("earliest", eta)
        self.assertNotIn("latest", eta)
        rendered = self.run_renderer("2026-10-06T14:00")
        self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
        self.assertIn("- eta: 16:00 PDT tomorrow, 60% done", rendered.stdout)

    def test_record_target_the_day_after_the_report_keeps_plus_one(self) -> None:
        self.open_phase_record(eta={
            "time": "2026-10-08T00:40:00+00:00", "earliest": None, "latest": None,
            "source": "stated", "stated_at": "2026-10-06T23:45:00+00:00",
            "basis": "the panel pass remains", "as_of": None,
        }, first="2026-10-08T00:40:00+00:00")
        self.status_lines(f"== {ALPHA}", "● Checking panel labels")
        self.judgment_file(alpha={"eta": {}})
        built = self.run_builder("--length", "simple")
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        eta = cast(dict[str, object], self.unit()["eta"])
        self.assertEqual((eta["time"], eta["stated"]), ("17:40+1", "2026-10-06T16:45"))
        rendered = self.run_renderer(AT)
        self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
        saved = cast(dict[str, dict[str, object]], json.loads(
            (self.root / "render-state.json").read_text(encoding="utf-8")))
        self.assertEqual(saved[ALPHA]["eta"], "2026-10-07T17:40:00")

    def test_record_target_far_after_its_source_uses_target_as_renderer_anchor(self) -> None:
        self.open_phase_record(eta={
            "time": "2026-10-07T00:00:00+00:00", "earliest": None, "latest": None,
            "source": "stated", "stated_at": "2026-10-06T01:00:00+00:00",
            "basis": "the long panel pass remains", "as_of": None,
        }, first="2026-10-07T00:00:00+00:00")
        self.status_lines(f"== {ALPHA}", "● Checking panel labels")
        self.judgment_file(alpha={"eta": {}})
        built = self.run_builder("--at", "2026-10-06T16:00", "--length", "simple")
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        eta = cast(dict[str, object], self.unit()["eta"])
        self.assertEqual((eta["time"], eta["stated"]), ("17:00", "2026-10-06T17:00"))
        rendered = self.run_renderer("2026-10-06T16:00")
        self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
        saved = cast(dict[str, dict[str, object]], json.loads(
            (self.root / "render-state.json").read_text(encoding="utf-8")))
        self.assertEqual(saved[ALPHA]["eta"], "2026-10-06T17:00:00")

    def test_record_backed_session_flags_stay_first(self) -> None:
        self.open_phase_record()
        for marker in ("SESSION GONE", "CLAUDE NOT RUNNING"):
            with self.subTest(marker=marker):
                self.status_lines(f"== {ALPHA}", marker)
                built = self.run_builder()
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                self.assertIn(f"flags first: {ALPHA}: {marker}", built.stdout)
                self.assertEqual(cast(dict[str, object], self.unit()["eta"])["time"], "17:40")

    def test_phases_that_could_start_are_printed_for_the_showrunner(self) -> None:
        self.open_phase_record()
        payload = cast(dict[str, dict[str, object]], json.loads(self.phase_table_json.read_text()))
        payload[ALPHA]["parallel"] = {"could_start": ["3", "4"], "line": "Parallel: Phase 3, Phase 4 could start"}
        _ = self.phase_table_json.write_text(json.dumps(payload), encoding="utf-8")
        built = self.run_builder()
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        self.assertIn(f"parallel check: {ALPHA}: Phase 3 could start\n"
                      + f"parallel check: {ALPHA}: Phase 4 could start", built.stdout)

        payload[ALPHA]["parallel"] = {"could_start": [], "line": "Parallel: none — no phase is waiting"}
        _ = self.phase_table_json.write_text(json.dumps(payload), encoding="utf-8")
        quiet = self.run_builder()
        self.assertEqual(quiet.returncode, 0, quiet.stdout + quiet.stderr)
        self.assertNotIn("parallel check:", quiet.stdout)

    def test_phase_table_failures_continue_from_the_status_capture(self) -> None:
        self.env["DAILIES_PHASE_TABLE_FAIL"] = "show"
        built = self.run_builder()
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        self.assertIn("phase tables: unavailable — show could not read records", built.stdout)
        self.assertEqual(cast(dict[str, object], self.unit()["eta"])["time"], "23:40")

        self.open_phase_record()
        self.status_lines(f"== {ALPHA}", "● Checking panel labels")
        judgment = cast(dict[str, list[dict[str, object]]], json.loads(
            self.judgment.read_text(encoding="utf-8")))
        del judgment["units"][0]["started"]
        _ = self.judgment.write_text(json.dumps(judgment) + "\n", encoding="utf-8")
        _ = self.env.pop("DAILIES_PHASE_TABLE_FAIL")
        recorded = self.run_builder()
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        self.assertNotIn("phase tables: unavailable", recorded.stdout)
        self.assertEqual(self.unit()["started"], "2026-10-06T08:30")
        self.assertEqual(cast(dict[str, object], self.unit()["eta"])["time"], "17:40")

        self.status_lines(f"== {ALPHA}", "● Checking panel labels", "ETA 23:40")
        self.judgment_file()
        _ = self.phase_table_json.write_text("not json\n", encoding="utf-8")
        unparsable = self.run_builder()
        self.assertEqual(unparsable.returncode, 0, unparsable.stdout + unparsable.stderr)
        self.assertIn("phase tables: unavailable —", unparsable.stdout)
        self.assertEqual(cast(dict[str, object], self.unit()["eta"])["time"], "23:40")

    def test_a_dailies_leaves_a_retired_units_note_for_the_timer_to_prune(self) -> None:
        note = self.root / "vault/old-showrunner/alpha-unit.md"
        note.parent.mkdir(parents=True)
        _ = note.write_text(
            "---\nphase_table: true\nproduction: example\nunit: alpha-unit\n---\n\n# Alpha\n",
            encoding="utf-8",
        )
        content = self.doc.read_text(encoding="utf-8")
        _ = self.doc.write_text(content.replace("`docs/alpha.md`", "retired after completion", 1),
                                encoding="utf-8")
        _ = self.env.pop("DAILIES_PHASE_TABLE")
        _ = self.assert_refused_without_output(mention="no live units: every Units row is retired")
        self.assertTrue(note.exists())

    def test_record_backed_input_renders_at_every_length_without_screen_eta(self) -> None:
        self.open_phase_record()
        self.status_lines(f"== {ALPHA}", "● Checking panel labels")
        for length in ("gantt", "simple", "page", "elaborate"):
            with self.subTest(length=length):
                built = self.run_builder("--length", length)
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                rendered = subprocess.run(
                    [sys.executable, str(RENDERER), str(self.output), "--at", AT],
                    env=self.env, capture_output=True, text=True, check=False,
                )
                self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)

    def test_projected_move_generates_the_gantt_change_reason(self) -> None:
        def projected(time: str) -> dict[str, object]:
            return {
                "time": time, "earliest": time, "latest": time, "source": "projected",
                "stated_at": None, "basis": None, "as_of": "2026-10-06T23:50:00+00:00",
            }

        self.open_phase_record(eta=projected("2026-10-07T00:40:00+00:00"))
        self.status_lines(f"== {ALPHA}", "● Checking panel labels")
        first = self.run_builder()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        first_render = self.run_renderer(AT)
        self.assertEqual(first_render.returncode, 0, first_render.stdout + first_render.stderr)
        self.open_phase_record(eta=projected("2026-10-07T01:00:00+00:00"))
        moved = self.run_builder()
        self.assertEqual(moved.returncode, 0, moved.stdout + moved.stderr)
        self.assertEqual(cast(dict[str, object], self.unit()["eta"])["why"],
                         "it is now projected from 60% done")
        rendered = self.run_renderer(AT)
        self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
        self.assertIn("changed: +0:20 because it is now projected from 60% done", rendered.stdout)

    def test_stated_move_uses_basis_unless_judgment_supplies_why(self) -> None:
        def stated(time: str, basis: str) -> dict[str, object]:
            return {
                "time": time, "earliest": None, "latest": None, "source": "stated",
                "stated_at": "2026-10-06T23:50:00+00:00", "basis": basis, "as_of": None,
            }

        self.open_phase_record(eta=stated("2026-10-07T00:40:00+00:00", "the first pass remains"))
        self.status_lines(f"== {ALPHA}", "● Checking panel labels")
        first = self.run_builder()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.run_renderer(AT).returncode, 0)
        self.open_phase_record(eta=stated("2026-10-07T01:00:00+00:00", "two panel checks remain"))
        generated = self.run_builder()
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        self.assertEqual(cast(dict[str, object], self.unit()["eta"])["why"], "two panel checks remain")

        self.judgment_file(alpha={"eta": {"why": "the showrunner saw one more check"}})
        overridden = self.run_builder()
        self.assertEqual(overridden.returncode, 0, overridden.stdout + overridden.stderr)
        self.assertEqual(cast(dict[str, object], self.unit()["eta"])["why"],
                         "the showrunner saw one more check")

    def test_retired_unit_needs_no_status_or_judgment_entry(self) -> None:
        retired_row = (f"| `{BETA}` | (retired by the user; run done) | `{self.root / 'beta'}` | "
                       f"`beta` | `{BETA}` | — | — |")
        content = self.doc.read_text(encoding="utf-8")
        _ = self.doc.write_text(content.replace("\n\n## Gates", f"\n{retired_row}\n\n## Gates"),
                                encoding="utf-8")
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        units = cast(list[dict[str, object]], self.report()["units"])
        self.assertEqual([unit["unit"] for unit in units], [ALPHA])

    def test_all_retired_units_report_no_live_units(self) -> None:
        content = self.doc.read_text(encoding="utf-8")
        _ = self.doc.write_text(content.replace("`docs/alpha.md`", "retired after completion", 1),
                                encoding="utf-8")
        _ = self.assert_refused_without_output(
            mention="no live units: every Units row is retired")

    def test_short_session_without_judgment_label_uses_renderer_default(self) -> None:
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("label", self.unit())

    def test_render_state_is_required_before_notifier_or_output(self) -> None:
        _ = self.output.write_text("sentinel\n", encoding="utf-8")
        result = self.run_builder(include_render_state=False, subprocess_cli=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("usage:", result.stderr)
        self.assertIn("--render-state", result.stderr)
        self.assertEqual(self.notifier_events(), [])
        self.assertEqual(self.output.read_text(encoding="utf-8"), "sentinel\n")

    def test_long_session_without_judgment_label_is_refused(self) -> None:
        self.rename_unit("enh-showrunner-unit")
        result = self.run_builder()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("judgment: failed", result.stdout)
        self.assertIn("units[0].label", result.stdout)
        self.assertFalse(self.output.exists())

    def test_long_session_accepts_short_judgment_label(self) -> None:
        self.rename_unit("enh-showrunner-unit")
        fields = cast(dict[str, object], json.loads(self.judgment.read_text(encoding="utf-8")))
        units = cast(list[dict[str, object]], fields["units"])
        units[0]["label"] = "showrunn"
        _ = self.judgment.write_text(json.dumps(fields), encoding="utf-8")
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.unit()["label"], "showrunn")

    def test_session_gone_is_printed_first(self) -> None:
        self.status_lines(f"== {ALPHA}", "SESSION GONE")
        self.judgment_file(alpha={"eta": {}})
        self.assertIsInstance(self.parsed_block()[0], SessionGone)
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("flags first:", result.stdout)
        self.assertIn("SESSION GONE", result.stdout)

    def test_claude_not_running_is_printed_first(self) -> None:
        self.status_lines(f"== {ALPHA}", "CLAUDE NOT RUNNING")
        self.judgment_file(alpha={"eta": {}})
        self.assertIsInstance(self.parsed_block()[0], ClaudeNotRunning)
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("flags first:", result.stdout)
        self.assertIn("CLAUDE NOT RUNNING", result.stdout)

    def test_form_waiting_sets_needs_user_and_is_printed_first(self) -> None:
        self.status_lines(f"== {ALPHA}", f"FORM WAITING on you in {ALPHA}: a question form is on its screen")
        self.judgment_file(alpha={"eta": {}})
        state, flags, _ = self.parsed_block()
        self.assertIsInstance(state, Running)
        self.assertIsInstance(flags[0], FormWaiting)
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.unit()["needs_user"])
        self.assertIn("flags first:", result.stdout)
        self.assertIn("FORM WAITING", result.stdout)

    def test_decision_block_sets_needs_user_and_is_printed_first(self) -> None:
        self.status_lines(f"== {ALPHA}", f"=== DECISION for you from {ALPHA} ===",
                          "● Which panel should open first?", f"=== end {ALPHA} ===")
        self.judgment_file(alpha={"eta": {}})
        self.assertIsInstance(self.parsed_block()[1][0], Decision)
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.unit()["needs_user"])
        self.assertIn("flags first:", result.stdout)
        self.assertIn("DECISION", result.stdout)

    def test_still_waiting_sets_needs_user(self) -> None:
        self.status_lines(f"== {ALPHA}", f"STILL WAITING on you, {ALPHA}: choose the first panel")
        self.judgment_file(alpha={"eta": {}})
        self.assertIsInstance(self.parsed_block()[1][0], StillWaiting)
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.unit()["needs_user"])

    def test_ticks_failing_is_accepted(self) -> None:
        self.status_lines(f"== {ALPHA}", "TICKS FAILING (the last tick missed)")
        self.judgment_file(alpha={"eta": {}})
        self.assertIsInstance(self.parsed_block()[1][0], TicksFailing)
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_block_with_age_is_accepted(self) -> None:
        self.status_lines(f"== {ALPHA}", f"BLOCK in {ALPHA}, open 1h20m: waiting on showrunner")
        self.judgment_file(alpha={"eta": {}})
        flag = self.parsed_block()[1][0]
        self.assertIsInstance(flag, Block)
        if isinstance(flag, Block):
            self.assertEqual(flag.age, "1h20m")
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_usage_limit_activity_is_printed_first_and_other_activity_is_kept(self) -> None:
        self.status_lines(f"== {ALPHA}", "● Usage limit reached", "▸ Checking the panel labels", "ETA 23:40")
        self.assertEqual([item.text for item in self.parsed_block()[2]],
                         ["● Usage limit reached", "▸ Checking the panel labels", "ETA 23:40"])
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("flags first:", result.stdout)
        self.assertIn("usage limit", result.stdout.lower())
        self.assertEqual(result.returncode, 0)

    def test_unknown_status_unit_is_refused(self) -> None:
        self.status_lines("== unknown-unit", "SESSION GONE")
        _ = self.assert_refused_without_output(mention="unknown-unit")

    def test_activity_before_first_unit_is_refused(self) -> None:
        _ = self.status.write_text(STAMP + "\n● Checking panel labels\n== " + ALPHA + "\n",
                                   encoding="utf-8")
        _ = self.assert_refused_without_output(mention="Checking panel labels")

    def test_holder_marks_other_unit_but_never_its_own(self) -> None:
        self.production_doc(beta=True)
        self.status_lines(f"== {ALPHA}", "● Checking panel labels", f"== {BETA}", "● Checking button labels")
        self.judgment_file(beta=True, alpha={"eta": {}})
        _ = (self.holders / ALPHA).write_text(json.dumps({"holder": ALPHA,
            "since": "2026-10-06T09:55:00-07:00", "for": "the panel timing check",
            "release_eta": "unknown"}) + "\n", encoding="utf-8")
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        units = cast(list[dict[str, object]], self.report()["units"])
        self.assertFalse(units[0].get("build_hold", False))
        self.assertTrue(units[0]["holds_builds"])
        self.assertTrue(units[1]["build_hold"])
        self.assertNotIn("holds_builds", units[1])

    def test_a_holder_with_no_other_unit_renders_with_no_marker(self) -> None:
        _ = (self.holders / ALPHA).write_text(json.dumps({"holder": ALPHA,
            "since": "2026-10-06T09:55:00-07:00", "for": "the panel timing check",
            "release_eta": "unknown"}) + "\n", encoding="utf-8")
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        units = cast(list[dict[str, object]], self.report()["units"])
        self.assertNotIn("build_hold", units[0])
        self.assertTrue(units[0]["holds_builds"])

    def test_a_supplied_holder_mark_is_refused(self) -> None:
        self.judgment_file(alpha={"holds_builds": True})
        _ = self.assert_refused_without_output(mention="holds_builds: supplied by holder files")

    def test_a_hold_with_every_unit_left_out_still_renders(self) -> None:
        # Seen 2026-10-12: the only unit's run had finished while another session held the builds.
        self.finished_run(self.root / "alpha")
        _ = self.judgment.write_text(json.dumps({"units": [], "topics": []}) + "\n", encoding="utf-8")
        _ = (self.holders / "elsewhere").write_text(json.dumps({"holder": "elsewhere",
            "since": "2026-10-06T09:55:00-07:00", "for": "a frame timing check",
            "release_eta": "unknown"}) + "\n", encoding="utf-8")
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.report()["units"], [])
        self.assertIn(f"left out, run finished and nothing waiting on the user: {ALPHA}", result.stdout)
        self.assertIn("build hold: ok — 1 active holders checked", result.stdout)

    def test_build_hold_marker_without_holder_file_is_refused(self) -> None:
        self.judgment_file(alpha={"build_hold": True})
        _ = self.assert_refused_without_output(mention="build_hold")

    def test_user_run_restarts_notifier_once_and_logs_next_due(self) -> None:
        result = self.run_builder("--user-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.notifier_events(), [["restart", "showrunner-example"]])
        self.assertEqual(self.report()["next_run"], "17:55")
        self.assertIn("next_due=", (self.checkout / "production.log").read_text(encoding="utf-8"))

    def test_on_demand_user_run_touches_no_clock_and_names_no_next_run(self) -> None:
        doc = self.doc.read_text(encoding="utf-8")
        _ = self.doc.write_text(doc.replace("- **Updates:** every 15 minutes", "- **Updates:** on demand"),
                                encoding="utf-8")
        result = self.run_builder("--user-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.notifier_events(), [])
        self.assertNotIn("next_run", self.report())
        self.assertIn("clock: ok — on demand: no scheduled dailies", result.stdout)

    def test_counts_configured_for_the_production_go_in_the_input(self) -> None:
        config = self.home / ".claude/config/dailies.conf"
        config.parent.mkdir(parents=True)
        _ = (self.home / "ledger.md").write_text("- a — proposed\n- b — declined\n", encoding="utf-8")
        _ = config.write_text("example.nightly-queue=lines ~/ledger.md — proposed$\nother.queue=files ~/*.md\n",
                              encoding="utf-8")
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.report()["counts"], [{"label": "nightly queue", "count": 1}])
        self.assertIn("counts: ok — 1 from dailies.conf", result.stdout)

    def test_a_bad_counts_line_leaves_the_counts_out_and_says_why(self) -> None:
        config = self.home / ".claude/config/dailies.conf"
        config.parent.mkdir(parents=True)
        _ = config.write_text("example.queue=words ~/ledger.md\n", encoding="utf-8")
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("counts", self.report())
        self.assertIn("counts: unavailable — dailies.conf line 1: the count kind is lines, files or issues", result.stdout)

    def test_scheduled_run_reads_notifier_status_without_restart(self) -> None:
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.notifier_events(), [["status", "showrunner-example"]])
        self.assertEqual(self.report()["next_run"], "17:55")

    def test_each_completed_step_reports_once_before_input(self) -> None:
        result = self.run_builder("--user-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        input_index = next(index for index, line in enumerate(lines) if line.startswith("input: ok — "))
        for step in ("build hold", "review watch", "merge branch", "clock", "judgment"):
            with self.subTest(step=step):
                positions = [index for index, line in enumerate(lines) if line.startswith(f"{step}: ok — ")]
                self.assertEqual(len(positions), 1, result.stdout)
                self.assertLess(positions[0], input_index)

    def test_with_no_length_the_builder_writes_a_gantt_report(self) -> None:
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.report()["length"], "gantt")

    def test_scheduled_prompt_saves_complete_status_for_dailies_gantt(self) -> None:
        commands = SCRIPT.parents[2] / "commands/showrunner"
        produce = (commands / "produce.md").read_text(encoding="utf-8")
        dailies = (commands / "dailies.md").read_text(encoding="utf-8")
        prompt = produce.split("The prompt:", 1)[1].split("**A tick**", 1)[0]
        self.assertIn("> <SCRATCH>/unit_status.txt", prompt)
        self.assertNotIn("cut -c1-400", prompt)
        self.assertIn("/showrunner:dailies gantt", prompt)
        self.assertIn("unit_status.txt", dailies)
        self.assertIn("/showrunner:dailies gantt", dailies)

    def test_dailies_command_surfaces_builder_step_failure_to_user(self) -> None:
        command = SCRIPT.parents[2] / "commands/showrunner/dailies.md"
        instructions = command.read_text(encoding="utf-8")
        self.assertIn(
            "When the builder prints `<step>: failed — <reason>`, give that exact line to " +
            "the user as the failure message.", " ".join(instructions.split()))

    def test_user_run_opens_log_before_restarting_notifier(self) -> None:
        (self.checkout / "production.log").mkdir()
        result = self.run_builder("--user-run")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("input: failed", result.stdout)
        self.assertFalse(any(event[0] == "restart" for event in self.notifier_events()))

    def test_next_run_uses_production_zone_across_host_day_boundary(self) -> None:
        epoch = int(datetime(2026, 10, 7, 0, 30, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp())
        host = datetime.fromtimestamp(epoch, ZoneInfo("Pacific/Honolulu"))
        self.assertEqual(host.date().isoformat(), "2026-10-06")
        self.env["TZ"] = "Pacific/Honolulu"
        self.env["DAILIES_TEST_NEXT_DUE"] = f"next_due={epoch} ({host:%Y-%m-%d %H:%M %Z})"
        result = self.run_builder("--at", "2026-10-06T23:45")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.report()["next_run"], "00:30+1")

    def test_newest_child_handoff_labels_the_merge_topic_without_a_unit_phase(self) -> None:
        _ = self.git("commit", "--allow-empty", "-m", "Merge alpha-unit phase 2 (aaaaaaa) into production")
        _ = self.git("commit", "--allow-empty", "-m", "Merge alpha-unit phase 2 shrink (bbbbbbb) into production")
        _ = self.git("commit", "--allow-empty", "-m", "chore(example): rename unit alpha-unit to renamed-unit")
        _ = self.git("commit", "--allow-empty", "-m", "Merge alpha-unit hand-off (ccccccc) into production")
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        topics = cast(list[dict[str, object]], self.report()["topics"])
        merge_topics = [topic for topic in topics if topic["title"] == "Merge branch"]
        self.assertEqual(len(merge_topics), 1)
        self.assertIn("last merge: alpha-unit style hand-off (ccccccc)", str(merge_topics[0]["update"]))
        self.assertNotIn("alpha-unit phase", str(merge_topics[0]["update"]))
        self.assertNotIn("renamed-unit", str(merge_topics[0]["update"]))

    def test_unpushed_merge_appears_as_merge_branch_topic_until_pushed(self) -> None:
        _ = self.git("checkout", "-b", "alpha")
        _ = (self.checkout / "alpha.txt").write_text("panel change\n", encoding="utf-8")
        _ = self.git("add", "alpha.txt")
        _ = self.git("commit", "-m", "panel change")
        short = self.git("rev-parse", "--short=7", "HEAD")
        _ = self.git("checkout", "production")
        _ = self.git("merge", "--no-ff", "-m",
                     f"Merge {ALPHA} phase 2 ({short}) into production", "alpha")
        unpushed = self.run_builder()
        self.assertEqual(unpushed.returncode, 0, unpushed.stdout + unpushed.stderr)
        topics = cast(list[dict[str, object]], self.report()["topics"])
        merge_topics = [item for item in topics if item["title"] == "Merge branch"]
        self.assertEqual(len(merge_topics), 1)
        self.assertIn(ALPHA, str(merge_topics[0]["update"]))
        _ = self.git("push", "origin", "production")
        pushed = self.run_builder()
        self.assertEqual(pushed.returncode, 0, pushed.stdout + pushed.stderr)
        topics = cast(list[dict[str, object]], self.report()["topics"])
        self.assertFalse(any(item["title"] == "Merge branch" for item in topics))

    def test_review_watch_is_a_topic_until_acknowledged(self) -> None:
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        topics = cast(list[dict[str, object]], self.report()["topics"])
        self.assertTrue(any(item["title"] == "Review watch" for item in topics))
        watch = self.home / ".claude/data/review_regime_watch.json"
        watch.parent.mkdir(parents=True)
        _ = watch.write_text(json.dumps({"acknowledged": "2026-10-06T16:00-07:00"}) + "\n",
                             encoding="utf-8")
        acknowledged = self.run_builder()
        self.assertEqual(acknowledged.returncode, 0, acknowledged.stdout + acknowledged.stderr)
        topics = cast(list[dict[str, object]], self.report()["topics"])
        self.assertFalse(any(item["title"] == "Review watch" for item in topics))

    def test_bevy_upgrade_topic_uses_record_line_and_waiting_answer_flag(self) -> None:
        for needs_user in (True, False):
            with self.subTest(needs_user=needs_user):
                waiting = " (1 waiting for your answer)" if needs_user else ""
                update = f"Bevy 0.20 upgrade: 1 of 2 done, 1 git ready{waiting}; checked 10:00 PDT"
                self.env["DAILIES_BEVY_OUTPUT"] = json.dumps({"update": update, "needs_user": needs_user})
                result = self.run_builder()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                topics = cast(list[dict[str, object]], self.report()["topics"])
                self.assertEqual([item for item in topics if item["title"] == "Bevy upgrade"], [
                    {"title": "Bevy upgrade", "update": update, "eta": "no ETA stated yet", "needs_user": needs_user}])
                self.assertIn(f"bevy upgrade: ok — {update}\n", result.stdout)
                self.assertLess(result.stdout.index("review watch: ok"), result.stdout.index("bevy upgrade: ok"))
                self.assertLess(result.stdout.index("bevy upgrade: ok"), result.stdout.index("merge branch: ok"))
                args = cast(list[str], json.loads((self.root / "bevy-events.json").read_text()))
                self.assertEqual(args, ["--production", str(self.doc), "line", "--json"])

    def test_bevy_upgrade_empty_output_adds_no_topic(self) -> None:
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("bevy upgrade: ok — no open watch\n", result.stdout)
        topics = cast(list[dict[str, object]], self.report()["topics"])
        self.assertFalse(any(item["title"] == "Bevy upgrade" for item in topics))

    def test_bevy_upgrade_long_sentence_is_bounded_and_report_is_written(self) -> None:
        limit = min(limit for limit in LENGTHS.values() if limit is not None)
        sentence = "Bevy 0.20 upgrade: " + "waiting " * limit
        self.env["DAILIES_BEVY_OUTPUT"] = json.dumps({"update": sentence, "needs_user": True})
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        topics = cast(list[dict[str, object]], self.report()["topics"])
        topic = next(item for item in topics if item["title"] == "Bevy upgrade")
        update = cast(str, topic["update"])
        self.assertLessEqual(len(update), limit)
        self.assertEqual(update, sentence[:limit - 1] + "…")
        self.assertIs(topic["needs_user"], True)
        self.assertIn(f"bevy upgrade: ok — {update}\n", result.stdout)
        self.assertIn("input: ok", result.stdout)

    def test_bevy_upgrade_long_failure_reason_is_bounded_and_report_is_written(self) -> None:
        limit = min(limit for limit in LENGTHS.values() if limit is not None)
        reason = "record unreadable " * limit
        self.env.update({"DAILIES_BEVY_EXIT": "2", "DAILIES_BEVY_STDERR": reason + "\nmore detail"})
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        topics = cast(list[dict[str, object]], self.report()["topics"])
        topic = next(item for item in topics if item["title"] == "Bevy upgrade")
        update = cast(str, topic["update"])
        self.assertLessEqual(len(update), limit)
        self.assertEqual(update, ("the record could not be read: " + reason)[:limit - 1] + "…")
        self.assertIs(topic["needs_user"], False)
        self.assertIn(f"bevy upgrade: unavailable — {update}\n", result.stdout)
        self.assertIn("input: ok", result.stdout)

    def test_bevy_upgrade_sentence_at_renderer_limit_is_unchanged(self) -> None:
        limit = min(limit for limit in LENGTHS.values() if limit is not None)
        sentence = "Bevy 0.20 upgrade: ".ljust(limit, "x")
        self.env["DAILIES_BEVY_OUTPUT"] = json.dumps({"update": sentence, "needs_user": False})
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        topics = cast(list[dict[str, object]], self.report()["topics"])
        topic = next(item for item in topics if item["title"] == "Bevy upgrade")
        self.assertEqual(topic["update"], sentence)
        self.assertIn(f"bevy upgrade: ok — {sentence}\n", result.stdout)
        self.assertIn("input: ok", result.stdout)

    def assert_bevy_unavailable(self, result: subprocess.CompletedProcess[str], reason: str) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        update = f"the record could not be read: {reason}"
        self.assertIn(f"bevy upgrade: unavailable — {update}\n", result.stdout)
        self.assertNotIn("bevy upgrade: failed", result.stdout)
        self.assertIn("input: ok", result.stdout)
        topics = cast(list[dict[str, object]], self.report()["topics"])
        self.assertEqual([item for item in topics if item["title"] == "Bevy upgrade"], [
            {"title": "Bevy upgrade", "update": update, "eta": "no ETA stated yet", "needs_user": False}])

    def test_bevy_upgrade_nonzero_exit_keeps_report_with_first_stderr_line(self) -> None:
        self.env.update({"DAILIES_BEVY_EXIT": "2", "DAILIES_BEVY_STDERR": "record is unreadable\nmore detail"})
        self.assert_bevy_unavailable(self.run_builder(), "record is unreadable")
        self.env["DAILIES_BEVY_STDERR"] = ""
        self.assert_bevy_unavailable(self.run_builder(), "command exited 2")

    def test_bevy_upgrade_wrong_json_shape_keeps_report_with_unavailable_topic(self) -> None:
        cases: tuple[object, ...] = ([], None, {}, {"update": 1, "needs_user": False},
                                   {"update": "Bevy upgrade", "needs_user": "false"},
                                   {"update": "", "needs_user": False},
                                   {"update": "Bevy upgrade\nextra line", "needs_user": False})
        for value in cases:
            with self.subTest(value=value):
                self.env["DAILIES_BEVY_OUTPUT"] = json.dumps(value)
                self.assert_bevy_unavailable(self.run_builder(),
                                             "expected an object with update text and needs_user boolean")

    def test_bevy_upgrade_invalid_json_prefers_first_stderr_line(self) -> None:
        self.env.update({"DAILIES_BEVY_OUTPUT": "not JSON", "DAILIES_BEVY_STDERR": "bad record\nmore detail"})
        self.assert_bevy_unavailable(self.run_builder(), "bad record")

    def test_bevy_upgrade_invalid_json_without_stderr_keeps_report_with_parse_reason(self) -> None:
        self.env["DAILIES_BEVY_OUTPUT"] = "not JSON"
        self.assert_bevy_unavailable(self.run_builder(), "Expecting value: line 1 column 1 (char 0)")

    def test_bevy_upgrade_missing_script_keeps_report_with_unavailable_topic(self) -> None:
        script = Path(self.env["DAILIES_BEVY_UPGRADE"])
        script.unlink()
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        topics = cast(list[dict[str, object]], self.report()["topics"])
        topic = next(item for item in topics if item["title"] == "Bevy upgrade")
        self.assertIn("can't open file", str(topic["update"]))
        self.assertFalse(topic["needs_user"])
        self.assertIn("bevy upgrade: unavailable", result.stdout)
        self.assertIn("input: ok", result.stdout)

    def test_bevy_upgrade_timeout_returns_unavailable_without_waiting(self) -> None:
        cases: tuple[tuple[str | bytes | None, str], ...] = (
            (None, "timed out after 30 seconds"),
            ("record locked\nmore detail", "record locked"),
            (b"record locked\nmore detail", "record locked"),
        )
        production = read_production(self.doc)
        command = [sys.executable, self.env["DAILIES_BEVY_UPGRADE"], "--production", str(self.doc), "line", "--json"]
        for stderr, reason in cases:
            with self.subTest(stderr=stderr), mock.patch.dict(os.environ, self.env), mock.patch(
                "dailies_input.subprocess.run", side_effect=subprocess.TimeoutExpired(command, 30, stderr=stderr),
            ) as run:
                self.assertEqual(bevy_upgrade_watch(production), BevyUpgradeUnavailable(reason))
                run.assert_called_once_with(command, capture_output=True, text=True, timeout=30, check=False)

    def test_dailies_first_review_alert_logs_once_without_a_phone_push(self) -> None:
        events = self.watch_stubs()
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("| phases merged | 12 |", result.stdout)
        topics = cast(list[dict[str, object]], self.report()["topics"])
        watch = next(item for item in topics if item["title"] == "Review watch")
        self.assertTrue(watch["needs_user"])
        self.assertIn("12 of 12 phases", str(watch["update"]))
        later = self.run_merge_watch()
        self.assertEqual(later.returncode, 0, later.stdout + later.stderr)
        self.assertNotIn("| phases merged |", later.stdout)
        calls = self.watch_events(events)
        self.assertEqual(sum(row[0] == "review_regime.py" and row[1] == "report" for row in calls), 1)
        self.assertEqual([row for row in calls if row[0] == "send.py"], [])
        self.assertIn("| phases merged | 12 |", (self.checkout / "production.log").read_text())
        self.assertEqual((self.checkout / "production.log").read_text().count("review watch"), 1)

    def test_merge_first_review_alert_logs_once_without_a_dailies_phone_push(self) -> None:
        events = self.watch_stubs()
        first = self.run_merge_watch()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("| phases merged |", first.stdout)
        later = self.run_builder()
        self.assertEqual(later.returncode, 0, later.stdout + later.stderr)
        topics = cast(list[dict[str, object]], self.report()["topics"])
        watch = next(item for item in topics if item["title"] == "Review watch")
        self.assertTrue(watch["needs_user"])
        calls = self.watch_events(events)
        self.assertEqual(sum(row[0] == "review_regime.py" and row[1] == "report" for row in calls), 1)
        self.assertEqual([row for row in calls if row[0] == "send.py"], [])
        self.assertIn("| phases merged | 12 |", (self.checkout / "production.log").read_text())
        self.assertEqual((self.checkout / "production.log").read_text().count("review watch"), 1)

    def test_judgment_held_and_testing_keep_merge_topic_even_when_pushed(self) -> None:
        fields = cast(dict[str, object], json.loads(self.judgment.read_text(encoding="utf-8")))
        fields["merge"] = {"held": "the panel check needs repair", "testing": "running panel checks"}
        _ = self.judgment.write_text(json.dumps(fields) + "\n", encoding="utf-8")
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        topics = cast(list[dict[str, object]], self.report()["topics"])
        merge_topics = [item for item in topics if item["title"] == "Merge branch"]
        self.assertEqual(len(merge_topics), 1)
        self.assertIn("held: the panel check needs repair", str(merge_topics[0]["update"]))
        self.assertIn("testing: running panel checks", str(merge_topics[0]["update"]))

    def test_failed_restart_leaves_output_untouched(self) -> None:
        _ = self.notifier_failure.touch()
        _ = self.assert_refused_without_output("--user-run", mention="failed")
        self.assertEqual(self.notifier_events(), [["restart", "showrunner-example"]])

    def test_stale_eta_keeps_time_and_requests_once_for_phase(self) -> None:
        first = self.run_builder()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        seen_path = self.state / "eta_seen.json"
        seen = cast(dict[str, dict[str, object]], json.loads(seen_path.read_text(encoding="utf-8")))
        key = f"{ALPHA}|Phase 2 of 3: panel labels stay clear"
        old = datetime.fromisoformat(AT) - timedelta(hours=2)
        seen[key]["first_seen"] = old.replace(tzinfo=None).isoformat()
        _ = seen_path.write_text(json.dumps(seen) + "\n", encoding="utf-8")
        stale = self.run_builder()
        self.assertEqual(stale.returncode, 0, stale.stdout + stale.stderr)
        eta = cast(dict[str, object], self.unit()["eta"])
        self.assertEqual(eta["time"], "23:40")
        self.assertEqual(eta["detail"], f"set {old:%H:%M}")
        self.assertIn(f"request /unit:eta: {ALPHA}", stale.stdout)
        repeated = self.run_builder()
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertNotIn("request /unit:eta:", repeated.stdout)

    def test_passed_eta_is_hidden_and_requests_once_for_phase(self) -> None:
        old = datetime.fromisoformat(AT) - timedelta(minutes=10)
        due = old.strftime("%H:%M")
        self.status_lines(f"== {ALPHA}", "● Checking panel labels", f"ETA {due}")
        self.judgment_file(alpha={"eta": {"percent": 60}})
        passed = self.run_builder()
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        eta = cast(dict[str, object], self.unit()["eta"])
        self.assertEqual(eta, {"none": "none measured - requested"})
        self.assertIn(f"request /unit:eta: {ALPHA}", passed.stdout)
        repeated = self.run_builder()
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertNotIn("request /unit:eta:", repeated.stdout)

    def test_an_eta_with_a_zone_is_read_in_the_report_zone(self) -> None:
        # The report zone is Pacific; 20:55 EDT is 17:55 there, still ahead of 17:00.
        for stated, shown in (("ETA 20:55 EDT", "17:55"), ("Phase ETA: 18:10 PDT", "18:10"),
                              ("ETA 01:30 UTC", "18:30"), ("ETA 20:55 ET (range 20:40-21:10)", "17:55"),
                              ("ETA 17:40 XYZ", "17:40"), ("ETA 02:10+1 EDT", "23:10"),
                              ("ETA 20:55 AST", "17:55"), ("ETA 21:55 ADT", "17:55")):
            with self.subTest(stated=stated):
                self.status_lines(f"== {ALPHA}", "● Checking panel labels", stated)
                self.judgment_file(alpha={"eta": {"percent": 60}})
                built = self.run_builder()
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                self.assertEqual(cast(dict[str, object], self.unit()["eta"])["time"], shown)

    def test_an_eta_already_passed_in_its_own_zone_is_hidden(self) -> None:
        # 19:50 EDT is 16:50 Pacific, ten minutes before the report; read as 19:50 it looked ahead.
        self.status_lines(f"== {ALPHA}", "● Checking panel labels", "Phase ETA: 19:50 EDT")
        built = self.run_builder()
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        self.assertEqual(self.unit()["eta"], {"none": "none measured - requested"})

    def test_an_eta_line_that_sat_in_the_pane_never_outranks_a_newer_one(self) -> None:
        pinned = "Phase ETA: 18:55 PDT"
        self.status_lines(f"== {ALPHA}", "● Checking panel labels", pinned)
        first = self.run_builder("--at", "2026-10-06T17:00")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(cast(dict[str, object], self.unit()["eta"])["time"], "18:55")

        # The unit states a new ETA above the pinned line, which stays the pane's lowest.
        self.status_lines(f"== {ALPHA}", "● ETA 18:05 PDT, the repair is in", pinned)
        second = self.run_builder("--at", "2026-10-06T17:20")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(cast(dict[str, object], self.unit()["eta"])["time"], "18:05")
        third = self.run_builder("--at", "2026-10-06T17:40")
        self.assertEqual(third.returncode, 0, third.stdout + third.stderr)
        self.assertEqual(cast(dict[str, object], self.unit()["eta"])["time"], "18:05")

        # Once the newer line has scrolled away the pinned one is all there is, and it reads as old.
        self.status_lines(f"== {ALPHA}", "● Checking panel labels", pinned)
        last = self.run_builder("--at", "2026-10-06T18:20")
        self.assertEqual(last.returncode, 0, last.stdout + last.stderr)
        eta = cast(dict[str, object], self.unit()["eta"])
        self.assertEqual((eta["time"], eta["detail"]), ("18:55", "set 17:00"))

    def test_held_unchanged_eta_keeps_one_moment_through_builder_and_renderer(self) -> None:
        self.status_lines(f"== {ALPHA}", "● Checking panel labels", "ETA 19:35")
        self.judgment_file(alpha={
            "held": "the panel review is paused",
            "eta": {"percent": 60},
        })
        baseline = self.run_builder("--at", "2026-10-06T19:30", "--length", "simple")
        self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)
        first_report = self.run_renderer("2026-10-06T19:30")
        self.assertEqual(first_report.returncode, 0, first_report.stdout + first_report.stderr)

        for at in ("2026-10-06T20:00", "2026-10-06T21:36", "2026-10-06T23:50"):
            with self.subTest(at=at):
                built = self.run_builder("--at", at, "--length", "simple")
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                self.assertEqual(cast(dict[str, object], self.unit()["eta"])["time"], "19:35")
                self.assertNotIn("request /unit:eta:", built.stdout)
                rendered = self.run_renderer(at)
                self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
                self.assertIn("- eta: 19:35 PDT, 60% done (unchanged, overdue)", rendered.stdout)
                self.assertNotIn("19:35 PDT tomorrow", rendered.stdout)
                saved = cast(dict[str, dict[str, object]], json.loads(
                    (self.root / "render-state.json").read_text(encoding="utf-8")))
                self.assertEqual(saved[ALPHA]["eta"], "2026-10-06T19:35:00")
                self.assertEqual(saved[ALPHA]["eta_text"], "19:35")
                log_line = (self.root / "dailies.log").read_text(encoding="utf-8").splitlines()[-1]
                self.assertIn(f"{ALPHA} Phase 2 of 3 19:35 PDT, 60% done", log_line)
                self.assertNotIn("tomorrow", log_line)

    def test_unheld_unchanged_passed_eta_is_hidden_and_requested(self) -> None:
        self.status_lines(f"== {ALPHA}", "● Checking panel labels", "ETA 19:35")
        first = self.run_builder("--at", "2026-10-06T19:30")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        passed = self.run_builder("--at", "2026-10-06T20:00")
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertEqual(self.unit()["eta"], {"none": "none measured - requested"})
        self.assertEqual(passed.stdout.count(f"request /unit:eta: {ALPHA}"), 1)

    def test_an_eta_a_new_one_was_asked_for_is_not_brought_back_when_its_unit_is_held(self) -> None:
        self.status_lines(f"== {ALPHA}", "● Checking panel labels", "ETA 19:35")
        first = self.run_builder("--at", "2026-10-06T19:30")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        passed = self.run_builder("--at", "2026-10-06T20:00")
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertEqual(self.unit()["eta"], {"none": "none measured - requested"})
        self.judgment_file(alpha={"held": "the panel review is paused", "eta": {"percent": 60}})

        held = self.run_builder("--at", "2026-10-06T20:10")
        self.assertEqual(held.returncode, 0, held.stdout + held.stderr)
        self.assertEqual(self.unit()["eta"], {"none": "none measured - requested"})
        self.assertNotIn("request /unit:eta:", held.stdout)

    def test_a_held_units_passed_eta_is_marked_overdue_in_a_report_with_no_earlier_report_of_it(self) -> None:
        self.status_lines(f"== {ALPHA}", "● Checking panel labels", "ETA 23:00")
        self.judgment_file(alpha={"held": "the panel review is paused", "eta": {"percent": 60}})
        baseline = self.run_builder("--at", "2026-10-06T19:30")
        self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)

        built = self.run_builder("--at", "2026-10-06T23:30", "--length", "simple")
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        rendered = self.run_renderer("2026-10-06T23:30")
        self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
        self.assertIn("- eta: 23:00 PDT, 60% done (overdue)", rendered.stdout)

    def test_a_time_that_names_no_day_is_read_on_the_day_it_was_stated(self) -> None:
        self.status_lines(f"== {ALPHA}", "● Checking panel labels", "ETA 19:35")
        self.judgment_file(alpha={"held": "the panel review is paused", "eta": {"percent": 60}})
        baseline = self.run_builder("--at", "2026-10-06T19:30")
        self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)

        # No report was rendered at 19:30, so the renderer has no moment saved for this ETA.
        built = self.run_builder("--at", "2026-10-06T23:50", "--length", "simple")
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        self.assertEqual(cast(dict[str, object], self.unit()["eta"])["stated"], "2026-10-06T19:30")
        rendered = self.run_renderer("2026-10-06T23:50")
        self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
        self.assertIn("- eta: 19:35 PDT, 60% done (overdue)", rendered.stdout)
        self.assertNotIn("tomorrow", rendered.stdout)

    def test_held_changed_eta_uses_the_two_hour_rule_in_the_renderer(self) -> None:
        self.status_lines(f"== {ALPHA}", "● Checking panel labels", "ETA 19:35")
        self.judgment_file(alpha={
            "held": "the panel review is paused",
            "eta": {"percent": 60},
        })
        baseline = self.run_builder("--at", "2026-10-06T19:30")
        self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)
        first_report = self.run_renderer("2026-10-06T19:30")
        self.assertEqual(first_report.returncode, 0, first_report.stdout + first_report.stderr)
        self.status_lines(f"== {ALPHA}", "● Checking panel labels", "ETA 19:36")
        self.judgment_file(alpha={
            "held": "the panel review is paused",
            "eta": {"percent": 60, "why": "the panel review found another repair"},
        })

        changed = self.run_builder("--at", "2026-10-06T23:50")
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        self.assertEqual(cast(dict[str, object], self.unit()["eta"])["time"], "19:36")
        rendered = self.run_renderer("2026-10-06T23:50")
        self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
        self.assertIn("19:36 PDT tomorrow", rendered.stdout)
        self.assertIn("changed: +24:01 because the panel review found another repair", rendered.stdout)

    def test_eta_just_after_midnight_is_tomorrow_and_stays_visible(self) -> None:
        self.status_lines(f"== {ALPHA}", "● Checking panel labels", "ETA 00:30")
        result = self.run_builder("--at", "2026-10-06T23:45")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(cast(dict[str, object], self.unit()["eta"])["time"], "00:30")
        self.assertNotIn("request /unit:eta:", result.stdout)
        rendered = subprocess.run([sys.executable, str(RENDERER), str(self.output),
                                   "--at", "2026-10-06T23:45"],
                                  env=self.env, capture_output=True, text=True, check=False)
        self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)

    def test_eta_just_before_now_is_passed(self) -> None:
        self.status_lines(f"== {ALPHA}", "● Checking panel labels", "ETA 23:40")
        result = self.run_builder("--at", "2026-10-06T23:45")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.unit()["eta"], {"none": "none measured - requested"})
        self.assertEqual(result.stdout.count(f"request /unit:eta: {ALPHA}"), 1)

    def test_no_status_eta_keeps_only_judgment_none_text(self) -> None:
        self.status_lines(f"== {ALPHA}", "● Checking panel labels")
        self.judgment_file(alpha={"eta": {"time": "18:30", "percent": 60,
                                           "detail": "draft", "none": "none measured"}})
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.unit()["eta"], {"none": "none measured"})

    def test_session_gone_without_status_eta_discards_judgment_numbers(self) -> None:
        self.status_lines(f"== {ALPHA}", "SESSION GONE")
        self.judgment_file(alpha={"eta": {"time": "18:30", "percent": 60}})
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.unit()["eta"], {"none": "no ETA stated yet"})

    def test_eta_request_first_makes_no_eta_report_requested_without_repeating_request(self) -> None:
        self.status_lines(f"== {ALPHA}", "● Checking panel labels")
        requested = self.run_eta_request()
        self.assertEqual(requested.returncode, 0, requested.stdout + requested.stderr)
        self.assertEqual(requested.stdout.count(f"request /unit:eta: {ALPHA}"), 1)
        seen_path = self.state / "eta_seen.json"
        seen = cast(dict[str, dict[str, object]], json.loads(seen_path.read_text(encoding="utf-8")))
        self.assertEqual(seen[f"{ALPHA}|Phase 2 of 3: panel labels stay clear"], {"requested": True})
        report = self.run_builder()
        self.assertEqual(report.returncode, 0, report.stdout + report.stderr)
        self.assertEqual(self.unit()["eta"], {"none": "none measured - requested"})
        self.assertNotIn("request /unit:eta:", report.stdout)
        later = self.run_eta_request()
        self.assertEqual(later.returncode, 0, later.stdout + later.stderr)
        self.assertNotIn("request /unit:eta:", later.stdout)

    def test_request_recorded_during_review_watch_survives_builder_write_without_duplicate(self) -> None:
        beta_phase = "Phase 1 of 2: button labels stay clear"
        self.production_doc(beta=True)
        self.status_lines(f"== {ALPHA}", "● Checking panel labels", "ETA 23:40",
                          f"== {BETA}", "● Checking button labels", "ETA 16:40")
        self.judgment_file(beta=True)
        review = self.root / "race-review.py"
        _ = review.write_text(RACE_WATCH_STUB.replace("__PYTHON__", sys.executable), encoding="utf-8")
        review.chmod(0o755)
        self.env.update({
            "DAILIES_REVIEW_REGIME": str(review),
            "CI_POINTS_REVIEW_REGIME": str(review),
            "DAILIES_RACE_WAITING": str(WAITING),
            "DAILIES_RACE_UNIT": BETA,
            "DAILIES_RACE_PHASE": beta_phase,
            "DAILIES_RACE_PRODUCTION": str(self.doc),
            "DAILIES_RACE_STATE": str(self.state),
        })

        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(f"request /unit:eta: {BETA}", result.stdout)
        seen = cast(dict[str, dict[str, object]], json.loads((self.state / "eta_seen.json").read_text()))
        record = seen[f"{BETA}|{beta_phase}"]
        self.assertIs(record["requested"], True)
        self.assertEqual(record["text"], "ETA 16:40")

    def test_no_eta_report_first_allows_later_request_once_for_phase(self) -> None:
        self.status_lines(f"== {ALPHA}", "● Checking panel labels")
        first = self.run_builder()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.unit()["eta"], {"none": "no ETA stated yet"})
        self.assertNotIn("request /unit:eta:", first.stdout)
        requested = self.run_eta_request()
        self.assertEqual(requested.returncode, 0, requested.stdout + requested.stderr)
        self.assertEqual(requested.stdout.count(f"request /unit:eta: {ALPHA}"), 1)
        second = self.run_builder()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.unit()["eta"], {"none": "none measured - requested"})
        self.assertNotIn("request /unit:eta:", second.stdout)
        next_phase = "Phase 3 of 3: panel edges align"
        self.judgment_file(alpha={"phase": next_phase, "then": ["nothing queued"]})
        new_phase = self.run_builder()
        self.assertEqual(new_phase.returncode, 0, new_phase.stdout + new_phase.stderr)
        self.assertEqual(self.unit()["eta"], {"none": "no ETA stated yet"})

    def test_changed_passed_eta_requests_once_per_phase(self) -> None:
        self.status_lines(f"== {ALPHA}", "ETA 16:40")
        first = self.run_builder()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn(f"request /unit:eta: {ALPHA}", first.stdout)
        self.status_lines(f"== {ALPHA}", "ETA 16:50")
        changed = self.run_builder()
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        self.assertNotIn("request /unit:eta:", changed.stdout)
        self.judgment_file(alpha={"phase": "Phase 3 of 3: panel edges align", "then": ["nothing queued"]})
        next_phase = self.run_builder()
        self.assertEqual(next_phase.returncode, 0, next_phase.stdout + next_phase.stderr)
        self.assertIn(f"request /unit:eta: {ALPHA}", next_phase.stdout)

    def test_refused_judgment_defers_eta_record_and_request_until_accepted(self) -> None:
        self.status_lines(f"== {ALPHA}", "ETA 16:50")
        fields = cast(dict[str, object], json.loads(self.judgment.read_text(encoding="utf-8")))
        units = cast(list[dict[str, object]], fields["units"])
        del units[0]["project"]
        _ = self.judgment.write_text(json.dumps(fields), encoding="utf-8")
        refused = self.run_builder()
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertNotIn(f"request /unit:eta: {ALPHA}", refused.stdout)
        self.assertFalse((self.state / "eta_seen.json").exists())
        units[0]["project"] = "panels that stay readable"
        _ = self.judgment.write_text(json.dumps(fields), encoding="utf-8")
        retried = self.run_builder()
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertIn(f"request /unit:eta: {ALPHA}", retried.stdout)
        seen = cast(dict[str, dict[str, object]], json.loads((self.state / "eta_seen.json").read_text()))
        record = seen[f"{ALPHA}|Phase 2 of 3: panel labels stay clear"]
        self.assertEqual(record["first_seen"], AT)
        self.assertIs(record["requested"], True)

    def test_invalid_at_is_named_as_input_failure(self) -> None:
        message = self.assert_refused_without_output("--at", "not-a-clock", mention="--at")
        self.assertIn("input: failed", message)

    def test_missing_required_judgment_field_is_named_before_clock_move(self) -> None:
        self.judgment_file(alpha={"held": None})
        fields = cast(dict[str, object], json.loads(self.judgment.read_text(encoding="utf-8")))
        units = cast(list[dict[str, object]], fields["units"])
        del units[0]["project"]
        del units[0]["phase"]
        _ = self.judgment.write_text(json.dumps(fields), encoding="utf-8")
        message = self.assert_refused_without_output("--user-run", mention="units[0].project: required")
        self.assertIn("units[0].phase: required", message)
        self.assertEqual(self.notifier_events(), [])

    def test_then_string_is_refused_with_renderer_message_before_clock_move(self) -> None:
        self.judgment_file(alpha={"then": "Phase 3: panel edges align"})
        message = self.assert_refused_without_output("--user-run", mention="units[0].then: must be a list")
        self.assertIn(str(self.judgment), message)
        self.assertEqual(self.notifier_events(), [])

    def test_chained_then_item_is_refused_with_its_index_before_clock_move(self) -> None:
        self.judgment_file(alpha={"then": ["Phase 3: panel edges align, then Phase 4: edges darken"]})
        message = self.assert_refused_without_output("--user-run", mention="units[0].then[0]: one item names more than one phase")
        self.assertIn(str(self.judgment), message)
        self.assertEqual(self.notifier_events(), [])

    def test_moved_eta_without_reason_preserves_clock_output_and_eta_record(self) -> None:
        baseline = self.run_builder()
        self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)
        seen_path = self.state / "eta_seen.json"
        seen = cast(dict[str, dict[str, object]], json.loads(seen_path.read_text(encoding="utf-8")))
        key = f"{ALPHA}|Phase 2 of 3: panel labels stay clear"
        seen[key]["first_seen"] = "2026-10-06T14:00"
        seen[key]["requested"] = False
        _ = seen_path.write_text(json.dumps(seen, separators=(",", ":")) + "\n", encoding="utf-8")
        before_seen = seen_path.read_bytes()
        _ = self.output.write_text("sentinel\n", encoding="utf-8")
        render_state = self.root / "render-state.json"
        old = {"phase": "Phase 2 of 3: panel labels stay clear", "eta": "2026-10-06T20:00:00",
               "eta_text": "20:00",
               "held": None, "first": "2026-10-06T20:00:00"}
        _ = render_state.write_text(json.dumps({ALPHA: old}), encoding="utf-8")
        self.events.unlink()

        refused = self.run_builder("--user-run", "--render-state", str(render_state))
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("judgment: failed", refused.stdout)
        self.assertIn("units[0].eta.why", refused.stdout)
        self.assertEqual(self.notifier_events(), [])
        self.assertEqual(self.output.read_text(encoding="utf-8"), "sentinel\n")
        self.assertEqual(seen_path.read_bytes(), before_seen)
        self.assertNotIn("request /unit:eta:", refused.stdout)

        self.judgment_file(alpha={"eta": {"percent": 60, "why": "the unit found another panel repair"}})
        accepted = self.run_builder("--user-run", "--render-state", str(render_state))
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertEqual(self.notifier_events(), [["restart", "showrunner-example"]])
        self.assertEqual(cast(dict[str, object], self.unit()["eta"])["why"],
                         "the unit found another panel repair")

    def timer(self, slug: str) -> None:
        """A production with a complete update timer, so the tree counts it."""
        instance = self.root / "notifier-state" / f"showrunner-{slug}"
        instance.mkdir(parents=True)
        _ = (instance / "conf").write_text(
            f"TARGET=session:{slug}-session\nCHECK=zsh /x/production_check.sh {self.doc}\n", encoding="utf-8")

    def attach(self, child: str, parent: str) -> None:
        for slug in (child, parent):
            if not (self.root / "notifier-state" / f"showrunner-{slug}").exists():
                self.timer(slug)
        edge = self.showrunner_state / child / "parent"
        edge.parent.mkdir(parents=True, exist_ok=True)
        _ = edge.write_text(parent + "\n", encoding="utf-8")

    def saved_report(self, slug: str) -> Path:
        return self.showrunner_state / slug / "dailies_last.json"

    def write_saved_report(self, slug: str, **changed: object) -> None:
        fields: dict[str, object] = {
            "written": "2026-10-06T16:50", "zone": "America/Los_Angeles", "interval": 15, "units": 3, "held": 1,
            "rows": [{"name": "fps", "started": "2026-10-06T15:10", "eta": "2026-10-06T19:20",
                      "earliest": "2026-10-06T19:20", "latest": "2026-10-06T19:20"}, {"name": "docs"}],
            **changed}
        self.saved_report(slug).parent.mkdir(parents=True, exist_ok=True)
        _ = self.saved_report(slug).write_text(json.dumps(fields), encoding="utf-8")

    def write_open_for(self, slug: str, content: str) -> None:
        path = self.showrunner_state / "outstanding" / f"{slug}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content, encoding="utf-8")

    def children(self) -> list[dict[str, object]]:
        return cast(list[dict[str, object]], self.report()["children"])

    def test_every_run_saves_what_the_showrunner_above_shows(self) -> None:
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"saved report: ok — wrote {self.saved_report('example')}", result.stdout)
        saved = cast(dict[str, object], json.loads(self.saved_report("example").read_text(encoding="utf-8")))
        self.assertEqual({key: saved[key] for key in ("written", "zone", "interval", "units", "held")},
                         {"written": AT, "zone": "America/Los_Angeles", "interval": 15, "units": 1, "held": 0})
        rows = cast(list[dict[str, object]], saved["rows"])
        self.assertEqual([(row["name"], row["eta"]) for row in rows], [("alpha", "2026-10-06T23:40")])
        # Alone, a production's input names nothing above or below it.
        self.assertNotIn("children", self.report())
        self.assertNotIn("reports_to", self.report())
        self.assertEqual([path.name for path in self.saved_report("example").parent.iterdir()], ["dailies_last.json"])

    def test_a_production_that_reports_on_demand_saves_no_interval(self) -> None:
        doc = self.doc.read_text(encoding="utf-8")
        _ = self.doc.write_text(doc.replace("- **Updates:** every 15 minutes", "- **Updates:** on demand"),
                                encoding="utf-8")
        self.assertEqual(self.run_builder().returncode, 0)
        saved = cast(dict[str, object], json.loads(self.saved_report("example").read_text(encoding="utf-8")))
        self.assertIsNone(saved["interval"])

    def style_ledger_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        ledger = SCRIPT.parent.parent / "style/style_ledger.py"
        result = subprocess.run([sys.executable, str(ledger), *arguments, "--repo", str(self.checkout)],
                                env=self.env, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def prepare_style_ledger(self) -> dict[str, object]:
        rules = Path(self.env["STYLE_HISTORY_NATE_STYLE_DIR"]) / "rust"
        rules.mkdir(parents=True)
        _ = (rules / "names.md").write_text(
            "---\ntier: C\nfamily: names\ninput_scope: file\n---\n# Names\n", encoding="utf-8")
        _ = (self.checkout / "Cargo.toml").write_text(
            '[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n', encoding="utf-8")
        (self.checkout / "src").mkdir()
        _ = (self.checkout / "src/lib.rs").write_text("pub fn value() {}\n", encoding="utf-8")
        _ = self.git("add", "Cargo.toml", "src/lib.rs")
        _ = self.git("-c", "commit.gpgsign=false", "commit", "-m", "Add sample crate")
        _ = self.style_ledger_cli("set-tip", "--tip", self.git("rev-parse", "HEAD"))
        return cast(dict[str, object], json.loads(self.style_ledger_cli("summary", "--json").stdout))

    def mark_style_production(self) -> None:
        content = self.doc.read_text(encoding="utf-8")
        _ = self.doc.write_text(content.replace("## Production Context", "## Production Context\n\n- **Kind:** style"),
                                encoding="utf-8")

    def test_style_summary_is_saved_only_for_a_style_production_and_keeps_every_ledger_key(self) -> None:
        summary = self.prepare_style_ledger()
        original_doc = self.doc.read_text(encoding="utf-8")
        for kind_line in ("", "- **Kind:** product\n"):
            with self.subTest(kind_line=kind_line):
                _ = self.doc.write_text(original_doc.replace("## Production Context",
                                       "## Production Context\n\n" + kind_line), encoding="utf-8")
                product = self.run_builder()
                self.assertEqual(product.returncode, 0, product.stdout + product.stderr)
                product_saved = cast(dict[str, object], json.loads(self.saved_report("example").read_text(encoding="utf-8")))
                self.assertNotIn("style", product_saved)
        _ = self.doc.write_text(original_doc, encoding="utf-8")
        self.mark_style_production()
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = cast(dict[str, object], json.loads(self.saved_report("example").read_text(encoding="utf-8")))
        self.assertEqual(saved["style"], summary)
        self.assertEqual(summary["rules_total"], 1)
        parsed = dailies_render.parse_child_report(saved, "saved", ZoneInfo("America/Los_Angeles")).style
        self.assertIsInstance(parsed, dailies_render.StyleSummary)
        assert isinstance(parsed, dailies_render.StyleSummary)
        self.assertIsInstance(parsed.waiting, dailies_render.NoPendingHandoffs)
        self.assertIsNone(parsed.synced_at.tzinfo)
        # Domain times are local datetimes; compare their UTC instants to the ledger's JSON.
        parsed_keys: dict[str, object] = {
            "rules_done": parsed.rules_done, "rules_total": parsed.rules_total,
            "unclassified_rules": parsed.unclassified_rules, "cells_current_pct": parsed.cells_current_pct,
            "stale": parsed.stale, "proposals": parsed.proposals,
            "waiting_to_merge_back": 0, "oldest_waiting": None,
            "synced_at": parsed.synced_at.replace(tzinfo=ZoneInfo("America/Los_Angeles")).astimezone(ZoneInfo("UTC")),
            "synced_tip": parsed.synced_tip,
        }
        expected = {**summary, "synced_at": datetime.fromisoformat(cast(str, summary["synced_at"]))}
        self.assertEqual(parsed_keys, expected)
        _ = self.output.write_text(json.dumps({
            "length": "simple", "zone": "America/Los_Angeles", "units": [],
            "children": [{"name": "hana-style", "open": 0, "report": saved}],
        }), encoding="utf-8")
        rendered = self.run_renderer(AT)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertIn("hana-style  rules done 0/1 · cells 0% · stale 0 · proposals 0 · synced ", rendered.stdout)
        self.assertNotIn("hand-off waiting", rendered.stdout)

    def test_unsent_handoff_age_from_the_ledger_flags_the_saved_style_report(self) -> None:
        _ = self.prepare_style_ledger()
        tip = self.git("rev-parse", "HEAD")
        _ = self.style_ledger_cli("handoff", "add", "--checkpoint", tip)
        pending = cast(dict[str, object], json.loads(self.style_ledger_cli("handoff", "pending").stdout))
        self.assertEqual(pending["state"], "pending")
        summary = cast(dict[str, object], json.loads(self.style_ledger_cli("summary", "--json").stdout))
        self.mark_style_production()
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = cast(dict[str, object], json.loads(self.saved_report("example").read_text(encoding="utf-8")))
        self.assertEqual(saved["style"], summary)
        parsed = dailies_render.parse_child_report(saved, "saved", ZoneInfo("America/Los_Angeles")).style
        assert isinstance(parsed, dailies_render.StyleSummary)
        self.assertIsInstance(parsed.waiting, dailies_render.PendingHandoffs)
        assert isinstance(parsed.waiting, dailies_render.PendingHandoffs)
        self.assertEqual(parsed.waiting.count, 1)
        created = datetime.fromisoformat(cast(str, summary["oldest_waiting"]))
        local_created = created.astimezone(ZoneInfo("America/Los_Angeles"))
        self.assertEqual(parsed.waiting.oldest_pending_since, local_created)
        _ = self.output.write_text(json.dumps({
            "length": "simple", "zone": "America/Los_Angeles", "units": [],
            "children": [{"name": "hana-style", "open": 0, "report": saved}],
        }), encoding="utf-8")
        render_at = (created + timedelta(hours=5, minutes=1)).astimezone(ZoneInfo("America/Los_Angeles"))
        rendered = self.run_renderer(render_at.strftime("%Y-%m-%dT%H:%M"))
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertIn(" · hand-off waiting 5h", rendered.stdout)

    def test_failed_or_invalid_summary_saves_unavailable_and_the_report_still_succeeds(self) -> None:
        self.mark_style_production()
        original_command = dailies_input.command
        for code, output, error in ((2, "", "ledger database is locked"),
                                    (0, "not JSON", ""),
                                    (0, "[]", ""),
                                    (0, '{"rules_done": 1}', "")):
            with self.subTest(code=code, output=output):
                def command(path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
                    if path.name == "style_ledger.py":
                        self.assertEqual(arguments, ("summary", "--repo", str(self.checkout), "--json"))
                        return subprocess.CompletedProcess([str(path), *arguments], code, output, error)
                    return original_command(path, *arguments)

                with mock.patch.object(dailies_input, "command", side_effect=command):
                    result = self.run_builder()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                saved = cast(dict[str, object], json.loads(self.saved_report("example").read_text(encoding="utf-8")))
                unavailable = cast(dict[str, object], saved["style"])
                self.assertEqual(set(unavailable), {"unavailable"})
                self.assertIsInstance(unavailable["unavailable"], str)
                self.assertTrue(unavailable["unavailable"])
                if error:
                    self.assertEqual(unavailable, {"unavailable": error})
                parsed = dailies_render.parse_child_report(saved, "saved", ZoneInfo("America/Los_Angeles"))
                self.assertIsInstance(parsed.style, dailies_render.StyleSummaryUnavailable)

    def test_a_production_below_is_read_into_the_input_with_what_waits_on_the_user(self) -> None:
        self.attach("kid", "example")
        self.attach("leaf", "kid")
        self.write_saved_report("kid")
        self.write_open_for("kid", json.dumps([{"since": "2026-10-06T09:00", "text": "pick a licence"},
                                               {"since": "2026-10-06T09:00", "text": "later",
                                                "after": "2026-10-07T09:00"}]))
        self.write_open_for("leaf", json.dumps([{"since": "2026-10-06T10:00", "text": "name the release"}]))
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("children: ok — 1 below: kid", result.stdout)
        kid = self.children()[0]
        self.assertEqual({key: kid[key] for key in ("name", "open", "expand")},
                         {"name": "kid", "open": 2, "expand": False})
        self.assertEqual(cast(dict[str, object], kid["report"])["units"], 3)
        self.assertNotIn("unreadable", kid)
        # Its own saved report takes the lower production's counts in, and one row for it.
        saved = cast(dict[str, object], json.loads(self.saved_report("example").read_text(encoding="utf-8")))
        self.assertEqual((saved["units"], saved["held"]), (4, 1))
        self.assertEqual([row["name"] for row in cast(list[dict[str, object]], saved["rows"])], ["alpha", "kid"])
        rendered = self.run_renderer(AT)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        row = next(line for line in rendered.stdout.splitlines() if line.startswith("kid "))
        self.assertTrue(row.endswith(" 19:20  3 units, 1 held, 2 open questions"), row)
        self.assertNotIn("nothing needed", rendered.stdout)

    def test_expand_names_a_production_directly_below_or_all(self) -> None:
        self.attach("kid", "example")
        self.attach("other", "example")
        self.attach("leaf", "kid")
        self.write_saved_report("kid")
        self.assertEqual(self.run_builder("--expand", "kid").returncode, 0)
        self.assertEqual([(child["name"], child["expand"]) for child in self.children()],
                         [("kid", True), ("other", False)])
        rendered = self.run_renderer(AT)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertEqual([line.split()[0] for line in rendered.stdout.splitlines() if line.startswith(("kid", "other"))],
                         ["kid", "kid/fps", "kid/docs", "other"])
        self.assertEqual(self.run_builder("--expand", "all").returncode, 0)
        self.assertEqual([child["expand"] for child in self.children()], [True, True])
        refused = self.run_builder("--expand", "leaf")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("expand: failed — leaf: not directly below example; choose from kid, other or all",
                      refused.stdout)

    def test_expand_is_refused_when_nothing_is_below(self) -> None:
        refused = self.run_builder("--expand", "all")
        self.assertEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        named = self.run_builder("--expand", "kid")
        self.assertEqual(named.returncode, 2)
        self.assertIn("expand: failed — kid: not directly below example; no production reports to it", named.stdout)

    def test_a_lower_production_s_bad_files_cost_its_row_and_never_the_report(self) -> None:
        self.attach("kid", "example")
        self.attach("leaf", "kid")
        self.write_saved_report("kid", written="yesterday")
        self.write_open_for("leaf", "{not a list")
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("children: unavailable — kid: ", result.stdout)
        self.assertIn(".written: 'yesterday' is not YYYY-MM-DDTHH:MM", result.stdout)
        kid = self.children()[0]
        self.assertNotIn("report", kid)
        self.assertEqual((kid["open"], kid["unreadable"]), (0, ["leaf"]))
        rendered = self.run_renderer(AT)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertIn("kid      ?  no report yet", rendered.stdout)
        self.assertIn("* leaf: needs-user list unreadable", rendered.stdout)
        self.assertNotIn("nothing needed", rendered.stdout)

    def test_a_showrunner_under_a_root_names_it_in_its_input(self) -> None:
        self.attach("example", "middle")
        self.attach("middle", "top")
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.report()["reports_to"], "top")
        rendered = self.run_renderer(AT)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertIn("* reports to top", rendered.stdout.splitlines())

    def test_a_saved_report_that_cannot_be_written_never_fails_the_run(self) -> None:
        self.showrunner_state.mkdir(parents=True)
        _ = (self.showrunner_state / "example").write_text("a file where the folder goes\n", encoding="utf-8")
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("saved report: unavailable — ", result.stdout)
        self.assertTrue(self.output.exists())


if __name__ == "__main__":
    _ = unittest.main()
