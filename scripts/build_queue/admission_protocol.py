"""Versioned admission envelopes; domain values use the shared snapshot codec."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import TypeVar, cast

from scripts.build_queue import snapshot_codec as codec
from scripts.build_queue.admission_code import (
    ADMISSION_CODE_PATHS as ADMISSION_CODE_PATHS,
    admission_code_version as admission_code_version,
)
from scripts.build_queue import admission_records as records
from scripts.build_queue import cloud_progress, progress_profile

VERSION = 2


class AdmissionUnavailable(RuntimeError):
    """A bounded queue query has no answer."""

    def __init__(self, reason: str) -> None:
        self.reason: str = reason
        super().__init__(reason)


class UnsupportedVersion(ValueError):
    def __init__(self, version: object) -> None:
        super().__init__(f"client version {version}, service supports 1..{VERSION}")


@dataclass(frozen=True)
class Owner:
    pid: int
    start_time: str


@dataclass(frozen=True)
class RunIdentity:
    pid: int
    start_time: str


@dataclass(frozen=True)
class NoRun:
    pass


NO_RUN = NoRun()


@dataclass(frozen=True)
class LocalPlace:
    pass


@dataclass(frozen=True)
class CloudPlace:
    target: str


@dataclass(frozen=True)
class RunPlace:
    run: RunIdentity
    since: float
    first_owner: Owner
    unit: str = ""


@dataclass(frozen=True)
class Identity:
    session_id: str
    name: str
    step: str
    folder: str
    unit: str = ""


Profile = records.StepProfile | records.ProgramProfile


@dataclass(frozen=True, kw_only=True)
class CallerCode:
    code_version: str = ""


@dataclass(frozen=True)
class AdmitRequest(CallerCode):
    episode: str
    owner: Owner
    argv: tuple[str, ...]
    cwd: str
    since: float
    sidecar: str
    identity: Identity
    call: records.Call
    test_width: records.TestWidth
    profile: Profile
    fresh_test: bool
    token_back_at: float = 0.0
    run: RunIdentity | NoRun = NO_RUN
    place: LocalPlace | CloudPlace = LocalPlace()


@dataclass(frozen=True)
class ServiceEstimateRequest(CallerCode):
    """Ask the service to estimate a cargo call from its own history."""
    episode: str
    owner: Owner
    argv: tuple[str, ...]
    cwd: str
    since: float
    sidecar: str
    repository: records.Repository
    worktree: str
    runner: str
    job: str
    test_width: records.TestWidth = records.NoTestWidth()


@dataclass(frozen=True)
class LaunchRequest(CallerCode):
    episode: str
    owner: Owner
    since: float
    identity: Identity
    profile: Profile


@dataclass(frozen=True)
class ReleaseRequest(CallerCode):
    request_id: str
    reservation: str
    status: int


@dataclass(frozen=True)
class WidthRequest(CallerCode):
    request_id: str
    reservation: str
    width: int


@dataclass(frozen=True)
class BuildHistoryRequest(CallerCode):
    pass


@dataclass(frozen=True)
class ViewRequest(CallerCode):
    pass


@dataclass(frozen=True)
class ExplainRequest(CallerCode):
    pass


@dataclass(frozen=True)
class MineRequest(CallerCode):
    session_id: str


@dataclass(frozen=True)
class AdmitHeadRequest(CallerCode):
    request_id: str
    why: str
    actor: str


@dataclass(frozen=True)
class PrioritySetRequest(CallerCode):
    request_id: str
    unit: str
    why: str
    actor: str


@dataclass(frozen=True)
class PriorityClearRequest(CallerCode):
    request_id: str
    actor: str


@dataclass(frozen=True)
class TokenWaitRequest(CallerCode):
    request_id: str
    reservation: str


Request = (ServiceEstimateRequest | AdmitRequest | LaunchRequest | ReleaseRequest | WidthRequest | ViewRequest
           | BuildHistoryRequest | ExplainRequest | MineRequest | AdmitHeadRequest | PrioritySetRequest | PriorityClearRequest | TokenWaitRequest)


@dataclass(frozen=True)
class CloudSlotWait:
    used: int
    slots: int

    @property
    def text(self) -> str:
        return f"waits for a cloud slot: {self.used} of {self.slots} in use"


@dataclass(frozen=True)
class Wait:
    reason: records.WaitReason | CloudSlotWait
    position: int
    head: str
    text: str
    snapshot: records.AdmissionWaitSnapshot


@dataclass(frozen=True)
class AcquisitionTiming:
    """Bound acquisition without reading the client account's configuration."""
    silence_seconds: float
    reconnect_seconds: float
    unreachable_limit_seconds: float
    wait_reply_seconds: float = 60.0


PROTOCOL_ACQUISITION_TIMING = AcquisitionTiming(120.0, 2.0, 300.0)


@dataclass(frozen=True)
class CleanupTiming:
    silence_seconds: float
    reconnect_seconds: float


# These protocol intervals permit cleanup before any configuration is usable.
PROTOCOL_CLEANUP_TIMING = CleanupTiming(120.0, 2.0)
CLEANUP_STRING_MAX = 4096
CLEANUP_STATUS_MIN = -(2 ** 63)
CLEANUP_STATUS_MAX = 2 ** 63 - 1


@dataclass(frozen=True)
class Admitted:
    reservation: str
    need: int
    source: str
    max_path: str
    wave_path: str
    sampling_context: dict[str, object]
    cleanup_timing: CleanupTiming = PROTOCOL_CLEANUP_TIMING


@dataclass(frozen=True)
class RunnerExecutionTiming:
    program_sample_seconds: float
    program_stop_seconds: float
    peak_wave_seconds: float


@dataclass(frozen=True)
class RunnerAdmitted(Admitted):
    execution: RunnerExecutionTiming = field(kw_only=True)


@dataclass(frozen=True)
class LaunchAdmitted:
    need: int
    source: str


@dataclass(frozen=True)
class Refused:
    reason: str


@dataclass(frozen=True)
class Ok:
    pass


@dataclass(frozen=True)
class BuildHistoryReply:
    generation: int


@dataclass(frozen=True)
class CloudRow:
    owner: Owner
    identity: Identity
    place: CloudPlace
    since: float
    started_at: float
    progress: cloud_progress.CloudProgress | cloud_progress.NoCloudProgress
    counter: progress_profile.ProgressCounter | progress_profile.NoCounter
    wait: CloudSlotWait | records.ReadyForAdmission


@dataclass(frozen=True)
class CloudQueueRead(records.QueueRead):
    cloud: tuple[CloudRow, ...] = ()
    cloud_slots: int = 0
    cloud_gate_stale_pushes: int = field(kw_only=True)


@dataclass(frozen=True)
class ViewReply:
    reading: records.QueueRead | CloudQueueRead


@dataclass(frozen=True)
class ExplainReply:
    explanation: records.QueueExplanation
    diagnostics: tuple[records.ConfigurationDiagnostic, ...] = ()


@dataclass(frozen=True)
class MineReply:
    reading: records.QueueRead | CloudQueueRead


Reply = Wait | RunnerAdmitted | Admitted | LaunchAdmitted | Refused | Ok | BuildHistoryReply | ViewReply | ExplainReply | MineReply

_REQUESTS: dict[str, type] = {
    "service_estimate": ServiceEstimateRequest, "admit": AdmitRequest, "launch": LaunchRequest, "release": ReleaseRequest,
    "width": WidthRequest, "view": ViewRequest, "explain": ExplainRequest,
    "build_history": BuildHistoryRequest, "mine": MineRequest, "admit_head": AdmitHeadRequest,
    "priority_set": PrioritySetRequest, "priority_clear": PriorityClearRequest, "token_wait": TokenWaitRequest,
}
_REPLIES: dict[str, type] = {
    "wait": Wait, "runner_admitted": RunnerAdmitted, "admitted": Admitted, "launch_admitted": LaunchAdmitted,
    "build_history": BuildHistoryReply, "refused": Refused, "ok": Ok, "view": ViewReply, "explain": ExplainReply, "mine": MineReply,
}
T = TypeVar("T")


def admission_socket_path() -> Path:
    from scripts.build_hold.override_guard import refuse_overrides
    refuse_overrides(Path(__file__), "build_queue/admission_protocol.py")
    return Path(os.environ.get("ADMISSION_SOCKET") or "/run/build-admission/build-admission.sock")


def encode_record(value: object) -> str:
    """State and message payloads have exactly the domain codec's representation."""
    return json.dumps(codec.snapshot_value(value), separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _read_wire_record(record: dict[str, object]) -> dict[str, object]:
    # Phase 33: older clients sent ShapeP90 with a fixed ten-percent share.
    if record.get("kind") == "ShapeP90":
        return {**record, "kind": "ShapeRankedPeak", "share_percent": 10}
    return record


def _write_wire_record(value: object) -> object:
    if isinstance(value, list):
        return [_write_wire_record(item) for item in cast(list[object], value)]
    if not isinstance(value, dict):
        return value
    record = {key: _write_wire_record(item) for key, item in cast(dict[str, object], value).items()}
    # Phase 33: a config change introduces other shares after deployment of the new service.
    if record.get("kind") == "ShapeRankedPeak" and record.get("share_percent") == 10:
        record["kind"] = "ShapeP90"
        del record["share_percent"]
    return record


def parse_record(text: str, schema: type[T]) -> T:
    value = cast(object, json.loads(text, object_hook=_read_wire_record))
    return cast(T, codec.parse_snapshot_value(value, schema))


def _encode(value: object, variants: dict[str, type], version: int = VERSION) -> str:
    name = next((name for name, schema in variants.items() if type(value) is schema), None)
    if name is None:
        raise ValueError("unknown admission message")
    data = _write_wire_record(codec.snapshot_value(value))
    return encode_record({"v": version, "type": name, "data": data}) + "\n"


def _parse(line: str | bytes, variants: dict[str, type]) -> tuple[int, object]:
    try:
        value = cast(object, json.loads(line, object_hook=_read_wire_record))
        if not isinstance(value, dict):
            raise ValueError("invalid admission envelope")
        envelope = cast(dict[str, object], value)
        version = envelope.get("v")
        if type(version) is not int or not 1 <= version <= VERSION:
            raise UnsupportedVersion(version)
        name = envelope.get("type")
        if not isinstance(name, str) or name not in variants or set(envelope) != {"v", "type", "data"}:
            raise ValueError("invalid admission envelope")
        data = envelope["data"]
        return version, codec.parse_snapshot_value(data, variants[name])
    except (ArithmeticError, KeyError, TypeError, UnicodeError) as error:
        raise ValueError("invalid admission message") from error


def _validate_cleanup(request: Request) -> None:
    if not isinstance(request, (ReleaseRequest, TokenWaitRequest)):
        return
    fields = (request.request_id, request.reservation, request.code_version)
    if any(len(value) > CLEANUP_STRING_MAX for value in fields):
        raise ValueError(f"cleanup string exceeds {CLEANUP_STRING_MAX} characters")
    if isinstance(request, ReleaseRequest) and not CLEANUP_STATUS_MIN <= request.status <= CLEANUP_STATUS_MAX:
        raise ValueError("cleanup status exceeds signed 64-bit range")


def encode_request(request: Request) -> str:
    _validate_cleanup(request)
    return _encode(request, _REQUESTS)


def parse_request(line: str | bytes) -> Request:
    return parse_request_envelope(line)[1]


def parse_request_envelope(line: str | bytes) -> tuple[int, Request]:
    version, value = _parse(line, _REQUESTS)
    request = cast(Request, value)
    _validate_cleanup(request)
    return version, request


def noncleanup_request_started(data: bytes) -> bool:
    """Identify a noncleanup header before its request body is complete."""
    source = data.decode("utf-8", errors="replace").lstrip()
    if not source.startswith("{"):
        return False
    decoder = json.JSONDecoder()
    rest = source[1:].lstrip()
    try:
        while rest:
            key, end = cast(tuple[object, int], decoder.raw_decode(rest))
            rest = rest[end:].lstrip()
            if not rest.startswith(":"):
                return False
            value, end = cast(tuple[object, int], decoder.raw_decode(rest[1:].lstrip()))
            if key == "type":
                return isinstance(value, str) and _REQUESTS.get(value) not in (ReleaseRequest, TokenWaitRequest)
            rest = rest[1:].lstrip()[end:].lstrip()
            if not rest.startswith(","):
                return False
            rest = rest[1:].lstrip()
    except ValueError:
        pass
    return False


def encode_reply(reply: Reply, *, version: int = VERSION) -> str:
    if version == 1 and isinstance(reply, (ViewReply, MineReply)) and isinstance(reply.reading, CloudQueueRead):
        reading = reply.reading
        local = records.QueueRead(reading.view, reading.explanation, reading.ordered, reading.diagnostics)
        reply = type(reply)(local)
    return _encode(reply, _REPLIES, version)


def parse_reply(line: str | bytes, request: Request | None = None) -> Reply:
    reply = cast(Reply, _parse(line, _REPLIES)[1])
    if (isinstance(request, LaunchRequest) and isinstance(reply, Admitted)
            or isinstance(request, (AdmitRequest, ServiceEstimateRequest)) and isinstance(reply, LaunchAdmitted)):
        raise ValueError("admission outcome does not match request")
    return reply


# JSON escapes one character into at most six ASCII bytes; include the newline.
CLEANUP_REQUEST_MAX_BYTES = max(len(encode_request(ReleaseRequest(
    "\0" * CLEANUP_STRING_MAX, "\0" * CLEANUP_STRING_MAX, status,
    code_version="\0" * CLEANUP_STRING_MAX)).encode())
    for status in (CLEANUP_STATUS_MIN, CLEANUP_STATUS_MAX))
