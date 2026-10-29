"""test implement launcher claims integration coverage."""

from __future__ import annotations

import unittest
from scripts.direct import implement_launcher_test_support as support
from scripts.direct.implement_launcher_test_support import STUB_MESH as STUB_MESH
from scripts.direct.implement_launcher_test_support import TEST_HANG_GUARD_SECS as TEST_HANG_GUARD_SECS
from typing import cast
import json
import os
import signal
import subprocess
from scripts.direct.wait_for_test_state import wait_for_pid
from scripts.direct.wait_for_test_state import wait_until


class ImplementLauncherWorkerTests(support.ImplementLauncherFixture):
    def test_follow_recovers_missing_claim_unless_the_roster_launcher_is_alive(self) -> None:
        for launcher_pid in (None, 99999999, os.getpid()):
            live = launcher_pid == os.getpid()
            with self.subTest(launcher_pid=launcher_pid):
                session_dir = self.start_phase(f"missing-claim-{launcher_pid}")
                first = self.mesh_launch(session_dir)
                self.assertEqual(first.returncode, 0, first.stderr)
                roster_path = session_dir / "mesh_roster.json"
                roster = cast(dict[str, dict[str, object]], json.loads(roster_path.read_text()))
                name = next(iter(roster))
                roster[name].update(status="paused", paused_from="running", paused_turn=True)
                if launcher_pid is not None:
                    roster[name]["launcher_pid"] = launcher_pid
                _ = roster_path.write_text(json.dumps(roster))
                status = session_dir / "impl_status_impl"
                _ = status.write_text("implementing\n")
                self.assertFalse((session_dir / "impl_claim_impl.json").exists())
                before = self.open_passes(session_dir)

                result = self.mesh_launch(session_dir, to=name)

                if live:
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(status.read_text(), "implementing\n")
                    self.assertEqual(self.open_passes(session_dir), before)
                    self.assertFalse((session_dir / "follow_message.txt").exists())
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual((session_dir / "status_before_claim.txt").read_text(), "error\n")
                    self.assertEqual(status.read_text(), "implemented\n")
                    followed = cast(dict[str, dict[str, object]], json.loads(roster_path.read_text()))
                    self.assertEqual(followed[name]["thread_id"], roster[name]["thread_id"])
                    self.assertTrue((session_dir / "follow_message.txt").exists())

    def test_identity_probes_default_to_thirty_one_second_waits_for_new_and_followed_workers(self) -> None:
        for follow in (False, True):
            with self.subTest(follow=follow):
                session = self.start_phase(f"default-probes-{follow}")
                arguments = ["bash", str(self.implement_script)]
                if follow:
                    initial = self.mesh_launch(session)
                    self.assertEqual(initial.returncode, 0, initial.stderr)
                    roster = cast("dict[str, object]", json.loads((session / "mesh_roster.json").read_text()))
                    arguments.extend(["--to", next(iter(roster))])
                arguments.extend([str(session), str(self.working_dir), str(self.prompt_file),
                                  "impl", "the retry path", "impl", "writing the retry path", "0", "impl"])
                binaries = session / "bin"
                binaries.mkdir()
                sleep = binaries / "sleep"
                _ = sleep.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$TEST_PROBES"\n')
                sleep.chmod(0o755)
                watcher = self.root / "scripts/agents/heartbeat_watch.sh"
                _ = watcher.write_text('#!/bin/bash\nif [[ -n "${TEST_PROBES_FINISHED:-}" ]]; then '
                                       + ': > "$TEST_PROBES_FINISHED"; fi\n')
                mesh = self.root / "scripts/agents/codex_mesh.py"
                _ = mesh.write_text(STUB_MESH.replace(
                    "verb = sys.argv[1]", "verb = sys.argv[1]\n"
                    + "if verb in ('start', 'follow'):\n"
                    + "    import subprocess\n"
                    + "    agent_pid = pathlib.Path(os.environ['TEST_AGENT_PID'])\n"
                    + "    agent_pid.with_name(agent_pid.name + '.tmp').write_text(str(os.getpid()))\n"
                    + "    os.replace(agent_pid.with_name(agent_pid.name + '.tmp'), agent_pid)\n"
                    + "    subprocess.run([sys.executable, os.environ['TEST_WAIT_HELPER'], os.environ['TEST_DEFAULT_RELEASE']], check=True)"
                ))
                probes = session / "probes"
                finished = session / "probes-finished"
                release = session / "release"
                pid_file = session / "agent-pid"
                environment = self.mesh_environment()
                for key in ("UNIT_WORKER_POLL_SECS", "UNIT_WORKER_WAIT_ATTEMPTS",
                            "UNIT_EARLY_POLL_SECS", "UNIT_TOKEN_CLOCK_FILE"):
                    _ = environment.pop(key, None)
                environment.update({"PATH": str(binaries) + os.pathsep + environment["PATH"],
                                    "TEST_PROBES": str(probes), "TEST_PROBES_FINISHED": str(finished),
                                    "TEST_AGENT_PID": str(pid_file), "TEST_DEFAULT_RELEASE": str(release)})
                process = subprocess.Popen(arguments, env=environment, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, start_new_session=True)
                try:
                    wait_until(lambda: finished.exists(), session, processes=(process,))
                    os.kill(wait_for_pid(pid_file, processes=(process,)), 0)
                    self.assertIsNone(process.poll())
                    self.assertEqual(probes.read_text().splitlines(), ["1"] * 30)
                    if follow:
                        claim = cast("dict[str, object]", json.loads((session / "impl_claim_impl.json").read_text()))
                        self.assertEqual(claim["turn_id"], "")
                    else:
                        self.assertFalse((session / "impl_worker_impl.json").exists())
                finally:
                    output, errors = self.finish_default_launcher(process, release, mesh)
                self.assertEqual(process.returncode, 0, output + errors)

    def test_killed_launcher_claim_recovers_before_and_after_turn_start(self) -> None:
        for stage in ("before", "after"):
            with self.subTest(stage=stage):
                session_dir = self.start_phase(f"claim-{stage}")
                first = self.mesh_launch(session_dir)
                self.assertEqual(first.returncode, 0, first.stderr)
                roster_path = session_dir / "mesh_roster.json"
                roster = cast("dict[str, dict[str, object]]", json.loads(
                    roster_path.read_text(encoding="utf-8")
                ))
                name = next(iter(roster))
                environment = self.mesh_environment()
                environment["STUB_FOLLOW_HOLD"] = stage
                process = subprocess.Popen(
                    ["bash", str(self.implement_script), "--to", name,
                     str(session_dir), str(self.working_dir), str(self.prompt_file),
                     "impl", "the retry path", "impl", "writing the retry path",
                     "0", "impl"],
                    env=environment, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True,
                )
                claim_path = session_dir / "impl_claim_impl.json"
                try:
                    def held_claim() -> bool:
                        try:
                            claim = cast("dict[str, object]", json.loads(
                                claim_path.read_text(encoding="utf-8")
                            ))
                            roster = cast("dict[str, dict[str, object]]", json.loads(
                                roster_path.read_text(encoding="utf-8")
                            ))
                        except (FileNotFoundError, json.JSONDecodeError):
                            return False
                        return (claim.get("turn_id") == "" if stage == "before"
                                else roster[name]["status"] == "running")

                    wait_until(held_claim, session_dir)
                finally:
                    os.killpg(process.pid, signal.SIGKILL)
                    _ = process.wait(timeout=TEST_HANG_GUARD_SECS)

                result = self.mesh_launch(session_dir, to=name)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    (session_dir / "impl_status_impl").read_text(encoding="utf-8"),
                    "implemented\n",
                )
                self.assertFalse((session_dir / "impl_claim_impl.json").exists())

    def test_live_claim_refuses_a_second_launcher_without_a_pass(self) -> None:
        session_dir = self.start_phase("claimed")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        _ = (session_dir / "impl_claim_impl.json").write_text(json.dumps({
            "pid": os.getpid(), "worker": name, "turn_id": "",
        }), encoding="utf-8")
        before = self.open_passes(session_dir)

        second = self.mesh_launch(session_dir, to=name)

        self.assertEqual(second.returncode, 2, second.stderr)
        self.assertEqual(self.open_passes(session_dir), before)
        self.assertEqual(
            (session_dir / "impl_claim_impl.json").read_text(encoding="utf-8"),
            json.dumps({"pid": os.getpid(), "worker": name, "turn_id": ""}),
        )

    def test_peer_turn_after_claim_closes_follow_up_as_error(self) -> None:
        session_dir = self.start_phase("peer-after-claim")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster = cast("dict[str, object]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        environment = self.mesh_environment()
        environment["STUB_FOLLOW_PEER_AFTER_CLAIM"] = "1"

        result = subprocess.run(
            ["bash", str(self.implement_script), "--to", name,
             str(session_dir), str(self.working_dir), str(self.prompt_file),
             "impl", "the retry path", "impl", "writing the retry path", "0", "impl"],
            env=environment, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual((session_dir / "impl_status_impl").read_text(encoding="utf-8"), "error\n")
        self.assertEqual(self.open_passes(session_dir)["impl"]["status"], "error")
        peer = cast("dict[str, dict[str, object]]", json.loads(
            (session_dir / "mesh_roster.json").read_text(encoding="utf-8")
        ))
        self.assertEqual(peer[name]["status"], "running")

    def test_unknown_and_busy_follow_ups_leave_pass_and_status_untouched(self) -> None:
        session_dir = self.start_phase("refused")
        first = self.mesh_launch(session_dir)
        self.assertEqual(first.returncode, 0, first.stderr)
        roster_path = session_dir / "mesh_roster.json"
        roster = cast("dict[str, dict[str, object]]", json.loads(
            roster_path.read_text(encoding="utf-8")
        ))
        name = next(iter(roster))
        status_file = session_dir / "impl_status_impl"
        _ = status_file.write_text("previous\n", encoding="utf-8")
        before = self.open_passes(session_dir)
        unknown = self.mesh_launch(session_dir, to="missing-worker")
        self.assertEqual(unknown.returncode, 2, unknown.stderr)
        self.assertEqual(status_file.read_text(encoding="utf-8"), "previous\n")
        self.assertEqual(self.open_passes(session_dir), before)

        roster[name]["status"] = "running"
        _ = roster_path.write_text(json.dumps(roster), encoding="utf-8")
        busy = self.mesh_launch(session_dir, to=name)
        self.assertEqual(busy.returncode, 2, busy.stderr)
        self.assertEqual(status_file.read_text(encoding="utf-8"), "previous\n")
        self.assertEqual(self.open_passes(session_dir), before)


if __name__ == "__main__":
    _ = unittest.main()
