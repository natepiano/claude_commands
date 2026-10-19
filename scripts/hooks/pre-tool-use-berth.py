#!/usr/bin/env python3
"""Check a unit's Codex seat edits against cargo-berth before `apply_patch` writes them.

Claude Code edits reach `cargo-berth hook pre-tool-use` through `berth_pre_edit.sh`; a Codex
seat's `apply_patch` had no berth check at all, so its first touch never turned the phase's
`enrolled_awaiting_first_touch` state `Active`. This hook runs for Codex `PreToolUse` on
`apply_patch`. It acts only for a seat of a running unit: the app-server a unit director starts
carries the director's `CLAUDE_CODE_SESSION_ID` and, when codex_mesh started it for a run, that run
as `PLAN_DELEGATE_BOARD_DIR`. The run named there is the session directory; when it is unset or
names a folder that is gone, the director's run marker names it. Any other Codex session is left
alone.

Seats persist across phases and the mesh knows no phase, so each edited path goes to the open
phase that owns it (`phase_reservation.attribute`): the only open phase, else the one whose
`files` list covers it. A path no single open phase owns refuses the edit.

For a seat it runs `cargo-berth check <paths> --json` from the repository root once per owning
phase, as that phase's berth session `<director session id>.phase-<phase>`, so two open phases
hold separate reservations. A path a sibling phase already holds is allowed and records nothing:
the engine leaves the session unmapped and names the sibling's reservation. A clear answer allows the edit and, when it carries the acquired reservation,
writes `Active` into that phase's state file. An unenrolled repository is allowed. Every other
answer, a missing engine or a timeout refuses the edit with the engine's reason: an unchecked
edit can collide with a foreign reservation, where a refused one only costs the seat a retry.

Codex accepts only a refusal from a PreToolUse hook: an allow is an exit 0 with no output.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "delegate"))
import phase_reservation  # noqa: E402
import unit_runs  # noqa: E402


def _seat_session_dir() -> Path | None:
    """The unit's session directory when this Codex process is one of its seats.

    The seat's own run comes first: codex_mesh names it as PLAN_DELEGATE_BOARD_DIR in the
    environment of every app-server it starts for a run, and a director running phases side by
    side marks just one of their runs. The marker is read when that variable is unset or names a
    folder that is gone, so a stale variable never lets an edit through unchecked.
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not session_id:
        return None
    board = os.environ.get("PLAN_DELEGATE_BOARD_DIR", "")
    if board and Path(board).is_dir():
        return Path(board)
    try:
        marker = unit_runs.marker(session_id, os.environ.get("PLAN_DELEGATE_ACTIVE_DIR"))
        lines = marker.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    run = Path(lines[0].strip()) if lines and lines[0].strip() else None
    return run if run is not None and run.is_dir() else None


def _refuse(reason: str) -> int:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": f"cargo-berth refused this edit: {reason} Do not retry around it; "
                                    + "tell your unit director, who answers berth overlaps.",
    }}))
    return 0


def decide(raw: str) -> int:
    try:
        payload = cast(object, json.loads(raw))
    except ValueError:
        return 0
    fields = cast(dict[str, object], payload) if isinstance(payload, dict) else {}
    tool_input = fields.get("tool_input")
    patch = cast(dict[str, object], tool_input).get("command") if isinstance(tool_input, dict) else None
    cwd = fields.get("cwd")
    if fields.get("tool_name") != "apply_patch" or not isinstance(patch, str) or not isinstance(cwd, str):
        return 0
    session_dir = _seat_session_dir()
    paths = phase_reservation.patch_paths(patch)
    if session_dir is None or not paths:
        return 0
    root = phase_reservation.repository_root(Path(cwd))
    scopes = phase_reservation.repository_paths(root, Path(cwd), paths) if root is not None else []
    if root is None or not scopes:
        return 0
    phases = phase_reservation.open_phases(session_dir)
    attribution = phase_reservation.attribute(phases, scopes) if phases else None
    if attribution is not None and attribution.refused:
        return _refuse(phase_reservation.refusal_reason(phases, attribution.refused))
    groups = list(attribution.by_phase.items()) if attribution is not None else [(None, scopes)]
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    for phase, phase_scopes in groups:
        berth_session = phase_reservation.phase_session_id(session_id, phase.phase) if phase else session_id
        answer = phase_reservation.check(root, phase_scopes, berth_session)
        if answer.status == phase_reservation.UNCONFIGURED:
            return 0
        if answer.status != phase_reservation.CLEAR:
            return _refuse(f"{answer.status}: {answer.message or 'no detail'}.")
        acquisition = phase_reservation.own_acquisition(answer, root, phases, session_id)
        if phase is not None and acquisition is not None:
            try:
                _ = phase_reservation.record_active(phase.directory, acquisition)
            except OSError:
                # The edit is protected either way; the checkpoint's backstop records Active.
                pass
    return 0


def main() -> int:
    return decide(sys.stdin.read())


if __name__ == "__main__":
    raise SystemExit(main())
