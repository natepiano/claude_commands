"""Build dailies input from disposable production state and status output."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast, override
from zoneinfo import ZoneInfo

from dailies_input import (Activity, Block, ClaudeNotRunning, Decision, FormWaiting,
                           Running, SessionGone, StillWaiting, TicksFailing, UnitRow,
                           status_blocks)


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
if Path(sys.argv[0]).name == "pushover.py":
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


class DailiesInputTests(unittest.TestCase):
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
    env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / "home"
        self.home.mkdir()
        self.origin = self.root / "origin.git"
        _ = subprocess.run(["git", "init", "--bare", str(self.origin)],
                           check=True, capture_output=True, text=True)
        self.checkout = self.root / "merge"
        self.checkout.mkdir()
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
        _ = self.notifier.write_text(NOTIFIER.replace("__PYTHON__", sys.executable), encoding="utf-8")
        self.notifier.chmod(0o755)
        self.env = {**os.environ, "HOME": str(self.home), "BUILD_HOLD_DIR": str(self.holders),
                    "DAILIES_TEST_EVENTS": str(self.events), "DAILIES_TEST_FAIL": str(self.notifier_failure)}
        _ = subprocess.run(["git", "init", "-b", "production", str(self.checkout)],
                           check=True, capture_output=True, text=True)
        _ = self.git("config", "user.name", "Dailies Test")
        _ = self.git("config", "user.email", "dailies@example.invalid")
        _ = self.git("remote", "add", "origin", str(self.origin))
        _ = (self.checkout / "README.md").write_text("base\n", encoding="utf-8")
        _ = self.git("add", "README.md")
        _ = self.git("commit", "-m", "initial")
        _ = self.git("push", "-u", "origin", "production")
        self.production_doc()
        self.status_lines(f"== {ALPHA}", "● Checking panel labels", "ETA 23:40")
        self.judgment_file()

    def git(self, *arguments: str) -> str:
        result = subprocess.run(["git", "-C", str(self.checkout), *arguments],
                                check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def production_doc(self, *, beta: bool = False) -> None:
        rows = [
            "# Production — example", "", "## Production Context", "",
            "- **Merge branch:** `production`",
            f"- **Showrunner checkout:** `{self.checkout}`",
            "- **Showrunner session:** test-showrunner",
            "- **Log:** `production.log`",
            "- **User zone:** America/Los_Angeles",
            "- **Updates:** every 15 minutes", "",
            "## Units", "",
            "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            f"| `{ALPHA}` | `docs/alpha.md` | `{self.root / 'alpha'}` | `alpha` | `{ALPHA}` | — | — |",
        ]
        if beta:
            rows.append(f"| `{BETA}` | `docs/beta.md` | `{self.root / 'beta'}` | `beta` | `{BETA}` | — | — |")
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

    def run_builder(self, *extra: str, include_render_state: bool = True) -> subprocess.CompletedProcess[str]:
        state_args = (
            ("--render-state", str(self.root / "render-state.json"))
            if include_render_state and "--render-state" not in extra else ()
        )
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--production", str(self.doc),
             "--status", str(self.status), "--judgment", str(self.judgment),
             "--state-dir", str(self.state), "--out", str(self.output),
             "--holders", str(self.holders), "--notifier", str(self.notifier),
             "--at", AT, *state_args, *extra],
            cwd=self.checkout, env=self.env, capture_output=True, text=True, check=False,
        )

    def report(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.output.read_text(encoding="utf-8")))

    def run_renderer(self, at: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RENDERER), str(self.output), "--at", at,
             "--state", str(self.root / "render-state.json"),
             "--log", str(self.root / "dailies.log")],
            cwd=self.checkout, env=self.env, capture_output=True, text=True, check=False,
        )

    def unit(self) -> dict[str, object]:
        return cast(dict[str, object], cast(list[object], self.report()["units"])[0])

    def notifier_events(self) -> list[list[str]]:
        if not self.events.exists():
            return []
        return [cast(list[str], json.loads(line)) for line in self.events.read_text(encoding="utf-8").splitlines()]

    def watch_stubs(self) -> Path:
        bin_path = self.root / "bin"
        bin_path.mkdir()
        events = self.root / "watch-events.jsonl"
        for name in ("review_regime.py", "pushover.py"):
            stub = bin_path / name
            _ = stub.write_text(WATCH_STUB.replace("__PYTHON__", sys.executable), encoding="utf-8")
            stub.chmod(0o755)
        push = self.home / ".claude/scripts/notify/pushover.py"
        push.parent.mkdir(parents=True, exist_ok=True)
        _ = push.write_text(WATCH_STUB.replace("__PYTHON__", sys.executable), encoding="utf-8")
        push.chmod(0o755)
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
                              capture_output=True, text=True, check=False, timeout=25)

    def run_eta_request(self, phase: str = "Phase 2 of 3: panel labels stay clear") -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(WAITING), "eta-request", ALPHA,
                               "--phase", phase, "--production", str(self.doc),
                               "--state-dir", str(self.state)], cwd=self.checkout, env=self.env,
                              capture_output=True, text=True, check=False, timeout=25)

    def rename_unit(self, name: str) -> None:
        for path in (self.doc, self.status, self.judgment):
            _ = path.write_text(path.read_text(encoding="utf-8").replace(ALPHA, name), encoding="utf-8")

    def parsed_block(self) -> tuple[Running | SessionGone | ClaudeNotRunning,
                                    tuple[FormWaiting | Decision | StillWaiting | Block | TicksFailing, ...],
                                    tuple[Activity, ...]]:
        block = status_blocks(self.status, (UnitRow(ALPHA, ALPHA),))[0]
        return block.state, block.flags, block.activity

    def assert_refused_without_output(self, *extra: str, mention: str) -> str:
        _ = self.output.write_text("sentinel\n", encoding="utf-8")
        result = self.run_builder(*extra)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(mention, result.stdout + result.stderr)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "sentinel\n")
        return result.stdout + result.stderr

    def test_each_length_produces_input_the_renderer_accepts(self) -> None:
        for length in ("simple", "page", "elaborate"):
            with self.subTest(length=length):
                result = self.run_builder("--length", length)
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
            mention="no live units: every Units row is marked retired")

    def test_short_session_without_judgment_label_uses_renderer_default(self) -> None:
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("label", self.unit())

    def test_render_state_is_required_before_notifier_or_output(self) -> None:
        _ = self.output.write_text("sentinel\n", encoding="utf-8")
        result = self.run_builder(include_render_state=False)
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
        self.assertTrue(units[1]["build_hold"])

    def test_holder_with_no_other_unit_is_refused(self) -> None:
        _ = (self.holders / ALPHA).write_text(json.dumps({"holder": ALPHA,
            "since": "2026-10-06T09:55:00-07:00", "for": "the panel timing check",
            "release_eta": "unknown"}) + "\n", encoding="utf-8")
        _ = self.assert_refused_without_output(mention="build_hold")

    def test_build_hold_marker_without_holder_file_is_refused(self) -> None:
        self.judgment_file(alpha={"build_hold": True})
        _ = self.assert_refused_without_output(mention="build_hold")

    def test_user_run_restarts_notifier_once_and_logs_next_due(self) -> None:
        result = self.run_builder("--user-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.notifier_events(), [["restart", "showrunner-example"]])
        self.assertEqual(self.report()["next_run"], "17:55")
        self.assertIn("next_due=", (self.checkout / "production.log").read_text(encoding="utf-8"))

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

    def test_scheduled_prompt_saves_complete_status_for_dailies_simple(self) -> None:
        commands = SCRIPT.parents[2] / "commands/showrunner"
        produce = (commands / "produce.md").read_text(encoding="utf-8")
        dailies = (commands / "dailies.md").read_text(encoding="utf-8")
        prompt = produce.split("The prompt:", 1)[1].split("**A tick**", 1)[0]
        self.assertIn("> <SCRATCH>/unit_status.txt", prompt)
        self.assertNotIn("cut -c1-400", prompt)
        self.assertIn("/showrunner:dailies simple", prompt)
        self.assertIn("unit_status.txt", dailies)
        self.assertIn("/showrunner:dailies simple", dailies)

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

    def test_dailies_first_review_alert_marks_topic_and_merge_watch_repeats(self) -> None:
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
        self.assertEqual(sum(row[0] == "pushover.py" for row in calls), 1)
        self.assertEqual((self.checkout / "production.log").read_text().count("review watch"), 1)

    def test_merge_first_review_alert_is_not_repeated_by_dailies(self) -> None:
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
        self.assertEqual(sum(row[0] == "pushover.py" for row in calls), 1)
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

    def test_held_unchanged_eta_keeps_one_moment_through_builder_and_renderer(self) -> None:
        self.status_lines(f"== {ALPHA}", "● Checking panel labels", "ETA 19:35")
        self.judgment_file(alpha={
            "held": "the panel review is paused",
            "eta": {"percent": 60},
        })
        baseline = self.run_builder("--at", "2026-10-06T19:30")
        self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)
        first_report = self.run_renderer("2026-10-06T19:30")
        self.assertEqual(first_report.returncode, 0, first_report.stdout + first_report.stderr)

        for at in ("2026-10-06T20:00", "2026-10-06T21:36", "2026-10-06T23:50"):
            with self.subTest(at=at):
                built = self.run_builder("--at", at)
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


if __name__ == "__main__":
    _ = unittest.main()
