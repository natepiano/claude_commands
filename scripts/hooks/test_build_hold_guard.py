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
VERIFY_BUILDS = ("check", "lint", "build", "doc", "clippy")


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
    def run_hook(self, command: str, tool: str = "Bash", session: str = "caller") -> str:
        payload = {"session_id": session, "tool_name": tool, "tool_input": {"command": command}}
        done = subprocess.run(["bash", str(FRONT)], input=json.dumps(payload),
                              capture_output=True, text=True, check=True,
                              env={**os.environ, "CLAUDE_PYTHON": sys.executable})
        return done.stdout

    def assert_denied(self, command: str) -> None:
        response = cast(dict[str, dict[str, str]], json.loads(self.run_hook(command)))
        decision = response["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse", command)
        self.assertEqual(decision["permissionDecision"], "deny", command)
        self.assertEqual(decision["permissionDecisionReason"], REFUSAL, command)

    def test_each_cargo_build_with_and_without_toolchain(self) -> None:
        self.hold()
        for verb in CARGO_BUILDS:
            for toolchain in ("", "+nightly "):
                with self.subTest(verb=verb, toolchain=toolchain):
                    self.assert_denied(f"cargo {toolchain}{verb}")

    def test_each_verify_build(self) -> None:
        self.hold()
        for verb in VERIFY_BUILDS:
            for path in ("verify.sh", "~/.claude/scripts/direct/verify.sh"):
                for options in ("", '--session-dir "check session" ', "--session-dir=run "):
                    with self.subTest(verb=verb, path=path, options=options):
                        self.assert_denied(f"{path} {options}{verb} x")

    def test_paths_prefixes_and_compound_commands(self) -> None:
        self.hold()
        for command in ("cd /tmp && cargo build", "pwd; cargo test", "true\ncargo check",
                        "env RUST_LOG=debug cargo +stable run", "RUST_LOG=debug cargo test",
                        "command cargo clippy", "/home/user/.cargo/bin/cargo build",
                        "cargo --offline check", "cargo --config config.toml build",
                        "bash ~/.claude/scripts/direct/verify.sh lint", "bash -lc 'cargo test'",
                        "if true; then cargo build; fi", "(cargo build)", "cargo fmt | cargo test",
                        "cargo \\\nbuild", "timeout 600 cargo test", "sudo -u user cargo build",
                        "nice -n 10 cargo check", "bash <<'EOF'\ncargo build\nEOF\n",
                        "cargo miri test", "cargo +nightly miri test",
                        "verify.sh --session-dir run check x", "bash verify.sh --session-dir run check x"):
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

    def test_refusal_names_only_in_force_holders(self) -> None:
        self.hold()
        self.hold("released", "released builder")
        self.releasing("released")
        self.assert_denied("cargo build")
        self.assert_denied("verify.sh lint")

    def test_caller_is_only_in_force_holder_next_to_releasing_hold(self) -> None:
        self.hold("caller")
        self.hold("released", "released builder")
        self.releasing("released")
        self.assertEqual(self.run_hook("cargo build"), "")

    def test_unreadable_state_is_allowed(self) -> None:
        self.unreadable()
        self.assertEqual(self.run_hook("cargo build"), "")

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
        for has_hold, command in ((False, "cargo build"), (True, "cargo metadata"), (True, "pwd")):
            if has_hold:
                self.hold()
            payload = {"tool_name": "Bash", "tool_input": {"command": command}}
            done = subprocess.run([sys.executable, "-v", str(HOOK)], input=json.dumps(payload),
                                  capture_output=True, text=True, check=True)
            self.assertEqual(done.stdout, "")
            self.assertNotIn("import 'hold_applies'", done.stderr)
            self.assertNotIn("import 'build_targets'", done.stderr)
            self.assertNotIn("import 'build_hold'", done.stderr)

    def test_invalid_payload_is_allowed(self) -> None:
        for raw in ("{", "null", '{"tool_name":"Bash","tool_input":{"command":null}}'):
            done = subprocess.run([sys.executable, str(HOOK)], input=raw, capture_output=True, text=True, check=True)
            self.assertEqual(done.stdout, "")


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
        raw = json.dumps({"tool_name": "Bash", "tool_input": {"command": "cargo build"}})
        self.assert_no_python(raw)
        self.holders.mkdir()
        self.assert_no_python(raw)

    def test_no_build_text_starts_no_python_during_hold(self) -> None:
        self.hold()
        self.assert_no_python(json.dumps({"tool_name": "Bash", "tool_input": {"command": "pwd"}}))

    def test_same_stdin_output_and_status_are_relayed(self) -> None:
        self.hold()
        os.environ["GUARD_OUTPUT"] = 'deny output\n'
        os.environ["GUARD_STATUS"] = "7"
        for command in ("cargo build", "verify.sh check"):
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


if __name__ == "__main__":
    _ = unittest.main()
