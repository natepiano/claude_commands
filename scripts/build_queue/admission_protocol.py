"""Admission wire types with the queue records used by view replies."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from scripts.build_queue.admission_code import (
    ADMISSION_CODE_PATHS as ADMISSION_CODE_PATHS,
    admission_code_version as admission_code_version,
)
from scripts.build_queue import admission_records as records
from scripts.build_queue import cloud_progress, progress_profile
from scripts.build_queue.admission_wire import (
    AdmissionUnavailable as AdmissionUnavailable,
    UnsupportedVersion as UnsupportedVersion,
    Owner as Owner,
    RunIdentity as RunIdentity,
    NoRun as NoRun,
    LocalPlace as LocalPlace,
    CloudPlace as CloudPlace,
    RunPlace as RunPlace,
    Identity as Identity,
    CallerCode as CallerCode,
    AdmitRequest as AdmitRequest,
    ServiceEstimateRequest as ServiceEstimateRequest,
    LaunchRequest as LaunchRequest,
    ReleaseRequest as ReleaseRequest,
    WidthRequest as WidthRequest,
    BuildHistoryRequest as BuildHistoryRequest,
    ViewRequest as ViewRequest,
    ExplainRequest as ExplainRequest,
    MineRequest as MineRequest,
    AdmitHeadRequest as AdmitHeadRequest,
    PrioritySetRequest as PrioritySetRequest,
    PriorityClearRequest as PriorityClearRequest,
    TokenWaitRequest as TokenWaitRequest,
    CloudSlotWait as CloudSlotWait,
    Wait as Wait,
    AcquisitionTiming as AcquisitionTiming,
    CleanupTiming as CleanupTiming,
    Admitted as Admitted,
    RunnerExecutionTiming as RunnerExecutionTiming,
    RunnerAdmitted as RunnerAdmitted,
    LaunchAdmitted as LaunchAdmitted,
    Refused as Refused,
    Ok as Ok,
    BuildHistoryReply as BuildHistoryReply,
    PlacementRequest as PlacementRequest,
    ClaimFailure as ClaimFailure,
    ConsumePermission as ConsumePermission,
    ExecutionReport as ExecutionReport,
    PlacementWait as PlacementWait,
    Assignment as Assignment,
    FirstAuthorization as FirstAuthorization,
    DispatchStatus as DispatchStatus,
    VERSION as VERSION,
    NO_RUN as NO_RUN,
    PROTOCOL_ACQUISITION_TIMING as PROTOCOL_ACQUISITION_TIMING,
    PROTOCOL_CLEANUP_TIMING as PROTOCOL_CLEANUP_TIMING,
    CLEANUP_STRING_MAX as CLEANUP_STRING_MAX,
    CLEANUP_STATUS_MIN as CLEANUP_STATUS_MIN,
    CLEANUP_STATUS_MAX as CLEANUP_STATUS_MAX,
    CLEANUP_REQUEST_MAX_BYTES as CLEANUP_REQUEST_MAX_BYTES,
    Request as Request,
    admission_socket_path as admission_socket_path,
    encode_record as encode_record,
    parse_record as parse_record,
    encode_envelope,
    parse_envelope,
    encode_request as encode_request,
    parse_request as parse_request,
    parse_request_envelope as parse_request_envelope,
    noncleanup_request_started as noncleanup_request_started,
    REPLY_TYPES,
)

Profile = records.StepProfile | records.ProgramProfile


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



Reply = (Wait | RunnerAdmitted | Admitted | LaunchAdmitted | Refused | Ok | BuildHistoryReply | ViewReply | ExplainReply | MineReply
         | PlacementWait | Assignment | FirstAuthorization | DispatchStatus)

_REPLIES: dict[str, type] = {**REPLY_TYPES, "view": ViewReply, "explain": ExplainReply, "mine": MineReply}


def encode_reply(reply: Reply, *, version: int = VERSION) -> str:
    if version == 1 and isinstance(reply, (ViewReply, MineReply)) and isinstance(reply.reading, CloudQueueRead):
        reading = reply.reading
        local = records.QueueRead(reading.view, reading.explanation, reading.ordered, reading.diagnostics)
        reply = type(reply)(local)
    return encode_envelope(reply, _REPLIES, version)



def parse_reply(line: str | bytes, request: Request | None = None) -> Reply:
    reply = cast(Reply, parse_envelope(line, _REPLIES)[1])
    if (isinstance(request, LaunchRequest) and isinstance(reply, Admitted)
            or isinstance(request, (AdmitRequest, ServiceEstimateRequest)) and isinstance(reply, LaunchAdmitted)):
        raise ValueError("admission outcome does not match request")
    return reply
