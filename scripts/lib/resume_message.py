"""Build resume messages and commands for existing worker threads."""
from __future__ import annotations

import json
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from scripts.direct.worker_state import worker_slot

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
        lines.append("Codex workers keep their existing threads. Use implement.sh --to <worker> or "
                     + "codex_mesh.py resume for a paused worker; do not open a replacement thread.")
    return ("\n" if workers.seats else " ").join(lines)


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
    launcher = "review.sh" if seat["role"] == "review" else "implement.sh"
    prompt = str(Path(seat["run_dir"]) / f"{seat['seat']}.resume-prompt.md")
    resumed = _resumed_launcher_command(seat["launcher_command"], seat["seat"], prompt) if seat["turn_live"] else None
    if resumed is not None:
        return resumed
    # The roster role is the last pass kind; implement.sh claims the worker by the slot its identity names.
    recorded_slot = worker_slot(Path(seat["run_dir"]), seat["seat"]) or ""
    team_role = recorded_slot or seat["role"]
    phase_id = _resume_phase_id(seat)
    # The launchers are not executable files: `bash` runs them as pasted.
    start = ["bash", str(SCRIPTS / "direct" / launcher), "--to", seat["seat"]]
    if phase_id:
        start.extend(("--phase", phase_id))
    kind = team_role if team_role in {"impl", "test", "fix"} else "impl"
    if seat["role"] == "fix":
        kind = "fix"
    if launcher == "implement.sh" and kind == "fix":
        start.extend(("--fix-cause", "resume after a move"))
    start.extend((seat["run_dir"], cwd, prompt))
    if launcher == "review.sh":
        return shlex.join([*start, "review", "resume after a move", "resuming after a move"])
    return shlex.join([*start, kind, "resume after a move", kind, "resuming after a move", "0", team_role])


def _resumed_launcher_command(command: list[str], seat: str, prompt: str) -> str | None:
    """Keep the recorded task arguments, following the worker with its resume prompt; None when incomplete."""
    index = next((i for i, arg in enumerate(command) if Path(arg).name in {"implement.sh", "review.sh"}), -1)
    if index < 0:
        return None
    # Retain supported task options; saved commands may predate the current launcher.
    supported = {"--phase", "--lens"} if Path(command[index]).name == "review.sh" else {"--phase", "--fix-cause"}
    position = index + 1
    options: list[str] = []
    while position < len(command) and command[position].startswith("--"):
        if position + 1 >= len(command):
            return None
        option, value = command[position], command[position + 1]
        if option in supported:
            options.extend((option, value))
        position += 2
    arguments = command[position:]
    # implement.sh requires its team role as the ninth argument; review.sh has defaults after three.
    if len(arguments) < (9 if Path(command[index]).name == "implement.sh" else 3):
        return None
    arguments[2] = prompt
    return shlex.join([*command[:index + 1], *options, "--to", seat, *arguments])


