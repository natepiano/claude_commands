"""Admission socket shapes and encoding without queue records at startup."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, cast

from scripts.build_queue import snapshot_codec as codec

if TYPE_CHECKING:
    from scripts.build_queue import admission_records as records
    from scripts.build_queue import placement
else:
    class _DeferredAdmissionRecords:
        """Load domain types when an annotation or a default needs them."""

        def __getattr__(self, name: str) -> object:
            from scripts.build_queue import admission_records
            return cast(object, getattr(admission_records, name))

    records = _DeferredAdmissionRecords()

    class _DeferredPlacement:
        """Load placement types only for version 3 messages."""

        def __getattr__(self, name: str) -> object:
            from scripts.build_queue import placement
            return cast(object, getattr(placement, name))

    placement = _DeferredPlacement()

VERSION = 3


def _no_test_width() -> records.NoTestWidth:
    return records.NoTestWidth()


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


@dataclass(frozen=True, kw_only=True)
class CallerCode:
    code_version: str = ""


NO_RUN = NoRun()


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
    profile: records.StepProfile | records.ProgramProfile
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
    test_width: records.TestWidth = field(default_factory=_no_test_width)


@dataclass(frozen=True)
class LaunchRequest(CallerCode):
    episode: str
    owner: Owner
    since: float
    identity: Identity
    profile: records.StepProfile | records.ProgramProfile


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
    unit: str = ""


@dataclass(frozen=True)
class TokenWaitRequest(CallerCode):
    request_id: str
    reservation: str


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
class PlacementRequest(CallerCode):
    episode: str
    owner: placement.SubmittingOwner
    since: float
    profile: records.StepProfile | records.ProgramProfile
    constraints: placement.PlacementConstraints


@dataclass(frozen=True)
class ClaimFailure(CallerCode):
    request_id: str
    assignment: placement.PlacementAssignment
    reason: str


@dataclass(frozen=True)
class ConsumePermission(CallerCode):
    request_id: str
    assignment: placement.PlacementAssignment
    claim: placement.ProviderClaimReference


@dataclass(frozen=True)
class ExecutionReport(CallerCode):
    request_id: str
    assignment: placement.PlacementAssignment
    claim: placement.ProviderClaimReference
    evidence: placement.ExecutionEvidence
    outcome: str = ""


@dataclass(frozen=True)
class PlacementWait:
    episode: str
    since: float
    position: int
    eligibility: tuple[placement.CandidateEligibility, ...]


@dataclass(frozen=True)
class Assignment:
    assignment: placement.PlacementAssignment


@dataclass(frozen=True)
class FirstAuthorization:
    assignment: placement.PlacementAssignment
    claim: placement.ProviderClaimReference


@dataclass(frozen=True)
class DispatchStatus:
    assignment: placement.PlacementAssignment | placement.UnknownAssignment
    state: placement.AssignmentState
    permission: placement.LaunchPermission | placement.NoLaunchPermission
    reason: str = ""


Request = (ServiceEstimateRequest | AdmitRequest | LaunchRequest | ReleaseRequest | WidthRequest | ViewRequest
           | BuildHistoryRequest | ExplainRequest | MineRequest | AdmitHeadRequest | PrioritySetRequest | PriorityClearRequest | TokenWaitRequest
           | PlacementRequest | ClaimFailure | ConsumePermission | ExecutionReport)

Reply = (Wait | RunnerAdmitted | Admitted | LaunchAdmitted | Refused | Ok | BuildHistoryReply
         | PlacementWait | Assignment | FirstAuthorization | DispatchStatus)

_REQUESTS: dict[str, type] = {
    "service_estimate": ServiceEstimateRequest, "admit": AdmitRequest, "launch": LaunchRequest, "release": ReleaseRequest,
    "width": WidthRequest, "view": ViewRequest, "explain": ExplainRequest,
    "build_history": BuildHistoryRequest, "mine": MineRequest, "admit_head": AdmitHeadRequest,
    "priority_set": PrioritySetRequest, "priority_clear": PriorityClearRequest, "token_wait": TokenWaitRequest,
    "placement": PlacementRequest, "claim_failure": ClaimFailure,
    "consume_permission": ConsumePermission, "execution_report": ExecutionReport,
}
REPLY_TYPES: dict[str, type] = {
    "wait": Wait, "runner_admitted": RunnerAdmitted, "admitted": Admitted, "launch_admitted": LaunchAdmitted,
    "build_history": BuildHistoryReply, "refused": Refused, "ok": Ok,
    "placement_wait": PlacementWait, "assignment": Assignment,
    "first_authorization": FirstAuthorization, "dispatch_status": DispatchStatus,
}
_PLACEMENT_TYPES = (PlacementRequest, ClaimFailure, ConsumePermission, ExecutionReport,
                    PlacementWait, Assignment, FirstAuthorization, DispatchStatus)
T = TypeVar("T")


def admission_socket_path() -> Path:
    from scripts.build_hold.override_guard import refuse_overrides
    refuse_overrides(Path(__file__).with_name("admission_protocol.py"), "build_queue/admission_protocol.py")
    return Path(os.environ.get("ADMISSION_SOCKET") or "/run/build-admission/build-admission.sock")


def encode_record(value: object) -> str:
    """State and message payloads have exactly the domain codec's representation."""
    return json.dumps(codec.snapshot_value(value), separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _read_wire_record(record: dict[str, object]) -> dict[str, object]:
    # Phase 33: older clients sent ShapeP90 with a fixed ten-percent share.
    if record.get("kind") == "ShapeP90":
        return {**record, "kind": "ShapeRankedPeak", "share_percent": 10}
    priority = record.get("priority")
    if isinstance(priority, dict):
        record["priority"] = records.upgrade_queue_priority(cast(dict[str, object], priority))
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
    if getattr(schema, "__name__", "") == "QueuePriority" and isinstance(value, dict):
        value = records.upgrade_queue_priority(cast(dict[str, object], value))
    return _parse_value(value, schema)


def _parse_value(value: object, schema: type[T]) -> T:
    return cast(T, codec.parse_snapshot_value(value, schema))


def encode_envelope(value: object, variants: dict[str, type], version: int = VERSION) -> str:
    name = next((name for name, schema in variants.items() if type(value) is schema), None)
    if name is None:
        raise ValueError("unknown admission message")
    if version < 3 and type(value) in _PLACEMENT_TYPES:
        raise ValueError("placement requires version 3")
    data = _write_wire_record(codec.snapshot_value(value))
    return encode_record({"v": version, "type": name, "data": data}) + "\n"


def parse_envelope(line: str | bytes, variants: dict[str, type]) -> tuple[int, object]:
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
        if version < 3 and variants[name] in _PLACEMENT_TYPES:
            raise ValueError("placement requires version 3")
        return version, cast(object, _parse_value(data, variants[name]))
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
    return encode_envelope(request, _REQUESTS)


def parse_request(line: str | bytes) -> Request:
    return parse_request_envelope(line)[1]


def parse_request_envelope(line: str | bytes) -> tuple[int, Request]:
    version, value = parse_envelope(line, _REQUESTS)
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


def parse_reply(line: str | bytes, request: Request | None = None) -> Reply:
    reply = cast(Reply, parse_envelope(line, REPLY_TYPES)[1])
    if (isinstance(request, LaunchRequest) and isinstance(reply, Admitted)
            or isinstance(request, (AdmitRequest, ServiceEstimateRequest)) and isinstance(reply, LaunchAdmitted)):
        raise ValueError("admission outcome does not match request")
    return reply


def encode_reply(reply: Reply, *, version: int = VERSION) -> str:
    return encode_envelope(reply, REPLY_TYPES, version)


# JSON escapes one character into at most six ASCII bytes; include the newline.
CLEANUP_REQUEST_MAX_BYTES = max(len(encode_request(ReleaseRequest(
    "\0" * CLEANUP_STRING_MAX, "\0" * CLEANUP_STRING_MAX, status,
    code_version="\0" * CLEANUP_STRING_MAX)).encode())
    for status in (CLEANUP_STATUS_MIN, CLEANUP_STATUS_MAX))
