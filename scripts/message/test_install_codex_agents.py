"""Install messaging instructions without changing the surrounding agent rules."""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict, cast, override


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

SCRIPT = Path(__file__).with_name("install_codex_agents.py")
MESSAGING_TEXT = Path(__file__).parents[2] / "codex" / "AGENTS-messaging.md"
BEGIN = b"<!-- claude-messaging: begin -->"
END = b"<!-- claude-messaging: end -->"


class SshInvocation(TypedDict):
    arguments: list[str]
    stdin: str


class InstallCodexAgentsTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.home: Path = Path()
        self.bin: Path = Path()
        self.ssh_log: Path = Path()
        self.target: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.ssh_log = self.root / "ssh.jsonl"
        self.target = self.root / "custom rules" / "AGENTS.md"
        ssh = self.bin / "ssh"
        _ = ssh.write_text(
            "\n".join([
                f"#!{sys.executable}",
                "import json",
                "import sys",
                "from pathlib import Path",
                f"with Path({str(self.ssh_log)!r}).open('a', encoding='utf-8') as log:",
                "    log.write(json.dumps({'arguments': sys.argv[1:], 'stdin': sys.stdin.read()}) + '\\n')",
            ]) + "\n",
            encoding="utf-8",
        )
        ssh.chmod(0o755)

    def run_cli(
        self, *arguments: str, preexec_fn: Callable[[], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            input="", capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
            env={"HOME": str(self.home), "PATH": str(self.bin), "LC_ALL": "C"},
            preexec_fn=preexec_fn,
        )

    def ssh_calls(self) -> list[SshInvocation]:
        if not self.ssh_log.exists():
            return []
        return [
            cast(SshInvocation, json.loads(line))
            for line in self.ssh_log.read_text(encoding="utf-8").splitlines()
        ]

    def assert_installed_block(self, installed: bytes) -> None:
        self.assertEqual(installed.count(BEGIN), 1)
        self.assertEqual(installed.count(END), 1)
        _, opening, rest = installed.partition(BEGIN)
        content, closing, _ = rest.partition(END)
        self.assertEqual((opening, closing), (BEGIN, END))
        self.assertEqual(
            content.strip(b"\r\n"), MESSAGING_TEXT.read_bytes().strip(b"\r\n")
        )

    def assert_success(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_absent_default_file_is_created_in_the_temporary_home(self) -> None:
        target = self.home / ".codex" / "AGENTS.md"
        self.assertFalse(target.parent.exists())

        self.assert_success(self.run_cli())

        self.assert_installed_block(target.read_bytes())
        self.assertEqual(self.ssh_calls(), [])

    def test_explicit_destination_is_created_without_writing_the_default_file(self) -> None:
        self.assert_success(self.run_cli("--agents-file", str(self.target)))

        self.assert_installed_block(self.target.read_bytes())
        self.assertFalse((self.home / ".codex").exists())
        self.assertEqual(self.ssh_calls(), [])

    def test_replacing_the_marked_block_keeps_every_outside_byte(self) -> None:
        prefix = b"# Rust rules\r\nKeep caf\xc3\xa9 examples.\r\n\r\n"
        suffix = b"\r\n\r\n# Other rules\nKeep the final line without a newline."
        self.target.parent.mkdir()
        _ = self.target.write_bytes(prefix + BEGIN + b"\r\nold instructions\r\n" + END + suffix)

        self.assert_success(self.run_cli("--agents-file", str(self.target)))

        installed = self.target.read_bytes()
        self.assert_installed_block(installed)
        before, _, rest = installed.partition(BEGIN)
        _, _, after = rest.partition(END)
        self.assertEqual((before, after), (prefix, suffix))
        self.assertNotIn(b"old instructions", installed)
        self.assertEqual(self.ssh_calls(), [])

    def test_existing_rules_without_a_block_are_kept_when_the_block_is_added(self) -> None:
        rules = b"# Existing Rust rules\r\nKeep these bytes, including this final line."
        self.target.parent.mkdir()
        _ = self.target.write_bytes(rules)

        self.assert_success(self.run_cli("--agents-file", str(self.target)))

        installed = self.target.read_bytes()
        self.assertTrue(installed.startswith(rules))
        self.assert_installed_block(installed)
        self.assertEqual(self.ssh_calls(), [])

    def test_failed_replacement_preserves_the_file_and_removes_the_temporary_file(self) -> None:
        original = b"# Rust rules\n" + BEGIN + b"\nold instructions\n" + END + b"\nKeep Rust rules.\n"
        self.target.parent.mkdir()
        _ = self.target.write_bytes(original)
        original_mtime = self.target.stat().st_mtime_ns
        original_files = set(self.target.parent.iterdir())

        def limit_file_size() -> None:
            resource.setrlimit(resource.RLIMIT_FSIZE, (256, 256))

        result = self.run_cli("--agents-file", str(self.target), preexec_fn=limit_file_size)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("install_codex_agents.py:", result.stderr)
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(self.target.stat().st_mtime_ns, original_mtime)
        self.assertEqual(set(self.target.parent.iterdir()), original_files)
        self.assertEqual(self.ssh_calls(), [])

    def test_replacement_preserves_the_existing_file_permissions(self) -> None:
        self.target.parent.mkdir()
        _ = self.target.write_bytes(BEGIN + b"\nold instructions\n" + END)
        self.target.chmod(0o600)

        self.assert_success(self.run_cli("--agents-file", str(self.target)))

        self.assert_installed_block(self.target.read_bytes())
        self.assertEqual(self.target.stat().st_mode & 0o7777, 0o600)
        self.assertEqual(self.ssh_calls(), [])

    def test_a_new_file_uses_the_default_permissions_under_the_process_umask(self) -> None:
        def set_file_permissions() -> None:
            _ = os.umask(0o027)

        self.assert_success(self.run_cli(
            "--agents-file", str(self.target), preexec_fn=set_file_permissions,
        ))

        self.assert_installed_block(self.target.read_bytes())
        self.assertEqual(self.target.stat().st_mode & 0o7777, 0o640)
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])
        self.assertEqual(self.ssh_calls(), [])

    def test_malformed_markers_are_refused_without_changing_the_file(self) -> None:
        count_reason = "AGENTS.md must have exactly one begin marker and one end marker"
        malformed_blocks = (
            (BEGIN + b"\nmissing end\n", count_reason),
            (b"missing begin\n" + END, count_reason),
            (BEGIN + b"\n" + BEGIN + b"\n" + END, count_reason),
            (END + b"\n" + BEGIN, "AGENTS.md messaging end marker precedes its begin marker"),
        )
        self.target.parent.mkdir()
        for block, reason in malformed_blocks:
            with self.subTest(block=block):
                original = b"# Rules\r\n" + block + b"\r\nKeep these rules."
                _ = self.target.write_bytes(original)
                original_mtime = self.target.stat().st_mtime_ns

                result = self.run_cli("--agents-file", str(self.target))

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(result.stderr, f"install_codex_agents.py: {reason}\n")
                self.assertEqual(self.target.read_bytes(), original)
                self.assertEqual(self.target.stat().st_mtime_ns, original_mtime)
                self.assertEqual(list(self.target.parent.iterdir()), [self.target])
                self.assertEqual(self.ssh_calls(), [])

    def test_a_second_install_leaves_the_file_bytes_and_modification_time_unchanged(self) -> None:
        self.assert_success(self.run_cli("--agents-file", str(self.target)))
        first = self.target.read_bytes()
        first_mtime = self.target.stat().st_mtime_ns

        self.assert_success(self.run_cli("--agents-file", str(self.target)))

        self.assertEqual(self.target.read_bytes(), first)
        self.assertEqual(self.target.stat().st_mtime_ns, first_mtime)
        self.assertEqual(self.ssh_calls(), [])

    def test_named_machine_uses_fake_ssh_and_writes_nothing_locally(self) -> None:
        local_rules = self.home / ".codex" / "AGENTS.md"
        local_rules.parent.mkdir()
        original = b"# Local Rust rules\r\nUntouched.\n"
        _ = local_rules.write_bytes(original)
        _ = self.home.joinpath("sentinel").write_bytes(b"keep")
        before = {path.relative_to(self.home): path.read_bytes() for path in self.home.rglob("*") if path.is_file()}

        for arguments in ((), ("--agents-file", str(self.target))):
            with self.subTest(arguments=arguments):
                previous_calls = len(self.ssh_calls())
                self.assert_success(self.run_cli("--machine", "mac", *arguments))

                after = {
                    path.relative_to(self.home): path.read_bytes()
                    for path in self.home.rglob("*") if path.is_file()
                }
                self.assertEqual(after, before)
                self.assertFalse(self.target.exists())
                self.assertFalse(self.target.parent.exists())
                calls = self.ssh_calls()[previous_calls:]
                self.assertEqual(len(calls), 1)
                self.assertIn("mac", calls[0]["arguments"])
                self.assertEqual(
                    calls[0]["stdin"], MESSAGING_TEXT.read_text(encoding="utf-8")
                )
                if arguments:
                    self.assertIn(str(self.target), calls[0]["arguments"][-1])

    def test_remote_install_runs_without_a_peer_checkout_and_preserves_existing_rules(self) -> None:
        peer_home = self.root / "peer home"
        shim = peer_home / ".claude" / "scripts" / "lib" / "py"
        shim.parent.mkdir(parents=True)
        _ = shim.write_text(
            "\n".join([
                f"#!{sys.executable}",
                "import os",
                "import sys",
                "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])",
            ]) + "\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)
        ssh = self.bin / "ssh"
        _ = ssh.write_text(
            "\n".join([
                f"#!{sys.executable}",
                "import json",
                "import os",
                "import subprocess",
                "import sys",
                "from pathlib import Path",
                "payload = sys.stdin.buffer.read()",
                f"with Path({str(self.ssh_log)!r}).open('a', encoding='utf-8') as log:",
                "    log.write(json.dumps({'arguments': sys.argv[1:], 'stdin': payload.decode('utf-8')}) + '\\n')",
                "result = subprocess.run(",
                "    ['/bin/sh', '-c', sys.argv[-1]], input=payload, capture_output=True,",
                f"    env={{**os.environ, 'HOME': {str(peer_home)!r}}},",
                f"    cwd={str(peer_home)!r}, timeout={TEST_HANG_GUARD_SECS}, check=False,",
                ")",
                "sys.stdout.buffer.write(result.stdout)",
                "sys.stderr.buffer.write(result.stderr)",
                "raise SystemExit(result.returncode)",
            ]) + "\n",
            encoding="utf-8",
        )
        peer_target = peer_home / ".codex" / "AGENTS.md"
        peer_target.parent.mkdir()
        prefix = b"# Peer Rust rules\r\nKeep raw bytes: \xff\r\n"
        suffix = b"\r\nOther peer rules, without a final newline."
        _ = peer_target.write_bytes(prefix + BEGIN + b"\nold peer text\n" + END + suffix)
        self.assertFalse((peer_home / "codex").exists())
        self.assertFalse((peer_home / ".claude" / "scripts" / "message").exists())

        self.assert_success(self.run_cli("--machine", "mac"))

        installed = peer_target.read_bytes()
        self.assert_installed_block(installed)
        before, _, rest = installed.partition(BEGIN)
        _, _, after = rest.partition(END)
        self.assertEqual((before, after), (prefix, suffix))
        first_mtime = peer_target.stat().st_mtime_ns

        self.assert_success(self.run_cli("--machine", "mac"))

        self.assertEqual(peer_target.read_bytes(), installed)
        self.assertEqual(peer_target.stat().st_mtime_ns, first_mtime)
        self.assertFalse((self.home / ".codex").exists())
        calls = self.ssh_calls()
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertIn("mac", call["arguments"])
            self.assertIn("--from-stdin", call["arguments"][-1])
            self.assertEqual(call["stdin"].encode("utf-8"), MESSAGING_TEXT.read_bytes())


if __name__ == "__main__":
    _ = unittest.main()
