"""Publishing commits only its file, protects main, and preserves queued Mac pulls."""
from __future__ import annotations

import os
from contextlib import redirect_stderr
from functools import cached_property
import io
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import final, override
import unittest
from unittest import mock

from scripts.lib import main_publish
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class PublicationRepository:
    """An isolated main checkout, bare origin, ssh program and queue state."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.checkout = root / "checkout"
        self.origin = root / "origin.git"
        self.config = self.checkout / "config/example.conf"
        self.home = root / "home"
        self.programs = root / "bin"
        self.home.mkdir()
        self.programs.mkdir()
        self.ssh = self.programs / "ssh"
        self.ssh_log = root / "ssh.log"
        _ = self.ssh.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$@" >> "$SSH_LOG"\n'
            + 'if test -f "$OFFLINE"; then echo "Mac asleep" >&2; exit 255; fi\n'
            + 'printf "rc=0\\n"\n',
        )
        self.ssh.chmod(0o755)
        self.env = {
            **os.environ, "HOME": str(self.home), "CONF_DIR": str(self.config.parent),
            "ZONE_SSH": str(self.ssh), "ZONE_STATE": str(root / "state"),
            "SSH_LOG": str(self.ssh_log), "OFFLINE": str(root / "OFFLINE"),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "PATH": str(self.programs) + os.pathsep + os.environ.get("PATH", os.defpath),
        }
        _ = self.git(root, "init", "-q", "--bare", str(self.origin))
        _ = self.git(root, "init", "-q", "-b", "main", str(self.checkout))
        _ = self.git(self.checkout, "config", "user.name", "Publication test")
        _ = self.git(self.checkout, "config", "user.email", "publish@example.invalid")
        _ = self.git(self.checkout, "remote", "add", "origin", str(self.origin))
        self.config.parent.mkdir()
        _ = self.config.write_text("# whole: 1..10\namount=2\n# one of: fast, slow\nmode=fast\n")
        _ = (self.checkout / "other.txt").write_text("original\n")
        _ = self.git(self.checkout, "add", ".")
        _ = self.git(self.checkout, "commit", "-q", "-m", "initial")
        _ = self.git(self.checkout, "push", "-q", "origin", "main")

    def git(self, cwd: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments], cwd=cwd, env=self.env, check=True,
            capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
        ).stdout.strip()

    def head(self) -> str:
        return self.git(self.checkout, "rev-parse", "HEAD")

    def remote_tip(self) -> str:
        return self.git(self.origin, "rev-parse", "refs/heads/main")

    def change(self, value: str = "3") -> str:
        before = self.config.read_text()
        _ = self.config.write_text(before.replace("amount=2", f"amount={value}"))
        return before


class PublicationTestCase(unittest.TestCase):
    @cached_property
    def repository(self) -> PublicationRepository:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        return PublicationRepository(root)


class MainPublishTests(PublicationTestCase):

    @override
    def setUp(self) -> None:
        self.enterContext(mock.patch.dict(os.environ, self.repository.env, clear=True))

    def test_commit_and_push_publish_only_the_named_file_and_preserve_other_staged_work(self) -> None:
        repo = self.repository
        before = repo.change()
        _ = (repo.checkout / "other.txt").write_text("unrelated staged work\n")
        _ = repo.git(repo.checkout, "add", "other.txt")
        _ = main_publish.commit(
            repo.checkout, repo.config, "tune(example): amount 2 -> 3\n\nApproved reason\n",
            main_publish.ExistingFile(before), subject_prefix="tune(", unchanged="unchanged",
        )
        self.assertEqual(repo.git(repo.checkout, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"),
                         "config/example.conf")
        self.assertEqual(repo.git(repo.checkout, "diff", "--cached", "--name-only"), "other.txt")
        pushed, detail = main_publish.push(repo.checkout)
        self.assertTrue(pushed, detail)
        self.assertEqual(repo.remote_tip(), repo.head())

    def test_push_refuses_to_publish_unrelated_local_commits(self) -> None:
        repo = self.repository
        remote = repo.remote_tip()
        _ = repo.change()
        _ = repo.git(repo.checkout, "commit", "-qam", "unrelated work")
        pushed, detail = main_publish.push(repo.checkout)
        self.assertFalse(pushed)
        self.assertIn("other commit", detail)
        self.assertEqual(repo.remote_tip(), remote)

    def test_failed_commit_restores_previous_file_contents_and_does_not_push(self) -> None:
        repo = self.repository
        before = repo.change()
        head = repo.head()
        hook = repo.checkout / ".git/hooks/pre-commit"
        _ = hook.write_text("#!/bin/sh\necho fixture-rejected >&2\nexit 1\n")
        hook.chmod(0o755)
        with self.assertRaises(main_publish.Refusal):
            _ = main_publish.commit(
                repo.checkout, repo.config, "tune(example): amount 2 -> 3",
                main_publish.ExistingFile(before), subject_prefix="tune(", unchanged="unchanged",
            )
        self.assertEqual(repo.config.read_text(), before)
        self.assertEqual(repo.head(), head)
        self.assertEqual(repo.remote_tip(), head)

    def test_failed_commit_restores_config_when_another_file_commit_moves_head(self) -> None:
        repo = self.repository
        before = repo.change()
        head = repo.head()
        other = repo.checkout / "other.txt"
        _ = other.write_text("concurrent zone change\n")
        real_git = shutil.which("git")
        assert real_git is not None
        lock = repo.checkout / ".git/index.lock"
        self.addCleanup(lock.unlink, missing_ok=True)
        wrapper = repo.programs / "git"
        _ = wrapper.write_text(
            '#!/bin/sh\nset -e\nif [ "$3" = commit ]; then\n'
            + f'  {shlex.quote(real_git)} -C "$2" commit -q --only '
            + '-m "zone(UTC): concurrent change" -- other.txt\n'
            + f'  touch {shlex.quote(str(lock))}\nfi\n'
            + f'exec {shlex.quote(real_git)} "$@"\n',
        )
        wrapper.chmod(0o755)
        errors = io.StringIO()

        with redirect_stderr(errors), self.assertRaisesRegex(main_publish.Refusal, r"index\.lock.*unchanged"):
            _ = main_publish.commit(
                repo.checkout, repo.config, "tune(example): amount 2 -> 3",
                main_publish.ExistingFile(before), subject_prefix="tune(", unchanged="unchanged",
            )

        self.assertNotEqual(repo.head(), head)
        self.assertEqual(repo.git(repo.checkout, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"),
                         "other.txt")
        self.assertEqual(repo.git(repo.checkout, "show", "HEAD:other.txt"), "concurrent zone change")
        self.assertEqual(repo.config.read_text(), before)
        self.assertEqual(repo.git(repo.checkout, "diff", "HEAD", "--", str(repo.config)), "")
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(repo.remote_tip(), head)

    def test_rerere_failure_after_commit_keeps_the_new_file_and_allows_push(self) -> None:
        repo = self.repository
        before = repo.change()
        changed = repo.config.read_text()
        head = repo.head()
        _ = repo.git(repo.checkout, "config", "rerere.enabled", "true")
        lock = repo.checkout / ".git/MERGE_RR.lock"
        lock.touch()
        errors = io.StringIO()

        with redirect_stderr(errors):
            short_head = main_publish.commit(
                repo.checkout, repo.config, "tune(example): amount 2 -> 3",
                main_publish.ExistingFile(before), subject_prefix="tune(", unchanged="unchanged",
            )

        self.assertNotEqual(repo.head(), head)
        self.assertEqual(short_head, repo.git(repo.checkout, "rev-parse", "--short", "HEAD"))
        self.assertEqual(repo.config.read_text(), changed)
        self.assertEqual(repo.git(repo.checkout, "show", "HEAD:config/example.conf"), changed.strip())
        self.assertEqual(errors.getvalue().splitlines(), [
            f"anomaly: git commit: fatal: Unable to create '{lock}': File exists.; the commit exists",
        ])
        pushed, detail = main_publish.push(repo.checkout)
        self.assertTrue(pushed, detail)
        self.assertEqual(repo.remote_tip(), repo.head())

    def test_push_publishes_mixed_tune_and_zone_commits_in_either_order(self) -> None:
        repo = self.repository
        for subjects in (("tune(example): amount 2 -> 3", "zone(UTC): set with /zone"),
                         ("zone(UTC): set with /zone", "tune(example): amount 3 -> 4")):
            with self.subTest(subjects=subjects):
                for number, subject in enumerate(subjects):
                    _ = (repo.checkout / "other.txt").write_text(f"{number}: {subject}\n")
                    _ = repo.git(repo.checkout, "commit", "-qam", subject)
                    if number == 0:
                        self.assertNotEqual(repo.head(), repo.remote_tip())
                pushed, detail = main_publish.push(repo.checkout)
                self.assertTrue(pushed, detail)
                self.assertEqual(repo.remote_tip(), repo.head())

    def test_push_publishes_a_pending_installed_zone_commit_under_a_tune_commit(self) -> None:
        repo = self.repository
        zone_file = repo.checkout / "config/user_zone.conf"
        _ = zone_file.write_text("zone=America/Curacao\n")
        _ = repo.git(repo.checkout, "add", "config/user_zone.conf")
        _ = repo.git(repo.checkout, "commit", "-qm", "user zone: America/Curacao (AST)")
        zone_commit = repo.head()
        _ = repo.change()
        _ = repo.git(repo.checkout, "commit", "-qam", "tune(example): amount 2 -> 3")

        pushed, detail = main_publish.push(repo.checkout)

        self.assertTrue(pushed, detail)
        self.assertEqual(repo.remote_tip(), repo.head())
        self.assertEqual(repo.git(repo.origin, "rev-parse", "main^"), zone_commit)
        self.assertEqual(repo.git(repo.origin, "show", "main:config/user_zone.conf"),
                         "zone=America/Curacao")
        self.assertIn("amount=3", repo.git(repo.origin, "show", "main:config/example.conf"))

    def install_stalled_git(self, operation: str) -> None:
        executable = shutil.which("git", path=self.repository.env["PATH"])
        self.assertIsNotNone(executable)
        program = self.repository.programs / "git"
        _ = program.write_text(
            f"#!{sys.executable}\nimport os, sys, threading\n"
            + f"if {operation!r} in sys.argv[1:]:\n"
            + "    print('network started', flush=True)\n"
            + "    print('network stalled', file=sys.stderr, flush=True)\n"
            + f"    threading.Event().wait({TEST_HANG_GUARD_SECS})\n"
            + f"else:\n    os.execv({executable!r}, ['git', *sys.argv[1:]])\n",
        )
        program.chmod(0o755)

    def test_stalled_fetch_refuses_before_merge_and_preserves_both_output_streams(self) -> None:
        repo = self.repository
        before = repo.head(), repo.remote_tip(), repo.config.read_bytes()
        self.install_stalled_git("fetch")
        with self.assertRaises(main_publish.Refusal) as refused:
            main_publish.ready(repo.checkout, fetch_limit=main_publish.CommandTimeLimit(0.25))
        self.assertIn("timed out", str(refused.exception))
        self.assertIn("network started", str(refused.exception))
        self.assertIn("network stalled", str(refused.exception))
        self.assertEqual((repo.head(), repo.remote_tip(), repo.config.read_bytes()), before)

    def test_stalled_push_retains_the_unpublished_commit_for_retry(self) -> None:
        repo = self.repository
        _ = repo.change()
        _ = repo.git(repo.checkout, "commit", "-qam", "tune(example): amount 2 -> 3")
        before = repo.head(), repo.remote_tip()
        self.install_stalled_git("push")
        pushed, detail = main_publish.push(repo.checkout, time_limit=main_publish.CommandTimeLimit(0.25))
        self.assertFalse(pushed)
        self.assertIn("timed out", detail)
        self.assertIn("network started", detail)
        self.assertIn("network stalled", detail)
        self.assertEqual((repo.head(), repo.remote_tip()), before)

    def test_ready_refuses_a_checkout_off_main_before_any_publication(self) -> None:
        repo = self.repository
        head = repo.head()
        _ = repo.git(repo.checkout, "switch", "-qc", "work")
        with self.assertRaises(main_publish.Refusal):
            main_publish.ready(repo.checkout)
        self.assertEqual(repo.head(), head)
        self.assertEqual(repo.remote_tip(), head)

    def test_ready_fast_forwards_to_origin_before_a_new_edit(self) -> None:
        repo = self.repository
        clone = repo.root / "second"
        _ = repo.git(repo.root, "clone", "-q", "-b", "main", str(repo.origin), str(clone))
        _ = repo.git(clone, "config", "user.name", "Other writer")
        _ = repo.git(clone, "config", "user.email", "other@example.invalid")
        _ = (clone / "remote.txt").write_text("remote work\n")
        _ = repo.git(clone, "add", "remote.txt")
        _ = repo.git(clone, "commit", "-qm", "remote work")
        _ = repo.git(clone, "push", "-q", "origin", "main")
        main_publish.ready(repo.checkout)
        self.assertEqual(repo.head(), repo.remote_tip())
        self.assertEqual((repo.checkout / "remote.txt").read_text(), "remote work\n")

    def other_writer(self) -> Path:
        repo = self.repository
        clone = repo.root / "other-writer"
        _ = repo.git(repo.root, "clone", "-q", "-b", "main", str(repo.origin), str(clone))
        _ = repo.git(clone, "config", "user.name", "Other writer")
        _ = repo.git(clone, "config", "user.email", "other@example.invalid")
        return clone

    def advance_origin(self, clone: Path) -> str:
        repo = self.repository
        _ = (clone / "remote.txt").write_text("another writer's work\n")
        _ = repo.git(clone, "add", "remote.txt")
        _ = repo.git(clone, "commit", "-qm", "another writer's work")
        _ = repo.git(clone, "push", "-q", "origin", "main")
        return repo.remote_tip()

    def retry_tune(self) -> subprocess.CompletedProcess[str]:
        repo = self.repository
        return subprocess.run(
            [sys.executable, str(Path(__file__).with_name("tune.py")),
             "example", "amount", "3", "--why", "Approved capacity", "--user-words", "yes set 3"],
            cwd=repo.checkout, env=repo.env, capture_output=True, text=True,
            check=False, timeout=TEST_HANG_GUARD_SECS,
        )

    def rejected_tune(self) -> str:
        repo = self.repository
        hook = repo.origin / "hooks/pre-receive"
        _ = hook.write_text("#!/bin/sh\necho fixture-rejected >&2\nexit 1\n")
        hook.chmod(0o755)
        first = self.retry_tune()
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        self.assertIn("not pushed", first.stdout)
        self.assertNotEqual(repo.head(), repo.remote_tip())
        self.assertFalse(repo.ssh_log.exists())
        hook.unlink()
        return repo.git(repo.checkout, "log", "-1", "--format=%B")

    def test_same_value_retry_rebases_a_rejected_tune_after_another_writer_pushes(self) -> None:
        repo = self.repository
        approval = self.rejected_tune()
        advanced = self.advance_origin(self.other_writer())

        repeated = self.retry_tune()

        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(repeated.stdout, "already amount=3\n")
        self.assertEqual(repo.head(), repo.remote_tip())
        self.assertEqual(repo.git(repo.checkout, "rev-parse", "HEAD^"), advanced)
        self.assertEqual(repo.git(repo.checkout, "rev-list", "--count", f"{advanced}..HEAD"), "1")
        self.assertEqual(repo.git(repo.checkout, "log", "-1", "--format=%B"), approval)
        self.assertIn("User-Approved: yes set 3", approval)
        self.assertIn("amount=3", repo.git(repo.origin, "show", "main:config/example.conf"))
        self.assertEqual((repo.checkout / "remote.txt").read_text(), "another writer's work\n")
        self.assertIn("git pull -q --ff-only", repo.ssh_log.read_text())
        head = repo.head()
        again = self.retry_tune()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(again.stdout, "already amount=3\n")
        self.assertEqual((repo.head(), repo.remote_tip()), (head, head))

    def test_same_value_retry_drops_a_rejected_change_already_published_by_another_writer(self) -> None:
        repo = self.repository
        _ = self.rejected_tune()
        clone = self.other_writer()
        config = clone / "config/example.conf"
        _ = config.write_text(config.read_text().replace("amount=2", "amount=3"))
        _ = repo.git(clone, "commit", "-qam", "approved value from another writer")
        advanced = self.advance_origin(clone)
        count = repo.git(repo.origin, "rev-list", "--count", "main")

        repeated = self.retry_tune()

        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(repeated.stdout, "already amount=3\n")
        self.assertEqual((repo.head(), repo.remote_tip()), (advanced, advanced))
        self.assertEqual(repo.git(repo.checkout, "rev-list", "--count", "HEAD"), count)
        self.assertEqual(repo.git(repo.checkout, "status", "--porcelain=v1"), "")
        self.assertIn("git pull -q --ff-only", repo.ssh_log.read_text())

    def test_ready_rebases_tune_zone_and_installed_zone_commits_in_order(self) -> None:
        repo = self.repository
        subjects = ("tune(example): amount 2 -> 3", "zone(UTC): approved zone", "user zone: UTC (UTC)")
        _ = repo.change()
        _ = repo.git(repo.checkout, "commit", "-qam", subjects[0])
        for name, subject in zip(("zone.txt", "installed-zone.txt"), subjects[1:]):
            _ = (repo.checkout / name).write_text(subject + "\n")
            _ = repo.git(repo.checkout, "add", name)
            _ = repo.git(repo.checkout, "commit", "-qm", subject)
        tree = repo.git(repo.checkout, "rev-parse", "HEAD^{tree}")
        advanced = self.advance_origin(self.other_writer())

        main_publish.ready(repo.checkout)

        self.assertEqual(repo.git(repo.checkout, "log", "--reverse", "--format=%s", f"{advanced}..HEAD"),
                         "\n".join(subjects))
        self.assertEqual(repo.git(repo.checkout, "diff", "--name-only", tree, "HEAD"), "remote.txt")
        pushed, detail = main_publish.push(repo.checkout)
        self.assertTrue(pushed, detail)
        self.assertEqual(repo.head(), repo.remote_tip())

    def test_ready_refuses_an_unrelated_commit_when_main_is_only_ahead_of_origin(self) -> None:
        repo = self.repository
        remote = repo.remote_tip()
        _ = (repo.checkout / "other.txt").write_text("unpublished feature\n")
        subject = "keep this unpublished feature"
        _ = repo.git(repo.checkout, "commit", "-qam", subject)
        rejected = repo.head()
        short_id = repo.git(repo.checkout, "rev-parse", "--short", "HEAD")
        self.assertEqual(repo.git(repo.checkout, "rev-parse", "HEAD^"), remote)
        self.assertEqual(repo.git(repo.checkout, "status", "--porcelain=v1"), "")

        with self.assertRaises(main_publish.Refusal) as refused:
            main_publish.ready(repo.checkout)

        message = str(refused.exception)
        self.assertIn(short_id, message)
        self.assertIn(subject, message)
        self.assertEqual(len(message.splitlines()), 1)
        self.assertEqual(repo.head(), rejected)
        self.assertEqual(repo.remote_tip(), remote)
        self.assertEqual(repo.git(repo.checkout, "status", "--porcelain=v1"), "")
        self.assertEqual((repo.checkout / "other.txt").read_text(), "unpublished feature\n")

    def test_ready_names_an_unrelated_commit_and_preserves_main_when_origin_diverges(self) -> None:
        repo = self.repository
        _ = repo.change()
        _ = repo.git(repo.checkout, "commit", "-qam", "tune(example): amount 2 -> 3")
        _ = (repo.checkout / "other.txt").write_text("local work\n")
        subject = "keep this unpublished feature"
        _ = repo.git(repo.checkout, "commit", "-qam", subject)
        rejected = repo.head()
        advanced = self.advance_origin(self.other_writer())
        before = repo.head(), repo.config.read_bytes(), repo.git(repo.checkout, "status", "--porcelain=v1")

        with self.assertRaises(main_publish.Refusal) as refused:
            main_publish.ready(repo.checkout)

        message = str(refused.exception)
        self.assertIn(subject, message)
        self.assertIn(rejected[:7], message)
        self.assertEqual(len(message.splitlines()), 1)
        self.assertEqual((repo.head(), repo.config.read_bytes(),
                          repo.git(repo.checkout, "status", "--porcelain=v1")), before)
        self.assertEqual(repo.remote_tip(), advanced)
        self.assertEqual((repo.checkout / "other.txt").read_text(), "local work\n")

    def test_ready_names_a_conflicting_commit_and_restores_main_after_partial_rebase(self) -> None:
        repo = self.repository
        zone = repo.checkout / "zone.txt"
        _ = zone.write_text("UTC\n")
        _ = repo.git(repo.checkout, "add", "zone.txt")
        _ = repo.git(repo.checkout, "commit", "-qm", "zone(UTC): approved zone")
        _ = repo.change()
        subject = "tune(example): amount 2 -> 3"
        _ = repo.git(repo.checkout, "commit", "-qam", subject)
        rejected = repo.head()
        clone = self.other_writer()
        config = clone / "config/example.conf"
        _ = config.write_text(config.read_text().replace("amount=2", "amount=4"))
        _ = repo.git(clone, "commit", "-qam", "conflicting capacity")
        _ = repo.git(clone, "push", "-q", "origin", "main")
        advanced = repo.remote_tip()
        before = repo.head(), repo.git(repo.checkout, "rev-parse", "HEAD^{tree}"), repo.config.read_bytes()

        with self.assertRaises(main_publish.Refusal) as refused:
            main_publish.ready(repo.checkout)

        message = str(refused.exception)
        self.assertIn("conflict", message)
        self.assertIn(subject, message)
        self.assertIn(rejected[:7], message)
        self.assertIn("config/example.conf", message)
        self.assertEqual(len(message.splitlines()), 1)
        self.assertEqual((repo.head(), repo.git(repo.checkout, "rev-parse", "HEAD^{tree}"),
                          repo.config.read_bytes()), before)
        self.assertEqual(repo.git(repo.checkout, "status", "--porcelain=v1"), "")
        self.assertEqual(repo.git(repo.checkout, "branch", "--show-current"), "main")
        self.assertEqual(zone.read_text(), "UTC\n")
        self.assertEqual(repo.remote_tip(), advanced)
        for name in ("rebase-merge", "rebase-apply"):
            self.assertFalse((repo.checkout / ".git" / name).exists())

    def test_mac_pull_uses_the_callers_ssh_and_reports_an_asleep_mac(self) -> None:
        repo = self.repository
        self.assertIsInstance(main_publish.pull_mac(repo.checkout, ssh_program=str(repo.ssh)), main_publish.MacPulled)
        arguments = repo.ssh_log.read_text()
        self.assertIn("mac\n", arguments)
        self.assertIn(f"git pull -q --ff-only natedev:{shlex.quote(str(repo.checkout))} main", arguments)
        _ = (repo.root / "OFFLINE").write_text("")
        outcome = main_publish.pull_mac(repo.checkout, ssh_program=str(repo.ssh))
        self.assertIsInstance(outcome, main_publish.MacPullFailed)
        if not isinstance(outcome, main_publish.MacPullFailed):
            self.fail("an asleep Mac did not return a failure reason")
        self.assertIn("Mac asleep", outcome.reason)

    def test_remote_git_failure_returns_a_named_failure_after_a_successful_ssh_connection(self) -> None:
        repo = self.repository
        _ = repo.ssh.write_text('#!/bin/sh\necho "pull refused" >&2\nprintf "rc=1\\n"\n')
        outcome = main_publish.pull_mac(repo.checkout, ssh_program=str(repo.ssh))
        self.assertIsInstance(outcome, main_publish.MacPullFailed)
        if not isinstance(outcome, main_publish.MacPullFailed):
            self.fail("a failed remote Git pull was treated as a completed pull")
        self.assertIn("pull refused", outcome.reason)

    def test_stalled_mac_pull_returns_a_failure_with_both_output_streams(self) -> None:
        repo = self.repository
        _ = repo.ssh.write_text(
            f"#!{sys.executable}\nimport sys, threading\n"
            + "print('connection made', flush=True)\n"
            + "print('pull stalled', file=sys.stderr, flush=True)\n"
            + f"threading.Event().wait({TEST_HANG_GUARD_SECS})\n",
        )
        outcome = main_publish.pull_mac(repo.checkout, ssh_program=str(repo.ssh),
                                        time_limit=main_publish.CommandTimeLimit(0.25))
        self.assertIsInstance(outcome, main_publish.MacPullFailed)
        if not isinstance(outcome, main_publish.MacPullFailed):
            self.fail("a stalled Mac pull did not return a failure")
        self.assertIn("timed out", outcome.reason)
        self.assertIn("connection made", outcome.reason)
        self.assertIn("pull stalled", outcome.reason)

    def test_queue_survives_a_new_process_and_uses_the_existing_zone_state_path(self) -> None:
        repo = self.repository
        change = main_publish.PendingMacPull("example.amount=3", "approved change", "2026-11-01", "Mac asleep")
        main_publish.queue(change)
        queue_path = repo.root / "state/mac.json"
        self.assertEqual(main_publish.queue_path(), queue_path)
        self.assertEqual(main_publish.read_queue(), change)
        result = subprocess.run(
            [str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", "from scripts.lib import main_publish; "
             + "value = main_publish.read_queue(); print(value)"],
            cwd=Path(__file__).resolve().parents[2], env=repo.env,
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("example.amount=3", result.stdout)
        self.assertIn("approved change", result.stdout)
        self.assertIn("Mac asleep", result.stdout)
        with self.subTest(queue="zone notice"):
            main_publish.queue_pull_only("example.amount", "still asleep")
            waiting = main_publish.PendingMacPull(change.label, change.text, change.since, "still asleep")
            self.assertEqual(main_publish.read_queue(), waiting)
            main_publish.clear_pull_only()
            self.assertEqual(main_publish.read_queue(), waiting)
        with self.subTest(queue="pull only"):
            queue_path.unlink()
            main_publish.queue_pull_only("example.amount", "Mac asleep")
            pending = main_publish.read_queue()
            self.assertIsInstance(pending, main_publish.PendingMacPull)
            assert isinstance(pending, main_publish.PendingMacPull)
            self.assertEqual((pending.label, pending.text, pending.reason), ("example.amount", "", "Mac asleep"))
            main_publish.clear_pull_only()
            self.assertIsInstance(main_publish.read_queue(), main_publish.NoPendingMacPull)


if __name__ == "__main__":
    _ = unittest.main()
