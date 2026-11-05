"""Build resume messages and commands for existing worker threads."""
from __future__ import annotations

import json
import shlex
from datetime import datetime, timezone
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from scripts.lib.script_envelope import script_prompt
from scripts.usage import agents_log

if TYPE_CHECKING:
    from scripts.shutdown.inventory import PausedSeat

SCRIPTS = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ResumeWorkers:
    """Existing worker threads and their work directory, for the opening message."""
    seats: Sequence[PausedSeat]
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
    if workers.seats:
        lines.append("Resume your Codex workers; do not open new ones.")
        for seat in workers.seats:
            if seat["turn_live"]:
                lines.append(f"{seat['seat']} ({seat['role']}, was working): {resume_worker_command(seat, workers.cwd)}")
            else:
                lines.append(f"{seat['seat']} ({seat['role']}, idle): paused on its thread; nothing to run now. "
                             + f"Your next dispatch with --to {seat['seat']} resumes it.")
        lines.append("Each command resumes the worker's existing thread.")
        lines.append("Each worker continues its own thread; after resuming, send later work with --to as usual.")
    elif why:
        lines.append("Codex workers keep their existing threads. Use assign.sh --to <worker> to resume a paused worker.")
    text = ("\n" if workers.seats else " ").join(lines)
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
    """Build an assignment command for the worker's existing thread."""
    from scripts.production import session_staff

    prompt = str(Path(seat["run_dir"]) / f"{seat['seat']}.resume-prompt.md")
    if seat["turn_live"] and seat["launcher_command"]:
        try:
            return _resume_task(seat, cwd, prompt)
        except ValueError:
            pass
    options = {"--role": "implement" if seat["role"] == "impl" else seat["role"]}
    phase = _resume_phase_id(seat)
    if phase:
        options["--phase"] = phase
    if seat["role"] == "review":
        assignment = agents_log.role_at(agents_log.assignments(agents_log.state_dir() / "agents.jsonl"),
                                       session_staff.worker_session(seat), datetime.now(timezone.utc))
        if isinstance(assignment, agents_log.Assignment) and assignment.role == "review" and assignment.lens:
            options["--lens"] = assignment.lens
    return _assignment_command(seat, cwd, prompt, options)


def _assignment_command(seat: PausedSeat, cwd: str, prompt: str, options: dict[str, str]) -> str:
    """Supply the current worker, folder, prompt, and authorized phase paths."""
    role = options["--role"]
    if role == "fix":
        _ = options.setdefault("--fix-cause", "resume after a move")
    if role != "review" and "--paths" not in options:
        directory = Path(seat["run_dir"])
        if phase := options.get("--phase"):
            directory = directory / "phases" / phase
        options["--paths"] = str(directory / "files")
    start = ["bash", str(SCRIPTS / "direct/assign.sh"), "--to", seat["seat"],
             "--session-dir", seat["run_dir"], "--cwd", cwd, "--prompt", prompt]
    return shlex.join([*start, *(value for pair in options.items() for value in pair)])


def _resume_task(seat: PausedSeat, cwd: str, prompt: str) -> str:
    """Convert a saved launcher command into an assignment command."""
    command = seat["launcher_command"]
    index = next((i for i, value in enumerate(command)
                  if Path(value).name in {"implement.sh", "review.sh", "assign.sh", "assign.py"}), -1)
    if index < 0:
        raise ValueError("the saved command has no worker launcher")
    launcher = Path(command[index]).name
    assignment = launcher in {"assign.sh", "assign.py"}
    supported = {"--role", "--paths", "--phase", "--lens", "--pass", "--fix-cause", "--summary",
                 "--style-since", "--style-until", "--early-ready", "--diff"}
    position = index + 1
    options: dict[str, str] = {}
    while position < len(command) and command[position].startswith("--"):
        if position + 1 >= len(command):
            raise ValueError("the saved command has an incomplete option")
        option, value = command[position:position + 2]
        if option in supported:
            options[option] = value
        position += 2
    if not assignment:
        review = launcher == "review.sh"
        arguments = command[position:]
        if len(arguments) < (3 if review else 9):
            raise ValueError("the saved command has incomplete task arguments")
        role = "review" if review else arguments[5]
        options["--role"] = "implement" if role == "impl" else role
        if review and len(arguments) > 6:
            options["--pass"] = str(max(1, int(arguments[6])))
    if "--role" not in options:
        raise ValueError("the saved assignment has no role")
    return _assignment_command(seat, cwd, prompt, options)
