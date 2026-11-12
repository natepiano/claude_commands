"""Publish private spending permission for existing Linux runner fixtures."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
import json
from pathlib import Path
import signal
import tempfile

from scripts.aws_target import ledger, remote
from scripts.aws_target.ledger_test_support import install_cost_settings


@dataclass(frozen=True)
class RunnerPermissionRecords:
    admission: str
    generation: str


@lru_cache(maxsize=4)
def runner_permission_records(now: int, boot_id: str) -> RunnerPermissionRecords:
    """Build durable permission once for each fixture class clock."""
    with tempfile.TemporaryDirectory(prefix="runner-permission-") as folder:
        private = Path(folder)
        settings = private / "aws_target.conf"
        install_cost_settings(settings)
        book = ledger.CostLedger(private / "ledger")
        book.record_inventory(ledger.ResourceInventory(now - 1, (
            ledger.PricedResource("retained-storage", Decimal("0")),), "private runner inventory"))
        book.observe(ledger.InstanceObservation(now - 1, "stopped", "private instance evidence"))
        decision = book.guard(settings, "admission", now, lambda _event: True, clock=lambda: float(now))
        if not isinstance(decision, ledger.SpendingAllowed):
            raise AssertionError(f"The private runner permission was refused: {decision}")
        permission = book.permission_for_decision(decision)
        envelope = remote.AdmissionEnvelope(1, "aws", "admission", "fixture-generation", now, now + 86400, permission)
        directory = private / "remote"
        if not remote.publish_admission(directory, json.dumps(remote.envelope_record(envelope)), now=now, boot_id=boot_id):
            raise AssertionError("The private runner permission was not published")
        return RunnerPermissionRecords((directory / "admission.json").read_text(),
                                       (directory / "generation.json").read_text())


def seed_runner_permission(root: Path, settings: Path, now: int, boot_id: str) -> Path:
    records = runner_permission_records(now, boot_id)
    directory = root / "runner-permission/remote"
    directory.mkdir(parents=True, exist_ok=True)
    _ = (directory / "admission.json").write_text(records.admission)
    _ = (directory / "generation.json").write_text(records.generation)
    lines = [line for line in settings.read_text().splitlines()
             if line.partition("=")[0].strip() != "remote_state_directory"]
    _ = settings.write_text("\n".join([*lines, f"remote_state_directory={directory}", ""]))
    return directory


def interrupt_runner(directory: Path, at: int) -> None:
    """Write interruption evidence and send SIGTERM twice."""
    remote.write_interruption(directory, target="aws", generation="fixture-generation", at=at)
    signal.raise_signal(signal.SIGTERM)
    signal.raise_signal(signal.SIGTERM)
