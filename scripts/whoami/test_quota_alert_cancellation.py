"""Quota alerts: cancellation."""

from __future__ import annotations

import os
import subprocess
import select
import shlex
import signal
import unittest
from unittest import mock
from scripts.whoami import quota_alert_test_support as support
import quota_alert

# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300

setUpModule = support.setUpModule


class QuotaAlertTests(support.SeededQuotaReminderFixture):
    def test_a_stopped_switch_leaves_the_registry_alone(self) -> None:
        _ = self.alert([self.note("codex1.md", "active", "0")], self.now)
        # A live descendant holds this pipe until the editor's whole group dies.
        # Without killpg it could still reach the real registry editor later.
        gate = self.root / "release editor"
        os.mkfifo(gate)
        reader, writer = os.pipe()
        editor = self.root / "held editor.sh"
        _ = editor.write_text(
            "agents_set_all_assignments() {\n"
            + f"  (printf 'ready\\n' >&{writer}; read -r release < {shlex.quote(str(gate))}; "
            + f"source {shlex.quote(str(quota_alert.AGENTS))}; agents_set_all_assignments \"$@\") &\n"
            + "  wait \"$!\"\n}\n")
        real_popen = subprocess.Popen
        with os.fdopen(reader, "rb", buffering=0) as ready, os.fdopen(writer, "wb", buffering=0) as held:
            def start_editor(command: list[str], *, stdout: int, stderr: int, text: bool,
                             env: dict[str, str], start_new_session: bool) -> subprocess.Popen[str]:
                process = real_popen(command, stdout=stdout, stderr=stderr, text=text,
                    env=env, start_new_session=start_new_session, pass_fds=(held.fileno(),))
                held.close()
                communicate = process.communicate

                def kill_editor_group() -> None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    _ = process.wait(timeout=TEST_HANG_GUARD_SECS)

                self.addCleanup(kill_editor_group)

                def expire_when_ready(input: str | None = None, timeout: float | None = None) -> tuple[str, str]:
                    if timeout is not None:
                        self.assertEqual(timeout, TEST_HANG_GUARD_SECS)
                        try:
                            self.assertTrue(select.select([ready], [], [], TEST_HANG_GUARD_SECS)[0])
                            self.assertEqual(ready.readline(), b"ready\n")
                        except BaseException:
                            kill_editor_group()
                            raise
                        raise subprocess.TimeoutExpired(command, timeout)
                    return communicate(input, timeout=TEST_HANG_GUARD_SECS)

                _ = self.enterContext(mock.patch.object(process, "communicate", side_effect=expire_when_ready))
                return process

            with (mock.patch.object(quota_alert, "AGENTS", editor),
                  mock.patch.object(quota_alert, "EDIT_TIMEOUT", TEST_HANG_GUARD_SECS),
                  mock.patch.object(subprocess, "Popen", side_effect=start_editor)):
                line = self.refuse()
            self.assertEqual(line, "switch to claude for codex1 failed: agents_set_all_assignments took longer "
                             + f"than {TEST_HANG_GUARD_SECS} s and was stopped")
            self.assertTrue(select.select([ready], [], [], TEST_HANG_GUARD_SECS)[0])
            self.assertEqual(ready.read(), b"", "an editor descendant can still change the registry")
        self.assertEqual(self.assignments()["direct"], "codex")
        self.assertEqual([path.name for path in self.root.glob("agents.conf.*")], [])


if __name__ == "__main__":
    _ = unittest.main()
