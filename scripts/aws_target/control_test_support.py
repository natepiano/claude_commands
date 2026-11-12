"""Private clocks, queue snapshots, and AWS replies for lifecycle tests."""
from __future__ import annotations


from collections.abc import Sequence
from dataclasses import dataclass, field
import json
import subprocess

from scripts.aws_target.ledger_test_support import account_reply, instant


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
        elif "describe-spot-price-history" in words:
            document = {"SpotPriceHistory": [{"Timestamp": self.generation, "SpotPrice": "0.25"}]}
        elif "get-cost-and-usage" in words:
            document = account_reply(arguments)
        else:
            raise AssertionError(f"Unexpected AWS operation: {words}")
        return subprocess.CompletedProcess(words, 0, json.dumps(document), "")

    def count(self, operation: str) -> int:
        return sum(operation in words for words in self.calls)
