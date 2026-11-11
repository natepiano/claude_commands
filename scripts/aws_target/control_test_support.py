"""Private clocks, queue snapshots, and AWS replies for lifecycle tests."""
from __future__ import annotations

from scripts.build_queue import admission_records

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
from typing import cast, override
from unittest import mock

from scripts.aws_target import ledger, remote
from scripts.build_queue import queue_snapshot
from scripts.mac_test import mac_test
from scripts.aws_target.ledger_test_support import CostFixture, account_reply, instant
from scripts.lint import memory_admit
from scripts.lint.memory_test_support import admission_policy_fixture, fake_memory_readings


@dataclass
class TestClock:
    now: int = instant("2026-01-15T12:00:00+00:00")

    def __call__(self) -> float:
        return float(self.now)

    def advance(self, seconds: int) -> None:
        self.now += seconds


@dataclass
class AwsReplies:
    """Return fixed AWS replies and record every requested command."""

    state: str = "stopped"
    generation: str = "2026-01-15T11:00:00+00:00"
    capacity_miss: bool = False
    unavailable: bool = False
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
        words = tuple(arguments)
        self.calls.append(words)
        if not words or words[0] != "aws":
            raise AssertionError(f"Unexpected command: {words}")
        if self.unavailable:
            return subprocess.CompletedProcess(words, 255, "", "API unavailable")
        if "describe-instances" in words:
            document: dict[str, object] = {"Reservations": [{"Instances": [{
                "InstanceId": "i-fixture", "State": {"Name": self.state},
                "LaunchTime": self.generation,
            }]}]}
        elif "start-instances" in words:
            if self.capacity_miss:
                return subprocess.CompletedProcess(words, 254, "", "InsufficientInstanceCapacity")
            self.state = "pending"
            document = {"StartingInstances": [{"InstanceId": "i-fixture",
                "CurrentState": {"Name": "pending"}, "PreviousState": {"Name": "stopped"}}]}
        elif "stop-instances" in words:
            self.state = "stopping"
            document = {"StoppingInstances": [{"InstanceId": "i-fixture",
                "CurrentState": {"Name": "stopping"}, "PreviousState": {"Name": "running"}}]}
        elif "describe-spot-price-history" in words:
            document = {"SpotPriceHistory": [{"Timestamp": self.generation, "SpotPrice": "0.25"}]}
        elif "get-cost-and-usage" in words:
            document = account_reply(arguments)
        else:
            raise AssertionError(f"Unexpected AWS operation: {words}")
        return subprocess.CompletedProcess(words, 0, json.dumps(document), "")

    def count(self, operation: str) -> int:
        return sum(operation in words for words in self.calls)


class ControlFixture(CostFixture):
    clock: TestClock
    aws: AwsReplies
    queue_directory: Path = Path()

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.clock = TestClock()
        self.aws = AwsReplies()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.queue_directory = self.root / "buildlog/admission"
        self.queue_directory.mkdir(parents=True)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            **fake_memory_readings(self.root), "BUILDLOG_DIR": str(self.root / "buildlog"),
        })))
        _ = (self.root / "build_targets.conf").write_text(
            "targets=aws,natedev,spare\nkind.aws=ssh\nplatform.aws=linux\nsettings.aws=aws_target.conf\n" +
            "kind.natedev=local\nplatform.natedev=linux\n" +
            "kind.spare=ssh\nplatform.spare=linux\nsettings.spare=spare.conf\n")
        _ = (self.root / "spare.conf").write_text(self.settings.read_text())
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(self.clock.now - 1,
            (ledger.PricedResource("retained-volume", Decimal("1")),), "private inventory"))
        book.observe(ledger.InstanceObservation(self.clock.now - 1, "stopped", "fake DescribeInstances"))

    def claim(self, paths: mac_test.StatePaths, *, slot: int = 1, at: int = 0,
              pid: int = 2147483000, generation: str = "generation-one") -> Path:
        scope = mac_test.RepositoryScope("hana", "https://example.invalid/team/hana", slot)
        path = mac_test.claim_path(paths, scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        record: mac_test.RemoteJobClaim = {
            "claim_id": f"claim-{slot}-{pid}-{at}", "episode": f"episode-{slot}",
            "attempt": f"attempt-{pid}-{at}", "submitting_session": "",
            "pid": pid, "proc_start": "old-process", "what": "test hana", "worktree": str(self.root),
            "since": datetime.fromtimestamp(at or self.clock.now - 10, UTC).isoformat(),
            "repo_name": "hana", "repo_key": scope.key, "slot": slot,
            "remote_host": "aws-build", "cleanup_pending": True,
            "instance_generation": generation,
        }
        _ = path.write_text(json.dumps(record))
        return path

    def admission_permission(self) -> ledger.AdmissionPermission:
        book = ledger.CostLedger(self.state)
        decision = book.guard(self.settings, "admission", self.clock.now, lambda _event: True)
        self.assertIsInstance(decision, ledger.SpendingAllowed)
        return book.permission_for_decision(decision)

    def publish_permission(self, directory: Path, permission: ledger.AdmissionPermission, *,
                           generation: str = "generation-one", lifetime: int = 60) -> None:
        envelope = remote.AdmissionEnvelope(1, "aws", "admission", generation, self.clock.now,
                                           self.clock.now + lifetime, permission)
        self.assertTrue(remote.publish_admission(directory, json.dumps(remote.envelope_record(envelope)),
                                                now=self.clock.now, boot_id="fixture-boot"))

    def waiter(self, pid: int, *, step: str = "test", age: int = 121,
               identity: str = "https://example.invalid/team/hana") -> admission_records.Waiter:
        return {"pid": pid, "start_time": str(pid), "need": 1024,
                "since": self.clock.now - age, "step": step,
                "repo_identity": identity, "repository_name": "hana", "folder": str(self.root)}

    def snapshot(self, *rows: admission_records.Waiter, age: int = 0) -> None:
        inputs = admission_records.QueueInputs(
            [(self.queue_directory / f"{row['pid']}.waiting", memory_admit.rank_waiter(row, admission_records.NormalOrder()))
             for row in rows], (), (),
            admission_records.HostMemory(1024**3, 1024**3), admission_records.NoPressureHold(),
            admission_records.ReadyToStart(), admission_records.NoHeadAdmissionOverride(), float(self.clock.now - age))
        queue_snapshot.write_queue_snapshot(self.queue_directory, inputs, admission_policy_fixture())
        self.assertIsInstance(queue_snapshot.read_queue_snapshot(self.queue_directory), queue_snapshot.QueueSnapshot)
