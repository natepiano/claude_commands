"""Run the complete verify entry point against slow fake Cargo and real git edits."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import cast, override

from scripts.direct.recorder_fixture import invoke_recorder
from scripts.direct.test_tree_watch import write_workspace
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, VERIFY, BOARD
from scripts.direct.wait_for_test_state import wait_until


CANCEL_LINE = ("verify.sh: cancelled — the tree changed while {cmd} ran, so this result "
               "would be stale. Resubmit the {cmd} when your edits are done.")

# Fake Cargo has a real, long-lived descendant. A cancellation that stops only
# the immediate cargo process leaves this child alive and fails the test.
CARGO = r'''
import json
import os
import subprocess
import sys
import time
from pathlib import Path

args = sys.argv[1:]
root = Path(os.environ["TEST_CONTROL"])
with (root / "events.jsonl").open("a") as events:
    events.write(json.dumps(args) + "\n")
if args[:1] == ["metadata"]:
    print(os.environ["TEST_METADATA"])
    raise SystemExit(0)
if "--version" in args:
    print("cargo fixture 1.0")
    raise SystemExit(0)
stage = ""
if args[:2] == ["nextest", "run"]:
    stage = "test"
elif args[:1] == ["clippy"]:
    stage = "fix" if "--fix" in args else "clippy"
elif args[:1] == ["mend"]:
    stage = "mend"
elif args[:2] == ["+nightly", "fmt"]:
    stage = "fmt"
elif args[:1] not in (["nextest"], ["test"]):
    raise SystemExit("unexpected cargo invocation: " + repr(args))
if stage in os.environ.get("TEST_REWRITE_STAGES", "").split(","):
    marker = root / (stage + ".rewritten")
    if not marker.exists():
        source = Path(os.environ["PACKAGE_ROOT"]) / "sample/src/lib.rs"
        with source.open("a") as output:
            output.write("// " + stage + " rewrote an input\n")
        marker.touch()
        # Stay in the rewrite stage across more than one watch interval.
        time.sleep(0.7)
if stage == os.environ.get("TEST_BLOCK_STAGE", "test"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (root / "descendant.pid").write_text(str(child.pid))
    (root / "running").touch()
    if os.environ.get("TEST_FINISH") == "1":
        from scripts.direct.wait_for_test_state import wait_for_file
        wait_for_file(root / "release")
        child.terminate()
        child.wait(timeout=10)
    else:
        time.sleep(60)
'''


MAC_RUNNER = r'''
import os
import signal
import subprocess
import sys
from pathlib import Path

root = Path(os.environ["TEST_CONTROL"])
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
(root / "descendant.pid").write_text(str(child.pid))

def cancel(signum, _frame):
    child.terminate()
    child.wait(timeout=10)
    (root / "mac-cancelled").write_text(str(signum))
    raise SystemExit(128 + signum)

signal.signal(signal.SIGTERM, cancel)
(root / "mac-attempt").touch()
(root / "running").touch()
signal.pause()
'''


class VerifyTreeCancelFixture(VerifyTokenWaitFixture):
    workspace: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    control: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    board: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        super().setUp()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        metadata = write_workspace(self.workspace)
        self.control = self.root / "control"
        self.control.mkdir()
        self.board = self.root / "board"
        stubs = self.root / "stubs"
        # All compilation tools stay private; only git is the real executable.
        git = shutil.which("git")
        assert git is not None
        (stubs / "git").unlink()
        (stubs / "git").symlink_to(git)
        self.write_tool(stubs / "cargo", CARGO)
        self.write_tool(stubs / "taplo", "pass\n")
        self.write_tool(stubs / "rustc", 'print("rustc fixture")\n')
        self.write_tool(stubs / "assertion-guard", "raise SystemExit(0)\n")
        # Do not inherit the token fixture's zero-duration polling sleep.
        sleep = shutil.which("sleep")
        assert sleep is not None
        (stubs / "sleep").unlink()
        (stubs / "sleep").symlink_to(sleep)
        self.environment.update({
            "PACKAGE_ROOT": str(self.workspace), "TEST_METADATA": json.dumps(metadata),
            "TEST_CONTROL": str(self.control), "VERIFY_TREE_WATCH_S": "0.2",
            "VERIFY_ASSERTION_GUARD_SCRIPT": str(stubs / "assertion-guard"),
            "UNIT_RUN_DIR": str(self.board), "UNIT_SESSION_DIR": str(self.board),
            "UNIT_TEAM_ROLE": "test", "CLAUDE_PYTHON": sys.executable,
            "PYTHONPATH": str(VERIFY.resolve().parents[2]), "BUILDLOG_SYNC": "1",
            "PYTHONDONTWRITEBYTECODE": "1", "NEXTEST_TEST_THREADS": "4",
        })

    @staticmethod
    def write_tool(path: Path, code: str) -> None:
        _ = path.write_text(f"#!{sys.executable}\n" + code)
        path.chmod(0o755)

    def start_verify(self, command: str, **extra: str) -> subprocess.Popen[str]:
        process = subprocess.Popen(["bash", str(VERIFY), command, "sample"],
                                   cwd=self.workspace, env={**self.environment, **extra},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        self.started_groups.add(process.pid)
        self.addCleanup(self.finish_process, process)
        return process

    @override
    def wait_for_running(self, path: Path, process: subprocess.Popen[str] | subprocess.Popen[bytes]) -> None:
        try:
            super().wait_for_running(path, process)
        except (AssertionError, TimeoutError):
            if process.poll() is not None:
                output, errors = process.communicate(timeout=10)
                self.fail(f"verify exited before {path.name}: {output!r} {errors!r}")
            raise

    def finish_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            self.end_group(process.pid)
        _ = process.communicate(timeout=30)

    def cargo_calls(self) -> list[list[str]]:
        return [cast(list[str], json.loads(line)) for line in
                (self.control / "events.jsonl").read_text().splitlines()]

    def edit_source(self) -> None:
        source = self.workspace / "sample/src/lib.rs"
        _ = source.write_text(source.read_text() + "// caller edited during cargo\n")

    def assert_resources_held(self) -> None:
        self.assertTrue((self.board / "locks/cargo.d").is_dir(), "test did not take the cargo token")
        self.assertTrue(list((self.root / "buildlog/admission").glob("*.reservation")),
                        "test did not take memory admission")

    def assert_resources_released(self) -> None:
        self.assertFalse((self.board / "locks/cargo.d").exists(), "cancel kept the token")
        self.assertEqual(list((self.board / "token_waiters").glob("*")), [], "cancel kept its queue place")
        admission = self.root / "buildlog/admission"
        reservations = [(path.name, path.read_text()) for path in admission.glob("*.reservation")]
        self.assertEqual(reservations, [], "cancel kept memory admission")
        self.assertEqual(list(admission.glob("*.waiting")), [], "cancel kept a memory queue place")
        self.assertEqual(list((self.root / "verify-steps").glob("*.json")), [], "cancel kept its active step")
        pid = int((self.control / "descendant.pid").read_text())
        state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                               capture_output=True, text=True, check=False, timeout=10).stdout.strip()
        self.assertTrue(not state or state.startswith("Z"), f"cargo descendant {pid} survived: {state}")

    def assert_no_cache_verdict(self) -> None:
        for suffix in ("*.pass", "*.fail"):
            self.assertEqual(list((self.board / "verify_cache").glob(suffix)), [],
                             "cancelled run recorded a cache verdict")


class VerifyTreeCancelTests(VerifyTreeCancelFixture):
    def assert_cancelled(self, process: subprocess.Popen[str], command: str) -> None:
        output, errors = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 77, output + errors)
        self.assertEqual((output + errors).splitlines().count(CANCEL_LINE.format(cmd=command)), 1,
                         output + errors)
        self.assert_no_cache_verdict()
        try:
            self.assert_resources_released()
        except AssertionError as failure:
            self.fail(f"{failure}\nverify output: {output}\nverify errors: {errors}")
        records = self.records()
        self.assertEqual(len(records), 1, records)
        self.assertEqual((records[0].get("outcome"), records[0].get("status")), ("cancelled", 77), records)

    def test_source_edit_cancels_once_releases_resources_and_records_no_verdict(self) -> None:
        process = self.start_verify("test")
        self.wait_for_running(self.control / "running", process)
        self.assert_resources_held()
        self.edit_source()
        self.assert_cancelled(process, "test")
        attempts = [args for args in self.cargo_calls() if args[:2] == ["nextest", "run"]]
        self.assertEqual(len(attempts), 1, "cancelled call restarted its tests")

    def test_lint_rewrites_finish_without_cancelling(self) -> None:
        process = self.start_verify("lint", TEST_BLOCK_STAGE="clippy", TEST_FINISH="1",
                                    TEST_REWRITE_STAGES="mend,fix,fmt")
        self.wait_for_running(self.control / "running", process)
        self.assert_resources_held()
        _ = (self.control / "release").touch()
        output, errors = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertNotIn("cancelled", output + errors)
        for stage in ("mend", "fix", "fmt"):
            self.assertTrue((self.control / (stage + ".rewritten")).exists(), f"{stage} never rewrote an input")
        self.assertEqual(len(list((self.board / "verify_cache").glob("*.pass"))), 1,
                         "lint rewrites prevented its final verdict from being recorded")
        self.assert_resources_released()

    def test_edit_during_clippy_judgment_cancels_lint(self) -> None:
        process = self.start_verify("lint", TEST_BLOCK_STAGE="clippy")
        self.wait_for_running(self.control / "running", process)
        self.assert_resources_held()
        self.edit_source()
        self.assert_cancelled(process, "lint")
        judgments = [args for args in self.cargo_calls() if args[:1] == ["clippy"] and "--fix" not in args]
        self.assertEqual(len(judgments), 1, "cancelled call restarted clippy")

    def test_tree_cancellation_closes_verification_progress_activity(self) -> None:
        environment = {**self.environment, "UNIT_HISTORY_DIR": str(self.root / "history"),
                       "UNIT_SYNC_WRITES": "0", "USER_ZONE": "UTC"}
        for args in (["start-run", "--working-dir", str(self.workspace),
                      "--main-family", "codex", "--main-model", "fixture-model"],
                     ["start-phase", "--phase-id", "2", "--phase-title", "Verify"]):
            result = invoke_recorder([*args, "--session-dir", str(self.board)], environment,
                                     self.workspace)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        process = self.start_verify("test", UNIT_HISTORY_DIR=environment["UNIT_HISTORY_DIR"],
                                    UNIT_SYNC_WRITES="0", USER_ZONE="UTC")
        self.wait_for_running(self.control / "running", process)
        state_file = self.board / "progress_history_state.json"
        active = cast(dict[str, object], json.loads(state_file.read_text()))
        activity = cast(dict[str, object], active["activity"])
        self.assertEqual(activity["status"], "active", "verify never opened an activity")
        self.assertIn("verify.sh test", str(activity["activity"]))
        self.edit_source()
        self.assert_cancelled(process, "test")
        closed = cast(dict[str, object], json.loads(state_file.read_text()))
        self.assertEqual(cast(dict[str, object], closed["activity"])["status"], "canceled")
        events = [cast(dict[str, object], json.loads(line)) for line in
                  Path(str(closed["history_file"])).read_text().splitlines()]
        finished = [event for event in events if event.get("event_type") == "activity_finished"]
        self.assertEqual(len(finished), 1, events)
        self.assertEqual(finished[0]["status"], "canceled")

    def test_cancel_during_reacquisition_removes_own_queue_entry_and_keeps_peer(self) -> None:
        memory = Path(self.environment["BUILDLOG_MEMINFO"])
        _ = memory.write_text("MemAvailable: 0 kB\n")
        admission = self.root / "buildlog/admission"
        admission.mkdir(parents=True)
        process = self.start_verify("test", BUILDLOG_MEM_POLL_S="1")
        lock = self.board / "locks/cargo.d"
        wait_until(lambda: (bool(list(admission.glob("*.waiting"))) and not lock.exists())
                   or process.poll() is not None, admission, timeout=60, processes=(process,))
        self.assertIsNone(process.poll(), "verify never entered memory admission")
        holder = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.started_groups.add(holder.pid)
        self.addCleanup(self.finish_process, holder)
        taken = subprocess.run(["bash", str(BOARD), "acquire", str(self.board), "peer", "cargo",
                                "--pid", str(holder.pid), "--hold", "3600", "--wait", "0"],
                               capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(taken.returncode, 0, taken.stdout + taken.stderr)
        waiters = self.board / "token_waiters"
        waiters.mkdir(exist_ok=True)
        _ = memory.write_text("MemAvailable: 50331648 kB\n")
        wait_until(lambda: bool(list(waiters.glob(f"*.{process.pid}.again")))
                   or process.poll() is not None, waiters, timeout=60, processes=(process,))
        own_entries = list(waiters.glob(f"*.{process.pid}.again"))
        self.assertEqual(len(own_entries), 1, "fixture never reached subshell reacquisition")
        # Keep a peer's entry present too: cancellation must remove only its own files.
        peer_entry = waiters / f"peer.0123456789abcdef.9999999999.{holder.pid}.again"
        peer_entry.touch()
        self.edit_source()
        output, errors = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 77, output + errors)
        self.assertEqual((output + errors).splitlines().count(CANCEL_LINE.format(cmd="test")), 1)
        self.assertEqual(sorted(waiters.iterdir()), [peer_entry], "cancel left its reacquisition entry")
        self.assertEqual((lock / "holder_pid").read_text(), str(holder.pid), "cancel released a peer's claim")
        self.assertIsNone(holder.poll(), "cancel killed its peer")
        self.assertEqual(list(admission.glob("*.reservation")), [])
        self.assertEqual(list(admission.glob("*.waiting")), [])
        self.assertEqual(list((self.root / "verify-steps").glob("*.json")), [])
        self.assert_no_cache_verdict()
        self.assertEqual([args for args in self.cargo_calls() if args[:2] == ["nextest", "run"]], [])
        records = self.records()
        self.assertEqual([(row.get("outcome"), row.get("status")) for row in records], [("cancelled", 77)])
        released = subprocess.run(["bash", str(BOARD), "release", str(self.board), "peer", "cargo",
                                   "--pid", str(holder.pid)], capture_output=True, text=True,
                                  check=False, timeout=10)
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)

    def mac_environment(self, *, local_first: bool = False) -> dict[str, str]:
        runner = self.root / "mac-runner.py"
        self.write_tool(runner, MAC_RUNNER)
        self.write_tool(self.root / "stubs/ssh", 'print("free_gib=1000\\nhost=test-mac\\nrc=0")\n')
        settings = self.root / "mac-settings.conf"
        _ = settings.write_text(f"offload=on\nlinux_only.{self.workspace.name}=\n")
        targets = self.root / "targets.conf"
        order = "natedev,mac" if local_first else "mac,natedev"
        _ = targets.write_text(f"targets={order}\nkind.mac=ssh\nplatform.mac=macos\n"
                               + f"settings.mac={settings.name}\nkind.natedev=local\nplatform.natedev=linux\n")
        return {"VERIFY_MAC_RUNNER": str(runner), "BUILD_TARGETS_CONFIG": str(targets),
                "MAC_TEST_CONFIG": str(settings)}

    def test_mac_offload_tree_edit_uses_runner_cancel_path_without_local_rerun(self) -> None:
        process = self.start_verify("test", **self.mac_environment())
        self.wait_for_running(self.control / "running", process)
        if not (self.control / "mac-attempt").exists():
            self.end_group(process.pid)
            output, errors = process.communicate(timeout=30)
            self.fail("verify never used the offload runner: " + output + errors)
        self.edit_source()
        self.assert_cancelled(process, "test")
        self.assertEqual((self.control / "mac-cancelled").read_text(), "15")
        self.assertEqual([args for args in self.cargo_calls() if args[:2] == ["nextest", "run"]], [],
                         "Mac cancellation fell back to a local test run")

    def test_local_memory_wait_to_mac_preserves_existing_watcher(self) -> None:
        environment = self.mac_environment(local_first=True)
        low_memory = self.root / "low-memory"
        _ = low_memory.write_text("MemAvailable: 1024 kB\n")
        free_now = self.root / "free-now.py"
        self.write_tool(free_now, "raise SystemExit(0)\n")
        process = self.start_verify("test", **environment, BUILDLOG_MEMINFO=str(low_memory),
                                    BUILDLOG_MEM_POLL_S="1", VERIFY_MAC_TEST_SCRIPT=str(free_now))
        self.wait_for_running(self.control / "running", process)
        self.assertTrue((self.control / "mac-attempt").exists(), "memory wait never transferred to the Mac")
        self.edit_source()
        self.assert_cancelled(process, "test")
        self.assertEqual((self.control / "mac-cancelled").read_text(), "15")
        self.assertEqual([args for args in self.cargo_calls() if args[:2] == ["nextest", "run"]], [],
                         "memory-blocked local step reached Cargo")

    def test_external_document_edit_does_not_cancel_test(self) -> None:
        process = self.start_verify("test", TEST_FINISH="1")
        self.wait_for_running(self.control / "running", process)
        _ = (self.workspace / "docs/design.md").write_text("new documentation\n")
        with self.assertRaises(subprocess.TimeoutExpired):
            _ = process.communicate(timeout=1)
        # A Cargo-owned handshake makes this a live span, not an already-ended run.
        _ = (self.control / "release").touch()
        output, errors = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertNotIn("cancelled", output + errors)
        self.assert_no_cache_verdict()
        self.assert_resources_released()


if __name__ == "__main__":
    _ = unittest.main()
