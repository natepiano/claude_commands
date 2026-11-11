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
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Generator, Mapping
from contextlib import ExitStack, contextmanager, redirect_stdout
from contextvars import ContextVar
from io import StringIO
from importlib import import_module
from threading import Event, Lock
from time import monotonic
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Never, Protocol, cast
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1]

from scripts.herdr import herdr
from scripts.herdr import herdr_types
from scripts.accounts import accounts, moves
from scripts.production.staff import STAFF_ROOT
from scripts.whoami import agent_accounts, note_file
from scripts.lib import conf, models_registry, operation_deadline
from scripts.lib.atomic_file import write_text_atomic
from scripts.message import session_send
from scripts.agents import member_account, pane_record
from scripts.agents.launch_names import LaunchNames, LaunchNamesNotRead
from scripts.agents.codex_trust import trust_arguments
from scripts.herdr import pane_launch
from scripts.production import staff
from scripts.lib.resume_message import (
    AccountMove, NO_ACCOUNT_MOVE, BusyPass, NO_OWN_PASS, launcher_prompt,
    ResumeWorkers as ResumeWorkers,
    NO_WORKERS as NO_WORKERS,
    resume_message as resume_message,
    resume_worker_command as resume_worker_command,
)
from scripts.agents.stop_results import (
    Stopped as Stopped, NotRunning as NotRunning, StopFailed as StopFailed,
    SESSION_WITHOUT_STAFF_ROW,
    StopUnproved as StopUnproved,
    ClaudeUp as ClaudeUp, ClaudeStartState, ResumePending as ResumePending, MoveHandedOff as MoveHandedOff, ResumeFailed as ResumeFailed,
    SessionNotFound as SessionNotFound, MoveResult as MoveResult, claude_up_line as claude_up_line,
    ResumeResult as ResumeResult, StopResult, SourceRelease, SourceReleased, SourceUnresolved, SourceReleaseUnknown,
)
from scripts.message import sessions
from scripts.agents.stop_results import shutdown_state_root

if TYPE_CHECKING:
    from scripts.agents.restart import StopReadArguments
    from scripts.herdr import worker_panes
    from scripts.production.staff import StaffRow, StaffSnapshot, ClaudeBackend, CodexBackend, BackendUnavailable, StaffId


@dataclass(frozen=True)
class LiveAccountUnproved(ResumeFailed):
    """A live process has no readable account evidence."""


class RowStarter(Protocol):
    def start_row(self, row: StaffRow, record: StaffSnapshot, client: herdr.Herdr, *,
                  target_account: str | accounts.AccountSelection) -> str: ...
    def session_launch(self, identity: str, name: str) -> tuple[Literal["claude", "codex"], dict[str, str], list[str]]: ...
    def producer_launch(self, row: StaffRow) -> tuple[Literal["claude", "codex"], dict[str, str], list[str]]: ...
    def producer_resume(self, row: StaffRow, entry: pane_record.PaneLaunch
                        ) -> tuple[Literal["claude", "codex"], dict[str, str], list[str]]: ...
    def producer_first_arguments(self, name: str) -> tuple[Literal["claude", "codex"], list[str]]: ...


@dataclass(frozen=True)
class ProducerFirstStart:
    """A configured producer that needs its first native conversation."""

    name: str
    folder: Path


@dataclass(frozen=True)
class PendingProducerAdmission:
    """A reserved pane whose native conversation still needs staff admission."""

    pane: str
    terminal: str
    kind: Literal["claude", "codex"]
    home: Path


def _producer_admission_path(name: str) -> Path:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("producer name must be one folder name")
    return pane_record.record_directory() / f".producer-admission-{name}"


def producer_admission_record(name: str) -> Path:
    """Locate the validated producer reservation for cleanup."""
    return _producer_admission_path(name)


def _producer_folder(name: str) -> Path:
    root = conf.read_or_repository("staff").text("PRODUCER_FOLDER_ROOT")
    return Path(root).expanduser() / name


def _reserve_producer_pane(request: ProducerFirstStart, kind: Literal["claude", "codex"], home: Path,
                           client: herdr.Herdr, *, lost_pane: str = "") -> PendingProducerAdmission:
    from scripts.lib import nix_env
    from scripts.whoami.account import named_account_environment

    workspace = conf.read_or_repository("staff").text("PRODUCER_WORKSPACE")
    entries = pane_record.read_strict(client.session)
    source = entries.get(lost_pane)
    pending_identities = ({source["session_id"], pane_record.resume_launch(source)["session_id"]}
                          if source is not None else set[str]())
    shells = [row for row in client.panes() if row["pane_id"] != lost_pane
              and (row["pane_id"] not in entries or entries[row["pane_id"]]["session_id"] in pending_identities)
              and (not row["session_id"] or row["session_id"] in pending_identities)
              and client.producer_pane(row["pane_id"], request.name, workspace)]
    if len(shells) > 1:
        raise ValueError("producer has several matching shells; retain them for inspection")
    if shells:
        row = shells[0]
    else:
        environment = named_account_environment(home) if kind == "claude" else {"CODEX_HOME": str(home)}
        prepared = herdr.prepare_agent_environment(str(request.folder), environment)
        if isinstance(prepared, nix_env.Refused):
            raise OSError(str(prepared))
        pane = client.open_tab(workspace, request.name, str(request.folder), prepared.env)
        row = client.pane_row(pane)
        if isinstance(row, herdr.NoPane):
            raise ValueError("reserved producer pane is unavailable")
    return PendingProducerAdmission(row["pane_id"], herdr.json_text(row.get("terminal_id"), "producer terminal"), kind, home)


def _save_producer_reservation(request: ProducerFirstStart, pending: PendingProducerAdmission) -> None:
    write_text_atomic(_producer_admission_path(request.name), json.dumps({
        "pane": pending.pane, "terminal": pending.terminal, "kind": pending.kind,
        "home": str(pending.home), "folder": str(request.folder)}))


def producer_conflicts(name: str, client: herdr.Herdr) -> list[tuple[str, str]]:
    """Find live named conversations and unresolved agents in labeled producer panes."""
    from scripts.message import codex_daemon

    panes = client.panes()
    workspace = conf.read_or_repository("staff").text("PRODUCER_WORKSPACE")
    spaces = {row.get("workspace_id") for row in client.listed("workspace") if row.get("label") == workspace}
    tabs = {row.get("tab_id") for row in client.listed("tab") if row.get("label") == name}
    conflicts: list[tuple[str, str]] = []
    for pane in panes:
        if pane["workspace_id"] not in spaces or not (pane.get("label") == name or pane.get("tab_id") in tabs):
            continue
        identity = pane["session_id"] or "unidentified"
        try:
            info = herdr.json_object(client.result("pane", "process-info", "--pane", pane["pane_id"]).get("process_info"),
                                     "named producer process info")
            kinds = producer_pane_kinds(info)
            if not kinds:
                continue
            if len(kinds) == 1:
                identity = client.producer_admission_launch(pane["pane_id"], kinds[0])["session_id"]
        except (OSError, ValueError, herdr_types.HerdrFailure):
            pass
        conflicts.append((identity, pane["pane_id"]))
    named: list[tuple[str, tuple[int, ...]]] = [
        (row["sessionId"], (row["pid"],)) for row in sessions.running_sessions() if row["name"] == name]
    homes = {agent_accounts.codex_home(), *(owner.home for owner in codex_daemon.daemons(os.environ))}
    threads: set[str] = set()
    for home in sorted(homes):
        try:
            lines = (home / "session_index.jsonl").read_text().splitlines()
        except FileNotFoundError:
            continue
        for line in lines:
            if not line.strip():
                continue
            entry = herdr.json_object(cast(object, json.loads(line)), "producer thread index")
            if entry.get("thread_name") == name:
                threads.add(herdr.json_text(entry.get("id"), "producer thread id"))
    if threads:
        codex = herdr.codex_conversations()
        named.extend((identity, codex.live[identity]) for identity in threads if identity in codex.live)
    for daemon in codex_daemon.read_daemons(os.environ):
        if isinstance(daemon.sessions, codex_daemon.DaemonUnreadable):
            raise ValueError(f"producer thread registry is unreadable: {daemon.sessions.reason}")
        named.extend((row.thread_id, ()) for row in daemon.sessions if row.name == name)
    for identity, pids in named:
        places = [pane["pane_id"] for pane in panes if pane["session_id"] == identity]
        if not places:
            for pid in pids:
                place = herdr.herdr_place_of(pid)
                if isinstance(place, herdr.HerdrPlace):
                    places.append(place.pane if place.session == client.session else f"{place.session}:{place.pane}")
        conflicts.append((identity, ",".join(places) or "outside herdr"))
    locations: dict[str, set[str]] = {}
    unresolved: list[tuple[str, str]] = []
    for identity, pane in conflicts:
        if identity == "unidentified":
            unresolved.append((identity, pane))
        else:
            locations.setdefault(identity, set()).update(pane.split(","))
    for identity, panes_for_session in locations.items():
        panes_for_session.discard("outside herdr")
        unresolved.append((identity, ",".join(sorted(panes_for_session)) or "outside herdr"))
    return sorted(set(unresolved))


def producer_pane_kinds(info: dict[str, object]) -> list[Literal["claude", "codex"]]:
    """Read agent families from a pane's foreground processes."""
    kinds: list[Literal["claude", "codex"]] = []
    for value in herdr.json_items(info.get("foreground_processes"), "producer foreground processes"):
        process = herdr.json_object(value, "producer foreground process")
        argv = [herdr.json_text(argument, "producer argument")
                for argument in herdr.json_items(process.get("argv"), "producer arguments")]
        if herdr.is_claude_process(argv):
            kinds.append("claude")
        elif herdr.is_codex_process(argv):
            kinds.append("codex")
    return kinds


def producer_adoption_launch(identity: str, pane: str, client: herdr.Herdr) -> pane_record.PaneLaunch:
    """Prove a named native conversation before staff admission."""
    pane = pane.removeprefix(client.session + ":")
    if any(row["pane_id"] == pane for row in client.panes()):
        info = herdr.json_object(client.result("pane", "process-info", "--pane", pane).get("process_info"),
                                 "producer adoption process info")
        kinds = producer_pane_kinds(info)
        if len(kinds) != 1:
            raise ValueError("producer has no unique native foreground process")
        entry = client.producer_admission_launch(pane, kinds[0])
        if entry["session_id"] != identity:
            raise ValueError("producer pane runs another conversation")
        return entry
    from scripts.message import codex_daemon

    observed = herdr.read_conversation(identity)
    if isinstance(observed, herdr.LiveConversation) and len(observed.pids) > 1:
        raise ValueError("producer conversation has several native processes")
    if isinstance(observed, herdr.LiveConversation) and len(observed.pids) == 1:
        pid = observed.pids[0]
        native = herdr.native_process(pid, observed.kind)
        if native.current_identity != identity:
            raise ValueError("producer process runs another conversation")
        if observed.kind == "claude":
            records = [row for row in sessions.running_sessions() if row["sessionId"] == identity]
            if len(records) != 1 or records[0]["pid"] != pid:
                raise ValueError("producer registry has no unique native process")
            folder = records[0].get("cwd", "")
        else:
            saved = _unrecorded_codex_launch(identity)
            if isinstance(saved, SessionNotFound):
                raise ValueError("producer thread folder is unavailable")
            folder = saved["cwd"]
        if not Path(folder).is_absolute():
            raise ValueError("producer folder is unavailable")
        entry = pane_record.launch("", identity, folder, native.environment, [observed.kind], kind=observed.kind)
        key = "CLAUDE_CONFIG_DIR" if observed.kind == "claude" else "CODEX_HOME"
        entry["config_dir"] = native.environment.get(key) or str(Path.home() / (".claude" if observed.kind == "claude" else ".codex"))
        entry.update(pid=pid, proc_start=native.started)
        if herdr.native_process(pid, observed.kind) != native:
            raise ValueError("producer process changed before adoption")
        return entry
    for daemon in codex_daemon.read_daemons(os.environ):
        if isinstance(daemon.sessions, codex_daemon.DaemonUnreadable):
            raise ValueError(daemon.sessions.reason)
        for thread in daemon.sessions:
            if thread.thread_id == identity and Path(thread.cwd).is_absolute():
                entry = pane_record.launch("", identity, thread.cwd, {"CODEX_HOME": str(daemon.owner.home)}, ["codex"], kind="codex")
                entry["config_dir"] = str(daemon.owner.home)
                return entry
    raise ValueError("producer has no proved live native conversation")


def adopt_named_producer(request: ProducerFirstStart, identity: str, pane: str, client: herdr.Herdr) -> ClaudeUp:
    """Admit one proved live conversation without a launch."""
    from scripts.agents import remote

    try:
        entry = producer_adoption_launch(identity, pane, client)
        native = staff.native_id(entry["session_id"])
        home = pane_launch.config_home(entry, record=staff.StaffSnapshot(()))
        if not isinstance(home, staff.SessionHome):
            raise ValueError("producer account is unavailable")
        folder = Path(entry["cwd"])
        kind = pane_launch.agent_kind(entry)
        entry["name"] = request.name
        if entry["pane_id"]:
            pane_record.write(client.session, entry)
        row = staff.ensure("producer", name=request.name, runtime_id=native,
            worktree=staff.PresentWorktree(remote.local_machine(), folder),
            doc=staff.LiveDoc(remote.local_machine(), request.folder))
        if entry["pane_id"]:
            expectation = pane_launch.ProducerStartExpectation(row.id, native, kind, folder, home.path)
            with pane_launch.producer_start(pane_record.record_directory(), expectation):
                row = pane_launch.staff_admitted(row)
        else:
            row = staff.set_home(row.id, home)
        _producer_admission_path(request.name).unlink(missing_ok=True)
        label = accounts.claude_config_label(home.path) if kind == "claude" else agent_accounts.codex_home_role_label(home.path)
        return ClaudeUp(str(row.current_session), entry["pane_id"], label, start_state=ClaudeStartState.ALREADY_RUNNING)
    except (OSError, ValueError, herdr_types.HerdrFailure) as error:
        raise ValueError(f"session {identity}, pane {pane}: producer adoption is unproved: {error}") from error


def _locked_first_producer_resume(request: ProducerFirstStart, *, to: str) -> ResumeResult:
    """Serialize first admission before the producer has a staff row."""
    _ = _producer_admission_path(request.name)
    with staff.slot_operation("producer-first-" + request.name, request.name):
        record = staff.initial_snapshot()
        row = next((row for row in record.rows if row.kind == "producer" and row.name == request.name), None)
        if row is not None and not _producer_admission_path(request.name).exists():
            return _resume(str(row.id), to=to)
        return _first_producer_resume(request, to=to)


def _first_producer_resume(request: ProducerFirstStart, *, to: str = "") -> ResumeResult:
    """Retain the pane before launch and admit only its proved native identity."""
    from scripts.agents import remote

    began = monotonic()
    try:
        if not (request.folder / "README.md").is_file():
            raise ValueError(f"producer README is unavailable: {request.folder / 'README.md'}")
        starter = cast(RowStarter, cast(object, import_module("scripts.agents.restart")))
        kind, arguments = starter.producer_first_arguments(request.name)
        client = herdr.Herdr(herdr.shared_session())
        path = _producer_admission_path(request.name)
        recovering_admission = path.exists()
        conflicts = producer_conflicts(request.name, client)
        if len(conflicts) > 1:
            raise ValueError("producer sessions conflict: " + "; ".join(
                f"session {identity}, pane {pane}" for identity, pane in conflicts))
        if recovering_admission:
            saved = herdr.json_object(cast(object, json.loads(path.read_text(encoding="utf-8"))), "pending producer admission")
            if saved.get("kind") != kind or saved.get("folder") != str(request.folder):
                raise ValueError("pending producer settings differ; retain its pane and inspect the admission record")
            pending = PendingProducerAdmission(herdr.json_text(saved.get("pane"), "producer pane"),
                herdr.json_text(saved.get("terminal"), "producer terminal"), kind,
                Path(herdr.json_text(saved.get("home"), "producer account")))
            if to:
                selected = member_account.producer_account(kind, to=to)
                requested_home = selected.dir if isinstance(selected, accounts.NamedAccount) else selected.home
                if requested_home.resolve() != pending.home.resolve():
                    raise ValueError("pending producer admission uses another account; complete its admission before an account move")
            if conflicts:
                retained = pane_record.read_strict(client.session).get(pending.pane)
                if retained is None or retained["session_id"] != conflicts[0][0]:
                    return adopt_named_producer(request, *conflicts[0], client)
        else:
            if conflicts:
                return adopt_named_producer(request, *conflicts[0], client)
            selected = member_account.producer_account(kind, to=to)
            home = selected.dir if isinstance(selected, accounts.NamedAccount) else selected.home
            pending = _reserve_producer_pane(request, kind, home, client)
            _save_producer_reservation(request, pending)
        pane_row = client.pane_row(pending.pane)
        if isinstance(pane_row, herdr.NoPane) or pane_row.get("terminal_id") != pending.terminal:
            lost = pending
            retained = pane_record.read_strict(client.session).get(lost.pane)
            if retained is not None and not isinstance(herdr.read_conversation(retained["session_id"], kind), herdr.StoppedConversation):
                raise ValueError("retained producer conversation is not proved stopped; retain its admission record")
            pending = _reserve_producer_pane(request, kind, lost.home, client, lost_pane=lost.pane)
            if retained is not None:
                replacement = retained.copy()
                replacement["pane_id"] = pending.pane
                _ = replacement.pop("pid", None)
                _ = replacement.pop("proc_start", None)
                with pane_launch.locked(pane_record.record_path(client.session)):
                    entries = pane_record.read_strict(client.session)
                    current = entries.get(pending.pane)
                    if (current is not None and pane_record.resume_launch(current)["session_id"]
                            != pane_record.resume_launch(retained)["session_id"]):
                        raise ValueError("replacement producer pane holds another conversation; retain its launch record")
                    entries[pending.pane] = replacement
                    pane_launch.save(pane_record.record_path(client.session), entries)
            _save_producer_reservation(request, pending)
            if retained is not None:
                pane_launch.retire_launch(pane_record.record_path(client.session), lost.pane, retained)
        if isinstance(client.pane_activity(pending.pane), herdr.PaneAtShell):
            retained = pane_record.read_strict(client.session).get(pending.pane)
            if retained is not None:
                arguments = pane_record.configured_resume_arguments(retained, retained["session_id"], request.name,
                    models_registry.launch_settings("producers", request.name))
                live = herdr.read_conversation(retained["session_id"], kind)
                if not isinstance(live, herdr.StoppedConversation):
                    raise ValueError("retained producer conversation is not proved stopped; retain its admission record")
            if kind == "claude":
                herdr.trust_claude_launch(request.folder, pending.home)
            client.run_in_pane(pending.pane, ["eval", herdr.agent_alias_script(kind) + ":"], terminal=pending.terminal)
            outcome = herdr.start_agent(client.session, pending.pane, kind,
                trust_arguments(kind, str(request.folder), arguments), herdr.NoOpeningPrompt(),
                int(conf.read("shutdown").number("START_SECONDS") * 1000), agent=request.name)
            if isinstance(outcome, herdr.PromptNotSubmitted):
                raise OSError(outcome.reason)
        entry = client.producer_admission_launch(pending.pane, kind)
        home = pane_launch.config_home(entry, record=staff.StaffSnapshot(()))
        if (not isinstance(home, staff.SessionHome) or home.path.resolve() != pending.home.resolve()
                or Path(entry["cwd"]).resolve() != request.folder.resolve()):
            raise ValueError("producer process differs from its reserved folder or account")
        entry["name"] = request.name
        pane_record.write(client.session, entry)
        identity = staff.native_id(entry["session_id"])
        row = staff.ensure("producer", name=request.name, runtime_id=identity,
            worktree=staff.PresentWorktree(remote.local_machine(), request.folder),
            doc=staff.LiveDoc(remote.local_machine(), request.folder))
        expectation = pane_launch.ProducerStartExpectation(row.id, identity, kind, request.folder, pending.home)
        with pane_launch.producer_start(pane_record.record_directory(), expectation):
            row = pane_launch.staff_admitted(row)
        publication = pane_record.read_strict(client.session).get(pending.pane)
        if publication is None or not publication.get("producer_binding", False):
            raise ValueError("producer admission needs pane publication; retain its native conversation")
        path.unlink(missing_ok=True)
        label = (accounts.claude_config_label(pending.home) if kind == "claude"
                 else agent_accounts.codex_home_role_label(pending.home))
        return ClaudeUp(str(row.current_session), pending.pane, label,
                        elapsed_seconds=monotonic() - began, start_state=ClaudeStartState.STARTED)
    except (OSError, ValueError, staff.StaffRefused, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
        return ResumeFailed(request.name, str(error))



_UNREAD_NAMES = LaunchNamesNotRead()
_resume_names: ContextVar[LaunchNames | LaunchNamesNotRead] = ContextVar("resume_names", default=_UNREAD_NAMES)
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
    bindings = {row.id: pane_record.publication_for_session(row.current_session, record=membership)
                for row in membership.rows if row.kind == "producer"}
    tabs: list[herdr.TabSession] = []
    for pane in client.panes():
        identity = pane["session_id"]
        entry = entries.get(pane["pane_id"])
        owner = membership.by_member_reference(entry["session_id"]) if entry is not None else None
        saved_identity = owner.current_session if owner is not None else entry["session_id"] if entry is not None else ""
        label = pane.get("label") or ""
        producer = owner or membership.by_current_session(identity or "")
        if producer is not None and producer.kind == "producer":
            binding = bindings.get(producer.id)
            if binding is not None:
                if binding.path != pane_record.record_path(client.session) or binding.entry["pane_id"] != pane["pane_id"]:
                    continue
                identity = producer.current_session
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
        except (OSError, ValueError, herdr_types.HerdrFailure) as error:
            raise PermissionError(f"{refusal}: {error}") from error
    if number in (signal.SIGTERM, signal.SIGKILL):
        operation_deadline.check()
    os.kill(pid, number)


_launch_note_output = Lock()


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
        with _launch_note_output, redirect_stdout(StringIO()):
            _ = pane_record.seed(client.session, select=selected_pane)
    except (OSError, ValueError, herdr_types.HerdrFailure) as error:
        print(f"stop: launch note anomaly: {error}", file=sys.stderr)


def release_source(row: StaffRow, backend: ClaudeBackend | CodexBackend | BackendUnavailable) -> SourceRelease:
    """Resolve native stop evidence before creating the destination."""
    from scripts.production import staff

    kind = "claude" if isinstance(backend, staff.ClaudeBackend) else "codex" if isinstance(backend, staff.CodexBackend) else ""
    result = stop(row.id, kind)
    if isinstance(result, SessionNotFound):
        return SourceUnresolved(f"{row.id}: lifecycle could not resolve the staff reference; check the staff row and retry")
    if isinstance(result, StopFailed):
        return SourceReleaseUnknown(f"{result.reason}; inspect the source session and retry")
    return SourceReleased()


def stop(session: str, kind: str = "", *, deadline: float = math.inf,
         codex: herdr.CodexConversations | None = None) -> StopResult:
    from scripts.production import staff

    try:
        with operation_deadline.within(deadline), staff.row_operation(session):
            shared: StopReadArguments = {"codex": codex} if codex is not None else {}
            return _stop_recorded(session, kind, **shared)
    except TimeoutError as error:
        reason = str(error)
        return StopFailed(session, reason if "deadline passed" in reason else f"deadline passed; {reason}")


def _stop_recorded(session: str, kind: str = "", *,
                   codex: herdr.CodexConversations | None = None) -> StopResult:
    """Stop on purpose; retain its launch for an explicit resume, not crash recovery."""
    from scripts.herdr import worker_panes
    from scripts.production import staff

    try:
        record = staff.initial_snapshot()
    except (OSError, ValueError):
        record = staff.StaffSnapshot(())
    row = record.by_member_reference(session)
    pane_worker = row is not None and row.kind == "worker"
    before = next((item for item in worker_panes.observe(row.parent.id, record) if item.row.id == row.id), None
                  ) if pane_worker and row is not None and isinstance(row.parent, staff.ReportsTo) else None
    if isinstance(before, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker)):
        return StopFailed(str(before.row.current_session), before.reason)
    if isinstance(before, worker_panes.StoppedWorker) and isinstance(_worker_stopped(
            before, **({"codex": codex} if codex is not None else {})), NotRunning):
        result: StopResult = NotRunning(str(before.row.current_session))
    else:
        _record_launches([session])
        result = _stop(session, kind, **({"codex": codex} if codex is not None else {}))
        after = (None if codex is None else herdr.CodexConversations(
            {identity: pids for identity, pids in codex.live.items() if identity != result.session_id},
            codex.unknown)) if isinstance(result, (Stopped, NotRunning)) else codex
        if before is not None and isinstance(result, (Stopped, NotRunning)):
            proof = _worker_stopped(before, **({"codex": after} if after is not None else {}))
            if isinstance(proof, StopFailed):
                return proof
    if isinstance(result, (Stopped, NotRunning)):
        _retire_prompt(result.session_id)
    if isinstance(result, Stopped):
        try:
            owner = herdr.shared_session()
            for pane, entry in pane_record.read(owner).items():
                member = record.by_member_reference(entry["session_id"])
                if entry["session_id"] == result.session_id or (member is not None and member.current_session == result.session_id):
                    pane_record.end(owner, pane, result.session_id, record=record)
        except (OSError, ValueError) as error:
            print(f"{result.session_id}: stop note anomaly: {error}", file=sys.stderr)
    return result


def _worker_stopped(before: worker_panes.WorkerPane, *,
                    codex: herdr.CodexConversations | None = None) -> NotRunning | StopFailed:
    """Prove the worker stop and retain the observed pane processes on failure."""
    from scripts.herdr import worker_panes

    identity = str(before.row.current_session)
    if isinstance(before, worker_panes.ClosedWorker):
        return NotRunning(identity)
    details = "pane unobserved; foreground processes not read"
    if isinstance(before, (worker_panes.OpenWorker, worker_panes.StoppedWorker)):
        client = herdr.Herdr(herdr.shared_session())
        details = f"pane {before.pane}; foreground processes not read"
        try:
            panes = client.panes()
            closed = not any(pane["pane_id"] == before.pane or pane.get("terminal_id") == before.terminal for pane in panes)
            at_shell = False
            if any(pane["pane_id"] == before.pane and pane.get("terminal_id") == before.terminal for pane in panes):
                activity = client.pane_activity(before.pane)
                at_shell = isinstance(activity, herdr.PaneAtShell)
                details = (f"pane {before.pane}; foreground processes: {', '.join(activity.commands) or 'none'}"
                           if isinstance(activity, herdr.PaneRunning) else f"pane {before.pane}; at its shell")
            if ((closed or at_shell)
                    and not isinstance(herdr.read_conversation(identity, codex=codex), herdr.LiveConversation)):
                return NotRunning(identity)
        except (OSError, ValueError, herdr_types.HerdrFailure) as error:
            details += f"; {error}"
    return StopFailed(identity, f"worker stop has no fresh shell or closed-pane proof; {details}")


def _retire_prompt(session_id: str) -> None:
    """Publish only the session id and stop time; any failure is separate from stop or move."""
    try:
        from scripts.production.stall_watch import retire_session_prompt

        retire_session_prompt(session_id)
    except Exception as error:
        print(f"{session_id}: prompt cancellation: {error}", file=sys.stderr)


def stop_grace_seconds() -> float:
    return float(conf.read("shutdown").text("STOP_GRACE_SECONDS"))


def stop_target(session: str) -> str | SessionNotFound | StopFailed:
    """Resolve the same local target for a stop and its preview."""
    try:
        try:
            record = staff.initial_snapshot()
        except (OSError, ValueError) as error:
            print(f"{session}: staff anomaly: {error}", file=sys.stderr)
            record = staff.StaffSnapshot(())
        member = record.by_member_reference(session)
        if member is None:
            producers = [row for row in record.rows if row.kind == "producer" and row.name == session]
            if len(producers) == 1:
                member = producers[0]
        if member is not None:
            return member.current_session
        registered = {row["sessionId"] for row in sessions.running_sessions()
                      if session in (row["sessionId"], row["name"])}
        if len(registered) > 1:
            return StopFailed(session, f"{session}: several live sessions match")
        if registered:
            return registered.pop()
        tab = _tab_of(session)
        return tab.session_id if isinstance(tab, herdr.TabSession) and tab.session_id else SessionNotFound(session)
    except (OSError, ValueError, herdr_types.HerdrFailure) as error:
        return StopFailed(session, str(error))


def _stop(session: str, kind: str = "", *,
          codex: herdr.CodexConversations | None = None) -> StopResult:
    """Signal only the process that proves the current conversation."""
    identity = session
    last_signal = ""
    signal_pid = 0
    try:
        settings = conf.read("shutdown")
        stop_grace, kill_grace = stop_grace_seconds(), float(settings.text("KILL_GRACE_SECONDS"))
        target = stop_target(session)
        if isinstance(target, (SessionNotFound, StopFailed)):
            return target
        identity = target
        if not kind:
            try:
                publication = pane_record.publication_for_session(identity)
                kind = pane_launch.agent_kind(publication.entry) if publication is not None else ""
            except (OSError, ValueError) as error:
                print(f"{identity}: launch record anomaly: {' '.join(str(error).splitlines())}", file=sys.stderr)
                kind = ""
        observed = herdr.read_conversation(identity, kind, **({"codex": codex} if codex is not None else {}))
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
            except herdr.NativeRegistryUnavailable:
                pass
            except (OSError, ValueError, herdr_types.HerdrFailure) as error:
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
    except (OSError, ValueError, herdr_types.HerdrFailure) as error:
        reason = str(error)
        if isinstance(error, TimeoutError) and last_signal:
            reason += f"; pid {signal_pid} received {last_signal} and still runs"
        return StopFailed(identity, reason)


def stop_unit_workers(unit: StaffId, record: StaffSnapshot, *,
                      codex: herdr.CodexConversations | None = None) -> list[str]:
    """Stop the selected workers concurrently and retain every staff row."""
    from scripts.agents import worker_lifecycle
    from scripts.herdr import worker_panes
    from scripts.agents import restart

    if not record.find(kind="worker", parent=unit):
        return []
    try:
        deadline = worker_lifecycle.effective_deadline()
        states = worker_panes.observe(unit, record)
        unresolved = [f"{item.row.name}: {item.reason}" for item in states
                      if isinstance(item, (worker_panes.ProcessUnknownWorker, worker_panes.UncertainWorker))]
        live = [item for item in states if isinstance(item, worker_panes.OpenWorker)]
        client = herdr.Herdr(herdr.shared_session())
        shared: StopReadArguments = {"codex": codex} if codex is not None else {}
        with ThreadPoolExecutor(max_workers=max(1, len(live))) as pool:
            futures = [(item, pool.submit(restart.stop_row, item.row, client,
                deadline=deadline, **shared)) for item in live]
            for item, future in futures:
                if not future.result():
                    unresolved.append(f"{item.row.name}: not proved stopped")
    except (OSError, ValueError, herdr_types.HerdrFailure, SystemExit) as error:
        unresolved = [f"{unit}: {' '.join(str(error).splitlines())}"]
        print(f"anomaly: {unresolved[0]}", file=sys.stderr)
    return unresolved


def resume_arguments(entry: pane_record.PaneLaunch) -> list[str]:
    names = _resume_names.get()
    if isinstance(names, LaunchNamesNotRead):
        names = LaunchNames.read()
    row = names.tab_record.by_member_reference(entry["session_id"])
    if row is not None and row.kind == "producer":
        restart = cast(RowStarter, cast(object, import_module("scripts.agents.restart")))
        return restart.producer_resume(row, names.resume(entry))[2]
    return pane_record.resume_arguments(names.resume(entry))


def _pane_environment(tab: herdr.TabSession, entry: pane_record.PaneLaunch) -> tuple[list[str], dict[str, str]]:
    """Read the pane environment and remove inherited launch proofs."""
    client = herdr.Herdr(herdr.shared_session())
    pane = next((pane for pane in client.panes() if pane["pane_id"] == tab.pane_id), None)
    if pane is None:
        raise herdr_types.HerdrFailure(f"no pane {tab.pane_id!r}")
    unset = ["HERDR_TAB_ID", "STAFF_ID", "STAFF_OPERATION_FD", "PYTHONUNBUFFERED", "REFUSAL_WATCH_INVENTORY",
             *sorted(key for key in os.environ.keys() | entry["environment"].keys() if key.startswith("STAFF_ROW_FD_"))]
    environment = {key: value for key, value in entry["environment"].items() if key not in unset}
    environment = {**environment, "HERDR_PANE_ID": tab.pane_id,
                   "HERDR_WORKSPACE_ID": pane["workspace_id"], "RELAUNCH_PROMPT_DELIVERY": "pane"}
    return unset, environment


def _launch_home(entry: pane_record.PaneLaunch) -> staff.SessionHome:
    """Use the selected launch environment, retained home, or normal account."""
    kind = pane_launch.agent_kind(entry)
    directory = entry["environment"].get("CODEX_HOME" if kind == "codex" else "CLAUDE_CONFIG_DIR", "")
    home = staff.SessionHome(Path(directory)) if directory else pane_launch.config_home(entry)
    if isinstance(home, staff.HomeUnknown):
        print(f"anomaly: {entry['session_id']}: session home is unknown; select the normal account", file=sys.stderr)
        selected = member_account.resume_account(kind)
        return staff.SessionHome(selected.dir if isinstance(selected, accounts.NamedAccount) else selected.home)
    return home


def _pane_command(tab: herdr.TabSession, entry: pane_record.PaneLaunch, account: str) -> list[str]:
    """Build the account command for a shell in another pane."""
    unset, environment = _pane_environment(tab, entry)
    kind = pane_launch.agent_kind(entry)
    if kind == "claude":
        home = environment.get("CLAUDE_CONFIG_DIR") or str(_launch_home(entry).path)
        herdr.trust_claude_launch(Path(entry["cwd"]), Path(home))
    program = [sys.executable, str(SCRIPTS / "accounts/accounts.py"), "exec", account]
    if kind == "codex":
        environment["CODEX_HOME"] = environment.get("CODEX_HOME") or str(_launch_home(entry).path)
        program = ["codex"]
    return ["env", *(argument for key in unset for argument in ("-u", key)),
            *(f"{key}={value}" for key, value in environment.items()),
            "sh", "-c", 'cd -- "$1" && shift && exec "$@"', "resume", entry["cwd"],
            *program, *trust_arguments(kind, entry["cwd"], resume_arguments(entry))]


def _exec_in_own_pane(tab: herdr.TabSession, entry: pane_record.PaneLaunch,
                      account: str, message: str) -> Never:
    """Replace the foreground command with the saved account launch."""
    from scripts.production import staff

    from scripts.lib.agent_program import resolve_program

    kind = pane_launch.agent_kind(entry)
    environment = accounts.launch_shell_environment(Path(entry["cwd"]))
    unset, pane_environment = _pane_environment(tab, entry)
    environment.update(pane_environment)
    for key in unset:
        _ = environment.pop(key, None)
    if kind == "claude":
        prepared = accounts.prepare_claude_launch(account, resume=True)
        for key in prepared.remove:
            _ = environment.pop(key, None)
        environment.update(prepared.variables)
        home = environment.get("CLAUDE_CONFIG_DIR") or str(_launch_home(entry).path)
        herdr.trust_claude_launch(Path(entry["cwd"]), Path(home))
    else:
        environment["CODEX_HOME"] = environment.get("CODEX_HOME") or str(_launch_home(entry).path)
    program = resolve_program(kind, environment=environment)
    command = [program, *trust_arguments(kind, entry["cwd"], resume_arguments(entry))]
    if message and kind == "claude":
        command.append(message)
    os.chdir(entry["cwd"])
    row = staff.initial_snapshot().by_member_reference(entry["session_id"])
    if row is not None and isinstance(row.exited, staff.Exited):
        _ = staff.resume(str(row.id))
    selected_home = staff.SessionHome(Path(environment["CODEX_HOME" if kind == "codex" else "CLAUDE_CONFIG_DIR"]).absolute())
    if row is not None and row.home != selected_home:
        try:
            _ = staff.set_home(row.id, selected_home)
        except (staff.StaffUnreadable, staff.StaffNotFound) as error:
            print(f"anomaly: {row.id}: pane home is unavailable: {error}", file=sys.stderr)
    try:
        os.execvpe(program, command, staff.child_environment(environment))
    except OSError:
        if row is not None and row.home != selected_home and isinstance(row.home, staff.SessionHome):
            _ = staff.set_home(row.id, row.home)
        if row is not None and isinstance(row.exited, staff.Exited):
            _ = staff.set_hold(str(row.id), row.exited)
        raise


def _prompt_identity(client: herdr.Herdr, pane: str, deadline: float) -> str:
    """Wait for the pane to expose its native identity within the start deadline."""
    while True:
        try:
            return client.native_session(pane, "claude", operation_deadline.remaining(max(0.001, deadline - monotonic())))
        except (OSError, ValueError, subprocess.SubprocessError, herdr_types.HerdrFailure):
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
    home = str(_launch_home(entry).path)
    transcript = herdr.SessionTranscript(Path(home), entry["session_id"])
    cursor: herdr.TranscriptCursor | herdr.UnspecifiedPromptTranscript = herdr.UnspecifiedPromptTranscript()
    if message:
        cursor = herdr.TranscriptCursor(transcript, datetime.now(timezone.utc))
        cursor.skip_existing_turns()
    launched_at = datetime.now(timezone.utc)
    if isinstance(cursor, herdr.TranscriptCursor):
        cursor.since = launched_at
    operation_deadline.check()
    client.run_in_pane(tab.pane_id, command)
    deadline = monotonic() + operation_deadline.remaining(start_seconds)
    live = sessions.session_liveness(entry["session_id"])
    while isinstance(live, sessions.SessionNotLive):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise herdr_types.HerdrFailure("never started")
        time.sleep(operation_deadline.remaining(min(0.5, remaining)))
        live = sessions.session_liveness(entry["session_id"])
    if isinstance(live, sessions.SessionLivenessUnknown):
        raise herdr_types.HerdrFailure(live.reason)
    if not message:
        return live
    try:
        identity = _prompt_identity(client, tab.pane_id, deadline)
    except (OSError, ValueError, subprocess.SubprocessError, herdr_types.HerdrFailure) as error:
        print(f"{entry['session_id']}: prompt identity anomaly: {error}", file=sys.stderr)
        return live
    if identity != entry["session_id"]:
        print(f"{entry['session_id']}: prompt identity anomaly: observed {identity}", file=sys.stderr)
        return live
    opening = herdr.OpeningPrompt(message, transcript, cursor=cursor)
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
        return LiveAccountUnproved(identity, setting.reason)
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
    from scripts.agents import remote

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
    restart = cast(RowStarter, cast(object, import_module("scripts.agents.restart")))
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
        from scripts.agents import remote

        member = matches[0]
        machine = _member_machine(member)
        if machine and machine != remote.local_machine():
            return ResumeFailed(session, f"session is on {machine}")
        if member.kind == "producer" and _producer_admission_path(member.name).exists():
            folder = (member.worktree.path if isinstance(member.worktree, staff.PresentWorktree)
                      else _producer_folder(member.name))
            return _locked_first_producer_resume(ProducerFirstStart(member.name, folder), to=to or "")
        session = member.current_session
    else:
        if (session and Path(session).name == session and session not in {".", ".."}
                and _producer_admission_path(session).exists()):
            try:
                return _locked_first_producer_resume(ProducerFirstStart(session, _producer_folder(session)), to=to or "")
            except (OSError, ValueError) as error:
                return ResumeFailed(session, str(error))
        try:
            configuration = (models_registry.launch_configuration(os.environ).text
                if models_registry.OPERATION_ENVIRONMENT[0] in os.environ else models_registry.registry_text())
            producers = models_registry.parse_registry(configuration).get("producers", {})
        except (OSError, ValueError):
            producers = {}
        if session in producers:
            try:
                folder = _producer_folder(session)
                if ((folder / "README.md").is_file()
                        or isinstance(_tab_of(session, names=names), SessionNotFound)):
                    return _locked_first_producer_resume(ProducerFirstStart(session, folder), to=to or "")
            except (OSError, ValueError) as error:
                return ResumeFailed(session, str(error))
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
    name_scope = _resume_names.set(names)
    start_scope = ExitStack()
    try:
        tab = _tab_of(session, names=names)
        client = herdr.Herdr(herdr.shared_session())
        row = names.tab_record.by_current_session(identity)
        if isinstance(tab, SessionNotFound):
            original = pane_record.publication_for_session(session, record=names.tab_record)
            if (original is not None and original.path == pane_record.record_path(client.session)
                    and any(pane["pane_id"] == original.entry["pane_id"] for pane in client.panes())):
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
        if row is not None and row.kind == "producer":
            restart = cast(RowStarter, cast(object, import_module("scripts.agents.restart")))
            kind = restart.producer_launch(row)[0]
            if entry is not None:
                entry = entry.copy()
                entry["kind"] = kind
        pane_id = entry["pane_id"] if isinstance(tab, SessionNotFound) and entry is not None else (
            tab.pane_id if isinstance(tab, herdr.TabSession) else "")
        running = running_on_account(identity, pane_id, kind=kind)
        if not isinstance(running, sessions.SessionNotLive):
            if row is not None and isinstance(running, ClaudeUp) and isinstance(running.pid, int) and running.account != "unknown account":
                member_account.repair_home(row, kind, running.pid)
            return running
        if entry is None:
            if row is None:
                return SessionNotFound(session)
            restart = cast(RowStarter, cast(object, import_module("scripts.agents.restart")))
            pane = restart.start_row(row, names.tab_record, client, target_account=to or "")
            result = running_on_account(identity, pane, kind=kind)
            if isinstance(result, ClaudeUp):
                return replace(result, elapsed_seconds=monotonic() - began, start_state=ClaudeStartState.STARTED)
            return ResumeFailed(identity, "session did not start") if isinstance(result, sessions.SessionNotLive) else result
        entry = entry.copy()
        entry["session_id"] = identity
        entry["environment"] = names.environment(entry)
        from scripts.whoami.account import named_account_environment

        source = pane_launch.config_home(entry, record=names.tab_record)
        source_label = ""
        if isinstance(source, staff.SessionHome):
            source_label = agent_accounts.codex_home_role_label(source.path) if kind == "codex" else accounts.claude_config_label(source.path)
        else:
            print(f"anomaly: {identity}: session home is unknown; select the normal account", file=sys.stderr)
        selected = member_account.resume_account(kind, row if row is not None else SESSION_WITHOUT_STAFF_ROW, to=to or "")
        copied_from = ""
        if isinstance(selected, accounts.CodexWorkersAccount):
            target_dir, label = selected.home, selected.label
            account_environment = {"CODEX_HOME": str(target_dir)}
        else:
            target_dir, label = selected.dir, selected.label
            account_environment = named_account_environment(target_dir)
        entry["environment"] = {**entry["environment"], **account_environment}
        if row is None:
            entry["config_dir"] = str(target_dir)
            previous_home = pane_launch.config_home(original.entry, record=names.tab_record) if original is not None else staff.HomeUnknown()
            if original is not None and (not isinstance(previous_home, staff.SessionHome) or previous_home.path != target_dir):
                try:
                    pane_record.write(client.session, pane_launch.without_staff_labels(entry))
                except (OSError, ValueError) as error:
                    print(f"{identity}: account record anomaly: {error}", file=sys.stderr)
        else:
            _ = entry.pop("config_dir", None)
        if row is not None and row.kind == "producer":
            space = herdr.row_workspace(row, names.tab_record, row.name)
            if isinstance(tab, SessionNotFound) or not client.producer_pane(tab.pane_id, row.name, space):
                destination = client.producer_row_tab(str(row.id), row.name, space)
                tab = (herdr.TabSession(destination.pane, row.name, "", identity)
                       if isinstance(destination, herdr.ExistingPane) else SessionNotFound(identity))
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
                raise herdr_types.HerdrFailure("new recovery pane is unavailable")
            tab = replace(tab, session_id=identity)
        elif entry["name"]:
            try:
                with herdr.restore_staff(names.tab_record, labels=names.labels):
                    _ = client.retitle(tab.pane_id, entry["name"])
            except (OSError, ValueError, herdr_types.HerdrFailure) as error:
                print(f"{identity}: title anomaly: {error}", file=sys.stderr)
        workers = NO_WORKERS
        own = NO_OWN_PASS
        from scripts.production import worker
        try:
            if row is not None and row.kind == "worker":
                own = worker.own_pass(row, names.tab_record)
            elif row is not None:
                workers = worker.resume_workers(row.id, names.tab_record)
        except (OSError, ValueError) as error:
            print(f"{identity}: workers anomaly: {error}", file=sys.stderr)
        if isinstance(own, worker.UnsubmittedAssignment):
            message = launcher_prompt(row.id, own.dispatch_id, own.command) if row is not None else ""
        else:
            message = resume_message(why, move=AccountMove(source_label, label) if source_label != label else NO_ACCOUNT_MOVE,
                                     workers=workers, own_pass=own) if (kind != "codex" or row is not None) and (
                                         row is None or row.kind != "worker" or isinstance(own, BusyPass)) else ""
        completed = entry
        if row is not None and row.kind == "producer":
            expectation = pane_launch.ProducerStartExpectation(row.id, row.current_session, kind, Path(entry["cwd"]), target_dir)
            start_scope.enter_context(pane_launch.producer_start(pane_record.record_directory(), expectation))
            completed = expectation.expected_launch(entry)
        if os.environ.get("HERDR_PANE_ID") == tab.pane_id:
            if row is None:
                with pane_record.locked(client.session):
                    entries = pane_record.read_strict(client.session)
                    pending = pane_launch.without_staff_labels(entry)
                    pending["session_id"] = original.entry["session_id"] if original is not None else identity
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
                trust_arguments(kind, entry["cwd"], resume_arguments(entry)), herdr.NoOpeningPrompt(),
                int(operation_deadline.remaining(conf.read("shutdown").number("START_SECONDS")) * 1000), agent=entry["name"] or identity)
            if isinstance(launched.outcome, herdr.PromptNotSubmitted):
                raise herdr_types.HerdrFailure(launched.outcome.reason)
            if message:
                client.prompt(tab.pane_id, message, target=identity)
            started = launched
        else:
            live = sessions.session_liveness(identity)
            if isinstance(live, sessions.SessionLivenessUnknown):
                raise herdr_types.HerdrFailure(live.reason)
            if isinstance(live, sessions.SessionNotLive):
                attempt_pane = tab.pane_id
                live = _start_session(tab, entry, label, message)
            started = live
        selected_home = staff.SessionHome(target_dir.absolute())
        if row is not None and row.home != selected_home:
            try:
                # The launch hook can publish the new home before the start returns.
                current = staff.by_id(row.id)
                if current is None or current.home != selected_home:
                    _ = staff.set_home(row.id, selected_home)
            except (staff.StaffUnreadable, staff.StaffNotFound) as error:
                print(f"anomaly: {row.id}: pane home is unavailable: {error}", file=sys.stderr)
        if original is not None:
            try:
                destination_path = pane_record.record_path(client.session)
                attempt = pane_launch.resume_attempt(destination_path, tab.pane_id)
                proof = names.resume_proof(original.entry, completed)
                if (original.path, original.entry["pane_id"]) == (destination_path, tab.pane_id):
                    pane_launch.finish_restore(original.path, original.entry, attempt, proof, close=client.close_tab_confirmed)
                else:
                    source_client = client if original.path == destination_path else herdr.Herdr(original.path.stem)
                    retained = next((pane for pane in source_client.panes() if pane["pane_id"] == original.entry["pane_id"]), None)
                    if retained is None or (retained["session_id"] in (None, "", identity)
                            and isinstance(source_client.pane_activity(retained["pane_id"]), herdr.PaneAtShell)):
                        pane_launch.finish_restore(original.path, original.entry, attempt, proof, close=client.close_tab_confirmed,
                                                   destination=destination_path, close_source=source_client.close_tab_confirmed)
            except (OSError, ValueError, herdr_types.HerdrFailure) as error:
                print(f"{identity}: launch record anomaly: {error}", file=sys.stderr)
        attempt_pane = ""
        if not _move_resume.get():
            _watch_resume(identity, tab.pane_id, label, started, requester, why)
        return ClaudeUp(identity, tab.pane_id, label, monotonic() - began, copied_from)
    except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
        if original is not None and attempt_pane:
            try:
                cleanup_client = herdr.Herdr(herdr.shared_session())
                destination_path = pane_record.record_path(cleanup_client.session)
                attempt = pane_launch.resume_attempt(destination_path, attempt_pane)
                pane_launch.finish_restore(original.path, original.entry, attempt, pane_launch.FailedResume(),
                                           close=cleanup_client.close_tab_confirmed, destination=destination_path)
            except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as cleanup:
                return ResumeFailed(identity, f"{error}; cleanup failed: {cleanup}")
        return ResumeFailed(identity, str(error))
    finally:
        _resume_names.reset(name_scope)
        start_scope.close()


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
            _ = _detach(["stay", "--session", identity, "--pane", pane,
                     "--account", account, "--pid", str(pid), "--generation", generation, "--why", why,
                     *(["--requester", requester] if requester else [])], "stay")
        else:
            print(f"{identity}: observer anomaly: process generation is unavailable", file=sys.stderr)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"{identity}: observer anomaly: {error}", file=sys.stderr)


_STOP_RESUME_PROGRAM = Path(__file__).resolve()


def _detach(arguments: list[str], task: str, *, program: Path = _STOP_RESUME_PROGRAM) -> Path:
    """Run lifecycle work outside the caller's stop boundary."""
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
    command = [sys.executable, str(program), arguments[0], "--log", str(log), *arguments[1:]]
    if sys.platform == "darwin":
        _ = subprocess.Popen(command, env=environment, start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        _ = subprocess.run(["systemd-run", "--user", "--collect", "--quiet", "--no-block",
                            "env", "-u", "CLAUDE_CODE_SESSION_ID", *command],
                           env=environment, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, check=True, timeout=15)
    return log


detach = _detach


def _handoff_batch(selected: list[str], target: str, why: str, warn_seconds: int, cause: moves.MoveCause,
                   source_accounts: Mapping[str, str] = _NO_SOURCE_ACCOUNTS) -> None:
    # Options precede the positional list so session names cannot become options.
    _ = _detach(["batch", "--target", target, "--why", why, "--cause", cause, "--warn-seconds", str(warn_seconds),
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
    from scripts.agents import remote

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
    except (OSError, ValueError, herdr_types.HerdrFailure):
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
        directory = pane_launch.config_home(saved if saved is not None else {"session_id": identity}, record=names.tab_record)
        if isinstance(directory, staff.SessionHome):
            home = directory.path
            label = note_file.parse_account_label(home.name.removeprefix("."))
            previous = label.label if isinstance(label, note_file.NumberedAccountLabel) else moves.HomeAccountUnresolved(home)
        else:
            print(f"anomaly: {identity}: session home is unknown; retain the normal source account", file=sys.stderr)
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
            entry = moves.ConfirmedMove(kind="confirmed", at=datetime.now(timezone.utc).isoformat(),
                move_id=request["move_id"], tool=request["tool"], subject=request["subject"],
                worker=request["subject"].split(": ", 1)[-1])
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
    account = note_file.parse_account_label(result.account)
    if isinstance(account, note_file.NumberedAccountLabel) and account.tool == "codex":
        observed = running_on_account(identity, result.pane_id, kind="codex")
        if isinstance(observed, ClaudeUp) and observed.account == result.account and isinstance(observed.pid, int):
            failure = _check_stay(identity, result.pane_id, result.account, observed.pid,
                                  sessions.process_start(observed.pid) or "", requester, why)
            return ResumeFailed(identity, failure) if failure else result
        reason = (observed.reason if isinstance(observed, ResumeFailed) else
                  f"Codex did not start on {result.account}")
        failure = _early_exit(identity, result.pane_id, result.account, 0.0,
                              requester=requester, why=why, failure=reason)
        return ResumeFailed(identity, failure)
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
    handoff_failures: dict[str, StopFailed] = {}
    sources: dict[str, str] = dict(source_accounts)
    identities: dict[str, str] = {}
    from scripts.production import staff

    try:
        membership = staff.initial_snapshot()
    except (staff.StaffUnreadable, OSError, ValueError):
        membership = staff.StaffSnapshot(())
    for session in selected:
        try:
            row = membership.by_member_reference(session) or next((row for row in membership.rows if row.name == session), None)
            identity = row.current_session if row is not None else session
            publication = pane_record.publication_for_session(identity, record=membership)
            kind = pane_launch.agent_kind(publication.entry) if publication is not None else "claude"
            if row is not None and row.kind == "producer":
                restart = cast(RowStarter, cast(object, import_module("scripts.agents.restart")))
                kind = restart.producer_launch(row)[0]
            tab = _tab_of(session)
            if isinstance(tab, SessionNotFound) and row is None and publication is None:
                continue
            identity = (tab.session_id or identity) if isinstance(tab, herdr.TabSession) else identity
            pane = tab.pane_id if isinstance(tab, herdr.TabSession) else ""
            identities[session] = identity
            running = running_on_account(identity, pane, kind=kind)
            if isinstance(running, LiveAccountUnproved):
                handoff_failures[session] = StopFailed(identity, running.reason)
            elif isinstance(running, ClaudeUp) and running.account == "unknown account":
                handoff_failures[session] = StopFailed(identity, "live process account is unreadable")
            if isinstance(running, ClaudeUp):
                _ = sources.setdefault(session, running.account)
            if isinstance(running, ClaudeUp) and running.account == target:
                already_up[session] = running
                if row is not None and isinstance(running.pid, int):
                    member_account.repair_home(row, kind, running.pid)
        except (OSError, ValueError, herdr_types.HerdrFailure):
            pass  # The stop returns the read failure for this member.
    _record_launches([session for session in selected if session not in already_up and session not in handoff_failures])
    caller = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if caller:
        try:
            tabs = [_tab_of(session) for session in selected]
            if any(session not in already_up and isinstance(tab, herdr.TabSession) and tab.session_id == caller
                   for session, tab in zip(selected, tabs)):
                _handoff_batch(selected, target, why, warn_seconds, cause, sources)
                return [MoveHandedOff(tab.session_id) if isinstance(tab, herdr.TabSession) and tab.session_id
                        else SessionNotFound(session) for session, tab in zip(selected, tabs)]
        except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
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
