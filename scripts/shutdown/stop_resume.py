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
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING, Never, cast
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS.parent))
for dependency in ("shutdown", "herdr", "message", "whoami", "lib"):
    sys.path.insert(0, str(SCRIPTS / dependency))

import herdr
import pane_record
from scripts.shutdown.launch_names import LaunchNames
import pane_launch
from scripts.lib.resume_message import (
    ResumeWorkers as ResumeWorkers,
    NO_WORKERS as NO_WORKERS,
    resume_message as resume_message,
    resume_worker_command as resume_worker_command,
)
from scripts.shutdown.stop_results import (
    Stopped as Stopped, NotRunning as NotRunning, StopFailed as StopFailed,
    ClaudeUp as ClaudeUp, ClaudeStartState, ResumePending as ResumePending, MoveHandedOff as MoveHandedOff, ResumeFailed as ResumeFailed,
    SessionNotFound as SessionNotFound, MoveResult as MoveResult, claude_up_line as claude_up_line,
    RunningOnAnotherAccount as RunningOnAnotherAccount, ResumeResult as ResumeResult,
    resume_failure_reason as resume_failure_reason,
)
import sessions
from scripts.shutdown.stop_results import shutdown_state_root

if TYPE_CHECKING:
    from scripts.production.staff import StaffRow

STOP_GRACE_SECONDS = 2.0
KILL_GRACE_SECONDS = 5.0
START_SECONDS = 20.0
STAY_SECONDS = 10.0


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
    if not selected:
        return
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
    if isinstance(result, (Stopped, NotRunning)):
        _retire_prompt(result.session_id)
    if isinstance(result, Stopped):
        try:
            owner = herdr.shared_session()
            for pane, entry in pane_record.read(owner).items():
                if entry["session_id"] == result.session_id:
                    pane_record.end(owner, pane, result.session_id)
        except (OSError, ValueError) as error:
            return StopFailed(result.session_id, f"Claude stopped, but its intentional stop could not be saved: {error}")
    return result


def _retire_prompt(session_id: str) -> None:
    """Publish only the session id and stop time; any failure is separate from stop or move."""
    try:
        from scripts.production.stall_watch import retire_session_prompt

        retire_session_prompt(session_id)
    except Exception as error:
        print(f"{session_id}: prompt cancellation: {error}", file=sys.stderr)


def _stop(session: str) -> Stopped | NotRunning | StopFailed | SessionNotFound:
    """Signal only the Claude identified by the session registry; leave workers alone."""
    identity = session
    try:
        from scripts.production import staff

        member = staff.initial_snapshot().by_id(staff.StaffId(session))
        if member is not None:
            session = identity = member.current_session
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
    return pane_record.resume_arguments(LaunchNames.read().resume(entry))


def _pane_command(tab: herdr.TabSession, entry: pane_record.PaneLaunch, account: str) -> list[str]:
    """Build the account command for both terminal paths."""
    client = herdr.Herdr(herdr.shared_session())
    pane = next((pane for pane in client.panes() if pane["pane_id"] == tab.pane_id), None)
    if pane is None:
        raise herdr.HerdrFailure(f"no pane {tab.pane_id!r}")
    environment = {**entry["environment"], "HERDR_PANE_ID": tab.pane_id,
                   "HERDR_WORKSPACE_ID": pane["workspace_id"], "RELAUNCH_PROMPT_DELIVERY": "pane"}
    _ = environment.pop("STAFF_ID", None)
    return ["env", "-u", "HERDR_TAB_ID", "-u", "STAFF_ID", *(f"{key}={value}" for key, value in environment.items()),
            "sh", "-c", 'cd -- "$1" && shift && exec "$@"', "resume", entry["cwd"],
            sys.executable, str(SCRIPTS / "accounts/accounts.py"), "exec", account,
            *pane_record.resume_arguments(entry)]


def _exec_in_own_pane(tab: herdr.TabSession, entry: pane_record.PaneLaunch,
                      account: str, message: str) -> Never:
    """Replace the foreground command with the saved account launch."""
    command = _pane_command(tab, entry, account)
    os.chdir(entry["cwd"])
    os.execvp(command[0], [*command, message])


def _start_session(tab: herdr.TabSession, entry: pane_record.PaneLaunch, account: str, message: str) -> sessions.LiveSessionProcess:
    """Run the account command in the session's own pane, with one resume message."""
    client = herdr.Herdr(herdr.shared_session())
    command = _pane_command(tab, entry, account)
    launched_at = datetime.now(timezone.utc)
    client.run_in_pane(tab.pane_id, command)
    deadline = monotonic() + START_SECONDS
    live = sessions.session_liveness(entry["session_id"])
    while isinstance(live, sessions.SessionNotLive):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise herdr.HerdrFailure("never started")
        time.sleep(min(0.5, remaining))
        live = sessions.session_liveness(entry["session_id"])
    if isinstance(live, sessions.SessionLivenessUnknown):
        raise herdr.HerdrFailure(live.reason)
    identity = client.native_session(tab.pane_id, "claude", START_SECONDS)
    if identity != entry["session_id"]:
        raise herdr.HerdrFailure(f"{entry['session_id']}: resumed registry identity differs: {identity}")
    opening = herdr.OpeningPrompt(message, herdr.SessionTranscript(Path(entry["config_dir"]), identity))
    outcome = client.submit_opening_prompt(tab.pane_id, opening, launched_at, int(START_SECONDS * 1000))
    if isinstance(outcome, herdr.PromptNotSubmitted):
        raise herdr.HerdrFailure(f"{identity}: resume prompt not submitted: {outcome.reason}")
    return live


def _running_on_account(identity: str, pane_id: str, target: str | None) -> ClaudeUp | ResumeFailed | sessions.SessionNotLive | RunningOnAnotherAccount:
    """A rerun leaves a proven live destination alone; unreadable attribution cannot authorize a stop."""
    live = sessions.session_liveness(identity)
    if isinstance(live, sessions.SessionNotLive):
        return live
    if isinstance(live, sessions.SessionLivenessUnknown):
        return ResumeFailed(identity, live.reason)
    from scripts.whoami import account
    from scripts.accounts import accounts

    setting = account.process_account_directory(account.process_config_dir(live.pid))
    if not isinstance(setting, account.ConfiguredClaudeFolder):
        return ResumeFailed(identity, "running Claude account is unreadable")
    label = accounts.claude_config_label(setting.path)
    if target is not None and setting.path != accounts.account_dir(target):
        return RunningOnAnotherAccount(identity)
    observed = herdr.Herdr(herdr.shared_session()).native_session(pane_id, "claude", START_SECONDS)
    if observed != identity:
        return ResumeFailed(identity, f"resumed registry identity differs: {observed}")
    return ClaudeUp(identity, pane_id, label, start_state=ClaudeStartState.ALREADY_RUNNING)


def _member_machine(member: StaffRow) -> str:
    """Return the member's recorded machine, or an empty string when unknown."""
    from scripts.production import staff

    location = member.worktree if isinstance(member.worktree, (staff.PresentWorktree, staff.RemovedWorktree)) else member.doc
    if isinstance(location, (staff.PresentWorktree, staff.RemovedWorktree, staff.LiveDoc, staff.WrappedDoc)):
        return location.machine
    return ""


def _nearby_resume_names(reference: str, names: LaunchNames) -> list[str]:
    """Select close names from local units and showrunners."""
    import remote

    machine = remote.local_machine()
    return sorted({row.name for row in names.tab_record.rows
                   if row.kind in {"unit", "showrunner"} and _member_machine(row) == machine
                   and reference.casefold() in row.name.casefold()})[:5]


def resume(session: str, *, why: str = "resumed by name", to: str | None = None, requester: str = "",
           restore_missing: bool = False, resume_ended: bool = False) -> ResumeResult:
    policy = sessions.retry_policy()
    names = LaunchNames.read()
    record = names.tab_record
    member = record.by_member_reference(session)
    matches = [row for row in record.rows if row.name == session or row == member]
    candidates = ", ".join(f"{row.name} ({row.id})" for row in matches[:5])
    if len(matches) > 1:
        return ResumeFailed(session, f"ambiguous name; candidates: {candidates}")
    if matches:
        import remote

        member = matches[0]
        machine = _member_machine(member)
        if machine and machine != remote.local_machine():
            return ResumeFailed(session, f"session is on {machine}")
        session = member.current_session
    attempt = 0
    while True:
        result = _resume_once(session, why=why, to=to, requester=requester, restore_missing=restore_missing, resume_ended=resume_ended, names=names)
        attempt += 1
        if isinstance(result, SessionNotFound) and not matches:
            nearby = _nearby_resume_names(session, names)
            suffix = "; candidates: " + ", ".join(nearby) if nearby else ""
            return ResumeFailed(session, "unknown name" + suffix)
        if isinstance(result, RunningOnAnotherAccount):
            return result
        if not isinstance(result, ResumeFailed) or attempt >= policy.resume_attempts:
            return result
        time.sleep(policy.resume_delay_seconds)


def _resume_once(session: str, *, why: str, to: str | None = None, requester: str = "",
                 restore_missing: bool = False, resume_ended: bool = False, names: LaunchNames) -> ResumeResult:
    began = monotonic()
    identity = session
    original: pane_launch.LaunchPublication | None = None
    attempt_pane = ""
    try:
        member = names.tab_record.by_member_reference(session)
        if member is not None:
            session = identity = member.current_session
        tab = _tab_of(session)
        client = herdr.Herdr(herdr.shared_session())
        if isinstance(tab, SessionNotFound):
            if not restore_missing:
                return tab
            original = pane_record.publication_for_session(session, record=names.tab_record)
            if original is None:
                return tab
            if original.path != pane_record.record_path(client.session):
                return ResumeFailed(identity, "recovery across retained session files requires an explicit transfer")
            entry = original.entry
        else:
            if tab.session_id is None:
                return SessionNotFound(session)
            identity = tab.session_id
            entry = pane_record.read_strict(client.session).get(tab.pane_id)
            if entry is not None:
                original = pane_launch.LaunchPublication(pane_record.record_path(client.session), entry)
        if entry is not None:
            entry = names.resume(entry)
        pane_id = entry["pane_id"] if isinstance(tab, SessionNotFound) and entry is not None else (
            tab.pane_id if isinstance(tab, herdr.TabSession) else "")
        running = _running_on_account(identity, pane_id, to)
        if not isinstance(running, sessions.SessionNotLive):
            return running
        if entry is None or entry["session_id"] != identity:
            return ResumeFailed(identity, "pane launch entry does not match the session")
        if restore_missing and not resume_ended and entry.get("ended", False):
            return ResumeFailed(identity, "Claude was closed by hand")
        if not entry.get("flags_read", False):
            return ResumeFailed(identity, "pane launch flags are unknown")
        entry["environment"] = names.environment(entry)
        from inventory import codex_seats
        import unit_runs

        from scripts.accounts import accounts
        from scripts.whoami.account import named_account_environment

        source = Path(entry["config_dir"])
        source_label = accounts.claude_config_label(source)
        target_dir = (accounts.account_dir(to) if to is not None else
                      Path(entry["environment"].get("CLAUDE_CONFIG_DIR", str(source))))
        label = to if to is not None else accounts.claude_config_label(target_dir)
        account_environment = named_account_environment(target_dir)
        trust = herdr.heal_claude_trust(Path(entry["cwd"]), target_dir / ".claude.json",
            accounts.trust_sources(target_dir, source), dry_run=False)
        if isinstance(trust, (herdr.Untrusted, herdr.UnreadableTrustFile)):
            reason = str(trust) if isinstance(trust, herdr.UnreadableTrustFile) else f"Claude has not trusted {entry['cwd']}"
            print(f"{entry['name']}: trust anomaly: {reason}", file=sys.stderr)
        if to is not None and original is not None and Path(original.entry["config_dir"]) != target_dir:
            original = pane_launch.finish_restore(original.path, original.entry,
                pane_launch.ResumeAttempt(original.entry["pane_id"], original.entry),
                pane_launch.PreparedAccountMove(target_dir, account_environment), close=client.close_tab_confirmed)
        entry = entry.copy()
        entry["config_dir"] = str(target_dir)
        entry["environment"] = {**entry["environment"], **account_environment}
        if isinstance(tab, SessionNotFound):
            with herdr.restore_staff(names.tab_record, labels=names.labels):
                pane = client.open_tab(entry["environment"].get("SHOWRUNNER_UNIT", "agents"),
                    entry["name"] or identity, entry["cwd"], entry["environment"])
            entry = entry.copy()
            entry["pane_id"] = pane
            attempt_pane = pane
            tab = next((item for item in client.tab_sessions() if item.pane_id == pane), None)
            if tab is None:
                raise herdr.HerdrFailure("new recovery pane is unavailable")
            tab = replace(tab, session_id=identity)
        elif entry["name"]:
            with herdr.restore_staff(names.tab_record, labels=names.labels):
                _ = client.retitle(tab.pane_id, entry["name"])
        copied_from = trust.source if isinstance(trust, herdr.TrustCopied) else ""
        workers = ResumeWorkers(codex_seats([Path(run) for run in unit_runs.held(identity)]), entry["cwd"])
        message = resume_message(why, moved_from=source_label if to else None,
                                 moved_to=to, workers=workers)
        if os.environ.get("HERDR_PANE_ID") == tab.pane_id:
            if original is None:
                raise ValueError("resume has no original publication")
            with pane_record.locked(client.session):
                entries = pane_record.read_strict(client.session)
                if entries.get(tab.pane_id) != original.entry:
                    raise ValueError("retained launch changed before the resume attempt")
                pending = entry.copy()
                pending["session_id"] = original.entry["session_id"]
                entries[tab.pane_id] = pending
                pane_record.save(client.session, entries)
            attempt_pane = tab.pane_id
            _exec_in_own_pane(tab, entry, label, message)
        live = sessions.session_liveness(identity)
        if isinstance(live, sessions.SessionLivenessUnknown):
            raise herdr.HerdrFailure(live.reason)
        if isinstance(live, sessions.SessionNotLive):
            attempt_pane = tab.pane_id
            live = _start_session(tab, entry, label, message)
        else:
            observed = client.native_session(tab.pane_id, "claude", START_SECONDS)
            if observed != identity:
                raise herdr.HerdrFailure(f"resumed registry identity differs: {observed}")
        if original is None:
            raise ValueError("resume has no original publication")
        generation = sessions.process_start(live.pid)
        if not generation:
            raise herdr.HerdrFailure(f"process identity unreadable for pid {live.pid}")
        attempt = pane_launch.resume_attempt(original.path, tab.pane_id)
        proof = names.resume_proof(original.entry, entry)
        attempt_pane = ""
        pane_launch.finish_restore(original.path, original.entry, attempt, proof, close=client.close_tab_confirmed)
        _detach(["stay", "--session", identity, "--pane", tab.pane_id,
                 "--account", label, "--pid", str(live.pid), "--generation", generation, "--why", why,
                 *(["--requester", requester] if requester else [])], "stay")
        return ClaudeUp(identity, tab.pane_id, label, monotonic() - began, copied_from)
    except (OSError, ValueError, herdr.HerdrFailure, subprocess.SubprocessError) as error:
        if original is not None and attempt_pane:
            try:
                attempt = pane_launch.resume_attempt(original.path, attempt_pane)
                pane_launch.finish_restore(original.path, original.entry, attempt, pane_launch.FailedResume(),
                                           close=herdr.Herdr(original.path.stem).close_tab_confirmed)
            except (OSError, ValueError, herdr.HerdrFailure, subprocess.SubprocessError) as cleanup:
                return ResumeFailed(identity, f"{error}; cleanup failed: {cleanup}")
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
    import remote

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
        _ = quota_alert.tell_user(render("shutdown.restart_failed", account=account, machine=remote.local_machine(),
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
    """Interrupt all selected sessions before resuming any; isolate each failure."""
    if not 0 <= warn_seconds <= 15:
        raise ValueError("warn_seconds must be between 0 and 15")
    selected = list(dict.fromkeys(sessions))
    if not selected:
        return []
    began = monotonic()
    already_up: dict[str, ClaudeUp] = {}
    for session in selected:
        try:
            tab = _tab_of(session)
            if isinstance(tab, herdr.TabSession):
                running = _running_on_account(tab.session_id or "", tab.pane_id, target)
                if isinstance(running, ClaudeUp):
                    already_up[session] = running
        except (OSError, ValueError, herdr.HerdrFailure):
            pass  # The per-session stop below returns the read failure without affecting other members.
    _record_launches([session for session in selected if session not in already_up])
    caller = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if caller:
        try:
            tabs = [_tab_of(session) for session in selected]
            if any(session not in already_up and isinstance(tab, herdr.TabSession) and tab.session_id == caller
                   for session, tab in zip(selected, tabs)):
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
        stops = [pool.submit(already_up.__getitem__, session) if session in already_up else
                 pool.submit(_stop, session) for session in selected]
        stopped: list[Stopped | NotRunning | StopFailed | SessionNotFound | ClaudeUp] = []
        for session, future in zip(selected, stops):
            try:
                result = future.result()
                if isinstance(result, (Stopped, NotRunning)):
                    _retire_prompt(result.session_id)
                stopped.append(result)
            except Exception as error:
                stopped.append(StopFailed(session, str(error)))
        def resume_moved(identity: str) -> ResumeResult:
            result = (resume(identity, why=why, to=target, requester=requester) if requester else
                      resume(identity, why=why, to=target))
            return replace(result, elapsed_seconds=monotonic() - began) if isinstance(result, ClaudeUp) else result

        resumes = {index: pool.submit(resume_moved, result.session_id)
                   for index, result in enumerate(stopped) if isinstance(result, (Stopped, NotRunning))}
        outcomes: list[MoveResult] = []
        for index, result in enumerate(stopped):
            if isinstance(result, (StopFailed, SessionNotFound, ClaudeUp)):
                outcomes.append(result)
                continue
            try:
                resumed = resumes[index].result()
                if isinstance(resumed, (ClaudeUp, RunningOnAnotherAccount)):
                    outcomes.append(resumed)
                else:
                    reason = "session has no tab after stop" if isinstance(resumed, SessionNotFound) else resumed.reason
                    outcomes.append(ResumePending(result.session_id, target, reason))
            except Exception as error:
                outcomes.append(ResumePending(result.session_id, target, str(error)))
        return outcomes


def move(session: str, target: str, *, why: str) -> MoveResult:
    return move_sessions([session], target, why=why)[0]


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
