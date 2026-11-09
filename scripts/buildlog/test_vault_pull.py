"""Sync temporary vault repositories and file questions through a local stand-in."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import cast, override
from unittest import mock


from scripts.buildlog import ci
from scripts.buildlog import cli
from scripts.lib import conf
from scripts.buildlog import sync
from scripts.buildlog import vault_pull
from scripts.buildlog.test_index import point_root_at
from scripts.lib.git_test_support import commit_repository, write_fixture_git_config


QUESTION_STAND_IN = '''import json, pathlib, sys
folder = pathlib.Path(__file__).parent
args = sys.argv[1:]
with (folder / "attempts.jsonl").open("a") as stream:
    stream.write(json.dumps(args) + "\\n")
status = int((folder / ("withdraw_exit" if args[0] == "withdraw" else "question_exit")).read_text())
if status == 0 and args[0] == "ask":
    with (folder / "questions.jsonl").open("a") as stream:
        stream.write(json.dumps(args) + "\\n")
raise SystemExit(status)
'''

GIT_STAND_IN = '''#!/bin/sh
if [ "$3" = "$VAULT_TEST_OPERATION" ]; then
  count=0
  [ ! -f "$VAULT_TEST_COUNTER" ] || read -r count < "$VAULT_TEST_COUNTER"
  count=$((count + 1))
  printf '%s\\n' "$count" > "$VAULT_TEST_COUNTER"
  if [ "$count" -le "$VAULT_TEST_LOCK_FAILURES" ]; then
    echo "$VAULT_TEST_FAILURE_DETAIL" >&2
    exit 128
  fi
fi
exec "$VAULT_TEST_GIT" "$@"
'''


GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def option(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


class VaultPullTests(unittest.TestCase):
    folder: Path = Path()
    root: Path = Path()
    github: Path = Path()
    local: Path = Path()
    mac: Path = Path()
    now: float = 1_800_000_000.0
    unreachable: bool = False
    warm: bool = True
    reject_push: bool = False
    lock_operation: str = ""
    lock_failures: int = 0
    unknown_remote_error: bool = False
    lock_detail: str = "fatal: Unable to create 'rust/hanadocs/.git/index.lock': File exists"
    push_rejection: str = "[rejected]"
    template: Path = Path()

    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.commands: list[list[str]] = []
        self.github_update: dict[str, str] = {}

    @classmethod
    @override
    def setUpClass(cls) -> None:
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.template = Path(temporary.name)
        github = cls.template / "github.git"
        local = cls.template / "natedev"
        mac = cls.template / "mac"
        with mock.patch.dict(os.environ, {
            **GIT_IDENTITY, "GIT_CONFIG_GLOBAL": str(write_fixture_git_config(cls.template)),
        }):
            _ = cls.command(["git", "init", "--bare", "--initial-branch=main", str(github)])
            _ = cls.command(["git", "init", "--initial-branch=main", str(local)])
            _ = (local / "shared.md").write_text("base\n")
            _ = commit_repository(local, "base")
            _ = cls.command(["git", "-C", str(local), "remote", "add", "origin", str(github)])
            _ = cls.command(["git", "-C", str(local), "push", "-u", "origin", "main"])
            _ = cls.command(["git", "clone", str(github), str(mac)])

    @override
    def setUp(self) -> None:
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.folder / "buildlog"
        self.github = self.folder / "github.git"
        self.local = self.folder / "natedev"
        self.mac = self.folder / "mac-host/rust/hanadocs"
        self.mac.parent.mkdir(parents=True)
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.folder), "BUILDLOG_PEER": "fixture-mac",
            **GIT_IDENTITY,
            "GIT_CONFIG_GLOBAL": str(write_fixture_git_config(self.folder)),
        }))
        for name, destination in (("github.git", self.github), ("natedev", self.local), ("mac", self.mac)):
            _ = shutil.copytree(self.template / name, destination)
        for repository in (self.local, self.mac):
            _ = self.git(repository, "remote", "set-url", "origin", str(self.github))
        settings = self.folder / "config"
        settings.mkdir()
        production = Path(__file__).resolve().parents[2] / "config/build_targets.conf"
        _ = (settings / "build_targets.conf").write_text(
            "# whole: 0..10\nvault.lock_retries=2\n# range: 0..5\nvault.lock_retry_delay_s=0\n"
            + production.read_text())
        self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(settings)}))
        point_root_at(self, self.root)
        script = self.folder / "update_registration.py"
        _ = script.write_text(QUESTION_STAND_IN)
        _ = (self.folder / "question_exit").write_text("0")
        _ = (self.folder / "withdraw_exit").write_text("0")
        shim = self.folder / "bin"
        shim.mkdir()
        _ = (shim / "git").write_text(GIT_STAND_IN)
        (shim / "git").chmod(0o700)
        _ = (shim / "sleep").write_text("#!/bin/sh\nexit 0\n")
        (shim / "sleep").chmod(0o700)
        _ = self.enterContext(mock.patch.object(vault_pull, "LOCAL_VAULT", self.local))
        _ = self.enterContext(mock.patch.object(vault_pull, "REGISTRATION", script))
        _ = self.enterContext(mock.patch.object(time, "time", side_effect=lambda: self.now))
        _ = self.enterContext(mock.patch.object(vault_pull, "run", new=self.run_transport))
        _ = self.enterContext(mock.patch.object(ci, "warm", new=self.credentials_warm))

    @staticmethod
    def command(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, capture_output=True, text=True, check=True, timeout=10)

    def git(self, repo: Path, *args: str) -> str:
        return self.command(["git", "-C", str(repo), *args]).stdout.strip()

    def commit(self, repo: Path, files: dict[str, str]) -> str:
        for name, text in files.items():
            file = repo / name
            file.parent.mkdir(parents=True, exist_ok=True)
            _ = file.write_text(text)
        return commit_repository(repo, "fixture edits")

    def credentials_warm(self) -> bool:
        if self.github_update:
            publisher = self.folder / "publisher"
            _ = self.command(["git", "clone", str(self.github), str(publisher)])
            _ = self.commit(publisher, self.github_update)
            _ = self.git(publisher, "push", "origin", "main")
            self.github_update = {}
        return self.warm

    def run_transport(self, args: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
        self.assertEqual(timeout, vault_pull.COMMAND_TIMEOUT_S)
        self.commands.append(args)
        if args[0] == "ssh":
            if self.unreachable:
                return subprocess.CompletedProcess(args, 255, "", "Mac unreachable")
            if self.unknown_remote_error:
                return subprocess.CompletedProcess(args, 0, "VAULT_OUTCOME=failed\n", "fixture remote error")
            environment = {
                **os.environ, "PATH": str(self.folder / "bin") + os.pathsep + os.environ["PATH"],
                "VAULT_TEST_GIT": shutil.which("git") or "git",
                "VAULT_TEST_OPERATION": self.lock_operation,
                "VAULT_TEST_COUNTER": str(self.folder / "lock_count"),
                "VAULT_TEST_LOCK_FAILURES": str(self.lock_failures),
                "VAULT_TEST_FAILURE_DETAIL": self.lock_detail,
            }
            result = subprocess.run(["/bin/sh", "-c", args[-1]], cwd=self.mac.parents[1], env=environment,
                                    capture_output=True, text=True, check=False, timeout=timeout)
            # Mac login can report success after a failed remote command.
            return subprocess.CompletedProcess(args, 0, result.stdout, result.stderr)
        if args[0] == "git":
            self.assertEqual(args[2], str(self.local))
            if self.unreachable and "fixture-mac:rust/hanadocs" in args:
                return subprocess.CompletedProcess(args, 128, "", "ssh: connect: Connection refused")
            if self.reject_push and "push" in args and "origin" in args:
                return subprocess.CompletedProcess(args, 1, f"!\tmain:main\t{self.push_rejection} (fetch first)\n", "")
            args = [str(self.mac) if arg == "fixture-mac:rust/hanadocs" else arg for arg in args]
        else:
            self.assertEqual(args[:2], ["python3", str(self.folder / "update_registration.py")])
        return subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)

    def hour(self) -> tuple[int, str]:
        output = StringIO()
        with redirect_stdout(output):
            status = vault_pull.vault()
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        return status, lines[0]

    def questions(self, name: str = "questions.jsonl") -> list[list[str]]:
        file = self.folder / name
        return [cast(list[str], json.loads(line)) for line in file.read_text().splitlines()] if file.exists() else []

    def withdrawals(self) -> list[list[str]]:
        return [args for args in self.questions("attempts.jsonl") if args[0] == "withdraw"]

    def question(self) -> str:
        return option(self.questions()[-1], "--question")

    def conflict(self) -> tuple[str, str]:
        local = self.commit(self.local, {"shared.md": "natedev edit\n"})
        _ = self.git(self.local, "push", "origin", "main")
        mac = self.commit(self.mac, {"shared.md": "Mac edit\n"})
        return local, mac

    def test_mac_commits_tracked_and_untracked_edits_and_publishes_both(self) -> None:
        _ = (self.mac / "shared.md").write_text("Mac edit\n")
        _ = (self.mac / "note.md").write_text("new note\n")
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(self.git(self.github, "show", "main:shared.md"), "Mac edit")
        self.assertEqual(self.git(self.github, "show", "main:note.md"), "new note")
        self.assertEqual(self.git(self.mac, "log", "-1", "--format=%s"), "vault: Mac edits")
        self.assertEqual(self.git(self.mac, "status", "--porcelain"), "")
        self.assertEqual(self.questions(), [])

    def test_different_files_merge_on_the_mac_and_reach_github(self) -> None:
        local = self.commit(self.local, {"from-natedev.md": "natedev\n"})
        _ = self.git(self.local, "push", "origin", "main")
        mac = self.commit(self.mac, {"from-mac.md": "Mac\n"})
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(len(self.git(self.mac, "rev-list", "--parents", "-n", "1", "HEAD").split()), 3)
        for ancestor in (local, mac):
            _ = self.git(self.github, "merge-base", "--is-ancestor", ancestor, "main")
        self.assertEqual(self.git(self.github, "show", "main:from-mac.md"), "Mac")
        self.assertEqual(self.git(self.github, "show", "main:from-natedev.md"), "natedev")

    def test_concurrent_github_edits_merge_in_the_natedev_object_store(self) -> None:
        _ = (self.mac / "mac.md").write_text("Mac\n")
        self.github_update = {"github.md": "GitHub\n"}
        before = self.git(self.local, "rev-parse", "HEAD")
        _ = (self.local / "shared.md").write_text("uncommitted natedev edit\n")
        _ = (self.local / "staged.md").write_text("staged natedev edit\n")
        _ = self.git(self.local, "add", "staged.md")
        index_before = (self.local / ".git/index").read_bytes()
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), before)
        self.assertEqual((self.local / "shared.md").read_text(), "uncommitted natedev edit\n")
        self.assertEqual((self.local / ".git/index").read_bytes(), index_before)
        self.assertEqual(self.git(self.github, "show", "main:mac.md"), "Mac")
        self.assertEqual(self.git(self.github, "show", "main:github.md"), "GitHub")
        self.assertEqual(len(self.git(self.github, "rev-list", "--parents", "-n", "1", "main").split()), 3)

    def test_same_lines_keep_both_commits_abort_the_mac_merge_and_file_one_question(self) -> None:
        local, mac = self.conflict()
        status, line = self.hour()
        self.assertEqual(status, 0)
        self.assertIn("stopped:", line)
        self.assertEqual(self.git(self.github, "rev-parse", "main"), local)
        self.assertEqual(self.git(self.mac, "rev-parse", "HEAD"), mac)
        self.assertEqual(self.git(self.mac, "status", "--porcelain"), "")
        self.assertFalse((self.mac / ".git/MERGE_HEAD").exists())
        self.assertEqual(self.questions(), [])
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(len(self.questions()), 1)
        self.assertIn("shared.md", self.question())
        self.assertIn("In a terminal on the Mac", self.question())
        self.assertIn("I recommend choice 1", self.question())
        self.assertFalse(any("push" in args and "origin" in args for args in self.commands))
        self.assertFalse(any("pushover" in " ".join(args) or "send.py" in " ".join(args) for args in self.commands))

    def test_open_mac_merge_files_a_conflict_question_without_changing_the_commit_or_file(self) -> None:
        local, mac = self.conflict()
        _ = self.git(self.mac, "fetch", "origin", "main")
        merged = subprocess.run(["git", "-C", str(self.mac), "merge", "--no-edit", "origin/main"],
                                capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(merged.returncode, 1)
        text = (self.mac / "shared.md").read_text()
        self.assertIn("<<<<<<< HEAD", text)
        self.assertEqual(self.git(self.mac, "rev-parse", "MERGE_HEAD"), local)
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(self.questions(), [])

        for staged in (False, True):
            with self.subTest(staged=staged):
                if staged:
                    _ = self.git(self.mac, "add", "shared.md")
                status = self.git(self.mac, "status", "--porcelain")
                self.assertEqual(self.hour()[0], 0)
                self.assertEqual(len(self.questions()), 1)
                self.assertIn("shared.md", self.question())
                self.assertIn("In a terminal on the Mac", self.question())
                self.assertEqual(self.git(self.mac, "rev-parse", "HEAD"), mac)
                self.assertEqual(self.git(self.mac, "rev-parse", "MERGE_HEAD"), local)
                self.assertEqual((self.mac / "shared.md").read_text(), text)
                self.assertEqual(self.git(self.mac, "status", "--porcelain"), status)
                self.assertEqual(self.git(self.github, "rev-parse", "main"), local)

    def test_natedev_conflict_defers_until_the_mac_files_the_conflict_question(self) -> None:
        _ = (self.mac / "shared.md").write_text("Mac\n")
        self.github_update = {"shared.md": "GitHub\n"}
        head = self.git(self.local, "rev-parse", "HEAD")
        outage = vault_pull.OpenOutage("command_failed", self.now - 3600, False, "previous error")
        vault_pull.write_state(vault_pull.VaultState(vault_pull.NeverPulled(), outage))
        self.assertEqual(self.hour(), (0, "buildlog vault: GitHub main conflicts with the Mac; the next hour merges it on the Mac"))
        self.assertEqual(self.questions(), [])
        self.assertEqual(vault_pull.read_state().last_good, vault_pull.LastGoodPull(self.now))
        self.assertEqual(vault_pull.read_state().outage, outage)
        self.now += 3600
        self.assertEqual(self.hour(), (0, "buildlog vault: stopped: The hanadocs merge has a conflict on Mac. Files: shared.md."))
        self.assertEqual(len(self.questions()), 1)
        self.assertIn("conflict on Mac", self.question())
        self.assertIn("shared.md", self.question())
        self.assertIn("In a terminal on the Mac", self.question())
        self.assertEqual(self.git(self.local, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(self.mac, "show", "HEAD:shared.md"), "Mac")
        self.assertEqual(self.git(self.github, "show", "main:shared.md"), "GitHub")
        self.assertFalse((self.local / ".git/MERGE_HEAD").exists())

    def test_a_second_stop_in_one_outage_files_no_second_question(self) -> None:
        for outcome in (vault_pull.VaultConflict(("shared.md",)), vault_pull.NotOnMain(),
                        vault_pull.VaultLocked(self.lock_detail), vault_pull.MacUnreachable(),
                        vault_pull.VaultCommandFailed("fixture command error")):
            with self.subTest(outcome=outcome):
                for name in ("questions.jsonl", "attempts.jsonl"):
                    (self.folder / name).unlink(missing_ok=True)
                vault_pull.write_state(vault_pull.VaultState(
                    vault_pull.LastGoodPull(self.now - vault_pull.UNREACHABLE_GRACE_S), vault_pull.NoOutage()))
                status = 1 if isinstance(outcome, vault_pull.VaultCommandFailed) else 0
                with mock.patch.object(vault_pull, "pull", return_value=outcome):
                    self.assertEqual(self.hour()[0], status)
                    self.assertEqual(self.questions("attempts.jsonl"), [])
                    outage = vault_pull.read_state().outage
                    assert isinstance(outage, vault_pull.OpenOutage)
                    self.assertFalse(outage.asked)
                    self.now += 3600
                    self.assertEqual(self.hour()[0], status)
                    self.assertEqual(len(self.questions()), 1)
                    self.now += 3600
                    self.assertEqual(self.hour()[0], status)
                self.assertEqual(len(self.questions()), 1)
                self.assertEqual(len(self.questions("attempts.jsonl")), 1)

    def test_a_good_run_closes_the_outage_and_a_later_stop_files_a_new_question(self) -> None:
        _ = self.conflict()
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(self.hour()[0], 0)
        first = self.questions()[0]
        _ = self.git(self.mac, "merge", "-s", "ours", "--no-edit", "origin/main")
        self.assertEqual(self.hour()[0], 0)
        self.assertIsInstance(vault_pull.read_state().outage, vault_pull.NoOutage)
        withdrawals = self.withdrawals()
        self.assertEqual(len(withdrawals), 1)
        self.assertEqual(withdrawals[0], [
            "withdraw", "--production", option(first, "--production"),
            "--item", option(first, "--text"),
        ])
        _ = self.git(self.mac, "switch", "-c", "feature")
        self.now += 3600
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(len(self.questions()), 2)
        second = self.questions()[1]
        self.assertNotEqual(option(second, "--text"), option(first, "--text"))
        _ = self.git(self.mac, "switch", "main")
        self.assertEqual(self.hour()[0], 0)
        self.assertIsInstance(vault_pull.read_state().outage, vault_pull.NoOutage)
        withdrawals = self.withdrawals()
        self.assertEqual(len(withdrawals), 2)
        self.assertEqual(option(withdrawals[1], "--item"), option(second, "--text"))

    def test_a_changed_cause_withdraws_the_original_question_text(self) -> None:
        _ = self.conflict()
        _ = self.git(self.mac, "switch", "-c", "feature")
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(self.hour()[0], 0)
        first = self.questions()[0]
        _ = self.git(self.mac, "switch", "main")
        self.now += 3600
        self.assertEqual(self.hour()[0], 0)
        outage = vault_pull.read_state().outage
        assert isinstance(outage, vault_pull.OpenOutage)
        self.assertEqual(outage.cause, "conflict")
        self.assertEqual(len(self.questions()), 1)
        _ = self.git(self.mac, "merge", "-s", "ours", "--no-edit", "origin/main")
        self.assertEqual(self.hour()[0], 0)
        withdrawals = self.withdrawals()
        self.assertEqual(len(withdrawals), 1)
        self.assertEqual(option(withdrawals[0], "--item"), option(first, "--text"))
        self.assertIsInstance(vault_pull.read_state().outage, vault_pull.NoOutage)

    def test_a_good_run_inside_the_unreachable_grace_withdraws_no_question(self) -> None:
        self.unreachable = True
        self.assertEqual(self.hour()[0], 0)
        outage = vault_pull.read_state().outage
        assert isinstance(outage, vault_pull.OpenOutage)
        self.assertFalse(outage.asked)
        self.unreachable = False
        self.now += 3600
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(self.questions("attempts.jsonl"), [])
        self.assertIsInstance(vault_pull.read_state().outage, vault_pull.NoOutage)

        for outcome in (vault_pull.VaultConflict(("shared.md",)), vault_pull.NotOnMain(),
                        vault_pull.VaultLocked(self.lock_detail), vault_pull.VaultCommandFailed("fixture error")):
            with self.subTest(outcome=outcome):
                with mock.patch.object(vault_pull, "pull", side_effect=[outcome, vault_pull.Current()]):
                    status, line = self.hour()
                    self.assertEqual(status, 1 if isinstance(outcome, vault_pull.VaultCommandFailed) else 0)
                    self.assertIn("stopped:", line)
                    outage = vault_pull.read_state().outage
                    assert isinstance(outage, vault_pull.OpenOutage)
                    self.assertFalse(outage.asked)
                    self.assertEqual(self.hour(), (0, "buildlog vault: current"))
                self.assertEqual(self.questions("attempts.jsonl"), [])
                self.assertIsInstance(vault_pull.read_state().outage, vault_pull.NoOutage)

    def assert_withdraw_does_not_retry(self) -> None:
        self.assertIsInstance(vault_pull.read_state().outage, vault_pull.NoOutage)
        attempts = self.questions("attempts.jsonl")
        self.assertEqual(len(self.withdrawals()), 1)
        self.assertEqual(self.hour(), (0, "buildlog vault: current"))
        self.assertEqual(self.questions("attempts.jsonl"), attempts)

    def test_a_failed_withdraw_keeps_the_good_result_and_does_not_retry(self) -> None:
        _ = self.git(self.mac, "switch", "-c", "feature")
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(self.hour()[0], 0)
        _ = self.git(self.mac, "switch", "main")
        _ = (self.folder / "withdraw_exit").write_text("2")
        self.assertEqual(self.hour(), (0, "buildlog vault: current; the question was not withdrawn"))
        self.assertEqual(len(self.questions()), 1)
        self.assert_withdraw_does_not_retry()

    def test_a_timed_out_withdraw_keeps_the_good_result_and_does_not_retry(self) -> None:
        _ = self.conflict()
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(self.hour()[0], 0)
        _ = self.git(self.mac, "merge", "-s", "ours", "--no-edit", "origin/main")

        def timed_out_withdraw(args: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
            result = self.run_transport(args, timeout)
            if "withdraw" in args:
                self.assertIsInstance(vault_pull.read_state().outage, vault_pull.NoOutage)
                return None
            return result

        with mock.patch.object(vault_pull, "run", new=timed_out_withdraw):
            self.assertEqual(self.hour(), (0, "buildlog vault: synced 0 incoming commits; the question was not withdrawn"))
        self.assert_withdraw_does_not_retry()

    def test_a_deferral_keeps_the_question_until_the_first_good_sync(self) -> None:
        _ = self.conflict()
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(self.hour()[0], 0)
        outage = vault_pull.read_state().outage
        attempts = self.questions("attempts.jsonl")
        _ = self.git(self.mac, "merge", "-s", "ours", "--no-edit", "origin/main")
        self.warm = False
        self.assertEqual(self.hour(), (0, "buildlog vault: GitHub credentials are cold; the next hour retries"))
        self.assertEqual(vault_pull.read_state().outage, outage)
        self.assertEqual(self.questions("attempts.jsonl"), attempts)
        self.warm = True
        self.assertEqual(self.hour()[0], 0)
        withdrawals = self.withdrawals()
        self.assertEqual(len(withdrawals), 1)
        first = self.questions()[0]
        self.assertEqual(option(withdrawals[0], "--item"), option(first, "--text"))
        self.assertIsInstance(vault_pull.read_state().outage, vault_pull.NoOutage)

    def test_not_on_main_files_one_question_without_committing_edits(self) -> None:
        _ = self.git(self.mac, "switch", "-c", "feature")
        _ = (self.mac / "note.md").write_text("keep\n")
        head = self.git(self.mac, "rev-parse", "HEAD")
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(self.git(self.mac, "rev-parse", "HEAD"), head)
        self.assertEqual(len(self.questions()), 1)
        self.assertIn("not on main", self.question())
        self.assertIn("In a terminal on the Mac", self.question())

    def test_unreachable_window_files_one_question_at_twenty_four_hours(self) -> None:
        self.assertEqual(self.hour()[0], 0)
        self.unreachable = True
        self.assertEqual(self.hour(), (0, "buildlog vault: stopped: Mac unreachable; the next hour retries"))
        self.now += vault_pull.UNREACHABLE_GRACE_S - 1
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(self.questions(), [])
        self.now += 1
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(len(self.questions()), 1)
        self.assertIn("24 hours", self.question())
        self.assertIn("Mac", self.question())

    def test_unreachable_without_a_good_run_uses_the_outage_start(self) -> None:
        self.unreachable = True
        self.assertEqual(self.hour()[0], 0)
        self.now += vault_pull.UNREACHABLE_GRACE_S
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(len(self.questions()), 1)

    def test_failed_or_held_questions_retry_until_the_command_succeeds(self) -> None:
        _ = self.conflict()
        _ = (self.folder / "question_exit").write_text("2")
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(self.questions("attempts.jsonl"), [])
        self.assertEqual(self.hour()[0], 1)
        outage = vault_pull.read_state().outage
        assert isinstance(outage, vault_pull.OpenOutage)
        self.assertFalse(outage.asked)
        _ = (self.folder / "question_exit").write_text("0")
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(len(self.questions()), 1)
        attempts = self.questions("attempts.jsonl")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(len(self.questions("attempts.jsonl")), 2)

    def test_question_command_names_the_cause_date_production_and_urgency(self) -> None:
        _ = self.conflict()
        _ = self.git(self.mac, "switch", "-c", "feature")
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(self.questions(), [])
        _ = self.git(self.mac, "switch", "main")
        self.assertEqual(self.hour()[0], 0)
        args = self.questions()[0]
        self.assertEqual(args[0], "ask")
        self.assertEqual(option(args, "--production"), "natedev")
        self.assertIn("conflict", option(args, "--question"))
        self.assertIn("Combine the edits and commit the result", option(args, "--question"))
        self.assertNotIn("Keep the current branch", option(args, "--question"))
        self.assertEqual(option(args, "--text"), "Hanadocs vault sync stopped, 2027-01-15T08:00:00+00:00")
        self.assertEqual(option(args, "--urgency"), "soon")
        self.assertEqual(option(args, "--due"), "2027-01-16")
        settings = self.folder / "config/build_targets.conf"
        _ = settings.write_text("vault.question_production=fixture-production\n" + settings.read_text())
        _ = self.git(self.mac, "merge", "-s", "ours", "--no-edit", "origin/main")
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(option(self.withdrawals()[0], "--production"), "fixture-production")

    def test_cold_credentials_defer_without_a_question_or_an_origin_fetch(self) -> None:
        self.warm = False
        _ = (self.mac / "note.md").write_text("Mac\n")
        self.assertEqual(self.hour(), (0, "buildlog vault: GitHub credentials are cold; the next hour retries"))
        self.assertEqual(self.questions(), [])
        self.assertEqual(vault_pull.read_state(), vault_pull.VaultState(vault_pull.LastGoodPull(self.now), vault_pull.NoOutage()))
        self.assertFalse(any("fetch" in args and "origin" in args for args in self.commands))
        self.assertFalse(any("push" in args and "origin" in args for args in self.commands))
        self.assertEqual(self.git(self.mac, "show", "HEAD:note.md"), "Mac")

    def test_rejected_github_push_defers_and_the_next_run_publishes(self) -> None:
        self.reject_push = True
        _ = (self.mac / "note.md").write_text("Mac\n")
        outage = vault_pull.OpenOutage("not_on_main", self.now - 3600, True, "previous question")
        vault_pull.write_state(vault_pull.VaultState(vault_pull.NeverPulled(), outage))
        self.assertEqual(self.hour(), (0, "buildlog vault: GitHub main changed; the next hour retries"))
        self.assertEqual(vault_pull.read_state(), vault_pull.VaultState(vault_pull.LastGoodPull(self.now), outage))
        self.assertEqual(self.questions(), [])
        self.reject_push = False
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(self.git(self.github, "show", "main:note.md"), "Mac")

    def test_unknown_remote_errors_keep_exit_one_after_the_question_is_filed(self) -> None:
        self.unknown_remote_error = True
        self.assertEqual(self.hour()[0], 1)
        self.assertEqual(self.hour()[0], 1)
        self.assertEqual(len(self.questions()), 1)
        self.assertIn("fixture remote error", self.question())
        self.assertIn("error on Mac", self.question())
        self.assertIn("In the natedev session on natedev", self.question())

    def locked_hour(self, operation: str, failures: int) -> tuple[int, str]:
        self.lock_operation = operation
        self.lock_failures = failures
        (self.folder / "lock_count").unlink(missing_ok=True)
        _ = (self.mac / "note.md").write_text(operation + "\n")
        return self.hour()

    def test_commit_and_merge_retry_index_locks_and_then_complete(self) -> None:
        remote_errors: list[str] = []

        def captured_transport(args: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
            result = self.run_transport(args, timeout)
            if args[0] == "ssh" and result is not None:
                remote_errors.append(result.stderr)
            return result

        for operation in ("commit", "merge"):
            with self.subTest(operation=operation):
                with mock.patch.object(vault_pull, "run", new=captured_transport):
                    self.assertEqual(self.locked_hour(operation, 2)[0], 0)
                self.assertNotIn(self.lock_detail, remote_errors[-1])
                self.assertEqual((self.folder / "lock_count").read_text().strip(), "3")
                self.assertEqual(self.git(self.github, "show", "main:note.md"), operation)
                self.assertEqual(self.questions(), [])

    def test_exhausted_commit_and_merge_locks_file_a_question(self) -> None:
        settings = self.folder / "config/build_targets.conf"
        _ = settings.write_text(settings.read_text().replace("vault.lock_retries=2", "vault.lock_retries=5"))
        for operation in ("commit", "merge"):
            with self.subTest(operation=operation):
                vault_pull.write_state(vault_pull.VaultState(vault_pull.NeverPulled(), vault_pull.NoOutage()))
                questions_before = len(self.questions())
                status, line = self.locked_hour(operation, 6)
                self.assertEqual(status, 0)
                self.assertEqual(len(self.questions()), questions_before)
                self.assertEqual(line.count(self.lock_detail), 1)
                self.assertEqual((self.folder / "lock_count").read_text().strip(), "6")
                self.assertEqual(self.locked_hour(operation, 6)[0], 0)
                self.assertEqual((self.folder / "lock_count").read_text().strip(), "6")
                self.assertIn("index.lock", self.question())
                self.assertEqual(self.question().count(self.lock_detail), 1)
        self.assertEqual(len(self.questions()), 2)

    def test_vault_retry_delay_limit_stays_below_one_minute(self) -> None:
        production = Path(__file__).resolve().parents[2] / "config/build_targets.conf"
        for settings in (production, self.folder / "config/build_targets.conf"):
            with self.subTest(settings=settings):
                conf.check(settings, "vault.lock_retries", "10")
                conf.check(settings, "vault.lock_retry_delay_s", "5")
                with self.assertRaises(conf.Unusable):
                    conf.check(settings, "vault.lock_retry_delay_s", "5.1")
                with self.assertRaises(conf.Unusable):
                    conf.check(settings, "vault.lock_retries", "11")

    def test_ignored_file_is_preserved_by_the_real_git_merge(self) -> None:
        _ = self.commit(self.local, {"protected.txt": "incoming\n"})
        _ = self.git(self.local, "push", "origin", "main")
        _ = (self.mac / ".git/info/exclude").write_text("protected.txt\n")
        _ = (self.mac / "protected.txt").write_text("keep this ignored file\n")
        base = self.git(self.mac, "rev-parse", "HEAD")
        outcome = vault_pull.pull()
        self.assertIsInstance(outcome, vault_pull.VaultCommandFailed)
        assert isinstance(outcome, vault_pull.VaultCommandFailed)
        self.assertIn("would be overwritten by merge", outcome.detail)
        self.assertIn("protected.txt", outcome.detail)
        self.assertEqual((self.mac / "protected.txt").read_text(), "keep this ignored file\n")
        self.assertEqual(self.git(self.mac, "rev-parse", "HEAD"), base)
        status, line = self.hour()
        self.assertEqual(status, 1)
        self.assertIn("would be overwritten by merge", line)
        self.assertEqual(self.questions(), [])
        self.assertEqual(self.hour()[0], 1)
        self.assertIn("protected.txt", self.question())

    def test_no_state_save_after_a_filed_question_can_make_the_next_hour_repeat_it(self) -> None:
        _ = self.conflict()
        self.assertEqual(self.hour()[0], 0)
        save = vault_pull.write_state

        def fail_after_question(state: vault_pull.VaultState) -> None:
            if self.questions():
                raise OSError("state disk unavailable after delivery")
            save(state)

        with mock.patch.object(vault_pull, "write_state", side_effect=fail_after_question):
            self.assertEqual(self.hour()[0], 0)
        self.assertEqual(vault_pull.read_state().outage, vault_pull.OpenOutage(
            "conflict", self.now, True, vault_pull.stop_details(vault_pull.VaultConflict(("shared.md",)))[1]))
        self.now += 3600
        _ = self.git(self.mac, "switch", "-c", "feature")
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(len(self.questions()), 1)
        self.assertEqual(len(self.questions("attempts.jsonl")), 1)
        outage = vault_pull.read_state().outage
        assert isinstance(outage, vault_pull.OpenOutage)
        self.assertEqual(outage.cause, "not_on_main")
        self.assertTrue(outage.asked)

    def test_a_failed_pre_question_state_save_does_not_run_ask(self) -> None:
        _ = self.conflict()
        self.assertEqual(self.hour()[0], 0)
        save = vault_pull.write_state

        def fail_question_intent(state: vault_pull.VaultState) -> None:
            if isinstance(state.outage, vault_pull.OpenOutage) and state.outage.asked:
                raise OSError("cannot save question intent")
            save(state)

        with mock.patch.object(vault_pull, "write_state", side_effect=fail_question_intent):
            with self.assertRaises(OSError):
                _ = self.hour()
        self.assertEqual(self.questions(), [])
        self.assertEqual(self.questions("attempts.jsonl"), [])

    def test_days_of_cold_credentials_keep_the_next_unreachable_hour_inside_the_grace(self) -> None:
        self.assertEqual(self.hour()[0], 0)
        outage = vault_pull.OpenOutage("not_on_main", self.now, True, "previous question")
        vault_pull.write_state(vault_pull.VaultState(vault_pull.LastGoodPull(self.now), outage))
        self.warm = False
        for day in (1, 2, 3):
            with self.subTest(day=day):
                self.now += 24 * 3600
                self.assertEqual(self.hour()[0], 0)
                self.assertEqual(vault_pull.read_state(), vault_pull.VaultState(vault_pull.LastGoodPull(self.now), outage))
        last_good = self.now
        self.unreachable = True
        self.now += 3600
        self.assertEqual(self.hour(), (0, "buildlog vault: stopped: Mac unreachable; the next hour retries"))
        self.assertEqual(self.questions(), [])
        self.assertEqual(vault_pull.read_state().last_good, vault_pull.LastGoodPull(last_good))

    def test_mac_transfer_succeeds_with_a_connection_warning_on_stderr(self) -> None:
        _ = self.commit(self.local, {"note.md": "incoming note\n"})
        _ = self.git(self.local, "push", "origin", "main")
        hook = self.mac / ".git/hooks/post-receive"
        _ = hook.write_text("#!/bin/sh\nprintf 'connection refused\\n' >&2\n")
        hook.chmod(0o700)

        result = vault_pull.mac_git("push", "fixture-mac:rust/hanadocs",
                                    f"{vault_pull.ORIGIN_MAIN}:{vault_pull.ORIGIN_MAIN}")

        self.assertIsInstance(result, subprocess.CompletedProcess)
        assert isinstance(result, subprocess.CompletedProcess)
        self.assertEqual(result.returncode, 0)
        self.assertIn("connection refused", result.stderr)
        self.assertEqual(self.git(self.mac, "show", "origin/main:note.md"), "incoming note")

    def test_mac_push_and_fetch_share_the_unreachable_rule(self) -> None:
        transport = self.run_transport
        for operation in ("push", "fetch"):
            for status, detail in ((255, ""), (128, "connection timed out"), (128, "connection refused"),
                                   (128, "could not resolve hostname"), (128, "no route to host"),
                                   (128, "network is unreachable"), (128, "connection closed"),
                                   (128, "permission denied (publickey")):
                with self.subTest(operation=operation, status=status, detail=detail):
                    def disconnected(args: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
                        if operation in args and "fixture-mac:rust/hanadocs" in args:
                            return subprocess.CompletedProcess(args, status, "", detail)
                        return transport(args, timeout)

                    with mock.patch.object(vault_pull, "run", new=disconnected):
                        self.assertIsInstance(vault_pull.pull(), vault_pull.MacUnreachable)
                        self.assertEqual(self.hour()[0], 0)
                    self.assertEqual(self.questions(), [])

    def test_timed_out_mac_push_script_and_fetch_are_unreachable(self) -> None:
        transport = self.run_transport
        for operation in ("push", "ssh", "fetch"):
            with self.subTest(operation=operation):
                def timed_out(args: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
                    if args[0] == operation or (operation in args and "fixture-mac:rust/hanadocs" in args):
                        return None
                    return transport(args, timeout)

                with mock.patch.object(vault_pull, "run", new=timed_out):
                    self.assertIsInstance(vault_pull.pull(), vault_pull.MacUnreachable)
                    self.assertEqual(self.hour()[0], 0)
                self.assertEqual(self.questions(), [])

    def test_zero_retries_stops_after_the_first_busy_lock(self) -> None:
        settings = self.folder / "config/build_targets.conf"
        _ = settings.write_text(settings.read_text().replace("vault.lock_retries=2", "vault.lock_retries=0"))
        self.assertEqual(self.locked_hour("merge", 3)[0], 0)
        self.assertEqual((self.folder / "lock_count").read_text().strip(), "1")
        outage = vault_pull.read_state().outage
        assert isinstance(outage, vault_pull.OpenOutage)
        self.assertEqual(outage.cause, "vault_locked")

    def test_other_merge_errors_do_not_retry(self) -> None:
        for detail in ("fatal: fixture remote failure", "fatal: cannot read index.lock", "fatal: other.lock: File exists"):
            with self.subTest(detail=detail):
                self.lock_detail = detail
                self.assertEqual(self.locked_hour("merge", 3)[0], 1)
                self.assertEqual((self.folder / "lock_count").read_text().strip(), "1")
                outage = vault_pull.read_state().outage
                assert isinstance(outage, vault_pull.OpenOutage)
                self.assertEqual(outage.cause, "command_failed")
                self.assertIn(detail, outage.detail)
                self.assertEqual(outage.detail.count(detail), 1)

    def test_mac_transport_options_do_not_apply_to_github(self) -> None:
        _ = (self.mac / "note.md").write_text("Mac\n")
        self.assertEqual(self.hour()[0], 0)
        for args in self.commands:
            if args[0] == "git":
                with self.subTest(command=args):
                    has_transport = any(arg.startswith("core.sshCommand=") for arg in args)
                    self.assertEqual(has_transport, "fixture-mac:rust/hanadocs" in args)
                    if "origin" in args and "push" in args:
                        self.assertIn("--porcelain", args)

    def test_remote_rejected_push_is_a_command_failure(self) -> None:
        self.reject_push = True
        self.push_rejection = "[remote rejected]"
        _ = (self.mac / "note.md").write_text("Mac\n")
        self.assertEqual(self.hour()[0], 1)
        self.assertEqual(self.hour()[0], 1)
        self.assertEqual(len(self.questions()), 1)
        self.assertIn("[remote rejected]", self.question())
        self.assertEqual(vault_pull.read_state().outage, vault_pull.OpenOutage(
            "command_failed", self.now, True,
            "The hanadocs sync has an unknown command error on natedev. Detail: stdout: !\tmain:main\t[remote rejected] (fetch first)"))

    def test_rejection_text_without_a_porcelain_rejected_line_is_a_command_failure(self) -> None:
        _ = (self.mac / "note.md").write_text("Mac\n")
        transport = self.run_transport
        for output, detail in (("", "[rejected] non-fast-forward; fetch first"),
                               ("error: [rejected]", "")):
            with self.subTest(output=output, detail=detail):
                def rejected(args: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
                    if "push" in args and "origin" in args:
                        return subprocess.CompletedProcess(args, 1, output, detail)
                    return transport(args, timeout)

                with mock.patch.object(vault_pull, "run", new=rejected):
                    self.assertIsInstance(vault_pull.pull(), vault_pull.VaultCommandFailed)

    def test_command_failures_keep_both_output_streams_and_the_machine(self) -> None:
        transport = self.run_transport
        for operation in ("fetch", "commit-tree"):
            with self.subTest(operation=operation):
                def failed(args: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
                    if operation in args and ("origin" in args or operation == "commit-tree"):
                        return subprocess.CompletedProcess(args, 128, "fixture stdout", "fixture stderr")
                    return transport(args, timeout)

                if operation == "commit-tree":
                    _ = (self.mac / "mac.md").write_text("Mac\n")
                    self.github_update = {"github.md": "GitHub\n"}
                with mock.patch.object(vault_pull, "run", new=failed):
                    outcome = vault_pull.pull()
                self.assertEqual(outcome, vault_pull.VaultCommandFailed("stderr: fixture stderr\nstdout: fixture stdout"))
                assert isinstance(outcome, vault_pull.VaultCommandFailed)
                self.assertIn("error on natedev", vault_pull.stop_details(outcome)[1])

    def test_each_stop_cause_survives_an_atomic_state_write(self) -> None:
        for cause in vault_pull.STOP_CAUSES:
            with self.subTest(cause=cause):
                state = vault_pull.VaultState(vault_pull.LastGoodPull(self.now),
                                             vault_pull.OpenOutage(cause, self.now, True, "saved detail"))
                vault_pull.write_state(state)
                self.assertEqual(vault_pull.read_state(), state)
                self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_outage_lock_covers_every_transport_call(self) -> None:
        transport = self.run_transport

        def locked_transport(args: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
            with vault_pull.state_path().with_suffix(".lock").open("a+b") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return transport(args, timeout)

        with mock.patch.object(vault_pull, "run", new=locked_transport):
            self.assertEqual(self.hour()[0], 0)

    def test_pause_preserves_the_outage_and_makes_no_transport_calls(self) -> None:
        _ = self.conflict()
        self.assertEqual(self.hour()[0], 0)
        state = vault_pull.read_state()
        self.commands.clear()
        with redirect_stdout(StringIO()):
            self.assertEqual(sync.pause("fixture hold"), 0)
        self.assertEqual(self.hour(), (0, "buildlog vault: paused (fixture hold); buildlog sync resume ends it"))
        self.assertEqual(self.commands, [])
        self.assertEqual(vault_pull.read_state(), state)

    def test_contact_holds_the_same_lock_as_sync_pause(self) -> None:
        transport = self.run_transport

        def locked_transport(args: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
            with (self.root / sync.LOCK_NAME).open("a+b") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return transport(args, timeout)

        with mock.patch.object(vault_pull, "run", new=locked_transport):
            self.assertEqual(self.hour()[0], 0)

    def test_old_alerted_state_is_not_a_filed_question(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _ = vault_pull.state_path().write_text(json.dumps({
            "last_good": self.now - 3600,
            "outage": {"cause": "tracked_files_changed", "since": self.now, "alerted": True},
        }))
        state = vault_pull.read_state()
        self.assertEqual(state.last_good, vault_pull.LastGoodPull(self.now - 3600))
        assert isinstance(state.outage, vault_pull.OpenOutage)
        self.assertFalse(state.outage.asked)
        _ = self.conflict()
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(len(self.questions()), 1)
        saved = cast(dict[str, dict[str, object]], json.loads(vault_pull.state_path().read_text()))
        self.assertTrue(saved["outage"]["asked"])
        self.assertNotIn("alerted", saved["outage"])

    def test_output_marker_controls_the_remote_outcome(self) -> None:
        commit = self.git(self.mac, "rev-parse", "HEAD")
        self.assertEqual(vault_pull.remote_outcome(subprocess.CompletedProcess(
            [], 255, f"login banner\nVAULT_OUTCOME=merged:2:{commit}\n", "")), vault_pull.MacMerged(2))
        self.assertEqual(vault_pull.remote_outcome(subprocess.CompletedProcess(
            [], 0, "VAULT_OUTCOME=merged:3:" + "a" * 64 + "\n", "")), vault_pull.MacMerged(3))
        for output in ("", "VAULT_OUTCOME=merged:0\n", "VAULT_OUTCOME=merged:-1:" + commit + "\n",
                       "VAULT_OUTCOME=merged:0:" + "a" * 39 + "\n", "VAULT_OUTCOME=merged:0:" + "g" * 40 + "\n", "VAULT_OUTCOME=failed\n",
                       f"VAULT_OUTCOME=branch\nVAULT_OUTCOME=merged:0:{commit}\n"):
            with self.subTest(output=output):
                self.assertIsInstance(vault_pull.remote_outcome(subprocess.CompletedProcess([], 0, output, "fixture error")),
                                      vault_pull.VaultCommandFailed)
        self.assertIsInstance(vault_pull.remote_outcome(subprocess.CompletedProcess([], 255, "", "")),
                              vault_pull.MacUnreachable)

    def test_current_run_saves_last_good_and_uses_the_expected_transport(self) -> None:
        for repository in (self.local, self.mac, self.github):
            _ = self.git(repository, "config", "fetch.prune", "true")
        for mac_changes in (False, True):
            with self.subTest(mac_changes=mac_changes):
                _ = self.git(self.local, "update-ref", "-d", "refs/remotes/mac/main")
                self.assertEqual(self.hour(), (0, "buildlog vault: current"))
                self.assertEqual(vault_pull.read_state(), vault_pull.VaultState(vault_pull.LastGoodPull(self.now), vault_pull.NoOutage()))
                self.assertEqual(self.commands[0][-2:], ["fixture-mac:rust/hanadocs", "refs/remotes/origin/main:refs/remotes/origin/main"])
                ssh = next(args for args in self.commands if args[0] == "ssh")
                self.assertEqual(ssh[:-1], ["ssh", *sync.ssh_options(), "fixture-mac"])
                self.assertEqual(self.git(self.local, "rev-parse", "refs/remotes/mac/main"), self.git(self.mac, "rev-parse", "HEAD"))
                if mac_changes:
                    _ = self.commit(self.mac, {"note.md": "new Mac commit\n"})
                self.assertEqual(self.hour()[0], 0)
                self.assertEqual(self.git(self.local, "rev-parse", "refs/remotes/mac/main"), self.git(self.mac, "rev-parse", "HEAD"))
                for args in self.commands:
                    if args[0] == "git":
                        self.assertFalse(any(token in args for token in ("checkout", "reset", "merge", "rebase", "stash")))

    def test_manual_vault_command_uses_the_same_transport(self) -> None:
        with redirect_stdout(StringIO()):
            self.assertEqual(cli.main(["vault"]), 0)
        self.assertEqual(vault_pull.read_state().last_good, vault_pull.LastGoodPull(self.now))


if __name__ == "__main__":
    _ = unittest.main()
