"""Build resume messages and commands for existing worker threads."""
from __future__ import annotations

import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, TypedDict

from scripts.direct import assign_gate, phase_reservation, progress_history
from scripts.lib.script_envelope import script_prompt
from scripts.production import staff


@dataclass(frozen=True)
class NoAccountMove:
    """Keep the current account."""


@dataclass(frozen=True)
class AccountMove:
    source: str
    destination: str


@dataclass(frozen=True)
class IdleWorker:
    """The worker has no active dispatch."""


@dataclass(frozen=True)
class BusyPass:
    dispatch_id: str


@dataclass(frozen=True)
class UnsubmittedAssignment:
    dispatch_id: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class NoOwnPass:
    """The recipient has no active pass."""


@dataclass(frozen=True)
class PaneWorkerResume:
    staff_id: staff.StaffId
    turn: BusyPass | UnsubmittedAssignment | IdleWorker


class ClaudeChildResume(TypedDict):
    """A Claude child keeps its launcher command and assignment."""

    run_dir: str
    seat: str
    role: str
    staff_id: str
    turn_live: bool
    launcher_command: list[str]


class Resumable(NamedTuple):
    seat: ClaudeChildResume


class Idle(NamedTuple):
    seat: ClaudeChildResume


class Unavailable(NamedTuple):
    staff_id: str
    name: str
    reason: str


WorkerResume = Resumable | Idle | Unavailable


class ResumeWorkers(NamedTuple):
    results: Sequence[WorkerResume] = ()
    cwd: str = ""
    panes: Sequence[PaneWorkerResume] = ()


NO_ACCOUNT_MOVE = NoAccountMove()
NO_WORKERS = ResumeWorkers()
NO_OWN_PASS = NoOwnPass()


def launcher_prompt(worker: staff.StaffId, dispatch_id: str, command: Sequence[str]) -> str:
    """Build launcher instructions; the prompt client adds the script envelope."""
    return (f"Resume worker {worker}, dispatch {dispatch_id}. "
            + f"Run this saved command from your session shell: {shlex.join(command)}")


def resume_message(why: str, *, move: NoAccountMove | AccountMove = NO_ACCOUNT_MOVE,
                   workers: ResumeWorkers = NO_WORKERS, context: str = "",
                   own_pass: BusyPass | NoOwnPass = NO_OWN_PASS) -> str:
    moved = f" You were moved from {move.source} to {move.destination}." if isinstance(move, AccountMove) else ""
    if isinstance(own_pass, BusyPass):
        opening = context or f"You were stopped and resumed because {why}.{moved}"
        return script_prompt(f"{opening}\nContinue pass {own_pass.dispatch_id} in this conversation. Keep the same pass id.")
    lines: list[str] = []
    if why or context:
        opening = context or f"You were stopped and resumed because {why}.{moved} Continue where you stopped."
        lines.append(opening.rstrip() + " First run `~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py pending` "
                     + "for messages kept while you were down, then continue from there; if you were waiting on the user, keep waiting. "
                     + "Background commands that you started before the restart ended with no result. Run them again.")
    for pane in workers.panes:
        if isinstance(pane.turn, UnsubmittedAssignment):
            lines.append(launcher_prompt(pane.staff_id, pane.turn.dispatch_id, pane.turn.command))
        elif isinstance(pane.turn, BusyPass):
            lines.append(f"Worker {pane.staff_id}: busy dispatch {pane.turn.dispatch_id}. "
                         + "Send this step to the worker's existing conversation. "
                         + f"Continue pass {pane.turn.dispatch_id} in this conversation. Keep the same pass id.")
        else:
            lines.append(f"Worker {pane.staff_id}: idle. Continue with this worker's existing conversation.")
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
    text = ("\n" if workers.results or workers.panes else " ").join(lines)
    return script_prompt(text)


def resume_worker_command(seat: ClaudeChildResume, cwd: str) -> str:
    """Build an assignment command for the worker's existing thread."""
    prompt = str(Path(seat["run_dir"]) / f"{seat['seat']}.resume-prompt.md")
    if seat["turn_live"] and seat["launcher_command"]:
        try:
            return _resume_task(seat, cwd, prompt)
        except ValueError:
            pass
    options = {"--role": seat["role"]}
    dispatch = progress_history.last_dispatch(staff.StaffId(seat["staff_id"]))
    if isinstance(dispatch, progress_history.RecordedDispatch):
        options.update({option: value for option, value in (
            ("--phase", dispatch.phase_id), ("--lens", dispatch.lens), ("--paths", dispatch.paths))
                        if isinstance(value, str)})
    return _assignment_command(seat, cwd, prompt, list(options.items()))


def _assignment_command(seat: ClaudeChildResume, cwd: str, prompt: str, options: list[tuple[str, str]]) -> str:
    """Supply the current folder, prompt, and authorized phase paths."""
    values = dict(options)
    role = values["--role"]
    if role == "fix" and "--fix-cause" not in values:
        options.append(("--fix-cause", "resume after a move"))
    if role != "review" and "--paths" not in values and (phase := values.get("--phase")):
        options.append(("--paths", str(phase_reservation.phase_directory(Path(seat["run_dir"]), phase)
                                     / phase_reservation.FILES_FILE)))
    refreshed = {"--to": seat["seat"], "--session-dir": seat["run_dir"], "--cwd": cwd, "--prompt": prompt}
    return assign_gate.assign_command(
        seat["run_dir"], seat["seat"], role,
        [*refreshed.items(), *((option, value) for option, value in options if option not in refreshed)])

def _resume_task(seat: ClaudeChildResume, cwd: str, prompt: str) -> str:
    """Convert a saved launcher command into an assignment command."""
    command = seat["launcher_command"]
    index = next((i for i, value in enumerate(command) if Path(value).name in assign_gate.WORKER_LAUNCHERS), -1)
    if index < 0:
        raise ValueError("the saved command has no worker launcher")
    launcher = Path(command[index]).name
    position = index + 1
    options: list[tuple[str, str]] = []
    while position < len(command) and command[position].startswith("--"):
        if position + 1 >= len(command):
            raise ValueError("the saved command has an incomplete option")
        option, value = command[position:position + 2]
        if launcher in assign_gate.ASSIGN_LAUNCHERS or option not in {"--pool", "--lease", "--slot"}:
            options.append((option, value))
        position += 2
    if launcher not in assign_gate.ASSIGN_LAUNCHERS:
        arguments = command[position:]
        if len(arguments) < 3:
            raise ValueError("the saved command has incomplete task arguments")
        options.append(("--role", "review"))
        if len(arguments) > 6:
            options.append(("--pass", str(max(1, int(arguments[6])))))
            for offset, option in ((7, "--lens"), (8, "--early-ready")):
                if len(arguments) > offset and arguments[offset] and option not in dict(options):
                    options.append((option, arguments[offset]))
    if "--role" not in dict(options):
        raise ValueError("the saved assignment has no role")
    return _assignment_command(seat, cwd, prompt, options)
