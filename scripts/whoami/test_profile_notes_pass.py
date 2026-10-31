"""The timer profiler measures reads without changing the input state."""
from __future__ import annotations

import io
import json
import os
import pstats
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, nullcontext, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import cast, override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.test_agents_registry import write_registry

from scripts.whoami import profile_notes_pass as profile


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() if path.is_file() else b"<directory>"
            for path in root.rglob("*")}


class ProfileNotesPassTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.home: Path = Path()
        self.notes: Path = Path()
        self.bin: Path = Path()

    @override
    def setUp(self) -> None:
        Path("/tmp/claude").mkdir(exist_ok=True)
        directory = tempfile.TemporaryDirectory(prefix="profile-fixture-", dir="/tmp/claude")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.notes = self.home / "notes"
        self.notes.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # These are input executables, not extra patches of pass functions.
        commands = {
            "ps": "exit 0", "pgrep": "exit 1", "herdr": "printf '[]\\n'",
            "git": "exit 0", "lsof": "exit 0", "security": "exit 1",
            "ssh": "exit 1", "claude": 'printf \'{"loggedIn":false}\\n\'',
        }
        for name, body in commands.items():
            path = self.bin / name
            _ = path.write_text("#!/bin/sh\n" + body + "\n")
            path.chmod(0o700)
        self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.home), "AGENT_NOTES_DIR": str(self.notes),
            "PATH": str(self.bin), "HERDR_BIN": str(self.bin / "herdr"),
            "SHUTDOWN_STATE_DIR": str(self.home / "shutdown"),
            "NOTIFIER_STATE_DIR": str(self.home / "notifier"),
            "NOTIFIER_SESSIONS_DIR": str(self.home / "sessions"),
            "ROSTER_CODEX_SOCKET": str(self.home / "daemon.sock"),
            "ROSTER_RUN_ROOT": str(self.home / "runs"),
            "SHOWRUNNERS_CONFIG": str(self.home / "showrunners.json"),
            "USER_ZONE": "UTC", "PYTHONDONTWRITEBYTECODE": "1",
        }, clear=True))
        _ = write_registry(self.home / "agents.conf")
        pass_ = profile.load_pass()
        # Modules may already be imported by another suite. Replace only input paths.
        for entry in profile.state_copies(pass_):
            if not entry.path_function:
                assert isinstance(entry.owner, ModuleType)
                target = self.home / "fixed" / entry.owner.__name__ / entry.attribute
                _ = self.enterContext(patch.object(entry.owner, entry.attribute, target))
        paths = (
            (self.pass_.quota, "CONFIG", self.home / "showrunners.json"),
            (self.pass_.five, "PROTOCOL", self.home / "quota_alerts.md"),
            (self.pass_.pacer, "SESSIONS", self.home / ".codex/sessions"),
            (self.pass_.pacer, "AGENTS_CONF", self.home / "agents.conf"),
            (self.pass_.pacer, "CODEX_CONFIG", self.home / ".codex/config.toml"),
            (self.pass_.quota, "REGISTRY", self.home / "agents.conf"),
            (self.pass_.quota, "SESSIONS", self.home / "sessions"),
            (self.pass_.quota, "PROTOCOL", self.home / "quota_alerts.md"),
        )
        for module, attribute, value in paths:
            _ = self.enterContext(patch.object(module, attribute, value))
        # Plain imports and package imports can refer to separate showrunner modules.
        for module in (self.pass_.showrunners, import_module("showrunners")):
            for attribute in ("CONFIG", "NOTIFIER_STATE_DIR", "SESSIONS_DIR", "SHOWRUNNER_STATE_DIR"):
                _ = self.enterContext(patch.object(module, attribute, self.home / attribute))
        _ = self.enterContext(patch.object(import_module("scripts.message.process_table"), "PS", [str(self.bin / "ps")]))
        _ = (self.home / "showrunners.json").write_text(json.dumps({
            "threshold_percent": 2, "repeat_minutes": 30, "stall_minutes": 5,
            "faults_to": "fixture", "always": [],
        }))

    @property
    def pass_(self) -> profile.NotesPass:
        return profile.load_pass()


    def test_every_action_patch_resolves_and_has_a_unique_name(self) -> None:
        entries = profile.patch_list(self.pass_)
        self.assertGreater(len(entries), 30)
        self.assertEqual(len(entries), len({entry.name for entry in entries}))
        for entry in entries:
            with self.subTest(target=entry.name):
                self.assertTrue(callable(cast(object, getattr(entry.owner, entry.attribute))))
                self.assertTrue(entry.effect)
        for entry in profile.state_copies(self.pass_):
            self.assertTrue(hasattr(entry.owner, entry.attribute))
            self.assertTrue(entry.source.is_relative_to(self.home))

    def test_argument_free_pass_keeps_all_input_files_unchanged(self) -> None:
        _ = (self.notes / "claude1.md").write_text(
            "---\nlogin: fixture@example.invalid\nweekly_remaining_usage: 75\n---\nFixture note.\n")
        # Seed a reused timer reading using the actual signature and store format.
        from scripts.accounts import accounts
        from scripts.whoami import agent_notes, usage_store
        from scripts.whoami.agent_accounts import observe_workers_home

        signature = agent_notes.timer_usage_signature(accounts.claude_account_selection(), observe_workers_home())
        usage_store.save_timer_usage(signature, [], datetime.now(timezone.utc))
        timer_save = next(entry for entry in profile.patch_list(self.pass_)
                          if entry.owner is usage_store and entry.attribute == "save_timer_usage")
        timer_save_name = f"{usage_store.__name__}.{timer_save.attribute}"
        self.assertIn(timer_save_name, {entry.name for entry in profile.patch_list(self.pass_)})
        state = profile.state_path(self.pass_.pacer, "STATE_FILE")
        state.parent.mkdir(parents=True, exist_ok=True)
        _ = state.write_text('{"files":{},"buckets":{},"history":[],"windows":{}}')
        before = snapshot(self.home)
        for number in range(2):
            output = io.StringIO()
            stats = self.root / f"pass-{number}.prof"
            with redirect_stdout(output):
                measured = profile.profile_pass(stats, self.root / "scratch")
            self.assertEqual(measured.incomplete, ())
            self.assertEqual(snapshot(self.home), before)
            self.assertTrue(stats.is_file())
            functions = cast(dict[tuple[str, int, str], object], getattr(pstats.Stats(str(stats)), "stats"))
            self.assertTrue(any(Path(file).name == "unit_watchdog.py" and name == "run"
                                for file, _, name in functions))
            self.assertEqual(measured.calls["agent_notes.write_note"], 1)
            self.assertEqual(measured.calls["codex_pacer.write_atomic"], 3)
            self.assertEqual(measured.calls[timer_save_name], 0)
            self.assertGreater(measured.calls["scripts.accounts.refusal_watch._watch_lock"], 0)
            self.assertTrue(measured.children)
            self.assertTrue(all(isinstance(child.completion, profile.ChildCompleted) and child.wall_time(0.0) >= 0.0
                                for child in measured.children))
            report = output.getvalue()
            self.assertIn("reaped children", report)
            self.assertIn("patched (cost not measured):", report)
            self.assertIn("children (argv[0..1], count, total wall):", report)
            for entry in profile.patch_list(self.pass_):
                self.assertIn(f"{entry.name}: ", report)
            self.assertNotIn("fixture@example.invalid", report)

    def test_scratch_outside_private_root_is_refused_before_loading_the_pass(self) -> None:
        error = io.StringIO()
        with redirect_stderr(error):
            result = profile.main(["--stats", str(self.root / "unused.prof"), "--scratch", "/var/tmp/notes-profile"])
        self.assertEqual(result, 1)
        self.assertIn("below /tmp/claude/", error.getvalue())
        self.assertFalse((self.root / "unused.prof").exists())
        with self.assertRaises(ValueError):
            _ = profile.private_scratch(Path("/tmp/claude"))

    def test_recovery_results_are_read_from_scratch_with_their_saved_job(self) -> None:
        state = profile.state_path(self.pass_.watchdog, "_state_file", True)
        result = state.parent / "unit-watchdog-results/job.json"
        result.parent.mkdir(parents=True)
        _ = result.write_text('"fixture restart failure"')
        _ = state.write_text(json.dumps({"session": {
            "first_seen": datetime.now(timezone.utc).isoformat(), "alerted": False,
            "job": {"result": str(result), "started": datetime.now(timezone.utc).isoformat()},
        }}))
        before = snapshot(self.home)
        with ExitStack() as stack:
            scratch = profile.private_scratch(self.root / "scratch")
            profile.redirect_state(self.pass_, scratch, stack)
            copied_state = profile.state_path(self.pass_.watchdog, "_state_file", True)
            copied = cast(dict[str, dict[str, dict[str, str]]], json.loads(copied_state.read_text()))
            copied_result = Path(copied["session"]["job"]["result"])
            self.assertTrue(copied_state.is_relative_to(scratch))
            self.assertTrue(copied_result.is_relative_to(scratch))
            self.assertEqual(copied_result.read_text(), result.read_text())
        self.assertEqual(snapshot(self.home), before)

    def test_scratch_symlink_cannot_escape_the_private_root(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as outside:
            link = self.root / "escape"
            link.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                _ = profile.private_scratch(link)

    def test_child_reader_is_timed_and_unaudited_action_is_not_started(self) -> None:
        measured = profile.Measurements()
        marker = self.root / "must-not-exist"
        action = self.bin / "action"
        _ = action.write_text(f"#!/bin/sh\nprintf action > '{marker}'\n")
        action.chmod(0o700)
        with ExitStack() as stack:
            for entry in profile.patch_list(self.pass_):
                if entry.mode.startswith("child_"):
                    _ = stack.enter_context(patch.object(entry.owner, entry.attribute, profile.replacement(entry, measured)))
            read = subprocess.run([str(self.bin / "ps"), "-eo", "pid=,ppid=,comm=,args="], capture_output=True, check=False)
            self.assertEqual(read.returncode, 0)
            with self.assertRaises(OSError):
                _ = subprocess.run([str(action)], capture_output=True, check=False)
        self.assertFalse(marker.exists())
        self.assertEqual(len(measured.children), 1)
        self.assertEqual(measured.children[0].command, (str(self.bin / "ps"), "-eo"))
        self.assertIsInstance(measured.children[0].completion, profile.ChildCompleted)
        self.assertGreaterEqual(measured.children[0].wall_time(0.0), 0.0)
        self.assertEqual(measured.calls["subprocess.Popen (acting or unaudited command)"], 1)

    def test_child_policy_preserves_reads_and_rejects_actions_with_read_words(self) -> None:
        self.assertTrue(profile.readonly_child(("herdr", "--session", "agents", "pane", "read", "pane-id")))
        self.assertTrue(profile.readonly_child(("git", "-C", "/fixture", "status", "--porcelain")))
        self.assertFalse(profile.readonly_child(("herdr", "pane", "send-keys", "pane", "list")))
        self.assertTrue(profile.readonly_child(("git", "-C", "/fixture", "branch", "--show-current")))
        self.assertFalse(profile.readonly_child(("git", "branch", "-D", "old-branch")))
        self.assertFalse(profile.readonly_child(("git", "clean", "--", "status")))
        self.assertFalse(profile.readonly_child(("claude", "--resume", "session-id")))
        self.assertFalse(profile.readonly_child(("codex", "app-server")))
        self.assertFalse(profile.readonly_child(("py", str(Path(__file__).resolve().parents[1] / "message/sessions.py"), "socket", "target")))

    def test_private_scratch_accepts_a_resolved_symlink_root(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            target = Path(directory) / "claude"
            target.mkdir()
            link = self.root / "tmp-link"
            link.symlink_to(target.parent, target_is_directory=True)
            with patch.object(profile, "SCRATCH_ROOT", link / "claude", create=True):
                private = profile.private_scratch(link / "claude/profile")
            self.assertTrue(private.is_relative_to(target.resolve()))

    def test_recovery_result_parent_traversal_cannot_rewrite_outside_scratch(self) -> None:
        state = profile.state_path(self.pass_.watchdog, "_state_file", True)
        state.parent.mkdir(parents=True, exist_ok=True)
        outside = self.root / "outside-result"
        _ = outside.write_text("keep this file")
        outside.chmod(0o640)
        traversal = "/" + "/".join([".."] * 30) + str(outside)
        _ = state.write_text(json.dumps({"session": {"job": {"result": traversal}}}))
        with ExitStack() as stack:
            with self.assertRaises(ValueError):
                profile.redirect_state(self.pass_, profile.private_scratch(self.root / "scratch"), stack)
        self.assertEqual(outside.read_text(), "keep this file")
        self.assertEqual(outside.stat().st_mode & 0o777, 0o640)

    def test_child_launch_options_cannot_substitute_executable_or_run_hooks(self) -> None:
        entry = next(entry for entry in profile.patch_list(self.pass_) if entry.mode == "child_launch")
        command = ("ps", "-eo", "pid=,ppid=,comm=,args=")
        options: tuple[dict[str, object], ...] = (
            {"executable": "/bin/sh"}, {"shell": True},
            {"preexec_fn": lambda: None}, {"startupinfo": object()},
            {"creationflags": 1}, {"unknown_launch_option": object()},
        )
        with patch.object(subprocess.Popen, "__init__") as started:
            guarded = profile.replacement(entry, profile.Measurements(peer_python={
                "mac": "/nix/store/fixture-python/bin/python3", "natedev": "/nix/store/fixture-python/bin/python3"}))
            for kwargs in options:
                with self.subTest(option=next(iter(kwargs))), self.assertRaises(OSError):
                    _ = guarded(object(), command, **kwargs)
            # Popen permits executable and shell as positional arguments too.
            with self.assertRaises(OSError):
                _ = guarded(object(), command, -1, "/bin/sh")
            self.assertEqual(started.call_count, 0)
            _ = guarded(object(), command)
            self.assertEqual(started.call_count, 1)

    def test_private_scratch_test_compares_resolved_paths(self) -> None:
        # Model macOS: spelling a path through /tmp differs from its resolved path.
        real = self.root / "private-tmp"
        real.mkdir()
        link = self.root / "tmp"
        link.symlink_to(real, target_is_directory=True)
        with patch.object(tempfile, "TemporaryDirectory", return_value=nullcontext(str(link))):
            self.test_private_scratch_accepts_a_resolved_symlink_root()

    def test_writing_children_with_read_prefixes_never_start(self) -> None:
        commands = (
            ("git", "symbolic-ref", "HEAD", "refs/heads/other"),
            ("git", "status", "--porcelain", "--output=/tmp/marker"),
            ("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "mac",
             "python3 ~/.claude/scripts/accounts/accounts.py role-map-read; touch /tmp/marker"),
            ("ssh", "mac", "python3 ~/.claude/scripts/message/roster.py --here --snapshot; touch /tmp/marker"),
            ("herdr", "pane", "list", "--output", "/tmp/marker"),
            ("python3", "/fixture/sessions.py", "socket", "target", "extra"),
            ("python3", "/fixture/roster.py", "--snapshot", "--send", "text"),
        )
        entry = next(entry for entry in profile.patch_list(self.pass_) if entry.mode == "child_launch")
        with patch.object(subprocess.Popen, "__init__") as started:
            guarded = profile.replacement(entry, profile.Measurements(peer_python={
                "mac": "/nix/store/fixture-python/bin/python3", "natedev": "/nix/store/fixture-python/bin/python3"}))
            for words in commands:
                with self.subTest(command=words), self.assertRaises(OSError):
                    _ = guarded(object(), words)
            self.assertEqual(started.call_count, 0)

    def test_audited_child_read_forms_still_start(self) -> None:
        commands = (
            ("git", "-C", "/fixture", "symbolic-ref", "--short", "HEAD"),
            ("git", "-C", "/fixture", "rev-parse", "--show-toplevel"),
            ("git", "-C", "/fixture", "rev-parse", "HEAD"),
            ("git", "-C", "/fixture", "branch", "--show-current"),
            ("git", "-C", "/fixture", "rev-list", "--count", "--invert-grep",
             "--grep=^agent-sessions-snapshot: ", "@{u}..HEAD"),
            ("git", "-C", "/fixture", "status", "--porcelain"),
            ("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "natedev",
             "python3 ~/.claude/scripts/accounts/accounts.py role-map-read"),
            ("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "natedev",
             "python3 ~/.claude/scripts/accounts/accounts.py next-read"),
            ("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "mac",
             "~/.claude/scripts/lib/py ~/.claude/scripts/message/roster.py --here --snapshot"),
            ("herdr", "--session", "agents", "pane", "list"),
            ("herdr", "--session", "agents", "pane", "process-info", "--pane", "new-pane"),
            ("herdr", "--session", "agents", "pane", "read", "new-pane", "--source", "visible"),
            ("python3", str(Path(__file__).resolve().parents[1] / "message/sessions.py"), "socket", "target"),
            ("python3", str(Path(__file__).resolve().parents[1] / "message/roster.py"), "--here", "--snapshot"),
        )
        entry = next(entry for entry in profile.patch_list(self.pass_) if entry.mode == "child_launch")
        with patch.object(subprocess.Popen, "__init__") as started:
            guarded = profile.replacement(entry, profile.Measurements(peer_python={
                "mac": "/nix/store/fixture-python/bin/python3", "natedev": "/nix/store/fixture-python/bin/python3"}))
            for words in commands:
                with self.subTest(command=words):
                    _ = guarded(object(), words)
            self.assertEqual(started.call_count, len(commands))
            self.assertTrue(all(Path(cast(tuple[str, ...], invocation.args[1])[0]).is_absolute()
                                for invocation in started.call_args_list))

    def test_ssh_read_form_with_any_shell_operator_is_refused(self) -> None:
        read = "python3 ~/.claude/scripts/accounts/accounts.py role-map-read"
        for operator in (";", "|", "&", "`", "$", "<", ">", "\n"):
            with self.subTest(operator=operator):
                self.assertFalse(profile.readonly_child(("ssh", "-o", "BatchMode=yes", "-o",
                    "ConnectTimeout=5", "mac", read + operator + "touch /tmp/marker")))

    def test_roster_child_runs_the_fixed_read_form_without_shell_expansion(self) -> None:
        words = ("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "mac",
                 '\"$HOME/.claude/scripts/lib/py\" \"$HOME/.claude/scripts/message/roster.py\" --here --snapshot')
        entry = next(entry for entry in profile.patch_list(self.pass_) if entry.mode == "child_launch")
        with patch.object(subprocess.Popen, "__init__") as started:
            guarded = profile.replacement(entry, profile.Measurements(peer_python={
                "mac": "/nix/store/fixture-python/bin/python3", "natedev": "/nix/store/fixture-python/bin/python3"}))
            _ = guarded(object(), words)
        argv = cast(tuple[str, ...], started.call_args.args[1])
        self.assertIn("/nix/store/fixture-python/bin/python3 -B -I -S ~/.claude/scripts/message/roster.py --here --snapshot", argv[-1])
        self.assertNotIn("lib/py", argv[-1])
        self.assertNotIn("$", argv[-1])
        self.assertIn("StrictHostKeyChecking=yes", argv)

    def test_child_launch_uses_only_the_pinned_executable_and_environment(self) -> None:
        substitute = self.root / "substitute"
        substitute.mkdir()
        (substitute / "ps").symlink_to("/bin/sh")
        lookalike = self.bin / "python-read-impostor"
        lookalike.symlink_to("/bin/sh")
        message = Path(__file__).resolve().parents[1] / "message/sessions.py"
        commands: tuple[tuple[tuple[str, ...], dict[str, object]], ...] = (
            (("./ps", "-eo", "pid=,ppid=,comm=,args="), {"cwd": str(substitute)}),
            ((str(substitute / "ps"), "-eo", "pid=,ppid=,comm=,args="), {}),
            (("ps", "-eo", "pid=,ppid=,comm=,args="), {"env": {**os.environ, "PATH": str(substitute)}}),
            ((str(lookalike), str(message), "socket", "target"), {}),
            *((((name, "--unexpected-write-option", "marker"), {}))
              for name in ("ps", "pgrep", "lsof", "readlink")),
        )
        entry = next(entry for entry in profile.patch_list(self.pass_) if entry.mode == "child_launch")
        with patch.object(subprocess.Popen, "__init__") as started:
            guarded = profile.replacement(entry, profile.Measurements(peer_python={
                "mac": "/nix/store/fixture-python/bin/python3", "natedev": "/nix/store/fixture-python/bin/python3"}))
            for words, options in commands:
                with self.subTest(command=words), self.assertRaises(OSError):
                    _ = guarded(object(), words, **options)
            self.assertEqual(started.call_count, 0)

    def test_python_reads_bypass_the_wrapper_and_create_no_interpreter_cache(self) -> None:
        message = Path(__file__).resolve().parents[1] / "message/sessions.py"
        entry = next(entry for entry in profile.patch_list(self.pass_) if entry.mode == "child_launch")
        before = snapshot(self.home)
        with patch.object(subprocess.Popen, "__init__") as started:
            guarded = profile.replacement(entry, profile.Measurements(peer_python={
                "mac": "/nix/store/fixture-python/bin/python3", "natedev": "/nix/store/fixture-python/bin/python3"}))
            _ = guarded(object(), ("python3", str(message), "socket", "target"))
        words = cast(tuple[str, ...], started.call_args.args[1])
        self.assertEqual(Path(words[0]), Path(sys.executable).resolve())
        self.assertIn("-B", words)
        self.assertNotIn("py", words)
        self.assertEqual(snapshot(self.home), before)

    def test_direct_python_read_leaves_absent_wrapper_cache_absent(self) -> None:
        message = Path(__file__).resolve().parents[1] / "message/sessions.py"
        before = snapshot(self.home)
        measured = profile.Measurements()
        with ExitStack() as stack:
            for entry in profile.patch_list(self.pass_):
                if entry.mode.startswith("child_"):
                    _ = stack.enter_context(patch.object(entry.owner, entry.attribute, profile.replacement(entry, measured)))
            read = subprocess.run([sys.executable, str(message), "socket", "absent"],
                                  capture_output=True, check=False)
        self.assertIn(read.returncode, (1, 3))
        self.assertEqual(snapshot(self.home), before)
        self.assertFalse((self.home / ".cache/claude").exists())
        self.assertFalse((self.home / "Library/Caches/claude").exists())
        self.assertEqual(measured.children[0].command, (str(Path(sys.executable).resolve()), "-B"))

    def test_child_timing_distinguishes_running_and_completed_even_at_zero(self) -> None:
        timing = profile.ChildTiming(("ps",), 0.0)
        self.assertIsInstance(timing.completion, profile.ChildRunning)
        timing.completion = profile.ChildCompleted(0.0)
        self.assertEqual(timing.wall_time(10.0), 0.0)

    def test_write_guard_discards_inline_file_writes_and_preserves_reads(self) -> None:
        source = self.home / "marker"
        _ = source.write_text("original")
        measured = profile.Measurements()
        entries = profile.patch_list(self.pass_)
        io_open = next(entry for entry in entries if entry.owner is io and entry.attribute == "open")
        with patch.object(io, "open", profile.replacement(io_open, measured)):
            with source.open("w") as output:
                _ = output.write("replacement")
            self.assertEqual(source.read_text(), "original")
        self.assertEqual(measured.calls[io_open.name], 1)
        with patch.object(io, "open", profile.replacement(io_open, measured)):
            with source.open("r+") as output:
                self.assertEqual(output.read(), "original")
                _ = output.seek(0)
                _ = output.write("ignored")
        self.assertEqual(source.read_text(), "original")


if __name__ == "__main__":
    _ = unittest.main()
