#!/usr/bin/env python3
"""Validate cargo-berth envelopes and coordinate one claim transition at a time."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "berth"

CONTRACT = "cargo-berth-claim-state/v1"
KNOWN_VERBS = {
    "init",
    "board",
    "check",
    "claim",
    "drift",
    "release",
    "sequence",
    "integrate",
    "resolve",
    "renew",
}
KNOWN_PAYLOAD_KINDS = {
    "no_facts",
    "init",
    "projection_repair",
    "reinitialize",
    "board",
    "check",
    "claim",
    "drift",
    "release",
    "sequence",
    "integrate",
    "resolve",
    "renew",
}
VERB_PAYLOAD_KINDS = {
    "init": {"no_facts", "init", "projection_repair", "reinitialize"},
    "board": {"no_facts", "board"},
    "check": {"no_facts", "check"},
    "claim": {"no_facts", "claim"},
    "drift": {"no_facts", "drift"},
    "release": {"no_facts", "release"},
    "sequence": {"no_facts", "sequence"},
    "integrate": {"no_facts", "integrate"},
    "resolve": {"no_facts", "resolve"},
    "renew": {"no_facts", "renew"},
}
STATUS_PAYLOAD_KINDS = {
    "unimplemented": {"no_facts"},
    "board_ready": {"board"},
    "initialized": {"init"},
    "projection_repaired": {"projection_repair"},
    "reinitialized": {"reinitialize"},
    "ledger_unreadable": {"no_facts"},
    "unconfigured": {"no_facts"},
    "terminal_view_failed": {"no_facts"},
    "clear": {"check", "drift"},
    "claimed": {"claim"},
    "widened": {"drift"},
    "incursion": {"drift"},
    "drift_collision": {"drift"},
    "drift_attribution_required": {"drift"},
    "reservation_limit_reached": {"claim"},
    "ordering_edge_limit_reached": {"claim", "sequence"},
    "blocked_by_overlap": {"check", "claim"},
    "blocked_by_ordering": {"integrate"},
    "needs_user_authorization": {"claim"},
    "invalid_input": {"no_facts", "integrate", "sequence"},
    "contention": {"no_facts"},
    "sequenced": {"sequence"},
    "duplicate_ordering_edge": {"sequence"},
    "ordering_cycle": {"sequence"},
    "missing_deferral": {"sequence"},
    "outstanding": {"release"},
    "integrated": {"integrate", "release", "resolve"},
    "trunk_rewritten": {"release"},
    "object_unknown": {"release"},
    "released": {"release", "resolve"},
    "recovered": {"resolve"},
    "renewed": {"renew"},
    "incursion_resolved": {"resolve"},
}
KNOWN_STATUSES = set(STATUS_PAYLOAD_KINDS)
FIXED_STATUS_EXIT_CODES = {
    "unimplemented": 0,
    "board_ready": 0,
    "initialized": 0,
    "projection_repaired": 0,
    "reinitialized": 0,
    "unconfigured": 4,
    "ledger_unreadable": 4,
    "terminal_view_failed": 7,
    "clear": 0,
    "claimed": 0,
    "widened": 0,
    "incursion": 1,
    "drift_collision": 1,
    "drift_attribution_required": 1,
    "reservation_limit_reached": 1,
    "ordering_edge_limit_reached": 2,
    "blocked_by_overlap": 1,
    "blocked_by_ordering": 2,
    "needs_user_authorization": 3,
    "invalid_input": 5,
    "contention": 6,
    "sequenced": 0,
    "duplicate_ordering_edge": 2,
    "ordering_cycle": 2,
    "missing_deferral": 2,
    "outstanding": 0,
    "integrated": 0,
    "trunk_rewritten": 0,
    "object_unknown": 0,
    "released": 0,
    "recovered": 0,
    "renewed": 0,
    "incursion_resolved": 0,
}
BOARD_FIELDS = (
    "journal_position",
    "recovered_bypasses_this_invocation",
    "integration_order",
    "ready_now",
    "waiting",
    "settled_ordering_constraints",
    "unresolved_overlaps",
    "recorded_overlap_answers",
    "unconstrained_reservations",
    "resolved",
    "available_forced_permits",
    "bypass_audit",
    "outstanding_incursions",
    "recorded_incursion_answers",
    "alerts",
    "git_cost",
)
BOARD_SECTIONS = (
    "ready_now",
    "waiting",
    "settled_ordering_constraints",
    "unresolved_overlaps",
    "recorded_overlap_answers",
    "unconstrained_reservations",
    "resolved",
    "available_forced_permits",
    "bypass_audit",
    "outstanding_incursions",
    "recorded_incursion_answers",
    "alerts",
)
BOARD_GIT_COST_FIELDS = (
    "trunk_resolution_calls",
    "worktree_list_calls",
    "reservation_evidence_revalidations",
    "protected_predecessor_ancestry_queries",
    "worktree_ahead_behind_computations",
    "orphan_recovery_evidence_queries",
)
USER_ACTION_KEYS = {
    "action",
    "instruction",
    "flag",
    "flags",
    "resolve_flag",
    "resolution",
}


class EnvelopePayload(TypedDict):
    """The validated payload tag, alerts, and verb-owned facts."""

    kind: str
    alerts: list[object]
    data: NotRequired[dict[str, object]]


class EnvelopeValue(TypedDict):
    """The frozen cargo-berth JSON envelope after boundary validation."""

    verb: str
    status: str
    exit_code: int
    reservations: list[str]
    blocked_by: list[str]
    message: str
    payload: EnvelopePayload


class ReservationScopeValue(TypedDict):
    """One validated exact-file or tree reservation scope."""

    path: str
    kind: Literal["file", "tree"]


class BranchClaimHeadValue(TypedDict):
    """A holder attached to one full branch ref at one commit."""

    kind: Literal["branch"]
    full_ref: str
    head: str


class DetachedClaimHeadValue(TypedDict):
    """A holder detached at one commit."""

    kind: Literal["detached"]
    head: str


ClaimHeadValue = BranchClaimHeadValue | DetachedClaimHeadValue


class WorkPlanClaimSourceValue(TypedDict):
    """A claim attributed to an external work plan and phase."""

    kind: Literal["work_plan"]
    plan: str
    phase: str


class FirstTouchClaimSourceValue(TypedDict):
    """A claim acquired by the edit that first touched its paths."""

    kind: Literal["first_touch"]


class ExplicitClaimSourceValue(TypedDict):
    """A direct claim carrying no plan or phase."""

    kind: Literal["explicit"]


ClaimSourceValue = (
    WorkPlanClaimSourceValue | FirstTouchClaimSourceValue | ExplicitClaimSourceValue
)


class ExplainedReservationPurposeValue(TypedDict):
    """A caller-supplied reason for protecting the paths."""

    kind: Literal["explained"]
    explanation: str


class UnexplainedReservationPurposeValue(TypedDict):
    """A reservation whose caller supplied no reason."""

    kind: Literal["not_provided_by_caller"]


ReservationPurposeValue = (
    ExplainedReservationPurposeValue | UnexplainedReservationPurposeValue
)


class ActiveHolderActivityValue(TypedDict):
    """A holder that recorded activity inside the freshness window."""

    status: Literal["active"]
    last_activity_at: str


class QuietHolderActivityValue(TypedDict):
    """A holder that has gone quiet beyond the freshness window."""

    status: Literal["quiet"]
    last_activity_at: str


HolderActivityValue = ActiveHolderActivityValue | QuietHolderActivityValue


class ReservationConflictValue(TypedDict):
    """Every engine fact needed to understand one blocking holder."""

    reservation_id: str
    reservation_revision: int
    overlap_scope_revision: list[ReservationScopeValue]
    holder_worktree_id: str
    holder_run_id: str
    head_snapshot: ClaimHeadValue
    source: ClaimSourceValue
    purpose: ReservationPurposeValue
    overlapping_scopes: list[ReservationScopeValue]
    claimed_at: str
    activity: HolderActivityValue


class PublishedMappingValue(TypedDict):
    """A disposable identity projection was published."""

    status: Literal["published"]


class UnavailableMappingValue(TypedDict):
    """A durable reservation outlived a failed identity projection update."""

    status: Literal["unavailable"]
    diagnostic: str


MappingPublicationValue = PublishedMappingValue | UnavailableMappingValue


class FirstTouchAcquisitionValue(TypedDict):
    """The reservation protection established by a clear edit check."""

    kind: Literal["appended", "widened", "already_held"]
    reservation_id: str
    coordination_run_id: str
    phase_start_head: str
    marker_publication: MappingPublicationValue
    session_mapping_publication: MappingPublicationValue


class ClearCheckStateValue(TypedDict):
    """A clear edit check and any degraded session-mapping consequence."""

    kind: Literal["edit_authorized"]
    acquisition: FirstTouchAcquisitionValue
    scopes: list[ReservationScopeValue]
    outcome: Literal["success", "nonblocking_degraded_success"]
    rendered_markdown: str


class SequencedOverlapAnswerValue(TypedDict):
    """A proposal that orders requester and holder."""

    kind: Literal["sequence"]
    blocker: str
    direction: Literal["requester_before_holder", "holder_before_requester"]


class DeferredOverlapAnswerValue(TypedDict):
    """A proposal that records an unresolved overlap without an edge."""

    kind: Literal["defer"]
    blocker: str


class OverriddenOverlapAnswerValue(TypedDict):
    """A proposal that records an unconstrained overlap without an edge."""

    kind: Literal["override"]
    blocker: str


OverlapAnswerValue = (
    SequencedOverlapAnswerValue
    | DeferredOverlapAnswerValue
    | OverriddenOverlapAnswerValue
)

OverlapAnswerConsequenceValue = Literal[
    "sequenced_integration",
    "both_integrations_held",
    "integration_unconstrained",
]


class ProposalAwaitingApprovalStateValue(TypedDict):
    """An answered claim whose exact proposal still needs later approval."""

    kind: Literal["proposal_awaiting_approval"]
    current_holders: list[ReservationConflictValue]
    answer: OverlapAnswerValue
    authorization_reason: str
    consequence: OverlapAnswerConsequenceValue
    proposal: dict[str, object]
    proposal_token: str
    rendered_markdown: str


class ClaimedStateValue(TypedDict):
    """A durable claim and the publication outcome callers must surface."""

    kind: Literal["claimed"]
    reservation_id: str
    coordination_run_id: str
    scopes: list[ReservationScopeValue]
    marker_publication: MappingPublicationValue
    session_mapping_publication: MappingPublicationValue
    session_mapping_guarantee: Literal[
        "durable_claim_with_mapping", "durable_claim_mapping_unavailable"
    ]
    outcome: Literal["success", "nonblocking_degraded_success"]
    rendered_markdown: str


class ReasonedClaimRefusalAnswerValue(TypedDict):
    """A blocked-edit answer that proposes one reasoned claim action."""

    kind: Literal["reasoned_claim"]
    selection: Literal["before", "after", "defer", "override"]
    label: str
    adds_ordering_edge: bool
    consequence: str


class LeaveAloneRefusalAnswerValue(TypedDict):
    """A blocked-edit answer that deliberately takes no engine action."""

    kind: Literal["leave_alone"]
    selection: Literal["leave_alone"]
    label: str
    consequence: str


RefusalAnswerValue = ReasonedClaimRefusalAnswerValue | LeaveAloneRefusalAnswerValue


class ExactRequestedScopeFactsValue(TypedDict):
    """A blocked check carried the exact scopes it tried to acquire."""

    kind: Literal["exact_requested_scopes"]
    requested_scopes: list[ReservationScopeValue]


class HolderSharedScopeFactsValue(TypedDict):
    """A blocked claim carries requested-scope facts only through each holder."""

    kind: Literal["holder_shared_scopes_only"]


RefusalScopeFactsValue = ExactRequestedScopeFactsValue | HolderSharedScopeFactsValue


class FirstTouchDispositionValue(TypedDict):
    """One first-touch holder and the verbs that clear it, which no answer reaches."""

    reservation_id: str
    release: str
    integrated_as: str
    abandon: str


class RefusalPresentationValue(TypedDict):
    """The complete refusal presentation shared by /sync and the edit shim."""

    kind: Literal["blocked"]
    current_holders: list[ReservationConflictValue]
    scope_facts: RefusalScopeFactsValue
    answers: list[RefusalAnswerValue]
    first_touch_dispositions: list[FirstTouchDispositionValue]
    rendered_markdown: str


class EnvelopeValidationError(Exception):
    """Process output did not satisfy the frozen envelope contract."""


class CoordinatorError(Exception):
    """The coordinator could not safely make an engine invocation."""


@dataclass(frozen=True)
class ValidatedEnvelope:
    """A frozen response whose process status and tagged payload agree."""

    value: EnvelopeValue

    @property
    def verb(self) -> str:
        return self.value["verb"]

    @property
    def status(self) -> str:
        return self.value["status"]

    @property
    def exit_code(self) -> int:
        return self.value["exit_code"]

    @property
    def payload(self) -> EnvelopePayload:
        return self.value["payload"]


@dataclass(frozen=True)
class EngineInvocation:
    """One completed engine process; there is deliberately no retry state."""

    argv: tuple[str, ...]
    process_exit: int
    standard_error: str
    envelope: ValidatedEnvelope

    def tagged(self) -> dict[str, object]:
        return {
            "kind": "single_attempt",
            "argv": list(self.argv),
            "attempts": 1,
            "process_exit": self.process_exit,
            "standard_error": self.standard_error,
        }


@dataclass(frozen=True)
class NeutralClaimTransition:
    """The caller requests only a collision observation."""


@dataclass(frozen=True)
class AnsweredClaimTransition:
    """The user's answer and reason request a fresh proposal."""

    answer: str
    blocker: str
    reason: str


@dataclass(frozen=True)
class ApprovedClaimTransition:
    """A later approval submits one exact proposal token."""

    answer: str
    blocker: str
    reason: str
    proposal: str


ClaimTransition = (
    NeutralClaimTransition | AnsweredClaimTransition | ApprovedClaimTransition
)


class InactiveIdentityKind(Enum):
    """The stale identity source named by a structured engine rejection."""

    SESSION_MAPPING = "inactive_session_mapping"
    MARKER_RUN = "inactive_marker_run"

    @property
    def recovery(self) -> str:
        if self is InactiveIdentityKind.SESSION_MAPPING:
            return "restart the coordination run or name the active reservation explicitly"
        return "remove or replace the stale worktree coordination marker"


@dataclass(frozen=True)
class InactiveIdentityRejection:
    """A typed inactive-identity rejection with its source-specific recovery."""

    kind: InactiveIdentityKind
    coordination_run_id: str

    def tagged(self, diagnostic: str) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "coordination_run_id": self.coordination_run_id,
            "diagnostic": diagnostic,
            "recovery": self.kind.recovery,
        }


@dataclass(frozen=True)
class NoInactiveIdentityRejection:
    """The payload carries no typed inactive-identity reason."""


InactiveIdentityClassification = (
    InactiveIdentityRejection | NoInactiveIdentityRejection
)


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _require_object(
    value: object, path: str, *, nonempty: bool = True
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise EnvelopeValidationError(f"{path} must be an object")
    untyped_mapping = cast(dict[object, object], value)
    if nonempty and not untyped_mapping:
        raise EnvelopeValidationError(f"{path} must not be an empty object")
    if not all(isinstance(key, str) for key in untyped_mapping):
        raise EnvelopeValidationError(f"{path} keys must be strings")
    return {
        key: child
        for key, child in untyped_mapping.items()
        if isinstance(key, str)
    }


def _require_list(value: object, path: str) -> list[object]:
    if not isinstance(value, list):
        raise EnvelopeValidationError(f"{path} must be an array")
    return cast(list[object], value)


def _require_string(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise EnvelopeValidationError(f"{path} must be a string")
    if not value:
        raise EnvelopeValidationError(f"{path} must not be an empty string")
    return value


def _require_integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EnvelopeValidationError(f"{path} must be an integer")
    return value


def _required_field(value: dict[str, object], key: str, path: str) -> object:
    if key not in value:
        raise EnvelopeValidationError(f"{path}.{key} is missing")
    if value[key] is None:
        raise EnvelopeValidationError(f"{path}.{key} must not be null")
    return value[key]


def _validate_identifier_array(value: object, path: str) -> list[str]:
    identifiers = _require_list(value, path)
    validated: list[str] = []
    for index, identifier in enumerate(identifiers):
        validated.append(_require_string(identifier, f"{path}[{index}]"))
    return validated


def parse_envelope(
    serialized: str, process_exit: int, expected_verb: str
) -> ValidatedEnvelope:
    """Parse untrusted process output and validate every frozen envelope field."""

    try:
        decoded = cast(object, json.loads(serialized))
    except json.JSONDecodeError as error:
        raise EnvelopeValidationError(f"stdout is not one JSON value: {error}") from error
    envelope = _require_object(decoded, "envelope")
    verb = _require_string(_required_field(envelope, "verb", "envelope"), "envelope.verb")
    status = _require_string(
        _required_field(envelope, "status", "envelope"), "envelope.status"
    )
    exit_code = _require_integer(
        _required_field(envelope, "exit_code", "envelope"), "envelope.exit_code"
    )
    reservations = _validate_identifier_array(
        _required_field(envelope, "reservations", "envelope"),
        "envelope.reservations",
    )
    blocked_by = _validate_identifier_array(
        _required_field(envelope, "blocked_by", "envelope"),
        "envelope.blocked_by",
    )
    message = _require_string(
        _required_field(envelope, "message", "envelope"), "envelope.message"
    )
    payload_value = _required_field(envelope, "payload", "envelope")

    if verb not in KNOWN_VERBS:
        raise EnvelopeValidationError(f"unknown verb tag {verb!r}")
    if verb != expected_verb:
        raise EnvelopeValidationError(
            f"expected verb {expected_verb!r}, received {verb!r}"
        )
    if status not in KNOWN_STATUSES:
        raise EnvelopeValidationError(f"unknown status tag {status!r}")
    if exit_code != process_exit:
        raise EnvelopeValidationError(
            f"process exit {process_exit} disagrees with envelope.exit_code {exit_code}"
        )
    if status in FIXED_STATUS_EXIT_CODES:
        required_exit = FIXED_STATUS_EXIT_CODES[status]
        if exit_code != required_exit:
            raise EnvelopeValidationError(
                f"status {status!r} requires exit code {required_exit}, received {exit_code}"
            )
    payload = _require_object(payload_value, "envelope.payload")
    kind = _require_string(
        _required_field(payload, "kind", "envelope.payload"), "envelope.payload.kind"
    )
    if kind not in KNOWN_PAYLOAD_KINDS:
        raise EnvelopeValidationError(f"unknown payload kind tag {kind!r}")
    if kind not in VERB_PAYLOAD_KINDS[verb]:
        raise EnvelopeValidationError(
            f"payload kind {kind!r} does not belong to verb {verb!r}"
        )
    if kind not in STATUS_PAYLOAD_KINDS[status]:
        raise EnvelopeValidationError(
            f"status {status!r} does not permit payload kind {kind!r}"
        )
    alerts = _require_list(
        _required_field(payload, "alerts", "envelope.payload"),
        "envelope.payload.alerts",
    )
    validated_payload: EnvelopePayload = {"kind": kind, "alerts": alerts}
    if kind == "no_facts":
        if "data" in payload:
            raise EnvelopeValidationError("no_facts payload must not contain data")
    else:
        validated_payload["data"] = _require_object(
            _required_field(payload, "data", "envelope.payload"),
            "envelope.payload.data",
        )
    validated_value: EnvelopeValue = {
        "verb": verb,
        "status": status,
        "exit_code": exit_code,
        "reservations": reservations,
        "blocked_by": blocked_by,
        "message": message,
        "payload": validated_payload,
    }
    return ValidatedEnvelope(validated_value)


def _publication(value: object, path: str) -> MappingPublicationValue:
    publication = _require_object(value, path)
    status = _require_string(_required_field(publication, "status", path), f"{path}.status")
    if status == "published":
        return {"status": "published"}
    if status == "unavailable":
        diagnostic = _require_string(
            _required_field(publication, "diagnostic", path), f"{path}.diagnostic"
        )
        return {"status": "unavailable", "diagnostic": diagnostic}
    raise EnvelopeValidationError(f"unknown publication status {status!r} at {path}")


def _claim_scope_set(value: object, path: str) -> list[ReservationScopeValue]:
    scopes = _require_list(value, path)
    if not scopes:
        raise EnvelopeValidationError(f"{path} must contain at least one scope")
    validated: list[ReservationScopeValue] = []
    for index, value_scope in enumerate(scopes):
        scope = _require_object(value_scope, f"{path}[{index}]")
        scope_path = _require_string(
            _required_field(scope, "path", f"{path}[{index}]"), f"{path}[{index}].path"
        )
        kind = _require_string(
            _required_field(scope, "kind", f"{path}[{index}]"), f"{path}[{index}].kind"
        )
        if kind not in {"file", "tree"}:
            raise EnvelopeValidationError(f"unknown scope kind {kind!r} at {path}[{index}]")
        validated.append(
            {
                "path": scope_path,
                "kind": cast(Literal["file", "tree"], kind),
            }
        )
    return validated


def _claim_head(value: object, path: str) -> ClaimHeadValue:
    head = _require_object(value, path)
    kind = _require_string(_required_field(head, "kind", path), f"{path}.kind")
    commit = _require_string(_required_field(head, "head", path), f"{path}.head")
    if kind == "branch":
        full_ref = _require_string(
            _required_field(head, "full_ref", path), f"{path}.full_ref"
        )
        return {"kind": "branch", "full_ref": full_ref, "head": commit}
    if kind == "detached":
        return {"kind": "detached", "head": commit}
    raise EnvelopeValidationError(f"unknown claim head kind {kind!r} at {path}")


def _claim_source(value: object, path: str) -> ClaimSourceValue:
    source = _require_object(value, path)
    kind = _require_string(_required_field(source, "kind", path), f"{path}.kind")
    if kind == "explicit":
        for field in ("plan", "phase"):
            if field in source:
                raise EnvelopeValidationError(
                    f"{path}.{field} must not be present for an explicit source"
                )
        return {"kind": "explicit"}
    if kind == "work_plan":
        plan = _require_string(_required_field(source, "plan", path), f"{path}.plan")
        phase = _require_string(_required_field(source, "phase", path), f"{path}.phase")
        return {"kind": "work_plan", "plan": plan, "phase": phase}
    if kind == "first_touch":
        for field in ("plan", "phase"):
            if field in source:
                raise EnvelopeValidationError(
                    f"{path}.{field} must not be present for a first_touch source"
                )
        return {"kind": "first_touch"}
    raise EnvelopeValidationError(f"unknown claim source kind {kind!r} at {path}")


def _claim_purpose(value: object, path: str) -> ReservationPurposeValue:
    purpose = _require_object(value, path)
    kind = _require_string(_required_field(purpose, "kind", path), f"{path}.kind")
    if kind == "explained":
        explanation = _require_string(
            _required_field(purpose, "explanation", path), f"{path}.explanation"
        )
        return {"kind": "explained", "explanation": explanation}
    if kind == "not_provided_by_caller":
        return {"kind": "not_provided_by_caller"}
    raise EnvelopeValidationError(f"unknown reservation purpose kind {kind!r} at {path}")


def _holder_activity(value: object, path: str) -> HolderActivityValue:
    activity = _require_object(value, path)
    status = _require_string(_required_field(activity, "status", path), f"{path}.status")
    last_activity_at = _require_string(
        _required_field(activity, "last_activity_at", path),
        f"{path}.last_activity_at",
    )
    if status == "active":
        return {"status": "active", "last_activity_at": last_activity_at}
    if status == "quiet":
        return {"status": "quiet", "last_activity_at": last_activity_at}
    raise EnvelopeValidationError(f"unknown holder activity status {status!r} at {path}")


def _claim_conflicts(value: object, path: str) -> list[ReservationConflictValue]:
    conflicts = _require_list(value, path)
    if not conflicts:
        raise EnvelopeValidationError(f"{path} must contain every current holder")
    validated: list[ReservationConflictValue] = []
    for index, value_conflict in enumerate(conflicts):
        conflict_path = f"{path}[{index}]"
        conflict = _require_object(value_conflict, conflict_path)
        reservation_id = _require_string(
            _required_field(conflict, "reservation_id", conflict_path),
            f"{conflict_path}.reservation_id",
        )
        reservation_revision = _require_integer(
            _required_field(conflict, "reservation_revision", conflict_path),
            f"{conflict_path}.reservation_revision",
        )
        overlap_scope_revision = _claim_scope_set(
            _required_field(conflict, "overlap_scope_revision", conflict_path),
            f"{conflict_path}.overlap_scope_revision",
        )
        holder_worktree_id = _require_string(
            _required_field(conflict, "holder_worktree_id", conflict_path),
            f"{conflict_path}.holder_worktree_id",
        )
        holder_run_id = _require_string(
            _required_field(conflict, "holder_run_id", conflict_path),
            f"{conflict_path}.holder_run_id",
        )
        head_snapshot = _claim_head(
            _required_field(conflict, "head_snapshot", conflict_path),
            f"{conflict_path}.head_snapshot",
        )
        source = _claim_source(
            _required_field(conflict, "source", conflict_path),
            f"{conflict_path}.source",
        )
        purpose = _claim_purpose(
            _required_field(conflict, "purpose", conflict_path), f"{conflict_path}.purpose"
        )
        overlapping_scopes = _claim_scope_set(
            _required_field(conflict, "overlapping_scopes", conflict_path),
            f"{conflict_path}.overlapping_scopes",
        )
        claimed_at = _require_string(
            _required_field(conflict, "claimed_at", conflict_path),
            f"{conflict_path}.claimed_at",
        )
        activity = _holder_activity(
            _required_field(conflict, "activity", conflict_path),
            f"{conflict_path}.activity",
        )
        validated.append(
            {
                "reservation_id": reservation_id,
                "reservation_revision": reservation_revision,
                "overlap_scope_revision": overlap_scope_revision,
                "holder_worktree_id": holder_worktree_id,
                "holder_run_id": holder_run_id,
                "head_snapshot": head_snapshot,
                "source": source,
                "purpose": purpose,
                "overlapping_scopes": overlapping_scopes,
                "claimed_at": claimed_at,
                "activity": activity,
            }
        )
    return validated


def _claim_answer(value: object, path: str) -> OverlapAnswerValue:
    answer = _require_object(value, path)
    kind = _require_string(_required_field(answer, "kind", path), f"{path}.kind")
    if kind not in {"sequence", "defer", "override"}:
        raise EnvelopeValidationError(f"unknown overlap answer kind {kind!r}")
    blocker = _require_string(
        _required_field(answer, "blocker", path), f"{path}.blocker"
    )
    if kind == "sequence":
        direction = _require_string(
            _required_field(answer, "direction", path), f"{path}.direction"
        )
        if direction not in {"requester_before_holder", "holder_before_requester"}:
            raise EnvelopeValidationError(f"unknown sequence direction {direction!r}")
        return {
            "kind": "sequence",
            "blocker": blocker,
            "direction": cast(
                Literal["requester_before_holder", "holder_before_requester"],
                direction,
            ),
        }
    if "direction" in answer:
        raise EnvelopeValidationError(
            f"{path}.direction must not be present for an overlap answer of {kind!r}"
        )
    if kind == "defer":
        return {"kind": "defer", "blocker": blocker}
    return {"kind": "override", "blocker": blocker}


def _refusal_answers() -> list[RefusalAnswerValue]:
    return [
        {
            "kind": "reasoned_claim",
            "selection": "before",
            "label": "Land before the holder",
            "adds_ordering_edge": True,
            "consequence": (
                "The requester takes the paths and integrates first; the holder is held "
                "until the requester is on trunk."
            ),
        },
        {
            "kind": "reasoned_claim",
            "selection": "after",
            "label": "Land after the holder",
            "adds_ordering_edge": True,
            "consequence": (
                "The requester takes the paths and integrates second; it remains held until "
                "the holder's protected tip is on trunk and is an ancestor of the "
                "requester's HEAD."
            ),
        },
        {
            "kind": "reasoned_claim",
            "selection": "defer",
            "label": "Defer the order",
            "adds_ordering_edge": False,
            "consequence": (
                "The requester takes the paths, no ordering edge is added, and the unresolved "
                "overlap stays visible on the board until someone sequences it."
            ),
        },
        {
            "kind": "reasoned_claim",
            "selection": "override",
            "label": "Override",
            "adds_ordering_edge": False,
            "consequence": (
                "The requester takes the paths, no ordering edge is added, and the recorded "
                "override and its reason stay visible on the board."
            ),
        },
        {
            "kind": "leave_alone",
            "selection": "leave_alone",
            "label": "Leave it alone",
            "consequence": (
                "Take no engine action, append nothing, and work elsewhere."
            ),
        },
    ]


def _claim_head_description(head: ClaimHeadValue) -> str:
    if head["kind"] == "branch":
        return head["full_ref"]
    return f"detached at {head['head']}"


def _claim_source_description(source: ClaimSourceValue) -> str:
    if source["kind"] == "work_plan":
        return f"work-plan claim; plan {source['plan']}; phase {source['phase']}"
    if source["kind"] == "first_touch":
        return "first-touch claim; no plan and no phase"
    return "explicit claim; no plan and no phase"


def _claim_purpose_description(purpose: ReservationPurposeValue) -> str:
    if purpose["kind"] == "explained":
        return purpose["explanation"]
    return "not provided by caller"


def _holder_activity_description(activity: HolderActivityValue) -> str:
    if activity["status"] == "active":
        return f"active; last activity {activity['last_activity_at']}"
    return f"gone quiet; last activity {activity['last_activity_at']}"


def _scope_description(scopes: list[ReservationScopeValue]) -> str:
    return ", ".join(f"{scope['kind']}:{scope['path']}" for scope in scopes)


def _render_holder_facts_markdown(
    conflicts: list[ReservationConflictValue],
    scope_facts: RefusalScopeFactsValue,
) -> str:
    lines = [
        "cargo-berth blocked this edit because these holders overlap the requested paths:",
        "",
    ]
    if scope_facts["kind"] == "exact_requested_scopes":
        lines.extend(
            [
                f"Requested scopes: {_scope_description(scope_facts['requested_scopes'])}.",
                "",
            ]
        )
    for conflict in conflicts:
        lines.append(
            "".join(
                (
                    f"- Reservation `{conflict['reservation_id']}` held by run ",
                    f"`{conflict['holder_run_id']}` on ",
                    f"`{_claim_head_description(conflict['head_snapshot'])}`; claimed ",
                    f"{conflict['claimed_at']}; ",
                    f"{_holder_activity_description(conflict['activity'])}; ",
                    f"{_claim_source_description(conflict['source'])}; reason: ",
                    f"{_claim_purpose_description(conflict['purpose'])}; shared scopes: ",
                    f"{_scope_description(conflict['overlapping_scopes'])}.",
                )
            )
        )
    return "\n".join(lines)


def _render_refusal_markdown(
    conflicts: list[ReservationConflictValue],
    scope_facts: RefusalScopeFactsValue,
    answers: list[RefusalAnswerValue],
    first_touch_dispositions: list[FirstTouchDispositionValue],
) -> str:
    lines = [
        _render_holder_facts_markdown(conflicts, scope_facts),
    ]
    lines.extend(
        [
            "",
            (
                "Choose exactly one answer for one named holder. The first four are "
                + "reasoned coordinator answers on `claim`, and each requires a non-empty "
                + "reason. Run the coordinator invocation shown for each answer:"
            ),
            "",
        ]
    )
    for index, answer in enumerate(answers, start=1):
        if answer["kind"] == "reasoned_claim":
            command_material = "".join(
                (
                    " — `PYTHONPATH=\"$HOME/.claude/scripts\" ",
                    "python3 -m berth.claim_state claim --cwd \"$PWD\" <paths...> ",
                    f"--answer {answer['selection']} ",
                    "--blocker <holder-reservation-id> ",
                    '--overlap-reason "<reason>"`',
                )
            )
        else:
            command_material = ""
        lines.append(
            f"{index}. **{answer['label']}**{command_material}. {answer['consequence']}"
        )
    lines.extend(
        [
            "",
            (
                "Only **Land before** and **Land after** add an ordering edge. Defer and "
                + "override add no edge; their recorded overlap remains visible on the board."
            ),
            "",
            (
                "An answered claim only mints a proposal at exit 3. Show that proposal and "
                + "wait for explicit approval in a later turn before submitting its exact "
                + "`--proposal` token. Never mint and spend a token together."
            ),
            "",
            "The trunk-gate bypass is not an edit answer and cannot permit this edit.",
        ]
    )
    if first_touch_dispositions:
        lines.extend(
            [
                "",
                (
                    "A first-touch holder was acquired by whichever edit reached the paths "
                    + "first, so it may protect no work at all. None of the answers above "
                    + "clears one; these verbs do, and they belong to the holder:"
                ),
                "",
            ]
        )
        lines.extend(
            "".join(
                (
                    f"- Reservation `{disposition['reservation_id']}`: ",
                    f"`{disposition['release']}` once the work is on trunk, ",
                    f"`{disposition['integrated_as']}` after that release when git ",
                    "cannot prove the integration, or ",
                    f"`{disposition['abandon']}` when the work was discarded.",
                )
            )
            for disposition in first_touch_dispositions
        )
        lines.extend(
            [
                "",
                (
                    "`release` records the protected checkpoint and must run from the "
                    + "holder's own worktree. Both `resolve` dispositions run from "
                    + "anywhere but assert facts about the holder's work, so ask the "
                    + "holder before recording one."
                ),
            ]
        )
    if len(conflicts) > 1:
        lines.extend(
            [
                "",
                (
                    "More than one holder remains. Narrow the requested scopes before asking "
                    + "for a proposal, because one proposal binds exactly one blocker."
                ),
            ]
        )
    return "\n".join(lines)


def _first_touch_dispositions(
    conflicts: list[ReservationConflictValue],
) -> list[FirstTouchDispositionValue]:
    return [
        {
            "reservation_id": conflict["reservation_id"],
            "release": f"cargo-berth release {conflict['reservation_id']}",
            "integrated_as": (
                f"cargo-berth resolve {conflict['reservation_id']} "
                "--integrated-as <TRUNK_OID>"
            ),
            "abandon": (
                f"cargo-berth resolve {conflict['reservation_id']} "
                "--abandon --why <WHY>"
            ),
        }
        for conflict in conflicts
        if conflict["source"]["kind"] == "first_touch"
    ]


def _refusal_presentation(
    conflicts: list[ReservationConflictValue],
    scope_facts: RefusalScopeFactsValue,
) -> RefusalPresentationValue:
    answers = _refusal_answers()
    first_touch_dispositions = _first_touch_dispositions(conflicts)
    return {
        "kind": "blocked",
        "current_holders": conflicts,
        "scope_facts": scope_facts,
        "answers": answers,
        "first_touch_dispositions": first_touch_dispositions,
        "rendered_markdown": _render_refusal_markdown(
            conflicts, scope_facts, answers, first_touch_dispositions
        ),
    }


def _proposal_consequence(
    answer: OverlapAnswerValue,
) -> OverlapAnswerConsequenceValue:
    if answer["kind"] == "sequence":
        return "sequenced_integration"
    if answer["kind"] == "defer":
        return "both_integrations_held"
    return "integration_unconstrained"


def _selected_direction_description(answer: OverlapAnswerValue) -> str:
    if answer["kind"] == "sequence":
        return answer["direction"]
    if answer["kind"] == "defer":
        return "no ordering direction; both integrations remain held"
    return "no ordering direction; integration remains unconstrained"


def _render_proposal_markdown(
    conflicts: list[ReservationConflictValue],
    answer: OverlapAnswerValue,
    reason: str,
    consequence: OverlapAnswerConsequenceValue,
    proposal: dict[str, object],
    token: str,
) -> str:
    holder_scope_facts: HolderSharedScopeFactsValue = {
        "kind": "holder_shared_scopes_only"
    }
    return "\n".join(
        [
            _render_holder_facts_markdown(conflicts, holder_scope_facts),
            "",
            "## Proposal awaiting explicit approval",
            "",
            f"- Selected answer: `{json.dumps(answer, sort_keys=True)}`",
            f"- Selected direction: {_selected_direction_description(answer)}",
            f"- Reason: {reason}",
            f"- Consequence: `{consequence}`",
            f"- Proposal facts: `{json.dumps(proposal, sort_keys=True)}`",
            f"- Exact transient proposal token: `{token}`",
            "",
            (
                "Stop here. This proposal is awaiting a later explicit approval. Apply "
                + "this exact token only after that approval; a token-bearing exit 3 is a "
                + "refreshed proposal and needs approval again."
            ),
        ]
    )


def classify_refusal(envelope: ValidatedEnvelope) -> RefusalPresentationValue:
    """Validate and render one blocked check or neutral claim envelope."""

    if envelope.verb not in {"check", "claim"}:
        raise EnvelopeValidationError(
            "refusal classification requires a check or claim envelope"
        )
    if envelope.status != "blocked_by_overlap" or envelope.exit_code != 1:
        raise EnvelopeValidationError(
            "refusal classification requires blocked_by_overlap at exit 1"
        )
    if envelope.payload["kind"] != envelope.verb or "data" not in envelope.payload:
        raise EnvelopeValidationError("blocked envelope is missing its verb payload")
    data = _require_object(envelope.payload["data"], "envelope.payload.data")
    state = _require_string(
        _required_field(data, "status", "envelope.payload.data"),
        "envelope.payload.data.status",
    )
    if state != "blocked":
        raise EnvelopeValidationError("blocked envelope payload status must be blocked")
    conflicts = _claim_conflicts(
        _required_field(data, "conflicts", "envelope.payload.data"),
        "envelope.payload.data.conflicts",
    )
    conflict_ids = [conflict["reservation_id"] for conflict in conflicts]
    if envelope.value["reservations"]:
        raise EnvelopeValidationError("a blocked edit must not report granted reservations")
    if envelope.value["blocked_by"] != conflict_ids:
        raise EnvelopeValidationError(
            "envelope.blocked_by must match the conflicts in payload order"
        )
    if envelope.verb == "check":
        scopes = _claim_scope_set(
            _required_field(data, "scopes", "envelope.payload.data"),
            "envelope.payload.data.scopes",
        )
        exact_scope_facts: ExactRequestedScopeFactsValue = {
            "kind": "exact_requested_scopes",
            "requested_scopes": scopes,
        }
        return _refusal_presentation(conflicts, exact_scope_facts)
    holder_scope_facts: HolderSharedScopeFactsValue = {
        "kind": "holder_shared_scopes_only"
    }
    return _refusal_presentation(conflicts, holder_scope_facts)


def classify_claim(envelope: ValidatedEnvelope) -> dict[str, object]:
    """Turn a validated claim envelope into the shared authorization state machine."""

    if envelope.verb != "claim":
        raise EnvelopeValidationError("claim classification requires a claim envelope")
    if envelope.status == "unconfigured":
        return {
            "kind": "unconfigured",
            "diagnostic": envelope.value["message"],
            "remedy": "cargo-berth init",
        }
    if envelope.status == "contention":
        return {
            "kind": "busy",
            "diagnostic": envelope.value["message"],
            "instruction": "the ledger is busy, try again",
        }
    if envelope.status == "invalid_input":
        return {
            "kind": "invalid_input",
            "diagnostic": envelope.value["message"],
        }
    if envelope.status == "ledger_unreadable":
        return {
            "kind": envelope.status,
            "diagnostic": envelope.value["message"],
        }
    if envelope.payload["kind"] != "claim":
        raise EnvelopeValidationError(
            f"claim status {envelope.status!r} requires a claim payload"
        )
    if "data" not in envelope.payload:
        raise EnvelopeValidationError("claim payload is missing data")
    data = _require_object(envelope.payload["data"], "envelope.payload.data")
    state = _require_string(
        _required_field(data, "status", "envelope.payload.data"),
        "envelope.payload.data.status",
    )
    if state == "blocked":
        return cast(dict[str, object], cast(object, classify_refusal(envelope)))
    if state == "needs_user_authorization":
        if envelope.exit_code != 3 or envelope.status != "needs_user_authorization":
            raise EnvelopeValidationError("proposal claim tags do not agree with exit 3")
        conflicts = _claim_conflicts(
            _required_field(data, "conflicts", "envelope.payload.data"),
            "envelope.payload.data.conflicts",
        )
        answer = _claim_answer(
            _required_field(data, "answer", "envelope.payload.data"),
            "envelope.payload.data.answer",
        )
        reason = _require_string(
            _required_field(data, "authorization_reason", "envelope.payload.data"),
            "envelope.payload.data.authorization_reason",
        )
        consequence = _require_string(
            _required_field(data, "consequence", "envelope.payload.data"),
            "envelope.payload.data.consequence",
        )
        if consequence not in {
            "sequenced_integration",
            "both_integrations_held",
            "integration_unconstrained",
        }:
            raise EnvelopeValidationError(f"unknown proposal consequence {consequence!r}")
        proposal = _require_object(
            _required_field(data, "proposal", "envelope.payload.data"),
            "envelope.payload.data.proposal",
        )
        token = _require_string(
            _required_field(data, "proposal_token", "envelope.payload.data"),
            "envelope.payload.data.proposal_token",
        )
        conflict_ids = [conflict["reservation_id"] for conflict in conflicts]
        if envelope.value["reservations"]:
            raise EnvelopeValidationError(
                "a proposal awaiting approval must not report granted reservations"
            )
        if envelope.value["blocked_by"] != conflict_ids:
            raise EnvelopeValidationError(
                "proposal envelope.blocked_by must match the current conflicts"
            )
        if len(conflicts) != 1:
            raise EnvelopeValidationError(
                "a proposal awaiting approval must display exactly one conflict"
            )
        displayed_blocker = conflicts[0]["reservation_id"]
        if answer["blocker"] != displayed_blocker:
            raise EnvelopeValidationError(
                "the proposal answer blocker must match its single displayed conflict"
            )
        expected_consequence = _proposal_consequence(answer)
        if consequence != expected_consequence:
            raise EnvelopeValidationError(
                "the proposal consequence must agree with its selected answer"
            )
        typed_consequence = expected_consequence
        proposal_markdown = _render_proposal_markdown(
            conflicts,
            answer,
            reason,
            typed_consequence,
            proposal,
            token,
        )
        proposal_state: ProposalAwaitingApprovalStateValue = {
            "kind": "proposal_awaiting_approval",
            "current_holders": conflicts,
            "answer": answer,
            "authorization_reason": reason,
            "consequence": typed_consequence,
            "proposal": proposal,
            "proposal_token": token,
            "rendered_markdown": proposal_markdown,
        }
        return cast(dict[str, object], cast(object, proposal_state))
    if state == "claimed":
        if envelope.exit_code != 0 or envelope.status != "claimed":
            raise EnvelopeValidationError("claimed tags do not agree with exit 0")
        reservation_id = _require_string(
            _required_field(data, "reservation_id", "envelope.payload.data"),
            "envelope.payload.data.reservation_id",
        )
        coordination_run_id = _require_string(
            _required_field(data, "coordination_run_id", "envelope.payload.data"),
            "envelope.payload.data.coordination_run_id",
        )
        scopes = _claim_scope_set(
            _required_field(data, "scopes", "envelope.payload.data"),
            "envelope.payload.data.scopes",
        )
        marker = _publication(
            _required_field(data, "marker_publication", "envelope.payload.data"),
            "envelope.payload.data.marker_publication",
        )
        session_mapping = _publication(
            _required_field(data, "session_mapping_publication", "envelope.payload.data"),
            "envelope.payload.data.session_mapping_publication",
        )
        mapping_is_available = session_mapping["status"] == "published"
        rendered_markdown = (
            "".join(
                (
                    f"Claimed reservation `{reservation_id}` for coordination run ",
                    f"`{coordination_run_id}`. The durable claim and session mapping are available.",
                )
            )
            if mapping_is_available
            else "".join(
                (
                    f"Claimed reservation `{reservation_id}` for coordination run ",
                    f"`{coordination_run_id}` with nonblocking degraded success. The journal ",
                    "and reservation are durable, but the disposable session mapping is ",
                    f"unavailable: {session_mapping['diagnostic']}. Continue, name reservation ",
                    f"`{reservation_id}` explicitly from now on, and expect later edits to fall ",
                    "back to `CARGO_BERTH_RUN` or the worktree marker.",
                )
            )
        )
        result: ClaimedStateValue = {
            "kind": "claimed",
            "reservation_id": reservation_id,
            "coordination_run_id": coordination_run_id,
            "scopes": scopes,
            "marker_publication": marker,
            "session_mapping_publication": session_mapping,
            "session_mapping_guarantee": (
                "durable_claim_with_mapping"
                if mapping_is_available
                else "durable_claim_mapping_unavailable"
            ),
            "outcome": (
                "success" if mapping_is_available else "nonblocking_degraded_success"
            ),
            "rendered_markdown": rendered_markdown,
        }
        return cast(dict[str, object], cast(object, result))
    if state in {"reservation_limit_reached", "ordering_edge_limit_reached"}:
        _ = _require_integer(
            _required_field(data, "maximum", "envelope.payload.data"),
            "envelope.payload.data.maximum",
        )
        return {"kind": state, "facts": data}
    raise EnvelopeValidationError(f"unknown claim payload status {state!r}")


def _first_touch_acquisition(value: object, path: str) -> FirstTouchAcquisitionValue:
    acquisition = _require_object(value, path)
    kind = _require_string(_required_field(acquisition, "kind", path), f"{path}.kind")
    if kind not in {"appended", "widened", "already_held"}:
        raise EnvelopeValidationError(
            f"unknown first-touch acquisition kind {kind!r} at {path}"
        )
    reservation_id = _require_string(
        _required_field(acquisition, "reservation_id", path),
        f"{path}.reservation_id",
    )
    coordination_run_id = _require_string(
        _required_field(acquisition, "coordination_run_id", path),
        f"{path}.coordination_run_id",
    )
    phase_start_head = _require_string(
        _required_field(acquisition, "phase_start_head", path),
        f"{path}.phase_start_head",
    )
    marker_publication = _publication(
        _required_field(acquisition, "marker_publication", path),
        f"{path}.marker_publication",
    )
    session_mapping_publication = _publication(
        _required_field(acquisition, "session_mapping_publication", path),
        f"{path}.session_mapping_publication",
    )
    return {
        "kind": cast(Literal["appended", "widened", "already_held"], kind),
        "reservation_id": reservation_id,
        "coordination_run_id": coordination_run_id,
        "phase_start_head": phase_start_head,
        "marker_publication": marker_publication,
        "session_mapping_publication": session_mapping_publication,
    }


def classify_check(envelope: ValidatedEnvelope) -> dict[str, object]:
    """Classify edit authorization and refusal from the phase-20 check envelope."""

    if envelope.verb != "check":
        raise EnvelopeValidationError("check classification requires a check envelope")
    if envelope.status == "blocked_by_overlap":
        return cast(dict[str, object], cast(object, classify_refusal(envelope)))
    if envelope.status == "unconfigured":
        return {
            "kind": "unconfigured",
            "diagnostic": envelope.value["message"],
            "remedy": "cargo-berth init",
        }
    if envelope.status == "contention":
        return {
            "kind": "busy",
            "diagnostic": envelope.value["message"],
            "instruction": "the ledger is busy, try again",
        }
    if envelope.status == "ledger_unreadable":
        return {"kind": "ledger_unreadable", "diagnostic": envelope.value["message"]}
    if envelope.status == "invalid_input":
        return {"kind": "invalid_input", "diagnostic": envelope.value["message"]}
    if envelope.status != "clear" or envelope.exit_code != 0:
        raise EnvelopeValidationError(
            f"check returned unexpected status {envelope.status!r}"
        )
    if envelope.payload["kind"] != "check" or "data" not in envelope.payload:
        raise EnvelopeValidationError("clear check is missing its check payload")
    data = _require_object(envelope.payload["data"], "envelope.payload.data")
    state = _require_string(
        _required_field(data, "status", "envelope.payload.data"),
        "envelope.payload.data.status",
    )
    if state != "clear":
        raise EnvelopeValidationError("clear check payload status must be clear")
    scopes = _claim_scope_set(
        _required_field(data, "scopes", "envelope.payload.data"),
        "envelope.payload.data.scopes",
    )
    acquisition = _first_touch_acquisition(
        _required_field(data, "acquisition", "envelope.payload.data"),
        "envelope.payload.data.acquisition",
    )
    if envelope.value["reservations"] != [acquisition["reservation_id"]]:
        raise EnvelopeValidationError(
            "a clear check must report exactly its acquired reservation"
        )
    if envelope.value["blocked_by"]:
        raise EnvelopeValidationError("a clear check must not report blockers")
    session_mapping = acquisition["session_mapping_publication"]
    mapping_is_available = session_mapping["status"] == "published"
    rendered_markdown = (
        "".join(
            (
                f"Edit authorized by reservation `{acquisition['reservation_id']}` via a ",
                f"`{acquisition['kind']}` first-touch acquisition.",
            )
        )
        if mapping_is_available
        else "".join(
            (
                f"Edit authorized by reservation `{acquisition['reservation_id']}` via a ",
                f"`{acquisition['kind']}` first-touch acquisition with nonblocking degraded ",
                "success. The journal and reservation are durable, but the disposable ",
                f"session mapping is unavailable: {session_mapping['diagnostic']}. Continue, ",
                f"name reservation `{acquisition['reservation_id']}` explicitly from now on, ",
                "and expect later edits to fall back to `CARGO_BERTH_RUN` or the worktree marker.",
            )
        )
    )
    result: ClearCheckStateValue = {
        "kind": "edit_authorized",
        "acquisition": acquisition,
        "scopes": scopes,
        "outcome": (
            "success" if mapping_is_available else "nonblocking_degraded_success"
        ),
        "rendered_markdown": rendered_markdown,
    }
    return cast(dict[str, object], cast(object, result))


def _validate_board(envelope: ValidatedEnvelope) -> dict[str, object]:
    if envelope.status == "unconfigured":
        return {
            "kind": "unconfigured",
            "diagnostic": envelope.value["message"],
            "remedy": "cargo-berth init",
        }
    if envelope.status == "ledger_unreadable":
        return {
            "kind": "ledger_unreadable",
            "diagnostic": envelope.value["message"],
            "remedy": "repair the ledger",
        }
    if envelope.status == "contention":
        return {
            "kind": "busy",
            "diagnostic": envelope.value["message"],
            "instruction": "the ledger is busy, try again",
            "command_to_rerun": "board --json",
        }
    if envelope.status != "board_ready" or envelope.exit_code != 0:
        raise EnvelopeValidationError(
            f"board --json returned unexpected status {envelope.status!r}"
        )
    if envelope.payload["kind"] != "board":
        raise EnvelopeValidationError("board_ready requires a board payload")
    if "data" not in envelope.payload:
        raise EnvelopeValidationError("board payload is missing data")
    data = _require_object(envelope.payload["data"], "envelope.payload.data")
    for field in BOARD_FIELDS:
        _ = _required_field(data, field, "envelope.payload.data")
    position = _require_object(data["journal_position"], "board.journal_position")
    _ = _require_integer(
        _required_field(position, "generation", "board.journal_position"),
        "board.journal_position.generation",
    )
    _ = _require_integer(
        _required_field(position, "journal_byte_offset", "board.journal_position"),
        "board.journal_position.journal_byte_offset",
    )
    _ = _require_list(
        data["recovered_bypasses_this_invocation"],
        "board.recovered_bypasses_this_invocation",
    )
    order = _require_string(data["integration_order"], "board.integration_order")
    if order not in {"undeclared", "constraints_recorded"}:
        raise EnvelopeValidationError(f"unknown integration_order {order!r}")
    for section_name in BOARD_SECTIONS:
        section = _require_object(data[section_name], f"board.{section_name}")
        section_position = _require_object(
            _required_field(section, "journal_position", f"board.{section_name}"),
            f"board.{section_name}.journal_position",
        )
        if section_position != position:
            raise EnvelopeValidationError(
                f"board.{section_name}.journal_position disagrees with board.journal_position"
            )
        _ = _require_list(
            _required_field(section, "entries", f"board.{section_name}"),
            f"board.{section_name}.entries",
        )
    git_cost = _require_object(data["git_cost"], "board.git_cost")
    for field in BOARD_GIT_COST_FIELDS:
        _ = _require_integer(
            _required_field(git_cost, field, "board.git_cost"),
            f"board.git_cost.{field}",
        )
    return {"kind": "board_ready"}


def _json_pointer_segment(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _walk_json(value: object, path: str = "") -> Iterable[tuple[str, object]]:
    yield path or "/", value
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        for untyped_key, child in mapping.items():
            if not isinstance(untyped_key, str):
                raise EnvelopeValidationError("rendered JSON object keys must be strings")
            key = untyped_key
            child_path = f"{path}/{_json_pointer_segment(key)}"
            yield from _walk_json(child, child_path)
    elif isinstance(value, list):
        sequence = cast(list[object], value)
        for index, child in enumerate(sequence):
            yield from _walk_json(child, f"{path}/{index}")


def render_board(envelope: ValidatedEnvelope) -> dict[str, object]:
    """Render every board field while separately surfacing every named user action."""

    board_state = _validate_board(envelope)
    if board_state["kind"] != "board_ready":
        if board_state["kind"] == "busy":
            return {
                **board_state,
                "rendered_markdown": (
                    f"Board unavailable: {board_state['diagnostic']}\n\n"
                    "The ledger is busy; try again with `cargo-berth board --json`."
                ),
                "rendered_paths": [],
                "user_actions": [],
            }
        return {
            **board_state,
            "rendered_markdown": (
                f"Board unavailable: {board_state['diagnostic']}\n\n"
                + (
                    "Run `cargo-berth init` to enroll this repository."
                    if board_state["kind"] == "unconfigured"
                    else "Repair the ledger before relying on board facts."
                )
            ),
            "rendered_paths": [],
            "user_actions": [],
        }
    walked = list(_walk_json(envelope.value))
    rendered_paths = [path for path, _ in walked]
    lines = ["# cargo-berth board", "", "Every frozen field:"]
    for path, value in walked:
        if isinstance(value, dict):
            rendered = "{object}"
        elif isinstance(value, list):
            rendered = f"[array length={len(cast(list[object], value))}]"
        else:
            rendered = json.dumps(value, ensure_ascii=False)
        lines.append(f"- `{path}` = {rendered}")
    actions: list[dict[str, object]] = []
    for path, value in walked:
        key = path.rsplit("/", maxsplit=1)[-1].replace("~1", "/").replace("~0", "~")
        if key in USER_ACTION_KEYS:
            actions.append({"path": path, "value": value})
    lines.extend(["", "User actions named by the payload:"])
    if actions:
        for action in actions:
            lines.append(
                f"- `{action['path']}` = {json.dumps(action['value'], ensure_ascii=False, sort_keys=True)}"
            )
    else:
        lines.append("- None.")
    return {
        "kind": "board_ready",
        "rendered_markdown": "\n".join(lines),
        "rendered_paths": rendered_paths,
        "user_actions": actions,
    }


def _installed_binary() -> str:
    binary = shutil.which("cargo-berth")
    if not binary:
        raise CoordinatorError("cargo-berth is not installed")
    return binary


def _repository_root(invocation_directory: Path) -> Path:
    directory = invocation_directory.resolve()
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=directory,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise CoordinatorError(
            f"could not resolve a Git repository root from {directory}: {error}"
        ) from error
    root = completed.stdout.strip()
    if completed.returncode != 0 or not root:
        raise CoordinatorError(f"directory is not inside a Git repository: {directory}")
    return Path(root).resolve()


def _session_environment() -> dict[str, str]:
    environment = os.environ.copy()
    session_id = environment.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if not session_id:
        raise CoordinatorError(
            "CLAUDE_CODE_SESSION_ID is unavailable; no cargo-berth invocation was made"
        )
    environment["CARGO_BERTH_SESSION_ID"] = session_id
    return environment


def _invoke_engine(arguments: list[str], cwd: Path, expected_verb: str) -> EngineInvocation:
    binary = _installed_binary()
    argv = (binary, *arguments)
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=_session_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    envelope = parse_envelope(completed.stdout, completed.returncode, expected_verb)
    return EngineInvocation(
        argv=argv,
        process_exit=completed.returncode,
        standard_error=completed.stderr,
        envelope=envelope,
    )


def _claim_arguments(
    arguments: CoordinatorArguments,
    transition: ClaimTransition,
) -> list[str]:
    command = ["claim", *arguments.paths]
    if isinstance(transition, (AnsweredClaimTransition, ApprovedClaimTransition)):
        command.extend(
            [
                f"--{transition.answer}",
                transition.blocker,
                "--overlap-why",
                transition.reason,
            ]
        )
        if isinstance(transition, ApprovedClaimTransition):
            command.extend(["--proposal", transition.proposal])
    if arguments.why:
        command.extend(["--why", arguments.why])
    supplied_plan = bool(arguments.plan)
    supplied_phase = bool(arguments.phase)
    if supplied_plan != supplied_phase:
        raise CoordinatorError("--plan and --phase must be supplied together")
    if supplied_plan:
        command.extend(["--plan", arguments.plan, "--phase", arguments.phase])
    if arguments.run:
        command.extend(["--run", arguments.run])
    command.append("--json")
    return command


def _generic_state(envelope: ValidatedEnvelope) -> dict[str, object]:
    if envelope.verb == "check":
        return classify_check(envelope)
    if envelope.status == "unconfigured":
        return {
            "kind": "unconfigured",
            "diagnostic": envelope.value["message"],
            "remedy": "cargo-berth init",
        }
    if envelope.status == "contention":
        return {
            "kind": "busy",
            "diagnostic": envelope.value["message"],
            "instruction": "the ledger is busy, try again",
        }
    if envelope.status == "ledger_unreadable":
        return {"kind": "ledger_unreadable", "diagnostic": envelope.value["message"]}
    inactive = _inactive_identity_classification(envelope)
    if isinstance(inactive, InactiveIdentityRejection):
        return inactive.tagged(envelope.value["message"])
    return {
        "kind": "engine_response",
        "verb": envelope.verb,
        "status": envelope.status,
        "payload_kind": envelope.payload["kind"],
    }


def _inactive_identity_classification(
    envelope: ValidatedEnvelope,
) -> InactiveIdentityClassification:
    if envelope.verb not in {"sequence", "integrate"}:
        return NoInactiveIdentityRejection()
    if envelope.payload["kind"] != envelope.verb:
        return NoInactiveIdentityRejection()
    data_value = envelope.payload.get("data")
    if not isinstance(data_value, dict):
        return NoInactiveIdentityRejection()
    data = _require_object(data_value, "envelope.payload.data")
    if data.get("status") != "rejected":
        return NoInactiveIdentityRejection()
    reason_value = data.get("reason")
    if not isinstance(reason_value, dict):
        return NoInactiveIdentityRejection()
    reason = _require_object(
        cast(object, reason_value), "envelope.payload.data.reason"
    )
    kind_value = reason.get("kind")
    if not isinstance(kind_value, str):
        return NoInactiveIdentityRejection()
    try:
        kind = InactiveIdentityKind(kind_value)
    except ValueError:
        return NoInactiveIdentityRejection()
    coordination_run_id = _require_string(
        _required_field(
            reason,
            "coordination_run_id",
            "envelope.payload.data.reason",
        ),
        "envelope.payload.data.reason.coordination_run_id",
    )
    return InactiveIdentityRejection(kind, coordination_run_id)


class CoordinatorArguments(argparse.Namespace):
    """Fully typed command-line values for every coordinator operation."""

    operation: str = ""
    process_exit: int = 0
    expected_verb: str = ""
    cwd: str = ""
    engine_arguments: list[str] = []
    paths: list[str] = []
    plan: str = ""
    phase: str = ""
    why: str = ""
    run: str = ""
    answer: str = ""
    blocker: str = ""
    overlap_reason: str = ""
    proposal: str = ""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)

    parse = subparsers.add_parser("parse-envelope", help="validate JSON read from stdin")
    _ = parse.add_argument("--process-exit", type=int, required=True)
    _ = parse.add_argument(
        "--expected-verb", choices=sorted(KNOWN_VERBS), required=True
    )

    refusal = subparsers.add_parser(
        "render-refusal",
        help="validate and render one blocked check or claim envelope from stdin",
    )
    _ = refusal.add_argument("--process-exit", type=int, required=True)
    _ = refusal.add_argument(
        "--expected-verb", choices=("check", "claim"), required=True
    )

    invoke = subparsers.add_parser("invoke", help="run one JSON engine invocation")
    _ = invoke.add_argument("--cwd", default=os.getcwd())
    _ = invoke.add_argument(
        "--expected-verb", choices=sorted(KNOWN_VERBS), required=True
    )
    _ = invoke.add_argument("engine_arguments", nargs=argparse.REMAINDER)

    board = subparsers.add_parser("board", help="read and render board --json exactly once")
    _ = board.add_argument("--cwd", default=os.getcwd())

    claim = subparsers.add_parser("claim", help="run one explicit claim transition")
    _ = claim.add_argument("--cwd", default=os.getcwd())
    _ = claim.add_argument("paths", nargs="+")
    _ = claim.add_argument("--plan", default="")
    _ = claim.add_argument("--phase", default="")
    _ = claim.add_argument("--why", default="")
    _ = claim.add_argument("--run", default="")
    _ = claim.add_argument(
        "--answer", choices=("before", "after", "defer", "override"), default=""
    )
    _ = claim.add_argument("--blocker", default="")
    _ = claim.add_argument("--overlap-reason", default="")
    _ = claim.add_argument("--proposal", default="")
    return parser


def _claim_transition(arguments: CoordinatorArguments) -> ClaimTransition:
    supplied_answer = bool(arguments.answer)
    supplied_blocker = bool(arguments.blocker)
    supplied_reason = bool(arguments.overlap_reason.strip())
    supplied_proposal = bool(arguments.proposal)
    if supplied_answer != supplied_blocker or supplied_answer != supplied_reason:
        raise CoordinatorError(
            "--answer, --blocker, and a non-empty --overlap-reason must be supplied together"
        )
    if supplied_proposal and not supplied_answer:
        raise CoordinatorError("--proposal requires the same answer, blocker, and reason")
    if not supplied_answer:
        return NeutralClaimTransition()
    if supplied_proposal:
        return ApprovedClaimTransition(
            answer=arguments.answer,
            blocker=arguments.blocker,
            reason=arguments.overlap_reason,
            proposal=arguments.proposal,
        )
    return AnsweredClaimTransition(
        answer=arguments.answer,
        blocker=arguments.blocker,
        reason=arguments.overlap_reason,
    )


def main(argv: list[str]) -> int:
    arguments = _build_parser().parse_args(argv, namespace=CoordinatorArguments())
    try:
        if arguments.operation == "parse-envelope":
            serialized = sys.stdin.read()
            envelope = parse_envelope(
                serialized, arguments.process_exit, arguments.expected_verb
            )
            _emit(
                {
                    "contract": CONTRACT,
                    "operation": "parse_envelope",
                    "outcome": {"kind": "valid"},
                    "envelope": envelope.value,
                }
            )
            return 0

        if arguments.operation == "render-refusal":
            serialized = sys.stdin.read()
            envelope = parse_envelope(
                serialized, arguments.process_exit, arguments.expected_verb
            )
            refusal = classify_refusal(envelope)
            _emit(
                {
                    "contract": CONTRACT,
                    "operation": "render_refusal",
                    "state": cast(dict[str, object], cast(object, refusal)),
                    "envelope": envelope.value,
                }
            )
            return 0

        repository_root = _repository_root(Path(arguments.cwd))

        if arguments.operation == "invoke":
            engine_arguments = list(arguments.engine_arguments)
            if engine_arguments and engine_arguments[0] == "--":
                _ = engine_arguments.pop(0)
            if not engine_arguments or engine_arguments[0] != arguments.expected_verb:
                raise CoordinatorError("engine arguments must begin with --expected-verb")
            if "--json" not in engine_arguments:
                raise CoordinatorError("every /sync engine invocation must request --json")
            if arguments.expected_verb == "board" and engine_arguments != ["board", "--json"]:
                raise CoordinatorError("board is invoked only as cargo-berth board --json")
            invocation = _invoke_engine(
                engine_arguments, repository_root, arguments.expected_verb
            )
            state = _generic_state(invocation.envelope)
            if state["kind"] == "busy":
                state["command_to_rerun"] = shlex.join(engine_arguments)
            _emit(
                {
                    "contract": CONTRACT,
                    "operation": "invoke",
                    "invocation": invocation.tagged(),
                    "state": state,
                    "envelope": invocation.envelope.value,
                }
            )
            return invocation.process_exit

        if arguments.operation == "board":
            invocation = _invoke_engine(
                ["board", "--json"], repository_root, "board"
            )
            rendering = render_board(invocation.envelope)
            _emit(
                {
                    "contract": CONTRACT,
                    "operation": "board",
                    "invocation": invocation.tagged(),
                    "state": rendering,
                    "envelope": invocation.envelope.value,
                }
            )
            return invocation.process_exit

        transition = _claim_transition(arguments)
        command = _claim_arguments(arguments, transition)
        invocation = _invoke_engine(command, repository_root, "claim")
        state = classify_claim(invocation.envelope)
        if state["kind"] == "busy":
            state["command_to_rerun"] = shlex.join(command)
        _emit(
            {
                "contract": CONTRACT,
                "operation": "claim",
                "invocation": invocation.tagged(),
                "state": state,
                "envelope": invocation.envelope.value,
            }
        )
        return invocation.process_exit
    except (CoordinatorError, EnvelopeValidationError) as error:
        print(f"coordinator refused input: {error}", file=sys.stderr)
        return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
