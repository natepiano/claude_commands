"""Stop a session's Claude, or resume it on its account in its herdr tab."""

from __future__ import annotations

import os
import argparse
import json
import shlex
import signal
import sys
import subprocess
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING, cast
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS.parent))
for dependency in ("shutdown", "herdr", "message", "whoami", "lib"):
    sys.path.insert(0, str(SCRIPTS / dependency))

import herdr
import pane_record
from scripts.shutdown import folder_trust
from scripts.shutdown.stop_results import (
    Stopped as Stopped, NotRunning as NotRunning, StopFailed as StopFailed,
    ClaudeUp as ClaudeUp, MoveHandedOff as MoveHandedOff, ResumeFailed as ResumeFailed,
    SessionNotFound as SessionNotFound, MoveResult as MoveResult, claude_up_line as claude_up_line,
    DownHandedOff as DownHandedOff, DownResult as DownResult, down_result_line as down_result_line,
)
import sessions
if TYPE_CHECKING:
    from scripts.shutdown.inventory import PausedSeat
from launch_permission import shutdown_state_root

STOP_GRACE_SECONDS = 2.0
KILL_GRACE_SECONDS = 5.0
START_SECONDS = 20.0
STAY_SECONDS = 10.0
RESUME_ATTEMPTS = 3


def _tab_of(session: str) -> herdr.TabSession | SessionNotFound:
    """Find an existing pane, retaining its saved conversation when herdr's id is stale."""
    client = herdr.Herdr(herdr.shared_session())
    entries = pane_record.read(client.session)
    tabs: list[herdr.TabSession] = []
    for pane in client.panes():
        identity = pane["session_id"]
        entry = entries.get(pane["pane_id"])
        label = pane.get("label") or ""
        if entry is not None and entry["session_id"] != identity and (
                session == entry["session_id"] or session == label):
            if identity:
                live = sessions.session_liveness(identity)
                if isinstance(live, sessions.SessionLivenessUnknown):
                    raise OSError(live.reason)
                if isinstance(live, sessions.LiveSessionProcess):
                    continue  # A saved entry cannot displace another running conversation.
            identity = entry["session_id"]
        tabs.append(herdr.TabSession(pane["pane_id"], label, pane["workspace_id"], identity))
    matches = [tab for tab in tabs if tab.session_id == session] or [tab for tab in tabs if tab.label == session]
    if len(matches) > 1:
        raise ValueError(f"{session}: several tabs match")
    return matches[0] if matches and matches[0].session_id else SessionNotFound(session)


def _same_claude(pid: int, started: str) -> bool:
    """Registry removal is not exit: only the saved pid and start time say the Claude is gone."""
    identity = sessions.process_liveness(pid, started)
    if isinstance(identity, sessions.SessionLivenessUnknown):
        raise OSError(identity.reason)
    return isinstance(identity, sessions.LiveSessionProcess)


def _record_launches(selected: list[str]) -> None:
    """Seed only selected panes while their Claude still exposes its launch flags."""
    try:
        client = herdr.Herdr(herdr.shared_session())
        entries = pane_record.read(client.session)
        def selected_pane(pane: herdr.PaneIdentity) -> bool:
            entry = entries.get(pane["pane_id"])
            return (pane["session_id"] in selected or pane.get("label") in selected
                    or entry is not None and entry["session_id"] in selected)
        with redirect_stdout(StringIO()):
            _ = pane_record.seed(client.session, select=selected_pane)
    except (OSError, ValueError, herdr.HerdrFailure):
        pass  # Resume names the session it cannot start; the stop still goes ahead.


def stop(session: str) -> Stopped | NotRunning | StopFailed | SessionNotFound:
    """Stop on purpose; retain its launch for an explicit resume, not crash recovery."""
    _record_launches([session])
    result = _stop(session)
    if isinstance(result, Stopped):
        try:
            owner = herdr.shared_session()
            for pane, entry in pane_record.read(owner).items():
                if entry["session_id"] == result.session_id:
                    pane_record.end(owner, pane, result.session_id)
        except (OSError, ValueError) as error:
            return StopFailed(result.session_id, f"Claude stopped, but its intentional stop could not be saved: {error}")
    return result


def down(only: Sequence[str] = ()) -> list[DownResult]:
    """Stop local Claudes concurrently, retaining explicit restart intent in their launches."""
    client = herdr.Herdr(herdr.shared_session())
    entries = pane_record.latest_launches()
    pane_entries = pane_record.read_strict(client.session)
    names: dict[str, str] = {}
    for pane in client.panes():
        entry = pane_entries.get(pane["pane_id"])
        identity = pane["session_id"] or (entry["session_id"] if entry is not None else "")
        if identity and not (entry is not None and entry.get("ended", False)
                             and not entry.get("restart_after_down", False)):
            names[identity] = pane.get("label") or identity
    for entry in entries.values():
        if entry.get("restart_after_down", False):
            _ = names.setdefault(entry["session_id"], entry["name"])
    names.update({entry["sessionId"]: entry["name"] for entry in sessions.running_sessions()})
    selected: list[str] = []
    for item in only:
        candidates = {**{identity: entry["name"] for identity, entry in entries.items()}, **names}
        matches = [item] if item in candidates else [identity for identity, name in candidates.items() if name == item]
        if len(matches) != 1:
            raise ValueError(f"{item}: {'several sessions match' if matches else 'no local session'}")
        selected.extend(matches)
    selected = list(dict.fromkeys(selected if only else names))
    if not selected:
        return []
    unmarked = [identity for identity in selected
                if identity not in entries or not entries[identity].get("restart_after_down", False)]
    if unmarked:
        _record_launches(unmarked)
    prepared: list[str] = []
    failures: dict[str, StopFailed] = {}
    for identity in selected:
        try:
            entry = pane_record.launch_for_session(identity)
            if entry is None:
                raise ValueError("no pane launch entry; cannot retain this session for restart")
            pane_record.prepare_down(client.session, entry["pane_id"], identity)
            prepared.append(identity)
        except (OSError, ValueError) as error:
            failures[identity] = StopFailed(identity, str(error))
    if os.environ.get("CLAUDE_CODE_SESSION_ID") in prepared:
        try:
            _detach(["down", "--", *prepared], "down")
            return [failures[identity] if identity in failures else DownHandedOff(identity) for identity in selected]
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            return [failures[identity] if identity in failures else StopFailed(identity, f"cannot hand off down: {error}")
                    for identity in selected]

    def stop_for_restart(identity: str) -> Stopped | NotRunning | StopFailed | SessionNotFound:
        try:
            if identity in failures:
                return failures[identity]
            entry = pane_record.launch_for_session(identity)
            if entry is None:
                return StopFailed(identity, "no pane launch entry; cannot retain this session for restart")
            live = sessions.session_liveness(identity)
            if isinstance(live, sessions.SessionLivenessUnknown):
                return StopFailed(identity, live.reason)
            if isinstance(live, sessions.SessionNotLive):
                if not entry.get("ended", False):
                    pane_record.end(client.session, entry["pane_id"], identity)
                return NotRunning(identity)
            result = _stop(identity)
            if isinstance(result, (Stopped, NotRunning)):
                pane_record.end(client.session, entry["pane_id"], identity)
            return result
        except (OSError, ValueError, herdr.HerdrFailure) as error:
            return StopFailed(identity, str(error))

    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        return list(pool.map(stop_for_restart, selected))


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
        started = sessions.process_start(live.pid)
        if not started:
            return StopFailed(identity, f"process identity unreadable for pid {live.pid}")
        if not _same_claude(live.pid, started):
            return NotRunning(identity)
        os.kill(live.pid, signal.SIGTERM)
        deadline = monotonic() + STOP_GRACE_SECONDS
        while _same_claude(live.pid, started):
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            _ = Event().wait(min(0.1, remaining))
        else:
            return Stopped(identity, live.pid)
        if _same_claude(live.pid, started):
            os.kill(live.pid, signal.SIGKILL)
            deadline = monotonic() + KILL_GRACE_SECONDS
            while _same_claude(live.pid, started):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return StopFailed(identity, f"pid {live.pid} is still running after SIGKILL")
                _ = Event().wait(min(0.1, remaining))
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
    client = herdr.Herdr(herdr.shared_session())
    pane = next((pane for pane in client.panes() if pane["pane_id"] == tab.pane_id), None)
    if pane is None:
        raise herdr.HerdrFailure(f"no pane {tab.pane_id!r}")
    # A tab moved between workspaces keeps its shell's old marks; hooks must see this pane.
    environment = {**entry["environment"], "HERDR_PANE_ID": tab.pane_id, "HERDR_WORKSPACE_ID": pane["workspace_id"]}
    command = ["env", "-u", "HERDR_TAB_ID", *(f"{key}={value}" for key, value in environment.items()),
               sys.executable, str(SCRIPTS / "accounts/accounts.py"), "exec", account,
               *resume_arguments(entry), message]
    client.run_in_pane(tab.pane_id, command)


def _heal_launch_trust(entry: pane_record.PaneLaunch, target_dir: Path, *, dry_run: bool = False
                       ) -> herdr.Trusted | herdr.TrustCopied | ResumeFailed:
    trust = folder_trust.heal(Path(entry["cwd"]), target_dir, Path(entry["config_dir"]), dry_run=dry_run)
    if isinstance(trust, (folder_trust.FolderNotTrusted, folder_trust.FolderTrustUnreadable)):
        return ResumeFailed(entry["session_id"], f"{trust.reason}. Run: {trust.command}")
    return trust


def resume(session: str, *, why: str, to: str | None = None, requester: str = "",
           restore_missing: bool = False) -> ClaudeUp | ResumeFailed | SessionNotFound:
    began = monotonic()
    identity = session
    try:
        tab = _tab_of(session)
        client = herdr.Herdr(herdr.shared_session())
        if isinstance(tab, SessionNotFound):
            if not restore_missing:
                return tab
            entry = pane_record.launch_for_session(session)
            if entry is None:
                return tab
        else:
            if tab.session_id is None:
                return SessionNotFound(session)
            identity = tab.session_id
            entry = pane_record.read(client.session).get(tab.pane_id)
        live = sessions.session_liveness(identity)
        if isinstance(live, sessions.SessionLivenessUnknown):
            return ResumeFailed(identity, live.reason)
        if entry is None or entry["session_id"] != identity:
            return ResumeFailed(identity, "pane launch entry does not match the session")
        if isinstance(live, sessions.LiveSessionProcess):
            if isinstance(tab, SessionNotFound):
                return ResumeFailed(identity, "running Claude has no matching pane")
            from scripts.accounts import accounts

            label = accounts.claude_config_label(Path(entry["config_dir"]))
            if to is not None and to != label:
                return ResumeFailed(identity, f"Claude is still running on {label}")
            if entry.get("ended", False) or entry.get("restart_after_down", False):
                pane_record.resumed(client.session, tab.pane_id, identity)
            return ClaudeUp(identity, tab.pane_id, label)
        if restore_missing and entry.get("ended", False) and not entry.get("restart_after_down", False):
            return ResumeFailed(identity, "Claude was closed by hand")
        if not entry.get("flags_read", False):
            return ResumeFailed(identity, "pane launch flags are unknown")
        from inventory import codex_seats
        import unit_runs

        from scripts.accounts import accounts

        source = Path(entry["config_dir"])
        source_label = accounts.claude_config_label(source)
        label = to if to is not None else source_label
        target_dir = accounts.account_dir(label) if to is not None else source
        trust = _heal_launch_trust(entry, target_dir)
        if isinstance(trust, ResumeFailed):
            return trust
        if isinstance(tab, SessionNotFound):
            from scripts.whoami.account import named_account_environment

            pane = client.open_tab(entry["environment"].get("SHOWRUNNER_UNIT", "agents"),
                entry["name"] or identity, entry["cwd"],
                {**entry["environment"], **named_account_environment(target_dir)})
            entry = entry.copy()
            entry["pane_id"] = pane
            pane_record.write(client.session, entry)
            tab = next((item for item in client.tab_sessions() if item.pane_id == pane), None)
            if tab is None:
                return ResumeFailed(identity, "new recovery pane is unavailable")
            tab = replace(tab, session_id=identity)
        copied_from = trust.source if isinstance(trust, herdr.TrustCopied) else ""
        workers = ResumeWorkers(codex_seats([Path(run) for run in unit_runs.held(identity)]), entry["cwd"])
        message = resume_message(why, moved_from=source_label if to else None,
                                 moved_to=to, workers=workers)
        reason = "never started"
        for _attempt in range(RESUME_ATTEMPTS):
            live = sessions.session_liveness(identity)
            if isinstance(live, sessions.SessionLivenessUnknown):
                return ResumeFailed(identity, live.reason)
            if isinstance(live, sessions.SessionNotLive):
                _start_session(tab, entry, label, message)
            deadline = monotonic() + START_SECONDS
            while isinstance(live, sessions.SessionNotLive):
                live = sessions.session_liveness(identity)
                remaining = deadline - monotonic()
                if not isinstance(live, sessions.SessionNotLive) or remaining <= 0:
                    break
                time.sleep(min(0.5, remaining))
            if isinstance(live, sessions.SessionLivenessUnknown):
                return ResumeFailed(identity, live.reason)
            if isinstance(live, sessions.SessionNotLive):
                reason = "never started"
                continue
            generation = sessions.process_start(live.pid)
            if not generation:
                return ResumeFailed(identity, f"process identity unreadable for pid {live.pid}")
            pane_record.resumed(client.session, tab.pane_id, identity)
            _detach(["stay", "--session", identity, "--pane", tab.pane_id,
                     "--account", label, "--pid", str(live.pid), "--generation", generation, "--why", why,
                     *(["--requester", requester] if requester else [])], "stay")
            return ClaudeUp(identity, tab.pane_id, label, monotonic() - began, copied_from)
        return ResumeFailed(identity, reason)
    except (OSError, ValueError, herdr.HerdrFailure, subprocess.SubprocessError) as error:
        return ResumeFailed(identity, str(error))


def _detach(arguments: list[str], task: str) -> None:
    """One detached process boundary for batch coordination and the post-start check."""
    from scripts.production import showrunners

    # Capture the registry identity before the child loses the caller's session id.
    if "--requester" not in arguments:
        requester = showrunners.own_slug()
        options = ["--requester", requester]
        boundary = arguments.index("--") if "--" in arguments else len(arguments)
        arguments = [*arguments[:boundary], *options, *arguments[boundary:]]
    logs = shutdown_state_root() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"{task}-{uuid4().hex}.log"
    environment = dict(os.environ)
    _ = environment.pop("CLAUDE_CODE_SESSION_ID", None)
    command = [sys.executable, str(Path(__file__).resolve()), arguments[0], "--log", str(log), *arguments[1:]]
    if sys.platform == "darwin":
        _ = subprocess.Popen(command, env=environment, start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        _ = subprocess.run(["systemd-run", "--user", "--collect", "--quiet", "--no-block",
                            "env", "-u", "CLAUDE_CODE_SESSION_ID", *command],
                           env=environment, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, check=True, timeout=15)


def _handoff_batch(selected: list[str], target: str, why: str, warn_seconds: int) -> None:
    # Options precede the positional list so session names cannot become options.
    _detach(["batch", "--target", target, "--why", why, "--warn-seconds", str(warn_seconds),
             "--", *selected], "move")


def _check_stay(identity: str, pane: str, account: str, pid: int, generation: str,
                requester: str = "", why: str = "") -> str:
    """Observe the started generation after the command returns; recovery belongs to the watchdog."""
    began = monotonic()
    while _same_claude(pid, generation):
        remaining = STAY_SECONDS - (monotonic() - began)
        if remaining <= 0:
            return ""
        time.sleep(min(0.1, remaining))
    return _early_exit(identity, pane, account, monotonic() - began, requester=requester, why=why)


def _early_exit(identity: str, pane: str, account: str, elapsed: float, *, requester: str = "",
                why: str = "") -> str:
    """Build one actionable outcome for the showrunner, user push and stay log."""
    from scripts.production import showrunners, staff_sources
    from scripts.accounts import unit_watchdog
    from scripts.notify.push_texts import render
    from datetime import datetime, timezone
    import quota_alert
    from probe_session import PROBE_ENV
    from restart_support import local_machine

    entry = pane_record.read(herdr.shared_session()).get(pane)
    entry = entry if entry and entry["session_id"] == identity else None
    environment = entry["environment"] if entry else {}
    name = entry["name"] if entry else ""
    try:
        tab = _tab_of(identity)
        if isinstance(tab, herdr.TabSession):
            name = tab.label or name
    except (OSError, ValueError, herdr.HerdrFailure):
        pass  # The saved launch name still identifies a session whose pane is gone.
    name = name or identity
    unit = environment.get("SHOWRUNNER_UNIT_ID", "")
    if unit:
        try:
            recovery = unit_watchdog.run(datetime.now(timezone.utc), dry_run=False)
            next_step = "; ".join(recovery) if recovery else "watchdog reported no recovery action"
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            next_step = f"watchdog could not recover it: {error}"
    else:
        command = shlex.join(["python3", "scripts/shutdown/shutdown.py", "resume", name,
                              "--to", account, "--why", why or "retry after early exit"])
        next_step = f"resume it: cd ~/.claude && {command}"
    cause = f"{name} stopped {elapsed:.1f} s after its move or resume to {account}; {next_step}"
    try:
        slug = staff_sources.unit_parent(unit, identity).slug if unit else requester
        if slug:
            print(showrunners.tell(slug, f"resume-ended-{identity}-{uuid4().hex}", cause, sender="shutdown"))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"{identity}: could not tell showrunner: {error}")
    if environment.get(PROBE_ENV) != "1":
        _ = quota_alert.tell_user(render("shutdown.restart_failed", account=account, machine=local_machine(),
                                        stopped=name, cause=cause))
    return cause


def _warn_session(session: str, target: str, why: str, seconds: int) -> None:
    tab = _tab_of(session)
    if isinstance(tab, SessionNotFound):
        return
    _ = subprocess.run([sys.executable, str(SCRIPTS / "message/send.py"),
        "--to", f"session:{tab.session_id}", "--from", "shutdown",
        "--text", f"Account move in {seconds} seconds: {why}. You will resume on {target}; continue your work and workers."],
        capture_output=True, timeout=15, check=False)


def move_sessions(sessions: list[str], target: str, *, why: str, warn_seconds: int = 0,
                  requester: str = "") -> list[MoveResult]:
    """Leave untrusted members running; stop eligible members before resuming any."""
    if not 0 <= warn_seconds <= 15:
        raise ValueError("warn_seconds must be between 0 and 15")
    selected = list(dict.fromkeys(sessions))
    if not selected:
        return []
    began = monotonic()
    _record_launches(selected)  # Herdr learns a new session's id late; the launch entry knows it now.
    from scripts.accounts import accounts

    outcomes: dict[str, MoveResult] = {}
    for session in selected:
        try:
            tab = _tab_of(session)
            if isinstance(tab, SessionNotFound):
                continue  # The stop path returns the existing missing-session outcome.
            entry = pane_record.read(herdr.shared_session()).get(tab.pane_id)
            if entry is not None and entry["session_id"] == tab.session_id:
                trust = _heal_launch_trust(entry, accounts.account_dir(target), dry_run=True)
                if isinstance(trust, ResumeFailed):
                    outcomes[session] = trust
        except (OSError, ValueError, herdr.HerdrFailure) as error:
            outcomes[session] = ResumeFailed(session, str(error))
    eligible = [session for session in selected if session not in outcomes]
    if eligible:
        outcomes.update(zip(eligible, _move_eligible(eligible, target, why, warn_seconds, requester, began)))
    return [outcomes[session] for session in selected]


def _move_eligible(selected: list[str], target: str, why: str, warn_seconds: int,
                   requester: str, began: float) -> list[MoveResult]:
    """Move eligible members, preserving the batch stop barrier."""
    caller = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if caller:
        try:
            tabs = [_tab_of(session) for session in selected]
            if any(isinstance(tab, herdr.TabSession) and tab.session_id == caller for tab in tabs):
                _handoff_batch(selected, target, why, warn_seconds)
                return [MoveHandedOff(tab.session_id) if isinstance(tab, herdr.TabSession) and tab.session_id
                        else SessionNotFound(session) for session, tab in zip(selected, tabs)]
        except (OSError, ValueError, herdr.HerdrFailure, subprocess.SubprocessError) as error:
            return [StopFailed(session, f"cannot hand off move: {error}") for session in selected]
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
        stops = [pool.submit(_stop, session) for session in selected]
        stopped: list[Stopped | NotRunning | StopFailed | SessionNotFound] = []
        for session, future in zip(selected, stops):
            try:
                stopped.append(future.result())
            except Exception as error:
                stopped.append(StopFailed(session, str(error)))
        def resume_moved(identity: str) -> ClaudeUp | ResumeFailed | SessionNotFound:
            result = (resume(identity, why=why, to=target, requester=requester) if requester else
                      resume(identity, why=why, to=target))
            return replace(result, elapsed_seconds=monotonic() - began) if isinstance(result, ClaudeUp) else result

        resumes = {index: pool.submit(resume_moved, result.session_id)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    batch = commands.add_parser("batch")
    _ = batch.add_argument("--requester", default="")
    _ = batch.add_argument("--target", required=True)
    _ = batch.add_argument("--why", required=True)
    _ = batch.add_argument("--warn-seconds", type=int, default=0)
    _ = batch.add_argument("--log", type=Path, required=True)
    _ = batch.add_argument("sessions", nargs="+")
    down_batch = commands.add_parser("down")
    _ = down_batch.add_argument("--requester", default="")
    _ = down_batch.add_argument("--log", type=Path, required=True)
    _ = down_batch.add_argument("sessions", nargs="+")
    stay = commands.add_parser("stay")
    for option in ("session", "pane", "account", "generation"):
        _ = stay.add_argument("--" + option, required=True)
    _ = stay.add_argument("--requester", default="")
    _ = stay.add_argument("--why", default="")
    _ = stay.add_argument("--pid", type=int, required=True)
    _ = stay.add_argument("--log", type=Path, required=True)
    args = parser.parse_args(argv)
    # Even a directly invoked batch must not hand itself off a second time.
    _ = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
    log = cast(Path, args.log)
    if cast(str, args.command) == "down":
        with log.open("a", encoding="utf-8") as output:
            outcomes = down(cast(list[str], args.sessions))
            for outcome in outcomes:
                _ = output.write(down_result_line(outcome) + "\n")
        return int(any(isinstance(outcome, (StopFailed, SessionNotFound)) for outcome in outcomes))
    if cast(str, args.command) == "stay":
        with log.open("a", encoding="utf-8") as output, redirect_stdout(output):
            cause = _check_stay(cast(str, args.session), cast(str, args.pane), cast(str, args.account),
                                cast(int, args.pid), cast(str, args.generation), cast(str, args.requester), cast(str, args.why))
            _ = output.write(cause + "\n")
        return int(bool(cause))
    with log.open("a", encoding="utf-8") as output:
        results = move_sessions(cast(list[str], args.sessions), cast(str, args.target),
                                why=cast(str, args.why), warn_seconds=cast(int, args.warn_seconds),
                                requester=cast(str, args.requester))
        for result in results:
            _ = output.write(json.dumps({"result": type(result).__name__, **asdict(result)}) + "\n")
    return int(any(not isinstance(result, ClaudeUp) for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
