#!/usr/bin/env python3
"""Integration tests for the durable unit progress history."""

from __future__ import annotations

from scripts.lib.test_agents_registry import write_registry

import argparse
import hashlib
import io
import json
import os
import shlex
import select
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO, cast, override
from unittest.mock import patch

from scripts.direct import phase_reservation, phase_table, plan_history, progress_history, recorder_state
# These regressions check the private verifier's semantic result before wait closure.
from scripts.direct.progress_history import _supplier_message  # pyright: ignore[reportPrivateUsage]
from scripts.direct.recorder_fixture import invoke_recorder
from scripts.history.rounds import load_phases
from scripts.history import history as stage_history


SCRIPT = Path(__file__).with_name("progress_history.py")
FINDINGS = Path(__file__).with_name("findings.py")
BOARD = Path(__file__).with_name("board.sh")
# Four reports per finished phase, as (raw percent, percent of the phase's time
# already spent): the shape of startup-polish Phase 5's history on 2026-10-03.
# Looked up at the exact number, each raw percent answers with its own time
# spent -- 78 says 40, 82 says 20, 84 says 55, 86 says 15 -- so four ticks a few
# points apart drew four ETAs hours apart.
SWING_REPORTS: tuple[tuple[int, int], ...] = ((86, 15), (82, 20), (78, 40), (84, 55))
SUMMARY_HEADER = ["Scope", "%", "Elapsed", "ETA", "Unchanged", "ETA low", "ETA high"]


def recorder_parser() -> argparse.ArgumentParser:
    builder = vars(progress_history).get("_build_parser")
    if not callable(builder):
        raise AssertionError("progress recorder parser is unavailable")
    return cast(Callable[[], argparse.ArgumentParser], builder)()


def recorder_handler(name: str) -> Callable[[argparse.Namespace], None]:
    handler = vars(progress_history).get(name)
    if not callable(handler):
        raise AssertionError(f"progress recorder handler is unavailable: {name}")
    return cast(Callable[[argparse.Namespace], None], handler)


def refresh_phase_table(session_dir: Path) -> None:
    refresh = vars(progress_history).get("_refresh_phase_table")
    if not callable(refresh):
        raise AssertionError("progress recorder phase-table refresh is unavailable")
    cast(Callable[[Path], None], refresh)(session_dir)


class ProgressHistoryTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    history_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    working_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    config_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    agents_config_file: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    team_slot: str  # pyright: ignore[reportUninitializedInstanceVariable]
    last_recorder_stderr: str = ""

    @override
    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {"UNIT_SYNC_WRITES": "0"}))
        # Socket fixtures need a short path even when the run's TMPDIR is long.
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.temporary.name)
        self.history_dir = self.root / "history"
        self.working_dir = self.root / "bevy_hana_rubric"
        self.working_dir.mkdir()
        # Pin the report interval and the timezone: without both, the clock line
        # renders from the machine's own direct.conf and local offset, and the
        # expected header would differ per machine.
        self.config_file = self.root / "direct.conf"
        self.write_interval(180)
        # The temporary home's registry path. Named so the digest hashes a file
        # the test controls rather than the machine's own agents.conf, which
        # would differ per machine and per registry edit. write_agents_registry
        # writes to this same path: one file, so arm-review resolves the agent
        # the test wrote instead of reading an empty one beside it.
        self.team_slot = ""
        self.agents_config_file = self.root / ".claude" / "config" / "agents.conf"
        _ = write_registry(self.agents_config_file)
        _ = subprocess.run(
            ["git", "init", "-b", "feature/rubric"],
            cwd=self.working_dir,
            check=True,
            capture_output=True,
            text=True,
        )

    @override
    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_review_trial_counts_rerun_findings_as_code(self) -> None:
        self.write_full_config()
        session = self.start_run("rerun-findings", 71_000)
        self.start_phase(session, 71_010)
        _ = self.run_findings(session, "open", "--severity", "minor", "--title", "Claim blocks retry",
                              "--caught-by", "worker", "--lens", "rerun", at=71_020)
        self.assertEqual(self.run_command("review-trial", "--session-dir", str(session), at=71_030),
                         "review trial: ux 0 findings, code 1 findings, review-worker minutes 0, "
                         + "ux check minutes 0, ux repair minutes 0")

    def test_production_resolver_accepts_both_plan_header_forms(self) -> None:
        plan = self.working_dir / "unit-plan.md"
        for header in ("**Production: Example**", "**Production:** Example"):
            for field in ("project_plan_doc", "plan_doc"):
                with self.subTest(header=header, field=field):
                    _ = plan.write_text(f'> {header} — unit `unit-a`; production doc `production.md`\n')
                    identity = progress_history.resolve_production_unit({
                        "working_dir": str(self.working_dir), field: plan.name,
                    })
                    self.assertEqual(identity, progress_history.ProductionUnitIdentity("Example", "unit-a"))

    def test_set_unit_rebinds_ledger_identity_without_changing_run_history(self) -> None:
        run = self.start_run("identity", 1760658000)
        state = run / "progress_history_state.json"
        before = cast(dict[str, object], json.loads(state.read_text()))
        _ = self.run_command("set-unit", "--session-dir", str(run), "--production", "receiving",
                             "--unit", "builders", at=1760658001)
        after = cast(dict[str, object], json.loads(state.read_text()))
        self.assertEqual(after.pop("production"), "receiving")
        self.assertEqual(after.pop("unit"), "builders")
        self.assertEqual(after, before)

    def test_event_write_syncs_by_default_and_flushes_without_sync_for_disposable_state(self) -> None:
        append = cast(Callable[[dict[str, object], dict[str, object]], None], vars(progress_history)["_append_event"])
        history = self.root / "sync-event.jsonl"

        def flushed(descriptor: int) -> None:
            self.assertGreaterEqual(descriptor, 0)
            self.assertTrue(history.read_text())

        for durable in (True, False):
            with self.subTest(durable=durable), patch.dict(os.environ), patch.object(os, "fsync", side_effect=flushed) as sync:
                if durable:
                    _ = os.environ.pop("UNIT_SYNC_WRITES", None)
                else:
                    os.environ["UNIT_SYNC_WRITES"] = "0"
                append({"history_file": str(history)}, {"event_type": "probe"})
                self.assertEqual(sync.call_count, int(durable))
                self.assertEqual(json.loads(history.read_text()), {"event_type": "probe"})
            history.unlink()

    def write_interval(self, seconds: object) -> None:
        _ = self.config_file.write_text(
            f"UNIT_PROGRESS_INTERVAL_SECONDS={seconds}\nROUTING_FROM_ROUND=4\nASTRA_AFTER_FAILED_FIXES=2\n",
            encoding="utf-8",
        )

    def test_cold_recorder_calls_use_each_home_and_restore_the_callers_configuration(self) -> None:
        caller = self.root / "caller"
        caller.mkdir()
        program = r'''import fcntl, hashlib, json, os, signal, sys
from pathlib import Path
from unittest.mock import patch
from scripts.direct.recorder_fixture import invoke_recorder
from scripts.direct import progress_history
from scripts.lib import user_zone

root, working = map(Path, sys.argv[1:])
caller = (os.environ.copy(), Path.cwd(), sys.argv.copy(), sys.path.copy(),
          progress_history.DEFAULT_CONFIG_PATH, progress_history.DEFAULT_AGENTS_CONFIG_PATH,
          user_zone.CONF, {sig: signal.getsignal(sig) for sig in
                          (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)})
plan = working / 'cold-plan.md'
plan.write_text('## Delegation Context\n\n- **Project:** Cold recorder\n')
for number in (1, 2):
    home = root / str(number)
    config = home / '.claude/config'
    config.mkdir(parents=True)
    settings = config / 'direct.conf'
    registry = config / 'agents.conf'
    settings.write_text(f'UNIT_PROGRESS_INTERVAL_SECONDS={number * 60}\n')
    registry.write_text(f'registry-{number}\n')
    environment = {**os.environ, 'HOME': str(home), 'USER_ZONE': 'UTC',
                   'UNIT_SYNC_WRITES': '0', 'UNIT_PASS_OWNER': 'launcher',
                   'UNIT_NOW_EPOCH': '1000'}
    for key in ('UNIT_CONFIG', 'AGENTS_CONFIG_FILE', 'UNIT_HISTORY_DIR',
                'CODEX_THREAD_ID', 'CLAUDE_CODE_SESSION_ID'):
        environment.pop(key, None)
    session = home / 'session'
    commands = (
        ['start-run', '--session-dir', str(session), '--working-dir', str(working),
         '--plan-doc', str(plan), '--main-model', 'fixture-model'],
        ['start-phase', '--session-dir', str(session), '--phase-id', '1', '--phase-title', 'First'],
        ['start-pass', '--session-dir', str(session), '--phase-id', '1', '--pass-kind', 'impl', '--activity', 'writing',
         '--called-task', 'direct.implementation', '--called-family', 'codex',
         '--called-model', 'fixture-model'],
    )
    for arguments in commands:
        with patch.object(fcntl, 'flock', wraps=fcntl.flock) as lock:
            result = invoke_recorder(arguments, environment, working, check=True)
        assert [call.args[1] for call in lock.call_args_list][0] == fcntl.LOCK_EX
        assert lock.call_args_list[-1].args[1] == fcntl.LOCK_UN
        assert (session / '.progress_history.lock').exists()
    history, = (home / '.local/state/unit-history/runs').glob('*.jsonl')
    events = [json.loads(line) for line in history.read_text().splitlines()]
    started, = [event for event in events if event['event_type'] == 'pass_started']
    assert started['config_digest'] == hashlib.sha256(registry.read_bytes() + settings.read_bytes()).hexdigest()[:12]
    assert progress_history._config_digest.cache_info().currsize == 0
    restored = (os.environ.copy(), Path.cwd(), sys.argv.copy(), sys.path.copy(),
                progress_history.DEFAULT_CONFIG_PATH, progress_history.DEFAULT_AGENTS_CONFIG_PATH,
                user_zone.CONF, {sig: signal.getsignal(sig) for sig in caller[-1]})
    assert restored == caller, (restored, caller)
print('two homes isolated; recorder lock retained; caller restored')
'''
        environment = {**os.environ, "HOME": str(caller),
                       "USER_ZONE_CONF": str(caller / "zone.conf")}
        result = subprocess.run([sys.executable, "-c", program, str(self.root), str(self.working_dir)],
                                env=environment, capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("two homes isolated; recorder lock retained; caller restored", result.stdout)

    def isolate_identity(self, environment: dict[str, str]) -> None:
        """Point unit-director detection at the temporary home, not this machine's.

        Every window opening re-detects the unit director, and left alone that
        reads the real session transcript, so the recorded identity would be
        whichever model happens to be running the tests.
        """
        environment["HOME"] = str(self.root)
        _ = environment.pop("CODEX_THREAD_ID", None)
        _ = environment.pop("CLAUDE_CODE_SESSION_ID", None)

    def isolate_recorder_roots(self, environment: dict[str, str]) -> None:
        """Keep every recorder integration inside this test's temporary root."""
        environment["PHASE_TABLE_VAULT"] = str(
            self.root / "vault" / "showrunners"
        )
        environment["NOTIFIER_STATE_DIR"] = str(self.root / "notifier")
        environment["NOTIFIER_SESSIONS_DIR"] = str(
            self.root / "notifier-sessions"
        )

    def valid_launch_arguments(self, arguments: tuple[str, ...]) -> tuple[str, ...]:
        """Give older recorder cases the phase and failed-gate context they already arrange."""
        if arguments and arguments[0] == "start-pass":
            if "--phase-id" not in arguments:
                session = Path(arguments[arguments.index("--session-dir") + 1])
                state_file = session / "progress_history_state.json"
                if state_file.exists():
                    state = cast(dict[str, object], json.loads(state_file.read_text()))
                    phase = cast(dict[str, object], state.get("phase") or {})
                    if phase.get("id"):
                        arguments = (*arguments, "--phase-id", str(phase["id"]))
            if "--pass-kind" in arguments and arguments[arguments.index("--pass-kind") + 1] == "fix":
                if "--fix-cause" not in arguments:
                    arguments = (*arguments, "--fix-cause", "fixture failed gate")
        return arguments

    def run_command(
        self,
        *arguments: str,
        at: int,
        claude_session: str = "",
        codex_session: str = "",
        zone: str = "UTC",
    ) -> str:
        environment = os.environ.copy()
        environment["UNIT_HISTORY_DIR"] = str(self.history_dir)
        environment["UNIT_CONFIG"] = str(self.config_file)
        environment["AGENTS_CONFIG_FILE"] = str(self.agents_config_file)
        environment["TZ"] = zone
        environment["USER_ZONE"] = zone
        environment["UNIT_NOW_EPOCH"] = str(at)
        environment["UNIT_PASS_OWNER"] = "launcher"
        # Popped rather than left alone: the suite copies the ambient
        # environment, so a developer running with a worker exported would
        # otherwise see it stamped on every pass these tests record.
        if self.team_slot:
            environment["UNIT_TEAM_ROLE"] = self.team_slot
        else:
            _ = environment.pop("UNIT_TEAM_ROLE", None)
        self.isolate_recorder_roots(environment)
        self.isolate_identity(environment)
        if claude_session:
            environment["CLAUDE_CODE_SESSION_ID"] = claude_session
        if codex_session:
            environment["CODEX_THREAD_ID"] = codex_session
        arguments = self.valid_launch_arguments(arguments)
        result = invoke_recorder(arguments, environment, Path.cwd(), check=True)
        self.last_recorder_stderr = result.stderr
        return result.stdout.strip()

    def run_failing_command(
        self, *arguments: str, at: int, zone: str = "UTC", complete_launch: bool = True
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["UNIT_HISTORY_DIR"] = str(self.history_dir)
        environment["UNIT_CONFIG"] = str(self.config_file)
        environment["AGENTS_CONFIG_FILE"] = str(self.agents_config_file)
        environment["TZ"] = zone
        environment["USER_ZONE"] = zone
        environment["UNIT_NOW_EPOCH"] = str(at)
        environment["UNIT_PASS_OWNER"] = "launcher"
        # Popped rather than left alone: the suite copies the ambient
        # environment, so a developer running with a worker exported would
        # otherwise see it stamped on every pass these tests record.
        if self.team_slot:
            environment["UNIT_TEAM_ROLE"] = self.team_slot
        else:
            _ = environment.pop("UNIT_TEAM_ROLE", None)
        self.isolate_recorder_roots(environment)
        self.isolate_identity(environment)
        if complete_launch:
            arguments = self.valid_launch_arguments(arguments)
        return invoke_recorder(arguments, environment, Path.cwd())

    def invoke_handler(
        self,
        handler: Callable[[argparse.Namespace], None],
        arguments: argparse.Namespace,
        at: int,
        refresh_result: subprocess.CompletedProcess[str] | BaseException,
    ) -> tuple[None, str, str]:
        environment = os.environ.copy()
        environment["UNIT_HISTORY_DIR"] = str(self.history_dir)
        environment["UNIT_CONFIG"] = str(self.config_file)
        environment["AGENTS_CONFIG_FILE"] = str(self.agents_config_file)
        environment["UNIT_NOW_EPOCH"] = str(at)
        environment["UNIT_PASS_OWNER"] = "launcher"
        environment["TZ"] = "UTC"
        environment["USER_ZONE"] = "UTC"
        self.isolate_recorder_roots(environment)
        self.isolate_identity(environment)
        stdout = io.StringIO()
        stderr = io.StringIO()
        if isinstance(refresh_result, BaseException):
            run_patch = patch(
                "scripts.direct.progress_history.subprocess.run",
                side_effect=refresh_result,
            )
        else:
            run_patch = patch(
                "scripts.direct.progress_history.subprocess.run",
                return_value=refresh_result,
            )
        with (
            patch.dict(os.environ, environment, clear=True),
            run_patch,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            returned = handler(arguments)
        return returned, stdout.getvalue(), stderr.getvalue()

    def add_production_marker(self, session_dir: Path) -> None:
        state = self.read_state(session_dir)
        plan_path = Path(cast(str, state["project_plan_doc"]))
        plan_text = plan_path.read_text(encoding="utf-8")
        _ = plan_path.write_text(
            plan_text
            + "> **Production: Example** — unit `unit-a`; "
            + "production doc `docs/example-production.md`\n",
            encoding="utf-8",
        )

    def start_run(self, name: str, started_at: int) -> Path:
        session_dir = self.root / name
        session_dir.mkdir()
        plan_path = self.working_dir / "docs" / f"{name}.md"
        plan_path.parent.mkdir(exist_ok=True)
        plan_text = "\n".join(
            (
                "## Delegation Context",
                "",
                "- **Project:** Test project",
                f"- **Project started:** {datetime.fromtimestamp(started_at, UTC).isoformat()}",
                "",
            )
        )
        _ = plan_path.write_text(plan_text, encoding="utf-8")
        _ = self.run_command(
            "start-run",
            "--session-dir",
            str(session_dir),
            "--working-dir",
            str(self.working_dir),
            "--plan-doc",
            str(plan_path.relative_to(self.working_dir)),
            "--main-family",
            "codex",
            "--main-model",
            "gpt-main",
            "--main-effort",
            "xhigh",
            "--main-session-id",
            f"main-{name}",
            at=started_at,
        )
        return session_dir

    def start_phase_and_pass(self, session_dir: Path, started_at: int) -> None:
        _ = self.run_command(
            "start-phase",
            "--session-dir",
            str(session_dir),
            "--phase-id",
            "3",
            "--phase-title",
            "Retry handling",
            at=started_at,
        )
        _ = self.run_command(
            "start-pass",
            "--session-dir",
            str(session_dir),
            "--pass-kind",
            "fix",
            "--fix-pass",
            "2",
            "--activity",
            "correcting retry recovery",
            "--called-task",
            "direct.escalation",
            "--called-family",
            "codex",
            "--called-model",
            "gpt-called",
            "--called-effort",
            "high",
            at=started_at + 10,
        )

    def complete_historical_run(self, index: int) -> None:
        started_at = 10_000 + index * 1_000
        session_dir = self.start_run(f"historical-{index}", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.run_command(
            "calibrate",
            "--session-dir",
            str(session_dir),
            "--candidate-percent",
            "65",
            at=started_at + 100,
        )
        _ = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--raw-percent",
            "65",
            "--percent",
            "65",
            "--activity",
            "correcting retry recovery",
            at=started_at + 100,
        )
        _ = self.run_command(
            "calibrate",
            "--session-dir",
            str(session_dir),
            "--candidate-percent",
            "65",
            at=started_at + 160,
        )
        _ = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--raw-percent",
            "65",
            "--percent",
            "65",
            "--activity",
            "correcting retry recovery",
            at=started_at + 160,
        )
        _ = self.run_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 200,
        )
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 400,
        )
        _ = self.run_command(
            "finish-run",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 410,
        )

    def seed_calibration_history(
        self,
        name: str,
        cap_stage: str,
        reports: tuple[tuple[int, int], ...],
        phases: int = 5,
    ) -> None:
        """Write finished phases straight into the history the calibrator reads.

        Each report is (raw percent, percent of the phase's time spent), made by
        the identity `start_phase_and_pass` records, so every one is a sample in
        the current run's narrowest scope. Driving hundreds of CLI calls to reach
        the same rows would only slow the suite.
        """
        duration = 10_000
        lines: list[str] = []
        for index in range(phases):
            started_at = 1_000_000 + index * duration
            phase: dict[str, object] = {
                "schema_version": 1,
                "phase_instance_id": f"{name}-{index}",
                "phase_started_at": started_at,
            }
            for raw_percent, spent_percent in reports:
                event: dict[str, object] = {
                    **phase,
                    "event_type": "progress_reported",
                    "timestamp_epoch": started_at + duration * spent_percent // 100,
                    "raw_percent": raw_percent,
                    "percent": raw_percent,
                    "cap_stage": cap_stage,
                    "pass_kind": "fix",
                    "main_agent": {"model": "gpt-main", "effort": "xhigh"},
                    "called_agent": {"model": "gpt-called", "effort": "high"},
                }
                lines.append(json.dumps(event))
            finished: dict[str, object] = {
                **phase,
                "event_type": "phase_finished",
                "status": "completed",
                "timestamp_epoch": started_at + duration,
            }
            lines.append(json.dumps(finished))
        path = self.history_dir / "runs" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def calibrate_and_report(
        self,
        session_dir: Path,
        raw_percent: int,
        cap_stage: str,
        at: int,
    ) -> tuple[int, str]:
        """Run one tick the way the report contract does: calibrate, then report
        the suggestion. Returns the suggestion and the phase row's ETA cell."""
        calibration_text = self.run_command(
            "calibrate",
            "--session-dir",
            str(session_dir),
            "--candidate-percent",
            str(raw_percent),
            "--cap-stage",
            cap_stage,
            at=at,
        )
        parsed: object = json.loads(calibration_text)  # pyright: ignore[reportAny]
        suggestion = cast(int, cast(dict[str, object], parsed)["suggested_percent"])
        header = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "50",
            "--project-percent",
            "50",
            "--phase-raw-percent",
            str(raw_percent),
            "--phase-percent",
            str(suggestion),
            "--cap-stage",
            cap_stage,
            "--activity",
            "running the last gates",
            at=at,
        )
        return suggestion, self.table_rows(header, SUMMARY_HEADER)[1][3]

    def test_header_and_calibration_use_completed_history(self) -> None:
        for index in range(5):
            self.complete_historical_run(index)

        started_at = 20_000
        session_dir = self.start_run("current", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        calibration_text = self.run_command(
            "calibrate",
            "--session-dir",
            str(session_dir),
            "--candidate-percent",
            "65",
            at=started_at + 100,
        )
        parsed: object = json.loads(calibration_text)  # pyright: ignore[reportAny]
        calibration = cast(dict[str, object], parsed)
        self.assertEqual(calibration["sample_count"], 5)
        self.assertEqual(calibration["suggested_percent"], 25)
        self.assertEqual(calibration["apply_suggestion"], True)

        header = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "80",
            "--project-percent",
            "80",
            "--phase-raw-percent",
            "65",
            "--phase-percent",
            "25",
            "--cap-stage",
            "open_findings",
            "--activity",
            "correcting retry recovery",
            at=started_at + 100,
        )
        self.assertEqual(
            self.table_rows(header, SUMMARY_HEADER)[:2],
            [
                ["Project", "80", "00:01:40", "—", "", "—", "—"],
                ["Phase 3", "25", "00:01:40", "today 05:40", "",
                 "today 05:36 (-00:03)", "today 05:46 (+00:06)"],
            ],
        )
        self.assertIn("▸ **Fix 2 - correcting retry recovery**", header)
        self.assertEqual(header.splitlines()[-1], "**now 1970-01-01 05:35:00**")

        _ = self.run_command(
            "calibrate",
            "--session-dir",
            str(session_dir),
            "--candidate-percent",
            "65",
            at=started_at + 160,
        )
        unchanged_header = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "80",
            "--project-percent",
            "80",
            "--phase-raw-percent",
            "65",
            "--phase-percent",
            "25",
            "--cap-stage",
            "open_findings",
            "--activity",
            "correcting retry recovery",
            at=started_at + 160,
        )
        self.assertIn(
            "| Project |  80 | 00:02:40 |           — | 00:01:00  |",
            unchanged_header,
        )
        self.assertIn(
            "| Phase 3 |  25 | 00:02:40 | today 05:44 | 00:01:00  |",
            unchanged_header,
        )

        _ = self.run_command(
            "calibrate",
            "--session-dir",
            str(session_dir),
            "--candidate-percent",
            "65",
            at=started_at + 180,
        )
        independently_changed = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "85",
            "--project-percent",
            "85",
            "--phase-raw-percent",
            "65",
            "--phase-percent",
            "25",
            "--cap-stage",
            "open_findings",
            "--activity",
            "correcting retry recovery",
            at=started_at + 180,
        )
        self.assertIn(
            "| Project |  85 | 00:03:00 |           — |           |",
            independently_changed,
        )
        self.assertIn(
            "| Phase 3 |  25 | 00:03:00 | today 05:45 | 00:01:20  |",
            independently_changed,
        )

        history_file = self.history_dir / "runs" / "current.jsonl"
        history_text = history_file.read_text(encoding="utf-8")
        self.assertIn('"model":"gpt-main"', history_text)
        self.assertIn('"model":"gpt-called"', history_text)
        self.assertIn('"raw_percent":65', history_text)
        self.assertIn('"percent":25', history_text)
        self.assertIn('"project_percent":80', history_text)
        self.assertIn('"phase_percent":25', history_text)
        self.assertIn('"decision_source":"calibrated"', history_text)
        self.assertIn('"suggested_percent":25', history_text)
        self.assertIn('"historical_bias_percentage_points":40.0', history_text)

        _ = self.run_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 200,
        )
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 400,
        )
        _ = self.run_command(
            "finish-run",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 410,
        )
        aggregate_text = self.run_command("aggregate", "--percent", "65", at=30_000)
        aggregate_parsed: object = json.loads(aggregate_text)  # pyright: ignore[reportAny]
        aggregate = cast(dict[str, object], aggregate_parsed)
        groups = cast(list[object], aggregate["groups"])
        group = cast(dict[str, object], groups[0])
        self.assertEqual(group["sample_count"], 6)
        decision_counts = cast(dict[str, object], group["decision_source_counts"])
        self.assertEqual(decision_counts["raw"], 5)
        self.assertEqual(decision_counts["calibrated"], 1)

    def test_legacy_progress_preserves_its_layout_and_only_adds_tables_on_request(self) -> None:
        started_at = 20_000
        session_dir = self.start_run("legacy", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        for tables in (False, True):
            with self.subTest(tables=tables):
                report = self.run_command(
                    "progress", *(["--tables"] if tables else []),
                    "--session-dir", str(session_dir), "--raw-percent", "20", "--percent", "20",
                    "--activity", "correcting retry recovery", at=started_at + 100,
                )
                expected = [
                    "**bevy_hana_rubric - feature/rubric**",
                    "**Phase 3: Retry handling - elapsed 00:01:40**",
                    "ETA unavailable",
                    "**Fix 2 - correcting retry recovery - elapsed 00:01:30**",
                    "**20% complete**",
                    "**Total elapsed 00:01:40**",
                ]
                self.assertEqual(report.splitlines()[:6], expected)
                self.assertEqual(report.splitlines()[-1],
                                 "**now 1970-01-01 05:35:00**")
                if tables:
                    self.assertEqual(self.table_rows(report, SUMMARY_HEADER)[1][:3],
                                     ["Phase 3", "20", "00:01:40"])
                    self.assertEqual(
                        self.table_rows(report, ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"]),
                        [["Fix 2", "05:33:30", "1m", "fix 1m running", "-", "running"]],
                    )
                else:
                    self.assertEqual(report.splitlines(), [
                        *expected, "**now 1970-01-01 05:35:00**",
                    ])

    def test_ad_hoc_run_reuses_plan_project_time_for_the_same_worktree_branch(
        self,
    ) -> None:
        planned_start = 1_000
        planned_session = self.start_run("planned", planned_start)
        _ = self.run_command(
            "finish-run",
            "--session-dir",
            str(planned_session),
            "--status",
            "stopped",
            at=1_100,
        )

        ad_hoc_start = 100_000
        ad_hoc_session = self.root / "ad-hoc"
        ad_hoc_session.mkdir()
        _ = self.run_command(
            "start-run",
            "--session-dir",
            str(ad_hoc_session),
            "--working-dir",
            str(self.working_dir),
            "--main-family",
            "codex",
            "--main-model",
            "gpt-main",
            "--main-effort",
            "xhigh",
            "--main-session-id",
            "main-ad-hoc",
            at=ad_hoc_start,
        )
        state_text = (ad_hoc_session / "progress_history_state.json").read_text(
            encoding="utf-8"
        )
        state_object: object = json.loads(state_text)  # pyright: ignore[reportAny]
        state = cast(dict[str, object], state_object)
        self.assertEqual(state["project_started_at"], float(planned_start))
        self.assertEqual(state["project_start_source"], "history_plan_field")
        self.assertEqual(state["plan_doc"], "")
        self.assertEqual(
            state["project_plan_doc"],
            str((self.working_dir / "docs" / "planned.md").resolve()),
        )

        del state["project_start_source"]
        del state["project_plan_doc"]
        state["project_started_at"] = float(ad_hoc_start)
        _ = (ad_hoc_session / "progress_history_state.json").write_text(
            json.dumps(state),
            encoding="utf-8",
        )

        self.start_phase_and_pass(ad_hoc_session, ad_hoc_start)
        header = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(ad_hoc_session),
            "--project-raw-percent",
            "40",
            "--project-percent",
            "40",
            "--phase-raw-percent",
            "10",
            "--phase-percent",
            "10",
            "--cap-stage",
            "implementation",
            "--activity",
            "implementing",
            at=ad_hoc_start + 10,
        )
        self.assertIn("| Project |  40 | 1 day 03:30:10 ", header)
        self.assertIn("| Phase 3 |  10 |       00:00:10 |", header)

    def test_a_finished_phase_is_refused(self) -> None:
        """With no active phase there is nothing to report."""
        started_at = 20_000
        report_at = started_at + 100
        session_dir = self.start_run("refused-phase", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 60,
        )
        result = self.run_failing_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--activity",
            "implementing",
            at=report_at,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No active phase to report: phase completed.", result.stderr)
        self.assertIn("say nothing about whether the workers are alive", result.stderr)

    def test_a_closed_phase_with_a_running_activity_reports_its_last_tables(
        self,
    ) -> None:
        started_at = 20_000
        session_dir = self.start_run("closed-phase-progress", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.run_progress(session_dir, at=started_at + 100)
        self.close_the_only_pass(session_dir, at=started_at + 200)
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 300,
        )
        _ = self.run_command(
            "start-activity",
            "--session-dir",
            str(session_dir),
            "--label",
            "shrink",
            "--activity",
            "recording the as-built phase",
            at=started_at + 310,
        )
        calibration_text = self.run_command(
            "calibrate",
            "--session-dir",
            str(session_dir),
            "--candidate-percent",
            "65",
            at=started_at + 320,
        )
        parsed: object = json.loads(calibration_text)  # pyright: ignore[reportAny]
        calibration = cast(dict[str, object], parsed)
        self.assertEqual(calibration["candidate_percent"], 65)

        history = self.history_dir / "runs" / "closed-phase-progress.jsonl"
        state_path = session_dir / "progress_history_state.json"
        events_before = history.read_text(encoding="utf-8")
        state_before = state_path.read_text(encoding="utf-8")
        report = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "99",
            "--project-percent",
            "99",
            "--phase-raw-percent",
            "99",
            "--phase-percent",
            "99",
            "--cap-stage",
            "complete",
            "--activity",
            "this report argument must be ignored",
            at=started_at + 400,
        )

        self.assertEqual(
            report.splitlines()[:3],
            [
                "**bevy_hana_rubric - feature/rubric**",
                "",
                "*Percentages and clocks as of 05:35:00, the last progress report.*",
            ],
        )
        self.assertEqual(
            [row[:3] for row in self.table_rows(report, SUMMARY_HEADER)[:2]],
            [["Project", "40", "00:01:40"], ["Phase 3", "30", "00:01:40"]],
        )
        rounds = self.table_rows(
            report,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        self.assertEqual([row[0] for row in rounds], ["Fix 2", "shrink"])
        self.assertEqual(rounds[-1][-1], "running")
        self.assertIn("recording the as-built phase", report)
        self.assertNotIn("this report argument must be ignored", report)
        self.assertNotIn("No pass or activity is open.", report)
        self.assertEqual(
            report.splitlines()[-1],
            "**now 1970-01-01 05:40:00**",
        )
        self.assertEqual(history.read_text(encoding="utf-8"), events_before)
        self.assertEqual(state_path.read_text(encoding="utf-8"), state_before)
        progress_events = [
            event
            for event in self.read_events("closed-phase-progress")
            if event.get("event_type") == "progress_reported"
        ]
        self.assertEqual(len(progress_events), 1)
        state = self.read_state(session_dir)
        self.assertEqual(state["pending_calibration"], calibration)

    def test_a_closed_phase_with_a_finished_activity_has_no_active_report(
        self,
    ) -> None:
        started_at = 21_000
        session_dir = self.start_run("closed-finished-activity", started_at)
        self.start_phase(session_dir, started_at + 10)
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 100,
        )
        _ = self.run_command(
            "start-activity",
            "--session-dir",
            str(session_dir),
            "--label",
            "shrink",
            "--activity",
            "recording the as-built phase",
            at=started_at + 110,
        )
        _ = self.run_command(
            "finish-activity",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            "--result",
            "pass",
            at=started_at + 140,
        )

        result = self.run_failing_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--activity",
            "reporting after shrink",
            at=started_at + 150,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No active phase to report: phase completed.", result.stderr)

    def test_an_active_phase_tables_report_keeps_its_clocks_and_rounds(self) -> None:
        started_at = 20_000
        session_dir = self.start_run("active-phase-output", started_at)
        self.start_phase_and_pass(session_dir, started_at)

        report = self.run_progress(session_dir, at=started_at + 100)

        self.assertEqual(report.splitlines()[0], "**bevy_hana_rubric - feature/rubric**")
        self.assertEqual(
            [row[:3] for row in self.table_rows(report, SUMMARY_HEADER)[:2]],
            [["Project", "40", "00:01:40"], ["Phase 3", "30", "00:01:40"]],
        )
        self.assertEqual(
            self.table_rows(report, ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"]),
            [["Fix 2", "05:33:30", "1m", "fix 1m running", "-", "running"]],
        )
        self.assertIn("▸ **Fix 2 - implementing**", report)
        self.assertEqual(report.splitlines()[-1], "**now 1970-01-01 05:35:00**")

    def test_an_activity_after_a_closed_phase_keeps_the_phase_record(self) -> None:
        started_at = 21_000
        session_dir = self.start_run("closed-phase-activity", started_at)
        self.start_phase(session_dir, started_at + 10)
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 100,
        )
        phase_before = cast(dict[str, object], self.read_state(session_dir)["phase"])
        phase_instance_id = cast(str, phase_before["instance_id"])

        _ = self.run_command(
            "start-activity",
            "--session-dir",
            str(session_dir),
            "--label",
            "Shrink",
            "--activity",
            "recording the as-built phase",
            at=started_at + 110,
        )
        _ = self.run_command(
            "finish-activity",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            "--result",
            "pass",
            at=started_at + 140,
        )

        phase_after = cast(dict[str, object], self.read_state(session_dir)["phase"])
        self.assertEqual(phase_after, phase_before)
        self.assertEqual(phase_after["status"], "completed")
        self.assertEqual(phase_after["finished_at"], float(started_at + 100))
        phase_finished_events = [
            event
            for event in self.read_events("closed-phase-activity")
            if event.get("event_type") == "phase_finished"
        ]
        self.assertEqual(len(phase_finished_events), 1)
        self.assertEqual(phase_finished_events[0].get("phase_elapsed_seconds"), 90)
        activity_events = [
            event
            for event in self.read_events("closed-phase-activity")
            if event.get("event_type") in {"activity_started", "activity_finished"}
        ]
        self.assertEqual(
            [event.get("event_type") for event in activity_events],
            ["activity_started", "activity_finished"],
        )
        self.assertEqual(
            {event.get("phase_instance_id") for event in activity_events},
            {phase_instance_id},
        )
        self.assertEqual(activity_events[-1].get("status"), "completed")
        self.assertEqual(activity_events[-1].get("activity_result"), "pass")
        self.assertEqual(activity_events[-1].get("activity_elapsed_seconds"), 30)

    def test_repeating_finish_phase_keeps_a_closed_phase_activity_open(self) -> None:
        started_at = 21_500
        session_dir = self.start_run("repeated-phase-finish", started_at)
        self.start_phase(session_dir, started_at + 10)
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 100,
        )
        phase_before = cast(dict[str, object], self.read_state(session_dir)["phase"])
        _ = self.run_command(
            "start-activity",
            "--session-dir",
            str(session_dir),
            "--label",
            "Shrink",
            "--activity",
            "recording the as-built phase",
            at=started_at + 110,
        )
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 120,
        )
        phase_after = cast(dict[str, object], self.read_state(session_dir)["phase"])
        self.assertEqual(phase_after["status"], phase_before["status"])
        self.assertEqual(phase_after["finished_at"], phase_before["finished_at"])
        _ = self.run_command(
            "finish-activity",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            "--result",
            "pass",
            at=started_at + 150,
        )

        events = self.read_events("repeated-phase-finish")
        phase_finished_events = [
            event for event in events if event.get("event_type") == "phase_finished"
        ]
        self.assertEqual(len(phase_finished_events), 1)
        activity_finished_events = [
            event for event in events if event.get("event_type") == "activity_finished"
        ]
        self.assertEqual(len(activity_finished_events), 1)
        self.assertEqual(activity_finished_events[0].get("status"), "completed")
        self.assertEqual(activity_finished_events[0].get("activity_result"), "pass")
        self.assertEqual(
            activity_finished_events[0].get("activity_elapsed_seconds"), 40
        )

    def test_start_activity_distinguishes_missing_phase_from_finished_run(self) -> None:
        started_at = 22_000
        no_phase = self.start_run("activity-without-phase", started_at)
        refused = self.run_failing_command(
            "start-activity",
            "--session-dir",
            str(no_phase),
            "--label",
            "Verification",
            "--activity",
            "running checks",
            at=started_at + 10,
        )
        self.assertEqual(refused.returncode, 1)
        self.assertIn("Start a phase before starting an activity", refused.stderr)

        finished = self.start_run("activity-after-run", started_at + 100)
        self.start_phase(finished, started_at + 110)
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(finished),
            "--status",
            "completed",
            at=started_at + 120,
        )
        _ = self.run_command(
            "finish-run",
            "--session-dir",
            str(finished),
            "--status",
            "completed",
            at=started_at + 130,
        )
        refused = self.run_failing_command(
            "start-activity",
            "--session-dir",
            str(finished),
            "--label",
            "Verification",
            "--activity",
            "running checks",
            at=started_at + 140,
        )
        self.assertEqual(refused.returncode, 1)
        self.assertIn("run is finished", refused.stderr.lower())

    def test_a_closed_phase_activity_is_interrupted_by_the_next_phase_or_run_end(
        self,
    ) -> None:
        for index, ending in enumerate(("next phase", "run end")):
            with self.subTest(ending=ending):
                started_at = 23_000 + index * 1_000
                name = f"closed-activity-{index}"
                session_dir = self.start_run(name, started_at)
                self.start_phase(session_dir, started_at + 10)
                phase = cast(dict[str, object], self.read_state(session_dir)["phase"])
                phase_instance_id = cast(str, phase["instance_id"])
                _ = self.run_command(
                    "finish-phase",
                    "--session-dir",
                    str(session_dir),
                    "--status",
                    "completed",
                    at=started_at + 100,
                )
                _ = self.run_command(
                    "start-activity",
                    "--session-dir",
                    str(session_dir),
                    "--label",
                    "Shrink",
                    "--activity",
                    "recording the completed phase",
                    at=started_at + 110,
                )
                if ending == "next phase":
                    _ = self.run_command(
                        "start-phase",
                        "--session-dir",
                        str(session_dir),
                        "--phase-id",
                        "4",
                        "--phase-title",
                        "Final checks",
                        at=started_at + 140,
                    )
                else:
                    _ = self.run_command(
                        "finish-run",
                        "--session-dir",
                        str(session_dir),
                        "--status",
                        "completed",
                        at=started_at + 140,
                    )

                finished_events = [
                    event
                    for event in self.read_events(name)
                    if event.get("event_type") == "activity_finished"
                ]
                self.assertEqual(len(finished_events), 1)
                self.assertEqual(
                    finished_events[0].get("phase_instance_id"), phase_instance_id
                )
                self.assertEqual(finished_events[0].get("status"), "interrupted")
                self.assertEqual(finished_events[0].get("activity_result"), "")
                self.assertEqual(
                    finished_events[0].get("activity_elapsed_seconds"), 30
                )

    def close_the_only_pass(self, session_dir: Path, at: int) -> None:
        """Finish the pass `start_phase_and_pass` opened: reviews closed, no writer yet."""
        _ = self.run_command(
            "finish-pass", "--session-dir", str(session_dir), "--status", "completed", at=at
        )

    def test_a_phase_between_windows_reports_its_last_recorded_tables(self) -> None:
        """No window open is a moment inside the phase, not the end of the report."""
        started_at = 20_000
        session_dir = self.start_run("between-windows", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.run_progress(session_dir, at=started_at + 100)
        self.close_the_only_pass(session_dir, at=started_at + 200)
        state_dir = self.root / "notifier"
        environment = os.environ.copy()
        environment["NOTIFIER_STATE_DIR"] = str(state_dir)
        environment["NOTIFIER_NOW_EPOCH"] = str(started_at)
        environment["TZ"] = "UTC"
        environment["USER_ZONE"] = "UTC"
        _ = subprocess.run(
            [
                "zsh",
                str(SCRIPT.parents[1] / "message" / "notifier.sh"),
                "new",
                f"unit-{session_dir.name}",
                "--to",
                "session:test-claude-session",
                "--every",
                "15",
                "--command",
                "/unit:report",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        report_at = started_at + 400
        with patch.dict(
            os.environ,
            {"NOTIFIER_STATE_DIR": str(state_dir), "NOTIFIER_NOW_EPOCH": str(report_at)},
        ):
            # Percents the tick passes are neither required nor checked: the
            # report shows what was recorded, not what this call claims.
            report = self.run_command(
                "progress", "--tables",
                "--session-dir",
                str(session_dir),
                "--phase-raw-percent",
                "55",
                "--activity",
                "waiting on the repair writers",
                at=report_at,
            )
        lines = report.splitlines()
        self.assertEqual(
            lines[:3],
            [
                "**bevy_hana_rubric - feature/rubric**",
                "",
                "*Percentages and clocks as of 05:35:00, the last progress report.*",
            ],
        )
        summary = self.table_rows(report, SUMMARY_HEADER)
        # The rows the 05:35:00 report printed: its percents and its clocks.
        self.assertEqual(
            [row[:3] for row in summary[:2]],
            [["Project", "40", "00:01:40"], ["Phase 3", "30", "00:01:40"]],
        )
        rounds = self.table_rows(
            report, ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"]
        )
        self.assertEqual(
            [(row[0], row[3], row[5]) for row in rounds],
            [("Fix 2", "fix 3m done", "done")],
        )
        warning = lines[-2]
        self.assertTrue(warning.startswith("No pass or activity is open. "))
        self.assertIn("say nothing about whether the workers are alive", warning)
        self.assertGreater(lines.index(warning), lines.index(next(
            line for line in lines if line.startswith("| Fix 2 ")
        )))
        self.assertEqual(lines[-1], "**now 1970-01-01 05:40:00**")

    def test_a_phase_between_windows_prints_recorded_summary_without_tables_flag(self) -> None:
        started_at = 20_000
        session_dir = self.start_run("between-summary", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.run_progress(session_dir, at=started_at + 100)
        self.close_the_only_pass(session_dir, at=started_at + 200)

        report = self.run_command(
            "progress", "--session-dir", str(session_dir),
            "--activity", "waiting on the repair writers", at=started_at + 400,
        )

        self.assertIn("as of 05:35:00, the last progress report", report)
        self.assertIn("No pass or activity is open.", report)
        self.assertEqual(
            [row[:3] for row in self.table_rows(report, SUMMARY_HEADER)],
            [["Project", "40", "00:01:40"], ["Phase 3", "30", "00:01:40"]],
        )
        self.assertFalse(any(line.startswith("| Stage ") for line in report.splitlines()), report)
        self.assertFalse(any(line.startswith("Earlier:") for line in report.splitlines()), report)

    def test_a_phase_never_reported_still_prints_both_tables(self) -> None:
        """Before the first report the clocks read as of the phase start."""
        started_at = 20_000
        session_dir = self.start_run("never-reported", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        self.close_the_only_pass(session_dir, at=started_at + 200)
        report = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--activity",
            "waiting on the repair writers",
            at=started_at + 400,
        )
        self.assertIn(
            "*Percentages and clocks as of 05:33:20, the phase start, before any "
            + "progress report.*",
            report,
        )
        self.assertEqual(
            [row[:3] for row in self.table_rows(report, SUMMARY_HEADER)[:2]],
            [["Project", "0", "00:00:00"], ["Phase 3", "0", "00:00:00"]],
        )
        self.assertEqual(
            [
                row[0]
                for row in self.table_rows(
                    report, ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"]
                )
            ],
            ["Fix 2"],
        )

    def test_a_phase_with_no_window_yet_draws_a_row_saying_so(self) -> None:
        started_at = 20_000
        session_dir = self.start_run("no-window-yet", started_at)
        self.start_phase(session_dir, started_at)
        report = self.run_command(
            "progress", "--tables", "--session-dir", str(session_dir), "--activity", "starting", at=started_at + 60
        )
        self.assertEqual(
            self.table_rows(report, ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"]),
            [["-", "-", "-", "-", "-", "no stage yet"]],
        )

    def test_a_report_between_windows_records_nothing(self) -> None:
        """No event, no window: pass counts and convergence never see the call."""
        started_at = 20_000
        session_dir = self.start_run("records-nothing", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.run_progress(session_dir, at=started_at + 100)
        self.close_the_only_pass(session_dir, at=started_at + 200)
        history = self.history_dir / "runs" / "records-nothing.jsonl"
        state_path = session_dir / "progress_history_state.json"
        events_before = history.read_text(encoding="utf-8")
        state_before = state_path.read_text(encoding="utf-8")
        _ = self.run_progress(session_dir, at=started_at + 400)
        self.assertEqual(history.read_text(encoding="utf-8"), events_before)
        self.assertEqual(state_path.read_text(encoding="utf-8"), state_before)
        self.assertEqual(
            {str(record["status"]) for record in self.pass_slots(session_dir).values()},
            {"completed"},
        )

    def report_phase_percent(self, session_dir: Path, percent: int, at: int) -> None:
        _ = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "40",
            "--project-percent",
            "40",
            "--phase-raw-percent",
            str(percent),
            "--phase-percent",
            str(percent),
            "--cap-stage",
            "implementation",
            "--activity",
            "implementing",
            at=at,
        )

    def test_a_report_between_windows_names_the_eta_day_from_now(self) -> None:
        """Recorded 23:30 on the 3rd, read at 03:00 on the 4th: 00:40 is today."""
        recorded_at = 2 * 86_400 + 23 * 3_600 + 1_800
        session_dir = self.start_run("past-midnight", recorded_at - 1_800)
        self.start_phase_and_pass(session_dir, recorded_at - 1_800)
        self.report_phase_percent(session_dir, 30, at=recorded_at)
        self.close_the_only_pass(session_dir, at=recorded_at + 60)
        report = self.run_progress(session_dir, at=3 * 86_400 + 3 * 3_600)
        self.assertIn("as of 1970-01-03 23:30:00, the last progress report", report)
        phase_row = self.table_rows(report, SUMMARY_HEADER)[1]
        self.assertEqual(phase_row[:4], ["Phase 3", "30", "00:30:00", "today 00:40"])
        self.assertTrue(phase_row[5].startswith("today "))
        self.assertTrue(phase_row[6].startswith("today "))

    def test_a_report_between_windows_reads_the_latest_report_of_the_phase(self) -> None:
        started_at = 20_000
        session_dir = self.start_run("latest-report", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        self.report_phase_percent(session_dir, 20, at=started_at + 100)
        self.report_phase_percent(session_dir, 50, at=started_at + 150)
        self.close_the_only_pass(session_dir, at=started_at + 200)
        report = self.run_progress(session_dir, at=started_at + 400)
        self.assertIn("as of 05:35:50, the last progress report", report)
        self.assertEqual(self.table_rows(report, SUMMARY_HEADER)[1][:2], ["Phase 3", "50"])

    def test_a_report_between_windows_ignores_an_earlier_phase(self) -> None:
        started_at = 20_000
        session_dir = self.start_run("earlier-phase", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        self.report_phase_percent(session_dir, 30, at=started_at + 100)
        self.close_the_only_pass(session_dir, at=started_at + 200)
        _ = self.run_command(
            "finish-phase", "--session-dir", str(session_dir), "--status", "completed",
            at=started_at + 300,
        )
        _ = self.run_command(
            "start-phase", "--session-dir", str(session_dir), "--phase-id", "4",
            "--phase-title", "Next phase", at=started_at + 400,
        )
        report = self.run_progress(session_dir, at=started_at + 500)
        self.assertIn("the phase start, before any progress report", report)
        self.assertEqual(self.table_rows(report, SUMMARY_HEADER)[1][:2], ["Phase 4", "0"])

    def test_a_run_with_no_phase_is_refused(self) -> None:
        session_dir = self.start_run("no-phase", 20_000)
        result = self.run_failing_command(
            "progress", "--tables", "--session-dir", str(session_dir), "--activity", "idle", at=20_100
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No active phase to report: phase missing.", result.stderr)

    def run_progress(self, session_dir: Path, at: int) -> str:
        return self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "40",
            "--project-percent",
            "40",
            "--phase-raw-percent",
            "30",
            "--phase-percent",
            "30",
            "--cap-stage",
            "implementation",
            "--activity",
            "implementing",
            at=at,
        )

    def test_start_run_persists_the_plan_git_time_without_an_agent_timestamp(
        self,
    ) -> None:
        plan_path = self.working_dir / "docs" / "derived.md"
        plan_path.parent.mkdir(exist_ok=True)
        _ = plan_path.write_text(
            "## Delegation Context\n\n- **Project:** Derived project\n",
            encoding="utf-8",
        )
        commit_time = 1_700_000_000
        commit_time_text = datetime.fromtimestamp(commit_time, UTC).isoformat()
        environment = os.environ.copy()
        environment["GIT_AUTHOR_DATE"] = commit_time_text
        environment["GIT_COMMITTER_DATE"] = commit_time_text
        _ = subprocess.run(
            ["git", "add", str(plan_path.relative_to(self.working_dir))],
            cwd=self.working_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        _ = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Plan Worker Test",
                "-c",
                "user.email=unit@example.invalid",
                "commit",
                "-m",
                "checkpoint(derived): phase 1",
            ],
            cwd=self.working_dir,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )

        session_dir = self.root / "derived"
        session_dir.mkdir()
        _ = self.run_command(
            "start-run",
            "--session-dir",
            str(session_dir),
            "--working-dir",
            str(self.working_dir),
            "--plan-doc",
            str(plan_path.relative_to(self.working_dir)),
            "--main-family",
            "codex",
            "--main-model",
            "gpt-main",
            "--main-effort",
            "xhigh",
            "--main-session-id",
            "main-derived",
            at=commit_time + 1_000,
        )

        state_text = (session_dir / "progress_history_state.json").read_text(
            encoding="utf-8"
        )
        state_object: object = json.loads(state_text)  # pyright: ignore[reportAny]
        state = cast(dict[str, object], state_object)
        self.assertEqual(state["project_started_at"], float(commit_time))
        self.assertEqual(state["project_start_source"], "plan_first_checkpoint")
        persisted_text = plan_path.read_text(encoding="utf-8")
        persisted_value = persisted_text.split(
            "- **Project started:** ",
            maxsplit=1,
        )[1].strip()
        self.assertEqual(
            datetime.fromisoformat(persisted_value).timestamp(),
            commit_time,
        )

    def test_long_elapsed_times_split_days_from_clock_hours(self) -> None:
        started_at = 30_000
        session_dir = self.start_run("long-duration", started_at)
        self.start_phase_and_pass(session_dir, started_at)

        first = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--raw-percent",
            "20",
            "--percent",
            "20",
            "--activity",
            "implementing",
            at=started_at + 90_061,
        )
        self.assertIn("elapsed 1 day 01:01:01", first)

        second = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--raw-percent",
            "20",
            "--percent",
            "20",
            "--activity",
            "implementing",
            at=started_at + 176_461,
        )
        self.assertIn("elapsed 2 days 01:01:01", second)
        self.assertIn("unchanged for 1 day 00:00:00", second)

    def test_cap_stage_clamps_optimistic_estimates(self) -> None:
        started_at = 40_000
        session_dir = self.start_run("capped", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        header = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "100",
            "--project-percent",
            "100",
            "--phase-raw-percent",
            "99",
            "--phase-percent",
            "99",
            "--activity",
            "waiting on the closure review",
            "--cap-stage",
            "open_findings",
            at=started_at + 100,
        )
        self.assertIn("| Phase 3 |  90 |", header)
        self.assertIn("| Project |  99 |", header)
        self.assertNotIn("| Phase 3 | 100 |", header)
        self.assertNotIn("| Project | 100 |", header)

        history_text = (self.history_dir / "runs" / "capped.jsonl").read_text(encoding="utf-8")
        self.assertIn('"cap_stage":"open_findings"', history_text)
        self.assertIn('"phase_uncapped_percent":99', history_text)
        self.assertIn('"phase_percent_capped_by":"open_findings"', history_text)
        self.assertIn('"project_uncapped_percent":100', history_text)

    def test_dual_layout_progress_requires_a_cap_stage(self) -> None:
        started_at = 50_000
        session_dir = self.start_run("uncapped", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        result = self.run_failing_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "40",
            "--project-percent",
            "40",
            "--phase-raw-percent",
            "40",
            "--phase-percent",
            "40",
            "--activity",
            "implementing",
            at=started_at + 60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--cap-stage is required", result.stderr)

    def contract_fixture(self) -> tuple[Path, Path, str]:
        deployed = self.root / ".claude"
        direct = deployed / "commands/unit/direct.md"
        direct.parent.mkdir(parents=True)
        step = deployed / "docs/direct/phase_end.md"
        step.parent.mkdir(parents=True)
        _ = direct.write_text(
            "<TagReferenceContract>\n| Contract | File | Moment |\n| --- | --- | --- |\n"
            + "| <PhaseEnd/> | `docs/direct/phase_end.md` | end |\n</TagReferenceContract>\n",
            encoding="utf-8",
        )
        _ = step.write_text("<PhaseEnd>\nFinish the phase.\n</PhaseEnd>\n", encoding="utf-8")
        for arguments in (
            ["git", "init", "-b", "main"],
            ["git", "add", "."],
            ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
             "-c", "commit.gpgsign=false", "commit", "-m", "Contract fixture"],
        ):
            _ = subprocess.run(arguments, cwd=deployed, check=True, capture_output=True, text=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=deployed, check=True,
                              capture_output=True, text=True).stdout.strip()
        return direct, step, head

    def recorded_contracts(self, name: str) -> list[dict[str, object]]:
        events: list[dict[str, object]] = [json.loads(line) for line in
                                          (self.history_dir / "runs" / f"{name}.jsonl").read_text().splitlines()]
        return [cast(dict[str, object], event["contract_revision"]) for event in events
                if event["event_type"] in {"phase_started", "phase_finished"}]

    def test_phase_boundaries_record_main_head_and_clean_contract_bytes(self) -> None:
        direct, step, head = self.contract_fixture()
        session = self.start_run("exposure-clean", 1000)
        _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", "3",
                             "--phase-title", "Clean contract", at=1010)
        _ = self.run_command("finish-phase", "--session-dir", str(session), "--status", "completed", at=1060)
        records = self.recorded_contracts("exposure-clean")
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertEqual(record["main_head"], {"state": "Recorded", "commit": head})
            self.assertEqual(record["direct_md"], {"state": "Recorded", "sha256": hashlib.sha256(direct.read_bytes()).hexdigest()})
            self.assertEqual(record["files"], {"docs/direct/phase_end.md":
                             {"state": "Recorded", "sha256": hashlib.sha256(step.read_bytes()).hexdigest()}})

    def test_finish_records_dirty_bytes_instead_of_reusing_start_or_git(self) -> None:
        direct, step, head = self.contract_fixture()
        old_direct = hashlib.sha256(direct.read_bytes()).hexdigest()
        old_step = hashlib.sha256(step.read_bytes()).hexdigest()
        session = self.start_run("exposure-dirty", 1000)
        _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", "3",
                             "--phase-title", "Dirty contract", at=1010)
        _ = direct.write_bytes(direct.read_bytes() + b"\nDirty route.\n")
        _ = step.write_bytes(b"<PhaseEnd>\nChanged on disk.\n</PhaseEnd>\n")
        _ = self.run_command("finish-phase", "--session-dir", str(session), "--status", "completed", at=1060)
        started, finished = self.recorded_contracts("exposure-dirty")
        self.assertEqual(started["direct_md"], {"state": "Recorded", "sha256": old_direct})
        self.assertEqual(started["files"], {"docs/direct/phase_end.md": {"state": "Recorded", "sha256": old_step}})
        self.assertEqual(finished["direct_md"], {"state": "Recorded", "sha256": hashlib.sha256(direct.read_bytes()).hexdigest()})
        self.assertEqual(finished["files"], {"docs/direct/phase_end.md":
                         {"state": "Recorded", "sha256": hashlib.sha256(step.read_bytes()).hexdigest()}})
        self.assertEqual(finished["main_head"], {"state": "Recorded", "commit": head})

    def test_missing_contract_file_is_a_named_state_at_each_boundary(self) -> None:
        direct, step, _ = self.contract_fixture()
        step.unlink()
        session = self.start_run("exposure-missing", 1000)
        _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", "3",
                             "--phase-title", "Missing contract", at=1010)
        direct.unlink()
        _ = self.run_command("finish-phase", "--session-dir", str(session), "--status", "completed", at=1060)
        started, finished = self.recorded_contracts("exposure-missing")
        files = cast(dict[str, dict[str, str]], started["files"])
        self.assertEqual(files["docs/direct/phase_end.md"]["state"], "Missing")
        self.assertIn("phase_end.md", files["docs/direct/phase_end.md"]["reason"])
        missing_direct = cast(dict[str, str], finished["direct_md"])
        self.assertEqual(missing_direct["state"], "Missing")
        self.assertIn("commands/unit/direct.md", missing_direct["reason"])
        self.assertNotIn("sha256", missing_direct)

    def test_old_phase_events_and_new_exposure_events_have_the_same_reader_boundaries(self) -> None:
        session = self.start_run("exposure-reader", 1000)
        _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", "3",
                             "--phase-title", "Reader compatibility", at=1010)
        _ = self.run_command("finish-phase", "--session-dir", str(session), "--status", "completed", at=1060)
        events: list[dict[str, object]] = [json.loads(line) for line in
                                          (self.history_dir / "runs" / "exposure-reader.jsonl").read_text().splitlines()]
        phases = load_phases(events)
        old = [{key: value for key, value in event.items() if key != "contract_revision"} for event in events]
        self.assertEqual(load_phases(old), phases)
        self.assertEqual((phases[0]["started_at"], phases[0]["finished_at"]), (1010, 1060))

    def covered_wait_phase(self, name: str) -> Path:
        session = self.start_run(name, 1000)
        _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", "6",
                             "--phase-title", "Wait coverage", at=1010)
        return session

    def forwarded_supplier_wait(self, name: str, body: str = "The checkpoint is merged.") -> tuple[Path, Path, str]:
        session = self.covered_wait_phase(name)
        self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root), "XDG_STATE_HOME": str(self.root / "state"),
            "UNIT_NOW_EPOCH": "1020", "CLAUDE_PROBE_SESSION": "0", "UNIT_SYNC_WRITES": "0",
        }))
        # Alias lookup is outside the recorder's evidence check. Hook fixtures
        # exercise the real live-session resolver with bound socket files.
        _ = self.enterContext(patch("scripts.message.sessions.live_sessions", return_value=[{
            "name": "postmaster", "sessionId": "postmaster-session", "formerNames": [],
            "messagingSocketPath": "/fixture/postmaster/socket",
        }]))
        self.assertTrue(progress_history.record_turn_wait(session, "director", "blocked: waiting on uds:/supplier.sock"))
        replies = self.root / "state/message/postmaster/replies"
        replies.mkdir(parents=True, exist_ok=True)
        record = replies / "1030000-2.json"
        _ = record.write_text(json.dumps({
            "at_ms": 1030000, "socket": "uds:/supplier.sock", "name": "renamed-supplier",
            "session_id": "supplier-session", "senders": ["another-director", "director"],
            "body": body, "targets": ["another-director", "director"],
        }))
        prompt = ('<cross-session-message from="uds:/fixture/postmaster/socket" from-name="postmaster">'
                  + "Message from renamed-supplier (their answer to one of another-director, director, "
                  + "passed on by postmaster) [1030000-9]:\n\n" + body + "</cross-session-message>")
        os.environ["UNIT_NOW_EPOCH"] = "1030"
        return session, record, prompt

    def assert_forwarded_supplier_wait_stays_open(self, name: str, session: Path, prompt: str) -> None:
        before = self.read_events(name)
        marker = (session / "turn_wait.json").read_bytes()
        progress_history.finish_turn_wait(session, "director", source="PEER", prompt=prompt)
        self.assertEqual(self.read_events(name), before)
        self.assertEqual((session / "turn_wait.json").read_bytes(), marker)

    def test_pending_forward_preserves_body_whitespace_and_ignores_the_record_counter(self) -> None:
        name = "pending-exact-body"
        session, _, prompt = self.forwarded_supplier_wait(name, "\nThe checkpoint is merged.\n\n")
        self.assertFalse((self.root / "state/message/postmaster/forwards").exists())
        progress_history.finish_turn_wait(session, "director", source="PEER", prompt=prompt)
        progress_history.finish_turn_wait(session, "director", source="PEER", prompt=prompt)
        waits = [event for event in self.read_events(name) if str(event["event_type"]).startswith("wait_")]
        self.assertEqual([event["event_type"] for event in waits], ["wait_started", "wait_finished"])
        self.assertEqual((waits[1]["wait_id"], waits[1]["owner_session"], waits[1]["phase_instance_id"]),
                         (waits[0]["wait_id"], "director", waits[0]["phase_instance_id"]))
        self.assertEqual(cast(dict[str, object], waits[1]["wait_end"])["epoch"], 1030)
        self.assertFalse((session / "turn_wait.json").exists())

    def test_write_once_reply_ends_wait_with_a_forward_completion_record(self) -> None:
        name = "forward-complete"
        session, record, prompt = self.forwarded_supplier_wait(name)
        forwards = record.parent.parent / "forwards"
        forwards.mkdir()
        # Completion ids do not decide whether this director was a target.
        _ = (forwards / record.name).write_text(json.dumps({"forwarded": ["another-director"]}))
        progress_history.finish_turn_wait(session, "director", source="PEER", prompt=prompt)
        waits = [event for event in self.read_events(name) if str(event["event_type"]).startswith("wait_")]
        self.assertEqual([event["event_type"] for event in waits], ["wait_started", "wait_finished"])
        self.assertEqual(waits[1]["wait_id"], waits[0]["wait_id"])
        self.assertFalse((session / "turn_wait.json").exists())

    def test_old_form_reply_ends_wait_without_validating_extra_fields(self) -> None:
        old_fields: tuple[tuple[str, object], ...] = (
            ("pending", []), ("done", ["director"]), ("unknown", []), ("pending", "director"),
        )
        for index, (forward, forwarded) in enumerate(old_fields):
            with self.subTest(forward=forward, forwarded=forwarded):
                name = f"forward-old-form-{index}"
                session, record, prompt = self.forwarded_supplier_wait(name)
                fields = cast(dict[str, object], json.loads(record.read_text()))
                fields.update(forward=forward, forwarded=forwarded)
                _ = record.write_text(json.dumps(fields))
                progress_history.finish_turn_wait(session, "director", source="PEER", prompt=prompt)
                waits = [event for event in self.read_events(name) if str(event["event_type"]).startswith("wait_")]
                self.assertEqual([event["event_type"] for event in waits], ["wait_started", "wait_finished"])
                self.assertEqual(waits[1]["wait_id"], waits[0]["wait_id"])
                self.assertFalse((session / "turn_wait.json").exists())

    def test_missing_reply_record_leaves_the_supplier_wait_open(self) -> None:
        name = "forward-missing"
        session, record, prompt = self.forwarded_supplier_wait(name)
        record.unlink()
        self.assert_forwarded_supplier_wait_stays_open(name, session, prompt)

    def test_duplicate_reply_records_leave_the_supplier_wait_open(self) -> None:
        name = "forward-duplicate"
        session, record, prompt = self.forwarded_supplier_wait(name)
        _ = record.with_name("1030000-3.json").write_bytes(record.read_bytes())
        self.assert_forwarded_supplier_wait_stays_open(name, session, prompt)

    def test_reply_socket_and_target_must_match_the_supplier_wait(self) -> None:
        name = "forward-address-mismatch"
        session, record, prompt = self.forwarded_supplier_wait(name)
        valid = cast(dict[str, object], json.loads(record.read_text()))
        mismatches: tuple[tuple[str, object], ...] = (
            ("socket", "uds:/another-supplier.sock"), ("targets", ["another-director"]),
        )
        for field, value in mismatches:
            with self.subTest(field=field):
                _ = record.write_text(json.dumps({**valid, field: value}))
                self.assert_forwarded_supplier_wait_stays_open(name, session, prompt)

    def test_direct_supplier_reply_ends_wait_without_a_reply_record(self) -> None:
        name = "direct-supplier-reply"
        session, record, _ = self.forwarded_supplier_wait(name)
        record.unlink()
        prompt = '<cross-session-message from="uds:/supplier.sock">The checkpoint is merged.</cross-session-message>'
        progress_history.finish_turn_wait(session, "director", source="PEER", prompt=prompt)
        waits = [event for event in self.read_events(name) if str(event["event_type"]).startswith("wait_")]
        self.assertEqual([event["event_type"] for event in waits], ["wait_started", "wait_finished"])
        self.assertEqual(waits[1]["wait_id"], waits[0]["wait_id"])
        self.assertFalse((session / "turn_wait.json").exists())

    def test_malformed_reply_record_is_rejected_after_one_read_without_sleep(self) -> None:
        session, record, prompt = self.forwarded_supplier_wait("forward-read-malformed")
        for malformed in ('{"at_ms":', "[]", OSError("record is unreadable")):
            with self.subTest(malformed=malformed), patch.object(
                Path, "read_text", side_effect=[malformed]
            ) as read_record, patch("time.sleep") as pause:
                message = _supplier_message("uds:/supplier.sock", prompt, "director")
                if not isinstance(message, progress_history.UnverifiedSupplierMessage):
                    self.fail("malformed record verified a reply")
                self.assertEqual(message.rejection, progress_history.SupplierMessageRejection.NO_RECORD)
                self.assertEqual(read_record.call_count, 1)
                pause.assert_not_called()
        _ = record.write_text('{"at_ms":')
        self.assert_forwarded_supplier_wait_stays_open("forward-read-malformed", session, prompt)

    def test_complete_reply_object_with_invalid_fields_is_not_retried(self) -> None:
        session, record, prompt = self.forwarded_supplier_wait("forward-invalid-no-retry")
        complete = record.read_text()
        with patch.object(Path, "read_text", side_effect=["{}", complete]) as read_record, patch(
            "time.sleep"
        ) as pause:
            message = _supplier_message("uds:/supplier.sock", prompt, "director")
            if not isinstance(message, progress_history.UnverifiedSupplierMessage):
                self.fail("invalid record fields verified a reply")
            self.assertEqual(message.rejection, progress_history.SupplierMessageRejection.NO_RECORD)
            read_record.assert_called_once_with(encoding="utf-8")
            pause.assert_not_called()
        self.assertTrue((session / "turn_wait.json").exists())

    def test_record_timestamp_must_match_the_forward_even_when_the_filename_matches(self) -> None:
        name = "forward-time-mismatch"
        session, record, prompt = self.forwarded_supplier_wait(name)
        fields = cast(dict[str, object], json.loads(record.read_text()))
        fields["at_ms"] = 1029999
        _ = record.write_text(json.dumps(fields))
        self.assert_forwarded_supplier_wait_stays_open(name, session, prompt)

    def test_body_whitespace_difference_leaves_the_supplier_wait_open(self) -> None:
        name = "forward-body-mismatch"
        session, record, prompt = self.forwarded_supplier_wait(name, "The checkpoint is merged.\n")
        fields = cast(dict[str, object], json.loads(record.read_text()))
        fields["body"] = "The checkpoint is merged."
        _ = record.write_text(json.dumps(fields))
        self.assert_forwarded_supplier_wait_stays_open(name, session, prompt)

    def test_invalid_reply_record_schema_leaves_the_supplier_wait_open(self) -> None:
        name = "forward-invalid-schema"
        session, record, prompt = self.forwarded_supplier_wait(name)
        valid = cast(dict[str, object], json.loads(record.read_text()))
        invalid_fields: tuple[tuple[str, object], ...] = (
            ("targets", "director"), ("at_ms", True), ("senders", []),
        )
        for field, invalid in invalid_fields:
            with self.subTest(field=field):
                _ = record.write_text(json.dumps({**valid, field: invalid}))
                self.assert_forwarded_supplier_wait_stays_open(name, session, prompt)
        _ = record.write_text("{unfinished record")
        self.assert_forwarded_supplier_wait_stays_open(name, session, prompt)

    def test_verified_forward_cannot_finish_another_owners_wait(self) -> None:
        name = "forward-wrong-owner"
        session, record, prompt = self.forwarded_supplier_wait(name)
        fields = cast(dict[str, object], json.loads(record.read_text()))
        fields["targets"] = ["director", "different-owner"]
        _ = record.write_text(json.dumps(fields))
        before = self.read_events(name)
        marker = (session / "turn_wait.json").read_bytes()
        progress_history.finish_turn_wait(session, "different-owner", source="PEER", prompt=prompt)
        self.assertEqual(self.read_events(name), before)
        self.assertEqual((session / "turn_wait.json").read_bytes(), marker)

    def test_repeated_supplier_declaration_reuses_the_open_wait_after_an_unrelated_prompt(self) -> None:
        session = self.covered_wait_phase("declared-repeat")
        with patch.dict(os.environ, {"UNIT_NOW_EPOCH": "1020", "CLAUDE_PROBE_SESSION": "0"}):
            self.assertTrue(progress_history.record_turn_wait(session, "director", "blocked: waiting on uds:/supplier.sock"))
            marker = (session / "turn_wait.json").read_text()
            os.environ["UNIT_NOW_EPOCH"] = "1030"
            progress_history.finish_turn_wait(session, "director", source="PEER",
                                             prompt='<cross-session-message from="uds:/other.sock">Ready.</cross-session-message>')
            os.environ["UNIT_NOW_EPOCH"] = "1040"
            self.assertTrue(progress_history.record_turn_wait(session, "director", "blocked: waiting on uds:/supplier.sock for review"))
        waits = [event for event in self.read_events("declared-repeat") if str(event["event_type"]).startswith("wait_")]
        self.assertEqual(len(waits), 1)
        self.assertEqual((waits[0]["event_type"], waits[0]["wait_started_epoch"], waits[0]["owner_session"]),
                         ("wait_started", 1020, "director"))
        self.assertEqual((session / "turn_wait.json").read_text(), marker)

    def test_phase_finish_closes_an_open_declared_supplier_wait_as_unclosed(self) -> None:
        session = self.covered_wait_phase("declared-phase-end")
        with patch.dict(os.environ, {"UNIT_NOW_EPOCH": "1020", "CLAUDE_PROBE_SESSION": "0"}):
            self.assertTrue(progress_history.record_turn_wait(session, "director", "blocked: waiting on uds:/supplier.sock"))
        _ = self.run_command("finish-phase", "--session-dir", str(session), "--status", "completed", at=1050)
        events = self.read_events("declared-phase-end")
        started, closed = [event for event in events if str(event["event_type"]).startswith("wait_")]
        self.assertEqual((closed["event_type"], closed["wait_end"]), ("wait_unclosed", {"state": "Unclosed"}))
        self.assertEqual((closed["wait_id"], closed["wait_started_epoch"], closed["phase_instance_id"]),
                         (started["wait_id"], 1020, started["phase_instance_id"]))
        self.assertLess(events.index(closed), next(index for index, event in enumerate(events)
                                                 if event["event_type"] == "phase_finished"))

    def test_thread_wait_retains_its_own_pass_with_shared_server_and_fix_worker_names(self) -> None:
        for same_phase in (False, True):
            for suffix in ("test", "fix"):
                with self.subTest(same_phase=same_phase, suffix=suffix):
                    name = f"thread-waits-{same_phase}-{suffix}"
                    session = self.covered_wait_phase(name)
                    for slot, phase, worker in (("impl", "6", "unit-impl"),
                                                ("test", "6" if same_phase else "7", f"unit-{suffix}")):
                        if phase == "7":
                            _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", "7",
                                                 "--phase-title", "Parallel phase", at=1020)
                        self.team_slot = slot
                        _ = self.run_command("start-pass", "--session-dir", str(session), "--phase-id", phase,
                                             "--pass-kind", "impl", "--activity", "work", "--called-task", "direct.impl",
                                             "--worker-name", worker, "--called-family", "codex", "--called-model", "gpt-called", at=1021)
                    _ = (session / "mesh_roster.json").write_text(json.dumps(
                        {"unit-impl": {"thread_id": "impl-thread"}, f"unit-{suffix}": {"thread_id": "test-thread"}}))
                    self.team_slot = "impl"
                    ids: list[str] = []
                    for thread in ("impl-thread", "test-thread"):
                        ids.append(self.run_command("start-wait", "--session-dir", str(session),
                            "--worker-thread-id", thread, "--owner-session", thread,
                            "--kind", "WaitingForMemoryOrSlot", at=1030))
                    _ = self.run_command("finish-wait", "--session-dir", str(session), "--wait-id", ids[0], at=1040)
                    _ = self.run_command("finish-phase", "--session-dir", str(session), "--phase-id",
                                         "6" if same_phase else "7", "--status", "completed", at=1050)
                    events = self.read_events(name)
                    passes = {str(event["worker_name"]): event for event in events if event["event_type"] == "pass_started"}
                    report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1060)))
                    waits = {str(wait["owner_session"]): wait for wait in cast(list[dict[str, object]], report["waits"])}
                    for thread, worker in (("impl-thread", "unit-impl"), ("test-thread", f"unit-{suffix}")):
                        wait = waits[thread]
                        started = next(event for event in events if event["event_type"] == "wait_started"
                                       and event["owner_session"] == thread)
                        self.assertEqual((wait["worker_name"], wait["pass_instance_id"], wait["phase_instance_id"]),
                                         (worker, passes[worker]["pass_instance_id"], passes[worker]["phase_instance_id"]))
                        self.assertEqual((started["worker_name"], started["pass_instance_id"], started["phase_instance_id"]),
                                         (wait["worker_name"], wait["pass_instance_id"], wait["phase_instance_id"]))
                    completed = cast(dict[str, object], waits["impl-thread"]["wait_end"])
                    self.assertEqual((completed["state"], completed["epoch"]), ("Finished", 1040))
                    self.assertEqual(waits["test-thread"]["wait_end"], {"state": "Unclosed"})

    def test_thread_attribution_miss_never_reports_candidate_phases_as_covered_zero(self) -> None:
        session = self.covered_wait_phase("thread-miss")
        self.team_slot = "impl"
        _ = self.run_command("start-pass", "--session-dir", str(session), "--phase-id", "6",
                             "--pass-kind", "impl", "--activity", "work", "--called-task", "direct.impl",
                             "--worker-name", "unit-impl", "--called-family", "codex", "--called-model", "gpt-called", at=1015)
        for roster in ({}, {"one": {"thread_id": "thread"}, "two": {"thread_id": "thread"}},
                       {"unmatched": {"thread_id": "thread"}}):
            _ = (session / "mesh_roster.json").write_text(json.dumps(roster))
            _ = self.run_command("start-wait", "--session-dir", str(session), "--worker-thread-id", "thread",
                                 "--owner-session", "thread", "--kind", "WaitingForQuotaRefusal", at=1020)
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1030)))
        phase = cast(list[dict[str, object]], report["phases"])[0]
        self.assertEqual((phase["state"], report["missing_record_n"], report["attribution_miss_n"]), ("UncertainCoverage", 3, 3))
        self.assertNotIn("wait_n", phase)
        self.assertEqual(len(cast(list[object], phase["attribution_miss_ids"])), 3)
        for miss in cast(list[dict[str, object]], report["missing"]):
            attribution = cast(dict[str, object], miss["wait_attribution"])
            self.assertTrue(attribution["reason"])
            self.assertTrue(attribution["candidates"])

    def test_uncertain_coverage_counts_repeated_reasons_in_first_seen_order(self) -> None:
        session = self.covered_wait_phase("repeated-uncertainty")
        _ = (session / "mesh_roster.json").write_text(json.dumps({
            "unit-impl": {"thread_id": "impl-thread"},
            "unit-test": {"thread_id": "test-thread"},
        }))
        wait_ids = [self.run_command(
            "start-wait", "--session-dir", str(session), "--worker-thread-id", thread,
            "--owner-session", thread, "--kind", "WaitingForMemoryOrSlot", at=1020 + index,
        ) for index, thread in enumerate(("impl-thread", "test-thread", *("impl-thread",) * 4))]
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1030)))
        phase, = cast(list[dict[str, object]], report["phases"])
        self.assertEqual(phase["state"], "UncertainCoverage")
        self.assertEqual(phase["reason"], "worker unit-impl has 0 active pass matches (5 times); "
                         + "worker unit-test has 0 active pass matches")
        self.assertEqual(phase["attribution_miss_ids"], wait_ids)
        missing = cast(list[dict[str, object]], report["missing"])
        expected_candidates = [candidate for wait in missing
                               for candidate in cast(list[object], cast(dict[str, object], wait["wait_attribution"])["candidates"])]
        self.assertEqual(len(expected_candidates), 6)
        self.assertEqual(phase["candidates"], expected_candidates)

    def test_checkpoint_identity_survives_reopening_runs_and_active_later_instances(self) -> None:
        for across_runs in (False, True):
            for active_later in (False, True):
                for associations in (0, 1, 2):
                    with self.subTest(across_runs=across_runs, active_later=active_later, associations=associations):
                        name = f"checkpoint-{across_runs}-{active_later}-{associations}"
                        first = self.covered_wait_phase(name)
                        commit = "a" * 40
                        if associations:
                            _ = self.run_command("checkpoint", "--session-dir", str(first), "--phase", "6", "--commit", commit, at=1015)
                        _ = self.run_command("finish-phase", "--session-dir", str(first), "--status", "completed", at=1020)
                        second = self.start_run(name + "-other", 1021) if across_runs else first
                        _ = self.run_command("start-phase", "--session-dir", str(second), "--phase-id", "6",
                                             "--phase-title", "Reopened", at=1030)
                        if associations == 2:
                            _ = self.run_command("checkpoint", "--session-dir", str(second), "--phase", "6", "--commit", commit, at=1035)
                        if not active_later:
                            _ = self.run_command("finish-phase", "--session-dir", str(second), "--status", "completed", at=1040)
                        wait_id = self.run_command("start-checkpoint-wait", "--commit", commit,
                            "--working-dir", str(self.working_dir), "--phase-id", "6", "--owner-session", "receiver", at=1050)
                        _ = self.run_command("finish-external-wait", "--wait-id", wait_id, at=1060)
                        report = cast(dict[str, object], json.loads(self.run_command("waits", at=1070)))
                        # Each subcase has its own history selection. Remove earlier
                        # fixture runs so one commit cannot acquire other owners.
                        phases = cast(list[dict[str, object]], report["phases"])
                        if associations == 1:
                            waits = cast(list[dict[str, object]], report["waits"])
                            self.assertEqual(sorted((str(phase["state"]), int(str(phase["wait_n"]))) for phase in phases), [("Covered", 0), ("Covered", 1)])
                            first_start = next(event for event in self.read_events(name) if event["event_type"] == "phase_started")
                            self.assertEqual(waits[0]["phase_instance_id"], first_start["phase_instance_id"])
                            self.assertEqual(cast(dict[str, object], waits[0]["wait_end"])["state"], "Finished")
                        else:
                            self.assertEqual([phase["state"] for phase in phases], ["UncertainCoverage", "UncertainCoverage"])
                            self.assertEqual(report["missing_record_n"], 1)
                            self.assertEqual(report["waits"], [])
                            for phase in phases:
                                self.assertTrue(phase["reason"])
                                self.assertEqual(len(cast(list[object], phase["candidates"])), 2)
                                self.assertNotIn("wait_n", phase)
                        for path in (self.history_dir / "runs").glob("*.jsonl"):
                            path.unlink()
                        (self.history_dir / "wait_attribution.jsonl").unlink()

    def test_production_handoff_has_no_phase_attribution_or_phase_uncertainty(self) -> None:
        session = self.covered_wait_phase("handoff")
        wait_id = self.run_command("start-handoff-wait", "--production", "child-style", "--commit", "b" * 40,
                                   "--owner-session", "parent", "--reason", "child checkpoint tests", at=1020)
        report = cast(dict[str, object], json.loads(self.run_command("waits", at=1030)))
        handoff = cast(list[dict[str, object]], report["handoffs"])[0]
        self.assertEqual(handoff["wait_end"], {"state": "Open"})
        self.assertNotIn("phase_instance_id", handoff)
        self.assertEqual(cast(dict[str, object], handoff["wait_attribution"])["state"], "ProductionHandoff")
        self.assertTrue(cast(dict[str, object], handoff["wait_attribution"])["reason"])
        _ = self.run_command("finish-external-wait", "--wait-id", wait_id, at=1040)
        _ = self.run_command("finish-external-wait", "--wait-id", wait_id, at=1050)
        report = cast(dict[str, object], json.loads(self.run_command("waits", at=1060)))
        self.assertEqual(report["production_handoff_n"], 1)
        self.assertEqual(report["uncertain_phase_n"], 0)
        self.assertEqual(cast(dict[str, object], cast(list[dict[str, object]], report["handoffs"])[0]["wait_end"])["epoch"], 1040)
        phase_report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1060)))
        self.assertEqual(cast(list[dict[str, object]], phase_report["phases"])[0]["wait_n"], 0)
        self.assertEqual(phase_report["handoffs"], [])

    def test_checkpoint_from_another_worktree_marks_the_requested_phases_uncertain(self) -> None:
        first = self.start_run("other-checkpoint-owner", 1000)
        _ = self.run_command("start-phase", "--session-dir", str(first), "--phase-id", "7", "--phase-title", "Owner", at=1010)
        commit = "c" * 40
        _ = self.run_command("checkpoint", "--session-dir", str(first), "--phase", "7", "--commit", commit, at=1015)
        self.working_dir = self.root / "different-worktree"
        self.working_dir.mkdir()
        second = self.start_run("requested-worktree", 1020)
        _ = self.run_command("start-phase", "--session-dir", str(second), "--phase-id", "7", "--phase-title", "Requested", at=1030)
        _ = self.run_command("start-checkpoint-wait", "--commit", commit, "--working-dir", str(self.working_dir),
                             "--phase-id", "7", "--owner-session", "receiver", at=1040)
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(second), at=1050)))
        phase, = cast(list[dict[str, object]], report["phases"])
        self.assertEqual((phase["state"], report["attribution_miss_n"]), ("UncertainCoverage", 1))
        self.assertNotIn("wait_n", phase)
        candidates = cast(list[dict[str, object]], phase["candidates"])
        self.assertEqual({candidate["run_id"] for candidate in candidates}, {first.name, second.name})
        self.assertEqual(len(candidates), 2)
        self.assertEqual(report["waits"], [])

    def test_director_wait_has_its_owner_and_requires_one_active_phase(self) -> None:
        session = self.covered_wait_phase("director-wait")
        _ = self.run_command("start-wait", "--session-dir", str(session), "--director", "--owner-session", "claude-director",
                             "--kind", "WaitingForMemoryOrSlot", at=1020)
        _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", "7", "--phase-title", "Concurrent", at=1030)
        _ = self.run_command("start-wait", "--session-dir", str(session), "--director", "--owner-session", "claude-director",
                             "--kind", "WaitingForMemoryOrSlot", at=1040)
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1050)))
        wait, = cast(list[dict[str, object]], report["waits"])
        self.assertEqual((wait["owner_session"], wait["phase_id"]), ("claude-director", "6"))
        self.assertEqual(report["attribution_miss_n"], 1)
        miss, = cast(list[dict[str, object]], report["missing"])
        candidates = cast(list[dict[str, object]], cast(dict[str, object], miss["wait_attribution"])["candidates"])
        self.assertEqual({candidate["phase_id"] for candidate in candidates}, {"6", "7"})
        phases = cast(list[dict[str, object]], report["phases"])
        self.assertEqual([phase["state"] for phase in phases], ["UncertainCoverage", "UncertainCoverage"])
        self.assertTrue(all("wait_n" not in phase for phase in phases))

    def test_external_wait_prefers_the_receiving_claude_directors_owner(self) -> None:
        _ = self.run_command("start-handoff-wait", "--production", "child", "--commit", "d" * 40, at=1020,
                             claude_session="receiving-director", codex_session="inherited-worker")
        report = cast(dict[str, object], json.loads(self.run_command("waits", at=1030)))
        wait, = cast(list[dict[str, object]], report["handoffs"])
        self.assertEqual(wait["owner_session"], "receiving-director")

    def test_phase_finish_marks_an_external_gate_unclosed_and_preserves_later_gate_completion(self) -> None:
        session = self.covered_wait_phase("external-close")
        commit = "e" * 40
        _ = self.run_command("checkpoint", "--session-dir", str(session), "--phase", "6", "--commit", commit, at=1015)
        first = self.run_command("start-checkpoint-wait", "--commit", commit, "--working-dir", str(self.working_dir),
                                 "--phase-id", "6", "--owner-session", "receiver", at=1020)
        _ = self.run_command("finish-phase", "--session-dir", str(session), "--status", "completed", at=1030)
        _ = self.run_command("finish-external-wait", "--wait-id", first, at=1040)
        second = self.run_command("start-checkpoint-wait", "--commit", commit, "--working-dir", str(self.working_dir),
                                  "--phase-id", "6", "--owner-session", "receiver", at=1050)
        _ = self.run_command("finish-external-wait", "--wait-id", second, at=1060)
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1070)))
        waits = {str(wait["wait_id"]): wait for wait in cast(list[dict[str, object]], report["waits"])}
        self.assertEqual(waits[first]["wait_end"], {"state": "Unclosed"})
        self.assertEqual(waits[second]["wait_end"], {"state": "Finished", "at": "1970-01-01T00:17:40.000+00:00", "epoch": 1060})

    def test_external_start_serializes_with_phase_finish_publication(self) -> None:
        session = self.covered_wait_phase("external-interleave")
        commit = "f" * 40
        _ = self.run_command("checkpoint", "--session-dir", str(session), "--phase", "6", "--commit", commit, at=1015)
        environment = {**os.environ, "UNIT_HISTORY_DIR": str(self.history_dir), "UNIT_NOW_EPOCH": "1030",
                       "UNIT_CONFIG": str(self.config_file), "AGENTS_CONFIG_FILE": str(self.agents_config_file)}
        self.isolate_recorder_roots(environment)
        self.isolate_identity(environment)
        finish_program = '''import sys
from unittest.mock import patch
from scripts.direct import progress_history as recorder
append = recorder._append_event
def publication(state, event):
    if event.get("event_type") == "phase_finished":
        print("publishing", flush=True)
        sys.stdin.readline()
    append(state, event)
sys.argv = ["progress_history.py", *sys.argv[1:]]
with patch.object(recorder, "_append_event", publication):
    recorder.main()
'''
        start_program = '''import fcntl, sys
from unittest.mock import patch
from scripts.direct import progress_history as recorder
flock = fcntl.flock
def admission(fd, operation):
    if operation == fcntl.LOCK_EX:
        try:
            flock(fd, operation | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            print("blocked", flush=True)
    flock(fd, operation)
sys.argv = ["progress_history.py", *sys.argv[1:]]
with patch.object(fcntl, "flock", admission):
    recorder.main()
'''
        finish: subprocess.Popen[str] = subprocess.Popen([sys.executable, "-c", finish_program, "finish-phase", "--session-dir", str(session),
                                   "--status", "completed"], env=environment, text=True,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: finish.kill() if finish.poll() is None else None)
        if finish.stdout is None:
            raise AssertionError("finish fixture has no output pipe")
        finish_output = cast(TextIO, finish.stdout)
        self.assertTrue(select.select([finish_output], [], [], 15)[0], "finish never reaches publication")
        self.assertEqual(finish_output.readline().strip(), "publishing")
        start: subprocess.Popen[str] = subprocess.Popen([sys.executable, "-c", start_program, "start-checkpoint-wait", "--commit", commit,
                                  "--working-dir", str(self.working_dir), "--phase-id", "6", "--owner-session", "receiver"],
                                 env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: start.kill() if start.poll() is None else None)
        try:
            if start.stdout is None:
                raise AssertionError("start fixture has no output pipe")
            start_output = cast(TextIO, start.stdout)
            self.assertTrue(select.select([start_output], [], [], 15)[0], "external start never attempts admission")
            self.assertEqual(start_output.readline().strip(), "blocked")
        finally:
            finished_out, finished_errors = finish.communicate("release\n", timeout=30)
        started_out, started_errors = start.communicate(timeout=30)
        self.assertEqual(finish.returncode, 0, finished_out + finished_errors)
        self.assertEqual(start.returncode, 0, started_out + started_errors)
        wait_id = started_out.strip()
        _ = self.run_command("finish-external-wait", "--wait-id", wait_id, at=1050)
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1060)))
        wait, = cast(list[dict[str, object]], report["waits"])
        self.assertEqual(cast(dict[str, object], wait["wait_end"])["state"], "Finished")

    def test_capacity_waits_keep_session_and_unit_holders_in_observation_order(self) -> None:
        name = "capacity-holders"
        session = self.covered_wait_phase(name)
        kinds = (("WaitingForQuotaRefusal", "account", "account-a"),
                 ("WaitingForMemoryOrSlot", "memory", "build-pool"),
                 ("ReadyWithoutCapacity", "slot", "worker-2"))
        expected: dict[str, list[dict[str, object]]] = {}
        for kind, resource_kind, resource in kinds:
            for first_is_session in (True, False):
                with self.subTest(kind=kind, first_is_session=first_is_session):
                    owners = (("--holder-session", "other-session", {"state": "SessionHolder", "session_id": "other-session"}),
                              ("--holder-unit", "other-unit", {"state": "UnitHolder", "unit": "other-unit"}))
                    first, second = owners if first_is_session else tuple(reversed(owners))
                    wait_id = self.run_command("start-wait", "--session-dir", str(session), "--kind", kind,
                        "--owner-session", "waiting-session", "--holder-resource-kind", resource_kind,
                        "--holder-resource", resource, first[0], first[1], at=1020)
                    started = self.read_events(name)[-1]
                    _ = self.run_command("wait-holder", "--session-dir", str(session), "--wait-id", wait_id,
                        "--holder-resource-kind", resource_kind, "--holder-resource", resource + "-next",
                        second[0], second[1], at=1030)
                    changed = self.read_events(name)[-1]
                    self.assertEqual(changed["event_type"], "wait_holder_seen")
                    for field in ("wait_id", "owner_session", "wait_attribution", "phase_instance_id", "run_id"):
                        self.assertEqual(changed[field], started[field])
                    report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1031)))
                    current = next(wait for wait in cast(list[dict[str, object]], report["waits"]) if wait["wait_id"] == wait_id)
                    self.assertEqual(current["wait_end"], {"state": "Open"})
                    expected[wait_id] = [
                        {"at": datetime.fromtimestamp(at, UTC).isoformat(timespec="milliseconds"), "epoch": at,
                         "holder": {"state": "NamedHolder", "resource_kind": resource_kind,
                                    "resource": held_resource, "holder": owner}}
                        for at, held_resource, owner in ((1020, resource, first[2]), (1030, resource + "-next", second[2]))
                    ]
                    self.assertEqual(current["holders"], expected[wait_id])
                    _ = self.run_command("finish-wait", "--session-dir", str(session), "--wait-id", wait_id, at=1040)
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1050)))
        waits = cast(list[dict[str, object]], report["waits"])
        self.assertEqual(len(waits), 6)
        for wait in waits:
            self.assertEqual(wait["holders"], expected[str(wait["wait_id"])])
            self.assertEqual(wait["owner_session"], "waiting-session")
            self.assertEqual(cast(dict[str, object], wait["wait_end"])["epoch"], 1040)

    def test_unnamed_holder_is_explicit_and_can_follow_a_named_holder(self) -> None:
        session = self.covered_wait_phase("unnamed-holder")
        wait_id = self.run_command("start-wait", "--session-dir", str(session), "--kind", "ReadyWithoutCapacity", at=1020)
        _ = self.run_command("wait-holder", "--session-dir", str(session), "--wait-id", wait_id,
                             "--holder-resource-kind", "slot", "--holder-resource", "slot-a", "--holder-unit", "unit-a", at=1030)
        _ = self.run_command("wait-holder", "--session-dir", str(session), "--wait-id", wait_id, at=1040)
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1050)))
        wait, = cast(list[dict[str, object]], report["waits"])
        holders = cast(list[dict[str, object]], wait["holders"])
        self.assertEqual([entry["epoch"] for entry in holders], [1020, 1030, 1040])
        self.assertEqual([entry["holder"] for entry in holders], [
            {"state": "HolderNotNamed", "reason": "producer named no holder"},
            {"state": "NamedHolder", "resource_kind": "slot", "resource": "slot-a",
             "holder": {"state": "UnitHolder", "unit": "unit-a"}},
            {"state": "HolderNotNamed", "reason": "producer named no holder"}])

    def test_incomplete_or_blank_holder_flags_are_refused_without_writing_history(self) -> None:
        name = "invalid-holder-flags"
        session = self.covered_wait_phase(name)
        wait_id = self.run_command("start-wait", "--session-dir", str(session), "--kind", "ReadyWithoutCapacity", at=1020)
        before = self.read_events(name)
        invalid = (
            ("--holder-resource-kind", "slot"), ("--holder-resource", "slot-a"), ("--holder-session", "session-a"),
            ("--holder-unit", "unit-a"), ("--holder-resource-kind", "slot", "--holder-resource", "slot-a"),
            ("--holder-resource-kind", "slot", "--holder-unit", "unit-a"),
            ("--holder-resource", "slot-a", "--holder-session", "session-a"),
            ("--holder-resource-kind", "slot", "--holder-resource", "slot-a", "--holder-session", "a", "--holder-unit", "b"),
            ("--holder-resource-kind", "slot", "--holder-resource", " ", "--holder-session", "a"),
            ("--holder-resource-kind", "slot", "--holder-resource", "slot-a", "--holder-session", ""),
            ("--holder-resource-kind", "slot", "--holder-resource", "slot-a", "--holder-unit", " "),
            ("--holder-resource-kind", "disk", "--holder-resource", "disk-a", "--holder-unit", "unit-a"),
        )
        for flags in invalid:
            for verb, selector in (("start-wait", ("--kind", "ReadyWithoutCapacity")), ("wait-holder", ("--wait-id", wait_id))):
                with self.subTest(flags=flags, verb=verb):
                    result = self.run_failing_command(verb, "--session-dir", str(session), *selector, *flags, at=1030)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(self.read_events(name), before)

    def test_holder_flags_are_refused_for_noncapacity_waits(self) -> None:
        name = "noncapacity-holder"
        session = self.covered_wait_phase(name)
        flags = ("--holder-resource-kind", "slot", "--holder-resource", "slot-a", "--holder-session", "session-a")
        for kind in ("WaitingForUser", "WaitingForSupplier", "WaitingForMergeGate"):
            with self.subTest(kind=kind):
                wait_id = self.run_command("start-wait", "--session-dir", str(session), "--kind", kind, at=1020)
                before = self.read_events(name)
                for verb, selector in (("start-wait", ("--kind", kind)), ("wait-holder", ("--wait-id", wait_id))):
                    result = self.run_failing_command(verb, "--session-dir", str(session), *selector, *flags, at=1030)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(self.read_events(name), before)

    def test_holder_update_refuses_closed_unknown_and_unclosed_waits(self) -> None:
        name = "closed-holder"
        session = self.covered_wait_phase(name)
        finished = self.run_command("start-wait", "--session-dir", str(session), "--kind", "WaitingForQuotaRefusal", at=1020)
        unclosed = self.run_command("start-wait", "--session-dir", str(session), "--kind", "WaitingForMemoryOrSlot", at=1021)
        _ = self.run_command("finish-wait", "--session-dir", str(session), "--wait-id", finished, at=1030)
        _ = self.run_command("finish-phase", "--session-dir", str(session), "--status", "completed", at=1040)
        before = self.read_events(name)
        for wait_id in (finished, unclosed, "not-recorded"):
            with self.subTest(wait_id=wait_id):
                result = self.run_failing_command("wait-holder", "--session-dir", str(session), "--wait-id", wait_id,
                    "--holder-resource-kind", "account", "--holder-resource", "account-a", "--holder-unit", "unit-a", at=1050)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(self.read_events(name), before)

    def test_history_before_holder_support_reports_unknown_holder_and_keeps_new_observations(self) -> None:
        name = "old-holder-history"
        session = self.covered_wait_phase(name)
        wait_id = self.run_command("start-wait", "--session-dir", str(session), "--kind", "WaitingForQuotaRefusal", at=1020)
        events = [{key: value for key, value in event.items() if key != "holder"} for event in self.read_events(name)]
        path = self.history_dir / "runs" / f"{name}.jsonl"
        _ = path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1030)))
        wait, = cast(list[dict[str, object]], report["waits"])
        expected: list[dict[str, object]] = [{"at": "1970-01-01T00:17:00.000+00:00", "epoch": 1020,
                                            "holder": {"state": "HolderNotNamed", "reason": "recorded before holders"}}]
        self.assertEqual(wait["holders"], expected)
        _ = self.run_command("wait-holder", "--session-dir", str(session), "--wait-id", wait_id,
            "--holder-resource-kind", "account", "--holder-resource", "account-a", "--holder-session", "session-a", at=1040)
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1050)))
        wait, = cast(list[dict[str, object]], report["waits"])
        holders = cast(list[dict[str, object]], wait["holders"])
        self.assertEqual(holders[:1], expected)
        self.assertEqual(holders[1]["epoch"], 1040)
        self.assertEqual(holders[1]["holder"], {"state": "NamedHolder", "resource_kind": "account", "resource": "account-a",
                                             "holder": {"state": "SessionHolder", "session_id": "session-a"}})

    def assert_terminal_holder_is_classified(self, terminal_type: str) -> None:
        for keep_start in (False, True):
            with self.subTest(terminal_type=terminal_type, keep_start=keep_start):
                name = f"terminal-holder-{terminal_type}-{keep_start}"
                session = self.covered_wait_phase(name)
                wait_id = self.run_command("start-wait", "--session-dir", str(session),
                    "--kind", "WaitingForQuotaRefusal", "--holder-resource-kind", "account",
                    "--holder-resource", "account-a", "--holder-session", "session-a", at=1020)
                if terminal_type == "wait_unclosed":
                    _ = self.run_command("finish-phase", "--session-dir", str(session), "--status", "completed", at=1030)
                else:
                    _ = self.run_command("finish-wait", "--session-dir", str(session), "--wait-id", wait_id, at=1030)
                events = [event for event in self.read_events(name)
                          if keep_start or event.get("event_type") != "wait_started"]
                terminal, = [event for event in events if event.get("event_type") == terminal_type]
                terminal["holder"] = []
                path = self.history_dir / "runs" / f"{name}.jsonl"
                _ = path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
                report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1040)))
                wait, = cast(list[dict[str, object]], report["waits"])
                holders = cast(list[dict[str, object]], wait["holders"])
                self.assertEqual(holders[-1], {"at": "1970-01-01T00:17:10.000+00:00", "epoch": 1030,
                    "holder": {"state": "MalformedHolder", "reason": "holder must be an object", "raw_value": []}})
                self.assertEqual(len(holders), 2 if keep_start else 1)
                if not keep_start:
                    del terminal["holder"]
                    _ = path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
                    report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1040)))
                    wait, = cast(list[dict[str, object]], report["waits"])
                    observation, = cast(list[dict[str, object]], wait["holders"])
                    self.assertEqual(observation["holder"],
                        {"state": "HolderNotNamed", "reason": "recorded before holders"})

    def test_finished_wait_keeps_its_malformed_holder_with_or_without_a_start_row(self) -> None:
        self.assert_terminal_holder_is_classified("wait_finished")

    def test_unclosed_wait_keeps_its_malformed_holder_with_or_without_a_start_row(self) -> None:
        self.assert_terminal_holder_is_classified("wait_unclosed")

    def test_nonobject_holder_is_malformed_and_keeps_its_stored_value(self) -> None:
        for index, raw in enumerate(([], None, "session-a", 12)):
            with self.subTest(raw=raw):
                name = f"nonobject-holder-{index}"
                session = self.covered_wait_phase(name)
                wait_id = self.run_command("start-wait", "--session-dir", str(session),
                                           "--kind", "WaitingForQuotaRefusal", at=1020)
                events = self.read_events(name)
                for event in events:
                    if event.get("wait_id") == wait_id:
                        event["holder"] = raw
                path = self.history_dir / "runs" / f"{name}.jsonl"
                _ = path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
                report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1030)))
                wait = next(wait for wait in cast(list[dict[str, object]], report["waits"]) if wait["wait_id"] == wait_id)
                observation, = cast(list[dict[str, object]], wait["holders"])
                self.assertEqual(observation["holder"], {"state": "MalformedHolder",
                    "reason": "holder must be an object", "raw_value": raw})

    def test_unparseable_holder_is_malformed_and_keeps_its_stored_fields(self) -> None:
        raw_records: tuple[dict[str, object], ...] = (
            {"state": "UnknownHolder", "session_id": "session-a"},
            {"state": "HolderNotNamed", "reason": ""},
            {"state": "NamedHolder", "resource_kind": "account", "resource": "account-a",
             "holder": {"state": "SessionHolder", "session_id": ""}},
        )
        for index, raw in enumerate(raw_records):
            with self.subTest(raw=raw):
                name = f"unparseable-holder-{index}"
                session = self.covered_wait_phase(name)
                wait_id = self.run_command("start-wait", "--session-dir", str(session),
                                           "--kind", "WaitingForQuotaRefusal", at=1020)
                events = self.read_events(name)
                for event in events:
                    if event.get("wait_id") == wait_id:
                        event["holder"] = raw
                path = self.history_dir / "runs" / f"{name}.jsonl"
                _ = path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
                report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1030)))
                wait, = cast(list[dict[str, object]], report["waits"])
                observation, = cast(list[dict[str, object]], wait["holders"])
                holder = cast(dict[str, object], observation["holder"])
                self.assertEqual(holder["state"], "MalformedHolder")
                self.assertEqual(holder["raw_value"], raw)
                self.assertTrue(holder["reason"])

    def test_unknown_holder_and_owner_fields_remain_visible_as_malformed(self) -> None:
        named: dict[str, object] = {"state": "NamedHolder", "resource_kind": "account", "resource": "account-a",
                                    "holder": {"state": "SessionHolder", "session_id": "session-a"}}
        raw_records: tuple[dict[str, object], ...] = (
            {**named, "unexpected": {"kept": [1, 2]}},
            {**named, "holder": {"state": "SessionHolder", "session_id": "session-a", "unexpected": 1}},
            {**named, "holder": {"state": "UnitHolder", "unit": "unit-a", "unexpected": 1}},
            {"state": "HolderNotNamed", "reason": "producer named no holder", "unexpected": 1},
        )
        for index, raw in enumerate(raw_records):
            with self.subTest(raw=raw):
                name = f"unknown-holder-fields-{index}"
                session = self.covered_wait_phase(name)
                wait_id = self.run_command("start-wait", "--session-dir", str(session),
                                           "--kind", "WaitingForQuotaRefusal", at=1020)
                events = self.read_events(name)
                for event in events:
                    if event.get("wait_id") == wait_id:
                        event["holder"] = raw
                path = self.history_dir / "runs" / f"{name}.jsonl"
                _ = path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
                _ = self.run_command("finish-wait", "--session-dir", str(session), "--wait-id", wait_id, at=1025)
                report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1030)))
                wait, = cast(list[dict[str, object]], report["waits"])
                observation, = cast(list[dict[str, object]], wait["holders"])
                holder = cast(dict[str, object], observation["holder"])
                self.assertEqual(holder["state"], "MalformedHolder")
                self.assertEqual(holder["raw_value"], raw)
                self.assertIn("unexpected", str(holder["reason"]))

    def test_each_wait_kind_records_its_cause_owner_phase_and_actual_end(self) -> None:
        session = self.covered_wait_phase("wait-kinds")
        kinds = ("WaitingForQuotaRefusal", "WaitingForMemoryOrSlot", "WaitingForMergeGate",
                 "WaitingForUser", "WaitingForSupplier", "ReadyWithoutCapacity")
        for index, kind in enumerate(kinds):
            started = 1020 + index * 10
            wait_id = self.run_command("start-wait", "--session-dir", str(session), "--kind", kind,
                                       "--reason", f"Cause {kind}", "--owner-session", "worker-session",
                                       "--unit", "unit-a", at=started)
            _ = self.run_command("finish-wait", "--session-dir", str(session), "--wait-id", wait_id, at=started + 5)
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1100)))
        waits = cast(list[dict[str, object]], report["waits"])
        self.assertEqual((report["covered_n"], report["missing_n"]), (1, 0))
        self.assertEqual([wait["wait_kind"] for wait in waits], list(kinds))
        phase = next(event for event in self.read_events("wait-kinds") if event["event_type"] == "phase_started")
        for index, wait in enumerate(waits):
            self.assertEqual(wait["wait_reason"], f"Cause {kinds[index]}")
            self.assertEqual((wait["phase_id"], wait["phase_instance_id"], wait["owner_session"], wait["unit"]),
                             ("6", phase["phase_instance_id"], "worker-session", "unit-a"))
            end = cast(dict[str, object], wait["wait_end"])
            self.assertEqual(end, {"state": "Finished", "at": datetime.fromtimestamp(1025 + index * 10, UTC).isoformat(timespec="milliseconds"),
                                   "epoch": 1025 + index * 10})
            self.assertEqual(wait["wait_started_epoch"], 1020 + index * 10)

    def test_explicit_and_account_probe_sessions_write_no_wait(self) -> None:
        session = self.covered_wait_phase("wait-probes")
        before = self.read_events("wait-probes")
        for flag in (True, False):
            with patch.dict(os.environ, {"CLAUDE_PROBE_SESSION": "0" if flag else "1"}):
                result = self.run_command("start-wait", "--session-dir", str(session), "--kind", "WaitingForQuotaRefusal",
                                          *(("--probe",) if flag else ()), at=1020)
            self.assertEqual(result, "")
        self.assertEqual(self.read_events("wait-probes"), before)

    def test_closing_one_overlapping_wait_leaves_the_other_open_and_phase_end_marks_it_unclosed(self) -> None:
        session = self.covered_wait_phase("wait-overlap")
        first = self.run_command("start-wait", "--session-dir", str(session), "--kind", "WaitingForUser", at=1020)
        second = self.run_command("start-wait", "--session-dir", str(session), "--kind", "WaitingForSupplier", at=1030)
        _ = self.run_command("finish-wait", "--session-dir", str(session), "--wait-id", first, at=1040)
        _ = self.run_command("finish-wait", "--session-dir", str(session), "--wait-id", first, at=1041)
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1050)))
        waits = {str(wait["wait_id"]): wait for wait in cast(list[dict[str, object]], report["waits"])}
        self.assertEqual(waits[second]["wait_end"], {"state": "Open"})
        self.assertEqual(waits[first]["owner_session"], "main-wait-overlap")
        _ = self.run_command("finish-phase", "--session-dir", str(session), "--status", "completed", at=1060)
        _ = self.run_command("finish-wait", "--session-dir", str(session), "--wait-id", second, at=1070)
        events = self.read_events("wait-overlap")
        unclosed = [event for event in events if event["event_type"] == "wait_unclosed"]
        self.assertEqual(len(unclosed), 1)
        self.assertEqual((unclosed[0]["wait_id"], unclosed[0]["wait_started_epoch"], unclosed[0]["wait_end"]),
                         (second, 1030, {"state": "Unclosed"}))
        self.assertEqual(len([event for event in events if event["event_type"] == "wait_finished"]), 1)

    def test_waits_keep_parked_and_reopened_phase_instances_apart(self) -> None:
        session = self.covered_wait_phase("wait-instances")
        first = self.run_command("start-wait", "--session-dir", str(session), "--kind", "WaitingForUser", at=1020)
        _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", "7", "--phase-title", "Other", at=1030)
        second = self.run_command("start-wait", "--session-dir", str(session), "--kind", "WaitingForSupplier", at=1040)
        third = self.run_command("start-wait", "--session-dir", str(session), "--phase-id", "6", "--kind", "WaitingForMergeGate", at=1045)
        _ = self.run_command("finish-phase", "--session-dir", str(session), "--phase-id", "6", "--status", "completed", at=1050)
        _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", "6", "--phase-title", "Reopened", at=1060)
        reopened = self.run_command("start-wait", "--session-dir", str(session), "--kind", "WaitingForUser", at=1070)
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1080)))
        waits = {str(wait["wait_id"]): wait for wait in cast(list[dict[str, object]], report["waits"])}
        self.assertEqual(waits[first]["wait_end"], {"state": "Unclosed"})
        self.assertEqual(waits[third]["phase_instance_id"], waits[first]["phase_instance_id"])
        self.assertEqual(waits[second]["wait_end"], {"state": "Open"})
        self.assertNotEqual(waits[reopened]["phase_instance_id"], waits[first]["phase_instance_id"])
        self.assertEqual(waits[reopened]["wait_end"], {"state": "Open"})

    def test_covered_phase_without_any_wait_reports_zero_instead_of_missing_history(self) -> None:
        session = self.covered_wait_phase("wait-zero")
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1020)))
        self.assertEqual((report["covered_n"], report["missing_n"], report["waits"]), (1, 0, []))
        phase = cast(list[dict[str, object]], report["phases"])[0]
        self.assertEqual((phase["state"], phase["wait_n"]), ("Covered", 0))

    def test_slot_waits_follow_the_live_pass_across_reused_servers_and_parked_phases(self) -> None:
        session = self.covered_wait_phase("wait-slot")
        self.team_slot = "impl"
        _ = self.run_command("start-pass", "--session-dir", str(session), "--phase-id", "6",
                             "--pass-kind", "impl", "--activity", "work in six", "--called-task", "direct.impl",
                             "--called-family", "codex", "--called-model", "gpt-called", at=1015)
        _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", "7",
                             "--phase-title", "Parallel phase", at=1020)
        self.team_slot = "test"
        _ = self.run_command("start-pass", "--session-dir", str(session), "--phase-id", "7",
                             "--pass-kind", "impl", "--activity", "work in seven", "--called-task", "direct.impl",
                             "--called-family", "codex", "--called-model", "gpt-called", at=1025)
        with patch.dict(os.environ, {"UNIT_PHASE_ID": "closed-server-phase"}):
            first = self.run_command("start-wait", "--session-dir", str(session), "--slot", "impl",
                                     "--kind", "WaitingForMemoryOrSlot", at=1030)
            self.team_slot = "impl"
            _ = self.run_command("finish-pass", "--session-dir", str(session), "--status", "completed", at=1040)
            _ = self.run_command("start-pass", "--session-dir", str(session), "--phase-id", "7",
                                 "--pass-kind", "impl", "--activity", "next dispatch", "--called-task", "direct.impl",
                                 "--called-family", "codex", "--called-model", "gpt-called", at=1045)
            second = self.run_command("start-wait", "--session-dir", str(session), "--slot", "impl",
                                      "--kind", "WaitingForMemoryOrSlot", at=1050)
        records = {str(event["wait_id"]): event for event in self.read_events("wait-slot")
                   if event["event_type"] == "wait_started"}
        self.assertEqual((records[first]["phase_id"], records[second]["phase_id"]), ("6", "7"))
        self.assertNotEqual(records[first]["phase_instance_id"], records[second]["phase_instance_id"])

    def test_unresolved_slot_wait_is_counted_without_borrowing_the_current_phase(self) -> None:
        session = self.covered_wait_phase("wait-missing-slot")
        wait_id = self.run_command("start-wait", "--session-dir", str(session), "--slot", "absent",
                                   "--kind", "WaitingForMemoryOrSlot", "--reason", "admission busy", at=1020)
        _ = self.run_command("finish-wait", "--session-dir", str(session), "--wait-id", wait_id, at=1030)
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1040)))
        self.assertEqual((report["missing_n"], report["waits"]), (1, []))
        missing = cast(list[dict[str, object]], report["missing"])[0]
        self.assertEqual((missing["wait_id"], missing["requested_slot"], missing["phase_instance_id"]),
                         (wait_id, "absent", ""))
        self.assertIn("no open pass", str(missing["wait_record_state"]))
        self.assertEqual(missing["wait_end"], {"state": "NotRecorded"})
        self.assertEqual(missing["wait_reason"], "admission busy")

    def test_merge_wait_can_start_after_finish_and_keeps_the_original_phase_instance(self) -> None:
        session = self.covered_wait_phase("wait-after-phase")
        original = next(event for event in self.read_events("wait-after-phase") if event["event_type"] == "phase_started")
        _ = self.run_command("finish-phase", "--session-dir", str(session), "--status", "completed", at=1020)
        _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", "6",
                             "--phase-title", "Reopened six", at=1030)
        wait_id = self.run_command("start-wait", "--session-dir", str(session), "--phase-instance-id",
                                   str(original["phase_instance_id"]), "--kind", "WaitingForMergeGate", at=1040)
        _ = self.run_command("finish-wait", "--session-dir", str(session), "--wait-id", wait_id, at=1050)
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1060)))
        wait = cast(list[dict[str, object]], report["waits"])[0]
        self.assertEqual(wait["phase_instance_id"], original["phase_instance_id"])
        self.assertEqual(wait["phase_id"], "6")
        self.assertEqual(cast(dict[str, object], wait["wait_end"])["epoch"], 1050)
        phases = cast(list[dict[str, object]], report["phases"])
        self.assertEqual([(phase["wait_n"], phase["state"]) for phase in phases], [(1, "Covered"), (0, "Covered")])

    def test_missing_merge_emission_prevents_covered_zero_and_obeys_the_read_window(self) -> None:
        session = self.covered_wait_phase("wait-missing-merge")
        original = next(event for event in self.read_events("wait-missing-merge") if event["event_type"] == "phase_started")
        _ = self.run_command("finish-phase", "--session-dir", str(session), "--status", "completed", at=1020)
        _ = self.run_command("start-wait", "--session-dir", str(session), "--phase-instance-id",
                             str(original["phase_instance_id"]), "--kind", "WaitingForMergeGate",
                             "--missing-reason", "gate observer lost its start", at=1030)
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1040)))
        phase = cast(list[dict[str, object]], report["phases"])[0]
        self.assertEqual((report["covered_n"], report["missing_n"], phase["state"]), (0, 1, "HistoryNotRecorded"))
        self.assertNotIn("wait_n", phase)
        self.assertEqual(phase["wait_missing_n"], 1)
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session),
                                  "--since", "1970-01-01T00:17:10Z", "--until", "1970-01-01T00:17:20Z", at=1050)))
        self.assertEqual((report["phases"], report["missing_n"]), ([], 1))
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session),
                                  "--until", "1970-01-01T00:17:10Z", at=1050)))
        self.assertEqual(report["missing_n"], 0)

    def test_unknown_phase_instance_is_a_missing_record_even_when_another_phase_is_open(self) -> None:
        session = self.covered_wait_phase("wait-unknown-instance")
        _ = self.run_command("start-wait", "--session-dir", str(session), "--phase-instance-id", "unknown-instance",
                             "--kind", "WaitingForMergeGate", at=1020)
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1040)))
        missing = cast(list[dict[str, object]], report["missing"])[0]
        self.assertEqual((report["missing_n"], missing["requested_phase_instance_id"], missing["phase_instance_id"]),
                         (1, "unknown-instance", ""))

    def test_emitter_without_a_phase_selector_records_the_miss_without_charging_an_open_phase(self) -> None:
        session = self.covered_wait_phase("wait-no-selector")
        _ = self.run_command("start-wait", "--session-dir", str(session), "--kind", "WaitingForMemoryOrSlot",
                             "--missing-reason", "verification has no worker slot", at=1020)
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1040)))
        missing = cast(list[dict[str, object]], report["missing"])[0]
        self.assertEqual((report["missing_n"], missing["phase_id"], missing["phase_instance_id"]), (1, "", ""))
        phase = cast(list[dict[str, object]], report["phases"])[0]
        self.assertEqual((phase["state"], phase["wait_n"]), ("Covered", 0))

    def test_covered_zero_and_old_unrecorded_phase_are_distinct_and_window_is_half_open(self) -> None:
        session = self.covered_wait_phase("wait-window")
        _ = self.run_command("start-wait", "--session-dir", str(session), "--kind", "WaitingForUser", at=1020)
        _ = self.run_command("start-wait", "--session-dir", str(session), "--kind", "WaitingForSupplier", at=1030)
        history_file = self.history_dir / "runs" / "wait-window.jsonl"
        events = self.read_events("wait-window")
        old_phase = dict(next(event for event in events if event["event_type"] == "phase_started"))
        old_phase.update({"run_id": "old", "phase_instance_id": "old-phase", "timestamp_epoch": 1015})
        del old_phase["wait_coverage"]
        _ = history_file.write_text("\n".join(json.dumps(event) for event in [*events, old_phase]))
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--since", "1970-01-01T00:16:50Z",
                                            "--until", "1970-01-01T00:17:00Z", at=1100)))
        self.assertEqual((report["covered_n"], report["missing_n"]), (1, 1))
        phases = cast(list[dict[str, object]], report["phases"])
        self.assertEqual(phases[0]["wait_n"], 0)
        self.assertEqual(phases[1]["state"], "HistoryNotRecorded")
        self.assertNotIn("wait_n", phases[1])
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--since", "1970-01-01T00:17:00Z",
                                            "--until", "1970-01-01T00:17:10Z", at=1100)))
        self.assertEqual([wait["wait_kind"] for wait in cast(list[dict[str, object]], report["waits"])], ["WaitingForUser"])

    def test_wait_rows_without_ids_count_as_ignored_and_unrecorded_for_their_phase(self) -> None:
        name = "wait-without-id"
        session = self.covered_wait_phase(name)
        _ = self.run_command("start-wait", "--session-dir", str(session), "--kind", "WaitingForUser", at=1020)
        events = self.read_events(name)
        wait = next(event for event in events if event["event_type"] == "wait_started")
        history = self.history_dir / "runs" / f"{name}.jsonl"
        for event_type in ("wait_started", "wait_finished", "wait_unclosed", "wait_not_recorded"):
            for absent in (True, False):
                with self.subTest(event_type=event_type, absent=absent):
                    broken = {**wait, "event_type": event_type, "wait_id": ""}
                    if absent:
                        del broken["wait_id"]
                    _ = history.write_text("\n".join(json.dumps(event) for event in [*events[:-1], broken]))
                    for arguments in ((), ("--session-dir", str(session))):
                        report = cast(dict[str, object], json.loads(self.run_command("waits", *arguments, at=1040)))
                        phase, = cast(list[dict[str, object]], report["phases"])
                        self.assertEqual((report["ignored_n"], report["wait_without_id_n"], report["covered_n"]), (1, 1, 0))
                        self.assertEqual((phase["state"], phase["reason"], phase["wait_without_id_n"]),
                                         ("HistoryNotRecorded", "wait record has no wait id", 1))
                        self.assertNotIn("wait_n", phase)
                        self.assertEqual(report["waits"], [])

    def test_wait_without_id_keeps_missing_record_fields_and_matches_both_phase_keys(self) -> None:
        name = "wait-without-id-and-missing"
        session = self.covered_wait_phase(name)
        phase = next(event for event in self.read_events(name) if event["event_type"] == "phase_started")
        _ = self.run_command("start-wait", "--session-dir", str(session), "--phase-instance-id",
                             str(phase["phase_instance_id"]), "--kind", "WaitingForMergeGate",
                             "--missing-reason", "observer missed start", at=1020)
        events = self.read_events(name)
        broken = {**events[-1], "wait_id": "", "event_type": "wait_finished"}
        other_run = {**phase, "run_id": "other-run"}
        other_phase = {**phase, "phase_instance_id": "other-phase"}
        _ = (self.history_dir / "runs" / f"{name}.jsonl").write_text(
            "\n".join(json.dumps(event) for event in [*events, broken, other_run, other_phase]))
        report = cast(dict[str, object], json.loads(self.run_command("waits", at=1040)))
        first, second, third = cast(list[dict[str, object]], report["phases"])
        self.assertEqual((report["wait_without_id_n"], report["ignored_n"], report["missing_record_n"]), (1, 1, 1))
        self.assertEqual((first["state"], first["reason"], first["wait_missing_n"], first["recorded_wait_n"],
                          first["wait_without_id_n"]), ("HistoryNotRecorded", "wait emission has a missing record", 1, 0, 1))
        self.assertEqual((second["state"], third["state"]), ("Covered", "Covered"))
        self.assertNotIn("wait_without_id_n", second)
        self.assertNotIn("wait_without_id_n", third)

    def phase_source_fixture(self, name: str) -> tuple[Path, Path, Path]:
        session = self.start_run(name, 1000)
        worktree = self.root / "widget-enhancements"
        worktree.mkdir()
        git_directory = self.root / "hana" / ".git" / "worktrees" / "widget-enhancements"
        git_directory.mkdir(parents=True)
        _ = (git_directory / "commondir").write_text("../..\n")
        _ = (worktree / ".git").write_text("gitdir: ../hana/.git/worktrees/widget-enhancements\n")
        plan = worktree / "tool-based-ui-widget.md"
        _ = plan.write_text("> **Production:** Hana — unit `widgets`; production doc `hana.md`\n")
        state_path = session / "progress_history_state.json"
        state = cast(dict[str, object], json.loads(state_path.read_text()))
        state.update(working_dir=str(worktree), worktree=worktree.name,
                     project_plan_doc=plan.name, plan_doc="superseded-plan.md")
        _ = state_path.write_text(json.dumps(state))
        return session, worktree, plan

    def test_new_phase_records_plan_production_and_common_repository_before_worktree_is_retired(self) -> None:
        name = "phase-source-new"
        session, worktree, plan = self.phase_source_fixture(name)
        _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", "6",
                             "--phase-title", "Phase source", at=1010)
        started = next(event for event in self.read_events(name) if event["event_type"] == "phase_started")
        self.assertEqual(started["production"], {"state": "Recorded", "value": "Hana"})
        self.assertEqual(started["unit"], {"state": "Recorded", "value": "widgets"})
        self.assertEqual(started["repository"], {"state": "Recorded", "value": "hana"})
        _ = worktree.rename(self.root / "retired-worktree")
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1040)))
        phase, = cast(list[dict[str, object]], report["phases"])
        self.assertEqual(phase["plan_doc"], {"state": "Recorded", "value": plan.name})
        self.assertEqual(phase["production"], started["production"])
        self.assertEqual(phase["repository"], started["repository"])
        self.assertEqual(phase["unit"], "widget-enhancements")

    def test_phase_source_prefers_explicit_run_identity_to_plan_header(self) -> None:
        name = "phase-source-explicit"
        session, worktree, plan = self.phase_source_fixture(name)
        _ = self.run_command("set-unit", "--session-dir", str(session), "--production", "Other production",
                             "--unit", "other-unit", at=1005)
        _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", "6",
                             "--phase-title", "Phase source", at=1010)
        started = next(event for event in self.read_events(name) if event["event_type"] == "phase_started")
        self.assertEqual((started["production"], started["unit"]),
                         ({"state": "Recorded", "value": "Other production"}, {"state": "Recorded", "value": "other-unit"}))
        self.assertEqual(started["working_dir"], str(worktree))
        report = cast(dict[str, object], json.loads(self.run_command("waits", at=1040)))
        phase, = cast(list[dict[str, object]], report["phases"])
        self.assertEqual(phase["production"], started["production"])
        self.assertEqual(phase["plan_doc"], {"state": "Recorded", "value": plan.name})

    def test_old_phases_resolve_plan_header_and_common_repository_once_per_run(self) -> None:
        name = "phase-source-old"
        session, worktree, plan = self.phase_source_fixture(name)
        _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", "6",
                             "--phase-title", "Phase source", at=1010)
        _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", "7",
                             "--phase-title", "Next phase source", at=1020)
        events = self.read_events(name)
        for event in events:
            if event["event_type"] == "phase_started":
                for key in ("production", "unit", "repository", "wait_coverage"):
                    del event[key]
                # Legacy plan_doc is still used when project_plan_doc is empty.
                event.update(project_plan_doc="", plan_doc=plan.name)
        _ = (self.history_dir / "runs" / f"{name}.jsonl").write_text("\n".join(json.dumps(event) for event in events))
        reads: list[Path] = []
        read_text = Path.read_text

        def read_source(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            reads.append(path)
            return read_text(path, encoding=encoding, errors=errors)

        with patch.object(Path, "read_text", read_source), patch.object(
            subprocess, "run", side_effect=AssertionError("waits must only read files")
        ):
            report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1040)))
        phases = cast(list[dict[str, object]], report["phases"])
        self.assertEqual(len(phases), 2)
        for phase in phases:
            self.assertEqual(phase["state"], "HistoryNotRecorded")
            self.assertEqual(phase["plan_doc"], {"state": "Recorded", "value": plan.name})
            self.assertEqual(phase["production"], {"state": "Recorded", "value": "Hana"})
            self.assertEqual(phase["repository"], {"state": "Recorded", "value": "hana"})
        self.assertEqual(reads.count(plan), 1)
        self.assertEqual(reads.count(worktree / ".git"), 1)
        self.assertEqual(sum(path.name == "commondir" for path in reads), 1)

    def test_old_phase_with_removed_worktree_reports_metadata_not_recorded(self) -> None:
        name = "phase-source-gone"
        session, worktree, plan = self.phase_source_fixture(name)
        _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", "6",
                             "--phase-title", "Phase source", at=1010)
        events = self.read_events(name)
        for event in events:
            if event["event_type"] == "phase_started":
                for key in ("production", "unit", "repository", "wait_coverage"):
                    del event[key]
        history = self.history_dir / "runs" / f"{name}.jsonl"
        _ = history.write_text("\n".join(json.dumps(event) for event in events))
        _ = worktree.rename(self.root / "retired-worktree")
        report = cast(dict[str, object], json.loads(self.run_command("waits", at=1040)))
        phase, = cast(list[dict[str, object]], report["phases"])
        self.assertEqual(phase["plan_doc"], {"state": "Recorded", "value": plan.name})
        self.assertEqual(phase["state"], "HistoryNotRecorded")
        for field in ("production", "repository"):
            value = cast(dict[str, object], phase[field])
            self.assertEqual(value["state"], "NotRecorded")
            self.assertTrue(value["reason"])
        for event in events:
            event.update(project_plan_doc="", plan_doc="")
        _ = history.write_text("\n".join(json.dumps(event) for event in events))
        report = cast(dict[str, object], json.loads(self.run_command("waits", at=1040)))
        phase, = cast(list[dict[str, object]], report["phases"])
        self.assertEqual(phase["plan_doc"], {"state": "NotRecorded", "reason": "phase has no plan document"})

    def test_production_doc_paths_report_the_heading_name_instead_of_the_path(self) -> None:
        name = "phase-production-doc-path"
        session, worktree, plan = self.phase_source_fixture(name)
        production_doc = worktree / "docs" / "hana" / "tool-based-ui-production.md"
        production_doc.parent.mkdir(parents=True)
        _ = production_doc.write_text("# Production — tool-based-ui\n", encoding="utf-8")
        for index, reference in enumerate((str(production_doc.relative_to(worktree)), str(production_doc))):
            with self.subTest(reference=reference):
                _ = plan.write_text(f"> **Production:** {reference} — unit geometry-material-unit, branch widgets\n",
                                    encoding="utf-8")
                _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", str(index + 1),
                                     "--phase-title", "Production document", at=1010 + index)
                report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1040)))
                phase = cast(list[dict[str, object]], report["phases"])[-1]
                self.assertEqual(phase["production"], {"state": "Recorded", "value": "tool-based-ui"})
                started = [event for event in self.read_events(name) if event["event_type"] == "phase_started"][-1]
                self.assertEqual(started["production"], phase["production"])
                self.assertEqual(started["unit"], {"state": "Recorded", "value": "geometry-material-unit"})

    def test_stored_production_doc_paths_resolve_for_plain_and_tagged_metadata(self) -> None:
        name = "phase-stored-production-doc-path"
        session, worktree, plan = self.phase_source_fixture(name)
        _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", "6",
                             "--phase-title", "Stored production document", at=1010)
        production_doc = worktree / "docs" / "hana" / "production.md"
        production_doc.parent.mkdir(parents=True)
        events = self.read_events(name)
        started = next(event for event in events if event["event_type"] == "phase_started")
        history = self.history_dir / "runs" / f"{name}.jsonl"
        # A stored document reference must not depend on a surviving plan header.
        plan.unlink()
        for reference in (str(production_doc.relative_to(worktree)), str(production_doc)):
            for tagged in (False, True):
                with self.subTest(reference=reference, tagged=tagged):
                    started["production"] = {"state": "Recorded", "value": reference} if tagged else reference
                    _ = history.write_text("\n".join(json.dumps(event) for event in events))
                    _ = production_doc.write_text("# Production — tool-based-ui\n", encoding="utf-8")
                    report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1040)))
                    phase, = cast(list[dict[str, object]], report["phases"])
                    self.assertEqual(phase["production"], {"state": "Recorded", "value": "tool-based-ui"})
                    production_doc.unlink()
                    report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1040)))
                    phase, = cast(list[dict[str, object]], report["phases"])
                    production = cast(dict[str, object], phase["production"])
                    self.assertEqual(production["state"], "NotRecorded")
                    self.assertIn("production document cannot be read:", str(production["reason"]))
                    self.assertEqual(self.read_events(name), events)

    def test_unreadable_or_unnamed_production_docs_report_missing_metadata_with_a_reason(self) -> None:
        name = "phase-production-doc-unavailable"
        session, worktree, plan = self.phase_source_fixture(name)
        production_doc = worktree / "production.md"
        _ = plan.write_text("> **Production:** production.md — unit geometry-material-unit, branch widgets\n",
                            encoding="utf-8")
        cases = (("missing", "production document cannot be read:"),
                 ("invalid UTF-8", "production document cannot be read:"),
                 ("no heading", "production doc lacks its name"),
                 ("empty name", "production document heading names no production"))
        for index, (state, reason) in enumerate(cases):
            with self.subTest(state=state):
                if state == "invalid UTF-8":
                    _ = production_doc.write_bytes(b"\xff")
                elif state == "no heading":
                    _ = production_doc.write_text("# A document without a production heading\n", encoding="utf-8")
                elif state == "empty name":
                    _ = production_doc.write_text("# Production — \n", encoding="utf-8")
                _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", str(index + 1),
                                     "--phase-title", "Unavailable production document", at=1010 + index)
                report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1040)))
                phase = cast(list[dict[str, object]], report["phases"])[-1]
                production = cast(dict[str, object], phase["production"])
                self.assertEqual(production["state"], "NotRecorded")
                self.assertIn(reason, str(production["reason"]))
                started = [event for event in self.read_events(name) if event["event_type"] == "phase_started"][-1]
                self.assertEqual(started["production"], production)
                self.assertEqual(started["unit"], {"state": "Recorded", "value": "geometry-material-unit"})

    def test_plain_production_names_do_not_require_a_production_doc(self) -> None:
        name = "phase-production-plain-name"
        session, worktree, plan = self.phase_source_fixture(name)
        _ = plan.write_text("> **Production:** tool-based-ui — unit geometry-material-unit, branch widgets\n",
                            encoding="utf-8")
        self.assertFalse((worktree / "tool-based-ui").exists())
        _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", "6",
                             "--phase-title", "Plain production name", at=1010)
        report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1040)))
        phase, = cast(list[dict[str, object]], report["phases"])
        self.assertEqual(phase["production"], {"state": "Recorded", "value": "tool-based-ui"})

    def test_new_phases_record_production_without_requiring_a_unit_header(self) -> None:
        name = "phase-bare-production-new"
        session, worktree, plan = self.phase_source_fixture(name)
        headers = (
            ("> **Production:** natedev", "natedev", ""),
            ("> **Production:** build-followups (enh-showrunner-unit)", "build-followups", ""),
            (f"> **Production:** natedev — unit herdr-unit, worktree {worktree}", "natedev", "herdr-unit"),
            ("> **Production: natedev**", "natedev", ""),
            ("> **Production: build-followups** (enh-showrunner-unit)", "build-followups", ""),
            (f"> **Production: natedev** — unit `herdr-unit`; worktree {worktree}", "natedev", "herdr-unit"),
        )
        for index, (header, production, unit) in enumerate(headers):
            with self.subTest(header=header):
                _ = plan.write_text(header + "\n", encoding="utf-8")
                _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", str(index + 1),
                                     "--phase-title", "Production without unit", at=1010 + index)
                started = [event for event in self.read_events(name) if event["event_type"] == "phase_started"][-1]
                self.assertEqual(started["production"], {"state": "Recorded", "value": production})
                if unit:
                    self.assertEqual(started["unit"], {"state": "Recorded", "value": unit})
                else:
                    self.assertEqual(started["unit"], {"state": "NotRecorded", "reason": "phase plan header names no unit"})
                report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1040)))
                phase = cast(list[dict[str, object]], report["phases"])[-1]
                self.assertEqual(phase["production"], {"state": "Recorded", "value": production})

    def test_old_phases_read_production_from_headers_without_a_quoted_unit(self) -> None:
        name = "phase-bare-production-old"
        session, worktree, plan = self.phase_source_fixture(name)
        _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", "6",
                             "--phase-title", "Legacy production", at=1010)
        events = self.read_events(name)
        for event in events:
            if event["event_type"] == "phase_started":
                for key in ("production", "unit", "repository", "wait_coverage"):
                    del event[key]
        _ = (self.history_dir / "runs" / f"{name}.jsonl").write_text("\n".join(json.dumps(event) for event in events))
        headers = (
            ("> **Production:** natedev", "natedev"),
            ("> **Production:** build-followups (enh-showrunner-unit)", "build-followups"),
            (f"> **Production:** natedev — unit herdr-unit, worktree {worktree}", "natedev"),
            ("> **Production: natedev**", "natedev"),
            ("> **Production: build-followups** (enh-showrunner-unit)", "build-followups"),
            (f"> **Production: natedev** — unit herdr-unit, worktree {worktree}", "natedev"),
        )
        for header, production in headers:
            with self.subTest(header=header):
                _ = plan.write_text(header + "\n", encoding="utf-8")
                report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1040)))
                phase, = cast(list[dict[str, object]], report["phases"])
                self.assertEqual(phase["production"], {"state": "Recorded", "value": production})
                self.assertEqual(phase["state"], "HistoryNotRecorded")

    def test_phase_production_and_unit_each_keep_their_run_state_override(self) -> None:
        name = "phase-partial-source"
        session, worktree, plan = self.phase_source_fixture(name)
        state_path = session / "progress_history_state.json"
        for index, (field, value, header, production, unit) in enumerate((
            ("unit", "assigned-unit", "> **Production:** natedev", "natedev", "assigned-unit"),
            ("production", "assigned-production", f"> **Production:** natedev — unit herdr-unit, worktree {worktree}",
             "assigned-production", "herdr-unit"),
        )):
            with self.subTest(field=field):
                state = cast(dict[str, object], json.loads(state_path.read_text()))
                _ = state.pop("production", None)
                _ = state.pop("unit", None)
                state[field] = value
                _ = state_path.write_text(json.dumps(state))
                _ = plan.write_text(header + "\n", encoding="utf-8")
                _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", str(index + 1),
                                     "--phase-title", "Independent source fields", at=1010 + index)
                started = [event for event in self.read_events(name) if event["event_type"] == "phase_started"][-1]
                self.assertEqual(started["production"], {"state": "Recorded", "value": production})
                self.assertEqual(started["unit"], {"state": "Recorded", "value": unit})
                report = cast(dict[str, object], json.loads(self.run_command("waits", at=1040)))
                self.assertEqual(cast(list[dict[str, object]], report["phases"])[-1]["production"], started["production"])

    def test_waits_returns_json_when_a_legacy_plan_is_invalid_utf8_or_unreadable(self) -> None:
        name = "phase-unreadable-plan"
        session, worktree, plan = self.phase_source_fixture(name)
        _ = self.run_command("start-phase", "--session-dir", str(session), "--phase-id", "6",
                             "--phase-title", "Unreadable legacy plan", at=1010)
        events = self.read_events(name)
        for event in events:
            if event["event_type"] == "phase_started":
                for key in ("production", "unit", "repository", "wait_coverage"):
                    del event[key]
        _ = (self.history_dir / "runs" / f"{name}.jsonl").write_text("\n".join(json.dumps(event) for event in events))
        _ = plan.write_bytes(b"> **Production:** natedev\n\xff")
        for unavailable in ("invalid UTF-8", "missing file", "directory"):
            with self.subTest(unavailable=unavailable):
                if unavailable == "missing file":
                    plan.unlink()
                elif unavailable == "directory":
                    plan.mkdir()
                report = cast(dict[str, object], json.loads(self.run_command("waits", "--session-dir", str(session), at=1040)))
                phase, = cast(list[dict[str, object]], report["phases"])
                production = cast(dict[str, object], phase["production"])
                self.assertEqual(production["state"], "NotRecorded")
                self.assertIn("phase plan cannot be read:", str(production["reason"]))
                self.assertEqual(phase["repository"], {"state": "Recorded", "value": "hana"})
                self.assertEqual(phase["unit"], worktree.name)
                self.assertEqual(phase["state"], "HistoryNotRecorded")

    def test_wait_reader_does_not_create_a_missing_session_or_its_lock(self) -> None:
        missing = self.root / "absent-reader"
        result = self.run_failing_command("waits", "--session-dir", str(missing), at=1100)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(missing.exists())

    def test_wait_events_and_coverage_do_not_change_old_history_readers(self) -> None:
        session = self.start_run("wait-old-readers", 1000)
        self.start_phase_and_pass(session, 1010)
        wait_id = self.run_command("start-wait", "--session-dir", str(session), "--kind", "WaitingForMemoryOrSlot", at=1020)
        _ = self.run_command("finish-wait", "--session-dir", str(session), "--wait-id", wait_id, at=1030)
        _ = self.run_command("finish-phase", "--session-dir", str(session), "--status", "completed", at=1060)
        events = self.read_events("wait-old-readers")
        old = [{key: value for key, value in event.items() if key != "wait_coverage"} for event in events
               if not str(event["event_type"]).startswith("wait_")]
        self.assertEqual(load_phases(events), load_phases(old))
        normalize = cast(Callable[[str, dict[str, object], dict[str, int], dict[str, int]], object],
                         vars(stage_history)["_normalize"])
        rows = [normalize("unit-history", event, {}, {}) for event in events]
        old_rows = [normalize("unit-history", event, {}, {}) for event in old]
        self.assertEqual([row for row in rows if row is not None], [row for row in old_rows if row is not None])
        self.assertEqual(len([row for row in rows if row is not None]), 1)

    def test_merge_receipts_distinguish_source_and_merge_and_deduplicate_retries(self) -> None:
        with patch.dict(os.environ, {"UNIT_HISTORY_DIR": str(self.history_dir)}):
            first = progress_history.write_merge_receipt(project="hana", phase_id="6", merge_commit="merge-a",
                       source_checkpoint="source-a", target_branch="sdlc", merged_at=1020)
            retry = progress_history.write_merge_receipt(project="hana", phase_id="6", merge_commit="merge-a",
                       source_checkpoint="source-a", target_branch="sdlc", merged_at=1080)
            _ = progress_history.write_merge_receipt(project="hana", phase_id="child", merge_commit="merge-b",
                       source_checkpoint="source-b", target_branch="sdlc", merged_at=1030, issue="HI7")
        self.assertEqual(retry, first)
        report = cast(dict[str, object], json.loads(self.run_command("receipts", "--since", "1970-01-01T00:17:00Z",
                                    "--until", "1970-01-01T00:17:10Z", "--project", "hana", at=1100)))
        self.assertEqual((report["covered_n"], report["missing_n"]), (1, 0))
        receipt = cast(list[dict[str, object]], report["receipts"])[0]
        self.assertEqual((receipt["merge_commit"], receipt["source_checkpoint"], receipt["target_branch"], receipt["phase_id"]),
                         ("merge-a", "source-a", "sdlc", "6"))
        self.assertEqual(receipt["merged_at"], "1970-01-01T00:17:00.000+00:00")
        missing = cast(dict[str, object], json.loads(self.run_command("receipts", "--merge-commit", "without-receipt", at=1100)))
        self.assertEqual((missing["covered_n"], missing["missing_n"], missing["missing"]),
                         (0, 1, [{"state": "ReceiptMissing", "merge_commit": "without-receipt"}]))
        self.assertEqual(len((self.history_dir / "merge_receipts.jsonl").read_text().splitlines()), 2)

    def test_receipt_reader_reports_missing_history_without_creating_it(self) -> None:
        report = cast(dict[str, object], json.loads(self.run_command("receipts", "--merge-commit", "missing", at=1100)))
        self.assertEqual((report["covered_n"], report["missing_n"]), (0, 1))
        self.assertFalse(self.history_dir.exists())

    def test_start_phase_records_work_order_size(self) -> None:
        started_at = 60_000
        session_dir = self.start_run("sized", started_at)
        work_order = self.root / "work_order.md"
        work_order_lines = [
            "**Goal:** wire the retry path",
            "",
            "- touch `src/retry.rs`",
            "- touch `src/lib.rs`",
            "- keep `RetryBudget` intact",
        ]
        _ = work_order.write_text(
            "\n".join(work_order_lines) + "\n",
            encoding="utf-8",
        )
        _ = self.run_command(
            "start-phase",
            "--session-dir",
            str(session_dir),
            "--phase-id",
            "3",
            "--phase-title",
            "Retry handling",
            "--work-order-file",
            str(work_order),
            at=started_at,
        )
        history_text = (self.history_dir / "runs" / "sized.jsonl").read_text(encoding="utf-8")
        self.assertIn('"work_order_lines":4', history_text)
        self.assertIn('"work_order_top_level_bullets":3', history_text)
        self.assertIn('"work_order_file_targets":2', history_text)

    def run_unowned_command(
        self, *arguments: str, at: int, former: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Invoke the recorder the way a caller outside a launcher would, with `former` names added."""
        environment = os.environ.copy()
        environment["UNIT_HISTORY_DIR"] = str(self.history_dir)
        environment["UNIT_CONFIG"] = str(self.config_file)
        environment["AGENTS_CONFIG_FILE"] = str(self.agents_config_file)
        environment["TZ"] = "UTC"
        environment["USER_ZONE"] = "UTC"
        environment["UNIT_NOW_EPOCH"] = str(at)
        _ = environment.pop("UNIT_PASS_OWNER", None)
        if self.team_slot:
            environment["UNIT_TEAM_ROLE"] = self.team_slot
        else:
            _ = environment.pop("UNIT_TEAM_ROLE", None)
        self.isolate_recorder_roots(environment)
        self.isolate_identity(environment)
        environment.update(former or {})
        return invoke_recorder(self.valid_launch_arguments(arguments), environment, Path.cwd())

    def test_starting_a_phase_reactivates_a_finished_run_without_replacing_its_workers(self) -> None:
        for status in ("completed", "error"):
            with self.subTest(status=status):
                name = f"reactivate-{status}"
                session_dir = self.start_run(name, 20_000)
                roster = session_dir / "mesh_roster.json"
                original = json.dumps({"unrelated-address": {
                    "thread_id": "kept-thread", "role": "impl", "status": "paused",
                    "model": "gpt-fixture-sol",
                }})
                _ = roster.write_text(original, encoding="utf-8")
                _ = self.run_command("finish-run", "--session-dir", str(session_dir),
                                     "--status", status, at=20_020)
                active = self.root / "active"
                active.mkdir(exist_ok=True)
                marker = active / f"main-{name}"
                with patch.dict(os.environ, {"UNIT_ACTIVE_DIR": str(active)}):
                    _ = self.run_command(
                        "start-phase", "--session-dir", str(session_dir),
                        "--phase-id", "4", "--phase-title", "Resume the unit", at=20_030,
                        claude_session=f"main-{name}",
                    )
                state = self.read_state(session_dir)
                self.assertEqual(state["status"], "active")
                self.assertNotIn("finished_at", state)
                self.assertEqual(marker.read_text().strip(), str(session_dir))
                self.assertEqual(roster.read_text(encoding="utf-8"), original)
                events = self.read_events(name)
                self.assertEqual(len([event for event in events
                                     if event.get("event_type") == "reopened"]), 1)
                self.assertEqual(events[-1]["event_type"], "phase_started")

    def test_starting_the_same_phase_twice_records_only_one_run_reactivation(self) -> None:
        session_dir = self.start_run("reactivate-once", 20_000)
        _ = self.run_command("finish-run", "--session-dir", str(session_dir),
                             "--status", "completed", at=20_020)
        arguments = ("start-phase", "--session-dir", str(session_dir),
                     "--phase-id", "4", "--phase-title", "Resume once")
        with patch.dict(os.environ, {"UNIT_ACTIVE_DIR": str(self.root / "active")}):
            _ = self.run_command(*arguments, at=20_030, claude_session="main-reactivate-once")
            _ = self.run_command(*arguments, at=20_040, claude_session="main-reactivate-once")
        events = self.read_events("reactivate-once")
        self.assertEqual([event["event_type"] for event in events].count("reopened"), 1)
        self.assertEqual([event["event_type"] for event in events].count("phase_started"), 1)

    def test_a_completed_phase_clears_its_enrolled_state_and_a_reopen_restores_it(self) -> None:
        session_dir = self.start_run("reopen", 20_000)
        start = ("start-phase", "--session-dir", str(session_dir), "--phase-id", "3", "--phase-title", "Retry handling")
        _ = self.run_command(*start, at=20_010)
        phase_dir = phase_reservation.phase_directory(session_dir, "3")
        _ = phase_reservation.enroll(phase_dir, "3", "board_ready")
        _ = self.run_command("finish-phase", "--session-dir", str(session_dir), "--status", "completed", at=20_020)
        self.assertFalse((phase_dir / phase_reservation.STATE_FILE).exists())
        _ = self.run_command(*start, at=20_030)
        self.assertEqual(phase_reservation.read_state(phase_dir), {"kind": "enrolled_awaiting_first_touch", "phase": "3"})

    def test_a_launcher_started_before_the_rename_owns_its_pass_by_the_former_names(self) -> None:
        session_dir = self.start_run("former", 20_000)
        _ = self.run_command("start-phase", "--session-dir", str(session_dir), "--phase-id", "3",
                             "--phase-title", "Retry handling", at=20_010)
        started = self.run_unowned_command(
            "start-pass", "--session-dir", str(session_dir), "--pass-kind", "review",
            "--activity", "reviewing", "--called-task", "direct.review", "--called-family", "codex",
            "--called-model", "gpt-called", at=20_020,
            former={"PLAN_DELEGATE_PASS_OWNER": "launcher", "PLAN_DELEGATE_TEAM_ROLE": "former-slot"})
        self.assertEqual(started.returncode, 0, started.stderr)
        history = (self.history_dir / "runs" / "former.jsonl").read_text(encoding="utf-8")
        self.assertIn('"team_slot":"former-slot"', history)

    def test_start_pass_outside_a_launcher_is_refused(self) -> None:
        session_dir = self.start_run("owned", 20_000)
        _ = self.run_command(
            "start-phase",
            "--session-dir",
            str(session_dir),
            "--phase-id",
            "3",
            "--phase-title",
            "Retry handling",
            at=20_010,
        )
        failure = self.run_unowned_command(
            "start-pass",
            "--session-dir",
            str(session_dir),
            "--pass-kind",
            "review",
            "--activity",
            "reviewing by hand",
            "--called-task",
            "direct.review",
            "--called-family",
            "codex",
            "--called-model",
            "gpt-called",
            at=20_020,
        )
        self.assertNotEqual(failure.returncode, 0)
        self.assertIn("launcher", failure.stderr)
        history = self.history_dir / "runs" / "owned.jsonl"
        self.assertNotIn("pass_started", history.read_text(encoding="utf-8"))

    def test_finish_pass_outside_a_launcher_only_cancels_an_orphan(self) -> None:
        session_dir = self.start_run("orphan", 20_000)
        self.start_phase_and_pass(session_dir, 20_010)
        unowned = self.run_unowned_command(
            "finish-pass", "--session-dir", str(session_dir), "--status", "completed", at=20_030
        )
        self.assertNotEqual(unowned.returncode, 0)
        self.assertIn("launcher", unowned.stderr)
        wrong_status = self.run_unowned_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            "--orphaned-launcher",
            at=20_040,
        )
        self.assertNotEqual(wrong_status.returncode, 0)
        self.assertIn("--status canceled", wrong_status.stderr)
        canceled = self.run_unowned_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "canceled",
            "--orphaned-launcher",
            at=20_050,
        )
        self.assertEqual(canceled.returncode, 0, canceled.stderr)
        history = (self.history_dir / "runs" / "orphan.jsonl").read_text(encoding="utf-8")
        self.assertIn('"status":"canceled"', history.replace(" ", ""))
        repeated = self.run_unowned_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "canceled",
            "--orphaned-launcher",
            at=20_060,
        )
        self.assertNotEqual(repeated.returncode, 0)
        self.assertIn("No pass is open", repeated.stderr)

    def pass_events(self, name: str, event_type: str) -> list[dict[str, object]]:
        return [
            event
            for event in self.read_events(name)
            if event.get("event_type") == event_type
        ]

    def test_a_pass_records_a_digest_of_the_configuration_it_ran_under(self) -> None:
        _ = write_registry(self.agents_config_file, {"direct.codex": {"impl": "gpt-called:xhigh"}})
        session_dir = self.start_run("digest", 20_000)
        self.start_phase_and_pass(session_dir, 20_010)
        expected = hashlib.sha256(
            self.agents_config_file.read_bytes() + self.config_file.read_bytes()
        ).hexdigest()[:12]
        started = self.pass_events("digest", "pass_started")
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].get("config_digest"), expected)

        # A conf edited between two passes has to move the digest, or grouping by
        # it would compare passes that ran under configurations that differ.
        self.write_interval(240)
        _ = self.run_command(
            "start-pass",
            "--session-dir",
            str(session_dir),
            "--pass-kind",
            "review",
            "--activity",
            "reviewing the retry path",
            "--called-task",
            "direct.review",
            "--called-family",
            "codex",
            "--called-model",
            "gpt-called",
            "--called-effort",
            "high",
            at=20_100,
        )
        started = self.pass_events("digest", "pass_started")
        self.assertEqual(len(started), 2)
        second = cast(str, started[1]["config_digest"])
        self.assertEqual(len(second), 12)
        self.assertNotEqual(second, expected)

    def test_a_pass_records_the_team_worker_that_launched_it(self) -> None:
        """Which of the three members recorded this pass."""
        self.team_slot = "impl"
        session_dir = self.start_run("slot", 23_000)
        self.start_phase_and_pass(session_dir, 23_010)
        started = self.pass_events("slot", "pass_started")
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].get("team_slot"), "impl")

    def test_a_pass_with_no_worker_records_an_empty_team_slot(self) -> None:
        """Empty is unknown, not a worker name.

        Passes predating the field, and any recorder outside implement.sh, have
        to stay separable from a launcher that really is the `impl` member.
        """
        session_dir = self.start_run("no-slot", 24_000)
        self.start_phase_and_pass(session_dir, 24_010)
        started = self.pass_events("no-slot", "pass_started")
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].get("team_slot"), "")

    def test_an_unreadable_conf_leaves_the_digest_empty_and_records_the_pass(self) -> None:
        self.agents_config_file.unlink()
        session_dir = self.start_run("no-registry", 21_000)
        self.start_phase_and_pass(session_dir, 21_010)
        started = self.pass_events("no-registry", "pass_started")
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].get("config_digest"), "")

    def test_the_finished_pass_carries_the_worker_that_opened_it(self) -> None:
        """The reader joins on finished events, so the worker has to survive there.

        Taken from the pass record rather than the environment: the phase ends
        under the unit director, which holds no worker at all, and the pass it closes
        still has to name the member that opened it.
        """
        self.team_slot = "review"
        session_dir = self.start_run("slot-finish", 25_000)
        self.start_phase_and_pass(session_dir, 25_010)
        self.team_slot = ""
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=25_500,
        )
        finished = self.pass_events("slot-finish", "pass_finished")
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].get("team_slot"), "review")

    def start_phase(self, session_dir: Path, at: int) -> None:
        _ = self.run_command(
            "start-phase",
            "--session-dir",
            str(session_dir),
            "--phase-id",
            "3",
            "--phase-title",
            "Retry handling",
            at=at,
        )

    def run_board(self, session_dir: Path, *arguments: str, at: int) -> None:
        """Drive board.sh on the test's clock, so its stamps and the recorder's
        events sit on one timeline instead of the board landing in the present."""
        environment = os.environ.copy()
        environment["UNIT_NOW_EPOCH"] = str(at)
        _ = subprocess.run(
            ["bash", str(BOARD), arguments[0], str(session_dir), *arguments[1:]],
            check=True,
            capture_output=True,
            env=environment,
        )

    def start_slot_pass(
        self,
        session_dir: Path,
        slot: str,
        pass_kind: str,
        at: int,
        called_model: str = "gpt-called",
        fix_pass: int = 0,
        *extra: str,
        worker_name: str = "",
    ) -> None:
        """Open a pass the way one member of a phase team opens its own."""
        self.team_slot = slot
        _ = self.run_command(
            "start-pass",
            "--session-dir",
            str(session_dir),
            "--pass-kind",
            pass_kind,
            "--fix-pass",
            str(fix_pass),
            "--activity",
            f"{slot or 'unslotted'} work",
            "--called-task",
            "direct.implementation",
            "--called-family",
            "codex",
            "--called-model",
            called_model,
            "--called-effort",
            "high",
            *extra,
            *(("--worker-name", worker_name) if worker_name else ()),
            at=at,
        )

    def finish_slot_pass(
        self,
        session_dir: Path,
        slot: str,
        status: str,
        at: int,
    ) -> None:
        self.team_slot = slot
        _ = self.run_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            status,
            at=at,
        )

    def pass_slots(self, session_dir: Path) -> dict[str, dict[str, object]]:
        """The recorder's per-slot pass records, as they sit on disk."""
        parsed: object = json.loads(  # pyright: ignore[reportAny]
            (session_dir / "progress_history_state.json").read_text(encoding="utf-8")
        )
        state = cast(dict[str, object], parsed)
        slots = cast(dict[str, object], state["pass"])
        return {slot: cast(dict[str, object], record) for slot, record in slots.items()}

    def demote_to_legacy_pass(self, session_dir: Path, keep_worker: bool) -> None:
        """Rewrite state the way a session that started before workers holds it.

        One pass object under the key rather than a map, and the worker under the
        `team_role` name it was stamped with for a day, which is what every run
        already in flight has on disk when this recorder replaces the old one.
        """
        path = session_dir / "progress_history_state.json"
        parsed: object = json.loads(path.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]
        state = cast(dict[str, object], parsed)
        slots = cast(dict[str, object], state["pass"])
        [record_value] = list(slots.values())
        record = cast(dict[str, object], record_value)
        worker = record.pop("team_slot", "")
        if keep_worker:
            record["team_role"] = worker
        state["pass"] = record
        _ = path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    def test_two_slots_hold_a_pass_of_their_own_at_the_same_time(self) -> None:
        """A phase team runs a launcher per slot and the recorder holds them all.

        With a single pass the second launcher to start closed the first, so the
        ledger described whichever member happened to finish last.
        """
        session_dir = self.start_run("two-slots", 40_000)
        self.start_phase(session_dir, 40_010)
        self.start_slot_pass(session_dir, "impl", "impl", 40_020)
        self.start_slot_pass(session_dir, "test", "fix", 40_030)
        slots = self.pass_slots(session_dir)
        self.assertEqual(sorted(slots), ["impl", "test"])
        self.assertEqual(slots["impl"]["status"], "active")
        self.assertEqual(slots["test"]["status"], "active")
        self.assertEqual(
            [
                event.get("team_slot")
                for event in self.pass_events("two-slots", "pass_started")
            ],
            ["impl", "test"],
        )
        self.assertEqual(self.pass_events("two-slots", "pass_finished"), [])

    def test_three_slots_each_record_a_start_and_a_finish_of_their_own(self) -> None:
        """The whole team's work reaches the ledger, in the order it happened."""
        session_dir = self.start_run("three-slots", 41_000)
        self.start_phase(session_dir, 41_010)
        for offset, (slot, kind) in enumerate(
            (("impl", "impl"), ("test", "fix"), ("review", "review"))
        ):
            self.start_slot_pass(session_dir, slot, kind, 41_020 + offset)
        for offset, slot in enumerate(("test", "review", "impl")):
            self.finish_slot_pass(session_dir, slot, "completed", 41_100 + offset)
        started = self.pass_events("three-slots", "pass_started")
        self.assertEqual(
            [event.get("team_slot") for event in started],
            ["impl", "test", "review"],
        )
        finished = self.pass_events("three-slots", "pass_finished")
        self.assertEqual(
            [event.get("team_slot") for event in finished],
            ["test", "review", "impl"],
        )
        self.assertEqual(
            {str(event.get("status")) for event in finished},
            {"completed"},
        )
        # Each finish names the window its own slot opened, never a peer's.
        self.assertEqual(
            {
                str(event.get("team_slot")): str(event.get("pass_instance_id"))
                for event in finished
            },
            {
                slot: str(record["instance_id"])
                for slot, record in self.pass_slots(session_dir).items()
            },
        )

    def test_a_slot_reopening_a_pass_interrupts_only_its_own(self) -> None:
        """A stale pass is this slot's problem and nobody else's.

        The peers belong to launchers still waiting on their own agents, so the
        second dispatch of one member must leave their windows untouched.
        """
        session_dir = self.start_run("reopen", 42_000)
        self.start_phase(session_dir, 42_010)
        self.start_slot_pass(session_dir, "impl", "impl", 42_020)
        self.start_slot_pass(session_dir, "test", "fix", 42_030)
        peer_instance = self.pass_slots(session_dir)["test"]["instance_id"]
        self.start_slot_pass(session_dir, "impl", "fix", 42_040)
        finished = self.pass_events("reopen", "pass_finished")
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].get("team_slot"), "impl")
        self.assertEqual(finished[0].get("status"), "interrupted")
        slots = self.pass_slots(session_dir)
        self.assertEqual(slots["test"]["status"], "active")
        self.assertEqual(slots["test"]["instance_id"], peer_instance)
        self.assertEqual(slots["impl"]["status"], "active")

    def test_a_peer_cannot_finish_the_pass_another_slot_opened(self) -> None:
        """finish-pass answers for the slot the environment names and no other.

        The launcher that finishes is the launcher that started; a member that
        closed a peer's window would end a pass whose agent is still working.
        """
        session_dir = self.start_run("peer-finish", 43_000)
        self.start_phase(session_dir, 43_010)
        self.start_slot_pass(session_dir, "impl", "impl", 43_020)
        self.finish_slot_pass(session_dir, "test", "completed", 43_100)
        self.assertEqual(self.pass_events("peer-finish", "pass_finished"), [])
        self.assertEqual(self.pass_slots(session_dir)["impl"]["status"], "active")

    def test_a_legacy_single_pass_object_still_finishes(self) -> None:
        """A run already in flight keeps the pass it had open.

        Its state holds one pass object instead of a map, so it reads as the
        entry for the slot recorded on it and its launcher closes it exactly as
        it would have.
        """
        session_dir = self.start_run("legacy", 44_000)
        self.start_phase(session_dir, 44_010)
        self.start_slot_pass(session_dir, "impl", "impl", 44_020)
        instance = self.pass_slots(session_dir)["impl"]["instance_id"]
        self.demote_to_legacy_pass(session_dir, keep_worker=True)
        self.finish_slot_pass(session_dir, "impl", "completed", 44_100)
        finished = self.pass_events("legacy", "pass_finished")
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].get("pass_instance_id"), instance)
        self.assertEqual(finished[0].get("team_slot"), "impl")
        self.assertEqual(self.pass_slots(session_dir)["impl"]["status"], "completed")

    def test_a_legacy_pass_with_no_slot_reads_as_the_empty_slot(self) -> None:
        """State written before the slot field existed carries no slot at all.

        Empty is a slot like any other, so the recorder that opened it without
        one is the recorder that closes it.
        """
        session_dir = self.start_run("legacy-unslotted", 45_000)
        self.start_phase(session_dir, 45_010)
        self.start_slot_pass(session_dir, "", "impl", 45_020)
        instance = self.pass_slots(session_dir)[""]["instance_id"]
        self.demote_to_legacy_pass(session_dir, keep_worker=False)
        self.finish_slot_pass(session_dir, "", "completed", 45_100)
        finished = self.pass_events("legacy-unslotted", "pass_finished")
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].get("pass_instance_id"), instance)
        self.assertEqual(finished[0].get("team_slot"), "")
        self.assertEqual(self.pass_slots(session_dir)[""]["status"], "completed")

    def test_finishing_the_run_closes_every_open_slot(self) -> None:
        """A run that stops mid-phase leaves no window open behind it.

        The launchers that would have closed the peers are the processes that
        went away with the run, so the cleanup that ends the phase has to close
        all of them or they stay open in the ledger forever.
        """
        session_dir = self.start_run("run-end", 46_000)
        self.start_phase(session_dir, 46_010)
        for offset, (slot, kind) in enumerate(
            (("impl", "impl"), ("test", "fix"), ("review", "review"))
        ):
            self.start_slot_pass(session_dir, slot, kind, 46_020 + offset)
        self.team_slot = ""
        _ = self.run_command(
            "finish-run",
            "--session-dir",
            str(session_dir),
            "--status",
            "stopped",
            at=46_200,
        )
        finished = self.pass_events("run-end", "pass_finished")
        self.assertEqual(
            sorted(str(event.get("team_slot")) for event in finished),
            ["impl", "review", "test"],
        )
        self.assertEqual(
            {str(event.get("status")) for event in finished},
            {"interrupted"},
        )
        self.assertEqual(
            {str(record["status"]) for record in self.pass_slots(session_dir).values()},
            {"interrupted"},
        )

    def test_a_round_gives_every_open_worker_its_own_column(self) -> None:
        """Two agents at work read across one row, not as whichever wrote last."""
        session_dir = self.start_run("live-rows", 47_000)
        self.start_phase(session_dir, 47_010)
        self.start_slot_pass(session_dir, "impl", "impl", 47_020, called_model="gpt-impl")
        self.start_slot_pass(session_dir, "test", "test", 47_030, called_model="gpt-test")
        self.team_slot = ""
        header = self.run_progress(session_dir, 47_100)
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # One row, because both workers are working one round. Each carries its
        # own elapsed: they launch together but do not finish together, and a
        # single shared number would hide exactly that.
        self.assertEqual(
            [(row[0], *row[3:5], row[2], row[5]) for row in rows],
            [
                (
                    "Impl",
                    "impl 1m running",
                    "test 1m running",
                    "1m",
                    "running",
                )
            ],
        )
        # The unit director holds no slot, so its report names the window that
        # opened first rather than the one that opened last.
        self.assertIn("▸ **Impl - implementing**", header)

    def test_a_worker_that_says_it_is_held_up_does_not_read_as_working(self) -> None:
        """A worker waiting on its peer has an open window and the peer's clock."""
        session_dir = self.start_run("waiting", 51_000)
        self.start_phase(session_dir, 51_010)
        for slot, kind in (("impl", "impl"), ("test", "test")):
            self.start_slot_pass(session_dir, slot, kind, 51_020)
        self.run_board(
            session_dir, "post", "test", "status",
            "Removing a redundant assertion", at=51_100,
        )
        self.run_board(
            session_dir, "post", "impl", "status",
            "Waiting for the test owner's final edit to land", at=51_110,
        )
        self.team_slot = ""
        rows = self.table_rows(
            self.run_progress(session_dir, 51_200),
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # Nothing in the pass records separates these two: both windows are
        # open and both clocks read the same. One is sitting on the other, and
        # only what each worker said last can say so.
        self.assertEqual(
            list(rows[-1][3:5]),
            ["impl 3m waiting", "test 3m running"],
        )
        # The board has a kind for it as well as words, and both count: a worker
        # that posted `blocked` said the same thing in the field rather than in
        # the sentence.
        self.run_board(
            session_dir, "post", "test", "blocked",
            "the integration-denial regression has not landed yet", at=51_220,
        )
        rows = self.table_rows(
            self.run_progress(session_dir, 51_300),
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        self.assertEqual(
            list(rows[-1][3:5]),
            ["impl 4m waiting", "test 4m waiting"],
        )

    def test_staggered_launcher_registers_do_not_split_a_round(self) -> None:
        """Two launchers coming up seconds apart is one round, not a row per launch."""
        session_dir = self.start_run("stagger", 46_000)
        self.start_phase(session_dir, 46_010)
        self.start_slot_pass(session_dir, "impl", "impl", 46_020)
        # What implement.sh writes: each launcher registers with its opening role
        # stamped, and they land a few seconds apart because they start in
        # sequence. That stagger is a roll-call, not the team changing shape.
        for offset, slot, role in ((0, "impl", "impl"), (3, "test", "test")):
            self.run_board(session_dir, "post", slot, "register", f"role={role}; up", at=46_020 + offset)
        self.start_slot_pass(session_dir, "test", "test", 46_023)
        self.team_slot = ""
        rows = self.table_rows(
            self.run_progress(session_dir, 50_540),
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # One row. Before the register/handoff split this rendered a near-empty
        # row per launch, seconds long, ahead of the real one.
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0][0], *rows[0][3:5], rows[0][2]),
            (
                "Impl",
                "impl 1h15m running",
                "test 1h15m running",
                "1h15m",
            ),
        )

    def test_a_round_gains_a_row_each_time_its_workers_change_role(self) -> None:
        """Workers recruit each other, and every new shape gets its own row."""
        session_dir = self.start_run("recruit", 49_000)
        self.start_phase(session_dir, 49_010)
        for slot in ("impl", "test"):
            self.run_board(session_dir, "post", slot, "register", "up", at=49_015)
        # Opens one writing and one testing.
        for slot, role in (("impl", "impl"), ("test", "test")):
            self.run_board(session_dir, "role", slot, role, "opening", at=49_020)
        for slot, kind in (("impl", "impl"), ("test", "test")):
            self.start_slot_pass(session_dir, slot, kind, 49_020)
        self.team_slot = ""
        # The test worker is recruited across to write, then both converge on
        # review.
        self.run_board(session_dir, "role", "test", "impl", "recruited", at=49_320)
        for slot in ("impl", "test"):
            self.run_board(session_dir, "role", slot, "review", "converging", at=49_620)
        header = self.run_progress(session_dir, 49_800)
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # One round, three shapes. The label names the round once: a repeat on
        # the continuation rows would read as three rounds rather than as one
        # team moving, and the result belongs to the round, so it sits on the
        # row that closes it.
        self.assertEqual(
            [(row[0], *row[3:5], row[2]) for row in rows],
            [
                ("Impl", "impl 5m done", "test 5m done", "5m"),
                ("", "impl 5m done", "impl 5m done", "5m"),
                ("", "review 3m running", "review 3m running", "3m"),
            ],
        )
        # A continuation row names the movement that opened it in its result
        # cell, so the reader is not left diffing two rows of cells to find
        # where the team moved; the round's tally still sits on the closing
        # row, after the movement that opened it.
        self.assertEqual(
            [row[5] for row in rows],
            [
                "",
                "Agent 2 → impl",
                "Agent 1 → review, Agent 2 → review; running",
            ],
        )
        # Which worker is in which worker sits under the table, once, and the
        # unit director is left out of it -- the reader is the unit director. Each
        # worker carries its last board line and that line's age, because a role
        # alone -- and an open pass window, which grows either way -- cannot say
        # whether a worker is working or has been silent since it took the role.
        for label, slot in (("Agent 1", "impl"), ("Agent 2", "test")):
            self.assertIn(
                f"- **{label}** ({slot}) gpt-called high · 3m ago · handoff: converging",
                header,
            )

    def test_a_role_reannouncement_does_not_split_a_round(self) -> None:
        """A handoff that re-states the role its slot already holds is not a movement.

        What live runs write: each worker opens with a `board.sh role` seconds
        after its launcher registered the same role, and a worker routes narration
        through `role` because it is the command that takes a note. Every such
        line used to open a near-empty row whose columns matched the row above.
        """
        session_dir = self.start_run("reannounce", 52_000)
        self.start_phase(session_dir, 52_010)
        # The test slot is recruited to write from second zero, the shape
        # fe9ae569 ran: its launcher registers role=impl and its pass kind is
        # impl, so the role it holds is not the one its slot name suggests.
        for offset, slot, kind in ((0, "impl", "impl"), (3, "test", "impl")):
            self.run_board(
                session_dir, "post", slot, "register", f"role={kind}; up", at=52_020 + offset
            )
            self.start_slot_pass(session_dir, slot, kind, 52_020 + offset)
        # The opening announcements, a second apart, each naming the role the
        # register already stamped.
        for offset, slot, kind in ((15, "impl", "impl"), (16, "test", "impl")):
            self.run_board(session_dir, "role", slot, kind, "opening task", at=52_020 + offset)
        # Narration routed through `role`, minutes apart, role unchanged.
        self.run_board(session_dir, "role", "test", "impl", "rechecking storage", at=53_500)
        self.run_board(session_dir, "role", "test", "impl", "validating exports", at=54_100)
        self.team_slot = ""
        rows = self.table_rows(
            self.run_progress(session_dir, 54_400),
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # One row: nothing moved, so nothing splits.
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0][0], *rows[0][3:5]),
            ("Impl", "impl 39m running", "impl 39m running"),
        )

    def test_movements_seconds_apart_are_one_row(self) -> None:
        """Workers converging one `board.sh role` at a time is one movement.

        The calls land seconds apart because the workers run in sequence, and a
        boundary per call would put a near-empty row on each. One boundary, and
        the roles the later calls carry describe the row from its start.
        """
        session_dir = self.start_run("converge", 55_000)
        self.start_phase(session_dir, 55_010)
        for slot, role in (("impl", "impl"), ("test", "test")):
            self.run_board(session_dir, "post", slot, "register", f"role={role}; up", at=55_020)
            self.start_slot_pass(session_dir, slot, role, 55_020)
        for offset, slot in ((300, "impl"), (302, "test")):
            self.run_board(session_dir, "role", slot, "review", "converging", at=55_020 + offset)
        self.team_slot = ""
        rows = self.table_rows(
            self.run_progress(session_dir, 55_500),
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        self.assertEqual(
            [(row[0], *row[3:5], row[5]) for row in rows],
            [
                ("Impl", "impl 5m done", "test 5m done", ""),
                (
                    "",
                    "review 3m running",
                    "review 3m running",
                    "Agent 1 → review, Agent 2 → review; running",
                ),
            ],
        )

    def run_gate(
        self,
        session_dir: Path,
        text: str,
        started_at: int,
        finished_at: int | None,
        passed: bool = True,
    ) -> None:
        """One verification command, recorded the way the unit director runs it."""
        _ = self.run_command(
            "start-activity",
            "--session-dir",
            str(session_dir),
            "--label",
            "Verification",
            "--activity",
            text,
            at=started_at,
        )
        if finished_at is None:
            return
        _ = self.run_command(
            "finish-activity",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed" if passed else "error",
            "--result",
            "pass" if passed else "fail",
            at=finished_at,
        )

    def test_a_verification_block_is_one_row_with_its_gates_beneath(self) -> None:
        """Gates run one command at a time, and a row per command was noise.

        The pattern a live run rendered as seven anonymous rows: two gates
        fail, the unit director repairs, the reruns pass. One row tells the
        block's outcome; the notes under the table name each gate, with a
        retry folded into the gate it reran rather than listed as a fresh one.
        """
        base = 70_000
        session_dir = self.start_run("gates", base - 100)
        self.start_phase(session_dir, base - 50)
        self.run_gate(session_dir, "test hana", base, base + 30, passed=False)
        self.run_gate(session_dir, "test hana_catalyst", base + 31, base + 36)
        self.run_gate(session_dir, "test hana_catalyst identity", base + 37, base + 39)
        self.run_gate(session_dir, "lint hana", base + 40, base + 47, passed=False)
        self.run_gate(session_dir, "lint hana_catalyst", base + 48, base + 49)
        self.run_gate(session_dir, "test hana", base + 107, base + 170)
        self.run_gate(session_dir, "lint hana", base + 171, base + 182)
        self.run_gate(session_dir, "doc hana", base + 200, None)
        self.team_slot = ""
        header = self.run_progress(session_dir, base + 212)
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        self.assertEqual(
            [(row[0], row[2], *row[3:5], row[5]) for row in rows],
            [("Verification", "3m", "-", "-", "gate 6 running")],
        )
        for line in (
            "- **Verification** (unit director) · 6 gates:",
            "  - ✗→✓ test hana · 30s failed · passed on rerun 1m",
            "  - ✓ test hana_catalyst · 5s",
            "  - ✓ test hana_catalyst identity · 2s",
            "  - ✗→✓ lint hana · 7s failed · passed on rerun 11s",
            "  - ✓ lint hana_catalyst · 1s",
            "  - ▸ doc hana · running 12s",
        ):
            self.assertIn(line, header)

    def test_a_finished_verification_block_reports_its_retries(self) -> None:
        """The Result cell alone answers whether verification went clean."""
        base = 74_000
        session_dir = self.start_run("gates-done", base - 100)
        self.start_phase(session_dir, base - 50)
        self.run_gate(session_dir, "test hana", base, base + 30, passed=False)
        self.run_gate(session_dir, "lint hana", base + 40, base + 47)
        self.run_gate(session_dir, "test hana", base + 107, base + 170)
        # A pass keeps the report alive once the gates are over; the block
        # stays a closed stage beside it.
        self.start_slot_pass(session_dir, "test", "review", base + 300)
        self.team_slot = ""
        header = self.run_progress(session_dir, base + 360)
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        self.assertEqual(
            [(row[0], row[5]) for row in rows],
            [("Verification", "1 failed, reran clean"), ("Review", "running")],
        )
        self.assertIn("- **Verification** (unit director) · 2 gates:", header)

    def test_a_second_round_is_a_second_row(self) -> None:
        """A repair round is a row of its own, named by the number it carries."""
        session_dir = self.start_run("rounds", 48_000)
        self.start_phase(session_dir, 48_010)
        for slot, kind in (("impl", "impl"), ("test", "test")):
            self.start_slot_pass(session_dir, slot, kind, 48_020)
            self.finish_slot_pass(session_dir, slot, "completed", 48_200)
        self.start_slot_pass(session_dir, "impl", "fix", 48_300, fix_pass=1)
        self.team_slot = ""
        header = self.run_progress(session_dir, 48_400)
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        self.assertEqual(
            [(row[0], row[3], row[5]) for row in rows],
            [("Impl", "impl 3m done", "clean"), ("Fix 1", "fix 1m running", "running")],
        )

    def test_finish_pass_ignores_awake_seconds_from_an_older_launcher(self) -> None:
        """A launcher started before the flag was retired still finishes its pass."""
        session_dir = self.start_run("older-launcher", 22_000)
        self.start_phase_and_pass(session_dir, 22_010)
        _ = self.run_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            "--agent-awake-seconds",
            "2100",
            at=22_200,
        )
        finished = self.pass_events("older-launcher", "pass_finished")
        self.assertEqual(len(finished), 1)
        self.assertNotIn("agent_awake_seconds", finished[0])

    def test_aggregate_reports_raw_and_calibrated_error_fields(self) -> None:
        self.complete_historical_run(0)
        output = self.run_command("aggregate", "--percent", "65", at=30_000)
        parsed: object = json.loads(output)  # pyright: ignore[reportAny]
        aggregate = cast(dict[str, object], parsed)
        groups = cast(list[object], aggregate["groups"])
        self.assertEqual(len(groups), 1)
        group = cast(dict[str, object], groups[0])
        self.assertEqual(group["sample_count"], 1)
        self.assertIn("median_raw_absolute_error_percentage_points", group)
        self.assertIn("median_suggested_absolute_error_percentage_points", group)
        self.assertIn("median_reported_absolute_error_percentage_points", group)

    def test_override_requires_and_records_reason(self) -> None:
        for index in range(5):
            self.complete_historical_run(index)

        started_at = 20_000
        session_dir = self.start_run("override", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.run_command(
            "calibrate",
            "--session-dir",
            str(session_dir),
            "--candidate-percent",
            "65",
            at=started_at + 100,
        )
        failure = self.run_failing_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--raw-percent",
            "65",
            "--percent",
            "35",
            "--activity",
            "correcting retry recovery",
            at=started_at + 100,
        )
        self.assertNotEqual(failure.returncode, 0)
        self.assertIn("--override-reason is required", failure.stderr)

        reason = "three of four recovery checks now pass"
        _ = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--raw-percent",
            "65",
            "--percent",
            "35",
            "--activity",
            "correcting retry recovery",
            "--override-reason",
            reason,
            at=started_at + 100,
        )
        history_file = self.history_dir / "runs" / "override.jsonl"
        parsed_events: list[dict[str, object]] = []
        for line in history_file.read_text(encoding="utf-8").splitlines():
            parsed: object = json.loads(line)  # pyright: ignore[reportAny]
            parsed_events.append(cast(dict[str, object], parsed))
        progress_events = [
            event for event in parsed_events if event.get("event_type") == "progress_reported"
        ]
        self.assertEqual(len(progress_events), 1)
        progress_event = progress_events[0]
        self.assertEqual(progress_event["decision_source"], "override")
        self.assertEqual(progress_event["override_reason"], reason)
        self.assertEqual(progress_event["suggested_percent"], 25)
        self.assertEqual(progress_event["suggested_adjustment_percentage_points"], -40)
        self.assertEqual(progress_event["reported_adjustment_percentage_points"], -30)

    def test_neighbouring_percents_in_one_stage_draw_one_steady_eta(self) -> None:
        """A few points' change in the entered percent moves the ETA a little.

        Calibrating on the exact number gave the SWING_REPORTS history's four
        neighbouring entries suggestions of 40, 20, 55 and 15, so an hour into
        the phase the ETA read 08:03, 10:33, 07:22 and 12:13 for one unchanged
        state of the work. Drawn from the stage's history around each number,
        the four land within one five-point step and half an hour of each other.
        """
        self.seed_calibration_history("swing", "implementation", SWING_REPORTS)
        started_at = 20_000
        session_dir = self.start_run("current", started_at)
        self.start_phase_and_pass(session_dir, started_at)

        ticks = [
            self.calibrate_and_report(
                session_dir, raw_percent, "implementation", at=started_at + 3_600
            )
            for raw_percent in (78, 82, 84, 86)
        ]

        self.assertEqual(
            ticks,
            [
                (30, "today 08:53"),
                (30, "today 08:53"),
                (35, "today 08:24"),
                (35, "today 08:24"),
            ],
        )

    def test_calibration_draws_on_the_stage_the_phase_has_reached(self) -> None:
        """An 84 entered during closure is nearly done; one during implementation
        is not. The named stage's history decides the suggestion, and a report at
        another stage refuses a calibration drawn for this one."""
        self.seed_calibration_history("swing", "implementation", SWING_REPORTS)
        self.seed_calibration_history("closing", "closure", ((84, 80),))
        started_at = 20_000
        session_dir = self.start_run("current", started_at)
        self.start_phase_and_pass(session_dir, started_at)

        self.assertEqual(
            self.calibrate_and_report(session_dir, 84, "closure", at=started_at + 3_600),
            (80, "today 06:48"),
        )
        self.assertEqual(
            self.calibrate_and_report(
                session_dir, 84, "implementation", at=started_at + 3_660
            ),
            (35, "today 08:27"),
        )

        _ = self.run_command(
            "calibrate",
            "--session-dir",
            str(session_dir),
            "--candidate-percent",
            "84",
            "--cap-stage",
            "implementation",
            at=started_at + 3_720,
        )
        failure = self.run_failing_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "50",
            "--project-percent",
            "50",
            "--phase-raw-percent",
            "84",
            "--phase-percent",
            "35",
            "--cap-stage",
            "closure",
            "--activity",
            "running the last gates",
            at=started_at + 3_720,
        )
        self.assertNotEqual(failure.returncode, 0)
        self.assertIn("drawn for stage implementation, not closure", failure.stderr)


    def test_eta_is_omitted_at_both_endpoints(self) -> None:
        """0% offers no rate to extend, and 100% leaves nothing to extend it over."""
        started_at = 40_000
        session_dir = self.start_run("endpoints", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        unstarted = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "0",
            "--project-percent",
            "0",
            "--phase-raw-percent",
            "0",
            "--phase-percent",
            "0",
            "--cap-stage",
            "implementation",
            "--activity",
            "reading the work order",
            at=started_at + 100,
        )
        self.assertIn("| Project |   0 | 00:01:40 ", unstarted)
        self.assertIn("| Phase 3 |   0 | 00:01:40 ", unstarted)
        self.assertNotIn("today", unstarted)

        finished = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "100",
            "--project-percent",
            "100",
            "--phase-raw-percent",
            "100",
            "--phase-percent",
            "100",
            "--cap-stage",
            "complete",
            "--activity",
            "closing the phase",
            at=started_at + 200,
        )
        self.assertIn("| Project | 100 | 00:03:20 ", finished)
        self.assertIn("| Phase 3 | 100 | 00:03:20 ", finished)
        self.assertNotIn("today", finished)


    def write_full_config(self) -> None:
        """findings.py refuses every convergence limit it cannot read."""
        _ = self.config_file.write_text(
            "\n".join(
                (
                    "UNIT_PROGRESS_INTERVAL_SECONDS=180",
                    "MIN_REPAIR_BUDGET=3",
                    "REPAIR_ROUNDS_PER_FINDING=0.5",
                    "RUNAWAY_ROUNDS=5",
                    "MAX_FIX_ATTEMPTS=2",
                    "MAX_REOPENS=2",
                    "STALLED_ROUNDS=2",
                    "MAX_CONSECUTIVE_SAME_KIND_PASSES=3",
                    "MAX_REVIEW_CANCELLATIONS=1",
                    "ROUTING_FROM_ROUND=4",
                    "ASTRA_AFTER_FAILED_FIXES=2",
                    "",
                )
            ),
            encoding="utf-8",
        )

    def run_findings(self, session_dir: Path, *arguments: str, at: int) -> str:
        environment = os.environ.copy()
        environment["UNIT_HISTORY_DIR"] = str(self.history_dir)
        environment["UNIT_CONFIG"] = str(self.config_file)
        environment["TZ"] = "UTC"
        environment["USER_ZONE"] = "UTC"
        environment["UNIT_NOW_EPOCH"] = str(at)
        if arguments and arguments[0] == "dispatch":
            arguments = (*arguments, "--accepted-checks", "none: fixture has no accepted caller")
        result = subprocess.run(
            ["python3", str(FINDINGS), *arguments, "--session-dir", str(session_dir)],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        return result.stdout.strip()

    def run_pass(
        self,
        session_dir: Path,
        kind: str,
        fix_pass: int,
        activity: str,
        started_at: int,
        finished_at: int,
    ) -> None:
        _ = self.run_command(
            "start-pass",
            "--session-dir",
            str(session_dir),
            "--pass-kind",
            kind,
            "--fix-pass",
            str(fix_pass),
            "--activity",
            activity,
            "--called-task",
            "direct.implementation",
            "--called-family",
            "codex",
            "--called-model",
            "gpt-called",
            "--called-effort",
            "high",
            at=started_at,
        )
        _ = self.run_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=finished_at,
        )

    def read_events(self, name: str) -> list[dict[str, object]]:
        path = self.history_dir / "runs" / f"{name}.jsonl"
        events: list[dict[str, object]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            parsed: object = json.loads(line)  # pyright: ignore[reportAny]
            events.append(cast(dict[str, object], parsed))
        return events

    def read_state(self, session_dir: Path) -> dict[str, object]:
        state_text = (session_dir / "progress_history_state.json").read_text(
            encoding="utf-8"
        )
        parsed: object = json.loads(state_text)  # pyright: ignore[reportAny]
        return cast(dict[str, object], parsed)

    def table_rows(self, rendered: str, header: list[str]) -> list[list[str]]:
        rows = [
            [cell.strip() for cell in line.removeprefix("| ").removesuffix(" |").split(" | ")]
            for line in rendered.splitlines()
            if line.startswith("| ") and set(line) - set("|-: ")
        ]
        return rows[rows.index(header) + 1 :]

    def test_a_running_round_reports_the_role_each_worker_holds_now(self) -> None:
        """One column per worker, showing the role it holds right now."""
        self.write_full_config()
        started_at = 60_000
        session_dir = self.start_run("team", started_at)
        _ = self.run_command(
            "start-phase",
            "--session-dir",
            str(session_dir),
            "--phase-id",
            "4",
            "--phase-title",
            "Adapter wiring",
            at=started_at,
        )
        # Drive the board through board.sh rather than writing board.log by
        # hand: what is under test is that the writer and the reader agree on a
        # format, which a hand-written fixture would assert nothing about.
        for slot in ("impl", "test"):
            self.run_board(session_dir, "post", slot, "register", "up", at=started_at + 1)
            self.run_board(session_dir, "role", slot, slot, "opening", at=started_at + 2)
        # The test worker gets recruited into implementation: the slot keeps its
        # identity and its column, and only the role in the cell changes.
        self.run_board(session_dir, "role", "test", "impl", "recruited", at=started_at + 3)
        self.start_slot_pass(session_dir, "impl", "impl", started_at + 10)
        self.team_slot = ""
        header = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "40",
            "--project-percent",
            "40",
            "--phase-raw-percent",
            "40",
            "--phase-percent",
            "40",
            "--cap-stage",
            "implementation",
            "--activity",
            "wiring the adapter",
            at=started_at + 300,
        )
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # The recruited test worker reads `impl` under Agent 2: the worker keeps its
        # identity and its column, and only the role in the cell moves. It has
        # registered without opening a pass yet, so it reports the role the
        # board knows and no duration, which is the part that is genuinely
        # unknown -- not a dash, which would read as idle.
        self.assertEqual(
            [rows[0][0], *rows[0][3:5]], ["Impl", "impl 4m running", "impl"]
        )

    def test_a_closed_round_keeps_the_roles_of_its_passless_workers(self) -> None:
        """A finished round still names what its test worker was doing."""
        started_at = 62_000
        session_dir = self.start_run("closed", started_at)
        self.start_phase(session_dir, started_at)
        for slot in ("impl", "test"):
            self.run_board(session_dir, "post", slot, "register", "up", at=started_at + 1)
            self.run_board(session_dir, "role", slot, slot, "opening", at=started_at + 2)
        # One worker records a pass here; the other is board-only for this round,
        # which is what the closed round has to keep reporting.
        self.start_slot_pass(session_dir, "impl", "impl", started_at + 10)
        self.finish_slot_pass(session_dir, "impl", "completed", started_at + 310)
        # A repair round opens behind it, so the closed round is read as history
        # -- which is the case the reader was seeing collapse to a solo pass.
        self.start_slot_pass(session_dir, "impl", "fix", started_at + 320, fix_pass=1)
        self.team_slot = ""
        rows = self.table_rows(
            self.run_progress(session_dir, started_at + 400),
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # The board knows this role after the round closes exactly as it did
        # while the round ran. Blanking it once the round ends would render
        # every finished round as a solo pass and lose the shape the team
        # worked in.
        self.assertEqual(
            [rows[0][0], *rows[0][3:5]], ["Impl", "impl 5m done", "test"]
        )

    def test_each_worker_reports_its_last_board_line_and_its_age(self) -> None:
        """The note under the table says what each worker said, and how long ago."""
        started_at = 63_000
        session_dir = self.start_run("narration", started_at)
        self.start_phase(session_dir, started_at)
        # `test` is board-only: it registers and narrates but opens no pass.
        self.run_board(session_dir, "post", "test", "register", "up", at=started_at + 1)
        # `impl` opens a pass before it has posted anything, which is the case
        # the note has to name rather than leave looking like a worker at work.
        self.start_slot_pass(session_dir, "impl", "impl", started_at + 10)
        self.team_slot = ""
        self.assertIn(
            "- **Agent 1** (impl) gpt-called high · no board line yet",
            self.run_progress(session_dir, started_at + 30),
        )
        self.run_board(
            session_dir, "post", "test", "status", "writing the token race test", at=started_at + 60
        )
        self.run_board(
            session_dir, "post", "impl", "status", "rerunning the scoped test", at=started_at + 120
        )
        header = self.run_progress(session_dir, started_at + 300)
        # The age is the whole point: a worker four minutes into one activity and a
        # worker that has said nothing read differently, where two identical role
        # words in the table do not.
        self.assertIn("- **Agent 1** (impl) gpt-called high · 3m ago · rerunning the scoped test", header)
        self.assertIn("- **Agent 2** (test) · 4m ago · writing the token race test", header)

    def test_a_finished_worker_keeps_its_own_last_words_under_the_launcher_done(self) -> None:
        """The launcher's exit post names the kind; the worker's narration names the work."""
        started_at = 64_000
        session_dir = self.start_run("ownwords", started_at)
        self.start_phase(session_dir, started_at)
        for slot in ("impl", "test"):
            self.run_board(session_dir, "post", slot, "register", "role=fix; up", at=started_at + 1)
            self.start_slot_pass(session_dir, slot, "fix", started_at + 10, fix_pass=1)
        self.run_board(
            session_dir, "post", "test", "status", "hana_catalyst tests 240 passed", at=started_at + 200
        )
        # What implement.sh posts on exit: after the worker's last line, and marked
        # as the launcher's. Without the mark every finished worker read `fix
        # finished` and the narration the reader wanted sat one line up, unseen.
        self.run_board(
            session_dir,
            "post",
            "test",
            "done",
            "launcher: fix finished; summary at impl_summary_test.txt",
            at=started_at + 260,
        )
        # `impl` never narrated, so the launcher's words are all there are.
        self.run_board(
            session_dir,
            "post",
            "impl",
            "done",
            "launcher: fix finished; summary at impl_summary_impl.txt",
            at=started_at + 270,
        )
        self.team_slot = ""
        header = self.run_progress(session_dir, started_at + 300)
        self.assertIn(
            "- **Agent 2** (test) gpt-called high · 40s ago · done: hana_catalyst tests 240 passed", header
        )
        self.assertIn(
            "- **Agent 1** (impl) gpt-called high · 30s ago · done: fix finished; summary at impl_summary_impl.txt",
            header,
        )

    def test_a_worker_finished_over_ten_minutes_ago_drops_out_of_the_report(self) -> None:
        """Only the report's worker lines drop it; a worker still at work stays."""
        started_at = 65_000
        session_dir = self.start_run("finished", started_at)
        self.start_phase(session_dir, started_at)
        for slot in ("impl", "test"):
            self.run_board(session_dir, "post", slot, "register", "role=fix; up", at=started_at + 1)
            self.start_slot_pass(session_dir, slot, "fix", started_at + 10, fix_pass=1)
        for slot in ("impl", "test"):
            self.finish_slot_pass(session_dir, slot, "completed", started_at + 310)
        # A second round opens for test alone, so impl's round is over.
        self.start_slot_pass(session_dir, "test", "fix", started_at + 320, fix_pass=2)
        self.team_slot = ""

        def report(at: int) -> str:
            return self.run_command(
                "progress", "--session-dir", str(session_dir),
                "--project-raw-percent", "40", "--project-percent", "40",
                "--phase-raw-percent", "30", "--phase-percent", "30",
                "--cap-stage", "implementation", "--activity", "implementing", at=at,
            )

        recent = report(started_at + 400)
        self.assertIn("- **Agent 1** (impl) fix 5m done", recent)
        later = report(started_at + 310 + 601)
        self.assertNotIn("**Agent 1**", later)
        self.assertIn("- **Agent 2** (test) fix", later)

    def broad_review(self, session_dir: Path, lenses: tuple[str, ...], started_at: int) -> None:
        """Both workers write, then the broad review workers one pass per lens.

        Each lens takes the worker review.sh gives it and posts the register line
        the launcher posts, a second apart in the order given.
        """
        workers = {"adversary": "test", "contract": "impl", "craft": "review"}
        self.start_phase(session_dir, started_at)
        for offset, slot in enumerate(("impl", "test")):
            self.start_slot_pass(session_dir, slot, slot, started_at + 10 + offset * 10)
        for offset, slot in enumerate(("impl", "test")):
            self.finish_slot_pass(session_dir, slot, "completed", started_at + 600 + offset * 10)
        for offset, lens in enumerate(lenses):
            at = started_at + 700 + offset
            self.run_board(
                session_dir, "post", workers[lens], "register",
                f"{lens} review up; mesh=none; role=review", at=at,
            )
            self.start_slot_pass(session_dir, workers[lens], "review", at, called_model=f"gpt-{lens}")
        self.team_slot = ""

    def test_a_craft_review_draws_a_third_worker_column(self) -> None:
        """The craft lens sits in `review`, and the table gains Agent 3 for it."""
        started_at = 66_000
        session_dir = self.start_run("craft-column", started_at)
        # The craft lens lands a second ahead of the other two, which must not
        # put a sliver of review in the writers' row.
        self.broad_review(session_dir, ("craft", "adversary", "contract"), started_at)
        header = self.run_progress(session_dir, started_at + 1_000)
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Agent 3", "Result"],
        )
        self.assertEqual(
            [row[3:] for row in rows],
            [
                ["impl 9m done", "test 9m done", "-", ""],
                [
                    "review 4m running",
                    "review 4m running",
                    "review 5m running",
                    "Agent 1 → review, Agent 2 → review, Agent 3 → review; running",
                ],
            ],
        )
        self.assertIn(
            "- **Agent 3** (review) gpt-craft high · 5m ago · register: craft review up",
            header,
        )

    def test_a_review_without_the_craft_lens_keeps_two_worker_columns(self) -> None:
        """No pass in `review`, no Agent 3: not in the header, not in the note.

        The review worker's board line from an earlier phase outlives that phase,
        and must not put a note line under a table with no column for it.
        """
        started_at = 67_000
        session_dir = self.start_run("two-columns", started_at)
        self.run_board(
            session_dir, "post", "review", "status", "an earlier phase's craft notes",
            at=started_at - 100,
        )
        self.broad_review(session_dir, ("adversary", "contract"), started_at)
        header = self.run_progress(session_dir, started_at + 1_000)
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        self.assertEqual(
            [row[3:] for row in rows],
            [
                ["impl 9m done", "test 9m done", ""],
                [
                    "review 4m running",
                    "review 5m running",
                    "Agent 1 → review, Agent 2 → review; running",
                ],
            ],
        )
        self.assertNotIn("Agent 3", header)

    def test_a_review2_pass_gets_its_own_progress_column(self) -> None:
        started_at = 67_500
        session_dir = self.start_run("review2-column", started_at)
        self.start_phase(session_dir, started_at)
        self.start_slot_pass(session_dir, "impl", "impl", started_at + 10)
        self.finish_slot_pass(session_dir, "impl", "completed", started_at + 600)
        self.start_slot_pass(
            session_dir, "review2", "review", started_at + 700,
            called_model="gpt-review2",
        )
        self.team_slot = ""

        report = self.run_progress(session_dir, started_at + 1_000)

        rows = self.table_rows(
            report,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Agent 3", "Result"],
        )
        self.assertTrue(any("review" in row[5] for row in rows))
        self.assertIn(
            "- **Agent 3** (review2) gpt-review2 high",
            report,
        )

    def test_a_fix2_handoff_gets_its_own_progress_column(self) -> None:
        started_at = 67_800
        session_dir = self.start_run("fix2-column", started_at)
        self.start_phase(session_dir, started_at)
        self.start_slot_pass(session_dir, "impl", "fix", started_at + 10, fix_pass=1)
        self.run_board(
            session_dir, "role", "fix2", "fix", "joining repair round",
            at=started_at + 100,
        )
        self.team_slot = ""

        report = self.run_progress(session_dir, started_at + 300)

        rows = self.table_rows(
            report,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Agent 3", "Result"],
        )
        self.assertTrue(any(row[5] == "fix" for row in rows))
        self.assertIn("- **Agent 3** (fix2)", report)

    def test_review_trial_reports_the_phase_lens_counts_and_review_minutes(self) -> None:
        """The checkpoint notice line: one phase's ux and craft findings and review time."""
        self.write_full_config()
        started_at = 68_000
        session_dir = self.start_run("trial", started_at)
        refused = self.run_failing_command(
            "review-trial", "--session-dir", str(session_dir), at=started_at + 5
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("No phase recorded", refused.stderr)

        # An earlier phase, whose review time and craft finding are not this one's.
        self.start_phase(session_dir, started_at + 10)
        self.start_slot_pass(session_dir, "review", "review", started_at + 20)
        self.finish_slot_pass(session_dir, "review", "completed", started_at + 620)
        _ = self.run_findings(
            session_dir, "open", "--severity", "minor", "--title", "earlier phase",
            "--caught-by", "worker", "--lens", "craft", at=started_at + 630,
        )
        self.team_slot = ""
        _ = self.run_command(
            "finish-phase", "--session-dir", str(session_dir), "--status", "completed",
            at=started_at + 700,
        )

        base = started_at + 1_000
        self.start_phase(session_dir, base)
        # A writer's pass is not review time.
        self.start_slot_pass(session_dir, "impl", "impl", base + 10)
        self.finish_slot_pass(session_dir, "impl", "completed", base + 3_010)
        # Every worker's review counts, whatever its status: 600 + 900 + 1200 s.
        reviews = (("impl", 600, "completed"), ("test", 900, "completed"), ("review", 1_200, "error"))
        for slot, _, _ in reviews:
            self.start_slot_pass(session_dir, slot, "review", base + 3_100)
        for slot, elapsed, status in reviews:
            self.finish_slot_pass(session_dir, slot, status, base + 3_100 + elapsed)
        self.team_slot = ""
        # The UX helper's 350 s counts; the unit director's other work does not.
        for label, opened, closed in (("UX review", 4_400, 4_750), ("Verify", 4_800, 5_300)):
            _ = self.run_command(
                "start-activity", "--session-dir", str(session_dir),
                "--label", label, "--activity", label.lower(), at=base + opened,
            )
            _ = self.run_command(
                "finish-activity", "--session-dir", str(session_dir), at=base + closed,
            )
        for lens in ("ux", "craft", "adversary,craft", "both", "contract"):
            _ = self.run_findings(
                session_dir, "open", "--severity", "minor", "--title", f"raised by {lens}",
                "--caught-by", "worker", "--lens", lens, at=base + 5_400,
            )

        # 3050 s is 50.8 minutes, which rounds to 51 rather than truncating to 50.
        expected = (
            "review trial: ux 1 findings, code 4 findings, review-worker minutes 51, "
            + "ux check minutes 6, ux repair minutes 0"
        )
        self.assertEqual(
            self.run_command("review-trial", "--session-dir", str(session_dir), at=base + 5_500),
            expected,
        )
        _ = self.run_command(
            "finish-phase", "--session-dir", str(session_dir), "--status", "completed",
            at=base + 5_600,
        )
        self.assertEqual(
            self.run_command("review-trial", "--session-dir", str(session_dir), at=base + 5_700),
            expected,
        )

    def test_review_trial_counts_each_code_finding_once(self) -> None:
        self.write_full_config()
        started_at = 71_000
        session_dir = self.start_run("code-findings", started_at)
        self.start_phase(session_dir, started_at + 10)

        def assert_trial_counts(ux_findings: int, code_findings: int, at: int) -> None:
            self.assertEqual(
                self.run_command(
                    "review-trial", "--session-dir", str(session_dir), at=at
                ),
                f"review trial: ux {ux_findings} findings, code {code_findings} findings, "
                + "review-worker minutes 0, ux check minutes 0, ux repair minutes 0",
            )

        for index, lens in enumerate(("adversary,contract", "contract", "contract")):
            _ = self.run_findings(
                session_dir,
                "open",
                "--severity",
                "minor",
                "--title",
                f"code finding {index}",
                "--caught-by",
                "delegate",
                "--lens",
                lens,
                at=started_at + 20 + index,
            )
        _ = self.run_findings(
            session_dir,
            "open",
            "--severity",
            "minor",
            "--title",
            "unit director finding",
            "--caught-by",
            "delegate",
            at=started_at + 30,
        )
        assert_trial_counts(0, 4, started_at + 40)

        for offset, lens, ux_findings, code_findings in (
            (50, "ux", 1, 4),
            (51, "ux,craft", 2, 5),
            (52, "both", 2, 6),
        ):
            _ = self.run_findings(
                session_dir,
                "open",
                "--severity",
                "minor",
                "--title",
                f"finding from {lens}",
                "--caught-by",
                "delegate",
                "--lens",
                lens,
                at=started_at + offset,
            )
            assert_trial_counts(ux_findings, code_findings, started_at + offset + 1)

    def test_review_trial_counts_the_findings_the_reviewers_raised(self) -> None:
        """A pool reviewer's findings count though the director opened none of them."""
        self.write_full_config()
        started_at = 71_500
        session_dir = self.start_run("raised-findings", started_at)
        self.start_phase(session_dir, started_at + 10)
        state = cast(dict[str, object], json.loads(
            (session_dir / "progress_history_state.json").read_text(encoding="utf-8")))
        instance_id = cast(str, cast(dict[str, object], state["phase"])["instance_id"])
        _ = self.run_findings(
            session_dir, "open", "--severity", "minor", "--title", "opened by the director",
            "--caught-by", "worker", "--lens", "contract", at=started_at + 20,
        )

        def trial() -> str:
            return self.run_command("review-trial", "--session-dir", str(session_dir), at=started_at + 40)

        # No turn record: the director's opened finding is the count.
        self.assertIn("code 1 findings", trial())
        records = self.history_dir / "review_turns" / session_dir.name
        records.mkdir(parents=True)
        for name, instance, kind, status, raised in (
            ("adversary", instance_id, "broad", "reviewed", 3),
            ("contract", instance_id, "broad", "reviewed", 2),
            ("closure", instance_id, "closure", "reviewed", 1),
            ("failed", instance_id, "broad", "error", 7),
            ("ux", instance_id, "ux", "reviewed", 4),
            ("other-phase", "another-instance", "broad", "reviewed", 9),
        ):
            _ = (records / f"{name}.json").write_text(json.dumps({
                "phase_instance_id": instance, "kind": kind, "status": status, "raised": raised,
            }), encoding="utf-8")
        _ = (records / "torn.json").write_text("{", encoding="utf-8")
        self.assertEqual(
            trial(),
            "review trial: ux 0 findings, code 6 findings, review-worker minutes 0, "
            + "ux check minutes 0, ux repair minutes 0",
        )

    def test_a_recorded_zero_review_does_not_fall_back_to_director_findings(self) -> None:
        self.write_full_config()
        started_at = 71_600
        session = self.start_run("zero-review-evidence", started_at)
        self.start_phase(session, started_at + 10)
        state = cast(dict[str, object], json.loads((session / "progress_history_state.json").read_text()))
        instance = str(cast(dict[str, object], state["phase"])["instance_id"])
        _ = self.run_findings(session, "open", "--severity", "minor", "--title", "director finding",
                              "--caught-by", "worker", "--lens", "contract", at=started_at + 20)
        with patch.object(plan_history, "history_root", return_value=self.history_dir):
            self.assertIsInstance(progress_history._raised_code_findings(session, instance),  # pyright: ignore[reportPrivateUsage]
                                  progress_history.NoRecordedCodeReview)
        self.assertIn("code 1 findings", self.run_command("review-trial", "--session-dir", str(session), at=started_at + 30))
        records = self.history_dir / "review_turns" / session.name
        records.mkdir(parents=True)
        _ = (records / "71620-123-0.json").write_text(json.dumps({
            "phase_instance_id": instance, "arm": "own", "kind": "broad", "status": "reviewed", "raised": 0,
        }))
        with patch.object(plan_history, "history_root", return_value=self.history_dir):
            self.assertEqual(progress_history._raised_code_findings(session, instance),  # pyright: ignore[reportPrivateUsage]
                             progress_history.RecordedCodeFindings(0))
        self.assertIn("code 0 findings", self.run_command("review-trial", "--session-dir", str(session), at=started_at + 40))

    def test_legacy_and_own_zero_reviews_remain_distinct_from_absent_evidence(self) -> None:
        self.write_full_config()
        session = self.start_run("retired-review-evidence", 71_650)
        self.start_phase(session, 71_660)
        state = cast(dict[str, object], json.loads((session / "progress_history_state.json").read_text()))
        instance = str(cast(dict[str, object], state["phase"])["instance_id"])
        records = self.history_dir / "review_turns" / session.name
        records.mkdir(parents=True)
        with patch.object(plan_history, "history_root", return_value=self.history_dir):
            for arm in ("pool", "own"):
                with self.subTest(arm=arm):
                    self.assertIsInstance(progress_history._raised_code_findings(session, instance),  # pyright: ignore[reportPrivateUsage]
                                          progress_history.NoRecordedCodeReview)
                    record = records / "zero.json"
                    _ = record.write_text(json.dumps({"phase_instance_id": instance, "arm": arm,
                        "kind": "broad", "status": "reviewed", "raised": 0}))
                    self.assertEqual(progress_history._raised_code_findings(session, instance),  # pyright: ignore[reportPrivateUsage]
                                     progress_history.RecordedCodeFindings(0))
                    record.unlink()

    def test_pool_and_own_review_records_with_one_launcher_sequence_all_count(self) -> None:
        self.write_full_config()
        started_at = 71_700
        session = self.start_run("mixed-review-evidence", started_at)
        self.start_phase(session, started_at + 10)
        state = cast(dict[str, object], json.loads((session / "progress_history_state.json").read_text()))
        instance = cast(dict[str, object], state["phase"])["instance_id"]
        records = self.history_dir / "review_turns" / session.name
        records.mkdir(parents=True)
        for sequence, arm, raised in ((0, "pool", 2), (1, "own", 3), (2, "own", 0)):
            _ = (records / f"71720-123-{sequence}.json").write_text(json.dumps({
                "phase_instance_id": instance, "arm": arm, "kind": "broad", "status": "reviewed", "raised": raised,
            }))
        self.assertIn("code 5 findings", self.run_command("review-trial", "--session-dir", str(session), at=started_at + 40))

    def test_review_trial_reports_the_screenshot_check_and_its_repair_minutes(self) -> None:
        """The check's own minutes, and only the fix rounds that covered a ux finding."""
        self.write_full_config()
        base = 72_000
        session_dir = self.start_run("uxcost", base)
        self.start_phase(session_dir, base + 10)
        _ = self.run_command(
            "start-activity", "--session-dir", str(session_dir),
            "--label", "UX review", "--activity", "judging the shots", at=base + 100,
        )
        _ = self.run_command("finish-activity", "--session-dir", str(session_dir), at=base + 250)

        def repair(finding: str, lens: str, round_number: int, workers: tuple[tuple[str, int], ...], at: int) -> None:
            _ = self.run_findings(
                session_dir, "open", "--severity", "blocker", "--title", f"{lens} finding",
                "--file", "src/lib.rs", "--caught-by", "worker", "--lens", lens, at=at,
            )
            _ = self.run_findings(session_dir, "gate", at=at + 5)
            _ = self.run_findings(session_dir, "dispatch", "--covers", finding, at=at + 10)
            for slot, _ in workers:
                self.start_slot_pass(session_dir, slot, "fix", at + 20, fix_pass=round_number)
            for slot, elapsed in workers:
                self.finish_slot_pass(session_dir, slot, "completed", at + 20 + elapsed)
            self.team_slot = ""
            _ = self.run_findings(session_dir, "landed", at=at + 2_000)
            _ = self.run_findings(
                session_dir, "verdict", "--id", finding, "--state", "accepted",
                "--evidence", "repaired", at=at + 2_010,
            )

        # Round 1 repairs a code finding: none of its 600 s is the check's.
        repair("F001", "adversary", 1, (("impl", 600),), base + 300)
        # Round 2 repairs a ux finding on two workers: 900 + 300 s is 20 minutes.
        repair("F002", "ux", 2, (("impl", 900), ("test", 300)), base + 3_000)

        # 150 s rounds to 3 minutes, not 2.
        self.assertEqual(
            self.run_command("review-trial", "--session-dir", str(session_dir), at=base + 6_000),
            "review trial: ux 1 findings, code 1 findings, review-worker minutes 3, "
            + "ux check minutes 3, ux repair minutes 20",
        )

    def test_the_board_refuses_a_handoff_that_names_no_role(self) -> None:
        """A handoff is a role change; prose there is a movement the table never shows."""
        session_dir = self.start_run("barehandoff", 65_000)
        environment = os.environ.copy()
        environment["UNIT_NOW_EPOCH"] = "65001"
        refused = subprocess.run(
            ["bash", str(BOARD), "post", str(session_dir), "test", "handoff", "round 8 done"],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("board.sh role", refused.stderr)
        log = session_dir / "board.log"
        self.assertNotIn("handoff", log.read_text(encoding="utf-8") if log.exists() else "")
        # The verb writes the field the table reads, and is the way through.
        self.run_board(session_dir, "role", "test", "review", "converging", at=65_002)
        self.assertIn("[test] handoff: role=review converging", log.read_text(encoding="utf-8"))

    def test_a_round_falls_back_when_no_board_exists(self) -> None:
        """A phase whose team has not registered still renders a row."""
        self.write_full_config()
        started_at = 60_000
        session_dir = self.start_run("noboard", started_at)
        _ = self.run_command(
            "start-phase",
            "--session-dir",
            str(session_dir),
            "--phase-id",
            "2",
            "--phase-title",
            "Nothing posted",
            at=started_at,
        )
        _ = self.run_command(
            "start-pass",
            "--session-dir",
            str(session_dir),
            "--pass-kind",
            "impl",
            "--fix-pass",
            "0",
            "--activity",
            "working",
            "--called-task",
            "direct.implementation",
            "--called-family",
            "codex",
            "--called-model",
            "gpt-called",
            "--called-effort",
            "high",
            at=started_at + 10,
        )
        header = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "10",
            "--project-percent",
            "10",
            "--phase-raw-percent",
            "10",
            "--phase-percent",
            "10",
            "--cap-stage",
            "implementation",
            "--activity",
            "working",
            at=started_at + 200,
        )
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # No board and no worker on the pass: the window sits in no round, so it
        # renders on its own the way every solo run always has, drawn in the
        # worker its kind names.
        self.assertEqual(
            [rows[0][0], *rows[0][3:5]], ["Impl", "impl 3m running", "-"]
        )

    def test_a_run_of_verifications_counts_as_one_stage(self) -> None:
        """Consecutive windows sharing a label spend one of the table's slots."""
        self.write_full_config()
        started_at = 61_000
        session_dir = self.start_run("verifications", started_at)
        _ = self.run_command(
            "start-phase",
            "--session-dir",
            str(session_dir),
            "--phase-id",
            "9",
            "--phase-title",
            "Retry budget",
            at=started_at,
        )
        self.run_pass(
            session_dir,
            "impl",
            0,
            "writing the retry budget",
            started_at + 10,
            started_at + 200,
        )
        # A repair round triggers verification more than once -- a failure, a
        # rerun, the scoped test that follows it -- and they land within a
        # minute of each other.
        for index in range(3):
            _ = self.run_command(
                "start-activity",
                "--session-dir",
                str(session_dir),
                "--label",
                "Verification",
                "--activity",
                "test hana_clerestory",
                at=started_at + 220 + index * 40,
            )
            _ = self.run_command(
                "finish-activity",
                "--session-dir",
                str(session_dir),
                "--status",
                "completed",
                "--result",
                "pass",
                at=started_at + 240 + index * 40,
            )
        _ = self.run_command(
            "start-pass",
            "--session-dir",
            str(session_dir),
            "--pass-kind",
            "fix",
            "--fix-pass",
            "1",
            "--activity",
            "repairing the budget",
            "--called-task",
            "direct.implementation",
            "--called-family",
            "codex",
            "--called-model",
            "gpt-called",
            "--called-effort",
            "high",
            at=started_at + 400,
        )
        header = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "40",
            "--project-percent",
            "40",
            "--phase-raw-percent",
            "60",
            "--phase-percent",
            "60",
            "--cap-stage",
            "implementation",
            "--activity",
            "repairing the budget",
            at=started_at + 440,
        )
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # Three stages, three rows: the verification run is one stage of the
        # cap AND one row of the table, its gate-by-gate story in the notes
        # beneath. Counting its windows singly once dropped the implementation
        # the phase opened with; rendering them singly filled the table with
        # rows a reader could tell apart only by result.
        self.assertEqual(
            [row[0] for row in rows],
            ["Impl", "Verification", "Fix 1"],
        )
        self.assertNotIn("Earlier:", header)

    def test_the_stage_table_names_every_window_the_phase_opened(self) -> None:
        """Each pass and activity in start order, with what the ledger recorded."""
        self.write_full_config()
        started_at = 60_000
        session_dir = self.start_run("stages", started_at)
        _ = self.run_command(
            "start-phase",
            "--session-dir",
            str(session_dir),
            "--phase-id",
            "7",
            "--phase-title",
            "Drift records",
            at=started_at,
        )
        self.run_pass(
            session_dir,
            "impl",
            0,
            "writing the drift records",
            started_at + 10,
            started_at + 200,
        )
        self.run_pass(
            session_dir,
            "review",
            0,
            "independent review of the finished code",
            started_at + 220,
            started_at + 300,
        )
        for index in (1, 2):
            _ = self.run_findings(
                session_dir,
                "open",
                "--severity",
                "blocker",
                "--title",
                f"finding {index}",
                "--file",
                "src/lib.rs",
                "--line",
                str(index),
                "--caught-by",
                "both",
                at=started_at + 320,
            )
        _ = self.run_findings(session_dir, "gate", at=started_at + 325)
        _ = self.run_findings(session_dir, "dispatch", "--covers", "F001,F002", at=started_at + 330)
        self.run_pass(
            session_dir,
            "fix",
            1,
            "repairing both findings",
            started_at + 340,
            started_at + 500,
        )
        _ = self.run_findings(session_dir, "landed", at=started_at + 500)
        self.run_pass(
            session_dir,
            "review",
            0,
            "closure review of the repair",
            started_at + 520,
            started_at + 560,
        )
        for finding in ("F001", "F002"):
            _ = self.run_findings(
                session_dir,
                "verdict",
                "--id",
                finding,
                "--state",
                "accepted",
                "--evidence",
                "repaired",
                at=started_at + 580,
            )
        _ = self.run_command(
            "start-activity",
            "--session-dir",
            str(session_dir),
            "--label",
            "Verification",
            "--activity",
            "test hana_clerestory",
            at=started_at + 600,
        )
        _ = self.run_command(
            "finish-activity",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            "--result",
            "pass",
            at=started_at + 640,
        )
        _ = self.run_command(
            "start-pass",
            "--session-dir",
            str(session_dir),
            "--pass-kind",
            "review",
            "--activity",
            "checking the remaining plan against what shipped",
            "--called-task",
            "direct.review",
            "--called-family",
            "codex",
            "--called-model",
            "gpt-called",
            "--called-effort",
            "max",
            at=started_at + 660,
        )
        header = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "50",
            "--project-percent",
            "50",
            "--phase-raw-percent",
            "95",
            "--phase-percent",
            "95",
            "--cap-stage",
            "closure",
            "--activity",
            "checking the remaining plan against what shipped",
            at=started_at + 700,
        )
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # A solo run records no worker, so every window keeps a row and the
        # per-window finding attribution it always had -- for the last three
        # stages, which is all the table draws. The three before them are named
        # by the line above it rather than dropped without a word.
        self.assertEqual(
            [(row[0], row[2], row[5]) for row in rows],
            [
                ("Review Fix 1", "40s", "2 fixed"),
                ("Verification", "40s", "pass"),
                ("Review 3", "40s", "running"),
            ],
        )
        self.assertIn("*Earlier: 3 stages not shown - Impl through Fix 1.*", header)
        # A slotless pass is drawn in the worker its kind names -- the closure
        # review under Agent 2, the worker that reviews, where a reader looks for
        # it -- and a unit-director activity names no worker and keeps both dashes.
        self.assertEqual(
            [tuple(row[3:5]) for row in rows],
            [
                ("-", "review 40s done"),
                ("-", "-"),
                ("-", "review 40s running"),
            ],
        )
        self.assertIn("▸ **Review 3 - checking the remaining plan against what shipped**", header)
        # Which agent ran each window is the `timeline` view's question, and the
        # unit director ran verification itself, so that row has no worker there.
        stage_rows = self.table_rows(
            self.run_command(
                "timeline",
                "--session-dir",
                str(session_dir),
                at=started_at + 700,
            ),
            ["Stage", "Main", "Worker", "Start", "Elapsed", "Result"],
        )
        self.assertEqual((stage_rows[4][1], stage_rows[4][2]), ("gpt-main xhigh", ""))
        self.assertEqual(stage_rows[0][1], "gpt-main xhigh")

    def write_agents_registry(self) -> None:
        """A registry under the temporary home, so arm-review resolves the same
        agent review.sh would without reading this machine's real assignments."""
        _ = write_registry(self.agents_config_file, {
            "direct.codex": {"impl": "gpt-called:xhigh", "review": "gpt-blind:max"},
            "codex.agents": {"gpt-called": "low,medium,high,xhigh,max", "gpt-blind": "low,medium,high,xhigh,max"},
        })

    def arm_early_review(self, session_dir: Path, at: int) -> str:
        return self.run_command(
            "arm-review",
            "--session-dir",
            str(session_dir),
            "--activity",
            "reviewing the diff the writer is still producing",
            "--called-task",
            "direct.review",
            at=at,
        )

    def test_an_early_review_naming_its_phase_watches_that_phase_directory(self) -> None:
        """`review.sh --phase` writes its status there, so the marker must watch the same file."""
        self.write_agents_registry()
        started_at = 90_000
        session_dir = self.start_run("early-phase", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.run_command(
            "arm-review", "--session-dir", str(session_dir), "--phase-id", "3", "--lens", "adversary",
            "--activity", "reviewing the diff the writer is still producing", at=started_at + 100,
        )
        state = cast(dict[str, object], json.loads((session_dir / "progress_history_state.json").read_text()))
        armed = cast(dict[str, object], state["early_review"])
        self.assertEqual(armed["status_file"], str(session_dir / "phases" / "3" / "review_status_adversary"))
        self.assertEqual(armed["pid_file"], str(session_dir / "phases" / "3" / "review_pid_adversary"))

    def test_an_early_review_runs_as_its_own_row_beside_the_open_pass(self) -> None:
        """The one moment two agents work at once, shown as two running rows."""
        self.write_agents_registry()
        started_at = 90_000
        session_dir = self.start_run("early", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.arm_early_review(session_dir, started_at + 100)
        header = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "40",
            "--project-percent",
            "40",
            "--phase-raw-percent",
            "75",
            "--phase-percent",
            "75",
            "--cap-stage",
            "implementation",
            "--activity",
            "correcting retry recovery",
            at=started_at + 160,
        )
        rows = self.table_rows(
            header,
            ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
        )
        # Neither window has opened a round -- the writer predates workers and the
        # armed reviewer has no pass yet -- so there is no round for the
        # reviewer to join and it keeps the row beside the writer that it
        # always had.
        self.assertEqual(
            [(row[0], row[2], row[5]) for row in rows],
            [("Fix 2", "2m", "running"), ("Review Fix 2", "1m", "running (early)")],
        )
        # The armed reviewer claims the test worker, where the adversary lens sits
        # and a lone closure reviewer reads, so it is drawn under Agent 2.
        self.assertEqual(
            [tuple(row[3:5]) for row in rows],
            [("fix 2m running", "-"), ("-", "review 1m running")],
        )
        # The report is about the writer, so the sentence beneath the table names
        # it -- not the reviewer, whose row is now the last one.
        self.assertIn("▸ **Fix 2 - correcting retry recovery**", header)

    def test_the_early_reviewers_real_pass_supersedes_its_armed_row(self) -> None:
        self.write_agents_registry()
        started_at = 91_000
        session_dir = self.start_run("adopted", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.arm_early_review(session_dir, started_at + 100)
        _ = self.run_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 200,
        )
        _ = self.run_command(
            "start-pass",
            "--session-dir",
            str(session_dir),
            "--pass-kind",
            "review",
            "--activity",
            "reviewing the finished diff",
            "--called-task",
            "direct.review",
            "--called-family",
            "codex",
            "--called-model",
            "gpt-blind",
            "--called-effort",
            "max",
            at=started_at + 210,
        )
        rendered = self.run_command(
            "timeline",
            "--session-dir",
            str(session_dir),
            at=started_at + 240,
        )
        stage_rows = self.table_rows(
            rendered,
            ["Stage", "Main", "Worker", "Start", "Elapsed", "Result"],
        )
        self.assertEqual(
            [(row[0], row[5]) for row in stage_rows],
            [("Fix 2", "done"), ("Review Fix 2", "running")],
        )
        disarmed = [
            event
            for event in self.read_events("adopted")
            if event.get("event_type") == "early_review_disarmed"
        ]
        self.assertEqual([event["reason"] for event in disarmed], ["adopted"])

    def test_a_killed_early_reviewer_leaves_no_row_behind(self) -> None:
        """A marker outliving its launcher would show a phantom agent working."""
        self.write_agents_registry()
        started_at = 92_000
        session_dir = self.start_run("orphan", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.arm_early_review(session_dir, started_at + 100)
        dead = subprocess.Popen(["true"])
        _ = dead.wait()
        pid_file = session_dir / "review_pid"
        _ = pid_file.write_text(f"{dead.pid}\n", encoding="utf-8")
        os.utime(pid_file, (started_at + 110, started_at + 110))
        rendered = self.run_command(
            "timeline",
            "--session-dir",
            str(session_dir),
            at=started_at + 160,
        )
        stage_rows = self.table_rows(
            rendered,
            ["Stage", "Main", "Worker", "Start", "Elapsed", "Result"],
        )
        self.assertEqual([row[0] for row in stage_rows], ["Fix 2"])

    def test_a_reviewer_that_failed_before_delivery_leaves_no_row_behind(self) -> None:
        """review.sh's own status retires the row a killed launcher cannot."""
        self.write_agents_registry()
        started_at = 94_000
        session_dir = self.start_run("failed-early", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        status = session_dir / "review_status"
        # The status a previous pass left behind must not retire the new row.
        _ = status.write_text("reviewed\n", encoding="utf-8")
        os.utime(status, (started_at + 50, started_at + 50))
        _ = self.arm_early_review(session_dir, started_at + 100)
        armed = self.run_command(
            "timeline",
            "--session-dir",
            str(session_dir),
            at=started_at + 120,
        )
        self.assertIn("running (early)", armed)

        _ = status.write_text("error\n", encoding="utf-8")
        os.utime(status, (started_at + 130, started_at + 130))
        retired = self.run_command(
            "timeline",
            "--session-dir",
            str(session_dir),
            at=started_at + 140,
        )
        self.assertNotIn("running (early)", retired)

    def test_arming_an_early_review_without_an_open_pass_is_refused(self) -> None:
        started_at = 93_000
        session_dir = self.start_run("unpaired", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.run_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 60,
        )
        failure = self.run_failing_command(
            "arm-review",
            "--session-dir",
            str(session_dir),
            "--activity",
            "reviewing nothing in particular",
            at=started_at + 70,
        )
        self.assertNotEqual(failure.returncode, 0)
        self.assertIn("a pass must be open", failure.stderr)

    def test_an_activity_records_its_own_identity_not_the_finished_pass(self) -> None:
        """A finished pass stays in state; it used to stamp every later event."""
        started_at = 70_000
        session_dir = self.start_run("identity", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.run_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 60,
        )
        _ = self.run_command(
            "start-activity",
            "--session-dir",
            str(session_dir),
            "--label",
            "Style",
            "--activity",
            "style-only review of the phase diff",
            at=started_at + 70,
        )
        started = [
            event
            for event in self.read_events("identity")
            if event.get("event_type") == "activity_started"
        ]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["activity_label"], "Style")
        self.assertEqual(started[0]["activity_text"], "style-only review of the phase diff")
        self.assertNotIn("pass_kind", started[0])
        self.assertNotIn("called_agent", started[0])

    def test_the_timeline_reports_a_phase_the_run_has_already_finished(self) -> None:
        started_at = 80_000
        session_dir = self.start_run("recorded", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        _ = self.run_command(
            "finish-pass",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 100,
        )
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            at=started_at + 120,
        )
        rendered = self.run_command(
            "timeline",
            "--session-dir",
            str(session_dir),
            at=started_at + 900,
        )
        self.assertIn("**Phase 3: Retry handling - elapsed 00:02:00 - completed**", rendered)
        self.assertIn(
            "| Fix 2 | gpt-main xhigh | gpt-called high | 22:13:30 | 00:01:30 | done",
            rendered,
        )

        missing = self.run_failing_command(
            "timeline",
            "--session-dir",
            str(session_dir),
            "--phase",
            "9",
            at=started_at + 900,
        )
        self.assertEqual(missing.returncode, 1)
        self.assertIn("recorded no phase 9", missing.stderr)

    def write_claude_transcript(self, session_id: str, model: str, effort: str) -> None:
        transcript = self.root / ".claude" / "projects" / "demo" / f"{session_id}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        _ = transcript.write_text(
            json.dumps({"type": "assistant", "message": {"model": model}, "effort": effort}) + "\n",
            encoding="utf-8",
        )

    def write_codex_transcript(self, thread_id: str, model: str, effort: str) -> None:
        transcript = self.root / ".codex" / "sessions" / f"rollout-{thread_id}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        _ = transcript.write_text(
            json.dumps({"type": "turn_context", "payload": {"model": model, "effort": effort}}) + "\n",
            encoding="utf-8",
        )

    def open_pass_and_activity(
        self,
        session_dir: Path,
        started_at: int,
        claude_session: str,
        codex_session: str = "",
    ) -> list[dict[str, object]]:
        """Open a phase, then a pass and an activity as the given session.

        Returns the stored `main_agent` after the pass and after the activity.
        """
        _ = self.run_command(
            "start-phase",
            "--session-dir",
            str(session_dir),
            "--phase-id",
            "3",
            "--phase-title",
            "Retry handling",
            at=started_at,
        )
        _ = self.run_command(
            "start-pass",
            "--session-dir",
            str(session_dir),
            "--pass-kind",
            "impl",
            "--activity",
            "writing the retry path",
            "--called-task",
            "direct.implementation",
            "--called-family",
            "codex",
            "--called-model",
            "gpt-called",
            "--called-effort",
            "high",
            at=started_at + 10,
            claude_session=claude_session,
            codex_session=codex_session,
        )
        after_pass = cast("dict[str, object]", self.read_state(session_dir)["main_agent"])
        _ = self.run_command(
            "start-activity",
            "--session-dir",
            str(session_dir),
            "--label",
            "Style",
            "--activity",
            "style-only review of the phase diff",
            at=started_at + 20,
            claude_session=claude_session,
            codex_session=codex_session,
        )
        after_activity = cast("dict[str, object]", self.read_state(session_dir)["main_agent"])
        return [after_pass, after_activity]

    def test_a_window_picks_up_the_main_agent_changing_mid_run(self) -> None:
        """`start-run` detects the unit director once, and it can change after."""
        started_at = 90_000
        session_dir = self.start_run("switched", started_at)
        self.write_claude_transcript("main-switched", "sonnet-5", "low")
        for identity in self.open_pass_and_activity(session_dir, started_at, "main-switched"):
            self.assertEqual(
                (identity["model"], identity["effort"], identity["session_id"]),
                ("sonnet-5", "low", "main-switched"),
            )
        rendered = self.run_command(
            "timeline",
            "--session-dir",
            str(session_dir),
            at=started_at + 100,
        )
        self.assertIn("| sonnet-5 low |", rendered)
        self.assertNotIn("gpt-main xhigh", rendered)

    def test_a_window_opened_in_a_codex_worker_keeps_the_director(self) -> None:
        """A Codex worker inherits the director's Claude session but has its own thread."""
        started_at = 91_000
        session_dir = self.start_run("codexworker", started_at)
        self.write_claude_transcript("main-codexworker", "sonnet-5", "low")
        self.write_codex_transcript("worker-thread", "gpt-worker", "medium")
        director = cast("dict[str, object]", self.read_state(session_dir)["main_agent"])
        for identity in self.open_pass_and_activity(
            session_dir, started_at, "main-codexworker", codex_session="worker-thread"
        ):
            self.assertEqual(identity, director)

    def test_a_window_opened_by_a_claude_worker_keeps_the_director(self) -> None:
        """A Claude worker is a Claude Code session of its own."""
        started_at = 92_000
        session_dir = self.start_run("claudeworker", started_at)
        self.write_claude_transcript("claude-worker", "sonnet-5", "low")
        director = cast("dict[str, object]", self.read_state(session_dir)["main_agent"])
        for identity in self.open_pass_and_activity(session_dir, started_at, "claude-worker"):
            self.assertEqual(identity, director)

    def test_the_directors_window_repairs_a_main_agent_a_worker_overwrote(self) -> None:
        """Runs started before the check carry a worker's thread; the run folder names the director."""
        started_at = 93_000
        session_dir = self.start_run("repaired", started_at)
        state = self.read_state(session_dir)
        state["main_agent"] = {
            "family": "codex", "model": "gpt-worker", "effort": "medium", "session_id": "worker-thread"}
        _ = (session_dir / "progress_history_state.json").write_text(json.dumps(state), encoding="utf-8")
        _ = (session_dir / "director").write_text("main-repaired\n", encoding="utf-8")
        self.write_claude_transcript("main-repaired", "sonnet-5", "low")
        for identity in self.open_pass_and_activity(session_dir, started_at, "main-repaired"):
            self.assertEqual(
                (identity["model"], identity["effort"], identity["session_id"]),
                ("sonnet-5", "low", "main-repaired"),
            )

    def test_identity_detection_prefers_codex_falls_back_to_claude_and_refuses_unknown(self) -> None:
        codex = self.root / ".codex/sessions/turn-codex-session.jsonl"
        claude = self.root / ".claude/projects/project/claude-session.jsonl"
        codex.parent.mkdir(parents=True)
        claude.parent.mkdir(parents=True)
        _ = codex.write_text(json.dumps({"type": "turn_context", "payload": {"model": "codex-model"}}) + "\n")
        _ = claude.write_text(json.dumps({"type": "assistant", "message": {"model": "claude-model"}}) + "\n")
        plan = self.working_dir / "identity-plan.md"
        _ = plan.write_text("## Delegation Context\n\n- **Project:** Identity fallback\n")
        environment = os.environ.copy()
        self.isolate_recorder_roots(environment)
        self.isolate_identity(environment)
        environment.update({"CODEX_THREAD_ID": "codex-session", "CLAUDE_CODE_SESSION_ID": "claude-session",
                            "UNIT_HISTORY_DIR": str(self.history_dir), "UNIT_NOW_EPOCH": "1000"})
        for family, model in (("codex", "codex-model"), ("claude", "claude-model"), ("unknown", "unknown")):
            with self.subTest(family=family):
                session = self.root / f"identity-{family}"
                result = invoke_recorder(["start-run", "--session-dir", str(session),
                                          "--working-dir", str(self.working_dir), "--plan-doc", str(plan)],
                                         environment, self.working_dir)
                if family == "unknown":
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("Unable to detect the unit director's model and effort", result.stderr)
                    self.assertFalse((session / "progress_history_state.json").exists())
                else:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    identity = cast("dict[str, object]", self.read_state(session)["main_agent"])
                    self.assertEqual((identity["family"], identity["model"], identity["effort"]),
                                     (family, model, "unset"))
                if family == "codex":
                    _ = codex.write_text('{"type":"turn_context","payload":{}}\n')
                elif family == "claude":
                    claude.unlink()

    def test_the_eta_names_the_day_the_work_is_expected_to_land(self) -> None:
        """A duration has to be added to the clock by hand; an arrival does not."""
        session_dir = self.start_run("arrival", 0)
        _ = self.run_command(
            "start-phase",
            "--session-dir",
            str(session_dir),
            "--phase-id",
            "3",
            "--phase-title",
            "Retry handling",
            at=70_000,
        )
        _ = self.run_command(
            "start-pass",
            "--session-dir",
            str(session_dir),
            "--pass-kind",
            "impl",
            "--activity",
            "writing the retry path",
            "--called-task",
            "direct.implementation",
            "--called-family",
            "codex",
            "--called-model",
            "gpt-called",
            "--called-effort",
            "high",
            at=70_010,
        )
        header = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "10",
            "--project-percent",
            "10",
            "--phase-raw-percent",
            "50",
            "--phase-percent",
            "50",
            "--cap-stage",
            "implementation",
            "--activity",
            "writing the retry path",
            at=80_000,
        )
        phase_row = next(line for line in header.splitlines() if line.startswith("| Phase 3"))
        self.assertEqual(
            [cell.strip() for cell in phase_row.strip("|").split("|")][1:4],
            ["50", "02:46:40", "tomorrow 01:00"],
        )


    def test_the_eta_band_brackets_the_arrival_and_names_its_own_swing(self) -> None:
        """One arrival hides how firm it is; two bracket it and show the spread.

        Ten points of doubt either way produce an asymmetric phase band, and
        the parenthesised distances show its spread without clock arithmetic.
        """
        session_dir = self.start_run("band", 0)
        self.start_phase_and_pass(session_dir, 70_000)
        header = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "10",
            "--project-percent",
            "10",
            "--phase-raw-percent",
            "50",
            "--phase-percent",
            "50",
            "--cap-stage",
            "implementation",
            "--activity",
            "writing the retry path",
            at=80_000,
        )
        phase_row = next(line for line in header.splitlines() if line.startswith("| Phase 3"))
        self.assertEqual(
            [cell.strip() for cell in phase_row.strip("|").split("|")][5:],
            ["tomorrow 00:04 (-00:55)", "tomorrow 02:23 (+01:23)"],
        )


    def test_the_header_names_no_position_when_ids_and_order_disagree(self) -> None:
        """A deferred earlier phase and a retired id leave three numbers apart.

        Phase 3 is in flight, one of four phases is done, and the active
        heading is the second. The header names where the run is and no
        position; the Phase row names the id and the Phases column the count.
        """
        session_dir = self.start_run("position", 0)
        plan_path = self.working_dir / "docs" / "position.md"
        _ = plan_path.write_text(
            plan_path.read_text(encoding="utf-8")
            + "### Phase 1 — Deferred  · status: todo\n\n"
            + "### Phase 3 — Live work  · status: todo\n\n"
            + "### Phase 4 — Shrunk archive form (`bbac234`)\n\n"
            + "### Phase 5 — Waiting  · status: todo\n\n",
            encoding="utf-8",
        )
        self.start_phase_and_pass(session_dir, 70_000)
        header = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "90",
            "--project-percent",
            "90",
            "--phase-raw-percent",
            "50",
            "--phase-percent",
            "50",
            "--cap-stage",
            "implementation",
            "--activity",
            "writing the retry path",
            at=80_000,
        )
        self.assertEqual(
            header.splitlines()[0],
            "**bevy_hana_rubric - feature/rubric**",
        )
        # The supplied 90 is advisory; one finished phase and a half-done
        # second of four is 38.
        self.assertIn("| Project |  38 | ", header)
        self.assertIn("| 1 of 4 done |", header)
        self.assertIn("| Phase 3 |", header)

    def test_the_phases_count_adds_a_run_phase_the_plan_has_no_heading_for(self) -> None:
        """Phase 3 opened before its plan listed phases, and the plan lists 1 and 2.

        The count takes Phase 3 in, so the report never puts "Phase 3" beside
        "of 2" (hana cables, 2026-10-19: "19 of 31 done" beside "Phase 32").
        """
        session_dir = self.start_run("unlisted", 0)
        self.start_phase_and_pass(session_dir, 70_000)
        plan_path = self.working_dir / "docs" / "unlisted.md"
        _ = plan_path.write_text(
            plan_path.read_text(encoding="utf-8")
            + "### Phase 1 — Done work  · status: done\n\n"
            + "### Phase 2 — Waiting  · status: todo\n\n",
            encoding="utf-8",
        )
        header = self.run_progress(session_dir, 80_000)
        self.assertIn("| 1 of 3 done |", header)
        self.assertIn("| Phase 3 |", header)

    def test_start_phase_refuses_a_phase_the_plan_has_no_heading_for(self) -> None:
        session_dir = self.start_run("refused", 0)
        plan_path = self.working_dir / "docs" / "refused.md"
        _ = plan_path.write_text(
            plan_path.read_text(encoding="utf-8") + "### Phase 1 — Only phase  · status: todo\n\n",
            encoding="utf-8",
        )
        result = self.run_failing_command(
            "start-phase", "--session-dir", str(session_dir), "--phase-id", "3",
            "--phase-title", "Retry handling", at=70_000,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Phase 3 has no heading in", result.stderr)
        state = cast(dict[str, object], json.loads(
            (session_dir / "progress_history_state.json").read_text(encoding="utf-8")
        ))
        self.assertIsNone(state["phase"])

    def test_start_phase_finds_a_standing_plan_phase_by_its_title_id(self) -> None:
        session_dir = self.start_run("standing", 0)
        plan_path = self.working_dir / "docs" / "standing.md"
        # style_plan.py next-phase writes the ticket id as the title, not the number.
        _ = plan_path.write_text(
            plan_path.read_text(encoding="utf-8") + "### Phase 201 — types-unit-201  · status: todo\n\n",
            encoding="utf-8",
        )
        _ = self.run_command(
            "start-phase", "--session-dir", str(session_dir), "--phase-id", "types-unit-201",
            "--phase-title", "types-unit-201", at=70_000,
        )
        state = cast(dict[str, object], json.loads(
            (session_dir / "progress_history_state.json").read_text(encoding="utf-8")
        ))
        phase = cast(dict[str, object], state["phase"])
        self.assertEqual(phase["id"], "types-unit-201")

    def test_the_header_names_a_worktree_once_when_its_branch_shares_the_name(
        self,
    ) -> None:
        _ = subprocess.run(
            ["git", "symbolic-ref", "HEAD", "refs/heads/bevy_hana_rubric"],
            cwd=self.working_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        session_dir = self.start_run("same_name", 0)
        self.start_phase_and_pass(session_dir, 70_000)
        header = self.run_command(
            "progress", "--tables",
            "--session-dir",
            str(session_dir),
            "--project-raw-percent",
            "50",
            "--project-percent",
            "50",
            "--phase-raw-percent",
            "50",
            "--phase-percent",
            "50",
            "--cap-stage",
            "implementation",
            "--activity",
            "writing the retry path",
            at=80_000,
        )
        self.assertEqual(header.splitlines()[0], "**bevy_hana_rubric**")

    def report_eta_chain(self, name: str, *, untimed_gate: bool = False, ranged: bool = True) -> tuple[Path, str]:
        def at(hour: int, minute: int) -> int:
            return int(datetime(2026, 10, 16, hour, minute, tzinfo=UTC).timestamp())

        session_dir = self.start_run(name, at(16, 0))
        plan_path = self.working_dir / "docs" / f"{name}.md"
        lines = [
            "### Phase 4 — Finished delivery  · status: done", "",
            "### Phase 5 — Current delivery  · status: todo", "",
            "### Phase 6 — Next delivery  · status: todo", "",
        ]
        if untimed_gate:
            lines += ["**Blocked by:** G2", ""]
        lines += ["### Phase 7 — Last delivery  · status: todo", ""]
        _ = plan_path.write_text(
            plan_path.read_text(encoding="utf-8") + "\n".join(lines), encoding="utf-8",
        )
        if untimed_gate:
            self.add_production_marker(session_dir)
            _ = (plan_path.parent / "example-production.md").write_text(
                "\n".join((
                    "# Production — Example", "", "## Production Context", "",
                    "- **User zone:** UTC", "", "## Gates", "",
                    "| Gate | Waiting | Waits on | Clears when |",
                    "| --- | --- | --- | --- |",
                    "| G2 | unit-a phase 6 | renderer phase 9 | reviewed checkpoint |", "",
                )),
                encoding="utf-8",
            )
        _ = self.run_command(
            "start-phase", "--session-dir", str(session_dir),
            "--phase-id", "4", "--phase-title", "Finished delivery", at=at(16, 18),
        )
        _ = self.run_command(
            "finish-phase", "--session-dir", str(session_dir), "--status", "completed",
            at=at(16, 28),
        )
        _ = self.run_command(
            "start-phase", "--session-dir", str(session_dir),
            "--phase-id", "5", "--phase-title", "Current delivery", at=at(16, 30),
        )
        _ = self.run_command(
            "start-pass", "--session-dir", str(session_dir), "--pass-kind", "impl",
            "--activity", "building the delivery", "--called-task", "direct.implementation",
            "--called-family", "codex", "--called-model", "fixture-model", at=at(16, 30),
        )
        _ = self.run_command(
            "eta", "--session-dir", str(session_dir), "--time", "18:30",
            *(("--earliest", "18:20", "--latest", "18:50") if ranged else ()), "--basis", "stated",
            at=at(17, 40),
        )
        report = self.run_command(
            "progress", "--tables", "--session-dir", str(session_dir),
            "--project-raw-percent", "10", "--project-percent", "10",
            "--phase-raw-percent", "50", "--phase-percent", "50",
            "--cap-stage", "implementation", "--activity", "building the delivery",
            at=at(18, 0),
        )
        return session_dir, report

    @staticmethod
    def report_time(hour: int, minute: int = 0) -> int:
        return int(datetime(2026, 10, 16, hour, minute, tzinfo=UTC).timestamp())

    def short_report_fixture(
        self, name: str, *, promises: tuple[str, ...] = ("17:15", "17:45", "18:30"),
        running_fix: bool = True, completed_fix: bool = True, gate: str = "",
    ) -> Path:
        session_dir = self.start_run(name, self.report_time(15))
        plan = self.working_dir / "docs" / f"{name}.md"
        _ = plan.write_text(
            plan.read_text(encoding="utf-8") + "\n".join((
                "### Phase 3 — Retry handling  · status: todo", "",
                "### Phase 4 — Next delivery  · status: todo", "",
                *(["**Blocked by:** G3", ""] if gate else []),
            )), encoding="utf-8",
        )
        if gate:
            self.add_production_marker(session_dir)
            _ = (plan.parent / "example-production.md").write_text("\n".join((
                "# Production — Example", "", "## Production Context", "",
                "- **User zone:** UTC", "", "## Gates", "",
                "| Gate | Waiting | Waits on | Clears when |",
                "| --- | --- | --- | --- |",
                f"| G3 | unit-a phase 4 | renderer phase 9 | {gate} |", "",
            )), encoding="utf-8")
        self.start_phase(session_dir, self.report_time(15))
        if completed_fix:
            self.start_slot_pass(session_dir, "impl", "fix", self.report_time(15, 1), fix_pass=1)
            self.finish_slot_pass(session_dir, "impl", "completed", self.report_time(15, 3))
        self.team_slot = ""
        for index, target in enumerate(promises):
            _ = self.run_command(
                "eta", "--session-dir", str(session_dir), "--time", target,
                "--basis", "remaining retry checks", at=self.report_time(15, 10 + index),
            )
        if running_fix:
            # Two workers in one fix round must read as one running round.
            for offset, slot in enumerate(("impl", "test")):
                self.run_board(session_dir, "post", slot, "register", "up",
                               at=self.report_time(15, 20) + offset)
                self.run_board(session_dir, "role", slot, "fix", "retry repair",
                               at=self.report_time(15, 21) + offset)
                self.start_slot_pass(session_dir, slot, "fix", self.report_time(15, 22) + offset,
                                     called_model=f"gpt-{slot}", fix_pass=2)
            self.run_board(session_dir, "post", "impl", "status", "repairing retry recovery",
                           at=self.report_time(15, 30))
            self.run_board(session_dir, "post", "test", "blocked", "waiting for retry fixture",
                           at=self.report_time(15, 31))
            self.team_slot = ""
        return session_dir

    def short_progress(
        self, session_dir: Path, at: int, *, tables: bool = False, zone: str = "UTC",
    ) -> str:
        return self.run_command(
            "progress", *(["--tables"] if tables else []),
            "--session-dir", str(session_dir),
            "--project-raw-percent", "40", "--project-percent", "40",
            "--phase-raw-percent", "30", "--phase-percent", "30",
            "--cap-stage", "implementation", "--activity", "implementing",
            at=at, zone=zone,
        )

    @staticmethod
    def eta_report_line(report: str) -> str:
        return next(line for line in report.splitlines()
                    if line.startswith(("ETA ", "gated ")))

    def assert_summary_without_round_table(self, report: str) -> None:
        rows = self.table_rows(report, [*SUMMARY_HEADER[:5], "Phases", *SUMMARY_HEADER[5:]])
        self.assertEqual([row[0] for row in rows], ["Project", "Phase 3"])
        self.assertFalse(any(line.startswith("| Stage ") for line in report.splitlines()), report)
        self.assertFalse(any(line.startswith("Earlier:") for line in report.splitlines()), report)

    def test_short_progress_keeps_promises_stage_and_worker_narration_without_tables(self) -> None:
        session_dir = self.short_report_fixture("short-workers")

        report = self.short_progress(session_dir, self.report_time(16))

        self.assertEqual(self.eta_report_line(report),
                         "ETA 18:30 (first 17:15, +1:15, moved 2×, 1 fix round + 1 running)")
        self.assertIn("**Phase 3: Retry handling**", report)
        self.assertIn("▸ **Fix 2 - implementing**", report)
        impl = next(line for line in report.splitlines() if "repairing retry recovery" in line)
        tester = next(line for line in report.splitlines() if "waiting for retry fixture" in line)
        for slot, model, state, worker in (
            ("impl", "gpt-impl", "running", impl), ("test", "gpt-test", "blocked", tester),
        ):
            self.assertIn(slot, worker)
            self.assertIn(model, worker)
            self.assertIn("fix", worker)
            self.assertIn(state, worker)
        self.assert_summary_without_round_table(report)

    def test_short_progress_names_the_first_eta_and_completed_fix_round(self) -> None:
        session_dir = self.short_report_fixture("short-first", promises=("16:10",), running_fix=False)

        report = self.short_progress(session_dir, self.report_time(16))

        self.assertEqual(self.eta_report_line(report), "ETA 16:10 (on its first ETA, 1 fix round)")
        self.assert_summary_without_round_table(report)

    def test_short_progress_retains_each_worker_while_the_director_runs_an_activity(self) -> None:
        session_dir = self.short_report_fixture("short-concurrent-activity")
        _ = self.run_command(
            "start-activity", "--session-dir", str(session_dir),
            "--label", "Verify", "--activity", "checking the combined repair",
            at=self.report_time(15, 40),
        )

        report = self.short_progress(session_dir, self.report_time(16))

        for slot, model, state in (("impl", "gpt-impl", "running"), ("test", "gpt-test", "waiting")):
            worker = next(line for line in report.splitlines() if f"({slot})" in line)
            self.assertRegex(worker, rf"\({slot}\) fix \S+ {state} ·")
            self.assertIn(model, worker)
        self.assertIn("▸ **Fix 2 - implementing**", report)
        self.assert_summary_without_round_table(report)

    def test_short_eta_names_a_running_fix_round_without_zero_completed_rounds(self) -> None:
        session_dir = self.short_report_fixture("short-running-fix", completed_fix=False)

        report = self.short_progress(session_dir, self.report_time(16))

        self.assertEqual(self.eta_report_line(report),
                         "ETA 18:30 (first 17:15, +1:15, moved 2×, 1 fix round running)")

    def test_short_eta_includes_the_date_for_a_promise_on_a_different_local_day(self) -> None:
        session_dir = self.short_report_fixture("short-next-day", promises=("2026-10-17T16:10",))

        utc = self.short_progress(session_dir, self.report_time(16))
        eastern = self.short_progress(session_dir, self.report_time(16), zone="America/New_York")

        self.assertEqual(self.eta_report_line(utc),
                         "ETA Sat 17 Oct 16:10 (on its first ETA, 1 fix round + 1 running)")
        self.assertEqual(self.eta_report_line(eastern),
                         "ETA Sat 17 Oct 12:10 (on its first ETA, 1 fix round + 1 running)")

    def test_short_eta_line_formats_promises_in_the_user_zone(self) -> None:
        session_dir = self.short_report_fixture("short-user-zone")

        report = self.short_progress(session_dir, self.report_time(16), zone="America/New_York")

        self.assertEqual(self.eta_report_line(report),
                         "ETA 14:30 (first 13:15, +1:15, moved 2×, 1 fix round + 1 running)")

    def test_short_progress_keeps_an_overdue_promise_instead_of_the_chain_schedule_end(self) -> None:
        session_dir = self.short_report_fixture("short-overdue")

        before = self.short_progress(session_dir, self.report_time(18, 31))
        later = self.short_progress(session_dir, self.report_time(19))

        for report in (before, later):
            self.assertEqual(self.eta_report_line(report),
                             "ETA 18:30 (passed, first 17:15, +1:15, moved 2×, 1 fix round + 1 running)")

    def test_short_report_between_windows_keeps_the_eta_and_workers(self) -> None:
        session_dir = self.short_report_fixture("short-between")
        _ = self.short_progress(session_dir, self.report_time(16))
        for offset, slot in enumerate(("impl", "test")):
            self.finish_slot_pass(session_dir, slot, "completed", self.report_time(16, 1) + offset)
        self.team_slot = ""
        state = session_dir / "progress_history_state.json"
        history = self.history_dir / "runs" / "short-between.jsonl"
        before = (state.read_bytes(), history.read_bytes())

        report = self.short_progress(session_dir, self.report_time(16, 10))

        self.assertEqual(self.eta_report_line(report),
                         "ETA 18:30 (first 17:15, +1:15, moved 2×, 3 fix rounds)")
        self.assertIn("repairing retry recovery", report)
        self.assertIn("waiting for retry fixture", report)
        self.assertIn("No pass or activity is open.", report)
        self.assert_summary_without_round_table(report)
        self.assertEqual((state.read_bytes(), history.read_bytes()), before)

    def test_tables_flag_adds_the_round_table_and_keeps_the_summary_and_eta_line(self) -> None:
        session_dir = self.short_report_fixture("short-tables")

        short = self.short_progress(session_dir, self.report_time(16))
        detailed = self.short_progress(session_dir, self.report_time(16), tables=True)

        self.assertEqual(self.eta_report_line(detailed), self.eta_report_line(short))
        self.assert_summary_without_round_table(short)
        header = [*SUMMARY_HEADER[:5], "Phases", *SUMMARY_HEADER[5:]]
        self.assertEqual(self.table_rows(detailed, header)[:2], self.table_rows(short, header))
        self.assertEqual(
            [row[0] for row in self.table_rows(
                detailed, [*SUMMARY_HEADER[:5], "Phases", *SUMMARY_HEADER[5:]],
            )[:2]],
            ["Project", "Phase 3"],
        )
        self.assertEqual(
            [row[0] for row in self.table_rows(
                detailed, ["Stage", "Start", "Elapsed", "Agent 1", "Agent 2", "Result"],
            )],
            ["Fix 1", "Fix 2"],
        )
        self.assertIn("repairing retry recovery", detailed)

    def test_later_phase_gates_do_not_replace_the_current_phase_promise(self) -> None:
        for name, gate in (
            ("timed", "release 2026-10-21 11:47"),
            ("untimed", "reviewed checkpoint"),
        ):
            with self.subTest(gate=gate):
                session_dir = self.short_report_fixture(f"short-{name}", gate=gate)

                report = self.short_progress(session_dir, self.report_time(16))

                self.assertEqual(self.eta_report_line(report),
                                 "ETA 18:30 (first 17:15, +1:15, moved 2×, 1 fix round + 1 running)")
                self.assert_summary_without_round_table(report)
                for offset, slot in enumerate(("impl", "test")):
                    self.finish_slot_pass(session_dir, slot, "completed", self.report_time(16, 1) + offset)
                self.team_slot = ""
                between = self.short_progress(session_dir, self.report_time(16, 10))
                self.assertEqual(self.eta_report_line(between),
                                 "ETA 18:30 (first 17:15, +1:15, moved 2×, 3 fix rounds)")
                self.assert_summary_without_round_table(between)

    def test_eta_line_names_only_the_requested_phases_own_gate(self) -> None:
        eta_line = cast(Callable[[Path, str], str], vars(progress_history)["_phase_eta_line"])
        for name, gate, expected in (
            ("timed", "release 2026-10-21 11:47", "gated until Wed 21 Oct 11:47"),
            ("untimed", "reviewed checkpoint", "gated by G3: renderer phase 9"),
        ):
            with self.subTest(gate=gate):
                session_dir = self.short_report_fixture(f"short-own-{name}", gate=gate)
                with patch.dict(os.environ, {
                    "UNIT_HISTORY_DIR": str(self.history_dir),
                    "UNIT_NOW_EPOCH": str(self.report_time(16)), "USER_ZONE": "UTC",
                }):
                    self.assertEqual(eta_line(session_dir, "4"), expected)

    def test_short_report_keeps_the_stage_of_an_activity_after_its_phase_finished(self) -> None:
        session_dir = self.short_report_fixture("short-shrink", running_fix=False)
        _ = self.run_command("finish-phase", "--session-dir", str(session_dir),
                             "--status", "completed", at=self.report_time(16))
        _ = self.run_command("start-activity", "--session-dir", str(session_dir),
                             "--label", "shrink", "--activity", "recording the as-built phase",
                             at=self.report_time(16, 1))

        report = self.short_progress(session_dir, self.report_time(16, 2))

        stage = next(line for line in report.splitlines() if line.startswith("▸ "))
        self.assertIn("shrink", stage)
        self.assertIn("recording the as-built phase", stage)
        self.assertNotIn("No pass or activity is open.", report)
        self.assert_summary_without_round_table(report)

    def test_project_eta_uses_the_last_chain_finish_and_shifts_the_open_eta_band(self) -> None:
        session_dir, report = self.report_eta_chain("project-chain")
        row = next(line for line in report.splitlines() if line.startswith("| Project"))
        cells = [cell.strip() for cell in row.strip("|").split("|")]

        self.assertEqual(cells[1:4], ["38", "02:00:00", "today 18:54"])
        self.assertEqual(cells[-2:], ["today 18:44 (-00:10)", "today 19:14 (+00:20)"])
        with patch.dict(os.environ, {
            "UNIT_HISTORY_DIR": str(self.history_dir),
            "UNIT_NOW_EPOCH": str(int(datetime(2026, 10, 16, 18, tzinfo=UTC).timestamp())),
            "USER_ZONE": "UTC",
        }):
            record = phase_table.build(session_dir)
        assert isinstance(record.plan_finish, phase_table.PredictedFinish)
        self.assertEqual(record.plan_finish.at, datetime(2026, 10, 16, 18, 54, tzinfo=UTC))

    def test_project_eta_takes_the_phase_rows_band_when_the_stated_eta_has_no_range(self) -> None:
        _, report = self.report_eta_chain("project-unranged", ranged=False)
        rows = {line.split("|")[1].strip(): [cell.strip() for cell in line.strip("|").split("|")]
                for line in report.splitlines() if line.startswith(("| Project", "| Phase"))}
        project, phase = rows["Project"], rows["Phase 5"]

        def swing(cell: str) -> str:
            return cell.rsplit(" ", 1)[1]
        self.assertNotIn("—", project[-2:])
        self.assertEqual([swing(cell) for cell in project[-2:]], [swing(cell) for cell in phase[-2:]])
        self.assertEqual(project[3], "today 18:54")

    def test_project_eta_names_an_untimed_gate_and_has_no_band(self) -> None:
        _, report = self.report_eta_chain("project-gated", untimed_gate=True)
        row = next(line for line in report.splitlines() if line.startswith("| Project"))
        cells = [cell.strip() for cell in row.strip("|").split("|")]

        self.assertEqual(cells[1:4], ["38", "02:00:00", "after G2"])
        self.assertEqual(cells[-2:], ["—", "—"])

    def test_eta_records_exact_and_ranged_events(self) -> None:
        now = int(datetime(2026, 10, 8, 12, tzinfo=UTC).timestamp())
        session_dir = self.start_run("eta-events", now - 100)
        self.start_phase(session_dir, now - 50)

        exact = self.run_command(
            "eta",
            "--session-dir",
            str(session_dir),
            "--time",
            "2026-10-08T13:00",
            "--basis",
            "the remaining checks took an hour last phase",
            at=now,
        )
        ranged = self.run_command(
            "eta",
            "--session-dir",
            str(session_dir),
            "--time",
            "2026-10-08T14:00",
            "--earliest",
            "2026-10-08T13:40",
            "--latest",
            "2026-10-08T14:30",
            "--basis",
            "the last two full gates took 100–150 minutes",
            at=now + 60,
        )

        self.assertEqual(exact, "ETA recorded: 13:00 UTC")
        self.assertEqual(ranged, "ETA recorded: 14:00 UTC")
        events = [
            event
            for event in self.read_events("eta-events")
            if event.get("event_type") == "eta_stated"
        ]
        self.assertEqual(len(events), 2)
        self.assertEqual([event.get("eta_phase_id") for event in events], ["3", "3"])
        self.assertEqual(
            events[0].get("eta_at"),
            datetime(2026, 10, 8, 13, tzinfo=UTC).timestamp(),
        )
        self.assertEqual(
            events[0].get("basis"),
            "the remaining checks took an hour last phase",
        )
        self.assertNotIn("eta_earliest_at", events[0])
        self.assertNotIn("eta_latest_at", events[0])
        self.assertEqual(
            events[1].get("eta_earliest_at"),
            datetime(2026, 10, 8, 13, 40, tzinfo=UTC).timestamp(),
        )
        self.assertEqual(
            events[1].get("eta_latest_at"),
            datetime(2026, 10, 8, 14, 30, tzinfo=UTC).timestamp(),
        )

    def test_eta_can_target_the_next_phase_while_another_is_active(self) -> None:
        now = int(datetime(2026, 10, 8, 12, tzinfo=UTC).timestamp())
        session_dir = self.start_run("eta-next-phase", now - 100)
        _ = self.run_command(
            "start-phase", "--session-dir", str(session_dir),
            "--phase-id", "2", "--phase-title", "Current delivery",
            at=now - 50,
        )
        active_phase = self.read_state(session_dir)["phase"]

        output = self.run_command(
            "eta", "--session-dir", str(session_dir),
            "--phase-id", "3", "--time", "13:00", "--basis", "checkpoint",
            at=now,
        )

        events = [event for event in self.read_events("eta-next-phase")
                  if event.get("event_type") == "eta_stated"]
        self.assertEqual(output, "ETA recorded: 13:00 UTC")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].get("eta_phase_id"), "3")
        self.assertEqual(events[0].get("phase_id"), "2")
        self.assertEqual(events[0].get("basis"), "checkpoint")
        self.assertEqual(
            events[0].get("eta_at"),
            datetime(2026, 10, 8, 13, tzinfo=UTC).timestamp(),
        )
        self.assertEqual(self.read_state(session_dir)["phase"], active_phase)

    def test_eta_can_target_a_phase_without_an_active_phase(self) -> None:
        now = int(datetime(2026, 10, 8, 12, tzinfo=UTC).timestamp())
        session_dir = self.start_run("eta-before-phase", now - 100)

        _ = self.run_command(
            "eta", "--session-dir", str(session_dir),
            "--phase-id", "3", "--time", "13:00", "--basis", "checkpoint",
            at=now,
        )

        events = [event for event in self.read_events("eta-before-phase")
                  if event.get("event_type") == "eta_stated"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].get("eta_phase_id"), "3")
        self.assertFalse(events[0].get("phase_id"))
        self.assertIsNone(self.read_state(session_dir)["phase"])

    def test_eta_uses_the_process_timezone(self) -> None:
        now = int(datetime(2026, 10, 8, 12, tzinfo=UTC).timestamp())
        session_dir = self.start_run("eta-local-zone", now - 100)
        self.start_phase(session_dir, now - 50)

        output = self.run_command(
            "eta",
            "--session-dir",
            str(session_dir),
            "--time",
            "2026-10-08T13:00",
            "--basis",
            "the measured final checks",
            at=now,
            zone="America/Los_Angeles",
        )

        self.assertEqual(output, "ETA recorded: 13:00 PDT")
        eta_events = [
            event
            for event in self.read_events("eta-local-zone")
            if event.get("event_type") == "eta_stated"
        ]
        self.assertEqual(len(eta_events), 1)
        self.assertEqual(
            eta_events[0].get("eta_at"),
            datetime(2026, 10, 8, 20, tzinfo=UTC).timestamp(),
        )

    def test_short_eta_uses_the_users_local_calendar_date(self) -> None:
        now = int(datetime(2026, 10, 8, 2, tzinfo=UTC).timestamp())
        session_dir = self.start_run("eta-local-date", now - 100)
        self.start_phase(session_dir, now - 50)

        output = self.run_command(
            "eta", "--session-dir", str(session_dir),
            "--time", "20:00", "--basis", "eta-command",
            at=now, zone="America/Los_Angeles",
        )

        events = [event for event in self.read_events("eta-local-date")
                  if event.get("event_type") == "eta_stated"]
        self.assertEqual(output, "ETA recorded: 20:00 PDT")
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].get("eta_at"),
            datetime(2026, 10, 8, 3, tzinfo=UTC).timestamp(),
        )

    def test_eta_refuses_invalid_times_without_recording_an_event(self) -> None:
        now = int(datetime(2026, 10, 8, 12, tzinfo=UTC).timestamp())
        cases = (
            (
                "past",
                ["--time", "2026-10-08T11:59"],
                "--time must not be in the past",
            ),
            (
                "past-short-time",
                ["--time", "11:59"],
                "--time must not be in the past",
            ),
            (
                "late-earliest",
                [
                    "--time",
                    "2026-10-08T13:00",
                    "--earliest",
                    "2026-10-08T13:01",
                    "--latest",
                    "2026-10-08T13:30",
                ],
                "--earliest must not be after --time",
            ),
            (
                "early-latest",
                [
                    "--time",
                    "2026-10-08T13:00",
                    "--earliest",
                    "2026-10-08T12:30",
                    "--latest",
                    "2026-10-08T12:59",
                ],
                "--latest must not be before --time",
            ),
            (
                "earliest-only",
                [
                    "--time",
                    "2026-10-08T13:00",
                    "--earliest",
                    "2026-10-08T12:30",
                ],
                "--earliest and --latest must be supplied together",
            ),
            (
                "latest-only",
                [
                    "--time",
                    "2026-10-08T13:00",
                    "--latest",
                    "2026-10-08T13:30",
                ],
                "--earliest and --latest must be supplied together",
            ),
        )
        for name, time_arguments, message in cases:
            with self.subTest(name=name):
                session_dir = self.start_run(f"eta-refused-{name}", now - 100)
                self.start_phase(session_dir, now - 50)
                result = self.run_failing_command(
                    "eta",
                    "--session-dir",
                    str(session_dir),
                    *time_arguments,
                    "--basis",
                    "measured gates",
                    at=now,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn(message, result.stderr)
                self.assertFalse(
                    any(
                        event.get("event_type") == "eta_stated"
                        for event in self.read_events(f"eta-refused-{name}")
                    )
                )

    def test_eta_refuses_a_time_the_local_clock_skips(self) -> None:
        now = int(datetime(2026, 10, 8, 12, tzinfo=UTC).timestamp())
        session_dir = self.start_run("eta-skipped-hour", now - 100)
        self.start_phase(session_dir, now - 50)

        result = self.run_failing_command(
            "eta",
            "--session-dir",
            str(session_dir),
            "--time",
            "2027-03-14T02:30",
            "--basis",
            "measured gates",
            at=now,
            zone="America/Los_Angeles",
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--time is a time the local clock skips", result.stderr)
        self.assertFalse(
            any(
                event.get("event_type") == "eta_stated"
                for event in self.read_events("eta-skipped-hour")
            )
        )

    def test_eta_refuses_a_blank_basis(self) -> None:
        now = int(datetime(2026, 10, 8, 12, tzinfo=UTC).timestamp())
        session_dir = self.start_run("eta-blank-basis", now - 100)
        self.start_phase(session_dir, now - 50)

        result = self.run_failing_command(
            "eta",
            "--session-dir",
            str(session_dir),
            "--time",
            "2026-10-08T13:00",
            "--basis",
            "  ",
            at=now,
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--basis must not be blank", result.stderr)
        self.assertFalse(
            any(
                event.get("event_type") == "eta_stated"
                for event in self.read_events("eta-blank-basis")
            )
        )

    def test_eta_requires_an_active_phase_without_an_explicit_phase_id(self) -> None:
        now = int(datetime(2026, 10, 8, 12, tzinfo=UTC).timestamp())
        session_dir = self.start_run("eta-no-phase", now - 100)

        result = self.run_failing_command(
            "eta",
            "--session-dir",
            str(session_dir),
            "--time",
            "2026-10-08T13:00",
            "--basis",
            "measured gates",
            at=now,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("An active phase is required", result.stderr)
        self.assertFalse(
            any(
                event.get("event_type") == "eta_stated"
                for event in self.read_events("eta-no-phase")
            )
        )

    def test_eta_refresh_failures_keep_the_event_and_success_output(self) -> None:
        parser = recorder_parser()
        now = int(datetime(2026, 10, 8, 12, tzinfo=UTC).timestamp())
        failures: tuple[
            tuple[str, subprocess.CompletedProcess[str] | BaseException], ...
        ] = (
            (
                "refused",
                subprocess.CompletedProcess(
                    [], 1, "", "target belongs to another unit\n"
                ),
            ),
            ("timeout", subprocess.TimeoutExpired(["phase_table.py"], 10)),
            ("launch", OSError("phase table unavailable")),
        )

        for name, refresh_result in failures:
            with self.subTest(name=name):
                session_dir = self.start_run(f"eta-refresh-{name}", now - 100)
                self.start_phase(session_dir, now - 50)
                self.add_production_marker(session_dir)
                arguments = parser.parse_args(
                    [
                        "eta",
                        "--session-dir",
                        str(session_dir),
                        "--time",
                        "2026-10-08T13:00",
                        "--basis",
                        "the measured final checks",
                    ]
                )

                returned, stdout, stderr = self.invoke_handler(
                    recorder_handler("_eta"),
                    arguments,
                    at=now,
                    refresh_result=refresh_result,
                )

                self.assertIsNone(returned)
                self.assertEqual(stdout, "ETA recorded: 13:00 UTC\n")
                self.assertEqual(len(stderr.splitlines()), 1)
                self.assertTrue(stderr.startswith("phase table not written:"))
                events = [
                    event
                    for event in self.read_events(f"eta-refresh-{name}")
                    if event.get("event_type") == "eta_stated"
                ]
                self.assertEqual(len(events), 1)

    def test_phase_table_failure_does_not_change_recorder_command_results(self) -> None:
        parser = recorder_parser()
        started_at = 40_000

        def prepare(
            command: str,
            name: str,
        ) -> tuple[Callable[[argparse.Namespace], None], argparse.Namespace]:
            session_dir = self.start_run(name, started_at)
            if command != "start-phase":
                self.start_phase_and_pass(session_dir, started_at)
            if command == "finish-run":
                self.close_the_only_pass(session_dir, at=started_at + 20)
                _ = self.run_command(
                    "finish-phase",
                    "--session-dir",
                    str(session_dir),
                    "--status",
                    "completed",
                    at=started_at + 30,
                )
            self.add_production_marker(session_dir)
            if command == "start-phase":
                arguments = parser.parse_args(
                    [
                        command,
                        "--session-dir",
                        str(session_dir),
                        "--phase-id",
                        "3",
                        "--phase-title",
                        "Retry handling",
                    ]
                )
                return recorder_handler("_start_phase"), arguments
            if command == "progress":
                arguments = parser.parse_args(
                    [
                        command,
                        "--session-dir",
                        str(session_dir),
                        "--raw-percent",
                        "30",
                        "--percent",
                        "30",
                        "--activity",
                        "implementing",
                    ]
                )
                return recorder_handler("_progress"), arguments
            if command == "finish-phase":
                arguments = parser.parse_args(
                    [
                        command,
                        "--session-dir",
                        str(session_dir),
                        "--status",
                        "completed",
                    ]
                )
                return recorder_handler("_finish_phase"), arguments
            arguments = parser.parse_args(
                [
                    command,
                    "--session-dir",
                    str(session_dir),
                    "--status",
                    "completed",
                ]
            )
            return recorder_handler("_finish_run"), arguments

        success = subprocess.CompletedProcess[str]([], 0, "", "")
        refusal = subprocess.CompletedProcess[str](
            [], 1, "", "target belongs to another unit\n"
        )
        for command in ("start-phase", "progress", "finish-phase", "finish-run"):
            with self.subTest(command=command):
                success_handler, success_arguments = prepare(
                    command, f"refresh-success-{command}"
                )
                failure_handler, failure_arguments = prepare(
                    command, f"refresh-failure-{command}"
                )
                successful = self.invoke_handler(
                    success_handler,
                    success_arguments,
                    at=started_at + 100,
                    refresh_result=success,
                )
                refused = self.invoke_handler(
                    failure_handler,
                    failure_arguments,
                    at=started_at + 100,
                    refresh_result=refusal,
                )

                self.assertEqual(refused[:2], successful[:2])
                self.assertEqual(successful[2], "")
                self.assertEqual(
                    refused[2],
                    "phase table not written: target belongs to another unit\n",
                )

    def test_phase_table_timeout_prints_one_line(self) -> None:
        session_dir = self.start_run("refresh-timeout", 50_000)
        self.add_production_marker(session_dir)
        arguments = recorder_parser().parse_args(
            [
                "start-phase",
                "--session-dir",
                str(session_dir),
                "--phase-id",
                "3",
                "--phase-title",
                "Retry handling",
            ]
        )

        returned, stdout, stderr = self.invoke_handler(
            recorder_handler("_start_phase"),
            arguments,
            at=50_100,
            refresh_result=subprocess.TimeoutExpired(["phase_table.py"], 10),
        )

        self.assertIsNone(returned)
        self.assertEqual(stdout, "")
        self.assertEqual(len(stderr.splitlines()), 1)
        self.assertIn("phase table not written:", stderr)
        self.assertIn("timed out after 10 seconds", stderr)

    def test_plan_without_production_marker_starts_no_phase_table_process(self) -> None:
        session_dir = self.start_run("refresh-not-a-production", 60_000)

        with patch("scripts.direct.progress_history.subprocess.run") as run:
            refresh_phase_table(session_dir)

        run.assert_not_called()

    def test_invalid_utf8_plan_starts_no_phase_table_process_or_output(self) -> None:
        session_dir = self.start_run("refresh-invalid-utf8", 65_000)
        state = self.read_state(session_dir)
        plan_path = Path(cast(str, state["project_plan_doc"]))
        _ = plan_path.write_bytes(b"\xff")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            patch.dict(
                os.environ,
                {
                    "PHASE_TABLE_VAULT": str(self.root / "vault"),
                    "UNIT_HISTORY_DIR": str(self.history_dir),
                },
            ),
            patch("scripts.direct.progress_history.subprocess.run") as run,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            refresh_phase_table(session_dir)

        run.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_progress_refresh_writes_the_production_phase_note(self) -> None:
        from scripts.production import fake_showrunner

        started_at = 70_000
        session_dir = self.start_run("refresh-end-to-end", started_at)
        self.start_phase_and_pass(session_dir, started_at)
        state = self.read_state(session_dir)
        plan_path = Path(cast(str, state["project_plan_doc"]))
        plan_text = plan_path.read_text(encoding="utf-8")
        _ = plan_path.write_text(
            plan_text + "### Phase 3 — Retry handling  · status: todo\n\n",
            encoding="utf-8",
        )
        self.add_production_marker(session_dir)
        production_doc = self.working_dir / "docs" / "example-production.md"
        _ = production_doc.write_text(
            "\n".join(
                (
                    "# Example production",
                    "",
                    "- **Merge branch:** main",
                    f"- **Showrunner checkout:** {self.working_dir}",
                    "- **Log:** logs/showrunner.md",
                    "- **User zone:** UTC",
                    "",
                    "## Units",
                    "",
                    "| Unit | Plan | Worktree | Branch | Port | Owns |",
                    "| --- | --- | --- | --- | --- | --- |",
                    "",
                )
            ),
            encoding="utf-8",
        )
        notifier_dir = self.root / "notifier"
        sessions_dir = self.root / "notifier-sessions"
        _ = fake_showrunner.write_timer(
            notifier_dir,
            "example",
            "example-showrunner-id",
            "UTC",
            production_doc,
        )
        held = fake_showrunner.write_session(
            sessions_dir,
            "example-showrunner",
            "example-showrunner-id",
        )
        self.addCleanup(held.close)
        vault_root = self.root / "vault" / "showrunners"
        vault_root.parent.mkdir(parents=True)
        history_paths_before = {
            path.relative_to(self.history_dir) for path in self.history_dir.rglob("*")
        }

        with patch.dict(
            os.environ,
            {
                "PHASE_TABLE_VAULT": str(vault_root),
                "NOTIFIER_STATE_DIR": str(notifier_dir),
                "NOTIFIER_SESSIONS_DIR": str(sessions_dir),
            },
        ):
            report = self.run_progress(session_dir, at=started_at + 100)

        note = vault_root / "example-showrunner" / "unit-a.md"
        self.assertTrue(note.is_file(), self.last_recorder_stderr)
        self.assertEqual(
            note.read_text(encoding="utf-8").splitlines()[:10],
            [
                "---",
                "phase_table: true",
                "showrunner: example-showrunner",
                f'production_file: "{production_doc.resolve().as_uri()}"',
                "unit: unit-a",
                'now: "Phase 3 of 1 — Retry handling"',
                "---",
                "",
                "# unit-a",
                "",
            ],
        )
        self.assertIn("**Phase 3 of 1 — Retry handling**", note.read_text(encoding="utf-8"))
        self.assertIn("| Phase 3 |", report)
        self.assertEqual(
            [path.relative_to(vault_root) for path in vault_root.rglob("*.md")],
            [Path("example-showrunner/unit-a.md")],
        )
        self.assertEqual(
            {path.relative_to(self.history_dir) for path in self.history_dir.rglob("*")},
            history_paths_before,
        )

    def test_eta_refresh_rewrites_the_production_phase_note(self) -> None:
        from scripts.production import fake_showrunner

        now = int(datetime(2026, 10, 8, 12, tzinfo=UTC).timestamp())
        session_dir = self.start_run("eta-refresh-end-to-end", now - 100)
        self.start_phase(session_dir, now - 50)
        state = self.read_state(session_dir)
        plan_path = Path(cast(str, state["project_plan_doc"]))
        plan_text = plan_path.read_text(encoding="utf-8")
        _ = plan_path.write_text(
            plan_text + "### Phase 3 — Retry handling  · status: todo\n\n",
            encoding="utf-8",
        )
        self.add_production_marker(session_dir)
        production_doc = self.working_dir / "docs" / "example-production.md"
        _ = production_doc.write_text(
            "\n".join(
                (
                    "# Example production",
                    "",
                    "- **Merge branch:** main",
                    f"- **Showrunner checkout:** {self.working_dir}",
                    "- **Log:** logs/showrunner.md",
                    "- **User zone:** UTC",
                    "",
                    "## Units",
                    "",
                    "| Unit | Plan | Worktree | Branch | Port | Owns |",
                    "| --- | --- | --- | --- | --- | --- |",
                    "",
                )
            ),
            encoding="utf-8",
        )
        notifier_dir = self.root / "notifier"
        sessions_dir = self.root / "notifier-sessions"
        _ = fake_showrunner.write_timer(
            notifier_dir,
            "example",
            "eta-example-showrunner-id",
            "UTC",
            production_doc,
        )
        held = fake_showrunner.write_session(
            sessions_dir,
            "example-showrunner",
            "eta-example-showrunner-id",
        )
        self.addCleanup(held.close)
        vault_root = self.root / "vault" / "showrunners"
        vault_root.parent.mkdir(parents=True)

        result = self.run_command(
            "eta",
            "--session-dir",
            str(session_dir),
            "--time",
            "2026-10-08T13:00",
            "--basis",
            "the measured final checks",
            at=now,
        )

        self.assertEqual(result, "ETA recorded: 13:00 UTC")
        note = vault_root / "example-showrunner" / "unit-a.md"
        self.assertTrue(note.is_file(), self.last_recorder_stderr)
        note_text = note.read_text(encoding="utf-8")
        self.assertIn("| ETA | 10-08 13:00 |", note_text)
        self.assertIn(
            "| ETA from | stated 12:00: the measured final checks |",
            note_text,
        )


    def start_numbered_phase(
        self, session_dir: Path, phase_id: str, at: int, *extra: str
    ) -> None:
        _ = self.run_command(
            "start-phase",
            "--session-dir",
            str(session_dir),
            "--phase-id",
            phase_id,
            "--phase-title",
            f"Phase {phase_id} work",
            *extra,
            at=at,
        )

    def finish_numbered_phase(self, session_dir: Path, at: int, *extra: str) -> None:
        _ = self.run_command(
            "finish-phase",
            "--session-dir",
            str(session_dir),
            "--status",
            "completed",
            *extra,
            at=at,
        )

    def phase_events(self, name: str, event_type: str) -> list[dict[str, object]]:
        return [event for event in self.read_events(name) if event.get("event_type") == event_type]

    def open_phase_ids(self, session_dir: Path) -> tuple[str, list[str]]:
        state = self.read_state(session_dir)
        current = cast(dict[str, object], state["phase"])
        parked = cast(list[dict[str, object]], state.get("open_phases", []))
        return cast(str, current["id"]), [cast(str, phase["id"]) for phase in parked]

    def test_start_phase_restores_ownership_after_a_checkpoint_release(self) -> None:
        started_at = 40_000
        session_dir = self.start_run("reopen-released-phase", started_at)
        self.start_numbered_phase(session_dir, "6", started_at + 10)
        directory = phase_reservation.phase_directory(session_dir, "6")
        _ = phase_reservation.enroll(directory, "6", "board_ready")
        _ = phase_reservation.record_active(directory, phase_reservation.Acquisition("r1", "c1", "a" * 40))
        phase_reservation.committed(directory, "b" * 40)
        self.assertTrue(phase_reservation.clear(directory, "b" * 40))
        marker = directory / "reservation_released"
        self.assertEqual(marker.read_text().strip(), "b" * 40)
        self.assertEqual(phase_reservation.open_phases(session_dir), [])
        self.finish_numbered_phase(session_dir, started_at + 20)
        self.start_numbered_phase(session_dir, "6", started_at + 30)
        self.assertFalse(marker.exists())
        self.assertEqual(phase_reservation.read_state(directory)["kind"], "enrolled_awaiting_first_touch")
        owners = phase_reservation.attribute(phase_reservation.open_phases(session_dir), ["unlisted.rs"])
        self.assertEqual([phase.phase for phase in owners.by_phase], ["6"])
        self.assertEqual(owners.refused, [])

    def test_open_phase_reader_tracks_start_finish_and_reopen_without_writing(self) -> None:
        started_at = 40_000
        session_dir = self.start_run("open-phase-reader", started_at)
        state_path = session_dir / plan_history.STATE_FILENAME
        self.assertEqual(recorder_state.read_open_phase_ids(session_dir), [])
        self.start_numbered_phase(session_dir, "6", started_at + 10)
        self.start_numbered_phase(session_dir, "18", started_at + 20)
        before = state_path.read_bytes()
        self.assertEqual(recorder_state.read_open_phase_ids(session_dir), ["6", "18"])
        self.assertEqual(state_path.read_bytes(), before)
        self.finish_numbered_phase(session_dir, started_at + 30, "--phase-id", "6")
        self.assertEqual(recorder_state.read_open_phase_ids(session_dir), ["18"])
        self.finish_numbered_phase(session_dir, started_at + 40)
        self.assertEqual(recorder_state.read_open_phase_ids(session_dir), [])
        directory = phase_reservation.phase_directory(session_dir, "6")
        _ = phase_reservation.enroll(directory, "6", "board_ready")
        _ = phase_reservation.clear(directory)
        self.start_numbered_phase(session_dir, "6", started_at + 50)
        self.assertEqual(recorder_state.read_open_phase_ids(session_dir), ["6"])
        self.assertEqual(phase_reservation.read_state(directory)["kind"], "enrolled_awaiting_first_touch")
        missing = session_dir / "not-a-run"
        self.assertEqual(recorder_state.read_open_phase_ids(missing), [])
        self.assertFalse(missing.exists())

    def test_a_phase_started_while_another_is_open_leaves_the_older_one_open(self) -> None:
        started_at = 40_000
        session_dir = self.start_run("overlapping-phases", started_at)
        self.start_numbered_phase(session_dir, "6", started_at + 10)
        self.start_slot_pass(session_dir, "impl", "impl", started_at + 20)
        self.team_slot = ""
        self.start_numbered_phase(session_dir, "18", started_at + 100)

        self.assertEqual(self.open_phase_ids(session_dir), ("18", ["6"]))
        self.assertEqual(self.pass_slots(session_dir)["impl"]["status"], "active")
        self.assertEqual(self.phase_events("overlapping-phases", "phase_finished"), [])

        self.finish_slot_pass(session_dir, "impl", "completed", started_at + 150)
        finished_pass = self.phase_events("overlapping-phases", "pass_finished")[-1]
        self.assertEqual(finished_pass.get("phase_id"), "6")

        self.team_slot = ""
        self.finish_numbered_phase(session_dir, started_at + 200)
        state = self.read_state(session_dir)
        current = cast(dict[str, object], state["phase"])
        self.assertEqual((current["id"], current["status"]), ("6", "active"))
        self.assertEqual(state["open_phases"], [])
        self.finish_numbered_phase(session_dir, started_at + 300, "--phase-id", "6")
        finished = self.phase_events("overlapping-phases", "phase_finished")
        self.assertEqual(
            [(event.get("phase_id"), event.get("phase_elapsed_seconds")) for event in finished],
            [("18", 100), ("6", 290)],
        )

    def test_finishing_an_older_open_phase_leaves_the_current_one_and_its_workers(self) -> None:
        started_at = 41_000
        session_dir = self.start_run("finish-older-phase", started_at)
        self.start_numbered_phase(session_dir, "6", started_at + 10)
        self.start_slot_pass(session_dir, "impl", "impl", started_at + 20)
        self.team_slot = ""
        self.start_numbered_phase(session_dir, "18", started_at + 100)
        self.start_slot_pass(session_dir, "review", "review", started_at + 110)
        self.team_slot = ""

        self.finish_numbered_phase(session_dir, started_at + 200, "--phase-id", "6")

        self.assertEqual(self.open_phase_ids(session_dir), ("18", []))
        slots = self.pass_slots(session_dir)
        self.assertEqual(slots["impl"]["status"], "interrupted")
        self.assertEqual(slots["review"]["status"], "active")
        finished = self.phase_events("finish-older-phase", "phase_finished")[-1]
        self.assertEqual(finished.get("phase_id"), "6")
        self.assertNotIn("pass_instance_id", finished)

    def test_finish_phase_naming_a_phase_that_is_not_open_is_refused(self) -> None:
        started_at = 42_000
        session_dir = self.start_run("finish-unknown-phase", started_at)
        self.start_numbered_phase(session_dir, "6", started_at + 10)
        with self.assertRaises(subprocess.CalledProcessError) as raised:
            self.finish_numbered_phase(session_dir, started_at + 20, "--phase-id", "7")
        self.assertIn("Phase 7 is not open; open phases: 6", cast(str, raised.exception.stderr))
        self.assertEqual(self.open_phase_ids(session_dir), ("6", []))

    def launch_probe(
        self, session_dir: Path, *extra: str, kind: str = "impl", at: int = 42_530,
    ) -> subprocess.CompletedProcess[str]:
        # Deliberately bypass valid_launch_arguments: these cases exercise the launch boundary.
        return self.run_failing_command(
            "start-pass", "--session-dir", str(session_dir), "--pass-kind", kind,
            "--activity", "probe launch identity", "--called-task", "direct.implementation",
            "--called-family", "codex", "--called-model", "fixture-model", *extra,
            at=at, complete_launch=False,
        )

    def assert_launch_refused(
        self, session_dir: Path, result: subprocess.CompletedProcess[str], *,
        phase_id: str, prompt_file: str = "", kind: str = "impl",
    ) -> None:
        self.assertNotEqual(result.returncode, 0)
        history = self.history_dir / "runs" / f"{session_dir.name}.jsonl"
        records = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        refused = [row for row in records if row["event_type"] == "launch_refused"]
        self.assertTrue(refused, result.stderr)
        event = refused[-1]
        self.assertTrue(event["reason"])
        self.assertEqual(event["pass_kind"], kind)
        self.assertEqual(event["phase_id"], phase_id)
        self.assertEqual(event["prompt_file"], prompt_file)
        self.assertFalse(any(row["event_type"] == "pass_started" for row in records))

    def test_launch_without_an_explicit_phase_records_a_refusal(self) -> None:
        for phase_open in (False, True):
            with self.subTest(phase_open=phase_open):
                session = self.start_run(f"launch-missing-phase-{phase_open}", 42_500)
                if phase_open:
                    self.start_phase(session, 42_510)
                result = self.launch_probe(session)
                self.assert_launch_refused(session, result, phase_id="")
                self.assertIn("phase", result.stderr.lower())

    def test_launch_into_a_closed_phase_records_a_refusal(self) -> None:
        session = self.start_run("launch-stale-phase", 42_500)
        self.start_numbered_phase(session, "13", 42_510)
        self.finish_numbered_phase(session, 42_520, "--phase-id", "13")
        self.start_numbered_phase(session, "16", 42_525)
        result = self.launch_probe(session, "--phase-id", "13")
        self.assert_launch_refused(session, result, phase_id="13")
        self.assertIn("Phase 13 is not open", result.stderr)

    def test_launch_refuses_each_stale_prompt_identity(self) -> None:
        for index, line in enumerate((
            "Phase 4 — Retry handling", "Phase 3 — Old title",
        )):
            with self.subTest(line=line):
                session = self.start_run(f"launch-stale-prompt-{index}", 42_500)
                self.start_phase(session, 42_510)
                prompt = self.root / f"stale-{index}.md"
                _ = prompt.write_text(f"Work Specification\n{line}\nFix the retry.\n")
                result = self.launch_probe(session, "--phase-id", "3", "--prompt-file", str(prompt))
                self.assert_launch_refused(session, result, phase_id="3", prompt_file=str(prompt))
                self.assertIn("prompt", result.stderr.lower())

    def test_only_the_first_phase_identity_line_checks_the_prompt(self) -> None:
        session = self.start_run("prompt-later-phase-content", 42_500)
        self.start_phase(session, 42_510)
        prompt = self.root / "prompt-with-follow-up.md"
        _ = prompt.write_text(
            "Work Specification\nPhase 3 — Retry handling\n"
            + "Follow-up Work Orders:\nPhase 4 — Other work\n")
        result = self.launch_probe(session, "--phase-id", "3", "--prompt-file", str(prompt))
        self.assertEqual(result.returncode, 0, result.stderr)
        started, = self.pass_events(session.name, "pass_started")
        check = cast(dict[str, object], started["launch_check"])
        self.assertEqual(check["prompt_phase_line"], "matched")
        self.assertEqual(check["phase_id"], "3")

    def restart_fallback_arguments(self, session: Path, worker: str, kind: str) -> list[str]:
        # Import the shutdown CLI in a child: it has its own script import roots.
        fallback_program = """import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'scripts/shutdown'))
import restart
seat = restart.PausedSeat(run_dir=sys.argv[1], seat=sys.argv[2], role=sys.argv[3],
    thread_id='paused-thread', turn_live=True, launcher_command=[])
print(restart._resume_worker_command(seat, sys.argv[4]))
"""
        built = subprocess.run(
            [sys.executable, "-c", fallback_program, str(session), worker, kind,
             str(self.working_dir)],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(built.returncode, 0, built.stderr)
        return shlex.split(built.stdout)

    def test_restart_fallback_uses_the_paused_pass_phase_and_passes_the_launch_check(self) -> None:
        for kind in ("impl", "review", "fix"):
            with self.subTest(kind=kind):
                session = self.start_run(f"resume-fallback-{kind}", 42_500)
                self.start_phase(session, 42_510)
                worker = f"paused-{kind}"
                self.start_slot_pass(session, kind, kind, 42_515, worker_name=worker)
                _ = (session / f"impl_worker_{kind}.json").write_text(json.dumps(
                    {"name": worker, "slot": kind, "thread_id": "paused-thread"}))
                self.team_slot = ""
                self.start_numbered_phase(session, "4", 42_520)
                arguments = self.restart_fallback_arguments(session, worker, kind)
                self.assertIn("--phase", arguments)
                phase = arguments[arguments.index("--phase") + 1]
                self.assertEqual(phase, "3", "the current phase 4 is not the paused pass's phase")
                cause: tuple[str, ...] = ()
                if kind == "fix":
                    self.assertIn("--fix-cause", arguments)
                    cause = ("--fix-cause", arguments[arguments.index("--fix-cause") + 1])
                    self.assertEqual(cause[1], "resume after a move")
                prompt = session / f"{worker}.resume-prompt.md"
                _ = prompt.write_text("Phase 3 — Retry handling\nResume the paused work.\n")
                state_path = session / plan_history.STATE_FILENAME
                before = state_path.read_bytes()
                self.team_slot = kind
                result = self.run_failing_command(
                    "check-launch", "--session-dir", str(session), "--pass-kind", kind,
                    "--phase-id", phase, "--prompt-file", str(prompt), *cause,
                    at=42_525, complete_launch=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(state_path.read_bytes(), before, "a preflight must not change pass or slot state")

    def test_restart_review_follows_worker_identity_across_a_different_durable_slot(self) -> None:
        session = self.start_run("resume-review-different-slot", 42_500)
        self.start_phase(session, 42_510)
        self.start_slot_pass(session, "fix", "review", 42_515, worker_name="project-fix")
        self.team_slot = ""
        self.start_numbered_phase(session, "4", 42_520)
        self.start_slot_pass(session, "impl", "impl", 42_525, worker_name="project-impl")
        slots = self.pass_slots(session)
        self.assertEqual(slots["fix"]["worker_name"], "project-fix")
        self.assertEqual(slots["impl"]["worker_name"], "project-impl")
        review, implementation = self.pass_events(session.name, "pass_started")
        self.assertEqual(review["worker_name"], "project-fix")
        self.assertEqual(implementation["worker_name"], "project-impl")
        _ = (session / "impl_worker_impl.json").write_text(json.dumps(
            {"name": "project-fix", "slot": "impl", "thread_id": "paused-thread"}))

        arguments = self.restart_fallback_arguments(session, "project-fix", "review")
        self.assertEqual(Path(arguments[1]).name, "review.sh")
        self.assertEqual(arguments[arguments.index("--to") + 1], "project-fix")
        self.assertIn("--phase", arguments)
        self.assertEqual(arguments[arguments.index("--phase") + 1], "3")
        prompt = session / "project-fix.resume-prompt.md"
        _ = prompt.write_text("Phase 3 — Retry handling\nResume the paused review.\n")
        result = self.run_failing_command(
            "check-launch", "--session-dir", str(session), "--pass-kind", "review",
            "--phase-id", arguments[arguments.index("--phase") + 1],
            "--prompt-file", str(prompt), at=42_530, complete_launch=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_restart_omits_phase_when_worker_identity_is_missing_or_ambiguous(self) -> None:
        for label, workers in (
            ("ambiguous", {"fix": "project-fix", "impl": "project-fix"}),
            ("missing", {"fix": "other-reviewer", "impl": "project-impl"}),
        ):
            with self.subTest(identity=label):
                session = self.start_run(f"resume-identity-{label}", 42_500)
                self.start_phase(session, 42_510)
                self.start_slot_pass(session, "fix", "review", 42_515, worker_name=workers["fix"])
                self.team_slot = ""
                self.start_numbered_phase(session, "4", 42_520)
                self.start_slot_pass(session, "impl", "impl", 42_525, worker_name=workers["impl"])
                _ = (session / "impl_worker_impl.json").write_text(json.dumps(
                    {"name": "project-fix", "slot": "impl", "thread_id": "paused-thread"}))

                arguments = self.restart_fallback_arguments(session, "project-fix", "review")
                self.assertNotIn("--phase", arguments, "a durable slot cannot prove the paused phase")
                result = self.run_failing_command(
                    "check-launch", "--session-dir", str(session), "--pass-kind", "review",
                    at=42_530, complete_launch=False,
                )
                self.assertNotEqual(result.returncode, 0)
                history = self.history_dir / "runs" / f"{session.name}.jsonl"
                events = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
                refused, = [event for event in events if event["event_type"] == "launch_refused"]
                self.assertEqual(refused["phase_id"], "")
                self.assertEqual(refused["pass_kind"], "review")
                self.assertIn("phase", str(refused["reason"]).lower())

    def test_matching_and_absent_prompt_phase_lines_both_record_the_launch_check(self) -> None:
        for label, text, expected in (
            ("matched", "Work Specification\nPhase 3 — Retry handling\nWrite the retry.\n", "matched"),
            ("absent", "Write the retry.\n", "absent"),
        ):
            with self.subTest(label=label):
                session = self.start_run(f"launch-{label}", 42_500)
                self.start_phase(session, 42_510)
                prompt = self.root / f"{label}.md"
                _ = prompt.write_text(text)
                result = self.launch_probe(session, "--phase-id", "3", "--prompt-file", str(prompt))
                self.assertEqual(result.returncode, 0, result.stderr)
                started, = self.pass_events(session.name, "pass_started")
                check = cast(dict[str, object], started["launch_check"])
                self.assertEqual(check["phase_id"], "3")
                self.assertEqual(check["phase_title"], "Retry handling")
                self.assertEqual(check["prompt_phase_line"], expected)
                self.assertIn("fix_cause", check)

    def test_fix_launch_without_a_ledger_round_or_cause_records_a_refusal(self) -> None:
        session = self.start_run("launch-no-fix-cause", 42_500)
        self.start_phase(session, 42_510)
        result = self.launch_probe(session, "--phase-id", "3", kind="fix")
        self.assert_launch_refused(session, result, phase_id="3", kind="fix")
        self.assertIn("--fix-cause", result.stderr)

    def test_fix_launch_with_a_failed_gate_cause_keeps_its_evidence(self) -> None:
        session = self.start_run("launch-explicit-fix-cause", 42_500)
        self.start_phase(session, 42_510)
        result = self.launch_probe(session, "--phase-id", "3", "--fix-cause",
                                   "retry tests fail after integration", kind="fix")
        self.assertEqual(result.returncode, 0, result.stderr)
        started, = self.pass_events(session.name, "pass_started")
        check = cast(dict[str, object], started["launch_check"])
        self.assertEqual(check["fix_cause"], "retry tests fail after integration")

    def test_fix_launch_uses_the_named_phases_in_flight_ledger_round(self) -> None:
        self.write_full_config()
        session = self.start_run("launch-ledger-cause", 42_500)
        self.start_phase(session, 42_510)
        finding = self.run_findings(session, "open", "--severity", "blocker", "--title", "retry",
                                    "--caught-by", "worker", at=42_515)
        _ = self.run_findings(session, "dispatch", "--covers", finding, at=42_520)
        result = self.launch_probe(session, "--phase-id", "3", kind="fix")
        self.assertEqual(result.returncode, 0, result.stderr)
        started, = self.pass_events(session.name, "pass_started")
        check = cast(dict[str, object], started["launch_check"])
        cause = cast(dict[str, object], check["fix_cause"])
        self.assertEqual(cause["round"], 1)
        self.assertEqual(cause["covered"], [finding])

    def test_another_open_phases_ledger_does_not_authorize_a_fix_launch(self) -> None:
        self.write_full_config()
        session = self.start_run("launch-other-ledger", 42_500)
        self.start_phase(session, 42_510)
        finding = self.run_findings(session, "open", "--severity", "blocker", "--title", "retry",
                                    "--caught-by", "worker", at=42_515)
        _ = self.run_findings(session, "dispatch", "--covers", finding, at=42_520)
        self.start_numbered_phase(session, "4", 42_525)
        result = self.launch_probe(session, "--phase-id", "4", kind="fix")
        self.assert_launch_refused(session, result, phase_id="4", kind="fix")

    def test_a_pass_naming_an_older_open_phase_is_stamped_with_it(self) -> None:
        started_at = 42_500
        session_dir = self.start_run("pass-names-phase", started_at)
        self.start_numbered_phase(session_dir, "15", started_at + 10)
        older = cast(dict[str, object], self.read_state(session_dir)["phase"])
        self.start_numbered_phase(session_dir, "16", started_at + 20)
        self.start_slot_pass(session_dir, "impl", "impl", started_at + 30, "gpt-called", 0, "--phase-id", "15")
        self.team_slot = ""
        self.start_slot_pass(session_dir, "test", "test", started_at + 40)
        self.team_slot = ""
        slots = self.pass_slots(session_dir)
        self.assertEqual(cast(dict[str, object], slots["impl"]["phase"])["instance_id"], older["instance_id"])
        current = cast(dict[str, object], self.read_state(session_dir)["phase"])
        self.assertEqual(cast(dict[str, object], slots["test"]["phase"])["instance_id"], current["instance_id"])
        with self.assertRaises(subprocess.CalledProcessError) as raised:
            self.start_slot_pass(session_dir, "fix", "fix", started_at + 50, "gpt-called", 0, "--phase-id", "7")
        self.assertIn("Phase 7 is not open; open phases: 15, 16", cast(str, raised.exception.stderr))

    def test_a_pass_naming_a_finished_phase_names_the_plan_review_order(self) -> None:
        # A plan review launched after its phase's finish-phase is refused; the refusal says why.
        started_at = 42_600
        session_dir = self.start_run("pass-after-finish", started_at)
        self.start_numbered_phase(session_dir, "13", started_at + 10)
        self.start_numbered_phase(session_dir, "16", started_at + 20)
        self.finish_numbered_phase(session_dir, started_at + 30, "--phase-id", "13")
        with self.assertRaises(subprocess.CalledProcessError) as raised:
            self.start_slot_pass(session_dir, "review", "review", started_at + 40, "gpt-called", 0, "--phase-id", "13")
        stderr = cast(str, raised.exception.stderr)
        self.assertIn("Phase 13 is not open; open phases: 16", stderr)
        self.assertIn("run its plan review before its finish-phase", stderr)

    def test_back_filling_an_earlier_phase_leaves_the_open_phase_unchanged(self) -> None:
        started_at = 43_000
        session_dir = self.start_run("back-filled-phase", started_at)
        self.start_numbered_phase(session_dir, "6", started_at + 10)
        self.start_slot_pass(session_dir, "impl", "impl", started_at + 20)
        self.team_slot = ""
        phase_six = cast(dict[str, object], self.read_state(session_dir)["phase"])

        def moment(offset: int) -> str:
            return datetime.fromtimestamp(started_at + offset, UTC).isoformat()

        self.start_numbered_phase(
            session_dir, "18", started_at + 500, "--started-at", moment(30)
        )
        self.finish_numbered_phase(
            session_dir, started_at + 510, "--phase-id", "18", "--finished-at", moment(70)
        )

        state = self.read_state(session_dir)
        self.assertEqual(state["phase"], phase_six)
        self.assertEqual(state["open_phases"], [])
        self.assertEqual(self.pass_slots(session_dir)["impl"]["status"], "active")
        started = self.phase_events("back-filled-phase", "phase_started")[-1]
        finished = self.phase_events("back-filled-phase", "phase_finished")[-1]
        self.assertEqual(
            (started.get("phase_id"), started.get("timestamp_epoch"), started.get("backfilled")),
            ("18", float(started_at + 30), True),
        )
        self.assertEqual(
            (finished.get("phase_id"), finished.get("phase_elapsed_seconds"), finished.get("backfilled")),
            ("18", 40, True),
        )
        trial = self.run_command(
            "review-trial", "--session-dir", str(session_dir), "--phase-id", "18",
            at=started_at + 520,
        )
        self.assertTrue(trial.startswith("review trial: ux 0 findings, code 0 findings"))

    def test_a_back_fill_time_in_the_future_or_before_its_start_is_refused(self) -> None:
        started_at = 44_000
        session_dir = self.start_run("bad-back-fill", started_at)

        def moment(offset: int) -> str:
            return datetime.fromtimestamp(started_at + offset, UTC).isoformat()

        with self.assertRaises(subprocess.CalledProcessError) as future:
            self.start_numbered_phase(
                session_dir, "18", started_at + 10, "--started-at", moment(60)
            )
        self.assertIn("--started-at", cast(str, future.exception.stderr))
        self.start_numbered_phase(session_dir, "18", started_at + 100, "--started-at", moment(50))
        with self.assertRaises(subprocess.CalledProcessError) as early:
            self.finish_numbered_phase(
                session_dir, started_at + 110, "--phase-id", "18", "--finished-at", moment(40)
            )
        self.assertIn("--finished-at is before Phase 18 started", cast(str, early.exception.stderr))

    def test_finish_run_closes_every_open_phase(self) -> None:
        started_at = 45_000
        session_dir = self.start_run("finish-run-open-phases", started_at)
        self.start_numbered_phase(session_dir, "6", started_at + 10)
        self.start_numbered_phase(session_dir, "18", started_at + 20)
        _ = self.run_command(
            "finish-run", "--session-dir", str(session_dir), "--status", "completed",
            at=started_at + 30,
        )
        finished = self.phase_events("finish-run-open-phases", "phase_finished")
        self.assertEqual([event.get("phase_id") for event in finished], ["18", "6"])
        self.assertEqual(self.read_state(session_dir)["open_phases"], [])


class PhaseCountTests(unittest.TestCase):
    """A plan's phase headings take three forms; all three must be counted.

    Counting only the `· status:` forms silently drops every shrunk phase,
    which under-reported a 68%-complete plan as 36%.
    """

    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {"UNIT_SYNC_WRITES": "0"}))
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def _count(self, body: str, phase_percent: int = 0) -> dict[str, object]:
        plan = Path(self.temporary.name) / "plan.md"
        _ = plan.write_text(body, encoding="utf-8")
        completed = subprocess.run(
            [
                "python3",
                str(SCRIPT),
                "phase-count",
                "--plan-doc",
                str(plan),
                "--phase-percent",
                str(phase_percent),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return cast("dict[str, object]", json.loads(completed.stdout))

    def test_counts_all_three_heading_forms(self) -> None:
        counts = self._count(
            "### Phase 1 — Shrunk archive form (`bbac234`)\n"
            + "\n"
            + "### Phase 2 — Another shrunk one (`8c5053a`)\n"
            + "\n"
            + "### Phase 3 — Completed, not yet shrunk  · status: done\n"
            + "\n"
            + "### Phase 4 — Live work  · status: todo\n"
        )
        self.assertEqual(counts["done"], 3)
        self.assertEqual(counts["todo"], 1)
        self.assertEqual(counts["total"], 4)

    def test_review_section_heading_is_not_a_phase(self) -> None:
        counts = self._count(
            "### Phase 1 — Real phase  · status: done\n"
            + "\n"
            + "### Phase 1 Review\n"
            + "\n"
            + "### Phase 2 — Real phase  · status: todo\n"
        )
        self.assertEqual(counts["total"], 2)
        self.assertEqual(counts["done"], 1)
        self.assertEqual(counts["todo"], 1)

    def test_repeated_identifier_counts_once(self) -> None:
        counts = self._count(
            "### Phase 7 — First mention  · status: done\n"
            + "\n"
            + "#### Phase 7 — Same id again  · status: done\n"
        )
        self.assertEqual(counts["total"], 1)
        self.assertEqual(counts["duplicate_ids"], ["7"])

    def test_letter_suffixed_identifier_is_counted(self) -> None:
        counts = self._count(
            "### Phase 4 — Plain  · status: done\n"
            + "\n"
            + "### Phase 4b — Suffixed legacy id  · status: todo\n"
        )
        self.assertEqual(counts["total"], 2)
        self.assertEqual(counts["todo"], 1)

    def test_project_percent_credits_the_phase_in_flight(self) -> None:
        body = (
            "".join(
                f"### Phase {index} — Done  · status: done\n\n" for index in range(1, 4)
            )
            + "### Phase 4 — Live  · status: todo\n"
        )
        self.assertEqual(self._count(body, 0)["project_percent"], 75)
        self.assertEqual(self._count(body, 100)["project_percent"], 100)
        self.assertEqual(self._count(body, 50)["project_percent"], 88)

    def test_plan_without_phase_headings_is_unavailable(self) -> None:
        counts = self._count("# A document with no phases\n")
        self.assertIs(counts["available"], False)
        self.assertIsNone(counts["project_percent"])


class PublicPhaseHelperTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {"UNIT_SYNC_WRITES": "0"}))
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_recorder_helpers_use_public_names_without_private_aliases(self) -> None:
        with patch.dict(os.environ, {"UNIT_NOW_EPOCH": "123.5"}):
            self.assertEqual(plan_history.now_epoch(), 123.5)
        self.assertEqual(
            plan_history.resolve_plan_path(self.root, "docs/plan.md"),
            (self.root / "docs" / "plan.md").resolve(),
        )
        self.assertEqual(plan_history.percent_spread(None), 10.0)
        self.assertFalse(hasattr(progress_history, "_now_epoch"))
        self.assertFalse(hasattr(progress_history, "_plan_path"))
        self.assertFalse(hasattr(progress_history, "_percent_spread"))

    def test_plan_phases_classifies_and_cleans_every_heading_form(self) -> None:
        plan = self.root / "plan.md"
        _ = plan.write_text(
            "\n".join(
                (
                    "### Phase 1 — Archived delivery (`abc1234`)",
                    "",
                    "### Phase 2 — Finished delivery  · status: done",
                    "",
                    "#### Phase 2 — Duplicate is ignored  · status: done",
                    "",
                    "### Phase 2 Review",
                    "",
                    "### Phase 3 — Live delivery  · status: todo",
                    "",
                )
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            plan_history.plan_phases(plan),
            [
                {"id": "1", "title": "Archived delivery", "done": True, "depends_on": None, "blocked_by": ""},
                {"id": "2", "title": "Finished delivery", "done": True, "depends_on": None, "blocked_by": ""},
                {"id": "3", "title": "Live delivery", "done": False, "depends_on": None, "blocked_by": ""},
            ],
        )

    def test_plan_phases_read_each_phase_dependencies_and_gate(self) -> None:
        plan = self.root / "plan.md"
        _ = plan.write_text(
            "\n".join(
                (
                    "### Phase 1 — Base  · status: todo",
                    "**Depends on:** none",
                    "### Phase 2 — Report  · status: todo",
                    "**Blocked by:** G3 — trunk phase 4 merged",
                    "**Depends on:** Phase 1, Phase 4",
                    "### Phase 3 — Docs  · status: todo",
                    "",
                )
            ),
            encoding="utf-8",
        )

        phases = plan_history.plan_phases(plan)

        self.assertEqual(
            [(phase["depends_on"], phase["blocked_by"]) for phase in phases],
            [([], ""), (["1", "4"], "G3"), (None, "")],
        )

    def test_plan_dependencies_read_only_named_local_phase_spans(self) -> None:
        cases: tuple[tuple[str, list[str]], ...] = (
            (
                "the checkpoints of Phases 3, 4 and 5, and the three review records: "
                + "the Phase 3 and Phase 5 re-runs, which the unit director runs on "
                + "2026-11-12 (two weeks after both merged into sdlc as 3c5dea67 on "
                + "2026-10-29) and records in the design doc under Steps 1 and 4",
                ["3", "4", "5"],
            ),
            (
                "phases-unit Phase 11 (the wait-holder field and the phase origin fields) "
                + "merged into sdlc; sdlc said 2026-10-29 its ETA is 19:00 EDT.",
                [],
            ),
            ("Phase 7; postmaster-unit's reply record (main abc8344f)", ["7"]),
            ("Phase 19 (numbering only)", ["19"]),
            ("Phase 10b, Phase 11", ["10b", "11"]),
            ("none", []),
        )
        plan = self.root / "dependencies.md"
        for depends, expected in cases:
            with self.subTest(depends=depends):
                _ = plan.write_text(
                    "### Phase 6 — Consumer  · status: todo\n"
                    + f"**Depends on:** {depends}\n",
                    encoding="utf-8",
                )
                self.assertEqual(plan_history.plan_phases(plan)[0]["depends_on"], expected)

    def test_plan_dependencies_accept_either_case_and_keep_first_seen_order(self) -> None:
        plan = self.root / "dependencies.md"
        _ = plan.write_text(
            "### Phase 6 — Consumer  · status: todo\n"
            + "**Depends on:** phases 4, 3 AND 5; PHASE 10b; phase 4\n",
            encoding="utf-8",
        )
        self.assertEqual(
            plan_history.plan_phases(plan)[0]["depends_on"], ["4", "3", "5", "10b"]
        )

    def test_plan_dependencies_skip_other_unit_spans_but_keep_local_references(self) -> None:
        plan = self.root / "dependencies.md"
        for external in (
            "phases-unit Phase 11",
            "models-unit's phase 4",
            "models-unit’s Phases 4, 5 and 6",
        ):
            with self.subTest(external=external):
                _ = plan.write_text(
                    "### Phase 6 — Consumer  · status: todo\n"
                    + f"**Depends on:** Phase 7; {external}; Phase 19\n",
                    encoding="utf-8",
                )
                self.assertEqual(
                    plan_history.plan_phases(plan)[0]["depends_on"], ["7", "19"]
                )

    def test_plan_phases_preserves_descriptive_parentheticals(self) -> None:
        plan = self.root / "plan.md"
        _ = plan.write_text(
            "\n".join(
                (
                    "### Phase 1 — Gradients (COLRv1 solid + linear/radial)  · status: done",
                    "### Phase 2 — Support (macOS)  · status: todo",
                    "",
                )
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            [phase["title"] for phase in plan_history.plan_phases(plan)],
            ["Gradients (COLRv1 solid + linear/radial)", "Support (macOS)"],
        )

    def test_plan_phases_strips_only_commit_annotations(self) -> None:
        plan = self.root / "plan.md"
        _ = plan.write_text(
            "\n".join(
                (
                    "### Phase 1 — Title (abc1234)",
                    "### Phase 2 — Title (commit abc1234def)",
                    "",
                )
            ),
            encoding="utf-8",
        )

        self.assertEqual(
            [phase["title"] for phase in plan_history.plan_phases(plan)],
            ["Title", "Title"],
        )

    def test_eta_band_seconds_exposes_the_existing_projection_math(self) -> None:
        self.assertEqual(
            plan_history.eta_band_seconds(50, 100, 10.0),
            plan_history.EtaBand(remaining=100, earliest=66, latest=150),
        )
        self.assertIsInstance(
            plan_history.eta_band_seconds(0, 100, 10.0),
            plan_history.EtaProjectionUnavailable,
        )

    def test_plan_runs_finds_matching_history_oldest_first(self) -> None:
        working_dir = self.root / "worktree"
        plan = working_dir / "docs" / "plan.md"
        plan.parent.mkdir(parents=True)
        _ = plan.write_text("# Plan\n", encoding="utf-8")
        runs_dir = self.root / "history" / "runs"
        runs_dir.mkdir(parents=True)

        def write_run(name: str, plan_doc: str, started_at: int) -> Path:
            path = runs_dir / f"{name}.jsonl"
            event = {
                "event_type": "run_started",
                "working_dir": str(working_dir),
                "plan_doc": plan_doc,
                "run_started_at": started_at,
            }
            _ = path.write_text(json.dumps(event) + "\n", encoding="utf-8")
            return path

        newer = write_run("alphabetically-first", str(plan), 200)
        older = write_run("alphabetically-last", "docs/plan.md", 100)
        _ = write_run("another-plan", "docs/other.md", 50)

        with patch.dict(
            os.environ,
            {"UNIT_HISTORY_DIR": str(self.root / "history")},
        ):
            self.assertEqual(plan_history.plan_runs(plan), [older, newer])

    def test_plan_runs_ignores_project_plan_inherited_without_a_run_plan(self) -> None:
        working_dir = self.root / "worktree"
        plan = working_dir / "docs" / "plan.md"
        plan.parent.mkdir(parents=True)
        _ = plan.write_text("# Plan\n", encoding="utf-8")
        runs_dir = self.root / "history" / "runs"
        runs_dir.mkdir(parents=True)
        inherited = {
            "event_type": "run_started",
            "working_dir": str(working_dir),
            "plan_doc": "",
            "project_plan_doc": str(plan),
            "run_started_at": 100,
        }
        _ = (runs_dir / "inherited.jsonl").write_text(
            json.dumps(inherited) + "\n",
            encoding="utf-8",
        )

        with patch.dict(
            os.environ,
            {"UNIT_HISTORY_DIR": str(self.root / "history")},
        ):
            self.assertEqual(plan_history.plan_runs(plan), [])


WorkSeconds = Callable[[list[dict[str, object]], str, float, float], int]
SummaryTable = Callable[[str, dict[str, int], dict[str, int], dict[str, object], float, float, float, dict[str, object]], list[str]]
CalibrationSamples = Callable[[list[dict[str, object]]], list[dict[str, object]]]


def private(name: str) -> object:
    found = vars(progress_history).get(name)
    if not callable(found):
        raise AssertionError(f"progress recorder function is unavailable: {name}")
    return found


def window(kind: str, key: str, started_at: float, finished_at: float | None) -> list[dict[str, object]]:
    """A pass or activity window of phase `p`, open when `finished_at` is None."""
    id_field = f"{kind}_instance_id"
    events: list[dict[str, object]] = [
        {"phase_instance_id": "p", "event_type": f"{kind}_started", id_field: key, "timestamp_epoch": started_at}
    ]
    if finished_at is not None:
        events.append(
            {"phase_instance_id": "p", "event_type": f"{kind}_finished", id_field: key, "timestamp_epoch": finished_at}
        )
    return events


class EtaWorkClockTests(unittest.TestCase):
    """The phase clock leaves idle stretches out, and the project never lands before its phase.

    herdr-trial, 2026-10-15: a phase whose clock ran through a night quoted the next
    evening for an hour of work, and the project row arrived hours before it.
    """

    @property
    def grace(self) -> int:
        grace = vars(progress_history).get("IDLE_GRACE_SECONDS")
        if not isinstance(grace, int):
            raise AssertionError("progress recorder idle grace is unavailable")
        return grace

    def work_seconds(self, events: list[dict[str, object]], phase_instance_id: str, started_at: float, until: float) -> int:
        return cast(WorkSeconds, private("_work_seconds"))(events, phase_instance_id, started_at, until)

    def test_an_overnight_gap_between_dispatches_counts_only_the_grace(self) -> None:
        night = 10 * 3600
        events = window("pass", "a", 100, 1_100) + window("pass", "b", 1_100 + night, 2_100 + night)
        self.assertEqual(self.work_seconds(events, "p", 0, 2_100 + night), 100 + 1_000 + self.grace + 1_000)

    def test_an_open_window_counts_up_to_now(self) -> None:
        events = window("activity", "a", 0, None)
        self.assertEqual(self.work_seconds(events, "p", 0, 5 * 3600), 5 * 3600)

    def test_a_wait_after_the_last_window_counts_only_the_grace(self) -> None:
        events = window("pass", "a", 0, 600)
        self.assertEqual(self.work_seconds(events, "p", 0, 600 + 8 * 3600), 600 + self.grace)

    def test_overlapping_windows_count_once(self) -> None:
        events = window("pass", "a", 0, 1_000) + window("activity", "b", 500, 1_500)
        self.assertEqual(self.work_seconds(events, "p", 0, 1_500), 1_500)

    def test_another_phase_windows_do_not_count(self) -> None:
        events = [{**event, "phase_instance_id": "q"} for event in window("pass", "a", 0, 10_000)]
        events += window("pass", "b", 0, 600)
        self.assertEqual(self.work_seconds(events, "p", 0, 10_000), 600 + self.grace)

    def test_a_phase_with_no_windows_keeps_the_wall_clock(self) -> None:
        self.assertEqual(self.work_seconds([], "p", 1_000, 1_000 + 9 * 3600), 9 * 3600)

    def test_the_project_eta_is_unknown_without_a_plan_chain(self) -> None:
        summary_table = cast(SummaryTable, private("_summary_table"))
        # Percent arithmetic can project both clocks, but there is no plan to
        # supply the Project chain. Only the phase retains that projection.
        project = {"percent": 90, "elapsed": 10 * 3600, "unchanged": 0}
        phase = {"percent": 20, "elapsed": 3600, "unchanged": 0}
        with patch.dict(os.environ, {"USER_ZONE": "UTC"}):
            rows = [
                [cell.strip() for cell in line.strip("|").split("|")]
                for line in summary_table("7", project, phase, {}, 10.0, 0.0, 0.0, {})
                if line.startswith("| Project") or line.startswith("| Phase")
            ]
        project_row, phase_row = rows
        self.assertEqual([project_row[3], *project_row[5:]], ["—", "—", "—"])
        self.assertEqual(phase_row[3], "today 04:00")

    def test_calibration_measures_time_spent_in_work_time(self) -> None:
        calibration_samples = cast(CalibrationSamples, private("_calibration_samples"))
        night = 10 * 3600
        phase: dict[str, object] = {"phase_instance_id": "p", "phase_started_at": 0}
        events: list[dict[str, object]] = [
            *window("pass", "a", 0, 3_600),
            *window("pass", "b", 3_600 + night, 7_200 + night),
            {**phase, "event_type": "progress_reported", "timestamp_epoch": 7_000 + night, "raw_percent": 90, "percent": 90},
            {**phase, "event_type": "phase_finished", "status": "completed", "timestamp_epoch": 7_200 + night},
        ]
        sample, = calibration_samples(events)
        # Work time: 3600 + grace + 3400 of 3600 + grace + 3600. Wall time read 79%.
        expected = 100.0 * (7_000 + self.grace) / (7_200 + self.grace)
        self.assertAlmostEqual(cast(float, sample["temporal_percent"]), expected, places=3)


if __name__ == "__main__":
    _ = unittest.main()
