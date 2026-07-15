from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


PRIORITIZE_DIR = Path(__file__).parents[1]
RUN_WATCHER = PRIORITIZE_DIR / "run_watcher.sh"
RUNNER_LOCK = PRIORITIZE_DIR / "runner_lock.py"
BUSY_EXIT = 75


class WatcherFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.issues = self.root / "issues"
        self.issues.mkdir()
        self.goals = self.root / "prioritization goals.md"
        self.goals.write_text("# Goals\n", encoding="utf-8")
        self.cache = self.root / "cache"
        self.state = self.root / "state"
        self.snapshot = self.root / "snapshot.py"
        self.renumber = self.root / "renumber.py"
        self.signature = self.root / "watch_signature.py"
        self.watcher = self.root / "run_watcher.sh"
        self.owner_release = self.root / "release-owner"
        self.failing_owner = self.root / "failing_owner.py"

        self.snapshot.write_text(
            """#!/usr/bin/env python3
import argparse

parser = argparse.ArgumentParser()
parser.add_argument(\"--output\", required=True)
parser.add_argument(\"--require-complete\", action=\"store_true\")
args = parser.parse_args()
with open(args.output, \"w\", encoding=\"utf-8\") as output:
    output.write('{\"schema\":1}\\n')
""",
            encoding="utf-8",
        )
        self.renumber.write_text(
            """#!/usr/bin/env python3
raise SystemExit(0)
""",
            encoding="utf-8",
        )
        self.signature.write_text(
            """#!/usr/bin/env python3
print(\"stable-signature\")
""",
            encoding="utf-8",
        )

        self.failing_owner.write_text(
            """#!/usr/bin/env python3
import sys
import time
from pathlib import Path

release = Path(sys.argv[1])
while not release.exists():
    time.sleep(0.01)
raise SystemExit(2)
""",
            encoding="utf-8",
        )

        content = RUN_WATCHER.read_text(encoding="utf-8")
        replacements = {
            'SNAPSHOT_TOOL="/Users/natemccoy/.claude/scripts/prioritize/snapshot.py"': (
                f'SNAPSHOT_TOOL="{self.snapshot}"'
            ),
            'RENUMBER_TOOL="/Users/natemccoy/.claude/scripts/prioritize/renumber.py"': (
                f'RENUMBER_TOOL="{self.renumber}"'
            ),
            'SIGNATURE_TOOL="/Users/natemccoy/.claude/scripts/prioritize/watch_signature.py"': (
                f'SIGNATURE_TOOL="{self.signature}"'
            ),
            'ISSUES_DIR="/Users/natemccoy/rust/hanadocs/issues"': (
                f'ISSUES_DIR="{self.issues}"'
            ),
            'GOALS_FILE="/Users/natemccoy/rust/hanadocs/prioritization goals.md"': (
                f'GOALS_FILE="{self.goals}"'
            ),
            'CACHE_DIR="/Users/natemccoy/Library/Caches/hanadocs-prioritize"': (
                f'CACHE_DIR="{self.cache}"'
            ),
            'STATE_DIR="/tmp/hanadocs-prioritize"': f'STATE_DIR="{self.state}"',
            'DEBOUNCE_SECONDS="0.25"': 'DEBOUNCE_SECONDS="0.01"',
            'POLL_SECONDS="0.5"': 'POLL_SECONDS="0.02"',
            'ERROR_RETRY_SECONDS="5"': 'ERROR_RETRY_SECONDS="0.05"',
            'CONCURRENT_RETRY_SECONDS="0.25"': (
                'CONCURRENT_RETRY_SECONDS="0.01"'
            ),
        }
        for original, replacement in replacements.items():
            if original not in content:
                raise AssertionError(f"watcher fixture could not replace: {original}")
            content = content.replace(original, replacement)
        self.watcher.write_text(content, encoding="utf-8")
        self.watcher.chmod(0o700)

    @property
    def runner_lock_path(self) -> Path:
        return self.state / "runner.lock"

    @property
    def event_log(self) -> Path:
        return self.state / "events.log"

    @property
    def last_status(self) -> Path:
        return self.state / "last-status"

    def wait_for_runner_lock(self) -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            result = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER_LOCK),
                    "status",
                    str(self.runner_lock_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 1 and result.stdout.strip() == "held":
                return
            time.sleep(0.01)
        raise AssertionError("runner lock owner did not acquire the test lock")

    def close(self) -> None:
        self.temporary.cleanup()


class RunWatcherTests(unittest.TestCase):
    fixture: WatcherFixture  # pyright: ignore[reportUninitializedInstanceVariable]

    def setUp(self) -> None:
        self.fixture = WatcherFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def _start_lock_owner(self, command: list[str]) -> subprocess.Popen[bytes]:
        owner = subprocess.Popen(
            [
                sys.executable,
                str(RUNNER_LOCK),
                "run",
                str(self.fixture.runner_lock_path),
                *command,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.fixture.wait_for_runner_lock()
        return owner

    def test_busy_one_shot_returns_retryable_status(self) -> None:
        owner = self._start_lock_owner(["/bin/sleep", "0.5"])
        try:
            result = subprocess.run(
                ["/bin/bash", str(self.fixture.watcher)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, BUSY_EXIT)
            self.assertTrue((self.fixture.state / "pending").exists())
        finally:
            owner.terminate()
            owner.wait(timeout=2)

    def test_daemon_retries_after_busy_owner_fails(self) -> None:
        owner = self._start_lock_owner(
            [
                sys.executable,
                str(self.fixture.failing_owner),
                str(self.fixture.owner_release),
            ]
        )
        daemon = subprocess.Popen(
            ["/bin/bash", str(self.fixture.watcher), "--daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5.0
            log = ""
            while time.monotonic() < deadline:
                if self.fixture.event_log.exists():
                    log = self.fixture.event_log.read_text(encoding="utf-8")
                if "detected change was not ranked exit=75" in log:
                    break
                time.sleep(0.02)
            else:
                self.fail(
                    "daemon did not observe the confirmed busy runner lock; "
                    f"log={log!r}"
                )

            self.fixture.owner_release.write_text("release\n", encoding="utf-8")
            self.assertEqual(owner.wait(timeout=2), 2)

            deadline = time.monotonic() + 5.0
            status = ""
            while time.monotonic() < deadline:
                log = self.fixture.event_log.read_text(encoding="utf-8")
                if self.fixture.last_status.exists():
                    status = self.fixture.last_status.read_text(encoding="utf-8")
                if status.startswith("ok ") and "renumber completed, validated" in log:
                    break
                time.sleep(0.02)
            else:
                self.fail(
                    "daemon did not retry after the failing owner released the lock; "
                    f"status={status!r} log={log!r}"
                )

            self.assertIn("renumber completed, validated", log)
            self.assertLess(
                log.index("detected change was not ranked exit=75"),
                log.index("renumber completed, validated"),
            )
        finally:
            if owner.poll() is None:
                owner.terminate()
                owner.wait(timeout=2)
            if daemon.poll() is None:
                os.killpg(daemon.pid, signal.SIGTERM)
                daemon.wait(timeout=2)

    def test_daemon_absorbs_its_own_canonical_rank_writes(self) -> None:
        issue = self.fixture.issues / "issue.md"
        _ = issue.write_text("source\n", encoding="utf-8")
        invocation_log = self.fixture.root / "renumber-invocations"
        _ = self.fixture.signature.write_text(
            f'''#!/usr/bin/env python3
import hashlib
from pathlib import Path

issue = Path({str(issue)!r})
print(hashlib.sha256(issue.read_bytes()).hexdigest())
''',
            encoding="utf-8",
        )
        _ = self.fixture.renumber.write_text(
            f'''#!/usr/bin/env python3
import sys
from pathlib import Path

issue = Path({str(issue)!r})
log = Path({str(invocation_log)!r})
with log.open("a", encoding="utf-8") as output:
    output.write(" ".join(sys.argv[1:]) + "\\n")
if "--apply" in sys.argv:
    issue.write_text("source with generated rank\\n", encoding="utf-8")
raise SystemExit(0)
''',
            encoding="utf-8",
        )

        daemon = subprocess.Popen(
            ["/bin/bash", str(self.fixture.watcher), "--daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5.0
            log = ""
            while time.monotonic() < deadline:
                if self.fixture.event_log.exists():
                    log = self.fixture.event_log.read_text(encoding="utf-8")
                if "ranking writes changed file signatures" in log:
                    break
                time.sleep(0.02)
            else:
                self.fail(f"daemon did not absorb its own rank write; log={log!r}")

            time.sleep(0.15)
            invocations = invocation_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(invocations, ["--apply", "--check", "--check"])
            self.assertNotIn("starting one fresh pass", log)
        finally:
            if daemon.poll() is None:
                os.killpg(daemon.pid, signal.SIGTERM)
                _ = daemon.wait(timeout=2)

    def test_daemon_retries_concurrent_edits_without_error_backoff(self) -> None:
        attempt_marker = self.fixture.root / "concurrent-attempt"
        _ = self.fixture.renumber.write_text(
            f'''#!/usr/bin/env python3
import sys
from pathlib import Path

marker = Path({str(attempt_marker)!r})
if "--apply" in sys.argv and not marker.exists():
    marker.write_text("retry\\n", encoding="utf-8")
    raise SystemExit(3)
raise SystemExit(0)
''',
            encoding="utf-8",
        )

        daemon = subprocess.Popen(
            ["/bin/bash", str(self.fixture.watcher), "--daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5.0
            log = ""
            status = ""
            while time.monotonic() < deadline:
                if self.fixture.event_log.exists():
                    log = self.fixture.event_log.read_text(encoding="utf-8")
                if self.fixture.last_status.exists():
                    status = self.fixture.last_status.read_text(encoding="utf-8")
                if status.startswith("ok ") and "coalescing and retrying" in log:
                    break
                time.sleep(0.02)
            else:
                self.fail(
                    "daemon did not promptly retry a concurrent edit; "
                    + f"status={status!r} log={log!r}"
                )

            self.assertNotIn("detected change was not ranked exit=3", log)
            self.assertNotIn("error: renumber apply failed exit=3", log)
        finally:
            if daemon.poll() is None:
                os.killpg(daemon.pid, signal.SIGTERM)
                _ = daemon.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
