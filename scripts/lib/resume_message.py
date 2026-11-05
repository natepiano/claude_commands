"""Build resume messages and commands for existing worker threads."""
from __future__ import annotations

import json
import shlex
from datetime import datetime, timezone
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple, TypedDict, cast

from scripts.lib.script_envelope import script_prompt
from scripts.usage import agents_log

SCRIPTS = Path(__file__).resolve().parents[1]


class PausedSeat(TypedDict):
    """A worker reference and the launcher that continues its task."""

    run_dir: str
    seat: str
    role: str
    staff_id: str
    turn_live: bool
    launcher_command: list[str]


class Resumable(NamedTuple):
    seat: PausedSeat


class Idle(NamedTuple):
    seat: PausedSeat


class Unavailable(NamedTuple):
    staff_id: str
    name: str
    reason: str


WorkerResume = Resumable | Idle | Unavailable


class ResumeWorkers(NamedTuple):
    results: Sequence[WorkerResume]
    cwd: str


NO_WORKERS = ResumeWorkers((), "")


def resume_message(why: str, *, moved_from: str | None = None, moved_to: str | None = None,
                   workers: ResumeWorkers = NO_WORKERS, context: str = "") -> str:
    lines: list[str] = []
    if why or context:
        move = f" You were moved from {moved_from} to {moved_to}." if moved_to else ""
        opening = context or f"You were stopped and resumed because {why}.{move} Continue where you stopped."
        lines.append(opening.rstrip() + " First run `~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py pending` "
                     + "for messages kept while you were down, then continue from there; if you were waiting on the user, keep waiting.")
    if workers.results:
        lines.append("Resume your workers; do not open new ones.")
        for result in workers.results:
            match result:
                case Unavailable(staff_id, name, reason):
                    lines.append(f"anomaly: {name} ({staff_id}): {reason}")
                case Resumable(seat):
                    lines.append(f"{seat['seat']} ({seat['role']}, was working): {resume_worker_command(seat, workers.cwd)}")
                case Idle(seat):
                    lines.append(f"{seat['seat']} ({seat['role']}, idle): paused on its thread; nothing to run now. "
                                 + f"Your next dispatch with --to {seat['seat']} resumes it.")
        lines.append("Each worker continues its own thread; after resuming, send later work with --to as usual.")
    elif why:
        lines.append("Codex workers keep their existing threads. Use implement.sh --to <worker> or "
                     + "codex_mesh.py resume for a paused worker; do not open a replacement thread.")
    text = ("\n" if workers.results else " ").join(lines)
    return script_prompt(text)


def _resume_phase_id(seat: PausedSeat) -> str:
    """Use the phase recorded for this worker; a slot can belong to another worker."""
    try:
        raw = cast(object, json.loads((Path(seat["run_dir"]) / "progress_history_state.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return ""
    if not isinstance(raw, dict):
        return ""
    passes = cast(dict[str, object], raw).get("pass")
    if not isinstance(passes, dict):
        return ""
    recorded_passes = cast(dict[str, object], passes)
    # Older state holds one pass directly; newer state holds a pass per slot.
    candidates = [recorded_passes] if "instance_id" in recorded_passes else recorded_passes.values()
    phase_ids: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        record = cast(dict[str, object], candidate)
        if record.get("worker_name") != seat["seat"]:
            continue
        phase = record.get("phase")
        if not isinstance(phase, dict):
            return ""
        phase_id = cast(dict[str, object], phase).get("id")
        if not isinstance(phase_id, str) or not phase_id.strip():
            return ""
        phase_ids.add(phase_id)
    return next(iter(phase_ids)) if len(phase_ids) == 1 else ""


def resume_worker_command(seat: PausedSeat, cwd: str) -> str:
    """Build a complete launcher invocation that continues this worker's thread."""
    from scripts.direct.worker_state import worker_slot
    from scripts.production import session_staff

    launcher = "review.sh" if seat["role"] == "review" else "implement.sh"
    prompt = str(Path(seat["run_dir"]) / f"{seat['seat']}.resume-prompt.md")
    if seat["turn_live"] and seat["launcher_command"]:
        try:
            return _resume_task(seat, cwd, prompt)
        except ValueError:
            pass
    # Find the saved slot through staff_id, including identities from named fix workers.
    recorded_slot = worker_slot(Path(seat["run_dir"]), seat["seat"]) or ""
    team_role = recorded_slot or seat["role"]
    phase_id = _resume_phase_id(seat)
    # The launchers are not executable files: `bash` runs them as pasted.
    start = ["bash", str(SCRIPTS / "direct" / launcher), "--to", seat["seat"]]
    if phase_id:
        start.extend(("--phase", phase_id))
    if launcher == "review.sh":
        assignment = agents_log.role_at(agents_log.assignments(agents_log.state_dir() / "agents.jsonl"),
                                       session_staff.worker_session(seat), datetime.now(timezone.utc))
        if isinstance(assignment, agents_log.Assignment) and assignment.role == "review" and assignment.lens:
            start.extend(("--lens", assignment.lens))
    kind = team_role if team_role in {"impl", "test", "fix"} else "impl"
    if seat["role"] == "fix":
        kind = "fix"
    if launcher == "implement.sh" and kind == "fix":
        start.extend(("--fix-cause", "resume after a move"))
    start.extend((seat["run_dir"], cwd, prompt))
    if launcher == "review.sh":
        return shlex.join([*start, "review", "resume after a move", "resuming after a move"])
    return shlex.join([*start, kind, "resume after a move", kind, "resuming after a move", "0", team_role])


def _resume_task(seat: PausedSeat, cwd: str, prompt: str) -> str:
    """Keep task options and arguments with the current folder and resume prompt."""
    command = seat["launcher_command"]
    index = next((i for i, value in enumerate(command) if Path(value).name in {"implement.sh", "review.sh"}), -1)
    if index < 0:
        raise ValueError("the saved command has no worker launcher")
    review = Path(command[index]).name == "review.sh"
    supported = {"--phase", "--lens"} if review else {"--phase", "--fix-cause"}
    position = index + 1
    options: list[str] = []
    while position < len(command) and command[position].startswith("--"):
        if position + 1 >= len(command):
            raise ValueError("the saved command has an incomplete option")
        option, value = command[position:position + 2]
        if option in supported:
            options.extend((option, value))
        position += 2
    arguments = command[position:]
    if len(arguments) < (3 if review else 9):
        raise ValueError("the saved command has incomplete task arguments")
    arguments[:3] = [seat["run_dir"], cwd, prompt]
    return shlex.join([*command[:index + 1], *options, "--to", seat["seat"], *arguments])
