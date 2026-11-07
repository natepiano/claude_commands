"""Vault automation with local fake transports, never a real vault or Mac."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, cast, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from scripts.buildlog import cli
from scripts.lib import conf
from scripts.buildlog import rust_release, sync, vault_pull
from scripts.buildlog.test_index import point_root_at

if TYPE_CHECKING:
    from ..notify.push_texts import RenderedPush


# Run the production remote script against this one fake Git implementation.
# No command in this fixture ever invokes Git or writes a repository.
FAKE_GIT = r'''#!/bin/sh
printf '%s\n' "$*" >> "$VAULT_TEST_GIT_LOG"
[ "$1" = -C ] && [ "$2" = rust/hanadocs ] || exit 90
case "$3" in
  symbolic-ref) [ "$VAULT_TEST_SCENARIO" = branch ] && echo feature || echo main ;;
  status) [ "$VAULT_TEST_SCENARIO" = tracked ] && echo ' M note.md'; exit 0 ;;
  merge-base) [ "$VAULT_TEST_SCENARIO" != diverged ] ;;
  rev-list) printf '%s\n' "$VAULT_TEST_COMMITS" ;;
  merge)
    tries=0
    while IFS= read -r command; do
      case "$command" in *" merge "*) tries=$((tries + 1)) ;; esac
    done < "$VAULT_TEST_GIT_LOG"
    case "$VAULT_TEST_SCENARIO" in
      locked|lock_clears)
        if [ "$VAULT_TEST_SCENARIO" = locked ] || [ "$tries" -lt 3 ]; then
          echo "error: Unable to create '/Users/fixture/rust/hanadocs/.git/index.lock': File exists." >&2
          exit 128
        fi ;;
    esac
    case "$VAULT_TEST_SCENARIO" in
      untracked) echo 'error: The following untracked working tree files would be overwritten by merge:' >&2; exit 1 ;;
      merge_refused) echo 'fatal: Not possible to fast-forward, aborting.' >&2; exit 128 ;;
      lock_name_only) echo 'fatal: cannot read index.lock' >&2; exit 128 ;;
      lock_exists_only) echo 'fatal: other.lock: File exists' >&2; exit 128 ;;
      remote_failed) echo 'fatal: fixture remote failure' >&2; exit 128 ;;
      *) echo 'Updating old..new' ;;
    esac ;;
  *) echo 'unsafe or unexpected git command' >&2; exit 91 ;;
esac
'''


class VaultPullTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    bin: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    git_log: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    scenario: str = "clean"
    commits: int = 7
    now: float = 1_800_000_000.0
    send_ok: bool = True
    commands: list[list[str]] = []
    pushes: list[RenderedPush] = []

    @override
    def setUp(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = folder / "buildlog"
        self.bin = folder / "bin"
        self.bin.mkdir()
        git = self.bin / "git"
        _ = git.write_text(FAKE_GIT)
        git.chmod(0o700)
        sleep = self.bin / "sleep"
        _ = sleep.write_text('#!/bin/sh\n[ "$1" = 0.0 ] || exit 92\n')
        sleep.chmod(0o700)
        config = folder / "config"
        config.mkdir()
        settings = Path(__file__).resolve().parents[2] / "config" / "build_targets.conf"
        _ = (config / "build_targets.conf").write_text(
            "# whole: 0..10\nvault.lock_retries=2\n"
            + "# range: 0..5\nvault.lock_retry_delay_s=0\n" + settings.read_text())
        self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(config)}))
        self.git_log = folder / "git.log"
        point_root_at(self, self.root)
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(folder), "BUILDLOG_PEER": "fixture-mac"}))
        self.commands = []
        self.pushes = []
        self.scenario = "clean"
        self.commits = 7
        self.now = 1_800_000_000.0
        self.send_ok = True
        _ = self.enterContext(mock.patch.object(vault_pull, "run", new=self.run_transport))
        _ = self.enterContext(mock.patch.object(time, "time", side_effect=lambda: self.now))
        _ = self.enterContext(mock.patch.object(rust_release, "send_release_text", new=self.send))

    def run_transport(self, args: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
        self.commands.append(args)
        if args[0] == "git":
            if self.scenario == "push_refused":
                return subprocess.CompletedProcess(args, 1, "", "! [rejected] main -> main (non-fast-forward)")
            if self.scenario == "push_unreachable":
                return subprocess.CompletedProcess(args, 128, "", "ssh: connect: Connection refused")
            if self.scenario == "command_failed":
                return subprocess.CompletedProcess(args, 128, "", "fatal: fixture transport configuration is invalid")
            return subprocess.CompletedProcess(args, 0, "", "")
        self.assertEqual(args[0], "ssh", "external commands must stay inside the fake transport")
        if self.scenario == "unreachable":
            return subprocess.CompletedProcess(args, 255, "", "Mac unreachable")
        if self.scenario == "timeout":
            return None
        if self.scenario == "no_marker":
            return subprocess.CompletedProcess(args, 0, "login banner only\n", "")
        result = subprocess.run(
            ["/bin/sh", "-c", args[-1]], cwd=self.bin.parent,
            env={**os.environ, "PATH": str(self.bin), "VAULT_TEST_SCENARIO": self.scenario,
                 "VAULT_TEST_COMMITS": str(self.commits), "VAULT_TEST_GIT_LOG": str(self.git_log)},
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        # /usr/bin/login on the Mac hides the remote shell's exit status.
        return subprocess.CompletedProcess(args, 0, result.stdout, result.stderr)

    def send(self, push: RenderedPush) -> bool:
        self.pushes.append(push)
        return self.send_ok

    def hour(self) -> tuple[int, str]:
        output = StringIO()
        with redirect_stdout(output):
            status = vault_pull.vault()
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1, "each hourly vault run prints one line")
        return status, lines[0]

    def remote_commands(self) -> list[list[str]]:
        return [shlex.split(line) for line in self.git_log.read_text().splitlines()]

    def test_clean_fast_forward_reports_the_commit_count_and_saves_last_good(self) -> None:
        self.assertEqual(self.hour(), (0, "buildlog vault: pulled 7 commits"))
        self.assertEqual(vault_pull.read_state(), vault_pull.VaultState(
            vault_pull.LastGoodPull(self.now), vault_pull.NoOutage()))
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.remote_commands()[-1], ["-C", "rust/hanadocs", "merge", "--ff-only", "--no-overwrite-ignore", "refs/remotes/origin/main"])

    def test_printed_outcomes_survive_a_successful_ssh_exit_for_every_cause(self) -> None:
        cases = (
            ("clean", vault_pull.Pulled), ("tracked", vault_pull.TrackedFilesChanged),
            ("branch", vault_pull.NotOnMain), ("diverged", vault_pull.HistoryDiverged),
            ("untracked", vault_pull.UntrackedWouldBeOverwritten),
            ("remote_failed", vault_pull.VaultCommandFailed),
        )
        for scenario, expected in cases:
            with self.subTest(scenario=scenario):
                self.scenario = scenario
                self.assertIsInstance(vault_pull.pull(), expected)

    def test_busy_lock_clears_on_a_later_try_and_pulls(self) -> None:
        self.scenario = "lock_clears"
        self.assertEqual(self.hour(), (0, "buildlog vault: pulled 7 commits"))
        self.assertEqual(sum(command[2] == "merge" for command in self.remote_commands()), 3)
        self.assertEqual(self.pushes, [])

    def test_busy_lock_on_every_try_has_its_own_stop_and_detail(self) -> None:
        self.scenario = "locked"
        status, line = self.hour()
        self.assertEqual(status, 0)
        self.assertIn("index.lock", line)
        self.assertEqual(sum(command[2] == "merge" for command in self.remote_commands()), 3)
        outage = vault_pull.read_state().outage
        self.assertIsInstance(outage, vault_pull.OpenOutage)
        assert isinstance(outage, vault_pull.OpenOutage)
        self.assertEqual(outage.cause, "vault_locked")
        self.assertIn("index.lock", outage.detail)
        self.assertIn("File exists", outage.detail)
        self.assertEqual(len(self.pushes), 1)
        self.assertIn(
            "Another git process held ~/rust/hanadocs/.git/index.lock on the Mac for every try.",
            self.pushes[0].message)
        self.assertIn(
            "In a terminal on the Mac, check that no git process uses ~/rust/hanadocs; "
            + "if none does, remove ~/rust/hanadocs/.git/index.lock.", self.pushes[0].message)
        self.assertNotIn("The update failed with an error the job does not know.", self.pushes[0].message)

    def test_vault_retry_delay_limit_stays_below_one_minute(self) -> None:
        production = Path(__file__).resolve().parents[2] / "config" / "build_targets.conf"
        for settings in (production, Path(os.environ["CONF_DIR"]) / "build_targets.conf"):
            with self.subTest(settings=settings):
                conf.check(settings, "vault.lock_retries", "10")
                conf.check(settings, "vault.lock_retry_delay_s", "5")
                with self.assertRaises(conf.Unusable):
                    conf.check(settings, "vault.lock_retry_delay_s", "5.1")
                with self.assertRaises(conf.Unusable):
                    conf.check(settings, "vault.lock_retries", "11")

    def test_zero_retries_stops_after_the_first_busy_lock(self) -> None:
        settings = Path(os.environ["CONF_DIR"]) / "build_targets.conf"
        _ = settings.write_text(settings.read_text().replace("vault.lock_retries=2", "vault.lock_retries=0"))
        self.scenario = "locked"
        outcome = vault_pull.pull()
        self.assertIsInstance(outcome, vault_pull.VaultLocked)
        self.assertEqual(sum(command[2] == "merge" for command in self.remote_commands()), 1)

    def test_other_merge_errors_do_not_retry(self) -> None:
        for scenario in ("remote_failed", "lock_name_only", "lock_exists_only"):
            with self.subTest(scenario=scenario):
                self.scenario = scenario
                _ = self.git_log.write_text("")
                self.assertIsInstance(vault_pull.pull(), vault_pull.VaultCommandFailed)
                self.assertEqual(sum(command[2] == "merge" for command in self.remote_commands()), 1)

    def test_command_failure_saves_detail_in_state(self) -> None:
        self.scenario = "command_failed"
        self.assertEqual(self.hour()[0], 1)
        saved = cast(dict[str, dict[str, object]], json.loads(vault_pull.state_path().read_text()))
        self.assertEqual(saved["outage"]["detail"], "fatal: fixture transport configuration is invalid")
        outage = vault_pull.read_state().outage
        assert isinstance(outage, vault_pull.OpenOutage)
        self.assertEqual(outage.detail, saved["outage"]["detail"])
        self.assertIn(str(vault_pull.state_path()), self.pushes[0].message)
        self.assertIn("journalctl --user -u buildlog.service | grep 'buildlog vault'", self.pushes[0].message)

    def test_older_outage_without_detail_still_reads(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _ = vault_pull.state_path().write_text(json.dumps({
            "last_good": self.now - 3600,
            "outage": {"cause": "command_failed", "since": self.now, "alerted": True},
        }))
        state = vault_pull.read_state()
        self.assertEqual(state.last_good, vault_pull.LastGoodPull(self.now - 3600))
        assert isinstance(state.outage, vault_pull.OpenOutage)
        self.assertEqual(state.outage.cause, "command_failed")
        self.assertEqual(state.outage.since, self.now)
        self.assertTrue(state.outage.alerted)
        self.assertEqual(state.outage.detail, "")

    def test_missing_marker_with_successful_ssh_is_a_command_failure(self) -> None:
        self.scenario = "no_marker"
        self.assertIsInstance(vault_pull.pull(), vault_pull.VaultCommandFailed)

    def test_pause_preserves_outage_state_and_makes_no_transport_calls(self) -> None:
        state = vault_pull.VaultState(vault_pull.NeverPulled(),
                                      vault_pull.OpenOutage("tracked_files_changed", self.now, False,
                                                            vault_pull.stop_details(vault_pull.TrackedFilesChanged())[1]))
        vault_pull.write_state(state)
        with redirect_stdout(StringIO()):
            self.assertEqual(sync.pause("fixture hold"), 0)
        self.assertEqual(self.hour(), (0, "buildlog vault: paused (fixture hold); buildlog sync resume ends it"))
        self.assertEqual(self.commands, [])
        self.assertEqual(self.pushes, [])
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
        self.assertEqual([args[0] for args in self.commands], ["git", "ssh"])

    def test_ignored_file_is_preserved_by_the_real_git_merge(self) -> None:
        # All Git commands use a new temporary repo, with no remotes or network.
        repo = self.bin.parent / "rust" / "hanadocs"
        repo.mkdir(parents=True)

        def git(*args: str) -> str:
            result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                                    text=True, check=True, timeout=10)
            return result.stdout.strip()

        _ = git("init", "--initial-branch=main")
        _ = git("config", "user.name", "Fixture")
        _ = git("config", "user.email", "fixture@example.invalid")
        _ = (repo / ".gitignore").write_text("protected.txt\n")
        _ = git("add", ".gitignore")
        _ = git("commit", "-m", "base")
        base = git("rev-parse", "HEAD")
        _ = git("switch", "-c", "incoming")
        _ = (repo / "protected.txt").write_text("incoming\n")
        _ = git("add", "--force", "protected.txt")
        _ = git("commit", "-m", "incoming")
        _ = git("update-ref", "refs/remotes/origin/main", "HEAD")
        _ = git("switch", "main")
        _ = (repo / "protected.txt").write_text("keep this ignored file\n")
        result = subprocess.run(["/bin/sh", "-c", vault_pull.REMOTE_PULL], cwd=self.bin.parent,
                                capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(result.stdout, "VAULT_OUTCOME=untracked\n")
        self.assertEqual((repo / "protected.txt").read_text(), "keep this ignored file\n")
        self.assertEqual(git("rev-parse", "HEAD"), base)

    def test_already_current_is_success_and_refreshes_last_good(self) -> None:
        self.commits = 0
        self.assertEqual(self.hour(), (0, "buildlog vault: current"))
        self.assertEqual(vault_pull.read_state().last_good, vault_pull.LastGoodPull(self.now))
        self.assertEqual(self.pushes, [])

    def test_tracked_changes_stop_before_merge_and_name_the_safe_fix(self) -> None:
        self.scenario = "tracked"
        status, line = self.hour()
        self.assertEqual(status, 0)
        self.assertIn("Some files in ~/rust/hanadocs on the Mac have changes that are not committed.", line)
        self.assertFalse(any(command[2] == "merge" for command in self.remote_commands()))
        self.assertEqual(len(self.pushes), 1)
        self.assertIn("commit", self.pushes[0].message)
        self.assertIn("Mac", self.pushes[0].message)
        self.assertIsInstance(vault_pull.pull(), vault_pull.TrackedFilesChanged)

    def test_tracked_changes_exit_zero_and_print_the_stop_line(self) -> None:
        self.scenario = "tracked"
        self.assertEqual(self.hour(), (
            0,
            "buildlog vault: stopped: Some files in ~/rust/hanadocs on the Mac have changes that are not committed.",
        ))
        self.assertEqual(len(self.pushes), 1)
        outage = vault_pull.read_state().outage
        assert isinstance(outage, vault_pull.OpenOutage)
        self.assertEqual(outage.cause, "tracked_files_changed")
        self.assertTrue(outage.alerted)

    def test_command_failure_exits_one_after_a_recorded_user_action_stop(self) -> None:
        self.scenario = "tracked"
        self.assertEqual(self.hour()[0], 0)
        self.scenario = "command_failed"
        self.assertEqual(self.hour(), (
            1, "buildlog vault: stopped: fatal: fixture transport configuration is invalid",
        ))
        self.assertEqual(len(self.pushes), 1)
        outage = vault_pull.read_state().outage
        assert isinstance(outage, vault_pull.OpenOutage)
        self.assertEqual(outage.cause, "command_failed")
        self.assertTrue(outage.alerted)

    def test_user_action_stops_exit_zero_after_sent_or_recorded_alerts(self) -> None:
        for scenario, cause in (
            ("tracked", "tracked_files_changed"), ("branch", "not_on_main"),
            ("diverged", "history_diverged"), ("push_refused", "history_diverged"),
            ("untracked", "untracked_would_be_overwritten"), ("locked", "vault_locked"),
        ):
            with self.subTest(scenario=scenario):
                vault_pull.write_state(vault_pull.VaultState(vault_pull.NeverPulled(), vault_pull.NoOutage()))
                self.pushes.clear()
                self.scenario = scenario
                status, line = self.hour()
                self.assertEqual(status, 0)
                self.assertTrue(line.startswith("buildlog vault: stopped: "))
                outage = vault_pull.read_state().outage
                assert isinstance(outage, vault_pull.OpenOutage)
                self.assertEqual(outage.cause, cause)
                self.assertTrue(outage.alerted)
                self.assertEqual(len(self.pushes), 1)
                self.assertEqual(self.hour(), (0, line))
                self.assertEqual(len(self.pushes), 1)

    def test_user_action_stops_exit_one_when_the_alert_fails(self) -> None:
        self.send_ok = False
        for scenario in ("tracked", "branch", "diverged", "untracked", "locked"):
            with self.subTest(scenario=scenario):
                vault_pull.write_state(vault_pull.VaultState(vault_pull.NeverPulled(), vault_pull.NoOutage()))
                self.pushes.clear()
                self.scenario = scenario
                status, line = self.hour()
                self.assertEqual(status, 1)
                self.assertTrue(line.startswith("buildlog vault: stopped: "))
                outage = vault_pull.read_state().outage
                assert isinstance(outage, vault_pull.OpenOutage)
                self.assertFalse(outage.alerted)
                self.assertEqual(len(self.pushes), 1)

    def test_non_main_branch_stops_before_status_or_merge(self) -> None:
        self.scenario = "branch"
        self.assertIsInstance(vault_pull.pull(), vault_pull.NotOnMain)
        self.assertEqual([command[2] for command in self.remote_commands()], ["symbolic-ref"])

    def test_push_and_merge_divergence_are_typed_stops(self) -> None:
        for scenario in ("push_refused", "diverged", "merge_refused"):
            with self.subTest(scenario=scenario):
                self.scenario = scenario
                self.commands.clear()
                self.assertIsInstance(vault_pull.pull(), vault_pull.HistoryDiverged)
                self.assertEqual(sum(command[0] == "ssh" for command in self.commands), 0 if scenario == "push_refused" else 1)

    def test_untracked_file_in_the_way_is_not_removed_and_stops_the_merge(self) -> None:
        self.scenario = "untracked"
        self.assertIsInstance(vault_pull.pull(), vault_pull.UntrackedWouldBeOverwritten)
        status, line = self.hour()
        self.assertEqual(status, 0)
        self.assertIn("An update would replace a file on the Mac that git does not track.", line)
        self.assertIn("move the untracked file that blocks the hanadocs update out of ~/rust/hanadocs", self.pushes[0].message)

    def test_command_failure_prints_its_detail_without_putting_it_in_the_push(self) -> None:
        self.scenario = "command_failed"
        self.assertEqual(self.hour(), (1, "buildlog vault: stopped: fatal: fixture transport configuration is invalid"))
        self.assertEqual(len(self.pushes), 1)
        push = self.pushes[0]
        self.assertEqual(push.title, "Mac vault copy is not updating")
        self.assertIn("The update failed with an error the job does not know.", push.message)
        self.assertIn(f"read the detail in {vault_pull.state_path()}", push.message)
        self.assertNotIn("fatal: fixture transport configuration is invalid", push.title + push.message)

    def test_unreachable_within_twenty_four_hours_does_not_alert_but_older_does(self) -> None:
        self.assertEqual(self.hour()[0], 0)
        good = self.now
        self.scenario = "unreachable"
        for age in (3600, 24 * 3600):
            self.now = good + age
            self.assertEqual(self.hour()[0], 0)
            self.assertEqual(self.pushes, [])
        self.now = good + 24 * 3600 + 1
        self.assertEqual(self.hour()[0], 1)
        self.assertEqual(len(self.pushes), 1)
        self.assertIn("Wake the Mac", self.pushes[0].message)
        self.assertEqual(vault_pull.read_state().last_good, vault_pull.LastGoodPull(good))

    def test_unreachable_before_the_first_good_pull_gets_a_twenty_four_hour_grace(self) -> None:
        self.scenario = "unreachable"
        self.assertEqual(self.hour()[0], 0)
        self.now += 24 * 3600 + 1
        self.assertEqual(self.hour()[0], 1)
        self.assertEqual(len(self.pushes), 1)

    def test_push_connection_failure_and_timeout_are_unreachable(self) -> None:
        for scenario in ("push_unreachable", "timeout"):
            with self.subTest(scenario=scenario):
                self.scenario = scenario
                self.assertIsInstance(vault_pull.pull(), vault_pull.MacUnreachable)

    def test_one_alert_per_outage_survives_hours_until_a_good_pull_closes_it(self) -> None:
        self.scenario = "tracked"
        self.assertEqual(self.hour()[0], 0)
        first = vault_pull.read_state().outage
        self.assertIsInstance(first, vault_pull.OpenOutage)
        for scenario in ("tracked", "branch", "untracked"):
            self.scenario = scenario
            self.now += 3600
            self.assertEqual(self.hour()[0], 0)
        self.assertEqual(len(self.pushes), 1)
        self.scenario = "clean"
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(vault_pull.read_state().outage, vault_pull.NoOutage())
        self.scenario = "tracked"
        self.now += 3600
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(len(self.pushes), 2)
        self.assertNotEqual(vault_pull.read_state().outage, first)

    def test_failed_send_retries_next_hour_and_marks_alerted_only_after_success(self) -> None:
        self.scenario = "tracked"
        self.send_ok = False
        self.assertEqual(self.hour()[0], 1)
        self.assertEqual(vault_pull.read_state().outage, vault_pull.OpenOutage("tracked_files_changed", self.now, False,
                                                            vault_pull.stop_details(vault_pull.TrackedFilesChanged())[1]))
        since = self.now
        self.send_ok = True
        self.now += 3600
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(len(self.pushes), 2)
        self.assertEqual(vault_pull.read_state().outage, vault_pull.OpenOutage("tracked_files_changed", since, True,
                                                            vault_pull.stop_details(vault_pull.TrackedFilesChanged())[1]))
        self.now += 3600
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(len(self.pushes), 2)

    def test_no_state_save_after_a_sent_alert_can_make_the_next_hour_repeat_it(self) -> None:
        self.scenario = "tracked"
        save = vault_pull.write_state

        def fail_after_send(state: vault_pull.VaultState) -> None:
            if self.pushes:
                raise OSError("state disk unavailable after delivery")
            save(state)

        with mock.patch.object(vault_pull, "write_state", side_effect=fail_after_send):
            self.assertEqual(self.hour()[0], 0)
        self.assertEqual(vault_pull.read_state().outage,
                         vault_pull.OpenOutage("tracked_files_changed", self.now, True,
                                              vault_pull.stop_details(vault_pull.TrackedFilesChanged())[1]))
        self.now += 3600
        self.assertEqual(self.hour()[0], 0)
        self.assertEqual(len(self.pushes), 1)

    def test_a_failed_pre_send_state_save_does_not_send_an_alert(self) -> None:
        self.scenario = "tracked"
        save = vault_pull.write_state

        def fail_alert_commit(state: vault_pull.VaultState) -> None:
            if isinstance(state.outage, vault_pull.OpenOutage) and state.outage.alerted:
                raise OSError("cannot save alert intent")
            save(state)

        with mock.patch.object(vault_pull, "write_state", side_effect=fail_alert_commit):
            with self.assertRaises(OSError):
                _ = self.hour()
        self.assertEqual(self.pushes, [])

    def test_transport_uses_one_ssh_git_c_and_no_destructive_commands(self) -> None:
        self.assertIsInstance(vault_pull.pull(), vault_pull.Pulled)
        push, ssh = self.commands
        self.assertEqual(push[:3], ["git", "-C", "/home/natepiano/rust/hanadocs"])
        self.assertIn("push", push)
        self.assertEqual(push[-2:], ["fixture-mac:rust/hanadocs", "refs/remotes/origin/main:refs/remotes/origin/main"])
        self.assertNotIn("--force", push)
        self.assertEqual(ssh[:-1], ["ssh", *sync.ssh_options(), "fixture-mac"])
        tokens = shlex.split(ssh[-1])
        for forbidden in ("cd", "stash", "reset", "checkout"):
            self.assertNotIn(forbidden, tokens)
        for command in self.remote_commands():
            self.assertEqual(command[:2], ["-C", "rust/hanadocs"])
        self.assertIn(["-C", "rust/hanadocs", "status", "--porcelain", "--untracked-files=no"], self.remote_commands())

    def test_manual_vault_command_uses_the_same_state_and_transport(self) -> None:
        with redirect_stdout(StringIO()):
            self.assertEqual(cli.main(["vault"]), 0)
        self.assertEqual(vault_pull.read_state().last_good, vault_pull.LastGoodPull(self.now))
        self.assertEqual(len(self.commands), 2)


if __name__ == "__main__":
    _ = unittest.main()
