"""Stop a session, or resume it on its account in its herdr tab."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable, Generator, Mapping
from contextlib import ExitStack, contextmanager, redirect_stdout
from contextvars import ContextVar
from io import StringIO
from importlib import import_module
from threading import Event
from time import monotonic
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Never, Protocol, cast
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1]

from scripts.herdr import herdr
from scripts.accounts import accounts, moves
from scripts.production.staff import STAFF_ROOT
from scripts.whoami import agent_accounts, note_file
from scripts.lib import conf, operation_deadline
from scripts.message import session_send
from scripts.shutdown import member_account, pane_record
from scripts.shutdown.launch_names import LaunchNames, LaunchNamesNotRead
from scripts.shutdown.codex_trust import trust_arguments
from scripts.herdr import pane_launch
from scripts.lib.resume_message import (
    AccountMove, NO_ACCOUNT_MOVE, BusyPass, NO_OWN_PASS,
    ResumeWorkers as ResumeWorkers,
    NO_WORKERS as NO_WORKERS,
    resume_message as resume_message,
    resume_worker_command as resume_worker_command,
)
from scripts.shutdown.stop_results import (
    Stopped as Stopped, NotRunning as NotRunning, StopFailed as StopFailed,
    SESSION_WITHOUT_STAFF_ROW,
    StopUnproved as StopUnproved,
    ClaudeUp as ClaudeUp, ClaudeStartState, ResumePending as ResumePending, MoveHandedOff as MoveHandedOff, ResumeFailed as ResumeFailed,
    SessionNotFound as SessionNotFound, MoveResult as MoveResult, claude_up_line as claude_up_line,
    ResumeResult as ResumeResult, StopResult, SourceRelease, SourceReleased, SourceUnresolved, SourceReleaseUnknown,
)
from scripts.message import sessions
from scripts.shutdown.stop_results import shutdown_state_root

if TYPE_CHECKING:
    from scripts.herdr import worker_panes
    from scripts.production.staff import StaffRow, StaffSnapshot, ClaudeBackend, CodexBackend, BackendUnavailable, StaffId


class RowStarter(Protocol):
    def start_row(self, row: StaffRow, record: StaffSnapshot, client: herdr.Herdr, *,
                  target_account: str | accounts.AccountSelection) -> str: ...
    def session_launch(self, identity: str, name: str) -> tuple[Literal["claude", "codex"], dict[str, str], list[str]]: ...



_UNREAD_NAMES = LaunchNamesNotRead()
_NO_SOURCE_ACCOUNTS: Mapping[str, str] = MappingProxyType({})
_detached_moves: ContextVar[bool] = ContextVar("detached_moves", default=False)
_move_resume: ContextVar[bool] = ContextVar("move_resume", default=False)


@contextmanager
def detached_moves() -> Generator[None]:
    """Keep move batches outside the maintenance part's kill boundary."""
    token = _detached_moves.set(True)
    try:
        yield
    finally:
        _detached_moves.reset(token)


def _tab_of(session: str, *, names: LaunchNames | LaunchNamesNotRead = _UNREAD_NAMES) -> herdr.TabSession | SessionNotFound:
    """Find an existing pane and retain its saved conversation when the herdr id is stale."""
    from scripts.production import staff

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
    matches = [tab for tab in tabs if tab.session_id == session]
    if not matches:
        try:
            _ = staff.native_id(session)
        except staff.StaffRefused:
            matches = [tab for tab in tabs if tab.label == session]
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

    kind = "claude" if isinstance(backend, staff.ClaudeBackend) else "codex" if isinstance(backend, staff.CodexBackend) else ""
    result = stop(row.id, kind)
    if isinstance(result, SessionNotFound):
        return SourceUnresolved(f"{row.id}: lifecycle could not resolve the staff reference; check the staff row and retry")
    if isinstance(result, StopUnproved):
        herdr.conversation_note(herdr.UnknownConversation(result.reason))
    if isinstance(result, (NotRunning, StopUnproved)) and isinstance(backend, staff.CodexBackend):
        stop_mesh = cast(Callable[[staff.StaffRow, tuple[Path, ...]], SourceRelease],
                         import_module("scripts.agents.codex_mesh").stop_worker_for_switch)
        released = stop_mesh(row, runs())
        return released if isinstance(released, SourceReleased) else replace(released, reason=f"{released.reason}; inspect the source session and retry")
    if isinstance(result, StopFailed):
        return SourceReleaseUnknown(f"{result.reason}; inspect the source session and retry")
    return SourceReleased()


def stop(session: str, kind: str = "", *, deadline: float = math.inf) -> StopResult:
    from scripts.production import staff

    try:
        with operation_deadline.within(deadline), staff.row_operation(session):
            return _stop_recorded(session, kind)
    except TimeoutError as error:
        reason = str(error)
        return StopFailed(session, reason if "deadline passed" in reason else f"deadline passed; {reason}")


def _stop_recorded(session: str, kind: str = "") -> StopResult:
    """Stop on purpose; retain its launch for an explicit resume, not crash recovery."""
    from scripts.herdr import worker_panes
    from scripts.production import staff

    try:
        record = staff.initial_snapshot()
    except (OSError, ValueError):
        record = staff.StaffSnapshot(())
    row = record.by_member_reference(session)
    from scripts.lib import unit_runs
    parent = record.parent_of(row) if row is not None else None
    pane_worker = row is not None and row.kind == "worker" and (parent is None or
        unit_runs.worker_transport(worker_panes.worker_run(parent)) == "herdr")
    before = next((item for item in worker_panes.observe(row.parent.id, record) if item.row.id == row.id), None
                  ) if pane_worker and row is not None and isinstance(row.parent, staff.ReportsTo) else None
    if isinstance(before, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
        return StopFailed(str(before.row.current_session), before.reason)
    if isinstance(before, worker_panes.StoppedWorker) and _worker_stopped(before):
        result: StopResult = NotRunning(str(before.row.current_session))
    else:
        _record_launches([session])
        result = _stop(session, kind)
        if before is not None and isinstance(result, (Stopped, NotRunning)) and not _worker_stopped(before):
            return StopFailed(str(before.row.current_session), "worker stop has no fresh shell proof on the same terminal")
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


def _worker_stopped(before: worker_panes.WorkerPane) -> bool:
    """Prove the recorded worker pane directly, even when the director has no tab."""
    from scripts.herdr import worker_panes

    if isinstance(before, worker_panes.ClosedWorker):
        return True
    if isinstance(before, (worker_panes.OpenWorker, worker_panes.StoppedWorker)):
        client = herdr.Herdr(herdr.shared_session())
        try:
            return (any(pane["pane_id"] == before.pane and pane.get("terminal_id") == before.terminal for pane in client.panes())
                    and isinstance(client.pane_activity(before.pane), herdr.PaneAtShell)
                    and not isinstance(herdr.read_conversation(str(before.row.current_session)), herdr.LiveConversation))
        except (OSError, ValueError, herdr.HerdrFailure):
            return False
    return False


def _retire_prompt(session_id: str) -> None:
    """Publish only the session id and stop time; any failure is separate from stop or move."""
    try:
        from scripts.production.stall_watch import retire_session_prompt

        retire_session_prompt(session_id)
    except Exception as error:
        print(f"{session_id}: prompt cancellation: {error}", file=sys.stderr)


def stop_grace_seconds() -> float:
    return float(conf.read("shutdown").text("STOP_GRACE_SECONDS"))


def _stop(session: str, kind: str = "") -> StopResult:
    """Signal only the process that proves the current conversation."""
    identity = session
    last_signal = ""
    signal_pid = 0
    tab: herdr.TabSession | SessionNotFound = SessionNotFound(session)
    try:
        from scripts.production import staff

        settings = conf.read("shutdown")
        stop_grace, kill_grace = stop_grace_seconds(), float(settings.text("KILL_GRACE_SECONDS"))
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
        if not kind:
            try:
                publication = pane_record.publication_for_session(identity)
                kind = pane_launch.agent_kind(publication.entry) if publication is not None else ""
            except (OSError, ValueError) as error:
                print(f"{identity}: launch record anomaly: {' '.join(str(error).splitlines())}", file=sys.stderr)
                kind = ""
        observed = herdr.read_conversation(identity, kind)
        if isinstance(observed, herdr.StoppedConversation):
            return NotRunning(identity)
        if isinstance(observed, herdr.UnknownConversation):
            return StopUnproved(identity, observed.reason)
        if not observed.pids:
            return StopUnproved(identity, "conversation has no unique signal target; no signal sent")
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
        deadline = monotonic() + stop_grace
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
            deadline = monotonic() + kill_grace
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


def stop_unit_workers(unit: StaffId, record: StaffSnapshot) -> list[str]:
    """Stop the selected workers concurrently and retain every staff row."""
    from scripts.agents import worker_lifecycle
    from scripts.herdr import worker_panes
    from scripts.lib import unit_runs
    from scripts.shutdown import restart

    holder = record.by_id(unit)
    if not record.find(kind="worker", parent=unit) or holder is not None and unit_runs.worker_transport(worker_panes.worker_run(holder)) == "mesh":
        return []
    try:
        deadline = worker_lifecycle.effective_deadline()
        states = worker_panes.observe(unit, record)
        unresolved = [f"{item.row.name}: {item.reason}" for item in states
                      if isinstance(item, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker))]
        live = [item for item in states if isinstance(item, worker_panes.OpenWorker)]
        client = herdr.Herdr(herdr.shared_session())
        with ThreadPoolExecutor(max_workers=max(1, len(live))) as pool:
            futures = [(item, pool.submit(restart.stop_row, item.row, client, deadline=deadline)) for item in live]
            for item, future in futures:
                if not future.result():
                    unresolved.append(f"{item.row.name}: not proved stopped")
    except (OSError, ValueError, herdr.HerdrFailure) as error:
        unresolved = [f"{unit}: {' '.join(str(error).splitlines())}"]
        print(f"anomaly: {unresolved[0]}", file=sys.stderr)
    return unresolved


def resume_arguments(entry: pane_record.PaneLaunch) -> list[str]:
    return pane_record.resume_arguments(LaunchNames.read().resume(entry))


def _pane_command(tab: herdr.TabSession, entry: pane_record.PaneLaunch, account: str) -> list[str]:
    """Build the account command for both terminal paths."""
    client = herdr.Herdr(herdr.shared_session())
    pane = next((pane for pane in client.panes() if pane["pane_id"] == tab.pane_id), None)
    if pane is None:
        raise herdr.HerdrFailure(f"no pane {tab.pane_id!r}")
    unset = ["HERDR_TAB_ID", "STAFF_ID", "STAFF_OPERATION_FD", "PYTHONUNBUFFERED", "REFUSAL_WATCH_INVENTORY",
             *sorted(key for key in os.environ.keys() | entry["environment"].keys() if key.startswith("STAFF_ROW_FD_"))]
    environment = {key: value for key, value in entry["environment"].items() if key not in unset}
    environment = {**environment, "HERDR_PANE_ID": tab.pane_id,
                   "HERDR_WORKSPACE_ID": pane["workspace_id"], "RELAUNCH_PROMPT_DELIVERY": "pane"}
    kind = pane_launch.agent_kind(entry)
    program = [sys.executable, str(SCRIPTS / "accounts/accounts.py"), "exec", account]
    if kind == "codex":
        environment["CODEX_HOME"] = entry["config_dir"]
        program = ["codex"]
    return ["env", *(argument for key in unset for argument in ("-u", key)),
            *(f"{key}={value}" for key, value in environment.items()),
            "sh", "-c", 'cd -- "$1" && shift && exec "$@"', "resume", entry["cwd"],
            *program, *trust_arguments(kind, entry["cwd"], pane_record.resume_arguments(entry))]


def _exec_in_own_pane(tab: herdr.TabSession, entry: pane_record.PaneLaunch,
                      account: str, message: str) -> Never:
    """Replace the foreground command with the saved account launch."""
    command = _pane_command(tab, entry, account)
    os.chdir(entry["cwd"])
    os.execvp(command[0], [*command, *([message] if message and pane_launch.agent_kind(entry) == "claude" else [])])


def _prompt_identity(client: herdr.Herdr, pane: str, deadline: float) -> str:
    """Wait for the pane to expose its native identity within the start deadline."""
    while True:
        try:
            return client.native_session(pane, "claude", operation_deadline.remaining(max(0.001, deadline - monotonic())))
        except (OSError, ValueError, subprocess.SubprocessError, herdr.HerdrFailure):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise
            time.sleep(operation_deadline.remaining(min(0.5, remaining)))


def _start_session(tab: herdr.TabSession, entry: pane_record.PaneLaunch, account: str, message: str) -> sessions.LiveSessionProcess:
    """Run the account command in the session's own pane, with one resume message."""
    client = herdr.Herdr(herdr.shared_session())
    command = _pane_command(tab, entry, account)
    settings = conf.read("shutdown")
    start_seconds = settings.number("START_SECONDS")
    _ = settings.number("STAY_SECONDS")
    launched_at = datetime.now(timezone.utc)
    operation_deadline.check()
    client.run_in_pane(tab.pane_id, command)
    deadline = monotonic() + operation_deadline.remaining(start_seconds)
    live = sessions.session_liveness(entry["session_id"])
    while isinstance(live, sessions.SessionNotLive):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise herdr.HerdrFailure("never started")
        time.sleep(operation_deadline.remaining(min(0.5, remaining)))
        live = sessions.session_liveness(entry["session_id"])
    if isinstance(live, sessions.SessionLivenessUnknown):
        raise herdr.HerdrFailure(live.reason)
    if not message:
        return live
    try:
        identity = _prompt_identity(client, tab.pane_id, deadline)
    except (OSError, ValueError, subprocess.SubprocessError, herdr.HerdrFailure) as error:
        print(f"{entry['session_id']}: prompt identity anomaly: {error}", file=sys.stderr)
        return live
    if identity != entry["session_id"]:
        print(f"{entry['session_id']}: prompt identity anomaly: observed {identity}", file=sys.stderr)
        return live
    opening = herdr.OpeningPrompt(message, herdr.SessionTranscript(Path(entry["config_dir"]), identity))
    outcome = client.submit_opening_prompt(tab.pane_id, opening, launched_at, int(operation_deadline.remaining(start_seconds) * 1000))
    if isinstance(outcome, herdr.PromptNotSubmitted):
        print(f"{identity}: prompt anomaly: {outcome.reason}", file=sys.stderr)
    return live


def _codex_running_on_account(identity: str, pane_id: str, pid: int) -> ClaudeUp | ResumeFailed:
    """Read one process that the conversation reader proves hosts this thread."""
    from scripts.whoami import account
    from scripts.accounts import accounts

    setting = account.process_codex_home(pid)
    if isinstance(setting, account.ProcessEnvironmentUnread):
        return ResumeFailed(identity, setting.reason)
    home = setting.home if isinstance(setting, account.ProcessCodexHome) else Path.home() / ".codex"
    return ClaudeUp(identity, pane_id, accounts.codex_home_label(home), start_state=ClaudeStartState.ALREADY_RUNNING, pid=pid)


def running_on_account(identity: str, pane_id: str, *, kind: str = "claude") -> ClaudeUp | ResumeFailed | sessions.SessionNotLive:
    """Keep a running conversation on its current account."""
    if kind == "codex":
        observed = herdr.read_conversation(identity, "codex")
        if isinstance(observed, herdr.StoppedConversation):
            return sessions.SessionNotLive()
        if isinstance(observed, herdr.UnknownConversation):
            herdr.conversation_note(observed)
            client = herdr.Herdr(herdr.shared_session())
            for pane in client.panes():
                if pane["session_id"] == identity and not isinstance(client.pane_activity(pane["pane_id"]), herdr.PaneAtShell):
                    return ClaudeUp(identity, pane["pane_id"], "unknown account", start_state=ClaudeStartState.ALREADY_RUNNING)
            return sessions.SessionNotLive()
        if not observed.pids:
            return ResumeFailed(identity, "live Codex thread has no process account proof")
        return _codex_running_on_account(identity, pane_id, observed.pids[0])
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
    return ClaudeUp(identity, pane_id, label, start_state=ClaudeStartState.ALREADY_RUNNING, pid=live.pid)


def _member_machine(member: StaffRow) -> str:
    """Return the member's recorded machine, or an empty string when unknown."""
    from scripts.production import staff

    location = member.worktree if isinstance(member.worktree, (staff.PresentWorktree, staff.RemovedWorktree)) else member.doc
    if isinstance(location, (staff.PresentWorktree, staff.RemovedWorktree, staff.LiveDoc, staff.WrappedDoc)):
        return location.machine
    return ""


def _nearby_resume_names(reference: str, names: LaunchNames) -> list[str]:
    """Select close names from local units and showrunners."""
    from scripts.shutdown import remote

    machine = remote.local_machine()
    return sorted({row.name for row in names.tab_record.rows
                   if row.kind in {"unit", "showrunner"} and _member_machine(row) == machine
                   and reference.casefold() in row.name.casefold()})[:5]


def resume(session: str, *, why: str = "resumed by name", to: str | None = None, requester: str = "") -> ResumeResult:
    from scripts.production import staff

    try:
        settings = conf.read("shutdown")
        for key in ("START_SECONDS", "STAY_SECONDS"):
            _ = settings.number(key)
    except conf.Unusable as error:
        return ResumeFailed(session, str(error))
    with staff.row_operation(session):
        return _resume(session, why=why, to=to, requester=requester)


def _codex_name_ids(name: str) -> list[str]:
    """Keep a UUID or read exact names from the shared Codex index."""
    from scripts.production import staff

    try:
        _ = staff.native_id(name)
        return [name]
    except staff.StaffRefused:
        pass
    try:
        entries = [cast(dict[str, str], json.loads(line))
                   for line in (agent_accounts.codex_home() / "session_index.jsonl").read_text().splitlines() if line.strip()]
        return sorted({entry["id"] for entry in entries if entry.get("thread_name") == name})
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        print(f"{name}: index anomaly: {error}", file=sys.stderr)
        return []


def _unrecorded_codex_launch(identity: str) -> pane_record.PaneLaunch | SessionNotFound:
    """Read the native resume arguments and the folder from the rollout metadata."""
    path = next((agent_accounts.codex_home() / "sessions").glob(f"**/rollout-*{identity}.jsonl"), None)
    if path is None:
        return SessionNotFound(identity)
    restart = cast(RowStarter, cast(object, import_module("scripts.shutdown.restart")))
    try:
        kind, environment, arguments = restart.session_launch(identity, identity)
    except OSError:
        return SessionNotFound(identity)
    if kind != "codex":
        return SessionNotFound(identity)
    with path.open(encoding="utf-8") as stream:
        meta = herdr.json_object(cast(object, json.loads(stream.readline())), "Codex rollout metadata")
    folder = herdr.json_text(herdr.json_object(meta.get("payload"), "Codex session metadata").get("cwd"), "Codex rollout cwd")
    return pane_record.launch("", identity, folder, environment, [kind, *arguments], kind=kind)


def _resume(session: str, *, why: str = "resumed by name", to: str | None = None, requester: str = "") -> ResumeResult:
    names = LaunchNames.read()
    record = names.tab_record
    member = record.by_member_reference(session)
    matches = [row for row in record.rows if row.name == session or row == member]
    candidates = ", ".join(f"{row.name} ({row.id})" for row in matches[:5])
    if len(matches) > 1:
        return ResumeFailed(session, f"ambiguous name; candidates: {candidates}")
    if matches:
        from scripts.shutdown import remote

        member = matches[0]
        machine = _member_machine(member)
        if machine and machine != remote.local_machine():
            return ResumeFailed(session, f"session is on {machine}")
        session = member.current_session
    else:
        identities = _codex_name_ids(session)
        if len(identities) > 1:
            return ResumeFailed(session, "ambiguous name; candidates: " + ", ".join(identities))
        if identities:
            session = identities[0]
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
            if original is not None and any(pane["pane_id"] == original.entry["pane_id"] for pane in client.panes()):
                return tab
            entry = original.entry if original is not None else None
        else:
            identity = tab.session_id or identity
            entry = pane_record.read_strict(client.session).get(tab.pane_id)
            if entry is not None:
                original = pane_launch.LaunchPublication(pane_record.record_path(client.session), entry)
        if entry is None and row is None:
            recovered = _unrecorded_codex_launch(identity)
            if isinstance(recovered, SessionNotFound):
                if isinstance(tab, herdr.TabSession):
                    running = running_on_account(identity, tab.pane_id)
                    if not isinstance(running, sessions.SessionNotLive):
                        return running
                return recovered
            entry = recovered
        if entry is not None:
            entry = names.resume(entry)
        kind = pane_launch.agent_kind(entry) if entry is not None else "claude"
        pane_id = entry["pane_id"] if isinstance(tab, SessionNotFound) and entry is not None else (
            tab.pane_id if isinstance(tab, herdr.TabSession) else "")
        running = running_on_account(identity, pane_id, kind=kind)
        if not isinstance(running, sessions.SessionNotLive):
            return running
        if entry is None:
            if row is None:
                return SessionNotFound(session)
            restart = cast(RowStarter, cast(object, import_module("scripts.shutdown.restart")))
            pane = restart.start_row(row, names.tab_record, client, target_account=to or "")
            result = running_on_account(identity, pane)
            if isinstance(result, ClaudeUp):
                return replace(result, elapsed_seconds=monotonic() - began, start_state=ClaudeStartState.STARTED)
            return ResumeFailed(identity, "session did not start") if isinstance(result, sessions.SessionNotLive) else result
        entry = entry.copy()
        entry["session_id"] = identity
        entry["environment"] = names.environment(entry)
        from scripts.whoami.account import named_account_environment

        source = Path(entry["config_dir"])
        source_label = agent_accounts.codex_home_role_label(source) if kind == "codex" else accounts.claude_config_label(source)
        selected = member_account.resume_account(kind, row if row is not None else SESSION_WITHOUT_STAFF_ROW, to=to or "")
        copied_from = ""
        if isinstance(selected, accounts.CodexWorkersAccount):
            target_dir, label = selected.home, selected.label
            account_environment = {"CODEX_HOME": str(target_dir)}
        else:
            target_dir, label = selected.dir, selected.label
            account_environment = named_account_environment(target_dir)
            trust = heal_account_trust(Path(entry["cwd"]), target_dir, source, entry["name"])
            copied_from = trust.source if isinstance(trust, herdr.TrustCopied) else ""
        entry["config_dir"] = str(target_dir)
        entry["environment"] = {**entry["environment"], **account_environment}
        if original is not None and Path(original.entry["config_dir"]) != target_dir:
            try:
                pane_record.write(client.session, pane_launch.without_staff_labels(entry))
            except (OSError, ValueError) as error:
                print(f"{identity}: account record anomaly: {error}", file=sys.stderr)
        if isinstance(tab, SessionNotFound):
            space = herdr.row_workspace(row or STAFF_ROOT, names.tab_record, entry["name"])
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
        workers = NO_WORKERS
        own = NO_OWN_PASS
        try:
            from scripts.production import worker
            if row is not None and row.kind == "worker":
                own = worker.own_pass(row, names.tab_record)
            elif row is not None:
                workers = worker.resume_workers(row.id, names.tab_record)
        except (OSError, ValueError) as error:
            print(f"{identity}: workers anomaly: {error}", file=sys.stderr)
        message = resume_message(why, move=AccountMove(source_label, label) if source_label != label else NO_ACCOUNT_MOVE,
                                 workers=workers, own_pass=own) if (kind != "codex" or row is not None) and (
                                     row is None or row.kind != "worker" or isinstance(own, BusyPass)) else ""
        if os.environ.get("HERDR_PANE_ID") == tab.pane_id:
            with pane_record.locked(client.session):
                entries = pane_record.read_strict(client.session)
                pending = pane_launch.without_staff_labels(entry)
                pending["session_id"] = original.entry["session_id"] if original is not None else (
                    row.id if row is not None else identity)
                entries[tab.pane_id] = pending
                pane_record.save(client.session, entries)
            attempt_pane = tab.pane_id
            _exec_in_own_pane(tab, entry, label, message)
        started: herdr.AgentLaunched | sessions.LiveSessionProcess
        if kind == "codex":
            from scripts.lib import nix_env

            prepared = herdr.prepare_session_environment(identity, entry["cwd"], entry["environment"], herdr.NO_ENVIRONMENT_REMOVALS)
            if isinstance(prepared, nix_env.Refused):
                raise OSError(str(prepared))
            attempt_pane = tab.pane_id
            launched = herdr.launch_prepared_agent(client.session, herdr.ExistingPane(tab.pane_id, ""), prepared, kind,
                trust_arguments(kind, entry["cwd"], pane_record.resume_arguments(entry)), herdr.NoOpeningPrompt(),
                int(operation_deadline.remaining(conf.read("shutdown").number("START_SECONDS")) * 1000), agent=entry["name"] or identity)
            if isinstance(launched.outcome, herdr.PromptNotSubmitted):
                raise herdr.HerdrFailure(launched.outcome.reason)
            if message:
                client.prompt(tab.pane_id, message, target=identity)
            started = launched
        else:
            live = sessions.session_liveness(identity)
            if isinstance(live, sessions.SessionLivenessUnknown):
                raise herdr.HerdrFailure(live.reason)
            if isinstance(live, sessions.SessionNotLive):
                attempt_pane = tab.pane_id
                live = _start_session(tab, entry, label, message)
            started = live
        if original is not None:
            try:
                attempt = pane_launch.resume_attempt(original.path, tab.pane_id)
                proof = names.resume_proof(original.entry, entry)
                pane_launch.finish_restore(original.path, original.entry, attempt, proof, close=client.close_tab_confirmed)
            except (OSError, ValueError, herdr.HerdrFailure) as error:
                print(f"{identity}: launch record anomaly: {error}", file=sys.stderr)
        attempt_pane = ""
        if not _move_resume.get():
            _watch_resume(identity, tab.pane_id, label, started, requester, why)
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


def _watch_resume(identity: str, pane: str, account: str,
                  started: herdr.AgentLaunched | sessions.LiveSessionProcess, requester: str, why: str) -> None:
    try:
        if isinstance(started, herdr.AgentLaunched):
            observed = running_on_account(identity, pane, kind="codex")
            if not isinstance(observed, ClaudeUp) or not isinstance(observed.pid, int):
                raise OSError(observed.reason if isinstance(observed, ResumeFailed) else "process id is unavailable")
            pid = observed.pid
        else:
            pid = started.pid
        generation = sessions.process_start(pid)
        if generation:
            _detach(["stay", "--session", identity, "--pane", pane,
                     "--account", account, "--pid", str(pid), "--generation", generation, "--why", why,
                     *(["--requester", requester] if requester else [])], "stay")
        else:
            print(f"{identity}: observer anomaly: process generation is unavailable", file=sys.stderr)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"{identity}: observer anomaly: {error}", file=sys.stderr)


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


def _handoff_batch(selected: list[str], target: str, why: str, warn_seconds: int, cause: moves.MoveCause,
                   source_accounts: Mapping[str, str] = _NO_SOURCE_ACCOUNTS) -> None:
    # Options precede the positional list so session names cannot become options.
    _detach(["batch", "--target", target, "--why", why, "--cause", cause, "--warn-seconds", str(warn_seconds),
             *(arg for identity, label in source_accounts.items() for arg in ("--source-account", f"{identity}={label}")),
             "--", *selected], "move")


def _check_stay(identity: str, pane: str, account: str, pid: int, generation: str,
                requester: str = "", why: str = "") -> str:
    """Observe the started generation after the command returns; recovery belongs to the watchdog."""
    settings = conf.read("shutdown")
    stay_seconds = settings.number("STAY_SECONDS")
    began = monotonic()
    while _same_process(pid, generation):
        remaining = stay_seconds - (monotonic() - began)
        if remaining <= 0:
            return ""
        time.sleep(operation_deadline.remaining(min(0.1, remaining)))
    return _early_exit(identity, pane, account, monotonic() - began, requester=requester, why=why)


def _early_exit(identity: str, pane: str, account: str, elapsed: float, *, requester: str = "",
                why: str = "", failure: str = "") -> str:
    """Build one actionable outcome for the showrunner, user push and stay log."""
    from scripts.production import showrunners, staff, staff_sources
    from scripts.accounts import unit_watchdog
    from scripts.notify.push_texts import render
    from datetime import datetime, timezone
    from scripts.whoami import quota_alert
    from scripts.lib.probe_session import PROBE_ENV
    from scripts.shutdown import remote

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
        command = "agents resume " + shlex.join([name, "--to", account, "--why", why or "retry after early exit"])
        next_step = f"resume it: {command}"
    cause = (f"{name}: resume on {account} failed: {failure}; {next_step}" if failure else
             f"{name} stopped {elapsed:.1f} s after its move or resume to {account}; {next_step}")
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


def _request_move(reference: str, identity: str, target: str, why: str, cause: moves.MoveCause,
                  source: str, names: LaunchNames) -> moves.RequestedMove:
    """Record the source observed before stopping the session."""
    row = names.tab_record.by_member_reference(identity) or next(
        (member for member in names.tab_record.rows if member.name == reference), None)
    identity = str(row.current_session) if row is not None else identity
    label = note_file.parse_account_label(target)
    if not isinstance(label, note_file.NumberedAccountLabel):
        raise ValueError(f"invalid move target: {target}")
    tool = label.tool
    previous: moves.HistoricalAccountLabel = source or moves.HomeAccountUnresolved(Path.home() / f".{tool}")
    if not source:
        entries = [entry for entry in names.history if entry["session_id"] in
                   (identity, row.id if row is not None else identity) or entry["name"] == reference]
        saved = max(entries, key=lambda entry: entry["written_at"], default=None)
        if saved is not None and saved["config_dir"]:
            home = Path(saved["config_dir"])
            label = note_file.parse_account_label(home.name.removeprefix("."))
            previous = label.label if isinstance(label, note_file.NumberedAccountLabel) else moves.HomeAccountUnresolved(home)
    request: moves.RequestedMove = {"kind": "requested", "at": datetime.now(timezone.utc).isoformat(),
        "move_id": uuid4().hex, "tool": tool, "subject": f"{identity}: {row.name}" if row is not None else reference,
        "from": previous, "to": target, "why": why, "window": "unknown", "cause": cause}
    moves.append_move(request)
    return request


def _request_moves(selected: list[str], target: str, why: str, cause: moves.MoveCause,
                   identities: dict[str, str], sources: dict[str, str]) -> dict[str, moves.RequestedMove]:
    """Read launch names once and isolate each request failure."""
    requests: dict[str, moves.RequestedMove] = {}
    try:
        names = LaunchNames.read()
    except Exception as error:
        for session in selected:
            print(f"{identities.get(session, session)}: move log anomaly: {error}", file=sys.stderr)
        return requests
    for session in selected:
        identity = identities.get(session, session)
        try:
            requests[identity] = _request_move(session, identity, target, why, cause, sources.get(session, ""), names)
        except Exception as error:
            print(f"{identity}: move log anomaly: {error}", file=sys.stderr)
    return requests


def _record_move_outcome(request: moves.RequestedMove, result: ClaudeUp | StopFailed | SessionNotFound | ResumePending) -> None:
    """Record one final outcome without losing the batch results."""
    try:
        entry: moves.ConfirmedMove | moves.FailedMove
        if isinstance(result, ClaudeUp):
            from scripts.accounts import accounts

            entry = moves.ConfirmedMove(kind="confirmed", at=datetime.now(timezone.utc).isoformat(),
                move_id=request["move_id"], tool=request["tool"], subject=request["subject"],
                worker=request["subject"].split(": ", 1)[-1], config_dir=str(accounts.account_dir(result.account)))
        else:
            reason = f"{result.name}: session not found" if isinstance(result, SessionNotFound) else result.reason
            entry = moves.FailedMove(kind="failed", at=datetime.now(timezone.utc).isoformat(),
                move_id=request["move_id"], tool=request["tool"], subject=request["subject"], reason=reason)
        moves.append_move(entry)
    except Exception as error:
        identity = result.name if isinstance(result, SessionNotFound) else result.session_id
        print(f"{identity}: move log anomaly: {error}", file=sys.stderr)


def _confirm_move_start(result: ClaudeUp, requester: str, why: str) -> ClaudeUp | ResumeFailed:
    """Keep the existing stay observation inside the move's outcome."""
    identity = result.session_id
    live = sessions.session_liveness(identity)
    failure = (_check_stay(identity, result.pane_id, result.account, live.pid,
                           sessions.process_start(live.pid) or "", requester, why)
               if isinstance(live, sessions.LiveSessionProcess) else
               _early_exit(identity, result.pane_id, result.account, 0.0, requester=requester, why=why))
    return ResumeFailed(identity, failure) if failure else result


def _resume_move(identity: str, target: str, requester: str, why: str, began: float) -> ResumeResult:
    token = _move_resume.set(True)
    try:
        try:
            result = (resume(identity, why=why, to=target, requester=requester) if requester else
                      resume(identity, why=why, to=target))
        except Exception as error:
            result = ResumeFailed(identity, str(error))
        if isinstance(result, ClaudeUp):
            return _confirm_move_start(replace(result, elapsed_seconds=monotonic() - began), requester, why)
        reason = "session has no tab after stop" if isinstance(result, SessionNotFound) else result.reason
        _ = _early_exit(identity, "", target, 0.0, requester=requester, why=why, failure=reason)
        return result
    finally:
        _move_resume.reset(token)


def move_sessions(sessions: list[str], target: str, *, why: str, warn_seconds: int = 0,
                  requester: str = "", cause: moves.MoveCause = "by hand",
                  source_accounts: Mapping[str, str] = _NO_SOURCE_ACCOUNTS) -> list[MoveResult]:
    """Interrupt all selected sessions before resuming any; isolate each failure."""
    warn_seconds = max(0, warn_seconds)
    selected = list(dict.fromkeys(sessions))
    if not selected:
        return []
    if _detached_moves.get():
        _handoff_batch(selected, target, why, warn_seconds, cause, source_accounts)
        return [MoveHandedOff(identity) for identity in selected]
    began = monotonic()
    already_up: dict[str, ClaudeUp] = {}
    sources: dict[str, str] = dict(source_accounts)
    identities: dict[str, str] = {}
    for session in selected:
        try:
            tab = _tab_of(session)
            if isinstance(tab, herdr.TabSession):
                identities[session] = tab.session_id or session
                running = running_on_account(tab.session_id or "", tab.pane_id)
                if isinstance(running, ClaudeUp):
                    _ = sources.setdefault(session, running.account)
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
                _handoff_batch(selected, target, why, warn_seconds, cause, sources)
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
            requests = _request_moves([session for session in selected if session not in already_up],
                                      target, why, cause, identities, sources)
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
            resumes = {pool.submit(_resume_move, result.session_id, target, requester, why, began): (index, result.session_id)
                       for index, result in enumerate(stopped) if isinstance(result, (Stopped, NotRunning))}
            outcomes: dict[int, ClaudeUp | StopFailed | SessionNotFound | ResumePending] = {}
            for index, result in enumerate(stopped):
                if isinstance(result, (StopFailed, ClaudeUp, SessionNotFound)):
                    outcomes[index] = result
                    identity = result.name if isinstance(result, SessionNotFound) else result.session_id
                    if identity in requests:
                        _record_move_outcome(requests[identity], result)
            for future in as_completed(resumes):
                index, identity = resumes[future]
                try:
                    resumed = future.result()
                    if isinstance(resumed, ClaudeUp):
                        outcome = resumed
                    elif isinstance(resumed, SessionNotFound):
                        outcome = ResumePending(identity, target, "session has no tab after stop")
                    else:
                        outcome = ResumePending(identity, target, resumed.reason)
                except Exception as error:
                    outcome = ResumePending(identity, target, str(error))
                outcomes[index] = outcome
                if identity in requests:
                    _record_move_outcome(requests[identity], outcome)
            return [outcomes[index] for index in range(len(selected))]


def move(session: str, target: str, *, why: str) -> MoveResult:
    return move_sessions([session], target, why=why)[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    batch = commands.add_parser("batch")
    _ = batch.add_argument("--requester", default="")
    _ = batch.add_argument("--target", required=True)
    _ = batch.add_argument("--why", default="account move")
    _ = batch.add_argument("--cause", choices=("by hand", "refusal", "auto", "credits off"), default="by hand")
    _ = batch.add_argument("--warn-seconds", type=int, default=0)
    _ = batch.add_argument("--source-account", action="append", default=[])
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
                                requester=cast(str, args.requester), cause=cast(moves.MoveCause, args.cause),
                                source_accounts={identity: label for item in cast(list[str], args.source_account)
                                                 for identity, _, label in (item.partition("="),)})
        for result in results:
            _ = output.write(json.dumps({"result": type(result).__name__, **asdict(result)}) + "\n")
    return int(any(not isinstance(result, ClaudeUp) for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
