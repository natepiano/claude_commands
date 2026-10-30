"""Interrupted conversion and runner ownership recover without real skills."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.claude_to_codex import sync
from scripts.lib.rerun_test_support import SCRIPTS, ScriptRepositoryTestCase


class SyncRerunTests(ScriptRepositoryTestCase):
    def test_a_skill_directory_without_its_file_is_completed_on_rerun_without_force(self) -> None:
        source = self.home / "commands"
        _ = self.write(source / "example.md", "# Example\n\nDo one thing.\n")
        destination = self.home / "skills"
        arguments = ["sync.py", "--source", str(source), "--dest", str(destination)]
        with mock.patch.object(sys, "argv", arguments), redirect_stdout(io.StringIO()):
            with mock.patch.object(sync, "atomic_write_text", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    _ = sync.main()
            skill = destination / "example" / "SKILL.md"
            self.assertTrue(skill.parent.is_dir())
            self.assertFalse(skill.exists())
            self.assertEqual(sync.main(), 0)
            self.assertIn("Example", skill.read_text())
            before = skill.read_bytes()
            self.assertEqual(sync.main(), 0)
            self.assertEqual(skill.read_bytes(), before)

    def test_a_sync_runner_killed_after_lock_acquisition_does_not_block_a_rerun(self) -> None:
        source = self.home / ".claude/commands"
        source.mkdir(parents=True)
        log_dir = self.root / "sync-state"
        copy = self.root / "sync-scripts/run_sync.sh"
        content = (SCRIPTS / "claude_to_codex/run_sync.sh").read_text()
        content = content.replace('PY="$SCRIPT_DIR/../lib/py"', f'PY="{sys.executable}"')
        content = content.replace('LOG_DIR="/tmp/claude-to-codex-sync"', f'LOG_DIR="{log_dir}"')
        content = content.replace('"$SCRIPT_DIR/../prioritize/runner_lock.py"', f'"{SCRIPTS / "prioritize/runner_lock.py"}"')
        content = content.replace('log "starting sync run"', '''if [[ ! -e "$LOG_DIR/stopped" ]]; then
    touch "$LOG_DIR/stopped"
    kill -KILL "$$"
fi
log "starting sync run"''')
        _ = self.write(copy, content)
        _ = self.write(copy.with_name("sync.py"), f'from pathlib import Path\nPath({str(log_dir / "converted")!r}).touch()\n')
        _ = self.stub("sleep", "pass\n")
        # A leftover directory from the old implementation has no authority.
        (log_dir / "lock").mkdir(parents=True)
        stopped = self.run_in_repo("bash", str(copy))
        self.assertNotEqual(stopped.returncode, 0)
        self.assertFalse((log_dir / "converted").exists())
        self.assertIn("started=", (log_dir / "runner.lock").read_text())
        finished = self.run_in_repo("bash", str(copy))
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertTrue((log_dir / "converted").exists())
        self.assertTrue((log_dir / "last-status").read_text().startswith("ok "))
