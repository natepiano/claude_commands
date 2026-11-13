"""Old schemas for the refusal and ShapeP90 compatibility tests."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Literal, cast

from scripts.build_queue import admission_records as records
from scripts.build_queue import admission_wire as wire
from scripts.build_queue import snapshot_codec as codec


@dataclass(frozen=True)
class Refused:
    reason: str


@dataclass(frozen=True)
class ShapeP90:
    runs: int
    floor: records.FailedPeakFloor | records.NoFloor = records.NoFloor()
    factor: records.HeadroomRule = records.NoHeadroom()
    cap: records.CappedAtBudget | records.UnderBudget = records.UnderBudget()


@dataclass(frozen=True)
class ExpectedPeak:
    bytes: int
    source: Literal["shape"]
    count: int
    basis: ShapeP90


@dataclass(frozen=True)
class StepProfile:
    repo: str
    step: str
    worktree: str
    peak: ExpectedPeak
    repository: records.Repository = records.UnknownRepository()
    scope: records.TestScope = "full"
    estimated_at: float = 0.0
    build_history_generation: records.BuildHistoryGeneration | records.NoBuildHistoryGeneration = records.NoBuildHistoryGeneration()


@dataclass(frozen=True)
class AdmitRequest:
    episode: str
    owner: wire.Owner
    argv: tuple[str, ...]
    cwd: str
    since: float
    sidecar: str
    identity: wire.Identity
    call: records.Call
    test_width: records.TestWidth
    profile: StepProfile
    fresh_test: bool
    token_back_at: float = 0.0
    run: wire.RunIdentity | wire.NoRun = wire.NoRun()
    place: wire.LocalPlace | wire.CloudPlace = wire.LocalPlace()
    code_version: str = field(default="", kw_only=True)


def _parse(line: str, message_type: str, version_limit: int, schema: type) -> object:
    try:
        value = cast(object, json.loads(line))
        if not isinstance(value, dict):
            raise ValueError("invalid admission envelope")
        envelope = cast(dict[str, object], value)
        version = envelope.get("v")
        if type(version) is not int or not 1 <= version <= version_limit:
            raise ValueError(f"client version {version}, service supports 1..{version_limit}")
        if envelope.get("type") != message_type or set(envelope) != {"v", "type", "data"}:
            raise ValueError("invalid admission envelope")
        return codec.parse_snapshot_value(envelope["data"], schema)
    except (ArithmeticError, KeyError, TypeError, UnicodeError) as error:
        raise ValueError("invalid admission message") from error


def parse_reply(line: str) -> Refused:
    """Decode only the refusal reply from protocol version one."""
    return cast(Refused, _parse(line, "refused", 1, Refused))


def parse_request(line: str) -> AdmitRequest:
    """Decode the ShapeP90 admission request from protocol versions one and two."""
    return cast(AdmitRequest, _parse(line, "admit", 2, AdmitRequest))
