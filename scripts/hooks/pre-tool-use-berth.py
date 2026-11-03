#!/usr/bin/env python3
"""Check a unit's Codex worker edits against cargo-berth before `apply_patch` writes them.

Claude Code edits reach `cargo-berth hook pre-tool-use` through `berth_pre_edit.sh`; a Codex
worker's `apply_patch` had no berth check at all, so its first touch never turned the phase's
`enrolled_awaiting_first_touch` state `Active`, and an `Active` record never followed a new
reservation of its phase. This hook runs for Codex `PreToolUse` on
`apply_patch`. It acts only for a worker of a running unit: the app-server a unit director starts
carries the director's `CLAUDE_CODE_SESSION_ID` and, when codex_mesh started it for a run, that run
as `UNIT_RUN_DIR`. The run named there is the session directory; when it is unset or
names a folder that is gone, the director's run marker names it. Any other Codex session is left
alone.

Workers persist across phases and the mesh knows no phase, so each edited path goes to the open
phase that owns it (`phase_reservation.attribute`): the only open phase, else the one whose
`files` list covers it. A path no single open phase owns refuses the edit. A recorder-open phase
with no state file and no berth mapping of its own was never enrolled (FinalGate's `final`) and owns
nothing; an unreadable recorder state leaves the phases whose directories hold a state file.

For a worker it runs `cargo-berth check <paths> --json` from the repository root once per owning
phase, as that phase's berth session `<director session id>.phase-<phase>`, so two open phases
hold separate reservations. A phase whose session berth's map already names, as a reservation
the board shows active, skips that check (`phase_reservation.phase_check`): its claim covered the
whole files list, and a check over many paths can outlast Codex's 30 s limit on this hook.
A path a sibling phase already holds is allowed and records nothing:
the engine leaves the session unmapped and names the sibling's reservation. A clear answer allows the edit and, when it carries the acquired reservation,
writes `Active` into that phase's state file. An unenrolled repository is allowed, and so is a
ledger the engine cannot read (`ledger_unreadable`, as in a scratch clone with no ledger): the
engine's own edit hook lets those edits through, because ledger loss fails open. Every other
answer, a missing engine or a timeout refuses the edit with the engine's reason: an unchecked
edit can collide with a foreign reservation, where a refused one only costs the worker a retry.

Codex accepts only a refusal from a PreToolUse hook: an allow is an exit 0 with no output.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "direct"))
import phase_reservation  # noqa: E402
import unit_runs  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.astra import design_roles  # noqa: E402


def _seat_session_dir() -> Path | None:
    """The unit's session directory when this Codex process is one of its workers.

    The worker's own run comes first: codex_mesh names it as UNIT_RUN_DIR in the
    environment of every app-server it starts for a run, and a director running phases side by
    side marks just one of their runs. The marker is read when that variable is unset or names a
    folder that is gone, so a stale variable never lets an edit through unchecked.
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not session_id:
        return None
    board = unit_runs.environ("RUN_DIR")
    if board and Path(board).is_dir():
        return Path(board)
    try:
        marker = unit_runs.marker(session_id, unit_runs.environ("ACTIVE_DIR") or None)
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
    held = os.environ.get("HELD_DESIGN_ID", "")
    if held:
        try:
            binding = design_roles.check_patch(held, int(os.environ.get("DESIGN_BINDING_GENERATION", "0")), Path(cwd), paths)
            session_dir = Path(binding.run_directory)
        except (OSError, ValueError) as error:
            return _refuse(str(error))
    if session_dir is None or not paths:
        return 0
    root = phase_reservation.repository_root(Path(cwd))
    scopes = phase_reservation.repository_paths(root, Path(cwd), paths) if root is not None else []
    if root is None or not scopes:
        return 0
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    try:
        phases = phase_reservation.open_phases(session_dir)
    except ValueError:
        # An unreadable recorder state gives no recorder list; check as before the recorder listed phases.
        phases = phase_reservation.held_phases(session_dir)
    phases = phase_reservation.enrolled_phases(phases, root, session_id)
    missing = phase_reservation.missing_record_phases(phases, scopes, root)
    if missing:
        commands = "; ".join(phase_reservation.repair_command(session_dir, phase.phase, root) for phase in missing)
        return _refuse(f"phase reservation record missing; restore it from berth's ledger: {commands}.")
    attribution = phase_reservation.attribute(phases, scopes, root)
    if isinstance(attribution, phase_reservation.Attribution) and attribution.refused:
        return _refuse(phase_reservation.refusal_reason(phases, attribution.refused))
    groups = list(attribution.by_phase.items()) if isinstance(attribution, phase_reservation.Attribution) else [(None, scopes)]
    for phase, phase_scopes in groups:
        answer = phase_reservation.phase_check(root, phase_scopes, session_id, phase)
        if answer.status in phase_reservation.FAILS_OPEN:
            return _refuse("binding reservation ledger is unreadable") if held else 0
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
