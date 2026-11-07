"""Stop live staff rows, then resume their current sessions in their saved tabs."""
from __future__ import annotations

import math
import os
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast
from importlib import import_module

from scripts.herdr import herdr
from scripts.message import sessions
from scripts.production import staff, session_staff
from scripts.accounts import accounts
from scripts.lib import conf, nix_env, operation_deadline, unit_runs
from scripts.shutdown import member_account, shutdown_record, pane_record
from scripts.shutdown.stop_results import StopResult, Stopped, NotRunning, StopFailed
from scripts.shutdown.codex_trust import trust_arguments
from scripts.herdr import pane_launch
from scripts.whoami.account import named_account_environment
from scripts.whoami import agent_accounts


class WorkerRow(Protocol):
    row: staff.StaffRow


class WorkerPanes(Protocol):
    StoppedWorker: type[WorkerRow]
    ClosedWorker: type[WorkerRow]
    def observe(self, parent: staff.StaffId, record: staff.StaffSnapshot) -> list[WorkerRow]: ...
    def worker_run(self, parent: staff.StaffRow) -> Path: ...


class WorkerRestore(Protocol):
    def restore(self, parent: staff.StaffId) -> int: ...


class MemberLifecycle(Protocol):
    def stop(self, session: str, *, deadline: float) -> StopResult: ...
    def stop_unit_workers(self, unit: staff.StaffId, record: staff.StaffSnapshot) -> list[str]: ...
    def heal_account_trust(self, cwd: Path, destination: Path, source: Path, name: str) -> object: ...


worker_panes = cast(WorkerPanes, cast(object, import_module("scripts.herdr.worker_panes")))
stop_resume = cast(MemberLifecycle, cast(object, import_module("scripts.shutdown.stop_resume")))


@dataclass(frozen=True)
class RegistryConfirmation:
    records: tuple[sessions.SessionRecord, ...]

    @property
    def live_ids(self) -> frozenset[str]:
        return frozenset(record["sessionId"] for record in self.records)


def wait_for_confirmation(session_ids: frozenset[str], timeout: float) -> RegistryConfirmation:
    deadline = time.monotonic() + timeout
    while True:
        records = tuple(record for record in sessions.live_sessions() if record["sessionId"] in session_ids)
        if session_ids <= {record["sessionId"] for record in records} or time.monotonic() >= deadline:
            return RegistryConfirmation(records)
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


def _anomaly(row: staff.StaffRow, error: object) -> None:
    reason = str(error).splitlines()
    print(f"{row.name}: restart anomaly: {reason[0] if reason else type(error).__name__}", file=sys.stderr)


def stop_row(row: staff.StaffRow, client: herdr.Herdr, *, deadline: float = math.inf) -> bool:
    with operation_deadline.within(deadline):
        try:
            panes = [pane for pane in client.panes() if pane["session_id"] == row.current_session]
            if len(panes) > 1:
                _anomaly(row, "duplicate panes retained; no signal sent")
                return False
            if panes and not herdr.check_target(str(row.current_session), action="signal",
                    place=herdr.HerdrPlace(client.session, panes[0]["pane_id"]),
                    terminal=panes[0].get("terminal_id", ""), client=client):
                return False
            if panes:
                # End the restore entry before the signal, so no restore revives the stopped session.
                pane_record.end(client.session, panes[0]["pane_id"], str(row.current_session),
                                record=staff.StaffSnapshot((row,)))
        except (herdr.HerdrFailure, OSError, ValueError) as error:
            _anomaly(row, error)
            return False
        result = stop_resume.stop(str(row.current_session), deadline=deadline)
        if isinstance(result, (Stopped, NotRunning)):
            return True
        _anomaly(row, result.reason if isinstance(result, StopFailed) else "session not found")
        return False


def _saved_launch(entry: pane_record.PaneLaunch, identity: str, name: str) -> tuple[Literal["claude", "codex"], dict[str, str], list[str]]:
    projected = entry.copy()
    projected["session_id"] = identity
    projected["name"] = name
    arguments = pane_record.resume_arguments(projected)
    kind = pane_launch.agent_kind(entry)
    if kind == "codex":
        home = entry["environment"].get("CODEX_HOME", entry["config_dir"])
        return kind, {"CODEX_HOME": home}, arguments
    return kind, named_account_environment(Path(entry["config_dir"])), arguments


def session_launch(identity: str, name: str) -> tuple[Literal["claude", "codex"], dict[str, str], list[str]]:
    try:
        entry = pane_record.launch_for_session(identity)
    except (OSError, ValueError) as error:
        print(f"{identity}: launch record anomaly: {str(error).splitlines()[0]}", file=sys.stderr)
        entry = None
    if entry is not None:
        return _saved_launch(entry, identity, name)
    for directory in sessions.config_dirs():
        if any((directory / "projects").glob(f"*/{identity}.jsonl")):
            return "claude", named_account_environment(directory), ["--resume", identity, "--name", name]
    homes = list(Path.home().glob(".codex*"))
    if home := os.environ.get("CODEX_HOME"):
        homes.insert(0, Path(home))
    for directory in homes:
        if any((directory / "sessions").glob(f"**/rollout-*{identity}.jsonl")):
            return "codex", {"CODEX_HOME": str(directory)}, ["resume", identity]
    raise OSError(f"no transcript for {identity}")


def pane_workers(row: staff.StaffRow) -> bool:
    return unit_runs.worker_transport(worker_panes.worker_run(row)) != "mesh"


def _restore_workers(row: staff.StaffRow, record: staff.StaffSnapshot) -> None:
    worker = cast(WorkerRestore, cast(object, import_module("scripts.production.worker")))
    if row.kind in ("unit", "showrunner") and any(
            child.kind == "worker" and child.parent == staff.ReportsTo(row.id) for child in record.rows
            ) and pane_workers(row) and worker.restore(row.id) != 0:
        raise OSError("worker restore is incomplete")


def pane_holds_other_work(client: herdr.Herdr, pane_id: str, identity: str) -> bool:
    fresh = next((pane for pane in client.panes() if pane["pane_id"] == pane_id), None)
    if fresh is None:
        return False
    activity = client.pane_activity(pane_id)
    if ((fresh["session_id"] and fresh["session_id"] != identity)
            or not isinstance(activity, herdr.PaneAtShell)):
        print(f"{pane_id}: pane close anomaly: pane holds other work; kept open", file=sys.stderr)
        return True
    return False


def start_row(row: staff.StaffRow, record: staff.StaffSnapshot, client: herdr.Herdr, *,
              present: bool = False, target_account: str | accounts.AccountSelection = "", deadline: float = math.inf,
              resolver: session_staff.RowResolver | None = None) -> str:
    with operation_deadline.within(deadline):
        if row.kind == "worker":
            parent = record.parent_of(row)
            if parent is not None and pane_workers(parent):
                return start_row(parent, record, client, present=present, target_account=target_account,
                                 deadline=deadline, resolver=resolver)
        live = herdr.read_conversation(str(row.current_session))
        if isinstance(live, herdr.LiveConversation):
            _restore_workers(row, record)
            print(f"{row.name}: unchanged")
            return ""
        if isinstance(live, herdr.UnknownConversation):
            herdr.conversation_note(live)
        machine, folder = shutdown_record.row_folder(row, record, resolver=resolver)
        from scripts.shutdown import remote
        if machine != remote.local_machine():
            raise ValueError(f"{row.name}: session is on {machine}")
        from scripts.shutdown.launch_names import LaunchNames

        names = LaunchNames(record)
        publication = pane_record.publication_for_session(str(row.current_session), record=record)
        original = publication.entry if publication is not None else None
        projected = names.resume(original) if original is not None else None
        kind, environment, arguments = (_saved_launch(original, str(row.current_session), row.name)
            if original is not None else session_launch(str(row.current_session), row.name))
        selected = member_account.resume_account(kind, row, to=target_account or (
            accounts.CodexWorkersAccount(agent_accounts.codex_home_role_label(Path(original["config_dir"])),
                                         Path(original["config_dir"])) if kind == "codex" and original is not None else ""))
        if isinstance(selected, accounts.NamedAccount):
            _ = stop_resume.heal_account_trust(folder, selected.dir,
                Path(environment.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"), row.name)
            environment = named_account_environment(selected.dir)
        else:
            environment = {"CODEX_HOME": str(selected.home)}
        if projected is not None:
            environment = {**names.environment(projected), **environment}
        destination = client.saved_row_tab(str(row.id), row.name, herdr.row_workspace(row, record, row.name))
        prepared = herdr.prepare_session_environment(str(row.current_session), str(folder), environment,
                                                      herdr.NO_ENVIRONMENT_REMOVALS)
        if isinstance(prepared, nix_env.Refused):
            raise OSError(str(prepared))
        target: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane = destination
        try:
            panes = [pane for pane in client.panes() if pane["session_id"] == row.current_session]
            if len(panes) > 1:
                _anomaly(row, "duplicate panes retained")
            elif panes and (original is None or original["cwd"] == str(folder)):
                target = herdr.ExistingPane(panes[0]["pane_id"], panes[0].get("terminal_id", ""))
            elif (not panes and publication is not None
                    and publication.path == pane_record.record_path(client.session)
                    and publication.entry["cwd"] == str(folder)):
                retained = next((pane for pane in client.panes() if pane["pane_id"] == publication.entry["pane_id"]), None)
                if (retained is not None and (terminal := retained.get("terminal_id", ""))
                        and isinstance(client.pane_activity(retained["pane_id"]), herdr.PaneAtShell)):
                    target = herdr.ExistingPane(retained["pane_id"], terminal)
        except (OSError, ValueError, herdr.HerdrFailure) as error:
            _anomaly(row, error)
        # Read liveness again immediately before the launch.
        current = herdr.read_conversation(str(row.current_session), kind)
        if isinstance(current, herdr.LiveConversation):
            _restore_workers(row, record)
            print(f"{row.name}: unchanged")
            return ""
        if isinstance(current, herdr.UnknownConversation) and not isinstance(live, herdr.UnknownConversation):
            herdr.conversation_note(current)
        settings = conf.read("shutdown")
        start_seconds = settings.number("START_SECONDS")
        operation_deadline.check()
        result = herdr.launch_prepared_agent(client.session, target, prepared, kind,
            trust_arguments(kind, str(folder), arguments), herdr.NoOpeningPrompt(),
            int(operation_deadline.remaining(start_seconds) * 1000), agent=row.name,
            reported_unknown=(current.reason if isinstance(current, herdr.UnknownConversation) else
                              live.reason if isinstance(live, herdr.UnknownConversation) else ""))
        if isinstance(result.outcome, herdr.PromptNotSubmitted):
            if isinstance(target, herdr.ExistingPane) and not target.terminal:
                return result.pane
            raise OSError(result.outcome.reason)
        if original is not None and projected is not None and result.pane != original["pane_id"]:
            if not pane_holds_other_work(client, original["pane_id"], str(row.current_session)):
                path = pane_record.record_path(client.session)
                try:
                    pane_launch.finish_restore(path, original, pane_launch.resume_attempt(path, result.pane),
                                               names.resume_proof(original, projected), close=client.close_tab_confirmed)
                except (OSError, ValueError, herdr.HerdrFailure) as error:
                    _anomaly(row, error)
        try:
            client.remember_row_tab(str(row.id), result.pane)
            if present:
                client.focus_tab_of(result.pane)
                client.close_startup_workspaces(result.pane.split(":", 1)[0])
        except (OSError, ValueError, herdr.HerdrFailure, subprocess.SubprocessError) as error:
            _anomaly(row, error)
        _restore_workers(row, record)
        print(f"{row.name}: restarted")
        return result.pane


def row_panes(rows: list[staff.StaffRow], client: herdr.Herdr) -> list[tuple[staff.StaffRow, str]]:
    try:
        panes = client.panes()
        launches = {entry["session_id"]: entry for entry in sorted(pane_record.launches(), key=lambda entry: entry["written_at"])}
        pane_ids = {pane["pane_id"] for pane in panes}
        found: list[tuple[staff.StaffRow, str]] = []
        for row in rows:
            matches = [pane["pane_id"] for pane in panes if pane["session_id"] == row.current_session]
            if matches:
                found.extend((row, pane) for pane in matches)
                continue
            entry = max((launches[reference] for reference in (str(row.id), str(row.current_session)) if reference in launches),
                        key=lambda entry: entry["written_at"], default=None)
            if entry is not None and entry["pane_id"] in pane_ids:
                found.append((row, entry["pane_id"]))
        return found
    except (OSError, ValueError, herdr.HerdrFailure) as error:
        print(f"restart: layout anomaly: {str(error).splitlines()[0]}", file=sys.stderr)
        return []


def live_members(rows: Iterable[staff.StaffRow], record: staff.StaffSnapshot, *,
                 resolver: session_staff.RowResolver | None = None,
                 codex: herdr.CodexConversations | None = None) -> list[staff.StaffRow]:
    """Read only the supplied rows and return their live local conversations."""
    from scripts.shutdown import remote

    selected: list[staff.StaffRow] = []
    shared_unknown = 0
    resolver = session_staff.RowResolver(record) if resolver is None else resolver
    for row in rows:
        try:
            machine, _folder = shutdown_record.row_folder(row, record, resolver=resolver)
            if machine != remote.local_machine():
                continue
            if codex is None:
                observed = herdr.read_conversation(str(row.current_session), "claude")
                if not isinstance(observed, herdr.LiveConversation):
                    codex = herdr.codex_conversations()
                    herdr.codex_notes(codex)
                    observed = herdr.read_conversation(str(row.current_session), codex=codex)
            else:
                observed = herdr.read_conversation(str(row.current_session), codex=codex)
            if isinstance(observed, herdr.LiveConversation):
                selected.append(row)
            elif isinstance(observed, herdr.UnknownConversation):
                if observed.shared_codex:
                    shared_unknown += 1
                if not observed.shared_codex or os.environ.get("SHUTDOWN_DETAIL") == "1":
                    herdr.conversation_note(observed)
        except (OSError, ValueError) as error:
            _anomaly(row, error)
    if shared_unknown:
        print(f"anomaly: {shared_unknown} rows not proved live or stopped (shared cause above); not blocking", file=sys.stderr)
    return selected


def live_rows(record: staff.StaffSnapshot, client: herdr.Herdr, *, save_tabs: bool = False,
              resolver: session_staff.RowResolver | None = None) -> list[staff.StaffRow]:
    resolver = session_staff.RowResolver(record) if resolver is None else resolver
    # The registry supplies candidate names; the reader proves each conversation.
    try:
        candidates = {session["sessionId"]: (session["name"] or session["sessionId"]) +
            (f" in {folder}" if (folder := session.get("cwd")) else "") for session in sessions.running_sessions()}
    except (OSError, ValueError) as error:
        print(f"restart: Claude registry anomaly: {error}", file=sys.stderr)
        candidates = {}
    codex = herdr.codex_conversations()
    for identity in herdr.running_codex_threads(thread_host=herdr.codex_managed_daemon, codex=codex):
        _ = candidates.setdefault(identity, identity)
    owned = {row.current_session for row in record.rows}
    for identity in sorted(candidates.keys() - owned):
        if isinstance(herdr.read_conversation(identity, codex=codex), herdr.LiveConversation):
            print(f"{' '.join(candidates[identity].splitlines())}: restart anomaly: unassigned live session", file=sys.stderr)
    workers: dict[staff.StaffId, WorkerRow] = {}
    for holder in record.rows:
        if holder.kind in ("unit", "showrunner") and any(child.parent == staff.ReportsTo(holder.id) for child in record.rows) and pane_workers(holder):
            workers.update((state.row.id, state) for state in worker_panes.observe(holder.id, record))
    selected = live_members((row for row in record.rows if row.id not in workers), record,
                            resolver=resolver, codex=codex)
    selected.extend(state.row for state in workers.values()
                    if not isinstance(state, (worker_panes.ClosedWorker, worker_panes.StoppedWorker)))
    if save_tabs:
        try:
            client.save_layout()
        except (OSError, ValueError, herdr.HerdrFailure, subprocess.SubprocessError) as error:
            print(f"restart: layout anomaly: {str(error).splitlines()[0]}", file=sys.stderr)
        for row, pane in row_panes(selected, client):
            try:
                client.remember_row_tab(str(row.id), pane)
            except (OSError, ValueError, herdr.HerdrFailure, subprocess.SubprocessError) as error:
                _anomaly(row, error)
    return selected


def restart(*, stop_all: bool = False, dry_run: bool = False, only: tuple[str, ...] = ()) -> int:
    try:
        record = staff.initial_snapshot()
    except (OSError, ValueError, staff.StaffRefused) as error:
        print(f"restart: staff anomaly: {str(error).splitlines()[0]}", file=sys.stderr)
        return 0
    resolver = session_staff.RowResolver(record)
    client = herdr.Herdr(herdr.shared_session())
    selected: list[staff.StaffRow] = []
    if stop_all:
        selected = live_rows(record, client, save_tabs=not dry_run, resolver=resolver)
    select_only = bool(only) and not stop_all
    pending_ids = list(only) if select_only else shutdown_record.read()
    if dry_run and not select_only and not stop_all:
        path = shutdown_record.record_path()
        saved_at = time.strftime("%H:%M", time.localtime(path.stat().st_mtime)) if path.exists() else "unknown"
        print(f"restart record: {len(pending_ids)} rows saved at {saved_at}")
        for row in live_rows(record, client, resolver=resolver):
            if str(row.id) not in pending_ids:
                print(f"{row.name}: live, not in the restart record")
    for reference in pending_ids:
        row = record.by_member_reference(reference) if select_only else record.by_id(staff.StaffId(reference))
        if select_only and row is None:
            matches = [item for item in record.rows if item.name == reference]
            row = matches[0] if len(matches) == 1 else None
        if row is None:
            print(f"{reference}: unknown or ambiguous staff row" if select_only else
                  f"{' '.join(reference.split())}: restart anomaly: in the restart record, no fixed staff id", file=sys.stderr)
            continue
        if row not in selected:
            selected.append(row)
    if dry_run:
        positions = {tab.get("row_id"): f"workspace {space['name']}, tab {index} ({tab['name']})"
                     for space in herdr.saved_layout(client.session)["workspaces"]
                     for index, tab in enumerate(space["tabs"], 1) if tab.get("row_id")}
        visible = row_panes(list(record.rows) if stop_all else selected, client)
        for row, pane in visible:
            if row not in selected:
                continue
            try:
                workspace, index, tab = client.pane_tab(pane)
                positions[str(row.id)] = f"workspace {workspace}, tab {index} ({tab['name']})"
            except (OSError, ValueError, herdr.HerdrFailure) as error:
                _anomaly(row, error)
        for row in selected:
            print(f"{row.name}: {row.id}, {row.kind}, {positions.get(str(row.id), 'no tab')}")
        for row in {row.id: row for row, _pane in visible if row not in selected}.values():
            print(f"{row.name}: left alone; no proved local restart target")
        return 0
    errors = (OSError, ValueError, staff.StaffRefused, herdr.HerdrFailure, subprocess.SubprocessError)
    pending: list[str] = []
    started: list[str] = []
    holders: dict[staff.StaffId, staff.StaffRow] = {}
    for row in selected:
        parent = record.parent_of(row) if row.kind == "worker" else None
        holder = parent if parent is not None and pane_workers(parent) else row
        holders[holder.id] = holder
    if stop_all:
        for holder in tuple(holders.values()):
            try:
                with staff.row_operation(str(holder.id)):
                    shutdown_record.update(started=[], pending=[str(row.id) for row in selected
                        if row.id == holder.id or row.parent == staff.ReportsTo(holder.id)])
                    if holder.kind in ("unit", "showrunner"):
                        _ = stop_resume.stop_unit_workers(holder.id, record)
                    _ = stop_row(holder, client)
            except staff.StaffRefused as error:
                _ = holders.pop(holder.id)
                _anomaly(holder, error)
            except errors as error:
                _anomaly(holder, error)
    present = False
    if holders:
        try:
            server = herdr.restore_server(client.session, int(herdr.PROGRAM_TIMEOUT_SECONDS * 1000),
                                     session_kind=herdr.security_session())
            present = isinstance(server, herdr.WindowServerCreated)
        except errors as error:
            print(f"restart: server anomaly: {str(error).splitlines()[0]}", file=sys.stderr)
    for holder in holders.values():
        members = [str(row.id) for row in selected if row.id == holder.id or
                   isinstance(row.parent, staff.ReportsTo) and row.parent.id == holder.id]
        try:
            with staff.row_operation(str(holder.id)):
                current_record = staff.initial_snapshot()
                current = current_record.by_id(holder.id)
                if current is None:
                    raise ValueError("staff row no longer exists")
                resolver.record = current_record
                _ = start_row(current, current_record, client, present=present, resolver=resolver)
            started.extend(members)
            present = False
        except staff.StaffRefused as error:
            _anomaly(holder, error)
        except errors as error:
            pending.extend(members)
            _anomaly(holder, error)
    if started or pending:
        shutdown_record.update(started=started, pending=pending)
    if started:
        try:
            _ = client.sort_tabs()
        except errors as error:
            print(f"restart: layout anomaly: {str(error).splitlines()[0]}", file=sys.stderr)
    return 0
