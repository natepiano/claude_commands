"""BRP launch admission uses the shared memory gate and a temporary hold cycle."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import TypedDict, cast, override


HOOK = Path(__file__).with_name("pre-tool-use-brp-launch-gate.sh")
SETTINGS = HOOK.parent.parent.parent / "settings.json"
CYCLE_ID = "a" * 32


class HookDecision(TypedDict):
    hookEventName: str
    permissionDecision: str
    permissionDecisionReason: str


class HookResponse(TypedDict):
    hookSpecificOutput: HookDecision


class PendingLaunchSession(TypedDict):
    session_id: str
    name: str
    state: str


class PendingLaunchCycle(TypedDict):
    release_started_at: str
    entries: list[PendingLaunchSession]


class BrpLaunchGateTests(unittest.TestCase):
    root: Path = Path()
    environment: dict[str, str] = {}
    cycle_file: Path = Path()
    hook: Path = HOOK

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        holders = self.root / "holders"
        release = self.root / "release"
        holders.mkdir()
        (release / CYCLE_ID).mkdir(parents=True)
        _ = (holders / "holder").write_text(json.dumps({
            "holder": "holder", "since": datetime.now().astimezone().isoformat(),
            "for": "the test", "release_eta": "unknown",
        }) + "\n")
        _ = (release / "current").write_text(CYCLE_ID + "\n")
        self.cycle_file = release / CYCLE_ID / "cycle.json"
        self.write_cycle()
        (holders / "holder").unlink(missing_ok=True)
        meminfo = self.root / "meminfo"
        _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
        self.environment = {
            **os.environ,
            "HOME": str(self.root),
            "BUILD_HOLD_DIR": str(holders),
            "BUILD_HOLD_RELEASE_DIR": str(release),
            "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
            "BUILD_TARGETS_SCRIPT": str(HOOK.parent.parent / "build_targets" / "build_targets.py"),
            "BUILDLOG_MEMINFO": str(meminfo),
            "BUILDLOG_MEM_POLL_S": "0.01",
            "UNIT_ACTIVE_DIR": str(self.root / "active"),
            "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "",
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "MAC_TEST_STATE_DIR": str(self.root / "mac-state"),
            "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
            "BUILDLOG_DIR": str(self.root / "buildlog"),
        }

    def write_cycle(self) -> None:
        now = datetime.now().astimezone().isoformat()
        cycle: dict[str, object] = {
            "id": CYCLE_ID, "opened_at": now, "release_started_at": now,
            "holders": {"holder": {"since": now, "released_at": ""}},
            "recipients": {"unit-worker": "unit"},
            "entries": [{
                "session_id": "unit-worker", "name": "unit", "state": "ReleasedAwaitingAdmission",
                "released_at": now,
            }],
        }
        _ = self.cycle_file.write_text(json.dumps(cycle) + "\n")

    def entry(self) -> dict[str, object]:
        cycle = cast(dict[str, object], json.loads(self.cycle_file.read_text()))
        entries = cast(list[dict[str, object]], cycle["entries"])
        return entries[0]

    def run_hook(self, session_id: str = "unit-worker", *, include_session: bool = True) -> subprocess.CompletedProcess[str]:
        hook_input = {"tool_name": "mcp__brp__brp_launch"}
        if include_session:
            hook_input["session_id"] = session_id
        return subprocess.run(
            ["bash", str(self.hook)], input=json.dumps(hook_input),
            capture_output=True, text=True, check=False, env=self.environment, timeout=10,
        )

    def use_memory_wait_stub(self, body: str) -> None:
        hooks = self.root / "hooks"
        lint = self.root / "lint"
        hooks.mkdir()
        lint.mkdir()
        self.hook = hooks / HOOK.name
        _ = self.hook.write_text(HOOK.read_text())
        _ = (lint / "memory_gate.sh").write_text(
            "buildlog_wait_for_memory() {\n" + body + "\n}\n"
        )

    def test_failed_memory_wait_denies_launch_with_its_last_reason_line(self) -> None:
        self.use_memory_wait_stub(
            "printf 'memory check started\\nchecker could not read meminfo\\n' >&2\nreturn 7"
        )
        self.assert_denied(
            self.run_hook(),
            "BRP launch memory check failed (exit 7): checker could not read meminfo",
        )

    def test_failed_memory_wait_without_output_denies_launch_with_exit_status(self) -> None:
        self.use_memory_wait_stub("return 9")
        self.assert_denied(self.run_hook(), "BRP launch memory check failed (exit 9)")

    def test_allowed_memory_wait_passes_launch_without_a_deny_answer(self) -> None:
        self.use_memory_wait_stub("printf 'memory admitted\\n' >&2\nreturn 0")
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "memory admitted\n")

    def test_low_memory_wait_leaves_old_release_record_unchanged(self) -> None:
        meminfo = Path(self.environment["BUILDLOG_MEMINFO"])
        _ = meminfo.write_text("MemAvailable: 8388608 kB\n")
        stubs = self.root / "stubs"
        stubs.mkdir()
        sleeper = stubs / "sleep"
        _ = sleeper.write_text(r'''#!/bin/sh
python3 - "$CYCLE_FILE" "$WAIT_STATE_LOG" <<'PY'
import json, sys
entry = json.load(open(sys.argv[1]))["entries"][0]
with open(sys.argv[2], "a") as output:
    output.write(entry["state"] + "\n")
PY
printf "MemAvailable: 67108864 kB\n" > "$BUILDLOG_MEMINFO"
''')
        sleeper.chmod(0o755)
        self.environment["PATH"] = f"{stubs}:{os.environ['PATH']}"
        self.environment["CYCLE_FILE"] = str(self.cycle_file)
        self.environment["WAIT_STATE_LOG"] = str(self.root / "wait-states")

        before = self.cycle_file.read_bytes()
        result = self.run_hook()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("waiting for memory", result.stderr)
        self.assertEqual((self.root / "wait-states").read_text().splitlines(), ["ReleasedAwaitingAdmission"])
        self.assertEqual(self.cycle_file.read_bytes(), before)

    def assert_denied(self, result: subprocess.CompletedProcess[str], reason: str) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        response = cast(HookResponse, json.loads(result.stdout))
        decision = response["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertEqual(decision["permissionDecisionReason"], reason)
        self.assertNotIn("waiting for memory", result.stderr)

    def prepare_hold_before_release(self) -> None:
        _ = (Path(self.environment["BUILD_HOLD_DIR"]) / "holder").write_text(json.dumps({
            "holder": "holder", "since": datetime.now().astimezone().isoformat(),
            "for": "the test", "release_eta": "unknown",
        }))
        cycle = cast(PendingLaunchCycle, json.loads(self.cycle_file.read_text()))
        cycle["release_started_at"] = ""
        cycle["entries"] = [{"session_id": "unit-worker", "name": "unit", "state": "AwaitingRelease"}]
        _ = self.cycle_file.write_text(json.dumps(cycle))

    def test_holder_session_launch_passes_through_without_a_deny_answer(self) -> None:
        self.prepare_hold_before_release()
        holder_file = Path(self.environment["BUILD_HOLD_DIR"]) / "holder"
        before = (self.cycle_file.read_bytes(), holder_file.read_bytes())
        # The hook input's session id equals the holder file's name.
        result = self.run_hook("holder")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertEqual((self.cycle_file.read_bytes(), holder_file.read_bytes()), before)
        self.assertEqual(self.entry().get("delivery", "NotAttempted"), "NotAttempted")
        self.assertNotIn("outcome", self.entry())

    def test_another_session_launch_is_denied_without_memory_wait_or_mark(self) -> None:
        self.prepare_hold_before_release()
        _ = Path(self.environment["BUILDLOG_MEMINFO"]).write_text("MemAvailable: 1 kB\n")
        self.assert_denied(self.run_hook("unit-worker"),
                           "natedev is held by holder for the test; launch after the release.")
        self.assertEqual(self.entry().get("delivery", "NotAttempted"), "NotAttempted")
        self.assertNotIn("outcome", self.entry())
        self.assertNotIn("wait_started_at", self.entry())

    def test_unreleased_session_is_denied_without_memory_wait_or_mark(self) -> None:
        self.prepare_hold_before_release()
        cycle = cast(dict[str, object], json.loads(self.cycle_file.read_text()))
        entries = cast(list[dict[str, object]], cycle["entries"])
        entries[0] = {"session_id": "unit-worker", "name": "unit", "state": "AwaitingRelease"}
        _ = self.cycle_file.write_text(json.dumps(cycle))
        _ = Path(self.environment["BUILDLOG_MEMINFO"]).write_text("MemAvailable: 1 kB\n")
        self.assert_denied(self.run_hook(), "natedev is held by holder for the test; launch after the release.")
        self.assertEqual(self.entry().get("delivery", "NotAttempted"), "NotAttempted")
        self.assertNotIn("outcome", self.entry())

    def test_missing_session_id_before_release_is_denied_without_memory_wait_or_mark(self) -> None:
        self.prepare_hold_before_release()
        self.environment["CLAUDE_CODE_SESSION_ID"] = "unit-worker"
        cycle = cast(dict[str, object], json.loads(self.cycle_file.read_text()))
        cycle["release_started_at"] = ""
        _ = self.cycle_file.write_text(json.dumps(cycle))
        before = self.cycle_file.read_bytes()
        self.assert_denied(self.run_hook(include_session=False), "natedev is held by holder for the test; launch after the release.")
        self.assertEqual(self.cycle_file.read_bytes(), before)

    def test_missing_session_id_during_release_is_admitted_without_a_mark(self) -> None:
        self.environment["CLAUDE_CODE_SESSION_ID"] = "unit-worker"
        before = self.cycle_file.read_bytes()
        result = self.run_hook(include_session=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.cycle_file.read_bytes(), before)

    def test_unheld_launch_passes_through(self) -> None:
        (Path(self.environment["BUILD_HOLD_DIR"]) / "holder").unlink(missing_ok=True)
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_unreadable_hold_state_denies_launch_without_memory_admission(self) -> None:
        holders = Path(self.environment["BUILD_HOLD_DIR"])
        (holders / "holder").unlink(missing_ok=True)
        holders.rmdir()
        _ = holders.write_text("not a directory")
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        response = cast(HookResponse, json.loads(result.stdout))
        decision = response["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("hold state cannot be read", decision["permissionDecisionReason"])
        self.assertNotIn("waiting for memory", result.stderr)
        self.assertEqual(self.entry()["state"], "ReleasedAwaitingAdmission")

    def test_unreadable_release_record_denies_launch_without_repairing_it(self) -> None:
        self.prepare_hold_before_release()
        _ = self.cycle_file.write_text("{broken")
        result = self.run_hook()
        response = cast(HookResponse, json.loads(result.stdout))
        self.assertEqual(response["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.cycle_file.read_text(), "{broken")
        self.assertNotIn("waiting for memory", result.stderr)

    def test_failed_hold_check_without_output_denies_with_a_reason(self) -> None:
        helper = self.root / "failed-hold-check.py"
        _ = helper.write_text("raise SystemExit(12)\n")
        self.environment["BUILD_TARGETS_SCRIPT"] = str(helper)
        before = self.cycle_file.read_bytes()
        result = self.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        response = cast(HookResponse, json.loads(result.stdout))
        decision = response["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertTrue(decision["permissionDecisionReason"].strip())
        self.assertNotIn("waiting for memory", result.stderr)
        self.assertEqual(self.cycle_file.read_bytes(), before)

    def test_settings_registers_hook_with_long_enough_timeout(self) -> None:
        settings = cast(dict[str, object], json.loads(SETTINGS.read_text()))
        hooks = cast(dict[str, list[dict[str, object]]], settings["hooks"])
        registrations = [item for item in hooks["PreToolUse"] if item.get("matcher") == "mcp__brp__brp_launch"]
        self.assertEqual(len(registrations), 1)
        commands = cast(list[dict[str, object]], registrations[0]["hooks"])
        self.assertEqual(len(commands), 1)
        self.assertIn("pre-tool-use-brp-launch-gate.sh", cast(str, commands[0]["command"]))
        self.assertGreater(cast(int, commands[0]["timeout"]), 900)


if __name__ == "__main__":
    _ = unittest.main()
