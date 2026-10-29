"""Build calls are refused during another session's hold; work can continue."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, cast, override
from unittest import mock

from scripts.build_hold.test_hold_applies import REFUSAL, TemporaryHoldTest

if TYPE_CHECKING:
    from ..build_hold import build_hold
else:
    import build_hold

HOOK = Path(__file__).with_name("pre-tool-use-build-hold-guard.py")
FRONT = HOOK.with_suffix(".sh")
CARGO_BUILDS = ("build", "b", "check", "c", "clippy", "test", "t", "nextest", "run", "r",
                "bench", "doc", "d", "rustc", "rustdoc", "fix", "install", "mend", "miri")
MERGE_GATE_COMMANDS = (
    "python3 merge_gate.py --tests-only",
    "/usr/bin/python3 scripts/production/merge_gate.py --promote-only",
    "python3.13 -u '/work tree/merge_gate.py' --repo /repo",
    "~/.claude/scripts/lib/py ~/.claude/scripts/production/merge_gate.py --repo /repo",
    "scripts/lib/py scripts/production/merge_gate.py",
    "./merge_gate.py --help",
    "/any/path/merge_gate.py --tests-only",
    '"/work tree/merge_gate.py" --promote-only',
    "bash -lc 'python3 merge_gate.py --tests-only'",
    "env CHECK=1 python3 -W ignore -X dev -- merge_gate.py --help",
)
MERGE_GATE_REFUSAL = REFUSAL + " The merge gate waits for the hold to end. Run it after the release."


class TemporaryGuardTest(TemporaryHoldTest):
    @override
    def setUp(self) -> None:
        with mock.patch("tempfile.tempdir", str(HOOK.resolve().parents[2])):
            super().setUp()

    @override
    def releasing(self, *keys: str) -> None:
        super().releasing(*keys)
        # A release removes its holder file before it sends any notice.
        with mock.patch.object(build_hold, "start_recorder"), mock.patch.object(build_hold, "notify_release", return_value=""):
            for key in keys:
                _ = build_hold.release_cycle(self.holders, key)


class BuildHoldGuardTest(TemporaryGuardTest):
    def run_hook(self, command: str | list[str], tool: str = "Bash", session: str = "caller") -> str:
        payload = {"session_id": session, "hook_event_name": "PreToolUse", "cwd": str(self.root),
                   "tool_name": tool, "tool_input": {"command": command}}
        done = subprocess.run(["bash", str(FRONT)], input=json.dumps(payload),
                              capture_output=True, text=True, check=True,
                              env={**os.environ, "CLAUDE_PYTHON": sys.executable})
        return done.stdout

    def assert_denied(self, command: str | list[str], reason: str = REFUSAL) -> None:
        response = cast(dict[str, dict[str, str]], json.loads(self.run_hook(command)))
        decision = response["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse", command)
        self.assertEqual(decision["permissionDecision"], "deny", command)
        self.assertEqual(decision["permissionDecisionReason"], reason, command)

    def codex_unit(self, owner: str, inherited_session: str) -> None:
        run = self.root / "codex-run"
        run.mkdir()
        active = self.root / "active"
        active.mkdir()
        _ = (active / owner).write_text(str(run) + "\n")
        os.environ.update({"UNIT_RUN_DIR": str(run), "UNIT_TEAM_ROLE": "impl",
                           "CODEX_THREAD_ID": "codex-thread", "CLAUDE_CODE_SESSION_ID": inherited_session})

    def test_codex_worker_of_sole_holder_uses_unit_marker(self) -> None:
        self.hold()
        self.codex_unit("other", "stale-session")
        for command in (["cargo", "check"], ["/bin/bash", "-lc", "verify.sh check"],
                        ["python3", "scripts/production/merge_gate.py", "--tests-only"]):
            with self.subTest(command=command):
                self.assertEqual(self.run_hook(command, session="codex-thread"), "")

    def test_codex_worker_of_another_unit_is_refused(self) -> None:
        self.hold()
        # The marker must take priority even when the worker inherits the holder's id.
        self.codex_unit("another-director", "other")
        for command, reason in ((["cargo", "check"], REFUSAL),
                                (["python3", "scripts/production/merge_gate.py", "--tests-only"], MERGE_GATE_REFUSAL)):
            with self.subTest(command=command):
                response = cast(dict[str, dict[str, str]], json.loads(self.run_hook(command, session="codex-thread")))
                self.assertEqual(response, {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}})
        self.assertEqual(self.run_hook(["/bin/bash", "-lc", "verify.sh test"], session="codex-thread"), "")
        self.assertEqual(self.run_hook(["printf", "%s", "cargo check"], session="codex-thread"), "")

    def test_merge_gate_launch_forms_are_refused_during_another_hold(self) -> None:
        self.hold()
        for command in MERGE_GATE_COMMANDS:
            with self.subTest(command=command):
                self.assert_denied(command, MERGE_GATE_REFUSAL)

    def test_merge_gate_launch_forms_are_allowed_without_a_hold(self) -> None:
        for command in MERGE_GATE_COMMANDS:
            with self.subTest(command=command):
                self.assertEqual(self.run_hook(command), "", command)

    def test_merge_gate_launch_forms_are_allowed_for_the_sole_holder(self) -> None:
        self.hold("caller")
        for command in MERGE_GATE_COMMANDS:
            with self.subTest(command=command):
                self.assertEqual(self.run_hook(command), "", command)

    def test_merge_gate_mentions_are_allowed_during_a_hold(self) -> None:
        self.hold()
        for command in (
            "echo 'python3 merge_gate.py --tests-only'",
            "printf '%s' 'scripts/lib/py scripts/production/merge_gate.py'",
            "rg 'merge_gate.py' scripts",
            "cat <<'EOF' > notes.md\npython3 merge_gate.py --tests-only\nEOF\n",
            "cat <<EOF > notes.md\n./merge_gate.py --promote-only\nEOF\n",
            "python3 -c \"print('merge_gate.py')\"",
            "python3 other.py merge_gate.py",
            "python3 -W merge_gate.py other.py",
            "python3 merge_gate.py.bak",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.run_hook(command), "", command)
        self.assert_denied("echo 'merge_gate.py'; cargo build")

    def test_each_cargo_build_with_and_without_toolchain(self) -> None:
        self.hold()
        for verb in CARGO_BUILDS:
            for toolchain in ("", "+nightly "):
                with self.subTest(verb=verb, toolchain=toolchain):
                    self.assert_denied(f"cargo {toolchain}{verb}")

    def test_verify_routes_itself_without_a_record_or_in_any_record_state(self) -> None:
        self.hold()
        directory = Path(os.environ["VERIFY_STEPS_DIR"])
        directory.mkdir()
        os.environ["BUILDLOG_CALL_ID"] = "verify-call"
        proc = self.root / "proc" / "10"
        proc.mkdir(parents=True)
        fields = ["0"] * 20
        fields[0], fields[19] = "S", "11000"
        _ = (proc / "stat").write_text("10 (verify.sh) " + " ".join(fields))
        os.environ["VERIFY_PROC_ROOT"] = str(proc.parent)
        for state in ("missing", "waiting_hold", "waiting_mac", "on_mac", "on_natedev", "unreadable"):
            record = directory / "verify-call.json"
            if state == "unreadable":
                _ = record.write_text("{broken")
            elif state != "missing":
                _ = record.write_text(json.dumps({
                    "v": 1, "call_id": "verify-call", "pid": 10, "proc_start": "11000",
                    "repo": str(self.root), "kind": "check", "package": None,
                    "runs_on": {"kind": "mac_allowed"}, "state": state,
                    "updated": "2026-10-28T12:00:00+00:00",
                }))
            for verb in ("check", "lint", "build", "doc", "clippy", "test", "example", "final"):
                for command in (f"verify.sh {verb}", f"bash verify.sh --session-dir run {verb} x"):
                    with self.subTest(state=state, command=command):
                        self.assertEqual(self.run_hook(command), "")
            # A record does not grant permission to start raw cargo.
            self.assert_denied("cargo check")

    def test_paths_prefixes_and_compound_commands(self) -> None:
        self.hold()
        for command in ("cd /tmp && cargo build", "pwd; cargo test", "true\ncargo check",
                        "env RUST_LOG=debug cargo +stable run", "RUST_LOG=debug cargo test",
                        "command cargo clippy", "/home/user/.cargo/bin/cargo build",
                        "cargo --offline check", "cargo --config config.toml build",
                        "bash -lc 'cargo test'",
                        "if true; then cargo build; fi", "(cargo build)", "cargo fmt | cargo test",
                        "cargo \\\nbuild", "timeout 600 cargo test", "sudo -u user cargo build",
                        "nice -n 10 cargo check", "bash <<'EOF'\ncargo build\nEOF\n",
                        "cargo miri test", "cargo +nightly miri test",
                        "verify.sh check; cargo check"):
            with self.subTest(command=command):
                self.assert_denied(command)

    def test_non_building_calls_are_allowed(self) -> None:
        self.hold()
        for command in ("verify.sh test", "verify.sh test package", "verify.sh status", "verify.sh help",
                        "verify.sh --session-dir run test x", 'verify.sh --session-dir "check" test x',
                        "verify.sh --session-dir=check test x", "verify.sh --session-dir",
                        "verify.sh", "cargo metadata", "cargo +nightly metadata", "cargo fmt", "cargo tree",
                        "cargo clean", "cargo --version", "cargo help build", "echo 'cargo build'",
                        "printf '%s' 'verify.sh check'", "rg 'cargo build' .", "# cargo build\npwd",
                        "cat <<'EOF' > notes.md\ncargo build\nverify.sh lint\nEOF\n",
                        "cat <<EOF > notes.md\ncargo build\nverify.sh lint\nEOF\n",
                        "sed -i 's/cargo build/cargo fmt/g' notes.md"):
            with self.subTest(command=command):
                self.assertEqual(self.run_hook(command), "", command)

    def test_no_hold_is_allowed(self) -> None:
        self.assertEqual(self.run_hook("cargo build"), "")
        self.holders.mkdir()
        self.assertEqual(self.run_hook("verify.sh check"), "")
        self.assertEqual(self.run_hook("python3 merge_gate.py --tests-only"), "")

    def test_holder_is_allowed(self) -> None:
        self.hold("caller")
        self.assertEqual(self.run_hook("cargo build"), "")
        self.assertEqual(self.run_hook("verify.sh lint"), "")

    def test_payload_identifies_holder_without_session_environment(self) -> None:
        self.hold("caller")
        os.environ["CLAUDE_CODE_SESSION_ID"] = ""
        self.assertEqual(self.run_hook("cargo build"), "")
        response = self.run_hook("cargo build", session="other")
        self.assertIn('"permissionDecision": "deny"', response)

    def test_another_holder_still_refuses_caller(self) -> None:
        self.hold("caller")
        self.hold()
        response = self.run_hook("cargo check")
        self.assertIn('"permissionDecision": "deny"', response)

    def test_release_removes_holder_file_and_allows_builds(self) -> None:
        self.hold()
        self.releasing("other")
        self.assertFalse((self.holders / "other").exists())
        self.assertEqual(self.run_hook("cargo build"), "")
        self.assertEqual(self.run_hook("verify.sh check"), "")
        self.assertEqual(self.run_hook("python3 merge_gate.py --tests-only"), "")

    def test_refusal_names_only_in_force_holders(self) -> None:
        self.hold()
        self.hold("released", "released builder")
        self.releasing("released")
        self.assert_denied("cargo build")
        self.assertEqual(self.run_hook("verify.sh lint"), "")

    def test_caller_is_only_in_force_holder_next_to_releasing_hold(self) -> None:
        self.hold("caller")
        self.hold("released", "released builder")
        self.releasing("released")
        self.assertEqual(self.run_hook("cargo build"), "")

    def test_unreadable_hold_directory_denies_builds_with_its_path(self) -> None:
        self.unreadable()
        for command in ("cargo build", "python3 merge_gate.py --tests-only"):
            response = cast(dict[str, dict[str, str]], json.loads(self.run_hook(command)))
            decision = response["hookSpecificOutput"]
            self.assertEqual(decision["permissionDecision"], "deny")
            self.assertIn("natedev build hold decision cannot be read: NotADirectoryError:",
                          decision["permissionDecisionReason"])
            self.assertIn(str(self.holders), decision["permissionDecisionReason"])

    def test_hold_reader_errors_and_other_decision_exceptions_deny_builds(self) -> None:
        self.hold()
        for exception, message, reason in (
            ("HoldStateReadError", "holder record cannot be read\nread failed",
             "natedev hold state cannot be read: holder record cannot be read read failed"),
            ("RuntimeError", "cycle reader failed\nno decision",
             "natedev build hold decision cannot be read: RuntimeError: cycle reader failed no decision"),
        ):
            with self.subTest(exception=exception):
                driver = '''import runpy, sys
from unittest import mock
sys.path.insert(0, sys.argv[2])
import hold_applies
error = getattr(hold_applies, sys.argv[3]) if sys.argv[3] == "HoldStateReadError" else RuntimeError
with mock.patch.object(hold_applies, "hold_applies", side_effect=error(sys.argv[4])):
    runpy.run_path(sys.argv[1], run_name="__main__")
'''
                done = subprocess.run(
                    [sys.executable, "-c", driver, str(HOOK), str(HOOK.parents[1] / "build_hold"), exception, message],
                    input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "cargo build"}}),
                    capture_output=True, text=True, check=True,
                )
                response = cast(dict[str, dict[str, str]], json.loads(done.stdout))
                self.assertEqual(response["hookSpecificOutput"], {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason,
                })

    def test_unreadable_release_state_is_allowed(self) -> None:
        self.hold("caller")
        release = self.root / "release"
        release.mkdir()
        _ = (release / "current").write_text("missing-cycle\n")
        self.assertEqual(self.run_hook("cargo test"), "")

    def test_other_tools_are_allowed(self) -> None:
        self.hold()
        self.assertEqual(self.run_hook("cargo build", tool="Write"), "")

    def test_fast_paths_do_not_import_hold_code(self) -> None:
        for has_hold, command in ((False, "cargo build"), (True, "cargo metadata"), (True, "pwd"),
                                  (True, "verify.sh check")):
            if has_hold:
                self.hold()
            payload = {"tool_name": "Bash", "tool_input": {"command": command}}
            done = subprocess.run([sys.executable, "-v", str(HOOK)], input=json.dumps(payload),
                                  capture_output=True, text=True, check=True)
            self.assertEqual(done.stdout, "")
            self.assertNotIn("import 'hold_applies'", done.stderr)
            self.assertNotIn("import 'build_targets'", done.stderr)
            self.assertNotIn("import 'build_hold'", done.stderr)

    def test_invalid_payload_denies_when_the_decision_cannot_be_read(self) -> None:
        for raw in ("{", "null"):
            done = subprocess.run([sys.executable, str(HOOK)], input=raw, capture_output=True, text=True, check=True)
            response = cast(dict[str, dict[str, str]], json.loads(done.stdout))
            self.assertEqual(response["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn("natedev build hold decision cannot be read:",
                          response["hookSpecificOutput"]["permissionDecisionReason"])


class ShellFrontTest(TemporaryGuardTest):
    front: Path = Path()
    calls: Path = Path()
    stdin_copy: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.front = self.root / "hooks" / FRONT.name
        self.front.parent.mkdir()
        _ = shutil.copyfile(FRONT, self.front)
        shim = self.root / "lib" / "py"
        shim.parent.mkdir()
        _ = shim.write_text('''#!/usr/bin/env bash
printf 'called\\n' >> "$GUARD_CALLS"
cat > "$GUARD_STDIN"
printf '%s' "$GUARD_OUTPUT"
exit "$GUARD_STATUS"
''')
        shim.chmod(0o755)
        self.calls = self.root / "python-calls"
        self.stdin_copy = self.root / "stdin-copy"
        self.enterContext(mock.patch.dict(os.environ, {
            "GUARD_CALLS": str(self.calls), "GUARD_STDIN": str(self.stdin_copy),
            "GUARD_OUTPUT": "", "GUARD_STATUS": "0",
        }))

    def run_front(self, raw: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(self.front)], input=raw, capture_output=True, text=True, check=False)

    def assert_no_python(self, raw: str) -> None:
        done = self.run_front(raw)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        self.assertFalse(self.calls.exists())

    def test_no_hold_starts_no_python(self) -> None:
        for has_directory in (False, True):
            if has_directory:
                self.holders.mkdir()
            for command in ("cargo build", "python3 merge_gate.py --tests-only"):
                raw = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
                self.assert_no_python(raw)

    def test_no_build_text_starts_no_python_during_hold(self) -> None:
        self.hold()
        for command in ("pwd", "verify.sh check", "bash verify.sh --session-dir run lint"):
            self.assert_no_python(json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}))

    def test_same_stdin_output_and_status_are_relayed(self) -> None:
        self.hold()
        os.environ["GUARD_OUTPUT"] = 'deny output\n'
        os.environ["GUARD_STATUS"] = "7"
        for command in ("cargo build", "python3 merge_gate.py --tests-only"):
            for ending in ("", "\n\n"):
                raw = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}) + ending
                done = self.run_front(raw)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (7, 'deny output\n', ""))
                self.assertEqual(self.stdin_copy.read_text(), raw)
        self.assertEqual(self.calls.read_text().splitlines(), ["called"] * 4)

    def test_hidden_hold_entry_starts_python(self) -> None:
        self.hold(".holder")
        done = self.run_front('cargo build')
        self.assertEqual(done.returncode, 0)
        self.assertTrue(self.calls.is_file())

    def test_hold_path_that_is_not_a_directory_starts_the_decision_reader(self) -> None:
        self.unreadable()
        done = self.run_front('cargo build')
        self.assertEqual(done.returncode, 0)
        self.assertTrue(self.calls.is_file())


if __name__ == "__main__":
    _ = unittest.main()
