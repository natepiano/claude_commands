"""Consultation status requires confirmed absence or a successful stop."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import override

from scripts.production import staff
from scripts.production.staff_test_support import CHECKOUT, StaffTestCase, fixture_session
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class EndFriendTests(StaffTestCase):
    scripts: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.scripts = self.root / "scripts"
        for folder in ("ask_a_friend", "lib", "production", "agents", "shutdown"):
            (self.scripts / folder).mkdir(parents=True)
        _ = shutil.copyfile(CHECKOUT / "scripts/ask_a_friend/end_friend.sh", self.scripts / "ask_a_friend/end_friend.sh")
        for name in ("lib/py", "production/worker_fill.py"):
            (self.scripts / name).symlink_to(CHECKOUT / "scripts" / name)
        for name in ("agents/codex_mesh.py", "shutdown/shutdown.py"):
            _ = (self.scripts / name).write_text('''import os, sys
from pathlib import Path
with Path(os.environ["STOP_LOG"]).open("a") as output:
    output.write(sys.argv[1] + "\\n")
raise SystemExit(1 if sys.argv[1] == "stop" else 0)
''')

    def test_status_changes_only_after_confirmed_absence_or_successful_stop(self) -> None:
        _, unit, worker = self.tree()
        for case in ("absent", "unreadable", "claude-stop-failed", "codex-stop-failed"):
            with self.subTest(case=case):
                run = self.root / case
                run.mkdir()
                _ = (run / "consult_agent").write_text("launched\n")
                _ = (run / "friend_name").write_text("missing" if case == "absent" else worker.name)
                status = run / "status"
                _ = status.write_text("running\n")
                stop_log = run / "stop-log"
                before = self.staff_file.read_bytes()
                if case == "unreadable":
                    _ = self.staff_file.write_text("{broken")
                elif case == "codex-stop-failed":
                    fixture_session(worker.current_session, staff.CodexBackend(self.root / "codex"))
                else:
                    fixture_session(worker.current_session, staff.ClaudeBackend())
                try:
                    result = subprocess.run(["bash", str(self.scripts / "ask_a_friend/end_friend.sh"), str(run)],
                        env={**os.environ, "CODEX_THREAD_ID": unit.current_session, "STOP_LOG": str(stop_log)},
                        capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
                    self.assertEqual(result.returncode == 0, case == "absent", result.stdout + result.stderr)
                    self.assertEqual(status.read_text(), "ended\n" if case == "absent" else "running\n")
                    self.assertEqual(stop_log.exists(), case.endswith("stop-failed"))
                finally:
                    _ = self.staff_file.write_bytes(before)

    def test_member_without_absence_option_still_refuses_a_missing_worker(self) -> None:
        _, unit, _ = self.tree()
        result = subprocess.run([str(CHECKOUT / "scripts/lib/py"),
            str(CHECKOUT / "scripts/production/worker_fill.py"), "member", "--name", "missing"],
            env={**os.environ, "CODEX_THREAD_ID": unit.current_session}, capture_output=True,
            text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("no worker staff row", result.stderr)
