"""Private workspace seeds and real process trees for verify cancellation tests."""
from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast, override

from scripts.direct.test_tree_watch import write_workspace
from scripts.direct.verify_token_ownership_test_support import VerifyTokenOwnershipFixture
from scripts.direct.verify_token_wait_test_support import VERIFY
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS



CANCEL_LINE = ("verify.sh: cancelled — the tree changed while {cmd} ran, so this result "
               + "would be stale. Resubmit the {cmd} when your edits are done.")

# Keep a real long-lived descendant: stopping only Cargo must fail cleanup.
CARGO = r'''
import json
import os
import subprocess
import sys
import signal
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
        # The parent observes each rewrite before allowing the next stage.
        from scripts.direct.wait_for_test_state import wait_for_file
        wait_for_file(root / (stage + ".release"))
if stage == os.environ.get("TEST_BLOCK_STAGE", "test"):
    child = subprocess.Popen([sys.executable, "-c", "import os, signal; signal.alarm(int(os.environ[\"TEST_HANG_GUARD_SECS\"])); signal.pause()"])
    (root / "descendant.pid").write_text(str(child.pid))
    (root / "running").touch()
    if os.environ.get("TEST_FINISH") == "1":
        from scripts.direct.wait_for_test_state import wait_for_file
        wait_for_file(root / "release")
        child.terminate()
        child.wait(timeout=int(os.environ["TEST_HANG_GUARD_SECS"]))
    else:
        signal.alarm(int(os.environ["TEST_HANG_GUARD_SECS"]))
        signal.pause()
'''

MAC_RUNNER = r'''
import os
import signal
import subprocess
import sys
from pathlib import Path

root = Path(os.environ["TEST_CONTROL"])
child = subprocess.Popen([sys.executable, "-c", "import os, signal; signal.alarm(int(os.environ[\"TEST_HANG_GUARD_SECS\"])); signal.pause()"])
(root / "descendant.pid").write_text(str(child.pid))

def cancel(signum, _frame):
    child.terminate()
    child.wait(timeout=int(os.environ["TEST_HANG_GUARD_SECS"]))
    (root / "mac-cancelled").write_text(str(signum))
    raise SystemExit(128 + signum)

signal.signal(signal.SIGTERM, cancel)
(root / "mac-attempt").touch()
(root / "running").touch()
signal.pause()
'''


class VerifyTreeCancelFixture(VerifyTokenOwnershipFixture):
    workspace: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    control: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    board: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def build_fixture(self) -> None:
        super().build_fixture()
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
        self.write_shell_tool(stubs / "taplo", "exit 0\n")
        self.write_shell_tool(stubs / "rustc", "printf 'rustc fixture\\n'\n")
        self.write_tool(stubs / "assertion-guard", """
class GuardError(Exception):
    pass

def default_base(repo, explicit_base=""):
    return "fixture-base"

def git(repo, *arguments):
    return "fixture-head" if arguments[0] == "rev-parse" else ""

def read_rulings(sources, messages):
    return {}

if __name__ == "__main__":
    raise SystemExit(0)
""")
        # Keep real heartbeat/board sleeps; only controlled memory transitions
        # use already-published routing readiness or the meminfo change event.
        sleep = shutil.which("sleep")
        assert sleep is not None
        (stubs / "sleep").unlink()
        self.write_shell_tool(stubs / "sleep",
            'if [ "$1" = "${BUILDLOG_MEM_POLL_S:-5}" ]; then\n'
            + '    if [ "${TEST_MEMORY_CHANGE_EVENT:-0}" = 1 ]; then exec python3 "$TEST_MEMORY_EVENT"; fi\n'
            + '    if [ -n "${TEST_ROUTING_READY_EVENT:-}" ]; then exec python3 "$TEST_WAIT_HELPER" "$TEST_ROUTING_READY_EVENT"; fi\n'
            + 'fi\nexec ' + shlex.quote(sleep) + ' "$@"\n')
        self.environment.update({
            "TEST_HANG_GUARD_SECS": str(TEST_HANG_GUARD_SECS),
            "PACKAGE_ROOT": str(self.workspace), "TEST_METADATA": json.dumps(metadata),
            "TEST_CONTROL": str(self.control), "VERIFY_TREE_WATCH_S": "0.2",
            "VERIFY_ASSERTION_GUARD_SCRIPT": str(stubs / "assertion-guard"),
            "UNIT_RUN_DIR": str(self.board), "UNIT_SESSION_DIR": str(self.board),
            "UNIT_TEAM_ROLE": "test", "CLAUDE_PYTHON": sys.executable,
            "PYTHONPATH": str(VERIFY.resolve().parents[2]), "BUILDLOG_SYNC": "1",
            "PYTHONDONTWRITEBYTECODE": "1", "NEXTEST_TEST_THREADS": "4",
        })

    @override
    def setUp(self) -> None:
        super().setUp()
        self.workspace = self.root / "workspace"
        self.control = self.root / "control"
        self.board = self.root / "board"

    def release_lint_rewrites(self, process: subprocess.Popen[str]) -> None:
        # Formatting runs before clippy fix, then runs again without rewriting.
        for stage in ("mend", "fmt", "fix"):
            self.wait_for_running(self.control / (stage + ".rewritten"), process)
            self.assertEqual(list((self.root / "tmp").glob("verify-tree.*")), [],
                             f"the watcher started during the {stage} rewrite")
            _ = (self.control / (stage + ".release")).touch()

    @staticmethod
    def write_tool(path: Path, code: str) -> None:
        _ = path.write_text(f"#!{sys.executable}\n" + code)
        path.chmod(0o755)

    @staticmethod
    def write_shell_tool(path: Path, code: str) -> None:
        _ = path.write_text("#!/bin/sh\n" + code)
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
                output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.fail(f"verify exited before {path.name}: {output!r} {errors!r}")
            raise

    def finish_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            self.end_group(process.pid)
        _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)

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
                               capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS).stdout.strip()
        self.assertTrue(not state or state.startswith("Z"), f"cargo descendant {pid} survived: {state}")

    def assert_no_cache_verdict(self) -> None:
        for suffix in ("*.pass", "*.fail"):
            self.assertEqual(list((self.board / "verify_cache").glob(suffix)), [],
                             "cancelled run recorded a cache verdict")

    def assert_cancelled(self, process: subprocess.Popen[str], command: str) -> None:
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
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
