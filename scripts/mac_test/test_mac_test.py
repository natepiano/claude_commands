#!/usr/bin/env python3
"""Exercise the Mac test state command through its process interface."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import TypedDict, cast, final, override
from zoneinfo import ZoneInfo


SCRIPT = Path(__file__).with_name("mac_test.py")
LOCAL_ZONE = ZoneInfo("America/New_York")


class RunState(TypedDict):
    pid: int
    proc_start: str
    what: str
    worktree: str
    since: str


BlockState = TypedDict(
    "BlockState",
    {
        "holder": str,
        "session": str | None,
        "for": str,
        "since": str,
        "state": str,
    },
)


SEND_STUB = r'''from __future__ import annotations

import json
import os
from pathlib import Path
import sys

root = Path(os.environ["MAC_TEST_STUB_DIR"])
with (root / "send.jsonl").open("a", encoding="utf-8") as output:
    output.write(json.dumps(sys.argv[1:]) + "\n")
status_file = root / "send-status"
raise SystemExit(int(status_file.read_text()) if status_file.exists() else 0)
'''


SESSIONS_STUB = r'''from __future__ import annotations

import json
import os
from pathlib import Path
import sys

root = Path(os.environ["MAC_TEST_STUB_DIR"])
with (root / "sessions.jsonl").open("a", encoding="utf-8") as output:
    output.write(json.dumps(sys.argv[1:]) + "\n")
if (root / "session-missing").exists():
    raise SystemExit(1)
print(root / "session.sock")
'''


@final
class MacTestCommandTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.state_directory = Path()
        self.stub_directory = Path()
        self.send_stub = Path()
        self.sessions_stub = Path()
        self.environment: dict[str, str] = {}
        self.children: list[subprocess.Popen[str]] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state_directory = self.root / "state"
        self.stub_directory = self.root / "stubs"
        self.stub_directory.mkdir()
        self.send_stub = self.stub_directory / "send.py"
        self.sessions_stub = self.stub_directory / "sessions.py"
        _ = self.send_stub.write_text(SEND_STUB, encoding="utf-8")
        _ = self.sessions_stub.write_text(SESSIONS_STUB, encoding="utf-8")
        self.environment = {
            **{
                name: value
                for name, value in os.environ.items()
                if name != "CLAUDE_CODE_SESSION_ID"
            },
            "MAC_TEST_STATE_DIR": str(self.state_directory),
            "MAC_TEST_SEND": str(self.send_stub),
            "MAC_TEST_SESSIONS": str(self.sessions_stub),
            "MAC_TEST_STUB_DIR": str(self.stub_directory),
            "TZ": "America/New_York",
        }
        self.children = []

    @override
    def tearDown(self) -> None:
        for child in reversed(self.children):
            self.stop_child(child)

    def command(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
        timeout: float = 10,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=self.root,
            env=environment or self.environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    def start_child(self, name: bytes = b"mac ) test") -> subprocess.Popen[str]:
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import ctypes, time; "
                    f"ctypes.CDLL(None).prctl(15, {name!r}); "
                    "time.sleep(30)"
                ),
            ],
            cwd=self.root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.children.append(child)
        deadline = time.monotonic() + 2
        process_name = b""
        while time.monotonic() < deadline:
            stat = Path(f"/proc/{child.pid}/stat").read_bytes()
            process_name = stat[stat.find(b"(") + 1 : stat.rfind(b")")]
            if process_name == name:
                break
            time.sleep(0.01)
        self.assertEqual(process_name, name)
        return child

    def stop_child(self, child: subprocess.Popen[str]) -> None:
        if child.poll() is not None:
            return
        child.terminate()
        try:
            _ = child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            _ = child.wait(timeout=3)

    def assert_command(
        self,
        result: subprocess.CompletedProcess[str],
        returncode: int,
        stdout: str,
        stderr: str = "",
    ) -> None:
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        self.assertEqual(result.stdout, stdout)
        self.assertEqual(result.stderr, stderr)

    def run_state(self) -> RunState:
        return cast(
            RunState,
            json.loads((self.state_directory / "run.json").read_text(encoding="utf-8")),
        )

    def block_state(self) -> BlockState:
        return cast(
            BlockState,
            json.loads((self.state_directory / "block.json").read_text(encoding="utf-8")),
        )

    def logged_arguments(self, name: str) -> list[list[str]]:
        path = self.stub_directory / f"{name}.jsonl"
        if not path.exists():
            return []
        return [
            cast(list[str], json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def local_time(self, since: str, include_zone: bool = True) -> str:
        format_string = "%H:%M %Z" if include_zone else "%H:%M"
        return datetime.fromisoformat(since).astimezone(LOCAL_ZONE).strftime(format_string)

    def assert_utc_time(self, value: str) -> None:
        parsed = datetime.fromisoformat(value)
        self.assertEqual(parsed.utcoffset(), timedelta(0))

    def expected_message(self, what: str, reason: str, ended: str) -> str:
        return (
            f"Message from mac-test: the Mac is free. {what} ended at {ended}; "
            f"your block ({reason}) is active, and nothing is built or tested there "
            "until you run unblock."
        )

    def test_status_reports_free_without_a_block(self) -> None:
        result = self.command("status")

        self.assert_command(result, 0, "free\nno block\n")
        self.assertTrue((self.state_directory / "state.lock").exists())
        self.assertFalse((self.state_directory / "run.json").exists())
        self.assertFalse((self.state_directory / "block.json").exists())

    def test_claim_busy_status_and_release_transitions(self) -> None:
        first = self.start_child()
        second = self.start_child()
        worktree = self.root / "alpha"

        claimed = self.command(
            "claim",
            "--pid",
            str(first.pid),
            "--what",
            "alpha tests",
            "--worktree",
            str(worktree),
        )
        self.assert_command(claimed, 0, "claimed\n")
        run = self.run_state()
        self.assertEqual(set(run), {"pid", "proc_start", "what", "worktree", "since"})
        self.assertEqual(run["pid"], first.pid)
        self.assertEqual(run["what"], "alpha tests")
        self.assertEqual(run["worktree"], str(worktree))
        self.assert_utc_time(run["since"])
        proc_fields = Path(f"/proc/{first.pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        self.assertEqual(run["proc_start"], proc_fields[19])

        busy = self.command(
            "claim", "--pid", str(second.pid), "--what", "beta tests"
        )
        self.assert_command(
            busy,
            11,
            f"busy: alpha tests since {self.local_time(run['since'], include_zone=False)}\n",
        )
        self.assertEqual(self.run_state(), run)

        status = self.command("status")
        self.assert_command(
            status,
            0,
            f"running: alpha tests ({worktree}) since {self.local_time(run['since'])}\n"
            + "no block\n",
        )

        other_release = self.command("release", "--pid", str(second.pid))
        self.assert_command(other_release, 0, "")
        self.assertEqual(self.run_state(), run)

        released = self.command("release", "--pid", str(first.pid))
        self.assert_command(released, 0, "")
        self.assertFalse((self.state_directory / "run.json").exists())

    def test_claim_and_status_read_a_process_whose_name_is_not_utf8(self) -> None:
        child = self.start_child(b"mac \xff test")

        claimed = self.command(
            "claim", "--pid", str(child.pid), "--what", "alpha tests"
        )
        self.assert_command(claimed, 0, "claimed\n")
        run = self.run_state()
        proc_fields = Path(f"/proc/{child.pid}/stat").read_bytes().rsplit(b")", 1)[1].split()
        self.assertEqual(run["proc_start"], proc_fields[19].decode("ascii"))

        status = self.command("status")
        self.assert_command(
            status,
            0,
            f"running: alpha tests ({run['worktree']}) since {self.local_time(run['since'])}\n"
            + "no block\n",
        )

    def test_waiting_claim_succeeds_after_release(self) -> None:
        first = self.start_child()
        second = self.start_child()
        self.assert_command(
            self.command("claim", "--pid", str(first.pid), "--what", "first tests"),
            0,
            "claimed\n",
        )
        waiter = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPT),
                "claim",
                "--pid",
                str(second.pid),
                "--what",
                "second tests",
                "--wait",
                "5",
            ],
            cwd=self.root,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.children.append(waiter)
        time.sleep(0.25)
        self.assertIsNone(waiter.poll())

        self.assert_command(
            self.command("release", "--pid", str(first.pid)), 0, ""
        )
        stdout, stderr = waiter.communicate(timeout=6)
        self.assertEqual(waiter.returncode, 0, stdout + stderr)
        self.assertEqual(stdout, "claimed\n")
        self.assertEqual(stderr, "")
        run = self.run_state()
        self.assertEqual(run["pid"], second.pid)
        self.assertEqual(run["worktree"], str(self.root))

    def test_active_block_rejects_claim_and_enforces_holder(self) -> None:
        child = self.start_child()
        blocked = self.command(
            "block", "--holder", "alice", "--for", "system maintenance"
        )
        self.assert_command(
            blocked,
            0,
            "Mac blocked for alice: nothing is running there, it is free now.\n",
        )
        block = self.block_state()
        self.assertEqual(block["holder"], "alice")
        self.assertIsNone(block["session"])
        self.assertEqual(block["for"], "system maintenance")
        self.assertEqual(block["state"], "active")
        self.assert_utc_time(block["since"])

        claim = self.command(
            "claim", "--pid", str(child.pid), "--what", "alpha tests"
        )
        self.assert_command(claim, 10, "blocked by alice: system maintenance\n")
        self.assertFalse((self.state_directory / "run.json").exists())

        conflict = self.command("block", "--holder", "bob", "--for", "other work")
        self.assert_command(conflict, 1, "already blocked by alice\n")
        self.assertEqual(self.block_state(), block)

        earlier = "2026-01-01T00:00:00+00:00"
        _ = (self.state_directory / "block.json").write_text(
            json.dumps({**block, "since": earlier}), encoding="utf-8"
        )
        replaced = self.command(
            "block", "--holder", "alice", "--for", "updated work"
        )
        self.assert_command(
            replaced,
            0,
            "Mac blocked for alice: nothing is running there, it is free now.\n",
        )
        replacement = self.block_state()
        self.assertEqual(replacement["for"], "updated work")
        self.assertEqual(replacement["state"], "active")
        self.assertEqual(replacement["since"], earlier)

        wrong_holder = self.command("unblock", "--holder", "bob")
        self.assertEqual(wrong_holder.returncode, 1, wrong_holder.stdout + wrong_holder.stderr)
        self.assertEqual(wrong_holder.stderr, "")
        self.assertEqual(len(wrong_holder.stdout.splitlines()), 1)
        self.assertIn("alice", wrong_holder.stdout)
        self.assertEqual(self.block_state(), replacement)

        self.assert_command(
            self.command("unblock", "--holder", "alice"), 0, "Mac unblocked.\n"
        )
        self.assertFalse((self.state_directory / "block.json").exists())
        self.assert_command(self.command("unblock", "--holder", "alice"), 0, "no block\n")

    def test_release_activates_pending_block_and_sends_once_to_session(self) -> None:
        child = self.start_child()
        rejected_child = self.start_child()
        environment = {**self.environment, "CLAUDE_CODE_SESSION_ID": "session-123"}
        worktree = self.root / "alpha"
        self.assert_command(
            self.command(
                "claim",
                "--pid",
                str(child.pid),
                "--what",
                "alpha tests",
                "--worktree",
                str(worktree),
                environment=environment,
            ),
            0,
            "claimed\n",
        )
        run = self.run_state()

        pending = self.command(
            "block",
            "--holder",
            "alice",
            "--for",
            "system maintenance",
            environment=environment,
        )
        self.assert_command(
            pending,
            0,
            "Block pending for alice: alpha tests is running on the Mac. Nothing new "
            + "starts there, and you get a message when it ends.\n",
        )
        block = self.block_state()
        self.assertEqual(block["session"], "session-123")
        self.assertEqual(block["state"], "pending")

        rejected = self.command(
            "claim",
            "--pid",
            str(rejected_child.pid),
            "--what",
            "beta tests",
            environment=environment,
        )
        self.assert_command(rejected, 10, "blocked by alice: system maintenance\n")
        self.assertEqual(self.run_state(), run)

        pending_status = self.command("status", environment=environment)
        self.assert_command(
            pending_status,
            0,
            f"running: alpha tests ({worktree}) since {self.local_time(run['since'])}\n"
            + "block pending for alice: system maintenance\n",
        )
        self.assertEqual(self.logged_arguments("send"), [])

        before_release = datetime.now(LOCAL_ZONE)
        released = self.command(
            "release", "--pid", str(child.pid), environment=environment
        )
        after_release = datetime.now(LOCAL_ZONE)
        self.assert_command(released, 0, "")
        self.assertFalse((self.state_directory / "run.json").exists())
        active = self.block_state()
        self.assertEqual(active["state"], "active")
        self.assertEqual(active["since"], block["since"])
        self.assertEqual(
            self.logged_arguments("sessions"), [["socket", "session:session-123"]]
        )
        send_calls = self.logged_arguments("send")
        self.assertEqual(len(send_calls), 1)
        send = send_calls[0]
        self.assertEqual(
            send[:8],
            [
                "--to",
                f"uds:{self.stub_directory / 'session.sock'}",
                "--from",
                "mac-test",
                "--summary",
                "Mac is free",
                "--key",
                f"mac-free-{block['since']}",
            ],
        )
        self.assertEqual(send[8], "--text")
        possible_times = {
            before_release.strftime("%H:%M %Z"),
            after_release.strftime("%H:%M %Z"),
        }
        self.assertIn(
            send[9],
            {
                self.expected_message("alpha tests", "system maintenance", ended)
                for ended in possible_times
            },
        )

        status = self.command("status", environment=environment)
        self.assert_command(
            status,
            0,
            "free\n"
            + f"blocked by alice since {self.local_time(block['since'])}: system maintenance\n",
        )
        self.assertEqual(len(self.logged_arguments("send")), 1)

    def test_status_settles_killed_run_and_uses_holder_when_session_is_gone(self) -> None:
        child = self.start_child()
        environment = {**self.environment, "CLAUDE_CODE_SESSION_ID": "gone-session"}
        self.assert_command(
            self.command(
                "claim",
                "--pid",
                str(child.pid),
                "--what",
                "beta tests",
                environment=environment,
            ),
            0,
            "claimed\n",
        )
        self.assert_command(
            self.command(
                "block",
                "--holder",
                "bob",
                "--for",
                "release work",
                environment=environment,
            ),
            0,
            "Block pending for bob: beta tests is running on the Mac. Nothing new "
            + "starts there, and you get a message when it ends.\n",
        )
        block = self.block_state()
        _ = (self.stub_directory / "session-missing").write_text("1\n", encoding="utf-8")
        self.stop_child(child)

        settled = self.command("status", environment=environment)
        self.assert_command(
            settled,
            0,
            "free\n"
            + f"blocked by bob since {self.local_time(block['since'])}: release work\n",
        )
        self.assertFalse((self.state_directory / "run.json").exists())
        self.assertEqual(self.block_state()["state"], "active")
        self.assertEqual(
            self.logged_arguments("sessions"), [["socket", "session:gone-session"]]
        )
        send_calls = self.logged_arguments("send")
        self.assertEqual(len(send_calls), 1)
        self.assertEqual(send_calls[0][:2], ["--to", "bob"])

        repeated = self.command("status", environment=environment)
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(len(self.logged_arguments("send")), 1)

    def test_send_status_one_is_delivered_without_warning(self) -> None:
        child = self.start_child()
        _ = (self.stub_directory / "send-status").write_text("1\n", encoding="utf-8")
        self.assert_command(
            self.command("claim", "--pid", str(child.pid), "--what", "gamma tests"),
            0,
            "claimed\n",
        )
        self.assert_command(
            self.command("block", "--holder", "carol", "--for", "benchmark work"),
            0,
            "Block pending for carol: gamma tests is running on the Mac. Nothing new "
            + "starts there, and you get a message when it ends.\n",
        )

        released = self.command("release", "--pid", str(child.pid))

        self.assert_command(released, 0, "")
        self.assertEqual(self.block_state()["state"], "active")
        self.assertEqual(self.logged_arguments("sessions"), [])
        send_calls = self.logged_arguments("send")
        self.assertEqual(len(send_calls), 1)
        self.assertEqual(send_calls[0][:2], ["--to", "carol"])

    def test_send_status_three_warns_once_and_keeps_active_block(self) -> None:
        child = self.start_child()
        _ = (self.stub_directory / "send-status").write_text("3\n", encoding="utf-8")
        self.assert_command(
            self.command("claim", "--pid", str(child.pid), "--what", "delta tests"),
            0,
            "claimed\n",
        )
        self.assert_command(
            self.command("block", "--holder", "dana", "--for", "release work"),
            0,
            "Block pending for dana: delta tests is running on the Mac. Nothing new "
            + "starts there, and you get a message when it ends.\n",
        )

        released = self.command("release", "--pid", str(child.pid))

        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
        self.assertEqual(released.stdout, "")
        self.assertEqual(len(released.stderr.splitlines()), 1)
        self.assertEqual(self.block_state()["state"], "active")
        self.assertEqual(len(self.logged_arguments("send")), 1)


if __name__ == "__main__":
    _ = unittest.main()
