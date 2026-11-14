"""Run a real pusher while another process owns its repository lock."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Literal
import unittest

from scripts.direct.wait_for_test_state import wait_until
from scripts.lib import main_push_lock
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


def lock_attempt_environment(root: Path, environment: Mapping[str, str]) -> dict[str, str]:
    """Observe the real lock attempt without changing the inherited marker."""
    _ = (root / "sitecustomize.py").write_text('''import fcntl
import os
from pathlib import Path
flock = fcntl.flock
def observe(file, operation):
    if operation == fcntl.LOCK_EX and Path(str(getattr(file, "name", ""))).name == "main-push.lock":
        Path(os.environ["MAIN_PUSH_TEST_ATTEMPT"]).write_text("attempted")
    return flock(file, operation)
fcntl.flock = observe
''')
    return {**environment, "MAIN_PUSH_TEST_ATTEMPT": str(root / "attempted"),
            "PYTHONPATH": str(Path(__file__).resolve().parent / "python_startup") + os.pathsep + str(root)
            + os.pathsep + environment.get("PYTHONPATH", "")}


def child_lock_handoff(checkout: Path, handoff: Literal["explicit", "plain"]) -> int:
    """Exercise nested holds and child handoff inside a bounded test process."""
    case = unittest.TestCase()
    timeout = min(TEST_HANG_GUARD_SECS, 5)
    command = [sys.executable, "-m", "scripts.lib.main_push_lock", "--checkout", str(checkout),
               "--", sys.executable, "-c", "raise SystemExit(23)"]
    previous = os.environ.get("MAIN_PUSH_LOCK_HELD")
    with tempfile.TemporaryDirectory(prefix="main-push-handoff-") as temporary:
        root = Path(temporary)
        attempted = root / "attempted"
        with main_push_lock.main_push_lock(checkout):
            with main_push_lock.main_push_lock(checkout):
                environment = (main_push_lock.held_environment(checkout) if handoff == "explicit"
                               else lock_attempt_environment(root, os.environ))
                process = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True)
                try:
                    if handoff == "explicit":
                        output, errors = process.communicate(timeout=timeout)
                        case.assertEqual(process.returncode, 23, output + errors)
                    else:
                        wait_until(lambda: attempted.exists() or process.poll() is not None,
                                   root, timeout=timeout, processes=(process,))
                        case.assertIsNone(process.poll(), "the plain child must wait for the holder")
                    case.assertEqual(os.environ.get("MAIN_PUSH_LOCK_HELD"), previous)
                except BaseException:
                    process.kill()
                    _ = process.communicate(timeout=timeout)
                    raise
        try:
            output, errors = process.communicate(timeout=timeout)
        except BaseException:
            process.kill()
            _ = process.communicate(timeout=timeout)
            raise
        case.assertEqual(process.returncode, 23, output + errors)
        case.assertEqual(os.environ.get("MAIN_PUSH_LOCK_HELD"), previous)
        return process.returncode


def publish_after_contender(case: unittest.TestCase, checkout: Path, origin: Path,
                            command: Sequence[str], environment: Mapping[str, str],
                            *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Confirm that the pusher leaves origin unchanged until the holder releases."""
    def git(repo: Path, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *arguments], env=environment,
                              capture_output=True, text=True, check=True,
                              timeout=TEST_HANG_GUARD_SECS).stdout.strip()

    common = Path(git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    before = git(origin, "rev-parse", "refs/heads/main")
    with tempfile.TemporaryDirectory(prefix="main-push-contender-") as temporary:
        root = Path(temporary)
        attempted = root / "attempted"
        child_environment = lock_attempt_environment(root, environment)
        _ = child_environment.pop("MAIN_PUSH_LOCK_HELD", None)
        with (common / "main-push.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process = subprocess.Popen(command, cwd=cwd, env=child_environment,
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            try:
                wait_until(lambda: attempted.exists() or process.poll() is not None, root)
                case.assertEqual(git(origin, "rev-parse", "refs/heads/main"), before,
                                 "the pusher changed main while another process held the lock")
                case.assertIsNone(process.poll(), "the pusher must wait for the holder")
            except BaseException as error:
                if process.poll() is None:
                    process.kill()
                output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                error.add_note(output + errors)
                raise
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        try:
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        except BaseException:
            process.kill()
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            raise
        case.assertEqual(process.returncode, 0, output + errors)
        return subprocess.CompletedProcess(command, process.returncode, output, errors)
