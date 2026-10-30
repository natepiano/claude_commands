"""Read target configuration and temporary build coordination records."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError, is_dataclass
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, TypedDict, cast, override
from unittest import mock

import targets
import build_hold
import mac_test
import offload


SHIPPED_CONFIG = Path(__file__).resolve().parents[2] / "config" / "build_targets.conf"
NOW = datetime(2026, 10, 11, 21, 0, tzinfo=timezone.utc)
SINCE = datetime(2026, 10, 11, 20, 5, tzinfo=timezone.utc)
END = datetime(2026, 10, 11, 22, 5, tzinfo=timezone.utc)

HoldRecord = TypedDict(
    "HoldRecord",
    {"holder": str, "since": str, "for": str, "release_eta": str},
)


class ClaimRecord(TypedDict):
    pid: int
    proc_start: str
    what: str
    worktree: str
    since: str


class FreeMessageRecord(TypedDict):
    kind: Literal["not_needed"]


ActiveBlockRecord = TypedDict(
    "ActiveBlockRecord",
    {
        "version": int,
        "holder": str,
        "for": str,
        "since": str,
        "expires": str,
        "session": str,
        "showrunner": str,
        "state": Literal["active"],
        "ci": Literal["off_by_this_block"],
        "free_message": FreeMessageRecord,
    },
)


class IsolatedTargetTest(unittest.TestCase):
    root: Path = Path()
    holders: Path = Path()
    mac_state: Path = Path()
    settings: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.holders = self.root / "holders"
        self.mac_state = self.root / "mac-state"
        self.settings = self.root / "mac_test.conf"
        self.holders.mkdir()
        self.mac_state.mkdir()
        (self.root / "sessions").mkdir()
        _ = self.settings.write_text("host=test-mac\n", encoding="utf-8")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "BUILD_HOLD_DIR": str(self.holders),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "MAC_TEST_STATE_DIR": str(self.mac_state),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "CLAUDE_CODE_SESSION_ID": "",
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "UNIT_ACTIVE_DIR": str(self.root / "active"),
            "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
        })))
        _ = self.enterContext(mock.patch.object(build_hold, "record_hold_event"))
        _ = os.environ.pop("BUILD_TARGETS_CONFIG", None)
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))
        _ = self.enterContext(mock.patch(
            "subprocess.run", side_effect=AssertionError("State reads must stay local"),
        ))
        _ = self.enterContext(mock.patch(
            "subprocess.Popen", side_effect=AssertionError("State reads must stay local"),
        ))

    def write_config(self, content: str, name: str = "targets.conf") -> Path:
        path = self.root / name
        _ = path.write_text(content, encoding="utf-8")
        return path

    def write_holder(self, release: str = "unknown") -> None:
        record: HoldRecord = {
            "holder": "hana",
            "since": SINCE.isoformat(),
            "for": "render checks",
            "release_eta": release,
        }
        _ = (self.holders / "hana").write_text(json.dumps(record), encoding="utf-8")

    def write_block(self, expires: datetime = END) -> None:
        record: ActiveBlockRecord = {
            "version": 2,
            "holder": "hana",
            "for": "render checks",
            "since": SINCE.isoformat(),
            "expires": expires.isoformat(),
            "session": "hana-session",
            "showrunner": "build-followups",
            "state": "active",
            "ci": "off_by_this_block",
            "free_message": {"kind": "not_needed"},
        }
        _ = (self.mac_state / "block.json").write_text(json.dumps(record), encoding="utf-8")

    def write_claim(self, file_name: str = "run.json", what: str = "nextest hana", since: datetime = SINCE) -> None:
        record: ClaimRecord = {
            "pid": 12345,
            "proc_start": "test-run-start",
            "what": what,
            "worktree": str(self.root / "hana"),
            "since": since.isoformat(),
        }
        path = self.mac_state / file_name
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps(record), encoding="utf-8")


class ConfigurationTests(IsolatedTargetTest):
    def test_shipped_config_reads_mac_then_natedev(self) -> None:
        self.assertEqual(targets.read_targets(SHIPPED_CONFIG), (
            targets.SshTarget("mac", "macos", SHIPPED_CONFIG.with_name("mac_test.conf")),
            targets.LocalTarget("natedev", "linux"),
        ))

    def test_order_follows_targets_key(self) -> None:
        config = self.write_config(
            "targets=natedev,mac\n"
            + "kind.natedev=local\nplatform.natedev=linux\n"
            + "kind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
        )
        self.assertEqual(targets.read_targets(config), (
            targets.LocalTarget("natedev", "linux"),
            targets.SshTarget("mac", "macos", self.settings),
        ))

    def test_environment_selects_config_path(self) -> None:
        config = self.write_config("targets=fixture\nkind.fixture=local\nplatform.fixture=linux\n")
        with mock.patch.dict(os.environ, {"BUILD_TARGETS_CONFIG": str(config)}):
            self.assertEqual(targets.read_targets(), (targets.LocalTarget("fixture", "linux"),))
            self.assertEqual(targets.read_targets(SHIPPED_CONFIG), (targets.LocalTarget("fixture", "linux"),))

    def test_unknown_kind_names_its_key(self) -> None:
        config = self.write_config("targets=mac\nkind.mac=network\nplatform.mac=macos\n")
        with self.assertRaisesRegex(targets.ConfigError, r"kind\.mac"):
            _ = targets.read_targets(config)

    def test_missing_platform_names_its_key(self) -> None:
        config = self.write_config("targets=natedev\nkind.natedev=local\n")
        with self.assertRaisesRegex(targets.ConfigError, r"platform\.natedev"):
            _ = targets.read_targets(config)

    def test_missing_settings_key_names_its_key(self) -> None:
        config = self.write_config("targets=mac\nkind.mac=ssh\nplatform.mac=macos\n")
        with self.assertRaisesRegex(targets.ConfigError, r"settings\.mac"):
            _ = targets.read_targets(config)

    def test_missing_settings_file_names_its_key(self) -> None:
        config = self.write_config(
            "targets=mac\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=absent.conf\n"
        )
        with self.assertRaisesRegex(targets.ConfigError, r"settings\.mac"):
            _ = targets.read_targets(config)

    def test_relative_settings_resolve_from_config_directory(self) -> None:
        folder = self.root / "configuration"
        folder.mkdir()
        settings = folder / "remote.conf"
        _ = settings.write_text("host=test-mac\n", encoding="utf-8")
        config = folder / "targets.conf"
        _ = config.write_text(
            "targets=mac\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=remote.conf\n",
            encoding="utf-8",
        )
        self.assertEqual(targets.read_targets(config), (targets.SshTarget("mac", "macos", settings),))

    def test_config_syntax_matches_existing_reader(self) -> None:
        config = self.write_config(
            "# targets in preferred order\n"
            + "[machines]\n"
            + "targets = natedev # local machine\n"
            + "kind.natedev = local\nkind.natedev = ssh\n"
            + "platform.natedev = linux\n"
        )
        self.assertEqual(targets.read_targets(config), (targets.LocalTarget("natedev", "linux"),))


class EligibilityTests(IsolatedTargetTest):
    @override
    def setUp(self) -> None:
        super().setUp()
        config = self.write_config(
            "targets=mac,natedev\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\n"
        )
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"BUILD_TARGETS_CONFIG": str(config)})))
        _ = self.enterContext(mock.patch.object(offload, "repository_identity", return_value=offload.KnownRepository("hana")))

    def test_eligibility_table_keeps_linux_and_excludes_each_mac_constraint(self) -> None:
        mac = targets.SshTarget("mac", "macos", self.settings)
        local = targets.LocalTarget("natedev", "linux")
        for settings, routing, expected in (
            ("linux_only.hana=\n", targets.AnyEligibleTarget(), targets.EligibleTarget(mac)),
            ("linux_only.other=\n", targets.AnyEligibleTarget(), targets.ExcludedTarget(mac, "repo")),
            ("linux_only.hana=other,hana\n", targets.AnyEligibleTarget(), targets.ExcludedTarget(mac, "linux_only")),
            ("linux_only.hana=other\n", targets.AnyEligibleTarget(), targets.EligibleTarget(mac)),
            ("", targets.LocalRequired("Linux sockets required"), targets.ExcludedTarget(mac, "Linux sockets required")),
        ):
            with self.subTest(settings=settings, routing=routing):
                _ = self.settings.write_text(settings)
                self.assertEqual(targets.target_eligibility(self.root, "hana", routing),
                                 (expected, targets.EligibleTarget(local)))

    def test_unknown_repository_excludes_mac_without_excluding_linux(self) -> None:
        with mock.patch.object(offload, "repository_identity", return_value=offload.UNKNOWN_REPOSITORY):
            results = targets.target_eligibility(self.root, "hana", targets.AnyEligibleTarget())
        self.assertEqual(results[0], targets.ExcludedTarget(targets.SshTarget("mac", "macos", self.settings), "repo"))
        self.assertEqual(results[1], targets.EligibleTarget(targets.LocalTarget("natedev", "linux")))

    def test_reversed_order_and_alternate_settings_decide_eligibility(self) -> None:
        alternate = self.root / "alternate.conf"
        _ = alternate.write_text("linux_only.hana=hana\n")
        config = self.write_config(
            "targets=natedev,mac\nkind.natedev=local\nplatform.natedev=linux\n"
            + "kind.mac=ssh\nplatform.mac=macos\nsettings.mac=alternate.conf\n"
        )
        with mock.patch.dict(os.environ, {"BUILD_TARGETS_CONFIG": str(config)}):
            results = targets.target_eligibility(self.root, "hana", targets.AnyEligibleTarget())
        self.assertEqual(results, (targets.EligibleTarget(targets.LocalTarget("natedev", "linux")),
                                  targets.ExcludedTarget(targets.SshTarget("mac", "macos", alternate), "linux_only")))

    def test_routing_results_are_frozen_domain_values(self) -> None:
        for value in (targets.AnyEligibleTarget(), targets.LocalRequired("reason"),
                      targets.EligibleTarget(targets.LocalTarget("natedev", "linux")),
                      targets.ExcludedTarget(targets.SshTarget("mac", "macos", self.settings), "repo")):
            self.assertTrue(is_dataclass(value))
            with self.assertRaises(FrozenInstanceError):
                setattr(value, "reason", "changed")


class TargetStateTests(IsolatedTargetTest):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(mock.patch.object(targets, "utc_now", return_value=NOW))

    def mac(self) -> targets.SshTarget:
        return targets.SshTarget("mac", "macos", self.settings)

    def test_local_without_holders_is_free(self) -> None:
        self.assertEqual(targets.target_state(targets.LocalTarget("natedev", "linux")), targets.Free())

    def test_missing_holder_directory_is_free(self) -> None:
        self.holders.rmdir()
        self.assertEqual(targets.target_state(targets.LocalTarget("natedev", "linux")), targets.Free())

    def test_local_eta_is_expected_release(self) -> None:
        self.write_holder(END.isoformat())
        self.assertEqual(targets.target_state(targets.LocalTarget("natedev", "linux")),
                         targets.Held("hana", "render checks", SINCE, targets.ReleaseEstimated(END), targets.HolderSession("hana")))

    def test_local_without_eta_is_held_until_release(self) -> None:
        self.write_holder()
        self.assertEqual(targets.target_state(targets.LocalTarget("natedev", "linux")),
                         targets.Held("hana", "render checks", SINCE, targets.UntilReleased(), targets.HolderSession("hana")))

    def test_past_local_eta_does_not_release_hold(self) -> None:
        eta = NOW - timedelta(minutes=1)
        self.write_holder(eta.isoformat())
        self.assertEqual(targets.target_state(targets.LocalTarget("natedev", "linux")),
                         targets.Held("hana", "render checks", SINCE, targets.ReleaseEstimated(eta), targets.HolderSession("hana")))

    def test_mac_without_block_or_claim_is_free(self) -> None:
        self.assertEqual(targets.target_state(self.mac()), targets.Free())

    def test_mac_active_block_expires_at_recorded_time(self) -> None:
        self.write_block()
        self.assertEqual(targets.target_state(self.mac()),
                         targets.Held("hana", "render checks", SINCE, targets.ExpiresAt(END), targets.HolderSession("hana-session")))

    def claim_mac(self) -> tuple[int, str]:
        args = mac_test.CliArguments()
        args.pid = os.getpid()
        args.what = "nextest hana"
        args.worktree = str(self.root)
        output = io.StringIO()
        with mock.patch.object(mac_test, "process_start_time", return_value="test-run-start"), \
             mock.patch.object(mac_test, "start_claim_watcher") as watcher:
            with redirect_stdout(output):
                code = mac_test.command_claim(args, mac_test.StatePaths(self.mac_state))
        if code == 0:
            watcher.assert_called_once()
            paths = cast(mac_test.StatePaths, watcher.call_args.args[0])
            self.assertEqual(paths.directory, self.mac_state)
        else:
            watcher.assert_not_called()
        return code, output.getvalue()

    def test_retained_expired_block_is_held_and_refuses_claim(self) -> None:
        self.write_block(NOW - timedelta(seconds=1))
        block = self.mac_state / "block.json"
        before = block.read_bytes()
        state = targets.target_state(self.mac())
        self.assertEqual(state, targets.Held("hana", "render checks", SINCE, targets.AwaitingBlockRemoval(), targets.HolderSession("hana-session")))
        self.assertEqual(block.read_bytes(), before)
        self.assertEqual(self.claim_mac(), (10, "blocked by hana: render checks\n"))

    def test_lifted_expired_block_is_free_and_allows_claim(self) -> None:
        self.write_block(NOW - timedelta(seconds=1))
        (self.mac_state / "block.json").unlink()
        self.assertEqual(targets.target_state(self.mac()), targets.Free())
        self.assertEqual(self.claim_mac(), (0, "claimed\n"))

    def test_pending_blocks_refuse_claims_before_and_after_expiry(self) -> None:
        cases: tuple[tuple[datetime, targets.HoldEnd], ...] = (
            (END, targets.ExpiresAt(END)),
            (NOW - timedelta(seconds=1), targets.AwaitingBlockRemoval()),
        )
        for expires, end in cases:
            with self.subTest(expires=expires):
                block = mac_test.PendingMacBlock(
                    "hana", "render checks", SINCE, expires,
                    mac_test.HolderQueue("hana"), mac_test.NO_SHOWRUNNER,
                    "off_by_this_block", mac_test.WAITING_ON_CI_JOB,
                )
                path = self.mac_state / "block.json"
                mac_test.write_block(path, block)
                before = path.read_bytes()
                self.assertEqual(targets.target_state(self.mac()), targets.Held("hana", "render checks", SINCE, end))
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(self.claim_mac(), (10, "blocked by hana: render checks\n"))

    def test_mac_block_expires_at_current_time(self) -> None:
        self.write_block(NOW)
        state = targets.target_state(self.mac())
        self.assertEqual(state, targets.Held("hana", "render checks", SINCE, targets.AwaitingBlockRemoval(), targets.HolderSession("hana-session")))
        self.assertEqual(self.claim_mac(), (10, "blocked by hana: render checks\n"))

    def test_live_claim_is_running(self) -> None:
        self.write_claim()
        with mock.patch("mac_test.process_start_time", return_value="test-run-start"):
            self.assertEqual(
                targets.target_state(self.mac()),
                targets.Running((targets.RunningJob("nextest hana", SINCE, targets.NOT_SEEN_IDLE),)),
            )

    def test_each_live_repository_claim_is_a_running_job_oldest_first(self) -> None:
        later = SINCE + timedelta(minutes=10)
        self.write_claim("claims/cargo-liner.json", "nextest cargo-liner", later)
        liner = self.mac_state / "claims" / "cargo-liner.json"
        record = cast(dict[str, object], json.loads(liner.read_text(encoding="utf-8")))
        record["idle_since"] = END.isoformat()
        _ = liner.write_text(json.dumps(record), encoding="utf-8")
        self.write_claim("claims/hana.json")
        with mock.patch("mac_test.process_start_time", return_value="test-run-start"):
            self.assertEqual(targets.target_state(self.mac()), targets.Running((
                targets.RunningJob("nextest hana", SINCE, targets.NOT_SEEN_IDLE),
                targets.RunningJob("nextest cargo-liner", later, targets.IdleSince(END)),
            )))

    def test_identical_running_descriptions_keep_each_claims_holder_session(self) -> None:
        for name, session_id in (("first", "report-session"), ("second", "inspector-session")):
            self.write_claim(f"claims/{name}.json")
            path = self.mac_state / "claims" / f"{name}.json"
            record = cast(dict[str, object], json.loads(path.read_text()))
            record["holder_session"] = session_id
            _ = path.write_text(json.dumps(record))
        with mock.patch.object(mac_test, "process_start_time", return_value="test-run-start"):
            state = targets.target_state(self.mac())
        self.assertEqual(state, targets.Running((
            targets.RunningJob("nextest hana", SINCE, targets.NOT_SEEN_IDLE, targets.HolderSession("report-session")),
            targets.RunningJob("nextest hana", SINCE, targets.NOT_SEEN_IDLE, targets.HolderSession("inspector-session")),
        )))

    def test_ended_repository_claim_leaves_only_live_jobs(self) -> None:
        record: ClaimRecord = {
            "pid": 12345,
            "proc_start": "ended-run-start",
            "what": "nextest cargo-liner",
            "worktree": str(self.root / "cargo-liner"),
            "since": SINCE.isoformat(),
        }
        (self.mac_state / "claims").mkdir()
        _ = (self.mac_state / "claims" / "cargo-liner.json").write_text(json.dumps(record), encoding="utf-8")
        self.write_claim("claims/hana.json")
        with mock.patch("mac_test.process_start_time", return_value="test-run-start"):
            self.assertEqual(
                targets.target_state(self.mac()),
                targets.Running((targets.RunningJob("nextest hana", SINCE, targets.NOT_SEEN_IDLE),)),
            )

    def test_invalid_repository_claim_is_a_state_read_failure(self) -> None:
        claim = self.mac_state / "claims" / "hana.json"
        claim.parent.mkdir()
        _ = claim.write_text("{}", encoding="utf-8")
        self.assertEqual(targets.target_state(self.mac()), targets.StateUnreadable(f"{claim} has an invalid run record"))

    def test_claim_with_gone_run_is_free(self) -> None:
        self.write_claim()
        with mock.patch("mac_test.process_start_time", return_value=None):
            self.assertEqual(targets.target_state(self.mac()), targets.Free())

    def test_claim_with_reused_pid_is_free(self) -> None:
        self.write_claim()
        with mock.patch("mac_test.process_start_time", return_value="different-run-start"):
            self.assertEqual(targets.target_state(self.mac()), targets.Free())

    def test_active_block_precedes_live_claim(self) -> None:
        self.write_block()
        self.write_claim()
        with mock.patch("mac_test.process_start_time", return_value="test-run-start"):
            self.assertEqual(targets.target_state(self.mac()),
                             targets.Held("hana", "render checks", SINCE, targets.ExpiresAt(END), targets.HolderSession("hana-session")))

    def test_retained_expired_block_precedes_live_claim(self) -> None:
        self.write_block(NOW - timedelta(seconds=1))
        self.write_claim()
        with mock.patch("mac_test.process_start_time", return_value="test-run-start"):
            self.assertIsInstance(targets.target_state(self.mac()), targets.Held)

    def test_state_reads_leave_records_unchanged(self) -> None:
        self.write_block(NOW - timedelta(seconds=1))
        self.write_claim()
        block = self.mac_state / "block.json"
        claim = self.mac_state / "run.json"
        before = (block.read_bytes(), claim.read_bytes())
        with mock.patch("mac_test.process_start_time", return_value=None):
            self.assertIsInstance(targets.target_state(self.mac()), targets.Held)
        self.assertEqual((block.read_bytes(), claim.read_bytes()), before)

    def test_stale_claim_remains_after_free_state_read(self) -> None:
        self.write_claim()
        claim = self.mac_state / "run.json"
        before = claim.read_bytes()
        with mock.patch.object(mac_test, "process_start_time", return_value=None):
            self.assertEqual(targets.target_state(self.mac()), targets.Free())
        self.assertEqual(claim.read_bytes(), before)

    def test_unreadable_holder_is_a_state_read_failure(self) -> None:
        self.write_holder()
        holder = self.holders / "hana"
        original_stat = Path.stat

        def stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            if path == holder:
                raise PermissionError("holder access denied")
            return original_stat(path, follow_symlinks=follow_symlinks)

        with mock.patch.object(Path, "stat", stat):
            self.assertEqual(
                targets.target_state(targets.LocalTarget("natedev", "linux")),
                targets.StateUnreadable("holder access denied"),
            )

    def test_holder_with_unreadable_content_is_held_as_the_build_hold_reads_it(self) -> None:
        holder = self.holders / "hana"
        _ = holder.write_bytes(b"\xff")
        state = targets.target_state(targets.LocalTarget("natedev", "linux"))
        self.assertIsInstance(state, targets.Held)
        if isinstance(state, targets.Held):
            self.assertEqual(
                (state.holder, state.reason, state.end),
                ("hana", "unreadable holder file", targets.UntilReleased()),
            )

    def test_invalid_json_block_is_a_state_read_failure(self) -> None:
        _ = (self.mac_state / "block.json").write_text("{", encoding="utf-8")
        self.assertIsInstance(targets.target_state(self.mac()), targets.StateUnreadable)

    def test_invalid_json_claim_is_a_state_read_failure(self) -> None:
        _ = (self.mac_state / "run.json").write_text("{", encoding="utf-8")
        self.assertIsInstance(targets.target_state(self.mac()), targets.StateUnreadable)

    def test_unreadable_block_is_a_state_read_failure(self) -> None:
        block = self.mac_state / "block.json"
        block.mkdir()
        state = targets.target_state(self.mac())
        self.assertIsInstance(state, targets.StateUnreadable)
        if isinstance(state, targets.StateUnreadable):
            self.assertIn(str(block), state.reason)

    def test_unreadable_claim_is_a_state_read_failure(self) -> None:
        claim = self.mac_state / "run.json"
        claim.mkdir()
        state = targets.target_state(self.mac())
        self.assertIsInstance(state, targets.StateUnreadable)
        if isinstance(state, targets.StateUnreadable):
            self.assertIn(str(claim), state.reason)

    def test_invalid_block_is_a_state_read_failure(self) -> None:
        block = self.mac_state / "block.json"
        _ = block.write_text("{}", encoding="utf-8")
        self.assertEqual(targets.target_state(self.mac()), targets.StateUnreadable(f"{block} has an invalid block record"))

    def test_invalid_claim_is_a_state_read_failure(self) -> None:
        claim = self.mac_state / "run.json"
        _ = claim.write_text("{}", encoding="utf-8")
        self.assertEqual(targets.target_state(self.mac()), targets.StateUnreadable(f"{claim} has an invalid run record"))

    def test_invalid_claim_precedes_blocked_state_like_claim_admission(self) -> None:
        self.write_block()
        claim = self.mac_state / "run.json"
        _ = claim.write_text("{}", encoding="utf-8")
        self.assertEqual(targets.target_state(self.mac()), targets.StateUnreadable(f"{claim} has an invalid run record"))
        self.assertEqual(self.claim_mac(), (12, f"state unreadable: {claim}\n"))

    def test_claim_with_invalid_timestamp_is_a_state_read_failure(self) -> None:
        record: ClaimRecord = {
            "pid": 12345,
            "proc_start": "test-run-start",
            "what": "nextest hana",
            "worktree": str(self.root),
            "since": "invalid timestamp",
        }
        claim = self.mac_state / "run.json"
        _ = claim.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(targets.target_state(self.mac()), targets.StateUnreadable(f"{claim} has an invalid since"))

    def test_unrelated_value_error_is_not_reported_as_state_failure(self) -> None:
        with mock.patch.object(mac_test, "read_claims", side_effect=ValueError("unrelated failure")):
            with self.assertRaisesRegex(ValueError, "unrelated failure"):
                _ = targets.target_state(self.mac())

    def test_target_state_does_not_probe_ssh(self) -> None:
        self.assertEqual(targets.target_state(self.mac()), targets.Free())
        self.write_block()
        self.assertEqual(targets.target_state(self.mac()),
                         targets.Held("hana", "render checks", SINCE, targets.ExpiresAt(END), targets.HolderSession("hana-session")))


class ValueTests(unittest.TestCase):
    def test_domain_values_are_frozen_dataclasses(self) -> None:
        values: tuple[object, ...] = (
            targets.LocalTarget("natedev", "linux"),
            targets.SshTarget("mac", "macos", Path("mac_test.conf")),
            targets.Free(),
            targets.RunningJob("nextest hana", SINCE, targets.IdleSince(END)),
            targets.Running((targets.RunningJob("nextest hana", SINCE, targets.NOT_SEEN_IDLE),)),
            targets.IdleSince(END),
            targets.NOT_SEEN_IDLE,
            targets.Held("hana", "render checks", SINCE, targets.UntilReleased()),
            targets.Unreachable("connection timed out"),
            targets.StateUnreadable("holder access denied"),
            targets.ExpiresAt(END),
            targets.ReleaseEstimated(END),
            targets.UntilReleased(),
            targets.AwaitingBlockRemoval(),
        )
        for value in values:
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(FrozenInstanceError):
                    setattr(value, "extra", "changed")


if __name__ == "__main__":
    _ = unittest.main()
