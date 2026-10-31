"""test implement launcher integration coverage."""

from __future__ import annotations

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

import unittest
import contextlib
from scripts.direct import implement_launcher_test_support as support
from scripts.direct.implement_launcher_test_support import STUB_MESH as STUB_MESH
from typing import cast
import json
import os
import select
import signal
import subprocess
import sys
from scripts.direct.wait_for_test_state import wait_for_file
from scripts.direct.wait_for_test_state import wait_for_pid


class ImplementLauncherWorkerTests(support.ImplementLauncherFixture):
    def test_named_phase_reaches_worker_verification_environment(self) -> None:
        session = self.start_phase("phase-environment")
        _ = self.prompt_file.write_text("Phase 3 — Retry handling\nWrite the retry path.\n")
        stub = self.root / "scripts" / "agents" / "agent_exec.sh"
        with stub.open("a") as output:
            _ = output.write('printf "%s|%s" "$UNIT_RUN_DIR" "$UNIT_TEAM_ROLE" > "$UNIT_RUN_DIR/wait-context"\n')
        result = self.checked_launch(session, "--phase", "3")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((session / "wait-context").read_text(), f"{session}|impl")

    def test_a_codex_seat_on_the_mac_opens_only_after_its_phase_claims_its_files(self) -> None:
        # A Mac Codex seat runs no berth check of its own, so the launcher claims the phase first.
        session = self.start_phase("claim-before-seat")
        initial = self.mesh_launch(session, slot="mac")
        self.assertEqual(initial.returncode, 0, initial.stderr)
        seat = next(iter(cast("dict[str, object]", json.loads((session / "mesh_roster.json").read_text()))))
        environment, calls = self.claim_fixture(session, "clear")
        result = subprocess.run(
            ["bash", str(self.implement_script), "--to", seat, "--phase", "3", str(session), str(self.working_dir),
             str(self.prompt_file), "impl", "the retry path", "impl", "writing the retry path", "0", "mac"],
            check=False, capture_output=True, text=True, env=environment, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.read_text(encoding="utf-8").splitlines(),
                         ["sid-director.phase-3 check reviewed.txt --json"])
        state = (session / "phases" / "3" / "delegated_phase_reservation_state.json").read_text(encoding="utf-8")
        self.assertIn('"r1"', state)
        events = (session / "mesh_events.txt").read_text(encoding="utf-8").splitlines()
        self.assertIn("follow", events[events.index("claim"):], events)

    def test_a_dispatch_names_its_director_in_the_run_folder(self) -> None:
        # The production tree reads the director here: its one marker names only one of the runs
        # it has open side by side, and a run opened by hand has none.
        session_dir = self.start_phase("director")
        environment = self.environment()
        environment["CLAUDE_CODE_SESSION_ID"] = "sid-director"
        result = subprocess.run(
            ["bash", str(self.implement_script), str(session_dir), str(self.working_dir),
             str(self.prompt_file), "impl", "the retry path", "impl", "writing the retry path", "0", "impl"],
            check=False, capture_output=True, text=True, env=environment, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((session_dir / "director").read_text(encoding="utf-8"), "sid-director\n")

    def test_a_dispatch_with_no_pass_kind_is_refused(self) -> None:
        # What the incident actually looked like on the wire: a worker launched
        # with the sixth argument dropped. It used to run the agent and record
        # nothing; now nothing runs at all.
        session_dir = self.start_phase("dropped")
        result = subprocess.run(
            [
                "bash", str(self.implement_script), str(session_dir), str(self.working_dir),
                str(self.prompt_file), "test", "the retry tests", "",
                "writing the retry tests", "0", "test",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
            timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("pass_kind must be impl, test, fix, or review", result.stderr)
        # Refused before anything ran: no status file to be read as a live worker,
        # and no register line for peers to answer.
        self.assertFalse((session_dir / "impl_status_test").exists())
        self.assertFalse((session_dir / "board.log").exists())

    def test_a_launch_naming_its_phase_writes_in_that_phase_directory(self) -> None:
        # Two phases running the same role each name their own, so neither overwrites the
        # other's status, summary or log, and the name stays off the worker's address.
        session_dir = self.start_phase("phase-directory")
        result = subprocess.run(
            ["bash", str(self.implement_script), "--phase", "3", str(session_dir), str(self.working_dir),
             str(self.prompt_file), "impl", "the retry path", "impl", "writing the retry path", "0", "impl"],
            check=False, capture_output=True, text=True, env=self.environment(), timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        phase_dir = session_dir / "phases" / "3"
        self.assertEqual((phase_dir / "impl_status_impl").read_text(encoding="utf-8").strip(), "implemented")
        self.assertTrue((phase_dir / "impl_agent_impl.log").is_file())
        self.assertEqual(sorted(path.name for path in session_dir.glob("impl_status*")), [])
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertNotIn("3-impl", board)

    def test_a_refused_claim_keeps_the_phases_codex_seat_closed(self) -> None:
        session = self.start_phase("claim-refused")
        initial = self.mesh_launch(session, slot="mac")
        self.assertEqual(initial.returncode, 0, initial.stderr)
        seat = next(iter(cast("dict[str, object]", json.loads((session / "mesh_roster.json").read_text()))))
        environment, _ = self.claim_fixture(session, "blocked")
        result = subprocess.run(
            ["bash", str(self.implement_script), "--to", seat, "--phase", "3", str(session), str(self.working_dir),
             str(self.prompt_file), "impl", "the retry path", "impl", "writing the retry path", "0", "mac"],
            check=False, capture_output=True, text=True, env=environment, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("phase 3 could not claim its files", result.stderr)
        events = (session / "mesh_events.txt").read_text(encoding="utf-8").splitlines()
        self.assertNotIn("follow", events[events.index("claim"):], events)

    def test_an_unknown_pass_kind_is_refused_by_the_same_check(self) -> None:
        session_dir = self.start_phase("unknown")
        result = self.launch(session_dir, "implementation", "impl")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("got 'implementation'", result.stderr)

    def test_an_unreadable_ledger_opens_the_seat_with_the_phase_still_awaiting_first_touch(self) -> None:
        # Ledger loss fails open for edits, so a scratch clone with no ledger still opens its seat.
        session = self.start_phase("claim-unreadable")
        initial = self.mesh_launch(session, slot="mac")
        self.assertEqual(initial.returncode, 0, initial.stderr)
        seat = next(iter(cast("dict[str, object]", json.loads((session / "mesh_roster.json").read_text()))))
        environment, _ = self.claim_fixture(session, "ledger_unreadable")
        result = subprocess.run(
            ["bash", str(self.implement_script), "--to", seat, "--phase", "3", str(session), str(self.working_dir),
             str(self.prompt_file), "impl", "the retry path", "impl", "writing the retry path", "0", "mac"],
            check=False, capture_output=True, text=True, env=environment, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = (session / "phases" / "3" / "delegated_phase_reservation_state.json").read_text(encoding="utf-8")
        self.assertIn("enrolled_awaiting_first_touch", state)
        events = (session / "mesh_events.txt").read_text(encoding="utf-8").splitlines()
        self.assertIn("follow", events[events.index("claim"):], events)

    def test_default_launcher_timeout_reaps_its_group_and_restores_the_mesh(self) -> None:
        ready = self.root / "blocked-launcher-child"
        child_code = ("import os, signal, sys; from pathlib import Path; "
                      "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                      "p = Path(sys.argv[1]); staged = p.with_name(p.name + '.tmp'); "
                      "staged.write_text(str(os.getpid())); os.replace(staged, p); signal.pause()")
        program = f"import subprocess, sys; child = subprocess.Popen([sys.executable, '-c', {child_code!r}, {str(ready)!r}]); child.wait()"
        process = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        mesh = self.root / "scripts/agents/codex_mesh.py"
        _ = mesh.write_text("blocked fixture")
        try:
            descriptor = os.pidfd_open(wait_for_pid(ready, processes=(process,)))
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    _ = self.finish_default_launcher(process, self.root / "release", mesh, timeout=0)
                self.assertIsNotNone(process.poll())
                self.assertEqual(select.select([descriptor], [], [], TEST_HANG_GUARD_SECS)[0], [descriptor])
                self.assertEqual(mesh.read_text(), STUB_MESH)
                self.assertTrue(process.stdout is not None and process.stdout.closed)
                self.assertTrue(process.stderr is not None and process.stderr.closed)
            finally:
                os.close(descriptor)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _ = process.communicate()
            _ = mesh.write_text(STUB_MESH)

    def test_failed_worker_with_live_turn_refuses_before_recording_a_pass(self) -> None:
        session_dir = self.start_phase("failed-live-turn")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster_path = session_dir / "mesh_roster.json"
        roster = cast("dict[str, dict[str, object]]", json.loads(roster_path.read_text()))
        name = next(iter(roster))
        roster[name]["status"] = "failed"
        roster[name]["live_turn"] = True
        _ = roster_path.write_text(json.dumps(roster))
        status_path = session_dir / "impl_status_impl"
        before = status_path.read_text(encoding="utf-8")
        passes = self.open_passes(session_dir)

        refused = self.mesh_launch(session_dir, to=name)

        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertEqual(status_path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.open_passes(session_dir), passes)

    def test_launcher_passes_its_prompt_to_the_launch_check(self) -> None:
        session = self.start_phase("checked-launch")
        _ = self.prompt_file.write_text("Phase 3 — Retry handling\nWrite the retry.\n")
        result = self.checked_launch(session, "--phase", "3")
        self.assertEqual(result.returncode, 0, result.stderr)
        started, = self.launch_records(session, "pass_started")
        check = cast(dict[str, object], started["launch_check"])
        self.assertEqual((check["phase_id"], check["phase_title"], check["prompt_phase_line"]),
                         ("3", "Retry handling", "matched"))
        self.assertTrue((session / "worker-started").exists())

    def test_launcher_stops_on_stale_prompt_and_missing_phase_refusals(self) -> None:
        for label, options, text in (
            ("stale-prompt", ("--phase", "3"), "Phase 9 — Other work\n"),
            ("missing-phase", (), "Write the retry.\n"),
        ):
            with self.subTest(label=label):
                session = self.start_phase(f"refused-{label}")
                _ = self.prompt_file.write_text(text)
                result = self.checked_launch(session, *options)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((session / "worker-started").exists())
                self.assertEqual(self.launch_records(session, "pass_started"), [])
                refusal, = self.launch_records(session, "launch_refused")
                self.assertEqual(refusal["prompt_file"], str(self.prompt_file))
                self.assertTrue(refusal["reason"])
                self.assertIn("prompt" if label == "stale-prompt" else "phase", result.stderr.lower())

    def test_new_launch_keeps_heartbeat_watcher_output(self) -> None:
        watcher = self.root / "scripts" / "agents" / "heartbeat_watch.sh"
        _ = watcher.write_text(
            "#!/usr/bin/env bash\nprintf 'heartbeat watcher output\\n'\n: > \"$TEST_HEARTBEAT_READY\"\n",
            encoding="utf-8",
        )
        agent = self.root / "scripts" / "agents" / "agent_exec.sh"
        _ = agent.write_text(
            agent.read_text(encoding="utf-8").replace(
                "set -euo pipefail", 'set -euo pipefail\npython3 "$TEST_WAIT_HELPER" "$TEST_HEARTBEAT_READY"'
            ), encoding="utf-8",
        )
        session_dir = self.start_phase("heartbeat-visible")

        result = self.launch(session_dir, "impl", "impl")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("heartbeat watcher output", result.stdout)

    def test_start_and_follow_write_the_current_kind_as_the_roster_role(self) -> None:
        session_dir = self.start_phase("roster-role")
        first = self.mesh_launch(session_dir, kind="impl")
        self.assertEqual(first.returncode, 0, first.stderr)
        roster_path = session_dir / "mesh_roster.json"
        roster = cast("dict[str, dict[str, object]]", json.loads(
            roster_path.read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        self.assertEqual(roster[name]["role"], "impl")
        self.assertEqual(roster[name]["task"], "direct.impl")

        followed = self.mesh_launch(
            session_dir, to=name, kind="review", subtask="impl"
        )

        self.assertEqual(followed.returncode, 0, followed.stderr)
        roster = cast("dict[str, dict[str, object]]", json.loads(
            roster_path.read_text(encoding="utf-8")
        ))
        self.assertEqual(roster[name]["role"], "review")
        self.assertEqual(roster[name]["task"], "direct.impl")

    def test_worker_and_turn_identity_are_recorded_before_the_heartbeat_watcher_starts(self) -> None:
        watcher = self.root / "scripts" / "agents" / "heartbeat_watch.sh"
        original_watcher = watcher.read_text()
        launch_directory = self.root / "launcher-cwd"
        launch_directory.mkdir()
        for follow in (False, True):
            with self.subTest(follow=follow):
                session = self.start_phase(f"identity-before-heartbeat-{follow}")
                arguments = ["bash", str(self.implement_script)]
                if follow:
                    with contextlib.chdir(launch_directory):
                        initial = self.mesh_launch(session)
                    self.assertEqual(initial.returncode, 0, initial.stderr)
                    roster = cast("dict[str, object]", json.loads((session / "mesh_roster.json").read_text()))
                    arguments.extend(["--to", next(iter(roster))])
                arguments.extend([str(session), str(self.working_dir), str(self.prompt_file),
                                  "impl", "the retry path", "impl", "writing the retry path", "0", "impl"])
                witness = session / "watcher-identity"
                source = session / ("impl_claim_impl.json" if follow else "impl_worker_impl.json")
                watcher = self.root / "scripts" / "agents" / "heartbeat_watch.sh"
                _ = watcher.write_text(
                    '#!/bin/bash\nif [ -f "$TEST_IDENTITY_SOURCE" ]; then\n'
                    + 'cat "$TEST_IDENTITY_SOURCE" > "$TEST_IDENTITY_WITNESS.tmp"\n'
                    + 'else printf "{}\\n" > "$TEST_IDENTITY_WITNESS.tmp"; fi\n'
                    + 'mv "$TEST_IDENTITY_WITNESS.tmp" "$TEST_IDENTITY_WITNESS"\n'
                )
                release = session / "launch-release"
                environment = self.mesh_environment()
                environment.update({"TEST_IDENTITY_SOURCE": str(source), "TEST_IDENTITY_WITNESS": str(witness),
                                    "STUB_LAUNCH_RELEASE": str(release)})
                if follow:
                    environment.update({"STUB_FOLLOW_HOLD": "after", "STUB_LAUNCH_RELEASE": str(release)})
                process = subprocess.Popen(arguments, env=environment, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, start_new_session=True, cwd=launch_directory)
                try:
                    wait_for_file(witness)
                    identity = cast("dict[str, object]", json.loads(witness.read_text()))
                    self.assertIsNone(process.poll())
                    if follow:
                        self.assertEqual(identity.get("turn_id"), "turn-2")
                        self.assertEqual(identity.get("pid"), process.pid)
                    else:
                        self.assertTrue(identity.get("worker_id"), identity)
                finally:
                    release.touch()
                    output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    _ = watcher.write_text(original_watcher)
                self.assertEqual(process.returncode, 0, output + errors)
                self.assertEqual(list(launch_directory.iterdir()), [], "heartbeat watcher leaked a relative temporary file")

    def test_a_refused_compaction_roster_write_stops_before_claiming_or_sending_follow_up(self) -> None:
        session_dir = self.start_phase("compact-roster-changed")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        self.set_mesh_context(session_dir, name, 126299)
        original = (session_dir / "mesh_roster.json").read_bytes()
        (session_dir / "compact_roster_changed").touch()

        result = self.mesh_launch(session_dir, to=name)

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("roster changed during compaction", result.stderr)
        self.assertEqual((session_dir / "mesh_events.txt").read_text().splitlines(), ["compact"])
        self.assertEqual((session_dir / "mesh_roster.json").read_bytes(), original)
        self.assertFalse((session_dir / "follow_message.txt").exists())
        board = (session_dir / "board.log").read_text(encoding="utf-8")
        self.assertIn("follow-up not sent", board)
        self.assertNotIn("continuing follow-up", board)


if __name__ == "__main__":
    _ = unittest.main()
