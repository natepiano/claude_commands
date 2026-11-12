"""Fixed cost evidence and host records for local AWS reports."""
from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from decimal import Decimal
import os
from pathlib import Path
import subprocess
from typing import cast, override
import unittest
from unittest import mock

from scripts.aws_target import ledger
from scripts.aws_target.ledger_test_support import REAL_POPEN, CostFixture, charge_readings, git_only, instant

from scripts.buildlog import index, store
from scripts.lib import conf
from scripts.buildlog.test_index import COORDINATION_VARIABLES, call, encode, step


DAY = "2026-01-15"
MIDNIGHT = instant(DAY + "T00:00:00+00:00")
NOW = MIDNIGHT + 12 * 3600
REPOSITORY = "github.com/example/hana"
COMMAND = ["cargo", "nextest", "run", "-p", "hana"]


def narrow[T](test: unittest.TestCase, value: object, expected: type[T]) -> T:
    test.assertIsInstance(value, expected)
    assert isinstance(value, expected)
    return value


@contextmanager
def read_only_evidence(test: MeasurementFixture, directory: Path, *,
                       allow_private_history: bool = False) -> Generator[None]:
    buildlog_root = store.root()

    def snapshot_files() -> dict[str, tuple[bytes, int]]:
        # Only the index WAL and SHM sidecars can change during a read.
        return {name: evidence for name, evidence in test.snapshot_files(directory).items()
                if not ((directory / name).parent == buildlog_root and any(
                    index.INDEX_PATTERN.fullmatch(Path(name).name.removesuffix(suffix))
                    for suffix in index.INDEX_SIDECAR_SUFFIXES[1:] if name.endswith(suffix)))}

    before = snapshot_files()
    commands = private_history if allow_private_history else AssertionError("readers cannot run commands")
    with mock.patch.object(subprocess, "run", side_effect=commands), \
         mock.patch.object(subprocess, "Popen", side_effect=AssertionError("readers cannot run commands")), \
         mock.patch.object(Path, "write_text", side_effect=AssertionError("readers cannot write files")), \
         mock.patch.object(Path, "write_bytes", side_effect=AssertionError("readers cannot write files")), \
         mock.patch.object(ledger.CostLedger, "guard", side_effect=AssertionError("readers cannot decide spending")), \
         mock.patch.object(ledger.CostLedger, "estimate", side_effect=AssertionError("readers cannot lock costs")):
        yield
    test.assertEqual(snapshot_files(), before)


def private_history(arguments: Sequence[str], **options: object) -> subprocess.CompletedProcess[str]:
    with mock.patch.object(subprocess, "Popen", REAL_POPEN):
        return git_only(arguments, **options)


class MeasurementFixture(CostFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILD_TARGETS_CONFIG": str(self.root / "build_targets.conf"),
            **{name: str(self.root / name) for name in COORDINATION_VARIABLES},
            "BUILDLOG_PEER": "", "USER_ZONE": "UTC", "TZ": "UTC",
        })))
        _ = (self.root / "build_targets.conf").write_text(
            "targets=natedev,mac,aws\nkind.natedev=local\nplatform.natedev=linux\n"
            + "kind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\n"
            + "kind.aws=ssh\nplatform.aws=linux\nsettings.aws=aws_target.conf\n"
            + "sync.hosts.natedev=natedev\nsync.hosts.mac=Mac,mac\nsync.hosts.aws=aws-build\n")

    def book(self) -> ledger.CostLedger:
        return ledger.CostLedger(self.state)

    def charge_fetch(self, usd: Decimal) -> mock.Mock:
        def fetch(start: int, end: int) -> tuple[ledger.ChargeRead, ...]:
            fee = conf.decimal(conf.read(self.settings).text('cost_explorer_read_usd'))
            return charge_readings(start, end, usd, fee)
        return mock.Mock(side_effect=fetch)

    def seed_cost(self, *, hours: int = 2, price: str = "1") -> None:
        book = self.book()
        book.record_inventory(ledger.ResourceInventory(
            MIDNIGHT, (ledger.PricedResource("retained-storage", Decimal("0")),), "fixture inventory"))
        book.observe(ledger.InstanceObservation(MIDNIGHT, "stopped", "fixture AWS state"))
        book.record_price(ledger.PriceSample(NOW - hours * 3600, Decimal(price), "fixture Spot price"))
        book.observe(ledger.InstanceObservation(NOW - hours * 3600, "running", "fixture AWS state"))
        book.observe(ledger.InstanceObservation(NOW, "stopped", "fixture AWS state"))
        _ = book.guard(self.settings, "admission", NOW, lambda _event: True)

    def pending_request(self) -> ledger.ComputeRequest:
        request = ledger.ComputeRequest(NOW - 7200, 'fixture', 'us-east-1', Decimal(10))
        self.book().record_compute_request(request)
        decision = self.book().guard(self.settings, 'admission', NOW, lambda _event: True)
        self.assertIsInstance(decision, ledger.SpendingRefused)
        return request

    def resolve_request(self, request: ledger.ComputeRequest,
                        resolution: ledger.ComputeInstanceConfirmed | ledger.ComputeResourcesAbsent) -> int:
        self.book().resolve_compute_request(request, resolution)
        later = NOW + 3600
        decision = self.book().guard(self.settings, 'admission', later, lambda _event: True)
        self.assertIsInstance(decision, ledger.SpendingRefused)
        return later

    def seed_previous_day(self, *, hours: int = 1) -> None:
        book = self.book()
        start = MIDNIGHT - 86400
        book.record_inventory(ledger.ResourceInventory(
            start, (ledger.PricedResource("volume", Decimal(0)),), "previous day"))
        book.record_price(ledger.PriceSample(start, Decimal(1), "previous day"))
        book.observe(ledger.InstanceObservation(start, "running" if hours else "stopped", "previous day"))
        if hours:
            book.observe(ledger.InstanceObservation(start + hours * 3600, "stopped", "previous day"))
        book.observe(ledger.InstanceObservation(MIDNIGHT, "stopped", "previous day"))

    def write_records(self, *rows: Mapping[str, object]) -> None:
        for row in rows:
            path = self.root / "buildlog" / str(row.get("host", "natedev")) / "2026-01.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab") as handle:
                _ = handle.write(encode(dict(row)))

    def local_step(self, name: str, wait: float, duration: float, **changes: object) -> dict[str, object]:
        row = step(name, **{
            "target": "natedev",
            "caller": "verify", "started_at": store.utc_iso(NOW - 3600),
            "ended_at": store.utc_iso(NOW - 3600 + duration), "duration_s": duration,
            "mem_wait_s": wait, "step": "nextest", "argv": COMMAND,
            "repo_identity": REPOSITORY, "repo_path": "/r/hana", "worktree": "/r/feature",
            "cwd": "/r/feature", "status": 0, "call_id": name,
        })
        row.update(changes)
        return row

    def attempt(self, name: str = "aws-attempt", **changes: object) -> dict[str, object]:
        row: dict[str, object] = {
            "kind": "ssh_attempt", "v": 1, "id": name, "host": "natedev",
            "at": store.utc_iso(NOW), "call_id": "fallback-call", "target": "aws",
            "repo_identity": REPOSITORY, "queued_at": NOW - 300,
            "departed_at": NOW - 200, "slot": 1, "outcome": "passed",
            "completion": "confirmed", "cleanup": "confirmed", "unresolved_count": 0,
            "evidence_at": NOW, "step": "test", "argv": COMMAND, "duration_s": 200,
        }
        row.update(changes)
        return row

    def comparison_rows(self) -> None:
        rows = (
            self.local_step("equal-departure", 100, 9999, argv=["cargo", "test"]),
            self.local_step("comparison-a", 300, 100),
            self.local_step("comparison-b", 500, 200),
            self.local_step("other-repository", 90000, 90000, repo_identity="github.com/example/other"),
            self.local_step("other-kind", 90000, 90000, step="check", argv=["cargo", "check"]),
            self.local_step("linux-alias", 90000, 90000, caller="verify-linux", target="loopback"),
            self.local_step("mac", 90000, 90000, host="Mac", target="mac", caller="verify-mac"),
            self.local_step("previous-day", 90000, 90000, started_at=store.utc_iso(MIDNIGHT - 1)),
        )
        for row in rows:
            self.write_records(row, self.call(str(row["id"]), command=(
                "test hana" if row["argv"] == COMMAND else "test other")))

    def call(self, name: str, **changes: object) -> dict[str, object]:
        row = call(name, **{
            "started_at": store.utc_iso(NOW - 3600), "ended_at": store.utc_iso(NOW),
            "tool": "verify.sh", "cached": False, "verb": "test", "command": "test hana", "package": "hana",
            "target": "natedev", "outcome": "ran", "status": 0, "wait_s": 0,
            "wall_s": 200, "repo_path": "/r/hana", "worktree": "/r/feature",
        })
        row.update(changes)
        return row

    def remote_step(self, **changes: object) -> dict[str, object]:
        return self.local_step("remote-step", 0, 200, **{
            "host": "aws-build", "target": "aws", "caller": "verify-linux",
            "call_id": "fallback-call", **changes,
        })

    def snapshot_files(self, directory: Path) -> dict[str, tuple[bytes, int]]:
        return {str(path.relative_to(directory)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in directory.rglob("*") if path.is_file()}
