#!/usr/bin/env python3
"""Tests for unit status tick health with an isolated active run."""

from __future__ import annotations

import json
import os
import socket
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from dailies_input import (ClaudeNotRunning, Decision, SessionGone, StatusBlock, StillWaiting, UnitRow,
                           status_blocks)
from unit_lookup import UnitState
import fake_unit_host
from fake_unit_host import HostedUnit
from sessions import SessionRecord


SCRIPT = Path(__file__).with_name("unit_status.sh")
SESSION_ID = "test-session"


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


def _write_executable(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


class UnitStatusTests(unittest.TestCase):
    def run_statuses(
        self, marker: str | None, health_text: str, health_exit: int, *,
        panes: tuple[dict[str, str], ...],
        units: tuple[str, ...] = ("hook",),
        retired_units: tuple[str, ...] = (),
        stopped_units: tuple[str, ...] = (),
        missing_units: tuple[str, ...] = (),
        unreadable_units: tuple[str, ...] = (),
    ) -> tuple[tuple[str, ...], str | None, str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "scripts" / "production" / "unit_status.sh"
            script.parent.mkdir(parents=True)
            _ = shutil.copy2(SCRIPT, script)
            _write_executable(root / "scripts" / "lib" / "py", '#!/bin/sh\nexec python3 "$@"\n')
            message = root / "scripts" / "message"
            message.mkdir()
            _ = shutil.copy2(SCRIPT.parent.parent / "message" / "sessions.py", message / "sessions.py")
            _write_executable(message / "notifier.sh", """print -r -- "$*" >> "$NOTIFIER_CALL_LOG"
print -r -- "$NOTIFIER_HEALTH_TEXT"
exit "$NOTIFIER_HEALTH_EXIT"
""")
            hooks = root / "scripts" / "hooks"
            hooks.mkdir()
            _ = (hooks / "direct_run.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
            for source_name in ("add_unit.py", "production_units.py", "live_units.py", "showrunners.py", "unit_lookup.py", "session_pane.py"):
                _ = shutil.copy2(SCRIPT.with_name(source_name), script.with_name(source_name))
            (root / "scripts" / "herdr").mkdir()
            _ = shutil.copy2(SCRIPT.parent.parent / "herdr" / "herdr.py", root / "scripts" / "herdr" / "herdr.py")
            for source_name in ("user_zone.py", "probe_session.py", "unit_runs.py", "nix_env.py"):
                _ = shutil.copy2(SCRIPT.parent.parent / "lib" / source_name, script.parent.parent / "lib" / source_name)
            active_dir = root / "active"
            active_dir.mkdir()
            call_log = root / "notifier_calls"
            records = root / "sessions"
            records.mkdir()
            responses_file = root / "herdr-responses.json"
            environment = {
                **os.environ,
                "HOME": str(root),
                "UNIT_ACTIVE_DIR": str(active_dir),
                "UNIT_HISTORY_DIR": str(root / "history"),
                "NOTIFIER_STATE_DIR": str(root / "notifier"),
                "NOTIFIER_NOW_EPOCH": "20100",
                "NOTIFIER_CALL_LOG": str(call_log),
                "NOTIFIER_HEALTH_TEXT": health_text,
                "NOTIFIER_HEALTH_EXIT": str(health_exit),
                "NOTIFIER_SESSIONS_DIR": str(records),
                "HERDR_BIN": str(SCRIPT.parent.parent / "herdr" / "fake_herdr.py"),
                "HERDR_PROC": str(root / "proc"),
                "HERDR_PLATFORM": "linux",
                "FAKE_HERDR_LOG": str(root / "herdr.jsonl"),
                "FAKE_HERDR_RESPONSES": str(responses_file),
            }
            pids: dict[str, int] = {}
            _ = responses_file.write_text(json.dumps({"default": {"code": 0, "stdout": ""}}))
            rows: list[str] = []
            for unit in units:
                worktree = root / "worktrees" / unit
                worktree.mkdir(parents=True)
                _ = (worktree / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
                plan = "(retired by the user)" if unit in retired_units else "docs/plan.md"
                rows.append(f"| {unit} | {plan} | {worktree} | {unit} | — | — |")
                if unit in missing_units:
                    continue
                if unit not in stopped_units:
                    address = root / f"{unit}.sock"
                    held = socket.socket(socket.AF_UNIX)
                    held.bind(str(address))
                    self.addCleanup(held.close)
                    session_id = f"{SESSION_ID}-{unit}"
                    if marker is not None:
                        _ = (active_dir / session_id).write_text(marker, encoding="utf-8")
                    pids[unit] = fake_unit_host.live_pid(self)
                    record = SessionRecord(pid=pids[unit], sessionId=session_id, name=f"claude-{unit}",
                        messagingSocketPath=str(address), updatedAt=1,
                        tmux="", formerNames=[])
                    _ = (records / f"{unit}.json").write_text(
                        "{" if unit in unreadable_units else json.dumps(record))
            production_doc = root / "example-production.md"
            _ = production_doc.write_text("\n".join((
                "# Production", "## Units", "| Unit | Plan | Worktree | Branch | Port | Owns |",
                "| --- | --- | --- | --- | --- | --- |", *rows)), encoding="utf-8")
            zsh = shutil.which("zsh")
            if zsh is None:
                raise RuntimeError("zsh is required for unit status tests")
            outputs: list[str] = []
            for pane_by_unit in panes:
                hosted = [
                    HostedUnit(unit, f"w{index}:p1", {"SHOWRUNNER_UNIT": "example", "SHOWRUNNER_UNIT_ID": unit},
                               pane_by_unit.get(unit, "— holding: waiting on x\n"))
                    for index, unit in enumerate(units, start=1) if unit not in missing_units]
                fake_unit_host.write(environment, hosted)
                for unit in hosted:
                    if unit.session in pids:
                        fake_unit_host.place_process(environment, pids[unit.session], unit.pane)
                result = subprocess.run(
                    [zsh, str(script), str(root / "status"), "America/Los_Angeles", "--production", str(production_doc)],
                    check=True, capture_output=True, text=True, env=environment,
                )
                self.assertEqual(result.stderr, "")
                outputs.append(result.stdout)
            calls = call_log.read_text(encoding="utf-8") if call_log.exists() else None
            seen_path = root / "status" / "decisions_seen"
            seen = seen_path.read_text(encoding="utf-8") if seen_path.exists() else ""
            return tuple(outputs), calls, seen

    def run_status(
        self, marker: str | None, health_text: str, health_exit: int, *,
        stopped_units: tuple[str, ...] = (),
    ) -> tuple[str, str | None]:
        outputs, calls, _ = self.run_statuses(
            marker, health_text, health_exit,
            panes=({"hook": "— holding: waiting on x\n"},), stopped_units=stopped_units,
        )
        return outputs[0], calls

    def parse_status(self, output: str, units: tuple[str, ...] = ("hook",)) -> tuple[StatusBlock, ...]:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "status.txt"
            _ = path.write_text(output, encoding="utf-8")
            return status_blocks(path, tuple(UnitRow(unit, unit, UnitState.RUNNING) for unit in units))

    def test_blocks_are_headed_by_the_unit_ids_of_the_registered_doc(self) -> None:
        output, _ = self.run_status(None, "ok", 0)
        self.assertIn("== hook", output)

    def test_showrunner_form_omits_retired_unit(self) -> None:
        outputs, _, _ = self.run_statuses(
            None, "ok", 0,
            panes=({"hook": "— holding: active\n", "stalls": "— holding: old\n"},),
            units=("hook", "stalls"), retired_units=("stalls",),
        )
        self.assertIn("== hook", outputs[0])
        self.assertNotIn("== stalls", outputs[0])
        self.assertNotIn("SESSION GONE", outputs[0])

    def test_the_last_three_distinct_eta_lines_are_printed_in_pane_order(self) -> None:
        pane = "\n".join(("ETA 09:00", "ETA 10:00 PDT", "  Phase ETA: 12:55 EDT", "ETA 11:00",
                          "From the user: what is the ETA", "ETA 10:00 PDT", "— holding: waiting on x")) + "\n"
        outputs, _, _ = self.run_statuses(None, "ok", 0, panes=({"hook": pane},))
        stated = [line for line in outputs[0].splitlines() if "ETA" in line]
        self.assertEqual(stated, ["Phase ETA: 12:55 EDT", "ETA 11:00", "ETA 10:00 PDT"])

    def test_idle_run_reports_failed_tick_health(self) -> None:
        output, calls = self.run_status("/tmp/test-run\n", "failing: no instance", 1)
        self.assertIn("TICKS FAILING (no instance)", output)
        self.assertEqual(calls, "health unit-test-run\n")

    def test_healthy_run_has_no_tick_warning(self) -> None:
        output, calls = self.run_status("/tmp/test-run\n", "ok", 0)
        self.assertNotIn("TICKS FAILING", output)
        self.assertEqual(calls, "health unit-test-run\n")

    def test_missing_active_marker_skips_health(self) -> None:
        output, calls = self.run_status(None, "failing: no instance", 1)
        self.assertNotIn("TICKS FAILING", output)
        self.assertIsNone(calls)

    def test_empty_active_marker_skips_health(self) -> None:
        output, calls = self.run_status("", "failing: no instance", 1)
        self.assertNotIn("TICKS FAILING", output)
        self.assertIsNone(calls)

    def test_claude_session_id_comes_from_lookup_even_with_a_different_name(self) -> None:
        output, calls = self.run_status("/tmp/test-run\n", "ok", 0)
        self.assertNotIn("CLAUDE NOT RUNNING", output)
        self.assertEqual(calls, "health unit-test-run\n")

    def test_no_live_claude_record_reports_not_running(self) -> None:
        output, calls = self.run_status("/tmp/test-run\n", "ok", 0, stopped_units=("hook",))
        self.assertIn("CLAUDE NOT RUNNING", output)
        self.assertNotIn("CLAUDE STATE UNKNOWN", output)
        self.assertIsNone(calls)

    def test_unreadable_claude_records_report_unknown_instead_of_not_running(self) -> None:
        outputs, calls, _ = self.run_statuses(
            "/tmp/test-run\n", "ok", 0, panes=({"hook": "— holding: waiting on x\n"},),
            unreadable_units=("hook",),
        )
        self.assertIn("CLAUDE STATE UNKNOWN", outputs[0].splitlines())
        self.assertNotIn("CLAUDE NOT RUNNING", outputs[0])
        self.assertIsNone(calls)

    def test_gate_first_sight_prints_decision_and_records_seen_key(self) -> None:
        gate = "● Choose the release path\n\n— gate: approve the blue path\n"
        outputs, _, seen = self.run_statuses(None, "ok", 0, panes=({"hook": gate},))
        self.assertIn("=== DECISION for you from hook ===", outputs[0])
        self.assertIn("— gate: approve the blue path", outputs[0])
        self.assertIn("=== end hook ===", outputs[0])
        self.assertEqual(seen, "hook|— gate: approve the blue path\n")

    def test_repeated_gate_prints_still_waiting(self) -> None:
        gate = "● Choose the release path\n\n— gate: approve the blue path\n"
        outputs, _, _ = self.run_statuses(
            None, "ok", 0, panes=({"hook": gate}, {"hook": gate})
        )
        self.assertIn("=== DECISION for you from hook ===", outputs[0])
        self.assertIn("STILL WAITING on you, hook:— gate: approve the blue path", outputs[1])
        self.assertNotIn("=== DECISION", outputs[1])

    def test_a_pause_question_after_an_open_gate_leaves_the_gate_waiting(self) -> None:
        gate = ("● The showrunner has closed this run on its side.\n\n"
                "  The question above is still open: the repair-round count, next / drop /\n  elaborate.\n\n"
                "  — gate: add-on review, item 1 of 2\n\n✻ Worked for 1s · done 16:46\n")
        paused = gate + ("\n› Message from @conversation-pause: conversation-pause: the user has been quiet\n"
                         "here for 15 minutes. Ask them this, word for word, and nothing else: Return…\n"
                         "(ctrl+o to expand)\n\n● Return to automatic updates? (yes / no)\n\n"
                         "✻ Cooked for 9s · done 17:02\n")
        outputs, _, _ = self.run_statuses(None, "ok", 0, panes=({"hook": gate}, {"hook": paused}))
        self.assertIn("=== DECISION for you from hook ===", outputs[0])
        self.assertIn("STILL WAITING on you, hook:  — gate: add-on review, item 1 of 2", outputs[1])
        waiting = self.parse_status(outputs[1])[0].flags
        self.assertEqual(waiting, (StillWaiting("— gate: add-on review, item 1 of 2"),))

    def test_holding_after_gate_prints_no_user_wait(self) -> None:
        gate = "● Choose the release path\n\n— gate: approve the blue path\n"
        outputs, _, _ = self.run_statuses(
            None, "ok", 0,
            panes=({"hook": gate}, {"hook": "— holding: preparing the release\n"}),
        )
        self.assertNotIn("WAITING on you", outputs[1])
        self.assertNotIn("=== DECISION", outputs[1])
        self.assertNotIn("BLOCK in", outputs[1])

    def test_holding_text_that_mentions_gate_clears_user_wait(self) -> None:
        gate = "● Choose the release path\n\n— gate: approve the blue path\n"
        outputs, _, _ = self.run_statuses(
            None, "ok", 0,
            panes=({"hook": gate}, {"hook": "— holding: documenting gate: handling\n"}),
        )
        self.assertNotIn("WAITING on you", outputs[1])
        self.assertNotIn("=== DECISION", outputs[1])
        self.assertNotIn("BLOCK in", outputs[1])

    def test_showrunner_gate_text_that_mentions_blocked_prints_no_block(self) -> None:
        gate = "— gate: showrunner must review the blocked: wording\n"
        outputs, _, seen = self.run_statuses(None, "ok", 0, panes=({"hook": gate},))
        self.assertNotIn("WAITING on you", outputs[0])
        self.assertNotIn("=== DECISION", outputs[0])
        self.assertNotIn("BLOCK in", outputs[0])
        self.assertEqual(seen, "")

    def test_gate_naming_showrunner_stays_activity_without_user_wait(self) -> None:
        gate = "— gate: showrunner must merge the checkpoint\n"
        outputs, _, seen = self.run_statuses(None, "ok", 0, panes=({"hook": gate},))
        self.assertNotIn("WAITING on you", outputs[0])
        self.assertNotIn("=== DECISION", outputs[0])
        self.assertNotIn("BLOCK in", outputs[0])
        self.assertIn("gate: showrunner must merge the checkpoint", outputs[0])
        self.assertEqual(seen, "")

    def test_gate_naming_another_unit_stays_activity_without_user_wait(self) -> None:
        gate = "— gate: peer must publish its checkpoint\n"
        outputs, _, seen = self.run_statuses(
            None, "ok", 0,
            panes=({"hook": gate, "peer": "— holding: publishing\n"},),
            units=("hook", "peer"),
        )
        hook_output = outputs[0].partition("== peer")[0]
        self.assertNotIn("WAITING on you", hook_output)
        self.assertNotIn("=== DECISION", hook_output)
        self.assertNotIn("BLOCK in", hook_output)
        self.assertIn("gate: peer must publish its checkpoint", hook_output)
        self.assertEqual(seen, "")

    def test_peer_name_routes_gate_only_as_a_whole_word(self) -> None:
        outputs, _, _ = self.run_statuses(
            None, "ok", 0,
            panes=(
                {"writer": "— gate: approve the webhook wording\n"},
                {"writer": "— gate: hook must approve the wording\n"},
            ),
            units=("writer", "hook"),
        )
        writer_output = outputs[0].partition("== hook")[0]
        self.assertIn("=== DECISION for you from writer ===", writer_output)
        routed_writer_output = outputs[1].partition("== hook")[0]
        self.assertNotIn("WAITING on you", routed_writer_output)
        self.assertNotIn("=== DECISION", routed_writer_output)

    def test_showrunner_name_routes_gate_only_as_a_whole_word(self) -> None:
        gate = "— gate: approve the showrunnerish wording\n"
        outputs, _, _ = self.run_statuses(None, "ok", 0, panes=({"hook": gate},))
        self.assertIn("=== DECISION for you from hook ===", outputs[0])

    def test_real_gate_outputs_become_dailies_flags_or_activity(self) -> None:
        gate = "● Choose the release path\n\n— gate: approve the blue path\n"
        outputs, _, _ = self.run_statuses(
            None, "ok", 0,
            panes=(
                {"hook": gate},
                {"hook": gate},
                {"hook": "— holding: preparing the release\n"},
                {"hook": "— gate: showrunner must merge the checkpoint\n"},
            ),
        )
        first = self.parse_status(outputs[0])[0]
        repeated = self.parse_status(outputs[1])[0]
        holding = self.parse_status(outputs[2])[0]
        routed = self.parse_status(outputs[3])[0]
        self.assertTrue(any(isinstance(flag, Decision) for flag in first.flags))
        self.assertTrue(any(isinstance(flag, StillWaiting) for flag in repeated.flags))
        self.assertEqual(holding.flags, ())
        self.assertEqual(routed.flags, ())
        self.assertTrue(any("gate: showrunner" in line.text for line in routed.activity))
        peer_outputs, _, _ = self.run_statuses(
            None, "ok", 0,
            panes=({"hook": "— gate: peer must publish its checkpoint\n",
                    "peer": "— holding: publishing\n"},),
            units=("hook", "peer"),
        )
        peer_routed = self.parse_status(peer_outputs[0], ("hook", "peer"))[0]
        self.assertEqual(peer_routed.flags, ())
        self.assertTrue(any("gate: peer" in line.text for line in peer_routed.activity))

    def test_a_unit_with_no_marked_session_reads_gone_while_a_marked_one_lives(self) -> None:
        outputs, _, _ = self.run_statuses(None, "ok", 0, panes=({},), units=("hook", "hookworm"),
                                          missing_units=("hook",), stopped_units=("hookworm",))
        gone, live = self.parse_status(outputs[0], ("hook", "hookworm"))
        self.assertIsInstance(gone.state, SessionGone, outputs[0])
        self.assertIsInstance(live.state, ClaudeNotRunning, outputs[0])

    def test_herdr_units_report_activity_and_tick_health(self) -> None:
        outputs, calls, seen = self.run_statuses(
            "/tmp/native-run\n", "failing: no instance", 1,
            units=("hook", "native"),
            panes=({"hook": "● Check hook progress\nPhase ETA: 12:00 EDT\n— holding: running\n",
                    "native": "● Check herdr progress\nPhase ETA: 13:00 EDT\n— gate: approve release\n"},),
        )
        output = outputs[0]
        self.assertIn("== hook", output)
        self.assertIn("● Check hook progress", output)
        self.assertIn("== native", output)
        self.assertIn("● Check herdr progress", output)
        self.assertIn("Phase ETA: 13:00 EDT", output)
        self.assertIn("=== DECISION for you from native ===", output)
        self.assertNotIn("CLAUDE NOT RUNNING", output)
        self.assertNotIn("SESSION GONE", output)
        self.assertEqual(calls, "health unit-native-run\n" * 2)
        self.assertEqual(seen, "native|— gate: approve release\n")
