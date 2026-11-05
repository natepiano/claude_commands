"""Stop a session, or resume it on its account in its herdr tab."""

from __future__ import annotations

import math
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
from collections.abc import Callable
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from importlib import import_module
from threading import Event
from time import monotonic
from typing import TYPE_CHECKING, Never, Protocol, cast
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS.parent))
for dependency in ("shutdown", "herdr", "message", "whoami", "lib"):
    sys.path.insert(0, str(SCRIPTS / dependency))

import herdr
from scripts.lib import operation_deadline
from scripts.message import session_send
import pane_record
from scripts.shutdown.launch_names import LaunchNames, LaunchNamesNotRead
from scripts.shutdown.codex_trust import trust_arguments
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
    ResumeResult as ResumeResult, StopResult, SourceRelease, SourceReleased, SourceUnresolved, SourceReleaseUnknown,
)
import sessions
from scripts.shutdown.stop_results import shutdown_state_root

if TYPE_CHECKING:
    from scripts.production.staff import StaffRow, StaffSnapshot, ClaudeBackend, CodexBackend, BackendUnavailable


class RowStarter(Protocol):
    def start_row(self, row: StaffRow, record: StaffSnapshot, client: herdr.Herdr, *,
                  target_account: str) -> str: ...

STOP_GRACE_SECONDS = 2.0
KILL_GRACE_SECONDS = 5.0
START_SECONDS = 20.0
STAY_SECONDS = 10.0


_UNREAD_NAMES = LaunchNamesNotRead()


def _tab_of(session: str, *, names: LaunchNames | LaunchNamesNotRead = _UNREAD_NAMES) -> herdr.TabSession | SessionNotFound:
    """Find an existing pane, retaining its saved conversation when herdr's id is stale."""
    client = herdr.Herdr(herdr.shared_session())
    entries = pane_record.read(client.session)
    membership = (LaunchNames.read() if isinstance(names, LaunchNamesNotRead) else names).tab_record
    tabs: list[herdr.TabSession] = []
    for pane in client.panes():
        identity = pane["session_id"]
        entry = entries.get(pane["pane_id"])
        owner = membership.by_member_reference(entry["session_id"]) if entry is not None else None
        saved_identity = owner.current_session if owner is not None else entry["session_id"] if entry is not None else ""
        label = pane.get("label") or ""
        if entry is not None and saved_identity != identity and (
                session in (entry["session_id"], saved_identity) or session == label):
            if identity:
                live = herdr.read_conversation(identity)
                if isinstance(live, herdr.UnknownConversation):
                    herdr.conversation_note(live)
                if isinstance(live, herdr.LiveConversation):
                    continue  # A saved entry cannot displace another running conversation.
            identity = saved_identity
        tabs.append(herdr.TabSession(pane["pane_id"], label, pane["workspace_id"], identity))
    matches = [tab for tab in tabs if tab.session_id == session] or [tab for tab in tabs if tab.label == session]
    if len(matches) > 1:
        raise ValueError(f"{session}: several tabs match")
    return matches[0] if matches and matches[0].session_id else SessionNotFound(session)


def _same_process(pid: int, started: str) -> bool:
    """Check the saved process and start time after registry removal."""
    identity = sessions.process_liveness(pid, started)
    if isinstance(identity, sessions.SessionLivenessUnknown):
        raise OSError(identity.reason)
    return isinstance(identity, sessions.LiveSessionProcess)


def _signal_process(pid: int, number: int) -> None:
    """Refuse proof signals unless herdr places the process in the selected workspace."""
    if number != 0 and "SHUTDOWN_PROOF_WORKSPACE" not in os.environ and (
            "STAFF_FILE" in os.environ or "SHUTDOWN_STATE_DIR" in os.environ):
        raise PermissionError(
            f"signal {number} to pid {pid} refused: isolated records require SHUTDOWN_PROOF_WORKSPACE")
    if number != 0 and "SHUTDOWN_PROOF_WORKSPACE" in os.environ:
        workspace = os.environ["SHUTDOWN_PROOF_WORKSPACE"]
        refusal = f"proof workspace {workspace!s}: signal {number} to pid {pid} refused"
        try:
            if not workspace or pid <= 0:
                raise ValueError("expected a workspace id and a positive pid")
            client = herdr.Herdr(herdr.shared_session())
            for pane in client.panes():
                if pane["workspace_id"] != workspace:
                    continue
                info = herdr.json_object(client.result(
                    "pane", "process-info", "--pane", pane["pane_id"]).get("process_info"), "proof process info")
                if any(isinstance(observed := herdr.json_object(process, "proof pane process").get("pid"), int)
                       and not isinstance(observed, bool) and observed == pid
                       for process in herdr.json_items(info.get("foreground_processes"), "proof pane processes")):
                    break
            else:
                raise ValueError("herdr did not place the pid in a workspace pane")
        except (OSError, ValueError, herdr.HerdrFailure) as error:
            raise PermissionError(f"{refusal}: {error}") from error
    if number in (signal.SIGTERM, signal.SIGKILL):
        operation_deadline.check()
    os.kill(pid, number)


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
    except (OSError, ValueError, herdr.HerdrFailure) as error:
        print(f"stop: launch note anomaly: {error}", file=sys.stderr)


def release_source(row: StaffRow, runs: Callable[[], tuple[Path, ...]], backend: ClaudeBackend | CodexBackend | BackendUnavailable) -> SourceRelease:
    """Resolve native stop evidence before creating the destination."""
    from scripts.production import staff

    result = stop(row.id)
    if isinstance(result, SessionNotFound):
        return SourceUnresolved(f"{row.id}: lifecycle could not resolve the staff reference; check the staff row and retry")
    if isinstance(result, StopFailed):
        return SourceReleaseUnknown(f"{result.reason}; inspect the source session and retry")
    if isinstance(result, NotRunning) and isinstance(backend, staff.CodexBackend):
        stop_mesh = cast(Callable[[staff.StaffRow, tuple[Path, ...]], SourceRelease],
                         import_module("scripts.agents.codex_mesh").stop_worker_for_switch)
        released = stop_mesh(row, runs())
        return released if isinstance(released, SourceReleased) else replace(released, reason=f"{released.reason}; inspect the source session and retry")
    return SourceReleased()


def stop(session: str, *, deadline: float = math.inf) -> StopResult:
    from scripts.production import staff

    try:
        with operation_deadline.within(deadline), staff.row_operation(session):
            return _stop_recorded(session)
    except TimeoutError as error:
        reason = str(error)
        return StopFailed(session, reason if "deadline passed" in reason else f"deadline passed; {reason}")


def _stop_recorded(session: str) -> StopResult:
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
            print(f"{result.session_id}: stop note anomaly: {error}", file=sys.stderr)
    return result


def _retire_prompt(session_id: str) -> None:
    """Publish only the session id and stop time; any failure is separate from stop or move."""
    try:
        from scripts.production.stall_watch import retire_session_prompt

        retire_session_prompt(session_id)
    except Exception as error:
        print(f"{session_id}: prompt cancellation: {error}", file=sys.stderr)


def _stop(session: str) -> StopResult:
    """Signal only the process that proves the current conversation."""
    identity = session
    last_signal = ""
    signal_pid = 0
    tab: herdr.TabSession | SessionNotFound = SessionNotFound(session)
    try:
        from scripts.production import staff

        try:
            member = staff.initial_snapshot().by_member_reference(session)
        except (OSError, ValueError) as error:
            print(f"{session}: staff anomaly: {error}", file=sys.stderr)
            member = None
        if member is not None:
            identity = member.current_session
        else:
            tab = _tab_of(session)
            if isinstance(tab, herdr.TabSession) and tab.session_id:
                identity = tab.session_id
            else:
                return SessionNotFound(session)
        observed = herdr.read_conversation(identity)
        if isinstance(observed, herdr.StoppedConversation):
            return NotRunning(identity)
        if isinstance(observed, herdr.UnknownConversation):
            herdr.conversation_note(observed)
            return StopFailed(identity, observed.reason)
        if len(observed.pids) != 1:
            return StopFailed(identity, "conversation has no unique signal target; no signal sent")
        live = sessions.LiveSessionProcess(observed.pids[0])
        kind = observed.kind
        proved = herdr.native_process(live.pid, kind)
        if proved.current_identity != identity:
            return StopFailed(identity, "process runs another conversation; no signal sent")
        started = proved.started
        if not _same_process(live.pid, started):
            return NotRunning(identity)
        if not herdr.check_native_target(live.pid, kind, proved):
            return StopFailed(identity, "process identity changed; no signal sent")
        _signal_process(live.pid, signal.SIGTERM)
        last_signal = "SIGTERM"
        signal_pid = live.pid
        deadline = monotonic() + STOP_GRACE_SECONDS
        while _same_process(live.pid, started):
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            _ = Event().wait(operation_deadline.remaining(min(0.1, remaining)))
        else:
            return Stopped(identity, live.pid)
        if _same_process(live.pid, started):
            try:
                if not herdr.check_native_target(live.pid, kind, proved):
                    return StopFailed(identity, "process identity changed; no signal sent")
            except ProcessLookupError:
                return Stopped(identity, live.pid)
            except (OSError, ValueError, herdr.HerdrFailure) as error:
                print(f"{identity}: stop identity anomaly after SIGTERM: {error}; waiting for exit", file=sys.stderr)
            else:
                _signal_process(live.pid, signal.SIGKILL)
                last_signal = "SIGKILL"
            deadline = monotonic() + KILL_GRACE_SECONDS
            while _same_process(live.pid, started):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return StopFailed(identity, f"pid {live.pid} is still running after {last_signal}")
                _ = Event().wait(operation_deadline.remaining(min(0.1, remaining)))
        return Stopped(identity, live.pid)
    except ProcessLookupError:
        return NotRunning(identity)
    except (OSError, ValueError, herdr.HerdrFailure) as error:
        reason = str(error)
        if isinstance(error, TimeoutError) and last_signal:
            reason += f"; pid {signal_pid} received {last_signal} and still runs"
        return StopFailed(identity, reason)


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
            *trust_arguments(pane_launch.agent_kind(entry), entry["cwd"], pane_record.resume_arguments(entry))]


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
    try:
        identity = client.native_session(tab.pane_id, "claude", START_SECONDS)
    except (OSError, ValueError, herdr.HerdrFailure) as error:
        print(f"{entry['session_id']}: prompt identity anomaly: {error}", file=sys.stderr)
        return live
    if identity != entry["session_id"]:
        print(f"{entry['session_id']}: prompt identity anomaly: observed {identity}", file=sys.stderr)
        return live
    opening = herdr.OpeningPrompt(message, herdr.SessionTranscript(Path(entry["config_dir"]), identity))
    outcome = client.submit_opening_prompt(tab.pane_id, opening, launched_at, int(START_SECONDS * 1000))
    if isinstance(outcome, herdr.PromptNotSubmitted):
        print(f"{identity}: prompt anomaly: {outcome.reason}", file=sys.stderr)
    return live


def _running_on_account(identity: str, pane_id: str) -> ClaudeUp | ResumeFailed | sessions.SessionNotLive:
    """Keep a running conversation on its current account."""
    live = sessions.session_liveness(identity)
    if isinstance(live, sessions.SessionNotLive):
        return live
    if isinstance(live, sessions.SessionLivenessUnknown):
        return ResumeFailed(identity, live.reason)
    from scripts.whoami import account
    from scripts.accounts import accounts

    label = "unknown account"
    try:
        setting = account.process_account_directory(account.process_config_dir(live.pid))
        if isinstance(setting, account.ConfiguredClaudeFolder):
            label = accounts.claude_config_label(setting.path)
    except (OSError, ValueError) as error:
        print(f"{identity}: account anomaly: {error}", file=sys.stderr)
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


def resume(session: str, *, why: str = "resumed by name", to: str | None = None, requester: str = "") -> ResumeResult:
    from scripts.production import staff

    with staff.row_operation(session):
        return _resume(session, why=why, to=to, requester=requester)


def _resume(session: str, *, why: str = "resumed by name", to: str | None = None, requester: str = "") -> ResumeResult:
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
    result = _resume_once(session, why=why, to=to, requester=requester, names=names)
    if isinstance(result, SessionNotFound) and not matches:
        nearby = _nearby_resume_names(session, names)
        suffix = "; candidates: " + ", ".join(nearby) if nearby else ""
        return ResumeFailed(session, "unknown name" + suffix)
    return result


def heal_account_trust(
    folder: Path, target: Path, source: Path, name: str,
) -> herdr.Trusted | herdr.TrustCopied | herdr.Untrusted | herdr.UnreadableTrustFile:
    from scripts.accounts import accounts

    trust = herdr.heal_claude_trust(folder, target / ".claude.json",
        accounts.trust_sources(target, source), dry_run=False)
    if isinstance(trust, (herdr.Untrusted, herdr.UnreadableTrustFile)):
        reason = str(trust) if isinstance(trust, herdr.UnreadableTrustFile) else f"Claude has not trusted {folder}"
        print(f"{name}: trust anomaly: {reason}", file=sys.stderr)
    return trust


def _resume_once(session: str, *, why: str, to: str | None = None, requester: str = "",
                 names: LaunchNames) -> ResumeResult:
    began = monotonic()
    identity = session
    original: pane_launch.LaunchPublication | None = None
    attempt_pane = ""
    try:
        tab = _tab_of(session, names=names)
        client = herdr.Herdr(herdr.shared_session())
        row = names.tab_record.by_current_session(identity)
        if isinstance(tab, SessionNotFound):
            original = pane_record.publication_for_session(session, record=names.tab_record)
            if original is None and row is None:
                return tab
            if original is not None and any(pane["pane_id"] == original.entry["pane_id"] for pane in client.panes()):
                return tab
            entry = original.entry if original is not None else None
        else:
            identity = tab.session_id or identity
            entry = pane_record.read_strict(client.session).get(tab.pane_id)
            if entry is not None:
                original = pane_launch.LaunchPublication(pane_record.record_path(client.session), entry)
        if entry is not None:
            entry = names.resume(entry)
        pane_id = entry["pane_id"] if isinstance(tab, SessionNotFound) and entry is not None else (
            tab.pane_id if isinstance(tab, herdr.TabSession) else "")
        running = _running_on_account(identity, pane_id)
        if not isinstance(running, sessions.SessionNotLive):
            return running
        if entry is None:
            if row is None:
                return SessionNotFound(session)
            restart = cast(RowStarter, cast(object, import_module("scripts.shutdown.restart")))
            pane = restart.start_row(row, names.tab_record, client, target_account=to or "")
            result = _running_on_account(identity, pane)
            if isinstance(result, ClaudeUp):
                return replace(result, elapsed_seconds=monotonic() - began, start_state=ClaudeStartState.STARTED)
            return ResumeFailed(identity, "session did not start") if isinstance(result, sessions.SessionNotLive) else result
        entry = entry.copy()
        entry["session_id"] = identity
        entry["environment"] = names.environment(entry)
        from inventory import codex_seats
        import unit_runs

        from scripts.accounts import accounts
        from scripts.whoami.account import named_account_environment

        if to is None and row is not None and row.kind in ("unit", "showrunner") and pane_launch.agent_kind(entry) == "claude":
            to = accounts.role_account(row.kind).label
        source = Path(entry["config_dir"])
        source_label = accounts.claude_config_label(source)
        target_dir = (accounts.account_dir(to) if to is not None else
                      Path(entry["environment"].get("CLAUDE_CONFIG_DIR", str(source))))
        label = to if to is not None else accounts.claude_config_label(target_dir)
        account_environment = named_account_environment(target_dir)
        trust = heal_account_trust(Path(entry["cwd"]), target_dir, source, entry["name"])
        entry["config_dir"] = str(target_dir)
        entry["environment"] = {**entry["environment"], **account_environment}
        if to is not None and original is not None and Path(original.entry["config_dir"]) != target_dir:
            try:
                pane_record.write(client.session, entry)
            except (OSError, ValueError) as error:
                print(f"{identity}: account record anomaly: {error}", file=sys.stderr)
        if isinstance(tab, SessionNotFound):
            workspace = row
            while workspace is not None and workspace.kind != "showrunner":
                workspace = names.tab_record.parent_of(workspace)
            space = workspace.name if workspace is not None else entry["name"] or "agents"
            with herdr.restore_staff(names.tab_record, labels=names.labels):
                pane = client.open_tab(space,
                    entry["name"] or identity, entry["cwd"], entry["environment"])
            entry = entry.copy()
            entry["pane_id"] = pane
            attempt_pane = pane
            tab = next((item for item in client.tab_sessions() if item.pane_id == pane), None)
            if tab is None:
                raise herdr.HerdrFailure("new recovery pane is unavailable")
            tab = replace(tab, session_id=identity)
        elif entry["name"]:
            try:
                with herdr.restore_staff(names.tab_record, labels=names.labels):
                    _ = client.retitle(tab.pane_id, entry["name"])
            except (OSError, ValueError, herdr.HerdrFailure) as error:
                print(f"{identity}: title anomaly: {error}", file=sys.stderr)
        copied_from = trust.source if isinstance(trust, herdr.TrustCopied) else ""
        workers = NO_WORKERS
        try:
            workers = ResumeWorkers(codex_seats([Path(run) for run in unit_runs.held(identity)]), entry["cwd"])
        except (OSError, ValueError) as error:
            print(f"{identity}: workers anomaly: {error}", file=sys.stderr)
        message = resume_message(why, moved_from=source_label if to else None,
                                 moved_to=to, workers=workers)
        if os.environ.get("HERDR_PANE_ID") == tab.pane_id:
            with pane_record.locked(client.session):
                entries = pane_record.read_strict(client.session)
                pending = entry.copy()
                pending["session_id"] = identity
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
        if original is not None:
            try:
                attempt = pane_launch.resume_attempt(original.path, tab.pane_id)
                proof = names.resume_proof(original.entry, entry)
                pane_launch.finish_restore(original.path, original.entry, attempt, proof, close=client.close_tab_confirmed)
            except (OSError, ValueError, herdr.HerdrFailure) as error:
                print(f"{identity}: launch record anomaly: {error}", file=sys.stderr)
        attempt_pane = ""
        try:
            generation = sessions.process_start(live.pid)
            if generation:
                _detach(["stay", "--session", identity, "--pane", tab.pane_id,
                         "--account", label, "--pid", str(live.pid), "--generation", generation, "--why", why,
                         *(["--requester", requester] if requester else [])], "stay")
            else:
                print(f"{identity}: observer anomaly: process generation is unavailable", file=sys.stderr)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"{identity}: observer anomaly: {error}", file=sys.stderr)
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
        requester = showrunners.own_name()
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
    while _same_process(pid, generation):
        remaining = STAY_SECONDS - (monotonic() - began)
        if remaining <= 0:
            return ""
        time.sleep(min(0.1, remaining))
    return _early_exit(identity, pane, account, monotonic() - began, requester=requester, why=why)


def _early_exit(identity: str, pane: str, account: str, elapsed: float, *, requester: str = "",
                why: str = "") -> str:
    """Build one actionable outcome for the showrunner, user push and stay log."""
    from scripts.production import showrunners, staff, staff_sources
    from scripts.accounts import unit_watchdog
    from scripts.notify.push_texts import render
    from datetime import datetime, timezone
    import quota_alert
    from probe_session import PROBE_ENV
    import remote

    try:
        record = staff.snapshot()
    except (staff.StaffUnreadable, ValueError) as error:
        print(f"{identity}: staff anomaly: {error}", file=sys.stderr)
        record = staff.StaffSnapshot(())
    row = record.by_current_session(identity)
    entry = pane_record.read(herdr.shared_session()).get(pane)
    entry = entry if entry and (entry["session_id"] == identity
        or row is not None and record.by_member_reference(entry["session_id"]) == row) else None
    environment = entry["environment"] if entry else {}
    name = entry["name"] if entry else ""
    try:
        tab = _tab_of(identity)
        if isinstance(tab, herdr.TabSession):
            name = tab.label or name
    except (OSError, ValueError, herdr.HerdrFailure):
        pass  # The saved launch name still identifies a session whose pane is gone.
    name = name or identity
    unit = row.name if row is not None and row.kind == "unit" else ""
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
        name = staff_sources.unit_parent(unit, identity).name if unit else requester
        if name:
            print(showrunners.tell(name, f"resume-ended-{identity}-{uuid4().hex}", cause, sender="shutdown"))
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
    _ = subprocess.run(session_send.send_argv("--to", f"session:{tab.session_id}", "--from", "shutdown",
        "--text", f"Account move in {seconds} seconds: {why}. You will resume on {target}; continue your work and workers."),
        capture_output=True, timeout=15, check=False)


def move_sessions(sessions: list[str], target: str, *, why: str, warn_seconds: int = 0,
                  requester: str = "") -> list[MoveResult]:
    """Interrupt all selected sessions before resuming any; isolate each failure."""
    warn_seconds = max(0, warn_seconds)
    selected = list(dict.fromkeys(sessions))
    if not selected:
        return []
    began = monotonic()
    already_up: dict[str, ClaudeUp] = {}
    for session in selected:
        try:
            tab = _tab_of(session)
            if isinstance(tab, herdr.TabSession):
                running = _running_on_account(tab.session_id or "", tab.pane_id)
                if isinstance(running, ClaudeUp) and running.account == target:
                    already_up[session] = running
        except (OSError, ValueError, herdr.HerdrFailure):
            pass  # The per-session stop below returns the read failure without affecting other members.
    _record_launches([session for session in selected if session not in already_up])
    handoff_failures: dict[str, StopFailed] = {}
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
            print(f"{caller}: move handoff anomaly: {error}", file=sys.stderr)
            for session in selected:
                tab = _tab_of(session)
                if isinstance(tab, herdr.TabSession) and tab.session_id == caller:
                    handoff_failures[session] = StopFailed(caller, str(error))
    from scripts.production import staff

    with ExitStack() as locks:
        for session in selected:
            _ = locks.enter_context(staff.row_operation(session))
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
            stops = [pool.submit(handoff_failures.__getitem__, session) if session in handoff_failures else
                     pool.submit(already_up.__getitem__, session) if session in already_up else
                     pool.submit(_stop, session) for session in selected]
            stopped: list[StopResult | ClaudeUp] = []
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
                if isinstance(result, (StopFailed, ClaudeUp, SessionNotFound)):
                    outcomes.append(result)
                    continue
                identity = result.session_id
                try:
                    resumed = resumes[index].result()
                    if isinstance(resumed, ClaudeUp):
                        outcomes.append(resumed)
                    elif isinstance(resumed, SessionNotFound):
                        outcomes.append(ResumePending(identity, target, "session has no tab after stop"))
                    else:
                        outcomes.append(ResumePending(identity, target, resumed.reason))
                except Exception as error:
                    outcomes.append(ResumePending(identity, target, str(error)))
            record = staff.initial_snapshot()
            for index, (session, outcome) in enumerate(zip(selected, outcomes)):
                identity = outcome.name if isinstance(outcome, SessionNotFound) else outcome.session_id
                running = _running_on_account(identity, "")
                if isinstance(running, ClaudeUp) and running.account == target:
                    continue
                member = record.by_member_reference(identity)
                name = member.name if member is not None else session
                line = (f"{name}: not running on {target} after the move; resume it with: "
                        + f"accounts.py move {identity} {target}")
                print(line, file=sys.stderr)
                if isinstance(outcome, ClaudeUp):
                    outcomes[index] = ResumePending(identity, target, line)
            return outcomes


def move(session: str, target: str, *, why: str) -> MoveResult:
    return move_sessions([session], target, why=why)[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    batch = commands.add_parser("batch")
    _ = batch.add_argument("--requester", default="")
    _ = batch.add_argument("--target", required=True)
    _ = batch.add_argument("--why", default="account move")
    _ = batch.add_argument("--warn-seconds", type=int, default=0)
    _ = batch.add_argument("--log", type=Path, default=shutdown_state_root() / "move.log")
    _ = batch.add_argument("sessions", nargs="+")
    stay = commands.add_parser("stay")
    for option in ("session", "pane", "account", "generation"):
        _ = stay.add_argument("--" + option, required=True)
    _ = stay.add_argument("--requester", default="")
    _ = stay.add_argument("--why", default="")
    _ = stay.add_argument("--pid", type=int, required=True)
    _ = stay.add_argument("--log", type=Path, default=shutdown_state_root() / "stay.log")
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
