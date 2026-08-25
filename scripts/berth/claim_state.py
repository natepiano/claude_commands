#!/usr/bin/env python3
"""Validate cargo-berth envelopes and coordinate one claim transition at a time."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    from berth.work_order import CONTRACT as WORK_ORDER_CONTRACT
    from berth.work_order import WorkOrderValidationError
except ModuleNotFoundError:  # Supports direct execution as well as ``python -m``.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from berth.work_order import CONTRACT as WORK_ORDER_CONTRACT
    from berth.work_order import WorkOrderValidationError


CONTRACT = "cargo-berth-claim-state/v1"
EXPECTED_DIGEST = "efceb301d2c4f61ee2a835694b5d6563bcfcfa01446b292f90119cdc85be19d9"
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
KNOWN_STATUSES = {
    "unimplemented",
    "board_ready",
    "initialized",
    "projection_repaired",
    "reinitialized",
    "ledger_unreadable",
    "unconfigured",
    "terminal_view_failed",
    "clear",
    "claimed",
    "widened",
    "incursion",
    "drift_collision",
    "drift_attribution_required",
    "reservation_limit_reached",
    "ordering_edge_limit_reached",
    "blocked_by_overlap",
    "blocked_by_ordering",
    "needs_user_authorization",
    "invalid_input",
    "contention",
    "sequenced",
    "duplicate_ordering_edge",
    "ordering_cycle",
    "missing_deferral",
    "outstanding",
    "integrated",
    "trunk_rewritten",
    "object_unknown",
    "released",
    "recovered",
    "renewed",
    "incursion_resolved",
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
INACTIVE_SESSION_DIAGNOSTIC = re.compile(
    r"^harness session mapping for coordination run (?P<run>[0-9a-f-]+) "
    r"no longer names an active reservation(?: in this worktree)?$"
)
INACTIVE_MARKER_DIAGNOSTIC = re.compile(
    r"^coordination-run marker (?P<run>[0-9a-f-]+) "
    r"no longer has an active reservation(?: in this worktree)?$"
)


class EnvelopeValidationError(Exception):
    """Process output did not satisfy the frozen envelope contract."""


class CoordinatorError(Exception):
    """The coordinator could not safely make an engine invocation."""


@dataclass(frozen=True)
class ValidatedEnvelope:
    """A frozen response whose process status and tagged payload agree."""

    value: dict[str, Any]

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
    def payload(self) -> dict[str, Any]:
        return self.value["payload"]


@dataclass(frozen=True)
class EngineInvocation:
    """One completed engine process; there is deliberately no retry state."""

    argv: tuple[str, ...]
    process_exit: int
    standard_error: str
    envelope: ValidatedEnvelope

    def tagged(self) -> dict[str, Any]:
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


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _require_object(value: Any, path: str, *, nonempty: bool = True) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EnvelopeValidationError(f"{path} must be an object")
    if nonempty and not value:
        raise EnvelopeValidationError(f"{path} must not be an empty object")
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise EnvelopeValidationError(f"{path} must be an array")
    return value


def _require_string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise EnvelopeValidationError(f"{path} must be a string")
    if not value:
        raise EnvelopeValidationError(f"{path} must not be an empty string")
    return value


def _require_integer(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EnvelopeValidationError(f"{path} must be an integer")
    return value


def _required_field(value: dict[str, Any], key: str, path: str) -> Any:
    if key not in value:
        raise EnvelopeValidationError(f"{path}.{key} is missing")
    if value[key] is None:
        raise EnvelopeValidationError(f"{path}.{key} must not be null")
    return value[key]


def _validate_identifier_array(value: Any, path: str) -> list[Any]:
    identifiers = _require_list(value, path)
    for index, identifier in enumerate(identifiers):
        _require_string(identifier, f"{path}[{index}]")
    return identifiers


def parse_envelope(
    serialized: str, process_exit: int, expected_verb: str
) -> ValidatedEnvelope:
    """Parse untrusted process output and validate every frozen envelope field."""

    try:
        decoded = json.loads(serialized)
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
    reservations = _required_field(envelope, "reservations", "envelope")
    blocked_by = _required_field(envelope, "blocked_by", "envelope")
    message = _required_field(envelope, "message", "envelope")
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
    _validate_identifier_array(reservations, "envelope.reservations")
    _validate_identifier_array(blocked_by, "envelope.blocked_by")
    _require_string(message, "envelope.message")

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
    _require_list(
        _required_field(payload, "alerts", "envelope.payload"),
        "envelope.payload.alerts",
    )
    if kind == "no_facts":
        if "data" in payload:
            raise EnvelopeValidationError("no_facts payload must not contain data")
    else:
        _require_object(
            _required_field(payload, "data", "envelope.payload"),
            "envelope.payload.data",
        )
    return ValidatedEnvelope(envelope)


def _publication(value: Any, path: str) -> dict[str, Any]:
    publication = _require_object(value, path)
    status = _require_string(_required_field(publication, "status", path), f"{path}.status")
    if status == "published":
        return publication
    if status == "unavailable":
        _require_string(
            _required_field(publication, "diagnostic", path), f"{path}.diagnostic"
        )
        return publication
    raise EnvelopeValidationError(f"unknown publication status {status!r} at {path}")


def _claim_scope_set(value: Any, path: str) -> list[Any]:
    scopes = _require_list(value, path)
    if not scopes:
        raise EnvelopeValidationError(f"{path} must contain at least one scope")
    for index, value_scope in enumerate(scopes):
        scope = _require_object(value_scope, f"{path}[{index}]")
        _require_string(
            _required_field(scope, "path", f"{path}[{index}]"), f"{path}[{index}].path"
        )
        kind = _require_string(
            _required_field(scope, "kind", f"{path}[{index}]"), f"{path}[{index}].kind"
        )
        if kind not in {"file", "tree"}:
            raise EnvelopeValidationError(f"unknown scope kind {kind!r} at {path}[{index}]")
    return scopes


def _claim_source(value: Any, path: str) -> dict[str, Any]:
    source = _require_object(value, path)
    kind = _require_string(_required_field(source, "kind", path), f"{path}.kind")
    if kind == "explicit":
        return source
    if kind == "work_plan":
        _require_string(_required_field(source, "plan", path), f"{path}.plan")
        _require_string(_required_field(source, "phase", path), f"{path}.phase")
        return source
    raise EnvelopeValidationError(f"unknown claim source kind {kind!r} at {path}")


def _claim_conflicts(value: Any, path: str) -> list[Any]:
    conflicts = _require_list(value, path)
    if not conflicts:
        raise EnvelopeValidationError(f"{path} must contain every current holder")
    for index, value_conflict in enumerate(conflicts):
        conflict_path = f"{path}[{index}]"
        conflict = _require_object(value_conflict, conflict_path)
        _require_string(
            _required_field(conflict, "reservation_id", conflict_path),
            f"{conflict_path}.reservation_id",
        )
        _require_string(
            _required_field(conflict, "holder_run_id", conflict_path),
            f"{conflict_path}.holder_run_id",
        )
        _claim_source(_required_field(conflict, "source", conflict_path), f"{conflict_path}.source")
        _require_object(
            _required_field(conflict, "purpose", conflict_path), f"{conflict_path}.purpose"
        )
        _claim_scope_set(
            _required_field(conflict, "overlapping_scopes", conflict_path),
            f"{conflict_path}.overlapping_scopes",
        )
    return conflicts


def _claim_answer(value: Any, path: str) -> dict[str, Any]:
    answer = _require_object(value, path)
    kind = _require_string(_required_field(answer, "kind", path), f"{path}.kind")
    if kind not in {"sequence", "defer", "override"}:
        raise EnvelopeValidationError(f"unknown overlap answer kind {kind!r}")
    _require_string(_required_field(answer, "blocker", path), f"{path}.blocker")
    if kind == "sequence":
        direction = _require_string(
            _required_field(answer, "direction", path), f"{path}.direction"
        )
        if direction not in {"requester_before_holder", "holder_before_requester"}:
            raise EnvelopeValidationError(f"unknown sequence direction {direction!r}")
    return answer


def classify_claim(envelope: ValidatedEnvelope) -> dict[str, Any]:
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
        inactive = _inactive_identity_state(envelope.value["message"])
        if inactive["kind"] != "not_an_inactive_identity":
            return inactive
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
    data = _require_object(envelope.payload["data"], "envelope.payload.data")
    state = _require_string(
        _required_field(data, "status", "envelope.payload.data"),
        "envelope.payload.data.status",
    )
    if state == "blocked":
        if envelope.exit_code != 1 or envelope.status != "blocked_by_overlap":
            raise EnvelopeValidationError("blocked claim tags do not agree with exit 1")
        conflicts = _claim_conflicts(
            _required_field(data, "conflicts", "envelope.payload.data"),
            "envelope.payload.data.conflicts",
        )
        return {"kind": "blocked", "current_holders": conflicts}
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
        return {
            "kind": "proposal_awaiting_approval",
            "current_holders": conflicts,
            "answer": answer,
            "authorization_reason": reason,
            "consequence": consequence,
            "proposal": proposal,
            "proposal_token": token,
        }
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
        result: dict[str, Any] = {
            "kind": "claimed",
            "reservation_id": reservation_id,
            "coordination_run_id": coordination_run_id,
            "scopes": scopes,
            "marker_publication": marker,
            "session_mapping_publication": session_mapping,
            "session_mapping_guarantee": (
                "durable_claim_with_mapping"
                if session_mapping["status"] == "published"
                else "durable_claim_mapping_unavailable"
            ),
        }
        return result
    if state in {"reservation_limit_reached", "ordering_edge_limit_reached"}:
        _require_integer(
            _required_field(data, "maximum", "envelope.payload.data"),
            "envelope.payload.data.maximum",
        )
        return {"kind": state, "facts": data}
    raise EnvelopeValidationError(f"unknown claim payload status {state!r}")


def _validate_board(envelope: ValidatedEnvelope) -> dict[str, Any]:
    if envelope.status in {"unconfigured", "ledger_unreadable", "contention"}:
        return {
            "kind": envelope.status,
            "diagnostic": envelope.value["message"],
            "remedy": "cargo-berth init" if envelope.status == "unconfigured" else "repair the ledger",
        }
    if envelope.status != "board_ready" or envelope.exit_code != 0:
        raise EnvelopeValidationError(
            f"board --json returned unexpected status {envelope.status!r}"
        )
    if envelope.payload["kind"] != "board":
        raise EnvelopeValidationError("board_ready requires a board payload")
    data = _require_object(envelope.payload["data"], "envelope.payload.data")
    for field in BOARD_FIELDS:
        _required_field(data, field, "envelope.payload.data")
    position = _require_object(data["journal_position"], "board.journal_position")
    _require_integer(
        _required_field(position, "generation", "board.journal_position"),
        "board.journal_position.generation",
    )
    _require_integer(
        _required_field(position, "journal_byte_offset", "board.journal_position"),
        "board.journal_position.journal_byte_offset",
    )
    _require_list(data["recovered_bypasses_this_invocation"], "board.recovered_bypasses_this_invocation")
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
        _require_list(
            _required_field(section, "entries", f"board.{section_name}"),
            f"board.{section_name}.entries",
        )
    git_cost = _require_object(data["git_cost"], "board.git_cost")
    for field in BOARD_GIT_COST_FIELDS:
        _require_integer(_required_field(git_cost, field, "board.git_cost"), f"board.git_cost.{field}")
    return {"kind": "board_ready"}


def _json_pointer_segment(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _walk_json(value: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    yield path or "/", value
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}/{_json_pointer_segment(key)}"
            yield from _walk_json(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_json(child, f"{path}/{index}")


def render_board(envelope: ValidatedEnvelope) -> dict[str, Any]:
    """Render every board field while separately surfacing every named user action."""

    board_state = _validate_board(envelope)
    if board_state["kind"] != "board_ready":
        return {
            "kind": board_state["kind"],
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
            rendered = f"[array length={len(value)}]"
        else:
            rendered = json.dumps(value, ensure_ascii=False)
        lines.append(f"- `{path}` = {rendered}")
    actions: list[dict[str, Any]] = []
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
    digest = hashlib.sha256(Path(binary).read_bytes()).hexdigest()
    if digest != EXPECTED_DIGEST:
        raise CoordinatorError(
            f"installed cargo-berth digest {digest} does not match required {EXPECTED_DIGEST}"
        )
    return binary


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


def _resolve_work_order(
    document: str, phase: str, coverage: str, repository_root: Path
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "berth.work_order",
        "--repository-root",
        str(repository_root),
        "resolve",
        "--document",
        document,
        "--phase",
        phase,
        "--coverage",
        coverage,
    ]
    environment = os.environ.copy()
    scripts_root = str(Path(__file__).resolve().parents[1])
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (scripts_root, environment.get("PYTHONPATH", "")) if part
    )
    completed = subprocess.run(
        command,
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise CoordinatorError(f"work-order resolver emitted malformed JSON: {error}") from error
    if completed.returncode != 0:
        raise CoordinatorError(
            "work-order resolution failed: "
            + json.dumps(result.get("outcome", result), sort_keys=True)
        )
    if result.get("contract") != WORK_ORDER_CONTRACT:
        raise CoordinatorError("work-order resolver returned an unknown contract")
    resolved = result.get("resolved_claim")
    if not isinstance(resolved, dict) or resolved.get("kind") != "declared_with_plan_scopes":
        raise CoordinatorError("work-order resolver returned no declared claim footprint")
    path_arguments = resolved.get("arguments")
    if not isinstance(path_arguments, list) or not path_arguments or not all(
        isinstance(value, str) and value for value in path_arguments
    ):
        raise CoordinatorError("work-order resolver returned invalid claim arguments")
    return result


def _claim_arguments(
    arguments: argparse.Namespace,
    resolved: dict[str, Any],
    transition: ClaimTransition,
) -> list[str]:
    path_arguments = resolved["resolved_claim"]["arguments"]
    command = ["claim", *path_arguments]
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
    command.extend(
        [
            "--why",
            arguments.why or f"Work Order Phase {resolved['phase']} in {resolved['document']}",
            "--plan",
            resolved["document"],
            "--phase",
            resolved["phase"],
        ]
    )
    if arguments.run:
        command.extend(["--run", arguments.run])
    command.append("--json")
    return command


def _generic_state(envelope: ValidatedEnvelope) -> dict[str, Any]:
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
    if envelope.verb == "sequence" and envelope.payload["kind"] == "sequence":
        data = envelope.payload["data"]
        if (
            isinstance(data, dict)
            and data.get("status") == "rejected"
            and isinstance(data.get("reason"), dict)
            and data["reason"].get("kind") == "inactive_session_mapping"
        ):
            return {
                "kind": "inactive_session_mapping",
                "coordination_run_id": data["reason"].get("coordination_run_id"),
                "diagnostic": envelope.value["message"],
                "recovery": "restart the coordination run or name the active reservation explicitly",
            }
    if envelope.status == "invalid_input":
        inactive = _inactive_identity_state(envelope.value["message"])
        if inactive["kind"] != "not_an_inactive_identity":
            return inactive
    return {
        "kind": "engine_response",
        "verb": envelope.verb,
        "status": envelope.status,
        "payload_kind": envelope.payload["kind"],
    }


def _inactive_identity_state(diagnostic: str) -> dict[str, Any]:
    session_match = INACTIVE_SESSION_DIAGNOSTIC.fullmatch(diagnostic)
    if session_match:
        return {
            "kind": "inactive_session_mapping",
            "coordination_run_id": session_match.group("run"),
            "diagnostic": diagnostic,
            "recovery": "restart the coordination run or name the active reservation explicitly",
        }
    marker_match = INACTIVE_MARKER_DIAGNOSTIC.fullmatch(diagnostic)
    if marker_match:
        return {
            "kind": "inactive_marker_run",
            "coordination_run_id": marker_match.group("run"),
            "diagnostic": diagnostic,
            "recovery": "remove or replace the stale worktree coordination marker",
        }
    return {"kind": "not_an_inactive_identity"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)

    parse = subparsers.add_parser("parse-envelope", help="validate JSON read from stdin")
    parse.add_argument("--process-exit", type=int, required=True)
    parse.add_argument("--expected-verb", choices=sorted(KNOWN_VERBS), required=True)

    invoke = subparsers.add_parser("invoke", help="run one JSON engine invocation")
    invoke.add_argument("--cwd", default=os.getcwd())
    invoke.add_argument("--expected-verb", choices=sorted(KNOWN_VERBS), required=True)
    invoke.add_argument("engine_arguments", nargs=argparse.REMAINDER)

    board = subparsers.add_parser("board", help="read and render board --json exactly once")
    board.add_argument("--cwd", default=os.getcwd())

    claim = subparsers.add_parser("claim", help="run one Work-Order claim transition")
    claim.add_argument("--cwd", default=os.getcwd())
    claim.add_argument("--document", required=True)
    claim.add_argument("--phase", required=True)
    claim.add_argument("--coverage", choices=("advisory", "required"), required=True)
    claim.add_argument("--why", default="")
    claim.add_argument("--run", default="")
    claim.add_argument("--answer", choices=("before", "after", "defer", "override"), default="")
    claim.add_argument("--blocker", default="")
    claim.add_argument("--overlap-reason", default="")
    claim.add_argument("--proposal", default="")
    return parser


def _claim_transition(arguments: argparse.Namespace) -> ClaimTransition:
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
    arguments = _build_parser().parse_args(argv)
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

        if arguments.operation == "invoke":
            engine_arguments = list(arguments.engine_arguments)
            if engine_arguments and engine_arguments[0] == "--":
                engine_arguments.pop(0)
            if not engine_arguments or engine_arguments[0] != arguments.expected_verb:
                raise CoordinatorError("engine arguments must begin with --expected-verb")
            if "--json" not in engine_arguments:
                raise CoordinatorError("every /sync engine invocation must request --json")
            if arguments.expected_verb == "board" and engine_arguments != ["board", "--json"]:
                raise CoordinatorError("board is invoked only as cargo-berth board --json")
            invocation = _invoke_engine(
                engine_arguments, Path(arguments.cwd).resolve(), arguments.expected_verb
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
                ["board", "--json"], Path(arguments.cwd).resolve(), "board"
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
        repository_root = Path(arguments.cwd).resolve()
        resolved = _resolve_work_order(
            arguments.document, arguments.phase, arguments.coverage, repository_root
        )
        command = _claim_arguments(arguments, resolved, transition)
        invocation = _invoke_engine(command, repository_root, "claim")
        state = classify_claim(invocation.envelope)
        if state["kind"] == "busy":
            state["command_to_rerun"] = shlex.join(command)
        _emit(
            {
                "contract": CONTRACT,
                "operation": "claim",
                "work_order_resolution": resolved,
                "invocation": invocation.tagged(),
                "state": state,
                "envelope": invocation.envelope.value,
            }
        )
        return invocation.process_exit
    except (CoordinatorError, EnvelopeValidationError, WorkOrderValidationError) as error:
        print(f"coordinator refused input: {error}", file=sys.stderr)
        return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
