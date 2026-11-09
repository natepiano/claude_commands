"""Verify startup gives nested and detached steps one live run identity."""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
from typing import TypedDict, cast
import unittest

from scripts.build_queue import admission_protocol as wire, admission_service as service
from scripts.build_queue.admission_test_support import AdmissionFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.process_group import send_process_group

VERIFY = Path(__file__).with_name("verify.sh")


class RunProbe(TypedDict):
    pid: int
    run: str
    name: str


class VerifyRunPlaceTests(unittest.TestCase):
    def script(self, fixture: AdmissionFixture) -> Path:
        """Execute startup and detachment without the command guard."""
        scripts = fixture.root / "scripts"
        direct = scripts / "direct"
        direct.mkdir(parents=True)
        (direct / "step_record.py").symlink_to(VERIFY.with_name("step_record.py"))
        for directory in ("agents", "build_targets", "build_hold"):
            (scripts / directory).symlink_to(VERIFY.parent.parent / directory, target_is_directory=True)
        library = scripts / "lib"
        library.mkdir()
        (library / "py").symlink_to(sys.executable)
        (library / "package_root.py").symlink_to(VERIFY.parent.parent / "lib/package_root.py")
        (library / "build_folders.py").symlink_to(VERIFY.parent.parent / "lib/build_folders.py")
        lint = scripts / "lint"
        lint.mkdir()
        _ = (lint / "invoke.sh").write_text("return 0\n")
        home_scripts = fixture.root / ".claude/scripts"
        home_scripts.parent.mkdir()
        home_scripts.symlink_to(scripts, target_is_directory=True)
        probe = direct / "probe.py"
        _ = probe.write_text('''import json, os, sys
print(json.dumps({"pid": int(sys.argv[1]), "run": os.environ.get("BUILDLOG_RUN", ""), "name": sys.argv[2]}), flush=True)
if not sys.stdin.readline():
    raise SystemExit("probe lost its controller")
''')
        prefix, command_marker, dispatch = VERIFY.read_text().partition('\nCMD="${1:-}"\n')
        self.assertTrue(command_marker, 'verify.sh startup marker is missing: CMD="${1:-}"')
        _, detach_marker, after_detach_marker = dispatch.partition('\nMESH_RUNS_SCRIPT=')
        self.assertTrue(detach_marker, "verify.sh startup marker is missing: MESH_RUNS_SCRIPT=")
        detach_and_identity, shift_marker, _ = after_detach_marker.partition('\nshift\n')
        self.assertTrue(shift_marker, "verify.sh startup marker is missing: shift after the run identity")
        script = direct / "verify.sh"
        _ = script.write_text(prefix + detach_marker + detach_and_identity + '''
"$PY" "${BASH_SOURCE[0]%/*}/probe.py" "$$" "$1"
if [[ "$1" == outer ]]; then
    bash "${BASH_SOURCE[0]}" inner
fi
:
''')
        return script

    def start(self, script: Path, name: str,
              **changes: str) -> subprocess.Popen[str]:
        environment = {**os.environ, "IN_NIX_SHELL": "fixture", "CODEX_THREAD_ID": "",
                       "UNIT_DETACHED_RUN": "", **changes}
        _ = environment.pop("PYTHONPATH", None)
        _ = environment.pop("BUILDLOG_RUN", None)
        process = subprocess.Popen(["bash", str(script), name], env=environment,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        self.addCleanup(self.stop, process)
        return process

    def stop(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            send_process_group(process.pid, signal.SIGKILL)
        _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)

    def probe(self, process: subprocess.Popen[str]) -> RunProbe:
        assert process.stdout is not None
        self.assertTrue(select.select([process.stdout], [], [], TEST_HANG_GUARD_SECS)[0],
                        "verify startup did not reach the probe")
        line = cast(str, process.stdout.readline())
        self.assertTrue(line, "verify startup exited before the probe")
        return cast(RunProbe, json.loads(line))

    def proceed(self, process: subprocess.Popen[str]) -> None:
        assert process.stdin is not None
        _ = process.stdin.write("continue\n")
        process.stdin.flush()

    def identity(self, fixture: AdmissionFixture, probe: RunProbe) -> wire.RunIdentity:
        owner = fixture.proc.owner_start(probe["pid"])
        assert isinstance(owner, service.LiveOwnerIdentity)
        expected = wire.RunIdentity(probe["pid"], owner.start_time)
        self.assertEqual(probe["run"], f"{expected.pid}:{expected.start_time}")
        return expected

    def test_nested_verify_keeps_the_outer_identity_and_queue_order(self) -> None:
        with AdmissionFixture() as fixture:
            process = self.start(self.script(fixture), "outer")
            outer = self.probe(process)
            run = self.identity(fixture, outer)
            first = fixture.request(run=run)
            _ = fixture.finish_step(first)
            fixture.free(0)
            fixture.clock.advance(1)
            peer = fixture.request()
            _ = fixture.server.handle_request(peer)
            self.proceed(process)
            inner = self.probe(process)
            self.assertNotEqual(inner["pid"], outer["pid"])
            self.assertEqual(inner["run"], outer["run"])
            fixture.clock.advance(1)
            second = fixture.request(run=run, need=9 * 1024 ** 3)
            _ = fixture.server.handle_request(second)
            self.assertEqual(fixture.queue_pids(), [second.owner.pid, peer.owner.pid])
            self.assertEqual(fixture.server.waiters[second.episode].row["since"], second.since)
            self.proceed(process)
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, 0, output + errors)

    def test_detached_run_keeps_its_place_after_the_launcher_exits(self) -> None:
        with AdmissionFixture() as fixture:
            script = self.script(fixture)
            launcher = script.with_name("detach.py")
            _ = launcher.write_text('''import os, subprocess, sys
from pathlib import Path
command = sys.argv[sys.argv.index("--") + 1:]
environment = {**os.environ, "UNIT_DETACHED_RUN": "1"}
with Path(os.environ["DETACHED_INPUT"]).open("r") as input_stream:
    with Path(os.environ["DETACHED_OUTPUT"]).open("w") as output:
        with Path(os.environ["DETACHED_ERROR"]).open("w") as errors:
            subprocess.Popen(command, env=environment, stdin=input_stream, stdout=output, stderr=errors,
                             start_new_session=True)
''')
            incoming = fixture.root / "detached.in"
            outgoing = fixture.root / "detached.out"
            os.mkfifo(incoming)
            os.mkfifo(outgoing)
            input_fd = os.open(incoming, os.O_RDWR | os.O_NONBLOCK)
            output_fd = os.open(outgoing, os.O_RDWR | os.O_NONBLOCK)
            self.addCleanup(os.close, input_fd)
            self.addCleanup(os.close, output_fd)
            process = self.start(script, "detached", CODEX_THREAD_ID="fixture",
                                 VERIFY_MESH_RUNS_SCRIPT=str(launcher), DETACHED_INPUT=str(incoming),
                                 DETACHED_OUTPUT=str(outgoing), DETACHED_ERROR=str(fixture.root / "detached.err"))
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, 0, output + errors)
            self.assertTrue(select.select([output_fd], [], [], TEST_HANG_GUARD_SECS)[0],
                            "detached verify did not reach the probe")
            probe = cast(RunProbe, json.loads(os.read(output_fd, 65536)))
            run = self.identity(fixture, probe)
            self.addCleanup(self.end_detached, run.pid)
            self.assertNotEqual(run.pid, process.pid)
            self.assertIsInstance(fixture.proc.owner_start(process.pid), service.OwnerProcessGone)
            first = fixture.request(run=run)
            _ = fixture.finish_step(first)
            fixture.free(0)
            fixture.clock.advance(1)
            peer = fixture.request()
            _ = fixture.server.handle_request(peer)
            fixture.clock.advance(1)
            second = fixture.request(run=run)
            _ = fixture.server.handle_request(second)
            self.assertEqual(fixture.queue_pids(), [second.owner.pid, peer.owner.pid])
            _ = os.write(input_fd, b"continue\n")

    def end_detached(self, pid: int) -> None:
        try:
            send_process_group(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_mac_startup_reads_no_proc_and_creates_no_run_identity(self) -> None:
        with AdmissionFixture() as fixture:
            script = self.script(fixture)
            shim = fixture.root / "scripts/lib/py"
            shim.unlink()
            _ = shim.write_text(f'''#!{sys.executable}
import os, sys
if sys.argv[2:3] == ["start"]:
    raise SystemExit("Mac startup requested a process start time")
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
''')
            shim.chmod(0o755)
            process = self.start(script, "mac", OSTYPE="darwin23")
            probe = self.probe(process)
            self.assertEqual(probe["run"], "")
            self.proceed(process)
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, 0, output + errors)
