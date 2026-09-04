from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import final, override


PRIORITIZE_DIR = Path(__file__).parents[1]
RUN_WATCHER = PRIORITIZE_DIR / "run_watcher.sh"
RUNNER_LOCK = PRIORITIZE_DIR / "runner_lock.py"
BUSY_EXIT = 75
# The real filter, wired the way the vault wires it. Pointing these at stubs
# would leave the interesting part -- whether git's own stat comparison goes
# quiet -- untested.
STRIP_GENERATED = PRIORITIZE_DIR / "strip_generated.py"
PY_SHIM = PRIORITIZE_DIR.parent / "lib" / "py"


@final
class WatcherFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.issues = self.root / "issues"
        self.issues.mkdir()
        self.goals = self.root / "prioritization goals.md"
        _ = self.goals.write_text("# Goals\n", encoding="utf-8")
        self.cache = self.root / "cache"
        self.state = self.root / "state"
        self.snapshot = self.root / "snapshot.py"
        self.renumber = self.root / "renumber.py"
        self.signature = self.root / "watch_signature.py"
        self.watcher = self.root / "run_watcher.sh"
        self.owner_release = self.root / "release-owner"
        self.failing_owner = self.root / "failing_owner.py"

        _ = self.snapshot.write_text(
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
        _ = self.renumber.write_text(
            """#!/usr/bin/env python3
raise SystemExit(0)
""",
            encoding="utf-8",
        )
        _ = self.signature.write_text(
            """#!/usr/bin/env python3
print(\"stable-signature\")
""",
            encoding="utf-8",
        )

        _ = self.failing_owner.write_text(
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
            'SNAPSHOT_TOOL="$HOME/.claude/scripts/prioritize/snapshot.py"': (
                f'SNAPSHOT_TOOL="{self.snapshot}"'
            ),
            'RENUMBER_TOOL="$HOME/.claude/scripts/prioritize/renumber.py"': (
                f'RENUMBER_TOOL="{self.renumber}"'
            ),
            'SIGNATURE_TOOL="$HOME/.claude/scripts/prioritize/watch_signature.py"': (
                f'SIGNATURE_TOOL="{self.signature}"'
            ),
            'ISSUES_DIR="$HOME/rust/hanadocs/issues"': (
                f'ISSUES_DIR="{self.issues}"'
            ),
            'GOALS_FILE="$HOME/rust/hanadocs/prioritization goals.md"': (
                f'GOALS_FILE="{self.goals}"'
            ),
            # Without this the fixture would aim the post-rank index refresh at
            # the real vault, so running the suite would reach outside its
            # temporary directory and rewrite a live .git/index.
            'VAULT_DIR="$HOME/rust/hanadocs"': f'VAULT_DIR="{self.root}"',
            'CACHE_DIR="${XDG_CACHE_HOME:-$HOME/$([ "$(uname -s)" = Darwin ] && echo Library/Caches || echo .cache)}/hanadocs-prioritize"': (
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
        _ = self.watcher.write_text(content, encoding="utf-8")
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

    @override
    def setUp(self) -> None:
        self.fixture = WatcherFixture()

    @override
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
        owner = self._start_lock_owner(["sleep", "0.5"])
        try:
            result = subprocess.run(
                ["bash", str(self.fixture.watcher)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, BUSY_EXIT)
            self.assertTrue((self.fixture.state / "pending").exists())
        finally:
            owner.terminate()
            _ = owner.wait(timeout=2)

    def test_daemon_retries_after_busy_owner_fails(self) -> None:
        owner = self._start_lock_owner(
            [
                sys.executable,
                str(self.fixture.failing_owner),
                str(self.fixture.owner_release),
            ]
        )
        daemon = subprocess.Popen(
            ["bash", str(self.fixture.watcher), "--daemon"],
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
                    + f"log={log!r}"
                )

            _ = self.fixture.owner_release.write_text("release\n", encoding="utf-8")
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
                    + f"status={status!r} log={log!r}"
                )

            self.assertIn("renumber completed, validated", log)
            self.assertLess(
                log.index("detected change was not ranked exit=75"),
                log.index("renumber completed, validated"),
            )
        finally:
            if owner.poll() is None:
                owner.terminate()
                _ = owner.wait(timeout=2)
            if daemon.poll() is None:
                os.killpg(daemon.pid, signal.SIGTERM)
                _ = daemon.wait(timeout=2)

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
            ["bash", str(self.fixture.watcher), "--daemon"],
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

    def _make_filtered_git_vault(self) -> Path:
        """Turn the fixture root into a vault wired exactly like the real one.

        A real repository, the real strip filter, and one committed issue note
        whose stored blob therefore carries no backlog_rank. That is the only
        setup under which the behaviour being tested even exists: the note has
        to be tracked, filtered, and clean before a rank write can make it look
        modified on stat data alone.
        """
        root = self.fixture.root
        issue = self.fixture.issues / "issue.md"
        _ = issue.write_text(
            "---\ntitle: sample\nstage: backlog\n---\n\nbody\n",
            encoding="utf-8",
        )
        _ = (root / ".gitattributes").write_text(
            "issues/*.md filter=hanadocs-strip-generated\n", encoding="utf-8"
        )

        def git(*arguments: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", "-C", str(root), *arguments],
                check=True,
                capture_output=True,
                text=True,
            )

        _ = git("init", "-q", "-b", "main")
        _ = git("config", "user.email", "watcher-test@example.invalid")
        _ = git("config", "user.name", "watcher test")
        _ = git("config", "commit.gpgsign", "false")
        _ = git(
            "config",
            "filter.hanadocs-strip-generated.clean",
            f"{PY_SHIM} {STRIP_GENERATED}",
        )
        # Only the notes and the attributes file. The fixture also litters this
        # directory with stub tools and cache/state dirs, which are scaffolding
        # rather than vault content and would otherwise show up as changes.
        _ = git("add", "issues", ".gitattributes")
        _ = git("commit", "-q", "-m", "seed")
        return issue

    def _vault_status(self) -> str:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(self.fixture.root),
                "status",
                "--porcelain",
                "--",
                "issues",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def _install_ranking_stubs(self, issue: Path, rank: str) -> None:
        """Snapshot reads semantic inputs only; renumber writes a derived rank.

        The split matters: a snapshot that saw the rank it just caused to be
        written would report its own output as a changed input and the watcher
        would loop, which is the same separation the real tools keep.
        """
        _ = self.fixture.snapshot.write_text(
            f'''#!/usr/bin/env python3
import argparse
import hashlib
from pathlib import Path

parser = argparse.ArgumentParser()
_ = parser.add_argument("--output", required=True)
_ = parser.add_argument("--require-complete", action="store_true")
args = parser.parse_args()
text = Path({str(issue)!r}).read_text(encoding="utf-8")
semantic = "".join(
    line for line in text.splitlines(keepends=True)
    if not line.startswith("backlog_rank:")
)
_ = Path(args.output).write_text(
    hashlib.sha256(semantic.encode("utf-8")).hexdigest() + "\\n", encoding="utf-8"
)
''',
            encoding="utf-8",
        )
        _ = self.fixture.renumber.write_text(
            f'''#!/usr/bin/env python3
import sys
from pathlib import Path

issue = Path({str(issue)!r})
if "--apply" in sys.argv:
    text = issue.read_text(encoding="utf-8")
    lines = [
        line for line in text.splitlines(keepends=True)
        if not line.startswith("backlog_rank:")
    ]
    lines.insert(1, "backlog_rank: {rank}\\n")
    _ = issue.write_text("".join(lines), encoding="utf-8")
raise SystemExit(0)
''',
            encoding="utf-8",
        )

    def test_ranking_writes_leave_git_status_quiet(self) -> None:
        issue = self._make_filtered_git_vault()
        self.assertEqual(self._vault_status(), "")
        self._install_ranking_stubs(issue, rank="900001")

        result = subprocess.run(
            ["bash", str(self.fixture.watcher)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)

        # The write really happened, so the note on disk no longer matches its
        # committed blob byte for byte. Without the refresh this is precisely
        # the state in which status reports the note as modified.
        self.assertIn("backlog_rank: 900001", issue.read_text(encoding="utf-8"))
        self.assertEqual(self._vault_status(), "")

        staged = subprocess.run(
            ["git", "-C", str(self.fixture.root), "diff", "--cached", "--name-only"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(staged.stdout, "", "the refresh must never stage content")

    def test_index_refresh_still_shows_a_real_edit(self) -> None:
        issue = self._make_filtered_git_vault()
        self._install_ranking_stubs(issue, rank="900002")

        # A genuine body change, of the kind the filter does not strip, made
        # before the watcher runs so the refresh has to decide about it.
        _ = issue.write_text(
            issue.read_text(encoding="utf-8") + "a real edit\n", encoding="utf-8"
        )

        result = subprocess.run(
            ["bash", str(self.fixture.watcher)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)

        self.assertIn(" M issues/issue.md", self._vault_status())

    def test_index_refresh_skipped_when_no_filter_configured(self) -> None:
        issue = self._make_filtered_git_vault()
        _ = subprocess.run(
            [
                "git",
                "-C",
                str(self.fixture.root),
                "config",
                "--unset",
                "filter.hanadocs-strip-generated.clean",
            ],
            check=True,
            capture_output=True,
        )
        self._install_ranking_stubs(issue, rank="900003")

        result = subprocess.run(
            ["bash", str(self.fixture.watcher)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)

        log = self.fixture.event_log.read_text(encoding="utf-8")
        self.assertIn("vault clean filter not configured", log)

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
            ["bash", str(self.fixture.watcher), "--daemon"],
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
    _ = unittest.main()
