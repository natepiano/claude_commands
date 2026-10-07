#!/usr/bin/env python3
"""Tests for unit status tick health with an isolated active run."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import NamedTuple

from dailies_input import (ClaudeNotRunning, Decision, SessionGone, StatusBlock, StillWaiting, UnitRow,
                           status_blocks)


SCRIPT = Path(__file__).with_name("unit_status.sh")
SESSION_ID = "test-session"


class RealTmux(NamedTuple):
    """A real tmux binary and the private socket directory its test server lives in."""

    binary: str
    server_dir: Path

    def environment(self, base: dict[str, str]) -> dict[str, str]:
        environment = {**base, "TMUX_TMPDIR": str(self.server_dir)}
        _ = environment.pop("TMUX", None)
        return environment


def _write_executable(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


class UnitStatusTests(unittest.TestCase):
    def run_statuses(
        self, marker: str | None, health_text: str, health_exit: int, *,
        panes: tuple[dict[str, str], ...],
        processes: str = "100 1 tmux pane\n200 100 zsh\n12345 200 claude --remote-control stalls\n",
        showrunner: str | None = None,
        units: tuple[str, ...] = ("hook",),
        real_tmux: RealTmux | None = None,
    ) -> tuple[tuple[str, ...], str | None, str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "scripts" / "production" / "unit_status.sh"
            script.parent.mkdir(parents=True)
            _ = shutil.copy2(SCRIPT, script)
            _write_executable(
                root / "scripts" / "lib" / "py",
                '#!/bin/sh\nexec python3 "$@"\n',
            )
            _ = (root / "scripts" / "message").mkdir(parents=True)
            _ = (root / "scripts" / "message" / "sessions.py").write_text(
                f'import sys\nprint("{SESSION_ID}" if sys.argv[-1] == "12345" else "")\n', encoding="utf-8"
            )
            _write_executable(
                root / "scripts" / "message" / "notifier.sh",
                """print -r -- "$*" >> "$NOTIFIER_CALL_LOG"
print -r -- "$NOTIFIER_HEALTH_TEXT"
exit "$NOTIFIER_HEALTH_EXIT"
""",
            )
            _ = (root / "scripts" / "hooks").mkdir(parents=True)
            _ = (root / "scripts" / "hooks" / "delegate_run.py").write_text(
                "raise SystemExit(1)\n", encoding="utf-8"
            )
            bin_dir = root / "bin"
            process_file = root / "processes.txt"
            _ = process_file.write_text(processes, encoding="utf-8")
            _write_executable(
                bin_dir / "tmux",
                """#!/bin/sh
case "$1" in
  has-session) exit 0 ;;
  display-message) printf '100\\n' ;;
  capture-pane)
    for target in "$@"; do :; done
    target=${target#=}
    cat "$TEST_PANE_DIR/${target%:}"
    ;;
  *) exit 2 ;;
esac
""" if real_tmux is None else f'#!/bin/sh\nexec {real_tmux.binary} "$@"\n',
            )
            _write_executable(bin_dir / "ps", '#!/bin/sh\ncat "$TEST_PROCESS_FILE"\n')
            _write_executable(bin_dir / "pgrep", "#!/bin/sh\nexit 2\n")
            pane_dir = root / "panes"
            pane_dir.mkdir()
            active_dir = root / "active"
            active_dir.mkdir()
            if marker is not None:
                _ = (active_dir / SESSION_ID).write_text(marker, encoding="utf-8")
            call_log = root / "notifier_calls"
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(root),
                    "PATH": f"{bin_dir}:{environment.get('PATH', '')}",
                    "PLAN_DELEGATE_ACTIVE_DIR": str(active_dir),
                    "NOTIFIER_STATE_DIR": str(root / "notifier"),
                    "NOTIFIER_NOW_EPOCH": "20100",
                    "NOTIFIER_CALL_LOG": str(call_log),
                    "NOTIFIER_HEALTH_TEXT": health_text,
                    "NOTIFIER_HEALTH_EXIT": str(health_exit),
                    "TEST_PROCESS_FILE": str(process_file),
                    "TEST_PANE_DIR": str(pane_dir),
                }
            )
            if real_tmux is not None:
                environment = real_tmux.environment(environment)
            config = root / "config" / "showrunners.json"
            config.parent.mkdir()
            _ = config.write_text('{"threshold_percent":2,"repeat_minutes":30,"stall_minutes":5,'
                                  + '"faults_to":"natedev","always":[],"showrunners":'
                                  + '[{"session":"director","zone":"America/Los_Angeles",'
                                  + '"units":["hook"]}]}')
            _ = shutil.copy2(SCRIPT.with_name("showrunners.py"), script.with_name("showrunners.py"))
            environment["SHOWRUNNERS_CONFIG"] = str(config)
            zsh = shutil.which("zsh")
            if zsh is None:
                raise RuntimeError("zsh is required for unit status tests")
            outputs: list[str] = []
            for pane_by_unit in panes:
                for unit in units:
                    _ = (pane_dir / unit).write_text(
                        pane_by_unit.get(unit, "— holding: waiting on x\n"), encoding="utf-8"
                    )
                result = subprocess.run(
                    [zsh, str(script), str(root / "status"), "America/Los_Angeles",
                     *(["--showrunner", showrunner] if showrunner else units)],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=environment,
                )
                outputs.append(result.stdout)
            calls = call_log.read_text(encoding="utf-8") if call_log.exists() else None
            seen_path = root / "status" / "decisions_seen"
            seen = seen_path.read_text(encoding="utf-8") if seen_path.exists() else ""
            return tuple(outputs), calls, seen

    def run_status(
        self, marker: str | None, health_text: str, health_exit: int, *,
        processes: str = "100 1 tmux pane\n200 100 zsh\n12345 200 claude --remote-control stalls\n",
        showrunner: str | None = None,
    ) -> tuple[str, str | None]:
        outputs, calls, _ = self.run_statuses(
            marker, health_text, health_exit,
            panes=({"hook": "— holding: waiting on x\n"},),
            processes=processes,
            showrunner=showrunner,
        )
        return outputs[0], calls

    def parse_status(self, output: str, units: tuple[str, ...] = ("hook",)) -> tuple[StatusBlock, ...]:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "status.txt"
            _ = path.write_text(output, encoding="utf-8")
            return status_blocks(path, tuple(UnitRow(unit, unit) for unit in units))

    def test_showrunner_form_reads_current_unit_names_from_config(self) -> None:
        output, _ = self.run_status(None, "ok", 0, showrunner="director")
        self.assertIn("== hook", output)

    def test_idle_run_reports_failed_tick_health(self) -> None:
        output, calls = self.run_status("/tmp/test-run\n", "failing: no instance", 1)
        self.assertIn("TICKS FAILING (no instance)", output)
        self.assertEqual(calls, "health delegate-test-run\n")

    def test_healthy_run_has_no_tick_warning(self) -> None:
        output, calls = self.run_status("/tmp/test-run\n", "ok", 0)
        self.assertNotIn("TICKS FAILING", output)
        self.assertEqual(calls, "health delegate-test-run\n")

    def test_missing_active_marker_skips_health(self) -> None:
        output, calls = self.run_status(None, "failing: no instance", 1)
        self.assertNotIn("TICKS FAILING", output)
        self.assertIsNone(calls)

    def test_empty_active_marker_skips_health(self) -> None:
        output, calls = self.run_status("", "failing: no instance", 1)
        self.assertNotIn("TICKS FAILING", output)
        self.assertIsNone(calls)

    def test_claude_is_found_under_the_pane_even_with_a_different_remote_name(self) -> None:
        output, calls = self.run_status("/tmp/test-run\n", "ok", 0)
        self.assertNotIn("CLAUDE NOT RUNNING", output)
        self.assertEqual(calls, "health delegate-test-run\n")

    def test_no_claude_descendant_reports_not_running(self) -> None:
        processes = "100 1 tmux pane\n200 100 zsh\n12345 1 claude --remote-control stalls\n"
        output, calls = self.run_status("/tmp/test-run\n", "ok", 0, processes=processes)
        self.assertIn("CLAUDE NOT RUNNING", output)
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

    def test_a_gone_unit_reads_gone_while_a_session_its_name_prefixes_lives(self) -> None:
        binary = shutil.which("tmux")
        if binary is None:
            self.skipTest("tmux is required to check its session matching")
        with tempfile.TemporaryDirectory() as server_dir:
            server = RealTmux(binary, Path(server_dir))
            _ = subprocess.run([binary, "-f", "/dev/null", "new-session", "-d", "-s", "hookworm", "sleep 60"],
                               env=server.environment(dict(os.environ)), check=True)
            try:
                outputs, _, _ = self.run_statuses(None, "ok", 0, panes=({},), units=("hook", "hookworm"),
                                                  real_tmux=server)
            finally:
                _ = subprocess.run([binary, "kill-server"], env=server.environment(dict(os.environ)), capture_output=True,
                                   check=False)
        gone, live = self.parse_status(outputs[0], ("hook", "hookworm"))
        self.assertIsInstance(gone.state, SessionGone, outputs[0])
        self.assertIsInstance(live.state, ClaudeNotRunning, outputs[0])
