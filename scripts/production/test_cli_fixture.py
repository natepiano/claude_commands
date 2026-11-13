"""Keep cold imports outside the CLI fixture's temporary environment."""

from __future__ import annotations

import os
import signal
from types import ModuleType
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production.cli_fixture import invoke_cli


class CliFixtureTests(unittest.TestCase):
    def test_cold_imported_production_paths_return_to_the_callers_home_after_each_case(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            caller = Path(scratch) / "caller"
            caller.mkdir()
            environment = {**os.environ, "HOME": str(caller)}
            for name in ("SHOWRUNNERS_CONFIG", "NOTIFIER_STATE_DIR", "NOTIFIER_SESSIONS_DIR",
                         "SHOWRUNNER_STATE_DIR", "SHOWRUNNERS_SESSIONS", "SHOWRUNNERS_SEND",
                         "CLAUDE_CONFIG_DIR", "AGENT_NOTES_DIR", "CLAUDE_SESSIONS_DIR"):
                _ = environment.pop(name, None)
            program = '''import os, sys
from pathlib import Path
from types import ModuleType
from scripts.production.cli_fixture import invoke_cli
assert "scripts.production.showrunners" not in sys.modules
assert "scripts.whoami.note_file" not in sys.modules
caller = Path.home()
caller_path = sys.path.copy()
command = ModuleType("fixture_command")
command.__file__ = "fixture_command.py"
def paths(home):
    from scripts.production import showrunners
    from scripts.whoami import note_file
    assert note_file.AGENTS_DIR == home / "rust/hanadocs/agents"
    assert showrunners.NOTIFIER_STATE_DIR == home / ".local/state/notifier"
    assert showrunners.sessions_directory() == home / ".claude/sessions"
    assert showrunners.SHOWRUNNER_STATE_DIR == home / ".local/state/showrunner"
for name in ("first", "second"):
    home = caller.parent / name
    home.mkdir()
    def main():
        paths(home)
        return 0
    result = invoke_cli(command, main, [], {**os.environ, "HOME": str(home)}, home,
                        production_state=True)
    assert result.returncode == 0, result.stderr
    assert Path.home() == caller
    assert sys.path == caller_path
    paths(caller)
'''
            root = Path(__file__).resolve().parents[2]
            result = subprocess.run([str(root / "scripts/lib/py"), "-c", program], env=environment,
                                    cwd=root, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cli_restores_the_callers_signal_mask_after_return_and_failure(self) -> None:
        original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        caller_mask = original_mask | {signal.SIGUSR1}
        command = ModuleType("fixture_command")
        command.__file__ = "fixture_command.py"
        original_handler = signal.getsignal(signal.SIGTERM)
        try:
            for failed in (False, True):
                with self.subTest(failed=failed):
                    _ = signal.pthread_sigmask(signal.SIG_SETMASK, caller_mask)

                    def main() -> int:
                        _ = signal.pthread_sigmask(signal.SIG_BLOCK,
                                                  {signal.SIGTERM, signal.SIGINT, signal.SIGHUP})
                        _ = signal.signal(signal.SIGTERM, signal.SIG_IGN)
                        if failed:
                            raise RuntimeError("fixture command failed")
                        return 0

                    if failed:
                        with self.assertRaisesRegex(RuntimeError, "fixture command failed"):
                            _ = invoke_cli(command, main, [], dict(os.environ), Path.cwd())
                    else:
                        result = invoke_cli(command, main, [], dict(os.environ), Path.cwd())
                        self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, set()), caller_mask)
                    self.assertEqual(signal.getsignal(signal.SIGTERM), original_handler)
        finally:
            _ = signal.signal(signal.SIGTERM, original_handler)
            _ = signal.pthread_sigmask(signal.SIG_SETMASK, original_mask)
