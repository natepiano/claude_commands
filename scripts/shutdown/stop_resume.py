"""Stop a session's Claude, or resume it on its account in its herdr tab."""

from __future__ import annotations

import os
import json
import shlex
import signal
import sys
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
from threading import Event
from time import monotonic
from typing import cast

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS.parent))
from scripts.lib import process_start as process_identity

for dependency in ("herdr", "message", "whoami", "lib"):
    sys.path.insert(0, str(SCRIPTS / dependency))

import herdr
import pane_record
import sessions
from shutdown_record import PausedSeat

STOP_GRACE_SECONDS = 2.0


@dataclass(frozen=True)
class Stopped:
    session_id: str
    pid: int


@dataclass(frozen=True)
class NotRunning:
    session_id: str


@dataclass(frozen=True)
class StopFailed:
    session_id: str
    reason: str


@dataclass(frozen=True)
class Resumed:
    session_id: str
    pane_id: str
    account: str


@dataclass(frozen=True)
class ResumeFailed:
    session_id: str
    reason: str


@dataclass(frozen=True)
class SessionNotFound:
    name: str


def _tab_of(session: str) -> herdr.TabSession | SessionNotFound:
    """The session's tab by id or label; herdr drops the id at exit, so the pane's launch entry supplies it."""
    client = herdr.Herdr(herdr.shared_session())
    entries = pane_record.read(client.session)
    tabs = [tab if tab.session_id or tab.pane_id not in entries
            else replace(tab, session_id=entries[tab.pane_id]["session_id"])
            for tab in client.tab_sessions()]
    matches = [tab for tab in tabs if tab.session_id == session] or [tab for tab in tabs if tab.label == session]
    if len(matches) > 1:
        raise ValueError(f"{session}: several tabs match")
    return matches[0] if matches and matches[0].session_id else SessionNotFound(session)


def _same_claude(session_id: str, pid: int, started: str) -> bool:
    live = sessions.session_liveness(session_id)
    if isinstance(live, sessions.SessionLivenessUnknown):
        raise OSError(live.reason)
    identity = sessions.process_liveness(pid, started)
    if isinstance(identity, sessions.SessionLivenessUnknown):
        raise OSError(identity.reason)
    return isinstance(live, sessions.LiveSessionProcess) and live.pid == pid and isinstance(identity, sessions.LiveSessionProcess)


def _record_launches() -> None:
    """Read each running Claude's launch now: a hand-resumed tab has no entry, and a stopped Claude cannot say."""
    try:
        with redirect_stdout(StringIO()):
            _ = pane_record.seed(herdr.shared_session())
    except (OSError, ValueError, herdr.HerdrFailure):
        pass  # Resume names the session it cannot start; the stop still goes ahead.


def stop(session: str) -> Stopped | NotRunning | StopFailed | SessionNotFound:
    _record_launches()
    return _stop(session)


def _stop(session: str) -> Stopped | NotRunning | StopFailed | SessionNotFound:
    """Signal only the Claude identified by the session registry; leave workers alone."""
    identity = session
    try:
        tab = _tab_of(session)
        if isinstance(tab, SessionNotFound):
            return tab
        if tab.session_id is None:
            return SessionNotFound(session)
        identity = tab.session_id
        live = sessions.session_liveness(identity)
        if isinstance(live, sessions.SessionNotLive):
            return NotRunning(identity)
        if isinstance(live, sessions.SessionLivenessUnknown):
            return StopFailed(identity, live.reason)
        started = process_identity.process_start(live.pid)
        if isinstance(started, process_identity.ProcessGone):
            return NotRunning(identity)
        if not _same_claude(identity, live.pid, started.text):
            return NotRunning(identity)
        os.kill(live.pid, signal.SIGTERM)
        _ = Event().wait(STOP_GRACE_SECONDS)
        if _same_claude(identity, live.pid, started.text):
            os.kill(live.pid, signal.SIGKILL)
        return Stopped(identity, live.pid)
    except ProcessLookupError:
        return NotRunning(identity)
    except (OSError, ValueError, herdr.HerdrFailure) as error:
        return StopFailed(identity, str(error))


def resume_arguments(entry: pane_record.PaneLaunch) -> list[str]:
    arguments = ["--resume", entry["session_id"]]
    if entry["name"]:
        arguments.extend(("-n", entry["name"]))
    if entry["remote_control"]:
        arguments.extend(("--remote-control", entry["name"]) if entry["name"] else ("--remote-control",))
    if entry["model"]:
        arguments.extend(("--model", entry["model"]))
    arguments.extend(("--settings", '{"disableAgentView": true}'))
    return arguments


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


def _start_session(tab: herdr.TabSession, entry: pane_record.PaneLaunch, account: str, message: str) -> None:
    """Run the account command in the session's own pane, with one resume message."""
    command = ["env", *(f"{key}={value}" for key, value in entry["environment"].items()),
               sys.executable, str(SCRIPTS / "accounts/accounts.py"), "exec", account,
               *resume_arguments(entry), message]
    herdr.Herdr(herdr.shared_session()).run_in_pane(tab.pane_id, command)


def resume(session: str, *, why: str, to: str | None = None) -> Resumed | ResumeFailed | SessionNotFound:
    identity = session
    try:
        tab = _tab_of(session)
        if isinstance(tab, SessionNotFound):
            return tab
        if tab.session_id is None:
            return SessionNotFound(session)
        identity = tab.session_id
        live = sessions.session_liveness(identity)
        if isinstance(live, sessions.LiveSessionProcess):
            return ResumeFailed(identity, "Claude is still running")
        if isinstance(live, sessions.SessionLivenessUnknown):
            return ResumeFailed(identity, live.reason)
        client = herdr.Herdr(herdr.shared_session())
        entry = pane_record.read(client.session).get(tab.pane_id)
        if entry is None or entry["session_id"] != identity:
            return ResumeFailed(identity, "pane launch entry does not match the session")
        if not entry.get("flags_read", False):
            return ResumeFailed(identity, "pane launch flags are unknown")
        from inventory import codex_seats
        import unit_runs

        source = Path(entry["config_dir"])
        label = to if to is not None else source.name.removeprefix(".")
        workers = ResumeWorkers(codex_seats([Path(run) for run in unit_runs.held(identity)]), entry["cwd"])
        message = resume_message(why, moved_from=source.name.removeprefix(".") if to else None,
                                 moved_to=to, workers=workers)
        _start_session(tab, entry, label, message)
        return Resumed(identity, tab.pane_id, label)
    except (OSError, ValueError, herdr.HerdrFailure) as error:
        return ResumeFailed(identity, str(error))


MoveResult = Resumed | StopFailed | ResumeFailed | SessionNotFound


def _warn_session(session: str, target: str, why: str, seconds: int) -> None:
    tab = _tab_of(session)
    if isinstance(tab, SessionNotFound):
        return
    _ = subprocess.run([sys.executable, str(SCRIPTS / "message/send.py"),
        "--to", f"session:{tab.session_id}", "--from", "shutdown",
        "--text", f"Account move in {seconds} seconds: {why}. You will resume on {target}; continue your work and workers."],
        capture_output=True, timeout=15, check=False)


def move_sessions(sessions: list[str], target: str, *, why: str, warn_seconds: int = 0) -> list[MoveResult]:
    """Interrupt all selected sessions before resuming any; isolate each failure."""
    if not 0 <= warn_seconds <= 15:
        raise ValueError("warn_seconds must be between 0 and 15")
    selected = list(dict.fromkeys(sessions))
    if not selected:
        return []
    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        if warn_seconds:
            warning_until = monotonic() + warn_seconds
            warnings = [pool.submit(_warn_session, session, target, why, warn_seconds) for session in selected]
            for warning in warnings:
                try:
                    warning.result()
                except Exception:
                    pass  # An undeliverable warning cannot prevent the requested interruption.
            _ = Event().wait(max(0.0, warning_until - monotonic()))
        _record_launches()
        stops = [pool.submit(_stop, session) for session in selected]
        stopped: list[Stopped | NotRunning | StopFailed | SessionNotFound] = []
        for session, future in zip(selected, stops):
            try:
                stopped.append(future.result())
            except Exception as error:
                stopped.append(StopFailed(session, str(error)))
        resumes = {index: pool.submit(resume, result.session_id, why=why, to=target)
                   for index, result in enumerate(stopped) if isinstance(result, (Stopped, NotRunning))}
        outcomes: list[MoveResult] = []
        for index, result in enumerate(stopped):
            if isinstance(result, (StopFailed, SessionNotFound)):
                outcomes.append(result)
                continue
            try:
                resumed = resumes[index].result()
                outcomes.append(ResumeFailed(result.session_id, "session has no tab after stop")
                                if isinstance(resumed, SessionNotFound) else resumed)
            except Exception as error:
                outcomes.append(ResumeFailed(result.session_id, str(error)))
        return outcomes


def move(session: str, target: str, *, why: str) -> MoveResult:
    return move_sessions([session], target, why=why)[0]


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
    from scripts.direct.worker_state import worker_slot

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
    # Launchers accept option/value pairs followed by session_dir, cwd, prompt_file.
    position = index + 1
    options: list[str] = []
    while position < len(command) and command[position].startswith("--"):
        if position + 1 >= len(command):
            return None
        option, value = command[position], command[position + 1]
        if option not in {"--slot", "--pool", "--lease", "--to"}:
            options.extend((option, value))
        position += 2
    arguments = command[position:]
    # implement.sh requires its team role as the ninth argument; review.sh has defaults after three.
    if len(arguments) < (9 if Path(command[index]).name == "implement.sh" else 3):
        return None
    arguments[2] = prompt
    return shlex.join([*command[:index + 1], *options, "--to", seat, *arguments])
