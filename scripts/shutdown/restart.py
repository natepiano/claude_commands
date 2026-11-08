"""Stop live staff rows, then resume their current sessions in their saved tabs."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import io
import math
import os
import subprocess
import sys
import time
from collections.abc import Iterable
from contextlib import redirect_stderr, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Event
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol, TypedDict, cast
from importlib import import_module

from scripts.herdr import herdr
from scripts.message import sessions, codex_daemon
from scripts.production import staff, session_staff
from scripts.accounts import accounts
from scripts.lib import conf, nix_env, operation_deadline, unit_runs
from scripts.lib.atomic_file import write_text_atomic
from scripts.shutdown import member_account, shutdown_record, pane_record
from scripts.shutdown.stop_results import StopResult, Stopped, NotRunning, StopFailed
from scripts.shutdown.codex_trust import trust_arguments
from scripts.herdr import pane_launch
from scripts.whoami.account import named_account_environment

class StopReadArguments(TypedDict, total=False):
    """Forward shared observations only when the caller supplies them."""

    codex: herdr.CodexConversations | None
    runs: dict[str, list[str]] | None


@dataclass(frozen=True)
class AllRestartAccounts:
    """Select rows without an account filter."""


ALL_RESTART_ACCOUNTS = AllRestartAccounts()


def account_matches(kind: Literal["claude", "codex"], environment: dict[str, str],
                    account: accounts.AccountSelection) -> bool:
    if isinstance(account, accounts.NamedAccount):
        return kind == "claude" and Path(environment.get("CLAUDE_CONFIG_DIR") or
            accounts.main_config_dir()).expanduser().resolve() == account.dir.resolve()
    return kind == "codex" and Path(environment.get("CODEX_HOME") or
        accounts.accounts_home() / ".codex").expanduser().resolve() == account.home.resolve()


class WorkerRestartScope(Enum):
    FOLLOW_OWNER = "restore the owner's workers"
    SELECTED_ROWS = "restart only selected rows"


class ClaudeTrustWriteFailed(OSError):
    """The shared launch already reported its failed trust write."""


class WorkerRow(Protocol):
    row: staff.StaffRow


class WorkerPanes(Protocol):
    StoppedWorker: type[WorkerRow]
    ClosedWorker: type[WorkerRow]
    def observe(self, parent: staff.StaffId, record: staff.StaffSnapshot) -> list[WorkerRow]: ...
    def observe_parents(self, parents: list[staff.StaffRow], record: staff.StaffSnapshot
                        ) -> dict[staff.StaffId, list[WorkerRow]]: ...
    def worker_run(self, member: staff.StaffRow, *,
                   record: staff.StaffSnapshot | unit_runs.RunStaffAtUse = unit_runs.RunStaffAtUse.LOCAL,
                   scanned: dict[str, list[str]] | None = None) -> Path: ...


class WorkerRestore(Protocol):
    def restore(self, parent: staff.StaffId, *, record: staff.StaffSnapshot | None = None,
                runs: dict[str, list[str]] | None = None,
                codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT) -> int: ...


class AgentsCommand(Protocol):
    def main(self, argv: list[str]) -> int: ...


class MemberLifecycle(Protocol):
    def detach(self, arguments: list[str], task: str, *, program: Path) -> Path: ...
    def stop(self, session: str, *, deadline: float,
             codex: herdr.CodexConversations | None = None,
             runs: dict[str, list[str]] | None = None) -> StopResult: ...
    def stop_unit_workers(self, unit: staff.StaffId, record: staff.StaffSnapshot, *,
                          codex: herdr.CodexConversations | None = None,
                          runs: dict[str, list[str]] | None = None) -> list[str]: ...


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


def stop_row(row: staff.StaffRow, client: herdr.Herdr, *, deadline: float = math.inf,
             codex: herdr.CodexConversations | None = None,
             runs: dict[str, list[str]] | None = None) -> bool:
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
        shared: StopReadArguments = {"codex": codex, "runs": runs} if codex is not None or runs is not None else {}
        result = stop_resume.stop(str(row.current_session), deadline=deadline, **shared)
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


@dataclass(frozen=True)
class RestartObservations:
    codex: herdr.CodexConversations
    runs: dict[str, list[str]]
    pane_owners: frozenset[staff.StaffId]


def _observations(record: staff.StaffSnapshot, client: herdr.Herdr) -> RestartObservations:
    owners = [row for row in record.rows if row.kind in ("unit", "showrunner")
              and any(child.parent == staff.ReportsTo(row.id) for child in record.rows)]
    runs = unit_runs.held_by_session(os.environ.get("ROSTER_RUN_ROOT"), record=record) if owners else {}
    pane_owners: set[staff.StaffId] = set()
    for row in owners:
        directory = worker_panes.worker_run(row, record=record, scanned=runs)
        if unit_runs.worker_transport(directory) != "mesh":
            pane_owners.add(row.id)
    return RestartObservations(_codex_conversations(client), runs, frozenset(pane_owners))


def _restore_workers(row: staff.StaffRow, record: staff.StaffSnapshot, *,
                     observed: RestartObservations | None = None) -> None:
    worker = cast(WorkerRestore, cast(object, import_module("scripts.production.worker")))
    if row.kind in ("unit", "showrunner") and any(
            child.kind == "worker" and child.parent == staff.ReportsTo(row.id) for child in record.rows
            ) and (pane_workers(row) if observed is None else row.id in observed.pane_owners):
        result = (worker.restore(row.id) if observed is None else
                  worker.restore(row.id, record=record, runs=observed.runs, codex=observed.codex))
        if result != 0:
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
              resolver: session_staff.RowResolver | None = None,
              worker_scope: WorkerRestartScope = WorkerRestartScope.FOLLOW_OWNER,
              observed: RestartObservations | None = None) -> str:
    with operation_deadline.within(deadline):
        live = (herdr.read_conversation(str(row.current_session)) if observed is None else
                herdr.read_conversation(str(row.current_session), codex=observed.codex))
        if isinstance(live, herdr.LiveConversation):
            if worker_scope is WorkerRestartScope.FOLLOW_OWNER:
                _restore_workers(row, record, **({"observed": observed} if observed is not None else {}))
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
        selected = member_account.resume_account(kind, row, to=target_account)
        if isinstance(selected, accounts.NamedAccount):
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
        current = (herdr.read_conversation(str(row.current_session), kind) if observed is None else
                   herdr.read_conversation(str(row.current_session), kind, codex=observed.codex))
        if isinstance(current, herdr.LiveConversation):
            if worker_scope is WorkerRestartScope.FOLLOW_OWNER:
                _restore_workers(row, record, **({"observed": observed} if observed is not None else {}))
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
                              live.reason if isinstance(live, herdr.UnknownConversation) else ""),
            codex=observed.codex if observed is not None else herdr.CodexLaunchRead.CURRENT)
        if isinstance(result.outcome, herdr.ClaudeTrustNotWritten):
            raise ClaudeTrustWriteFailed(result.outcome.reason)
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
        if worker_scope is WorkerRestartScope.FOLLOW_OWNER:
            _restore_workers(row, record, **({"observed": observed} if observed is not None else {}))
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
        missing = [row for row in rows if row.kind == "agent" and not any(item.id == row.id for item, _pane in found)]
        if missing:
            scan = client.scan_row_panes(staff.StaffSnapshot(tuple(missing)), [dict(pane) for pane in panes])
            found.extend((row, str(pane["pane_id"])) for row in missing for pane in scan.found.get(row.id, []))
        return found
    except (OSError, ValueError, herdr.HerdrFailure) as error:
        print(f"restart: layout anomaly: {str(error).splitlines()[0]}", file=sys.stderr)
        return []


def live_members(rows: Iterable[staff.StaffRow], record: staff.StaffSnapshot, *,
                 resolver: session_staff.RowResolver | None = None,
                 codex: herdr.CodexConversations | None = None,
                 account: AllRestartAccounts | accounts.AccountSelection = ALL_RESTART_ACCOUNTS) -> list[staff.StaffRow]:
    """Read only the supplied rows and return their live local conversations."""
    from scripts.shutdown import remote

    selected: list[staff.StaffRow] = []
    shared_unknown = 0
    hosted = {session.thread_id: read.owner.home
              for read in codex_daemon.read_daemons(os.environ)
              if not isinstance(read.sessions, codex_daemon.DaemonUnreadable)
              for session in read.sessions} if not isinstance(account, AllRestartAccounts) else {}
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
                if (isinstance(account, AllRestartAccounts)
                        or any(account_matches(observed.kind, herdr.process_environment(pid), account)
                               for pid in observed.pids)
                        or not observed.pids and observed.kind == "codex"
                        and isinstance(account, accounts.CodexWorkersAccount)
                        and (home := hosted.get(str(row.current_session))) is not None
                        and home.resolve() == account.home.resolve()):
                    selected.append(row)
                elif row.kind == "worker" and not observed.pids and (
                        observed.kind == "claude" or str(row.current_session) not in hosted):
                    print(f"{row.name}: left alone; account unknown")
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


def _codex_conversations(client: herdr.Herdr) -> herdr.CodexConversations:
    """Use the native pane reader for resumed Codex processes without open rollouts."""
    observed = herdr.codex_conversations()
    if not observed.unknown:
        return observed
    live = dict(observed.live)
    unknown = list(observed.unknown)
    try:
        panes = client.listed("pane")
    except (OSError, ValueError, herdr.HerdrFailure):
        return observed
    for pane in panes:
        if pane.get("agent") != "codex":
            continue
        try:
            pane_id = herdr.json_text(pane.get("pane_id"), "pane id")
            info = herdr.json_object(client.result("pane", "process-info", "--pane", pane_id).get("process_info"),
                                     "native pane processes")
            identity = herdr.pane_native_process(info, "codex", pane).current_identity
            _ = live.setdefault(identity, ())
            name = client.pane_tab(pane_id)[2]["name"]
            unknown = [reason for reason in unknown if reason != f"{name} (Codex thread unknown)"]
        except (OSError, ValueError, herdr.HerdrFailure):
            continue
    return herdr.CodexConversations(live, tuple(unknown))


@herdr.observation_snapshot()
def live_rows(record: staff.StaffSnapshot, client: herdr.Herdr, *, save_tabs: bool = False,
              resolver: session_staff.RowResolver | None = None,
              account: AllRestartAccounts | accounts.AccountSelection = ALL_RESTART_ACCOUNTS,
              observed: RestartObservations | None = None) -> list[staff.StaffRow]:
    observed = _observations(record, client) if observed is None else observed
    resolver = session_staff.RowResolver(record) if resolver is None else resolver
    # The registry supplies candidate names; the reader proves each conversation.
    try:
        candidates = {session["sessionId"]: (session["name"] or session["sessionId"]) +
            (f" in {folder}" if (folder := session.get("cwd")) else "") for session in sessions.running_sessions()}
    except (OSError, ValueError) as error:
        print(f"restart: Claude registry anomaly: {error}", file=sys.stderr)
        candidates = {}
    codex = observed.codex
    for identity in herdr.running_codex_threads(thread_host=herdr.codex_managed_daemon, codex=codex):
        _ = candidates.setdefault(identity, identity)
    owned = {row.current_session for row in record.rows}
    for identity in sorted(candidates.keys() - owned):
        if isinstance(herdr.read_conversation(identity, codex=codex), herdr.LiveConversation):
            print(f"{' '.join(candidates[identity].splitlines())}: restart anomaly: unassigned live session", file=sys.stderr)
    owners = [row for row in record.rows if row.id in observed.pane_owners]
    workers = {state.row.id: state for states in worker_panes.observe_parents(owners, record).values()
               for state in states} if owners else {}
    active_workers = [state.row for state in workers.values()
                      if not isinstance(state, (worker_panes.ClosedWorker, worker_panes.StoppedWorker))]
    candidates_rows = [row for row in record.rows if row.id not in workers]
    if not isinstance(account, AllRestartAccounts):
        candidates_rows.extend(active_workers)
    selected = live_members(candidates_rows, record, resolver=resolver, codex=codex, account=account)
    if isinstance(account, AllRestartAccounts):
        selected.extend(active_workers)
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


def _after_unconfirmed_stop(row: staff.StaffRow, before: herdr.CodexConversations
                            ) -> herdr.LiveConversation | herdr.StoppedConversation | herdr.UnknownConversation:
    identity = str(row.current_session)
    kind, _environment, _arguments = session_launch(identity, row.name)
    if kind == "claude":
        return herdr.read_conversation(identity, kind)
    pids = before.live.get(identity, ())
    if not pids:
        return herdr.UnknownConversation(f"{identity}: the unconfirmed stop has no target pid")
    live: list[int] = []
    unknown: list[str] = []
    for pid in pids:
        try:
            process = herdr.native_process(pid, kind)
            if process.current_identity != identity:
                raise ValueError(f"pid {pid}: current conversation differs; original generation is unknown")
            state = sessions.process_liveness(pid, process.started)
            if isinstance(state, sessions.LiveSessionProcess):
                live.append(pid)
            elif isinstance(state, sessions.SessionLivenessUnknown):
                raise OSError(state.reason)
            else:
                current = herdr.native_process(pid, kind)
                if current.started != process.started and current.current_identity != identity:
                    continue
                raise ValueError(f"pid {pid}: generation changed without proof that the target exited")
        except (OSError, ValueError, herdr.HerdrFailure) as error:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except OSError:
                pass
            unknown.append(str(error))
    if live:
        return herdr.LiveConversation("codex", tuple(live))
    if unknown:
        return herdr.UnknownConversation(unknown[0])
    return herdr.StoppedConversation()


class AfterStop(Enum):
    RESTART = "resume the selected rows"
    KEEP_STOPPED = "keep the selected rows for the next restart"


def stop_all(*, dry_run: bool = False) -> int:
    return restart(stop_all=True, dry_run=dry_run, after_stop=AfterStop.KEEP_STOPPED)


def restart(*, stop_all: bool = False, dry_run: bool = False, only: tuple[str, ...] = (),
            account: AllRestartAccounts | accounts.AccountSelection = ALL_RESTART_ACCOUNTS,
            after_stop: AfterStop = AfterStop.RESTART) -> int:
    try:
        record = staff.initial_snapshot()
    except (OSError, ValueError, staff.StaffRefused) as error:
        print(f"restart: staff anomaly: {str(error).splitlines()[0]}", file=sys.stderr)
        return 0
    with herdr.observation_snapshot():
        resolver = session_staff.RowResolver(record)
        client = herdr.Herdr(herdr.shared_session())
        observed = _observations(record, client)
        selected: list[staff.StaffRow] = []
        if stop_all:
            selected = live_rows(record, client, save_tabs=not dry_run, resolver=resolver, account=account, observed=observed)
            if only:
                requested = {row.id for reference in only if (row := record.by_member_reference(reference)) is not None}
                selected = [row for row in selected if row.id in requested]
        select_only = bool(only) and not stop_all
        pending_ids = ([] if after_stop == AfterStop.KEEP_STOPPED else
                       list(only) if select_only else
                       [] if stop_all and (only or not isinstance(account, AllRestartAccounts)) else shutdown_record.read())
        if dry_run and not select_only and not stop_all and isinstance(account, AllRestartAccounts):
            path = shutdown_record.record_path()
            saved_at = time.strftime("%H:%M", time.localtime(path.stat().st_mtime)) if path.exists() else "unknown"
            print(f"restart record: {len(pending_ids)} rows saved at {saved_at}")
            for row in live_rows(record, client, resolver=resolver, observed=observed):
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
            if not isinstance(account, AllRestartAccounts):
                try:
                    kind, environment, _arguments = session_launch(str(row.current_session), row.name)
                    if not account_matches(kind, environment, account):
                        continue
                except (OSError, ValueError) as error:
                    _anomaly(row, error)
                    continue
            if row not in selected:
                selected.append(row)
        if dry_run:
            positions = {tab.get("row_id"): f"workspace {space['name']}, tab {index} ({tab['name']})"
                         for space in herdr.saved_layout(client.session)["workspaces"]
                         for index, tab in enumerate(space["tabs"], 1) if tab.get("row_id")}
            visible = row_panes(list(record.rows) if stop_all and not only and isinstance(account, AllRestartAccounts) else selected, client)
            for row, pane in visible:
                if row not in selected:
                    continue
                try:
                    workspace, index, tab = client.pane_tab(pane)
                    positions[str(row.id)] = f"workspace {workspace}, tab {index} ({tab['name']})"
                except (OSError, ValueError, herdr.HerdrFailure) as error:
                    _anomaly(row, error)
            for row in selected:
                action = "; would stop and save" if after_stop == AfterStop.KEEP_STOPPED else ""
                print(f"{row.name}: {row.id}, {row.kind}, {positions.get(str(row.id), 'no tab')}{action}")
            for row in {row.id: row for row, _pane in visible if row not in selected}.values():
                print(f"{row.name}: left alone; no proved local restart target")
            return 0
    errors = (OSError, ValueError, staff.StaffRefused, herdr.HerdrFailure, subprocess.SubprocessError)
    pending: list[str] = []
    started: list[str] = []
    worker_scope = (WorkerRestartScope.SELECTED_ROWS if only or not isinstance(account, AllRestartAccounts)
                    else WorkerRestartScope.FOLLOW_OWNER)
    exit_code = 0
    holders: dict[staff.StaffId, staff.StaffRow] = {}
    members_by_holder: dict[staff.StaffId, list[str]] = {}
    selected_ids = {row.id for row in selected}
    for row in selected:
        parent = record.parent_of(row) if row.kind == "worker" else None
        holder = (parent if worker_scope is WorkerRestartScope.FOLLOW_OWNER
                  and parent is not None and parent.id in selected_ids and parent.id in observed.pane_owners else row)
        holders[holder.id] = holder
        members_by_holder.setdefault(holder.id, []).append(str(row.id))
    callers = {os.environ.get("CLAUDE_CODE_SESSION_ID"), os.environ.get("CODEX_THREAD_ID")} - {None, ""}
    if stop_all and any(row.current_session in callers for row in selected):
        arguments = ["restart", "--all", "--only", ",".join(str(row.id) for row in selected)]
        if not isinstance(account, AllRestartAccounts):
            arguments.append(account.label)
        log = stop_resume.detach(arguments, "restart", program=Path(__file__).resolve())
        print(f"restart continues outside this session; log: {log}")
        return 0
    selected_sessions = {str(row.current_session) for row in selected} if stop_all else set[str]()
    pre_stop_codex = observed.codex
    unconfirmed: set[staff.StaffId] = set()
    unconfirmed_workers: set[staff.StaffId] = set()
    def stop_holder(holder: staff.StaffRow, previous: Event, recorded: Event) -> tuple[bool, tuple[staff.StaffId, ...]]:
        _ = previous.wait()
        try:
            with staff.row_operation(str(holder.id)):
                shutdown_record.update(started=[], pending=members_by_holder[holder.id])
                recorded.set()
                workers: tuple[staff.StaffId, ...] = ()
                if worker_scope is WorkerRestartScope.FOLLOW_OWNER and holder.kind in ("unit", "showrunner"):
                    unresolved = stop_resume.stop_unit_workers(holder.id, record, codex=observed.codex, runs=observed.runs)
                    if unresolved and holder.id in observed.pane_owners:
                        workers = tuple(child.id for child in record.find(kind="worker", parent=holder.id)
                                        if child.id in selected_ids)
                return stop_row(holder, client, codex=observed.codex, runs=observed.runs), workers
        finally:
            recorded.set()

    if stop_all and holders:
        recorded = [Event() for _ in range(len(holders) + 1)]
        recorded[0].set()
        with ThreadPoolExecutor(max_workers=min(16, len(holders))) as pool:
            futures = [(holder, pool.submit(copy_context().run, stop_holder, holder, recorded[index], recorded[index + 1]))
                       for index, holder in enumerate(tuple(holders.values()))]
            for holder, future in futures:
                try:
                    stopped, workers = future.result()
                    unconfirmed_workers.update(workers)
                    if not stopped:
                        unconfirmed.add(holder.id)
                except staff.StaffRefused as error:
                    _ = holders.pop(holder.id)
                    _anomaly(holder, error)
                except errors as error:
                    unconfirmed.add(holder.id)
                    if holder.id in observed.pane_owners:
                        unconfirmed_workers.update(child.id for child in record.find(kind="worker", parent=holder.id)
                                                   if child.id in selected_ids)
                    _anomaly(holder, error)
    if after_stop == AfterStop.KEEP_STOPPED:
        return 0
    blocked_workers: set[staff.StaffId] = set()
    for child in record.rows:
        if child.id not in unconfirmed_workers:
            continue
        try:
            fresh = _after_unconfirmed_stop(child, pre_stop_codex)
        except errors as error:
            fresh = herdr.UnknownConversation(str(error))
        if not isinstance(fresh, herdr.StoppedConversation):
            blocked_workers.add(child.id)
            selected_sessions.discard(str(child.current_session))
            _anomaly(child, "target process still runs; worker kept pending" if isinstance(fresh, herdr.LiveConversation) else fresh.reason)
    observed = RestartObservations(herdr.CodexConversations(
        {identity: pids for identity, pids in observed.codex.live.items() if identity not in selected_sessions},
        observed.codex.unknown), observed.runs, observed.pane_owners)
    present = False
    if holders:
        try:
            server = herdr.restore_server(client.session, int(herdr.PROGRAM_TIMEOUT_SECONDS * 1000),
                                     session_kind=herdr.security_session())
            present = isinstance(server, herdr.WindowServerCreated)
        except errors as error:
            print(f"restart: server anomaly: {str(error).splitlines()[0]}", file=sys.stderr)
    def start_holder(holder: staff.StaffRow, focus: bool) -> None:
        with staff.row_operation(str(holder.id)):
            current_record = staff.initial_snapshot()
            current_record = replace(current_record, rows=tuple(row for row in current_record.rows if row.id not in blocked_workers))
            current = current_record.by_id(holder.id)
            if current is None:
                raise ValueError("staff row no longer exists")
            if holder.id in unconfirmed:
                fresh = _after_unconfirmed_stop(current, pre_stop_codex)
                if isinstance(fresh, herdr.UnknownConversation):
                    raise OSError(fresh.reason)
                if isinstance(fresh, herdr.LiveConversation):
                    if worker_scope is WorkerRestartScope.FOLLOW_OWNER:
                        _restore_workers(current, current_record, observed=observed)
                    print(f"{current.name}: unchanged")
                    return
            facts = session_staff.RowResolver(current_record)
            if worker_scope is WorkerRestartScope.FOLLOW_OWNER:
                _ = start_row(current, current_record, client, present=focus, resolver=facts, observed=observed)
            else:
                _ = start_row(current, current_record, client, present=focus, resolver=facts,
                              target_account="" if isinstance(account, AllRestartAccounts) else account,
                              worker_scope=worker_scope, observed=observed)

    if holders:
        with ThreadPoolExecutor(max_workers=min(16, len(holders))) as pool:
            futures = [(holder, pool.submit(copy_context().run, start_holder, holder, present and index == 0))
                       for index, holder in enumerate(holders.values())]
            for holder, future in futures:
                members = members_by_holder[holder.id]
                try:
                    future.result()
                    started.extend(member for member in members if staff.StaffId(member) not in blocked_workers)
                    pending.extend(member for member in members if staff.StaffId(member) in blocked_workers)
                except ClaudeTrustWriteFailed:
                    pending.extend(members)
                    exit_code = 1
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
    return exit_code


def main() -> int:
    """Run detached restart work and save its output."""
    command = cast(AgentsCommand, cast(object, import_module("scripts.shutdown.shutdown")))

    parser = argparse.ArgumentParser(add_help=False)
    _ = parser.add_argument("--log", type=Path, required=True)
    _ = parser.add_argument("--requester", default="")
    options, arguments = parser.parse_known_args()
    output = io.StringIO()
    _ = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
    _ = os.environ.pop("CODEX_THREAD_ID", None)
    try:
        with redirect_stdout(output), redirect_stderr(output):
            return command.main(arguments)
    finally:
        write_text_atomic(cast(Path, options.log), output.getvalue())


if __name__ == "__main__":
    raise SystemExit(main())
