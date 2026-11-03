"""Versioned admission envelopes; domain values use memory_admit's one codec."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import TypeVar, cast

from scripts.lint import memory_admit as gate

VERSION = 1


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
class RunPlace:
    run: RunIdentity
    since: float
    first_owner: Owner


@dataclass(frozen=True)
class Identity:
    session_id: str
    name: str
    step: str
    folder: str


Profile = gate.StepProfile | gate.ProgramProfile


@dataclass(frozen=True)
class AdmitRequest:
    episode: str
    owner: Owner
    argv: tuple[str, ...]
    cwd: str
    since: float
    sidecar: str
    identity: Identity
    call: gate.Call
    test_width: gate.TestWidth
    profile: Profile
    fresh_test: bool
    token_back_at: float = 0.0
    run: RunIdentity | NoRun = NO_RUN


@dataclass(frozen=True)
class LaunchRequest:
    episode: str
    owner: Owner
    since: float
    identity: Identity
    profile: Profile


@dataclass(frozen=True)
class ReleaseRequest:
    request_id: str
    reservation: str
    status: int


@dataclass(frozen=True)
class WidthRequest:
    request_id: str
    reservation: str
    width: int


@dataclass(frozen=True)
class BuildHistoryRequest:
    pass


@dataclass(frozen=True)
class ViewRequest:
    pass


@dataclass(frozen=True)
class ExplainRequest:
    pass


@dataclass(frozen=True)
class MineRequest:
    session_id: str


@dataclass(frozen=True)
class AdmitHeadRequest:
    request_id: str
    why: str
    actor: str


@dataclass(frozen=True)
class PrioritySetRequest:
    request_id: str
    unit: str
    why: str
    actor: str


@dataclass(frozen=True)
class PriorityClearRequest:
    request_id: str
    actor: str


@dataclass(frozen=True)
class TokenWaitRequest:
    request_id: str
    reservation: str


Request = (AdmitRequest | LaunchRequest | ReleaseRequest | WidthRequest | ViewRequest
           | BuildHistoryRequest | ExplainRequest | MineRequest | AdmitHeadRequest | PrioritySetRequest | PriorityClearRequest | TokenWaitRequest)


@dataclass(frozen=True)
class Wait:
    reason: gate.WaitReason
    position: int
    head: str
    text: str
    snapshot: gate.AdmissionWaitSnapshot


@dataclass(frozen=True)
class Admitted:
    reservation: str
    need: int
    source: str
    max_path: str
    wave_path: str
    sampling_context: dict[str, object]


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
class ViewReply:
    reading: gate.QueueRead


@dataclass(frozen=True)
class ExplainReply:
    explanation: gate.QueueExplanation
    fallbacks: tuple[gate.ConfigFallback, ...] = ()


@dataclass(frozen=True)
class MineReply:
    reading: gate.QueueRead


Reply = Wait | Admitted | LaunchAdmitted | Refused | Ok | BuildHistoryReply | ViewReply | ExplainReply | MineReply

_REQUESTS: dict[str, type] = {
    "admit": AdmitRequest, "launch": LaunchRequest, "release": ReleaseRequest,
    "width": WidthRequest, "view": ViewRequest, "explain": ExplainRequest,
    "build_history": BuildHistoryRequest, "mine": MineRequest, "admit_head": AdmitHeadRequest,
    "priority_set": PrioritySetRequest, "priority_clear": PriorityClearRequest, "token_wait": TokenWaitRequest,
}
_REPLIES: dict[str, type] = {
    "wait": Wait, "admitted": Admitted, "launch_admitted": LaunchAdmitted,
    "build_history": BuildHistoryReply, "refused": Refused, "ok": Ok, "view": ViewReply, "explain": ExplainReply, "mine": MineReply,
}
T = TypeVar("T")


def admission_socket_path() -> Path:
    from scripts.build_hold.override_guard import refuse_overrides
    refuse_overrides(Path(__file__), "build_queue/admission_protocol.py")
    return Path(os.environ.get("ADMISSION_SOCKET") or "/run/build-admission/build-admission.sock")


def encode_record(value: object) -> str:
    """State and message payloads have exactly the domain codec's representation."""
    return json.dumps(gate.snapshot_value(value), separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def parse_record(text: str, schema: type[T]) -> T:
    value = cast(object, json.loads(text))
    return cast(T, gate.parse_snapshot_value(value, schema))


def _encode(value: object, variants: dict[str, type]) -> str:
    name = next((name for name, schema in variants.items() if type(value) is schema), None)
    if name is None:
        raise ValueError("unknown admission message")
    data = gate.snapshot_value(value)
    if isinstance(value, AdmitRequest) and isinstance(value.run, NoRun):
        # Remove this main-service compatibility after the admission cut-over.
        del cast(dict[str, object], data)["run"]
    return encode_record({"v": VERSION, "type": name, "data": data}) + "\n"


def _parse(line: str | bytes, variants: dict[str, type]) -> object:
    try:
        value = cast(object, json.loads(line))
        if not isinstance(value, dict):
            raise ValueError("invalid admission envelope")
        envelope = cast(dict[str, object], value)
        if type(envelope.get("v")) is not int or envelope["v"] != VERSION:
            raise UnsupportedVersion(envelope.get("v"))
        name = envelope.get("type")
        if not isinstance(name, str) or name not in variants or set(envelope) != {"v", "type", "data"}:
            raise ValueError("invalid admission envelope")
        data = envelope["data"]
        if variants[name] is AdmitRequest and isinstance(data, dict) and "run" not in data:
            data = {**cast(dict[str, object], data), "run": gate.snapshot_value(NO_RUN)}
        return gate.parse_snapshot_value(cast(object, data), variants[name])
    except (KeyError, TypeError, UnicodeError) as error:
        raise ValueError("invalid admission message") from error


def encode_request(request: Request) -> str:
    return _encode(request, _REQUESTS)


def parse_request(line: str | bytes) -> Request:
    return cast(Request, _parse(line, _REQUESTS))


def encode_reply(reply: Reply) -> str:
    return _encode(reply, _REPLIES)


def parse_reply(line: str | bytes, request: Request | None = None) -> Reply:
    reply = cast(Reply, _parse(line, _REPLIES))
    if (isinstance(request, LaunchRequest) and isinstance(reply, Admitted)
            or isinstance(request, AdmitRequest) and isinstance(reply, LaunchAdmitted)):
        raise ValueError("admission outcome does not match request")
    return reply
