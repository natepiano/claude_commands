"""Check SSH routing before reservation with private target state."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from functools import partial
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING, cast, override

from scripts.lib import process_start as process_identity
from scripts.lib.process_start_test_support import process_reader

import unittest
from unittest import mock


if TYPE_CHECKING:
    from scripts.build_targets import build_targets, targets
    from scripts.aws_target import ledger
    from scripts.aws_target.ledger_test_support import (
        REAL_POPEN, git_only, install_cost_settings, instant, numeric_settings,
    )
    from scripts.mac_test import mac_test as mac_test, offload as offload
    from scripts.build_hold import build_hold
    from scripts.lint.memory_test_support import config_environment, write_config_fixture
else:
    from scripts.build_targets import build_targets, targets
    from scripts.mac_test import mac_test, offload
    from scripts.build_hold import build_hold
    from scripts.aws_target import ledger
    from scripts.aws_target.ledger_test_support import (
        REAL_POPEN, git_only, install_cost_settings, instant, numeric_settings,
    )
    from scripts.lint.memory_test_support import config_environment, write_config_fixture


class SshEligibilityTests(unittest.TestCase):
    root: Path = Path()
    commands: list[list[str]] = []
    reachable: bool = True
    now: int = instant("2026-01-15T00:00:00+00:00")
    notices: list[ledger.LimitEvent] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.commands = []
        self.reachable = True
        self.notices = []
        self.now = instant("2026-01-15T00:00:00+00:00")
        environment = config_environment(self.root)
        _ = (self.root / "build_targets.conf").write_text(
            "targets=mac,natedev,aws\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\n"
            + "kind.aws=ssh\nplatform.aws=linux\nsettings.aws=aws_target.conf\n")
        _ = write_config_fixture(self.root / "mac_test.conf", "offload=on\nlinux_only.hana=linux-part\n")
        install_cost_settings(self.root / "aws_target.conf",
                              "offload=on\nhost=fake-aws\nkinds=test,check,doc\nslots=2\nslots.hana=2\n")
        _ = (self.root / "Cargo.toml").write_text('[package]\nname="linux-part"\nversion="0.1.0"\n')
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            **environment, "HOME": str(self.root), "MAC_TEST_CONFIG": str(self.root / "mac_test.conf"),
            "MAC_TEST_STATE_DIR": str(self.root / "mac-state"),
            "BUILDLOG_DIR": str(self.root / "buildlog"), "BUILD_HOLD_DIR": str(self.root / "holds"),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "releases"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
        })))
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))
        _ = self.enterContext(mock.patch.object(offload, "repository_identity",
                                               return_value=offload.KnownRepository("hana", "github.com/team/hana")))
        _ = self.enterContext(mock.patch.object(mac_test, "target_claim_scope",
                                               return_value=mac_test.RepositoryScope("hana", "github.com/team/hana")))
        _ = self.enterContext(mock.patch.object(process_identity, "process_start", side_effect=process_reader(self.process_start)))
        _ = self.enterContext(mock.patch.object(mac_test, "start_claim_watcher"))
        _ = self.enterContext(mock.patch.object(build_hold, "owning_session", return_value=build_hold.NoOwningSession()))
        book = ledger.CostLedger(mac_test.state_paths("aws").directory)
        book.record_inventory(ledger.ResourceInventory(self.now, (
            ledger.PricedResource("retained-storage", Decimal("0")),), "priced fixture inventory"))
        book.observe(ledger.InstanceObservation(self.now, "stopped", "fake DescribeInstances"))
        _ = self.enterContext(mock.patch.object(
            ledger, "spending_guard",
            new=partial(ledger.spending_guard, clock=lambda: self.now, notify=self.notice)))
        _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=AssertionError("Unexpected external command")))
        _ = self.enterContext(mock.patch.object(subprocess, "Popen", side_effect=AssertionError("Unexpected child process")))
        _ = self.enterContext(mock.patch.object(offload.ProcessController, "capture", side_effect=self.capture))
        _ = self.enterContext(mock.patch.object(offload, "probe_mac", side_effect=AssertionError("No execution probe before reservation")))

    def process_start(self, pid: int) -> str:
        return f"start-{pid}"

    def notice(self, event: ledger.LimitEvent) -> bool:
        self.notices.append(event)
        return True

    def capture(self, arguments: Sequence[str],
                _deadline: offload.CommandDeadline = offload.NO_COMMAND_TIME_LIMIT) -> offload.CommandOutput:
        argv = list(arguments)
        self.commands.append(argv)
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[-1], "true", "Routing must not evaluate a shell")
        return offload.CommandOutput(0 if self.reachable else 255, "", "")

    def answer(self, kind: str = "test", local: bool = False) -> list[list[str]]:
        output = io.StringIO()
        arguments = ["build_targets.py", "eligible", "--kind", kind, "--repo-root", str(self.root),
                     "--package", "linux-part"]
        if local:
            arguments += ["--local", "requires local files"]
        with mock.patch.object(sys, "argv", arguments), redirect_stdout(output):
            self.assertEqual(build_targets.main(), 0)
        return [line.split("\t") for line in output.getvalue().splitlines()[1:]]

    def aws(self, kind: str = "test", local: bool = False) -> list[str]:
        return next(row for row in self.answer(kind, local) if row[0] == "aws")

    def occupy(self, pid: int) -> None:
        args = mac_test.CliArguments()
        args.target, args.worktree, args.pid = "aws", str(self.root), pid
        args.what, args.remote = "check hana", "fake-aws"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(mac_test.command_claim(args, mac_test.state_paths("aws")), 0)

    def decision(self) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(build_targets.command_kind_eligible(
                self.root, "linux-part", "test", targets.ANY_ELIGIBLE_TARGET), 0)
        return output.getvalue().splitlines()[0]

    def reserve(self, name: str = "aws", *, recover_only: bool = False) -> int:
        args = build_targets.CliArguments()
        args.kind, args.repo_root, args.package = "test", str(self.root), "linux-part"
        args.pid, args.target = 501, name
        args.reservation_file = str(self.root / "reservation.tsv")
        args.recover_only = recover_only
        return build_targets.command_reserve(args)

    def test_pinned_stopped_aws_defers_reachability_until_the_controller_starts_it(self) -> None:
        paths = mac_test.state_paths("aws")
        paths.directory.mkdir(parents=True, exist_ok=True)
        _ = (paths.directory / "controller.json").write_text('{"state":"stopped"}')
        args = build_targets.CliArguments()
        args.target, args.kind, args.repo_root, args.package = "aws", "test", str(self.root), "linux-part"
        output = io.StringIO()
        self.reachable = False
        with redirect_stdout(output):
            self.assertEqual(build_targets.command_pinned_eligible(args, targets.ANY_ELIGIBLE_TARGET), 0)
        self.assertIn(f"aws\texcluded\tstarting\t{self.root / 'aws_target.conf'}\tlinux\n", output.getvalue())
        self.assertEqual(self.commands, [])
        _ = (paths.directory / "controller.json").write_text('{"state":"running"}')
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(build_targets.command_pinned_eligible(args, targets.ANY_ELIGIBLE_TARGET), 2)
        self.assertTrue(self.commands)

    def test_pinned_final_requires_check_and_test_before_waiting_for_aws(self) -> None:
        paths = mac_test.state_paths("aws")
        paths.directory.mkdir(parents=True, exist_ok=True)
        _ = (paths.directory / "controller.json").write_text('{"state":"stopped"}')
        args = build_targets.CliArguments()
        args.target, args.kind, args.repo_root, args.package = "aws", "final", str(self.root), "linux-part"
        settings = self.root / "aws_target.conf"
        original = settings.read_text()
        for kinds, expected in (("check", 2), ("test", 2), ("test,check", 0)):
            with self.subTest(kinds=kinds):
                _ = settings.write_text(original.replace("kinds=test,check,doc", "kinds=" + kinds))
                with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                    self.assertEqual(build_targets.command_pinned_eligible(args, targets.ANY_ELIGIBLE_TARGET), expected)
        self.assertEqual(self.commands, [])

    def test_pinned_stopped_aws_still_refuses_a_spending_limit(self) -> None:
        paths = mac_test.state_paths("aws")
        paths.directory.mkdir(parents=True, exist_ok=True)
        _ = (paths.directory / "controller.json").write_text('{"state":"stopped"}')
        numeric_settings(self.root / "aws_target.conf", {"monthly_limit_usd": "0.001"})
        book = ledger.CostLedger(paths.directory)
        book.record_inventory(ledger.ResourceInventory(self.now - 86400,
            (ledger.PricedResource("retained-storage", Decimal("1")),), "fixture inventory"))
        args = build_targets.CliArguments()
        args.target, args.kind, args.repo_root, args.package = "aws", "test", str(self.root), "linux-part"
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(build_targets.command_pinned_eligible(args, targets.ANY_ELIGIBLE_TARGET), 2)
        self.assertIn("spending", errors.getvalue())
        self.assertEqual(self.commands, [])

    def test_a_mac_block_stops_a_memory_wait_from_reusing_its_reservation(self) -> None:
        _ = write_config_fixture(self.root / "mac_test.conf", "offload=on\nlinux_only.hana=\n")
        self.assertEqual(self.reserve("mac"), 0)
        paths = mac_test.state_paths("mac")
        claim, = mac_test.read_claims(paths)
        block = mac_test.ActiveMacBlock(
            "assembly", "assembly checks", datetime.now(timezone.utc),
            datetime.now(timezone.utc) + timedelta(hours=1),
            mac_test.RecordedSession("assembly-session", "assembly"), mac_test.NO_SHOWRUNNER,
            "off_by_this_block", mac_test.FREE_MESSAGE_NOT_NEEDED)
        mac_test.write_block(paths.block, block)
        for published in (True, False):
            with self.subTest(reservation_published=published):
                destination = self.root / "reservation.tsv"
                if not published:
                    destination.unlink(missing_ok=True)
                self.assertEqual(self.reserve("mac"), 1)
                self.assertFalse(destination.exists())
                retained, = mac_test.read_claims(paths)
                self.assertEqual(retained.record, claim.record)

    def test_disabled_aws_neither_contacts_a_host_nor_reserves_capacity(self) -> None:
        _ = write_config_fixture(self.root / "aws_target.conf", "offload=off\n")
        self.assertEqual(self.aws()[1:3], ["excluded", "off"])
        self.assertEqual(self.reserve(), 1)
        self.assertEqual(self.commands, [])
        self.assertFalse((self.root / "reservation.tsv").exists())
        self.assertEqual(mac_test.read_claims(mac_test.state_paths("aws")), ())

    def add_spare_target(self) -> None:
        _ = write_config_fixture(self.root / "spare.conf",
                                 "offload=on\nhost=fake-spare\nkinds=test,check,doc\n", template="aws_target")
        table = self.root / "build_targets.conf"
        _ = table.write_text(table.read_text().replace("targets=mac,natedev,aws", "targets=mac,natedev,aws,spare")
                             + "kind.spare=ssh\nplatform.spare=linux\nsettings.spare=spare.conf\n")

    def required_slot(self, claim: mac_test.HeldClaim) -> int:
        if "slot" not in claim.record:
            self.fail("The Linux claim has no slot")
        return claim.record["slot"]

    def test_spending_refusal_cleans_exited_owner_claims_without_new_work(self) -> None:
        self.occupy(71)
        self.occupy(72)
        paths = mac_test.state_paths("aws")
        original = mac_test.read_claims(paths)
        numeric_settings(self.root / "aws_target.conf", {"daily_limit_usd": "3"})
        cleaned: list[int] = []

        def cleanup(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(arguments[0], "ssh")
            self.assertIn("fake-aws", arguments)
            for claim in mac_test.read_claims(paths):
                if claim.record["pid"] == 501:
                    self.assertTrue(claim.record.get("cleanup_pending"))
                    slot = self.required_slot(claim)
                    expected = offload.leftover_check_script(
                        offload.KnownRepository("hana", "github.com/team/hana"), "aws", slot)
                    self.assertEqual(arguments[-1], expected)
                    cleaned.append(slot)
            return subprocess.CompletedProcess(arguments, 0, "leftovers=0\nrc=0\n", "")

        def start(pid: int) -> str | None:
            return None if pid in (71, 72) else self.process_start(pid)

        with mock.patch.object(process_identity, "process_start", side_effect=process_reader(start)), \
                mock.patch.object(subprocess, "run", side_effect=cleanup), redirect_stderr(io.StringIO()):
            self.assertEqual(self.aws()[1:3], ["excluded", "spending_limit"])
            self.assertEqual(self.reserve(recover_only=True), 1)
        self.assertCountEqual(cleaned, [self.required_slot(claim) for claim in original])
        self.assertEqual(mac_test.read_claims(paths), ())
        self.assertFalse((self.root / "reservation.tsv").exists())
        self.assertEqual(self.commands, [])
        self.assertFalse(offload.backoff_path("aws").exists())

    def test_spending_refusal_keeps_unconfirmed_cleanup_owned_until_retry(self) -> None:
        self.occupy(71)
        paths = mac_test.state_paths("aws")
        original, = mac_test.read_claims(paths)
        numeric_settings(self.root / "aws_target.conf", {"daily_limit_usd": "3"})
        def start(pid: int) -> str | None:
            return None if pid == 71 else self.process_start(pid)

        with mock.patch.object(process_identity, "process_start", side_effect=process_reader(start)), \
                mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                    ["ssh"], 0, "leftovers=2\nrc=0\n", "")), redirect_stderr(io.StringIO()):
            self.assertEqual(self.reserve(recover_only=True), 1)
        retained, = mac_test.read_claims(paths)
        self.assertEqual(retained.path, original.path)
        self.assertEqual(retained.record["pid"], 501)
        self.assertEqual(retained.record["proc_start"], self.process_start(501))
        self.assertTrue(retained.record.get("cleanup_pending"))
        self.assertFalse((self.root / "reservation.tsv").exists())
        self.assertFalse(offload.backoff_path("aws").exists())
        _ = (self.root / "reservation.tsv").write_text(f"aws\t{self.root / 'aws_target.conf'}\n")
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                ["ssh"], 0, "leftovers=0\nrc=0\n", "")), redirect_stderr(io.StringIO()):
            self.assertEqual(self.reserve(recover_only=True), 1)
        self.assertEqual(mac_test.read_claims(paths), ())
        self.assertFalse((self.root / "reservation.tsv").exists())
        self.assertEqual(self.commands, [])

    def test_spending_refusal_skips_own_live_claim_without_a_reservation_file(self) -> None:
        self.add_spare_target()
        self.assertEqual(self.reserve("spare"), 0)
        (self.root / "reservation.tsv").unlink()
        self.assertEqual(self.reserve(), 0)
        (self.root / "reservation.tsv").unlink()
        paths = mac_test.state_paths("aws")
        original, = mac_test.read_claims(paths)
        numeric_settings(self.root / "aws_target.conf", {"daily_limit_usd": "3"})
        self.commands.clear()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.aws()[1:3], ["excluded", "spending_limit"])
            self.commands.clear()
            self.assertEqual(self.reserve("", recover_only=True), 0)
        self.assertEqual((self.root / "reservation.tsv").read_text(), f"spare\t{self.root / 'spare.conf'}\n")
        retained, = mac_test.read_claims(paths)
        self.assertEqual(retained.record, original.record)
        self.assertNotIn("cleanup_pending", retained.record)
        self.assertEqual(self.commands, [])

    def test_invalid_live_limit_excludes_only_aws_and_reserves_the_next_target(self) -> None:
        self.add_spare_target()
        settings = self.root / "aws_target.conf"
        original = settings.read_text()
        for key in ("daily_limit_usd", "monthly_limit_usd"):
            with self.subTest(key=key):
                _ = settings.write_text(original)
                numeric_settings(settings, {key: "broken"})
                self.commands.clear()
                with mock.patch.object(subprocess, "Popen", new=REAL_POPEN), \
                        mock.patch.object(subprocess, "run", new=git_only), redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(self.aws()[1:3], ["excluded", "spending_unavailable"])
                    self.assertEqual(self.reserve(""), 0)
                self.assertIn(key, errors.getvalue())
                self.assertIn("spending refused", errors.getvalue())
                self.assertEqual((self.root / "reservation.tsv").read_text(), f"spare\t{self.root / 'spare.conf'}\n")
                self.assertEqual(mac_test.read_claims(mac_test.state_paths("aws")), ())
                self.assertTrue(all("fake-spare" in command for command in self.commands))
                self.assertEqual(build_targets.command_release_wait(501), 0)
                (self.root / "reservation.tsv").unlink()

    def test_invalid_spending_limit_still_allows_retained_claim_cleanup(self) -> None:
        self.occupy(71)
        paths = mac_test.state_paths("aws")
        original, = mac_test.read_claims(paths)
        numeric_settings(self.root / "aws_target.conf", {"daily_limit_usd": "broken"})
        cleaned: list[list[str]] = []

        def start(pid: int) -> str | None:
            return None if pid == 71 else self.process_start(pid)

        def cleanup(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
            if arguments[0] == "git":
                return git_only(arguments, **options)
            self.assertEqual(arguments[0], "ssh")
            self.assertIn("fake-aws", arguments)
            self.assertEqual(arguments[-1], offload.leftover_check_script(
                offload.KnownRepository("hana", "github.com/team/hana"), "aws", self.required_slot(original)))
            owned, = mac_test.read_claims(paths)
            self.assertEqual(owned.record["pid"], 501)
            self.assertTrue(owned.record.get("cleanup_pending"))
            cleaned.append(list(arguments))
            return subprocess.CompletedProcess(arguments, 0, "leftovers=0\nrc=0\n", "")

        with mock.patch.object(process_identity, "process_start", side_effect=process_reader(start)), \
                mock.patch.object(subprocess, "Popen", new=REAL_POPEN), \
                mock.patch.object(subprocess, "run", side_effect=cleanup), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(self.aws()[1:3], ["excluded", "spending_unavailable"])
            self.assertEqual(self.reserve(recover_only=True), 1)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(mac_test.read_claims(paths), ())
        self.assertIn("daily_limit_usd", errors.getvalue())
        self.assertFalse((self.root / "reservation.tsv").exists())
        self.assertFalse(offload.backoff_path("aws").exists())
        self.assertEqual(self.commands, [])

    def test_invalid_spending_limit_on_disabled_aws_does_not_stop_selection(self) -> None:
        self.add_spare_target()
        settings = self.root / "aws_target.conf"
        original = settings.read_text()
        book = ledger.CostLedger(mac_test.state_paths("aws").directory)
        for published in (False, True):
            with self.subTest(reservation_published=published):
                _ = settings.write_text(original)
                self.assertEqual(self.reserve(), 0)
                reservation = self.root / "reservation.tsv"
                if not published:
                    reservation.unlink()
                claim, = mac_test.read_claims(mac_test.state_paths("aws"))
                before = {path: path.read_bytes() for path in book.path.parent.rglob("*") if path.is_file()}
                _ = settings.write_text(settings.read_text().replace("offload=on", "offload=off"))
                numeric_settings(settings, {"daily_limit_usd": "broken"})
                self.commands.clear()
                with mock.patch.object(ledger, "spending_guard", side_effect=AssertionError("Disabled AWS must not read the guard")), \
                        mock.patch.object(subprocess, "Popen", new=REAL_POPEN), \
                        mock.patch.object(subprocess, "run", new=git_only):
                    self.assertEqual(self.aws()[1:3], ["excluded", "off"])
                    self.assertEqual(self.reserve(""), 0)
                self.assertEqual(reservation.read_text(), f"spare\t{self.root / 'spare.conf'}\n")
                retained, = mac_test.read_claims(mac_test.state_paths("aws"))
                self.assertEqual(retained.record, claim.record)
                self.assertTrue(all("fake-spare" in command for command in self.commands))
                self.assertTrue(self.commands)
                self.assertEqual({path: path.read_bytes() for path in before}, before)
                self.assertEqual(build_targets.command_release_wait(501), 0)
                reservation.unlink()

    def test_refused_cleanup_keeps_claims_when_connection_settings_are_unusable(self) -> None:
        settings = self.root / "aws_target.conf"
        original_settings = settings.read_text()
        for key in ("host", "connect_timeout_s", "leftover_timeout_s"):
            with self.subTest(setting=key):
                _ = settings.write_text(original_settings)
                self.occupy(71)
                paths = mac_test.state_paths("aws")
                original, = mac_test.read_claims(paths)
                numeric_settings(settings, {"daily_limit_usd": "broken"})
                if key == "host":
                    _ = settings.write_text(settings.read_text().replace("host=fake-aws\n", ""))
                else:
                    numeric_settings(settings, {key: "broken"})

                def start(pid: int) -> str | None:
                    return None if pid == 71 else self.process_start(pid)

                with mock.patch.object(process_identity, "process_start", side_effect=process_reader(start)), \
                        mock.patch.object(subprocess, "Popen", new=REAL_POPEN), \
                        mock.patch.object(subprocess, "run", new=git_only), redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(self.reserve(recover_only=True), 1)
                retained, = mac_test.read_claims(paths)
                self.assertEqual(retained.path, original.path)
                self.assertEqual(retained.record.get("remote_host"), "fake-aws")
                self.assertIn(key, errors.getvalue())
                self.assertIn("cleanup settings unavailable", errors.getvalue())
                self.assertFalse((self.root / "reservation.tsv").exists())
                self.assertFalse(offload.backoff_path("aws").exists())
                self.assertEqual(self.commands, [])
                retained.path.unlink()

    def test_refused_cleanup_uses_the_recorded_host_and_live_connection_limits(self) -> None:
        self.occupy(71)
        paths = mac_test.state_paths("aws")
        settings = self.root / "aws_target.conf"
        _ = settings.write_text(settings.read_text().replace("host=fake-aws", "host=new-host"))
        numeric_settings(settings, {"daily_limit_usd": "broken", "connect_timeout_s": "9", "leftover_timeout_s": "23"})
        calls: list[list[str]] = []

        def start(pid: int) -> str | None:
            return None if pid == 71 else self.process_start(pid)

        def cleanup(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
            if arguments[0] == "git":
                return git_only(arguments, **options)
            calls.append(list(arguments))
            self.assertEqual(arguments[-2], "fake-aws")
            self.assertIn("ConnectTimeout=9", arguments)
            self.assertNotIn("new-host", arguments)
            self.assertEqual(options["timeout"], 23)
            return subprocess.CompletedProcess(arguments, 0, "leftovers=0\nrc=0\n", "")

        with mock.patch.object(process_identity, "process_start", side_effect=process_reader(start)), \
                mock.patch.object(subprocess, "Popen", new=REAL_POPEN), \
                mock.patch.object(subprocess, "run", side_effect=cleanup), redirect_stderr(io.StringIO()):
            self.assertEqual(self.reserve(recover_only=True), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(mac_test.read_claims(paths), ())
        self.assertFalse((self.root / "reservation.tsv").exists())

    def test_recovery_only_does_not_create_a_new_claim(self) -> None:
        self.assertEqual(self.reserve(recover_only=True), 1)
        self.assertFalse((self.root / "reservation.tsv").exists())
        self.assertEqual(mac_test.read_claims(mac_test.state_paths("aws")), ())

    def test_busy_mac_recovery_keeps_the_existing_reserve_status(self) -> None:
        _ = write_config_fixture(self.root / "mac_test.conf", "offload=on\nlinux_only.hana=\n")
        self.assertEqual(self.reserve("mac", recover_only=True), 11)
        self.assertFalse((self.root / "reservation.tsv").exists())

    def test_spending_limits_exclude_aws_and_keep_retries_without_reservations(self) -> None:
        for key in ("daily_limit_usd", "monthly_limit_usd"):
            with self.subTest(key=key):
                settings = self.root / "aws_target.conf"
                original = settings.read_text()
                numeric_settings(settings, {key: "3"})
                with redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(self.aws()[1:3], ["excluded", "spending_limit"])
                    self.assertEqual(self.decision(), "any_target")
                    self.assertEqual(self.reserve(), 1)
                self.assertIn("spending refused", errors.getvalue())
                self.assertEqual(self.commands, [])
                self.assertFalse((self.root / "reservation.tsv").exists())
                self.assertEqual(mac_test.read_claims(mac_test.state_paths("aws")), ())
                _ = settings.write_text(original)

    def test_spending_refusal_continues_to_another_eligible_target(self) -> None:
        settings = self.root / "aws_target.conf"
        numeric_settings(settings, {"daily_limit_usd": "3"})
        _ = write_config_fixture(self.root / "spare.conf",
                                 "offload=on\nhost=fake-spare\nkinds=test,check,doc\n", template="aws_target")
        with (self.root / "build_targets.conf").open("a") as stream:
            _ = stream.write("kind.spare=ssh\nplatform.spare=linux\nsettings.spare=spare.conf\n")
        table = self.root / "build_targets.conf"
        _ = table.write_text(table.read_text().replace("targets=mac,natedev,aws", "targets=mac,natedev,aws,spare"))
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.reserve(""), 0)
        self.assertEqual((self.root / "reservation.tsv").read_text(), f"spare\t{self.root / 'spare.conf'}\n")
        self.assertEqual(mac_test.read_claims(mac_test.state_paths("aws")), ())
        self.assertEqual(len(mac_test.read_claims(mac_test.state_paths("spare"))), 1)
        self.assertTrue(self.commands)
        self.assertTrue(all("fake-spare" in command for command in self.commands))

    def test_a_new_limit_refusal_prevents_reuse_of_an_existing_reservation(self) -> None:
        self.assertEqual(self.reserve(), 0)
        claims = mac_test.read_claims(mac_test.state_paths("aws"))
        self.assertEqual(len(claims), 1)
        numeric_settings(self.root / "aws_target.conf", {"daily_limit_usd": "3"})
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.reserve(), 1)
        self.assertFalse((self.root / "reservation.tsv").exists())

    def test_unavailable_cost_evidence_excludes_aws_before_ssh_or_claim_creation(self) -> None:
        book = ledger.CostLedger(mac_test.state_paths("aws").directory)
        _ = book.path.write_bytes(b"{unfinished ledger")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.aws()[1:3], ["excluded", "spending_unavailable"])
            self.assertEqual(self.reserve(), 1)
            self.assertEqual(self.decision(), "any_target")
        self.assertEqual(self.commands, [])
        self.assertEqual(mac_test.read_claims(mac_test.state_paths("aws")), ())
        self.assertEqual(book.path.read_bytes(), b"{unfinished ledger")

    def test_recovered_live_limits_refuse_real_eligibility_and_reservation(self) -> None:
        settings = self.root / "aws_target.conf"
        with mock.patch.object(subprocess, "Popen", new=REAL_POPEN), \
                mock.patch.object(subprocess, "run", new=git_only):
            for arguments in (("init", "-q"), ("config", "user.name", "Cost fixture"),
                              ("config", "user.email", "cost@example.invalid"),
                              ("add", settings.name), ("commit", "-qm", "Record spending limits")):
                _ = subprocess.run(["git", "-C", str(self.root), *arguments], check=True)
            original = settings.read_text()
            for fault in ("missing", "broken"):
                with self.subTest(fault=fault):
                    if fault == "missing":
                        damaged = "\n".join(line for line in original.splitlines()
                                            if not line.startswith("daily_limit_usd=")) + "\n"
                    else:
                        damaged = original.replace("daily_limit_usd=20", "daily_limit_usd=broken")
                    _ = settings.write_text(damaged)
                    with redirect_stderr(io.StringIO()) as errors:
                        self.assertEqual(self.aws()[1:3], ["excluded", "spending_unavailable"])
                        self.assertEqual(self.reserve(), 1)
                    self.assertIn("daily_limit_usd", errors.getvalue())
                    self.assertEqual(self.commands, [])
                    self.assertEqual(mac_test.read_claims(mac_test.state_paths("aws")), ())

    def test_linux_backoff_keeps_the_memory_leave_route(self) -> None:
        offload.write_backoff(10**12, "aws")
        self.assertEqual(self.decision(), "any_target")
        self.assertEqual(self.commands, [])

    def test_unreachable_linux_target_keeps_the_memory_leave_route(self) -> None:
        self.reachable = False
        self.assertEqual(self.decision(), "any_target")
        self.assertTrue(self.commands)

    def test_disk_refusal_keeps_the_memory_leave_route(self) -> None:
        with mock.patch.object(mac_test, "free_now", return_value="disk"):
            self.assertEqual(self.decision(), "any_target")
        self.assertEqual(self.commands, [])

    def test_capacity_exclusions_keep_the_memory_leave_route_available(self) -> None:
        self.occupy(71)
        self.occupy(72)
        self.assertEqual(self.decision(), "any_target")
        with mock.patch.object(mac_test, "free_now", return_value="blocked"):
            self.assertEqual(self.decision(), "any_target")
        self.assertEqual(self.commands, [])

    def test_linux_only_work_keeps_linux_capacity_without_a_mac(self) -> None:
        rows = self.answer()
        self.assertEqual([row[0] for row in rows], ["mac", "natedev", "aws"])
        self.assertEqual(rows[0][1:3], ["excluded", "linux_only"])
        self.assertEqual(rows[2][1:3], ["eligible", "-"])
        self.assertEqual([row[4] for row in rows], ["macos", "-", "linux"])
        self.assertTrue(self.commands)

    def test_linux_test_check_and_doc_need_no_mac_repository_setting(self) -> None:
        for kind in ("test", "check", "doc"):
            with self.subTest(kind=kind):
                self.assertEqual(self.aws(kind)[1], "eligible")

    def test_local_only_request_excludes_every_ssh_target(self) -> None:
        rows = self.answer(local=True)
        self.assertEqual([(row[0], row[1]) for row in rows],
                         [("mac", "excluded"), ("natedev", "eligible"), ("aws", "excluded")])
        self.assertEqual(self.commands, [])

    def test_artifacts_final_gate_and_lint_never_use_linux_ssh(self) -> None:
        for kind in ("build", "example", "final", "lint"):
            with self.subTest(kind=kind):
                self.assertEqual(self.aws(kind)[1], "excluded")
        self.assertEqual(self.commands, [])

    def test_configured_kinds_are_read_on_each_eligibility_check(self) -> None:
        self.assertEqual(self.aws("check")[1], "eligible")
        _ = write_config_fixture(self.root / "aws_target.conf", "offload=on\nkinds=test,doc\n")
        self.assertEqual(self.aws("check")[1], "excluded")

    def test_mac_backoff_does_not_exclude_linux_but_linux_backoff_does(self) -> None:
        offload.write_backoff(10**12, "mac")
        self.assertEqual(self.aws()[1], "eligible")
        self.commands.clear()
        offload.write_backoff(10**12, "aws")
        self.assertEqual(self.aws()[1:3], ["excluded", "backoff"])
        self.assertEqual(self.commands, [])

    def test_all_occupied_slots_exclude_target_before_ssh(self) -> None:
        self.occupy(71)
        self.assertEqual(self.aws()[1], "eligible")
        self.occupy(72)
        self.commands.clear()
        self.assertEqual(self.aws()[1:3], ["excluded", "busy"])
        self.assertEqual(self.commands, [])

    def test_unreachable_host_is_excluded_without_shell_evaluation(self) -> None:
        self.reachable = False
        self.assertEqual(self.aws()[1:3], ["excluded", "unreachable"])
        self.assertTrue(self.commands)

    def test_queue_retries_never_evaluate_or_reserve_a_shell(self) -> None:
        with mock.patch.object(mac_test, "command_claim", side_effect=AssertionError("Routing cannot reserve")):
            for _ in range(3):
                self.assertEqual(self.aws()[1], "eligible")
        self.assertTrue(self.commands)
        self.assertEqual(mac_test.read_claims(mac_test.state_paths("aws")), ())


if __name__ == "__main__":
    _ = unittest.main()
