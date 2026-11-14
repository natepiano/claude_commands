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
from contextlib import ExitStack, nullcontext, redirect_stderr, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Event
from dataclasses import dataclass, field, replace
from enum import Enum
from functools import partial
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, cast
from importlib import import_module

from scripts.herdr import herdr
from scripts.herdr import herdr_types
from scripts.message import sessions, codex_daemon
from scripts.production import staff, session_staff, worker_admission
from scripts.accounts import accounts
from scripts.lib import conf, models_registry, nix_env, operation_deadline, unit_runs, resume_message
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.transcripts import codex_transcript_homes, open_transcript, transcript_paths
from scripts.agents import member_account, shutdown_record, pane_record
from scripts.agents.stop_results import StopResult, Stopped, NotRunning, StopFailed
from scripts.agents.codex_trust import trust_arguments
from scripts.herdr import pane_launch
from scripts.whoami.account import named_account_environment

if TYPE_CHECKING:
    from scripts.herdr.worker_panes import AllWorkers, SelectedWorkerRows as WorkerRowSelection

class StopReadArguments(TypedDict, total=False):
    """Forward shared observations only when the caller supplies them."""

    codex: herdr.CodexConversations | None


@dataclass(frozen=True)
class AllRestartAccounts:
    """Select rows without an account filter."""


ALL_RESTART_ACCOUNTS = AllRestartAccounts()
NO_RESTART_MESSAGE = herdr.NoOpeningPrompt()
DEFAULT_RESTART_CAUSE = "agents restart ran"


def account_matches(kind: Literal["claude", "codex"], environment: dict[str, str],
                    account: accounts.AccountSelection) -> bool:
    if isinstance(account, accounts.NamedAccount):
        return kind == "claude" and Path(environment.get("CLAUDE_CONFIG_DIR") or
            accounts.main_config_dir()).expanduser().resolve() == account.dir.resolve()
    return kind == "codex" and Path(environment.get("CODEX_HOME") or
        accounts.accounts_home() / ".codex").expanduser().resolve() == account.home.resolve()


class AllRestartRows(Enum):
    CURRENT = "all staff rows"


@dataclass(frozen=True)
class SelectedRestartRows:
    """Observe only the requested fixed staff rows."""

    ids: frozenset[staff.StaffId]


class WorkerRestartScope(Enum):
    FOLLOW_OWNER = "restore the owner's workers"
    SELECTED_ROWS = "restart only selected rows"


class RestartDisposition(Enum):
    STARTED = "this attempt launched the row"
    RESTORED = "the row needs no new launch"
    HELD = "the row keeps its pause or exit record"


class RestartSuccessReport(Enum):
    START_ROW = "start_row prints the success line"
    CALLER = "the caller prints the success line"


@dataclass(frozen=True)
class FailedHolderStart:
    """A failed start retains its arguments for one stopped-director retry."""

    holder: staff.StaffRow
    focus: bool
    error: Exception


class ResumeCheckState(Enum):
    NO_MESSAGE = "no continuation message"
    NO_WORK = "no work after the continuation message"
    UNREADABLE = "transcript unreadable"
    RESUMED = "resumed"


@dataclass
class CodexResumeTranscript:
    """Read new turns in the account and thread used at launch."""

    transcript: herdr.SessionTranscript
    since: datetime
    offsets: dict[Path, int] = field(default_factory=dict)

    def skip_existing_turns(self) -> None:
        try:
            source = worker_admission.codex_transcript_source(self.transcript.session_id, self.transcript.config_dir)
            with open_transcript(source) as stream:
                self.offsets[source] = herdr.complete_transcript_offset(stream)
        except (OSError, ValueError):
            pass

    def progress(self, text: str) -> ResumeCheckState:
        state = ResumeCheckState.NO_MESSAGE
        try:
            source = worker_admission.codex_transcript_source(self.transcript.session_id, self.transcript.config_dir)
            offset = self.offsets.get(source, 0)
            if source.stat().st_size < offset:
                offset = 0
            for row in worker_admission.transcript_records(
                    source, offset=offset, boundary=worker_admission.TranscriptRecordBoundary.COMPLETE_LINES):
                if isinstance(row, worker_admission.TornTranscriptRecord):
                    return ResumeCheckState.UNREADABLE
                stamp = row.get("timestamp")
                if not isinstance(stamp, str):
                    continue
                at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if at.tzinfo is None or at <= self.since:
                    continue
                entry = worker_admission.transcript_entry(row, set())
                if entry.startswith("user:\n") and text in entry:
                    state = ResumeCheckState.NO_WORK
                elif state is ResumeCheckState.NO_WORK:
                    raw = row.get("payload")
                    if isinstance(raw, dict):
                        payload = cast(dict[str, object], raw)
                        if (payload.get("role") == "assistant"
                                or payload.get("type") in ("agent_message", "function_call", "custom_tool_call")):
                            return ResumeCheckState.RESUMED
            return state
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            return ResumeCheckState.UNREADABLE


@dataclass
class DirectorResumeCheck:
    name: str
    text: str
    reader: herdr.TranscriptCursor | CodexResumeTranscript

    def progress(self) -> ResumeCheckState:
        if isinstance(self.reader, CodexResumeTranscript):
            return self.reader.progress(self.text)
        turns = self.reader.read_turns()
        if isinstance(self.reader.readability, herdr.TranscriptUnreadable) or not self.reader.current_paths:
            return ResumeCheckState.UNREADABLE
        state = ResumeCheckState.NO_MESSAGE
        for turn in sorted(turns, key=lambda turn: turn[0]):
            row = turn[1]
            if state is ResumeCheckState.NO_WORK and row.get("type") == "assistant":
                return ResumeCheckState.RESUMED
            if (row.get("type") == "user" and not row.get("isMeta", False)
                    and self.text in worker_admission.transcript_entry(row, set())):
                state = ResumeCheckState.NO_WORK
        return state


class NoDirectorResumeChecks(Enum):
    CURRENT = "this caller does not check director progress"


class RetainedPaneState(Enum):
    SHELL = "the retained pane has no native agent"
    EXITING = "the retained pane still has work or unknown evidence"
    ABSENT = "the retained pane is absent"
    FOREIGN = "another terminal or native owner holds the pane"


@dataclass(frozen=True)
class RetainedRestartPane:
    """The original terminal and publication owned by the restarted row."""

    row: staff.StaffRow
    publication: pane_launch.LaunchPublication
    client: herdr.Herdr
    terminal: str

    def state(self) -> RetainedPaneState:
        try:
            pane = next((pane for pane in self.client.panes()
                         if pane["pane_id"] == self.publication.entry["pane_id"]), None)
            if pane is None:
                return RetainedPaneState.ABSENT
            if not self.terminal or pane.get("terminal_id") != self.terminal:
                return RetainedPaneState.FOREIGN
            identities = pane_launch.native_sessions(self.client, pane)
            if identities - {str(self.row.current_session)}:
                return RetainedPaneState.FOREIGN
            if not identities and isinstance(self.client.pane_activity(pane["pane_id"]), herdr.PaneAtShell):
                return RetainedPaneState.SHELL
        except (OSError, ValueError, herdr_types.HerdrFailure):
            pass
        return RetainedPaneState.EXITING


@dataclass(frozen=True)
class ReplacedRestartPane:
    """A retained source whose replacement started before its terminal became idle."""

    source: RetainedRestartPane
    destination_client: herdr.Herdr
    destination: Path
    attempt: pane_launch.ResumeAttempt
    proof: pane_launch.VerifiedResume

    def finish(self) -> bool:
        state = self.source.state()
        if state is RetainedPaneState.FOREIGN:
            return True
        if state is RetainedPaneState.EXITING:
            return False
        try:
            pane_launch.finish_restore(self.source.publication.path, self.source.publication.entry,
                self.attempt, self.proof, close=self.destination_client.close_tab_confirmed,
                destination=self.destination,
                close_source=lambda pane: self.source.client.close_tab_confirmed(pane, terminal=self.source.terminal),
                source_pane=pane_launch.ResumeSourcePane(self.source.publication.entry["pane_id"]))
            return pane_launch.read_strict(self.source.publication.path).get(
                self.source.publication.entry["pane_id"]) != self.source.publication.entry
        except (OSError, ValueError, herdr_types.HerdrFailure):
            return False


@dataclass
class DirectorResumeChecks:
    checks: list[DirectorResumeCheck] = field(default_factory=list)
    replaced_panes: list[ReplacedRestartPane] = field(default_factory=list)

    def report(self) -> None:
        if not self.checks and not self.replaced_panes:
            return
        try:
            settings = conf.read("shutdown")
            deadline = time.monotonic() + settings.number("RESUME_CHECK_SECONDS")
            interval = settings.number("RESUME_CHECK_INTERVAL_SECONDS")
        except (OSError, ValueError):
            for check in self.checks:
                print(f"{check.name}: restart check: {ResumeCheckState.UNREADABLE.value}")
            print(f"restart check: 0 of {len(self.checks)} directors resumed")
            return
        states = [ResumeCheckState.NO_MESSAGE] * len(self.checks)
        while True:
            self.replaced_panes = [pane for pane in self.replaced_panes if not pane.finish()]
            for index, check in enumerate(self.checks):
                if states[index] is not ResumeCheckState.RESUMED:
                    states[index] = check.progress()
            if not self.replaced_panes and all(state is ResumeCheckState.RESUMED for state in states):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))
        for check, state in zip(self.checks, states):
            if state is not ResumeCheckState.RESUMED:
                print(f"{check.name}: restart check: {state.value}")
        passed = sum(state is ResumeCheckState.RESUMED for state in states)
        if self.checks:
            print(f"restart check: {passed} of {len(self.checks)} directors resumed")
        for pane in self.replaced_panes:
            _anomaly(pane.source.row, f"source pane {pane.source.client.session}:"
                     + f"{pane.source.publication.entry['pane_id']} still has work after the restart check")


class ClaudeTrustWriteFailed(OSError):
    """The shared launch already reported its failed trust write."""


class WorkerRow(Protocol):
    row: staff.StaffRow


class WorkerPanes(Protocol):
    StoppedWorker: type[WorkerRow]
    ClosedWorker: type[WorkerRow]
    def observe(self, parent: staff.StaffId, record: staff.StaffSnapshot) -> list[WorkerRow]: ...
    SelectedWorkerRows: type[WorkerRowSelection]
    def observe_parents(self, parents: list[staff.StaffRow], record: staff.StaffSnapshot, *,
                        selection: WorkerRowSelection | AllWorkers = ...
                        ) -> dict[staff.StaffId, list[WorkerRow]]: ...


class WorkerRestore(Protocol):
    def restore(self, parent: staff.StaffId, *, record: staff.StaffSnapshot | None = None,
                only: frozenset[staff.StaffId] | Literal["all"] = "all", account: str | accounts.AccountSelection = "",
                started_panes: dict[staff.StaffId, str] | Literal["discard"] = "discard",
                runs: dict[str, list[str]] | None = None,
                codex: herdr.CodexConversations | herdr.CodexLaunchRead = herdr.CodexLaunchRead.CURRENT) -> int: ...


class AgentsCommand(Protocol):
    def main(self, argv: list[str]) -> int: ...


class MemberLifecycle(Protocol):
    def detach(self, arguments: list[str], task: str, *, program: Path) -> Path: ...
    def stop(self, session: str, *, deadline: float,
             codex: herdr.CodexConversations | None = None) -> StopResult: ...
    def stop_unit_workers(self, unit: staff.StaffId, record: staff.StaffSnapshot, *,
                          codex: herdr.CodexConversations | None = None) -> list[str]: ...


worker_panes = cast(WorkerPanes, cast(object, import_module("scripts.herdr.worker_panes")))
stop_resume = cast(MemberLifecycle, cast(object, import_module("scripts.agents.stop_resume")))


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


def _error_reason(error: object) -> str:
    reason = str(error).splitlines()
    return reason[0] if reason else type(error).__name__


def _anomaly(row: staff.StaffRow, error: object) -> None:
    print(f"{row.name}: restart anomaly: {_error_reason(error)}", file=sys.stderr)


def stop_row(row: staff.StaffRow, client: herdr.Herdr, *, deadline: float = math.inf,
             codex: herdr.CodexConversations | None = None) -> bool:
    with operation_deadline.within(deadline):
        try:
            record = staff.initial_snapshot()
            if record.by_id(row.id) is None:
                record = staff.StaffSnapshot((*record.rows, row))
            panes = pane_launch.reference_panes(pane_launch.StaffLaunchReference(row.id), record, client,
                pane_record.read(client.session))
            if not panes and any(pane["session_id"] == row.current_session for pane in client.panes()):
                raise herdr_types.HerdrFailure(f"would signal a process that does not run session {row.current_session}",
                                               "wrong_target")
            if len(panes) > 1:
                _anomaly(row, "duplicate panes retained; no signal sent")
                return False
            if panes and not herdr.check_target(str(row.current_session), action="signal",
                    place=herdr.HerdrPlace(client.session, str(panes[0]["pane_id"])),
                    terminal=str(panes[0].get("terminal_id", "")), client=client):
                return False
            if panes:
                # End the restore entry before the signal, so no restore revives the stopped session.
                pane_record.end(client.session, str(panes[0]["pane_id"]), str(row.current_session),
                                record=staff.StaffSnapshot((row,)))
        except (herdr_types.HerdrFailure, OSError, ValueError) as error:
            _anomaly(row, error)
            return False
        shared: StopReadArguments = {"codex": codex} if codex is not None else {}
        result = stop_resume.stop(str(row.current_session), deadline=deadline, **shared)
        if isinstance(result, (Stopped, NotRunning)):
            return True
        _anomaly(row, result.reason if isinstance(result, StopFailed) else "session not found")
        return False


def _saved_launch(entry: pane_record.PaneLaunch, identity: str, name: str, *,
                  record: staff.StaffSnapshot | pane_launch.StaffSnapshotRead = pane_launch.StaffSnapshotRead.CURRENT) -> tuple[Literal["claude", "codex"], dict[str, str], list[str]]:
    record = staff.initial_snapshot() if isinstance(record, pane_launch.StaffSnapshotRead) else record
    projected = pane_launch.current_launch(entry, record)
    if "staff_id" not in projected:
        projected["session_id"] = identity
        projected["name"] = name
    arguments = pane_record.resume_arguments(projected)
    kind = pane_launch.saved_kind(projected)
    home = pane_launch.config_home(projected, record=record)
    if isinstance(home, staff.HomeUnknown):
        print(f"anomaly: {identity}: session home is unknown; select the normal account", file=sys.stderr)
        return kind, {}, arguments
    environment = {"CODEX_HOME": str(home.path)} if kind == "codex" else named_account_environment(home.path)
    return kind, environment, arguments


def producer_first_arguments(name: str) -> tuple[Literal["claude", "codex"], list[str]]:
    """Select the configured producer settings without a resume identity."""
    settings = models_registry.launch_settings("producers", name)
    if settings.kind == "codex":
        arguments = ["--model", settings.model,
                     "-c", f'model_reasoning_effort="{settings.effort}"']
        if settings.service_tier:
            arguments.extend(("-c", f'service_tier="{settings.service_tier}"'))
    else:
        arguments = ["--name", name,
                     "--model", settings.model, "--effort", settings.effort,
                     "--settings", '{"disableAgentView": true}']
    return settings.kind, arguments


def producer_launch(row: staff.StaffRow) -> tuple[Literal["claude", "codex"], dict[str, str], list[str]]:
    """Resume the producer's current conversation with its configured settings."""
    kind, arguments = producer_first_arguments(row.name)
    return kind, {}, ["resume" if kind == "codex" else "--resume", str(row.current_session), *arguments]


def producer_resume(row: staff.StaffRow, entry: pane_record.PaneLaunch
                    ) -> tuple[Literal["claude", "codex"], dict[str, str], list[str]]:
    """Keep saved launch options with the producer's current model settings."""
    settings = models_registry.launch_settings("producers", row.name)
    return settings.kind, {}, pane_record.configured_resume_arguments(entry, str(row.current_session), row.name, settings)


def session_launch(identity: str, name: str) -> tuple[Literal["claude", "codex"], dict[str, str], list[str]]:
    try:
        entry = pane_record.launch_for_session(identity)
    except (OSError, ValueError) as error:
        print(f"{identity}: launch record anomaly: {str(error).splitlines()[0]}", file=sys.stderr)
        entry = None
    if entry is not None:
        return _saved_launch(entry, identity, name)
    for directory in sessions.config_dirs():
        if transcript_paths(directory / "projects", f"*/{identity}.jsonl"):
            return "claude", named_account_environment(directory), ["--resume", identity, "--name", name]
    for directory in codex_transcript_homes():
        if transcript_paths(directory / "sessions", f"**/rollout-*{identity}.jsonl"):
            return "codex", {"CODEX_HOME": str(directory)}, ["resume", identity]
    raise OSError(f"no transcript for {identity}")


@dataclass(frozen=True)
class PaneConversations:
    codex: herdr.CodexConversations
    names: dict[str, str]


@dataclass(frozen=True)
class RestartObservations:
    codex: herdr.CodexConversations
    runs: dict[str, list[str]]
    pane_owners: frozenset[staff.StaffId]
    pane_names: dict[str, str]


def _observations(record: staff.StaffSnapshot, client: herdr.Herdr) -> RestartObservations:
    owners = frozenset(row.id for row in record.rows if row.kind in ("unit", "showrunner")
                       and record.find(kind="worker", parent=row.id))
    runs = unit_runs.held_by_session(os.environ.get("ROSTER_RUN_ROOT"), record=record) if owners else {}
    panes = _codex_conversations(client)
    return RestartObservations(panes.codex, runs, owners, panes.names)


def _restore_workers(row: staff.StaffRow, record: staff.StaffSnapshot, *,
                     observed: RestartObservations | None = None,
                     target_account: str | accounts.AccountSelection = "") -> str:
    worker = cast(WorkerRestore, cast(object, import_module("scripts.production.worker")))
    started_panes: dict[staff.StaffId, str] = {}
    if row.kind == "worker":
        parent = record.parent_of(row)
        if parent is None:
            return ""
        result = worker.restore(parent.id, record=record, only=frozenset({row.id}), started_panes=started_panes,
            account=target_account,
            runs=observed.runs if observed is not None else None,
            codex=observed.codex if observed is not None else herdr.CodexLaunchRead.CURRENT)
    elif row.kind in ("unit", "showrunner") and any(
            child.kind == "worker" and child.parent == staff.ReportsTo(row.id) for child in record.rows
            ):
        result = (worker.restore(row.id, record=record) if observed is None else
                  worker.restore(row.id, record=record, runs=observed.runs, codex=observed.codex))
    else:
        return ""
    if result != 0:
        raise OSError("worker restore is incomplete")
    return started_panes.get(row.id, "")


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
              observed: RestartObservations | None = None,
              opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt = NO_RESTART_MESSAGE,
              resume_checks: DirectorResumeChecks | NoDirectorResumeChecks = NoDirectorResumeChecks.CURRENT,
              success_report: RestartSuccessReport = RestartSuccessReport.START_ROW) -> str:
    with operation_deadline.within(deadline):
        live = (herdr.read_conversation(str(row.current_session)) if observed is None else
                herdr.read_conversation(str(row.current_session), codex=observed.codex))
        if isinstance(live, herdr.LiveConversation):
            if live.pids:
                member_account.repair_home(row, live.kind, live.pids[0])
            if row.kind != "worker" and worker_scope is WorkerRestartScope.FOLLOW_OWNER:
                _ = (_restore_workers(row, record) if observed is None else
                     _restore_workers(row, record, observed=observed))
            print(f"{row.name}: unchanged")
            return ""
        if isinstance(live, herdr.UnknownConversation):
            herdr.conversation_note(live)
        if row.kind == "worker":
            return _restore_workers(row, record, observed=observed, target_account=target_account)
        machine, folder = shutdown_record.row_folder(row, record, resolver=resolver)
        from scripts.agents import remote
        if machine != remote.local_machine():
            raise ValueError(f"{row.name}: session is on {machine}")
        from scripts.agents.launch_names import LaunchNames

        names = LaunchNames(record)
        publication = pane_record.publication_for_session(str(row.current_session), record=record)
        original = publication.entry if publication is not None else None
        projected = names.resume(original) if original is not None else None
        if row.kind == "producer":
            kind, environment, arguments = producer_resume(row, original) if original is not None else producer_launch(row)
        else:
            kind, environment, arguments = (_saved_launch(original, str(row.current_session), row.name, record=record)
                if original is not None else session_launch(str(row.current_session), row.name))
        selected = member_account.resume_account(kind, row, to=target_account)
        if isinstance(selected, accounts.NamedAccount):
            environment = named_account_environment(selected.dir)
        else:
            environment = {"CODEX_HOME": str(selected.home)}
        if projected is not None:
            environment = {**names.environment(projected), **environment}
        settings = conf.read("shutdown")
        start_seconds = settings.number("START_SECONDS")
        reuse_deadline = time.monotonic() + operation_deadline.remaining(start_seconds)
        workspace = herdr.row_workspace(row, record, row.name)
        destination = (client.producer_row_tab(str(row.id), row.name, workspace, record=record) if row.kind == "producer" else
                       client.saved_row_tab(str(row.id), row.name, workspace))
        selected_home = selected.dir if isinstance(selected, accounts.NamedAccount) else selected.home
        expectation = pane_launch.ProducerLaunchRequest(row.id, row.current_session, kind, Path(folder), selected_home)
        if row.kind == "producer":
            environment = expectation.environment(environment)
        prepared = herdr.prepare_session_environment(str(row.current_session), str(folder), environment,
                                                      herdr.NO_ENVIRONMENT_REMOVALS)
        if isinstance(prepared, nix_env.Refused):
            raise OSError(str(prepared))
        target: herdr.NewTab | herdr.ExistingPane = destination
        source_pane: pane_launch.ResumeSourcePane | pane_launch.ResumeSourceAbsent = pane_launch.ResumeSourceAbsent.CURRENT
        exiting_source: RetainedRestartPane | pane_launch.ResumeSourceAbsent = pane_launch.ResumeSourceAbsent.CURRENT
        source_client = client
        try:
            if publication is not None and publication.path != pane_record.record_path(client.session):
                source_client = herdr.Herdr(publication.path.stem)
            entries = ({original["pane_id"]: original} if original is not None else {})
            locations = [dict(pane) for pane in source_client.panes()]
            evidence = pane_launch.NativePaneEvidence(locations, source_client.scan_row_panes(record, locations))
            panes = pane_launch.reference_panes(pane_launch.StaffLaunchReference(row.id), record, source_client,
                                                entries, evidence=evidence)
            if not panes and publication is not None and original is not None:
                retained = next((pane for pane in locations if pane["pane_id"] == original["pane_id"]
                                 and any(unknown.pane == pane["pane_id"] for unknown in evidence.scan.unknown)), None)
                if retained is not None:
                    exiting_source = RetainedRestartPane(row, publication, source_client, str(retained.get("terminal_id", "")))
                    state = exiting_source.state()
                    while state is RetainedPaneState.EXITING:
                        remaining = reuse_deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        time.sleep(operation_deadline.remaining(min(0.1, remaining)))
                        state = exiting_source.state()
                    source_pane = pane_launch.ResumeSourcePane(original["pane_id"])
                    if state is RetainedPaneState.SHELL:
                        panes = [retained]
                        if row.kind == "producer" and source_client.session == client.session:
                            target = client.producer_row_tab(str(row.id), row.name, workspace, record=record)
                    elif state is RetainedPaneState.FOREIGN:
                        source_pane = pane_launch.ResumeSourceAbsent.OTHER_OWNER
            if len(panes) > 1:
                _anomaly(row, "duplicate panes retained")
            elif panes:
                pane = panes[0]
                source_pane = pane_launch.ResumeSourcePane(str(pane["pane_id"]))
                if (row.kind != "producer" and source_client.session == client.session
                        and pane.get("terminal_id")
                        and (pane in evidence.scan.found.get(row.id, ())
                             or isinstance(client.pane_activity(str(pane["pane_id"])), herdr.PaneAtShell))):
                    target = herdr.ExistingPane(str(pane["pane_id"]), str(pane["terminal_id"]))
            elif original is not None and not any(pane["pane_id"] == original["pane_id"] for pane in locations):
                source_pane = pane_launch.ResumeSourcePane(original["pane_id"])
            elif original is not None and any(pane["pane_id"] == original["pane_id"]
                    for owner_id, matches in evidence.scan.found.items() if owner_id != row.id for pane in matches):
                source_pane = pane_launch.ResumeSourceAbsent.OTHER_OWNER
        except (OSError, ValueError, herdr_types.HerdrFailure) as error:
            _anomaly(row, error)
        # Read liveness again immediately before the launch.
        current = (herdr.read_conversation(str(row.current_session), kind) if observed is None else
                   herdr.read_conversation(str(row.current_session), kind, codex=observed.codex))
        if isinstance(current, herdr.LiveConversation):
            if current.pids:
                member_account.repair_home(row, current.kind, current.pids[0])
            if worker_scope is WorkerRestartScope.FOLLOW_OWNER:
                _ = (_restore_workers(row, record) if observed is None else
                     _restore_workers(row, record, observed=observed))
            print(f"{row.name}: unchanged")
            return ""
        if isinstance(current, herdr.UnknownConversation) and not isinstance(live, herdr.UnknownConversation):
            herdr.conversation_note(current)
        operation_deadline.check()
        check: DirectorResumeCheck | NoDirectorResumeChecks = NoDirectorResumeChecks.CURRENT
        if isinstance(opening, herdr.OpeningPrompt):
            transcript = herdr.SessionTranscript(selected_home, str(row.current_session))
            opening = replace(opening, transcript=transcript)
            since = datetime.now(timezone.utc)
            if kind == "claude":
                opening = opening.prepare_for_launch(since)
            if row.kind in ("unit", "showrunner") and isinstance(resume_checks, DirectorResumeChecks):
                reader = (cast(herdr.TranscriptCursor, opening.cursor) if kind == "claude" else
                          CodexResumeTranscript(transcript, since))
                if isinstance(reader, CodexResumeTranscript):
                    reader.skip_existing_turns()
                check = DirectorResumeCheck(row.name, opening.text, reader)
                if isinstance(reader, herdr.TranscriptCursor):
                    cursor = reader
                    before_prompt = opening.before_prompt

                    def native_transcript(actual: herdr.SessionTranscript) -> object:
                        cursor.bind_transcript(actual)
                        return before_prompt(actual)

                    opening = replace(opening, before_prompt=native_transcript)
        scope = (pane_launch.producer_launch_request(expectation)
                 if row.kind == "producer" else nullcontext())
        with scope:
            result = herdr.launch_prepared_agent(client.session, target, prepared, kind,
                trust_arguments(kind, str(folder), arguments),
                opening if kind == "claude" else NO_RESTART_MESSAGE,
                int(operation_deadline.remaining(start_seconds) * 1000), agent=row.name,
                reported_unknown=(current.reason if isinstance(current, herdr.UnknownConversation) else
                                  live.reason if isinstance(live, herdr.UnknownConversation) else ""),
                codex=observed.codex if observed is not None else herdr.CodexLaunchRead.CURRENT, client=client)
        if isinstance(result.outcome, herdr.ClaudeTrustNotWritten):
            raise ClaudeTrustWriteFailed(result.outcome.reason)
        if isinstance(result.outcome, herdr.PromptNotSubmitted):
            if isinstance(target, herdr.ExistingPane) and not target.terminal:
                return result.pane
            raise OSError(result.outcome.reason)
        if kind == "codex" and isinstance(opening, herdr.OpeningPrompt):
            try:
                client.prompt(result.pane, opening.text, target=str(row.current_session))
            except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
                _anomaly(row, f"continuation message was not sent: {error}")
        if row.kind == "producer":
            home = selected.dir if isinstance(selected, accounts.NamedAccount) else selected.home
            try:
                _ = staff.set_home(row.id, staff.SessionHome(home.absolute()))
            except (staff.StaffUnreadable, staff.StaffNotFound) as error:
                _anomaly(row, error)
        path = pane_record.record_path(client.session)
        if (publication is not None and original is not None and projected is not None
                and (publication.path != path or result.pane != original["pane_id"])):
            try:
                if isinstance(exiting_source, RetainedRestartPane):
                    completed = expectation.expected_launch(projected) if row.kind == "producer" else projected
                    cleanup = ReplacedRestartPane(exiting_source, client, path,
                                                  pane_launch.resume_attempt(path, result.pane),
                                                  names.resume_proof(original, completed))
                    if not cleanup.finish():
                        if isinstance(resume_checks, DirectorResumeChecks):
                            resume_checks.replaced_panes.append(cleanup)
                        else:
                            DirectorResumeChecks(replaced_panes=[cleanup]).report()
                elif (isinstance(source_pane, pane_launch.ResumeSourceAbsent)
                        or not pane_holds_other_work(source_client, source_pane.pane, str(row.current_session))):
                    completed = expectation.expected_launch(projected) if row.kind == "producer" else projected
                    pane_launch.finish_restore(publication.path, original,
                                               pane_launch.resume_attempt(path, result.pane),
                                               names.resume_proof(original, completed), close=client.close_tab_confirmed,
                                               destination=path, close_source=source_client.close_tab_confirmed,
                                               source_pane=source_pane)
            except (OSError, ValueError, herdr_types.HerdrFailure) as error:
                _anomaly(row, error)
        try:
            client.remember_row_tab(str(row.id), result.pane)
            if present:
                client.focus_tab_of(result.pane)
                client.close_startup_workspaces(result.pane.split(":", 1)[0])
        except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
            _anomaly(row, error)
        if worker_scope is WorkerRestartScope.FOLLOW_OWNER:
            _ = (_restore_workers(row, record) if observed is None else
                 _restore_workers(row, record, observed=observed))
        if isinstance(resume_checks, DirectorResumeChecks) and isinstance(check, DirectorResumeCheck):
            resume_checks.checks.append(check)
        if success_report is RestartSuccessReport.START_ROW:
            print(f"{row.name}: restarted")
        return result.pane


def row_panes(rows: list[staff.StaffRow], client: herdr.Herdr) -> list[tuple[staff.StaffRow, str]]:
    try:
        entries = pane_record.read(client.session)
        record = staff.initial_snapshot()
        record = staff.StaffSnapshot((*record.rows, *(row for row in rows if record.by_id(row.id) is None)))
        with herdr.pane_evidence_run():
            panes = [dict(pane) for pane in client.panes()]
            evidence = pane_launch.NativePaneEvidence(panes, client.scan_row_panes(record, panes))
            found: list[tuple[staff.StaffRow, str]] = []
            for row in rows:
                reference = pane_launch.StaffLaunchReference(row.id)
                newest = max((entry for entry in entries.values() if pane_launch.launch_reference(entry, record) == reference),
                             key=lambda entry: entry["written_at"], default=None)
                retained = {newest["pane_id"]: newest} if newest is not None else {}
                found.extend((row, str(pane["pane_id"])) for pane in pane_launch.reference_panes(
                    reference, record, client, retained, evidence=evidence))
        return found
    except (OSError, ValueError, herdr_types.HerdrFailure) as error:
        print(f"restart: layout anomaly: {str(error).splitlines()[0]}", file=sys.stderr)
        return []


@dataclass(frozen=True)
class CompleteLiveRows:
    rows: list[staff.StaffRow]


@dataclass(frozen=True)
class PartialLiveRows:
    rows: list[staff.StaffRow]


def _read_live_members(rows: Iterable[staff.StaffRow], record: staff.StaffSnapshot, *,
                 resolver: session_staff.RowResolver | None = None,
                 codex: herdr.CodexConversations | None = None,
                 account: AllRestartAccounts | accounts.AccountSelection = ALL_RESTART_ACCOUNTS
                 ) -> CompleteLiveRows | PartialLiveRows:
    """Read only the supplied rows and return their live local conversations."""
    from scripts.agents import remote

    selected: list[staff.StaffRow] = []
    complete = True
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
                complete = False
                if observed.shared_codex:
                    shared_unknown += 1
                if not observed.shared_codex or os.environ.get("SHUTDOWN_DETAIL") == "1":
                    herdr.conversation_note(observed)
        except (OSError, ValueError) as error:
            complete = False
            _anomaly(row, error)
    if shared_unknown:
        print(f"anomaly: {shared_unknown} rows not proved live or stopped (shared cause above); not blocking", file=sys.stderr)
    return CompleteLiveRows(selected) if complete else PartialLiveRows(selected)


def live_members(rows: Iterable[staff.StaffRow], record: staff.StaffSnapshot, *,
                 resolver: session_staff.RowResolver | None = None,
                 codex: herdr.CodexConversations | None = None,
                 account: AllRestartAccounts | accounts.AccountSelection = ALL_RESTART_ACCOUNTS) -> list[staff.StaffRow]:
    """Return proved live members for callers that do not replace restart records."""
    return _read_live_members(rows, record, resolver=resolver, codex=codex, account=account).rows


def _codex_conversations(client: herdr.Herdr) -> PaneConversations:
    """Read panes once for native Codex identities and report names."""
    live: dict[str, tuple[int, ...]] = {}
    names: dict[str, str] = {}
    resolved_tabs: set[str] = set()
    try:
        panes = client.listed("pane")
    except (OSError, ValueError, herdr_types.HerdrFailure):
        return PaneConversations(herdr.codex_conversations(), names)
    try:
        tabs = {herdr.json_text(tab.get("tab_id"), "tab id"):
                herdr.json_text(tab.get("label") or "", "tab label") for tab in client.listed("tab")}
    except (OSError, ValueError, herdr_types.HerdrFailure):
        tabs = {}
    for pane in panes:
        try:
            pane_id = herdr.json_text(pane.get("pane_id"), "pane id")
            tab_name = tabs.get(str(pane.get("tab_id", "")), "")
            label = herdr.json_text(pane.get("label") or "", "pane label")
            display = f"{tab_name} {label} ({pane_id})".strip()
            if identity := herdr.pane_session(pane):
                names[identity] = display
            if pane.get("agent") != "codex":
                continue
            info = herdr.json_object(client.result("pane", "process-info", "--pane", pane_id).get("process_info"),
                                     "native pane processes")
            identity = herdr.pane_native_process(info, "codex", pane).current_identity
            live[identity] = ()
            names[identity] = display
            resolved_tabs.add(f"{tab_name} (Codex thread unknown)")
        except (OSError, ValueError, herdr_types.HerdrFailure):
            continue
    observed = herdr.codex_conversations()
    return PaneConversations(herdr.CodexConversations(
        {**live, **observed.live}, tuple(reason for reason in observed.unknown if reason not in resolved_tabs)), names)


@herdr.observation_snapshot()
def live_rows(record: staff.StaffSnapshot, client: herdr.Herdr, *, save_tabs: bool = False,
              resolver: session_staff.RowResolver | None = None,
              account: AllRestartAccounts | accounts.AccountSelection = ALL_RESTART_ACCOUNTS,
              observed: RestartObservations | None = None,
              selection: SelectedRestartRows | AllRestartRows = AllRestartRows.CURRENT
              ) -> CompleteLiveRows | PartialLiveRows:
    from scripts.herdr.worker_panes import UncertainWorker, ProcessUnknownWorker

    observed = _observations(record, client) if observed is None else observed
    resolver = session_staff.RowResolver(record) if resolver is None else resolver
    codex = observed.codex
    rows = [row for row in record.rows if not isinstance(selection, SelectedRestartRows) or row.id in selection.ids]
    complete = True
    if isinstance(selection, AllRestartRows):
        # The registry supplies candidate names; the reader proves each conversation.
        try:
            candidates = {session["sessionId"]: (session["name"] or session["sessionId"]) +
                (f" in {folder}" if (folder := session.get("cwd")) else "") for session in sessions.running_sessions()}
        except (OSError, ValueError) as error:
            complete = False
            print(f"restart: Claude registry anomaly: {error}", file=sys.stderr)
            candidates = {}
        for identity in herdr.running_codex_threads(thread_host=herdr.codex_managed_daemon, codex=codex):
            _ = candidates.setdefault(identity, identity)
        owned = {row.current_session for row in record.rows}
        for identity in sorted(candidates.keys() - owned):
            if isinstance(herdr.read_conversation(identity, codex=codex), herdr.LiveConversation):
                print(f"{' '.join(observed.pane_names.get(identity, candidates[identity]).splitlines())}: "
                      + "restart anomaly: unassigned live session", file=sys.stderr)
        owners = [row for row in record.rows if row.id in observed.pane_owners]
        observations = worker_panes.observe_parents(owners, record) if owners else {}
    else:
        parents = {parent.id for row in rows if row.kind == "worker"
                   and (parent := record.parent_of(row)) is not None}
        owners = [row for row in record.rows if row.id in parents]
        observations = worker_panes.observe_parents(
            owners, record, selection=worker_panes.SelectedWorkerRows(selection.ids)) if owners else {}
    workers = {state.row.id: state for states in observations.values() for state in states}
    active_workers = [state.row for state in workers.values()
                      if not isinstance(state, (worker_panes.ClosedWorker, worker_panes.StoppedWorker))]
    candidates_rows = [row for row in rows if row.id not in workers]
    if not isinstance(account, AllRestartAccounts):
        candidates_rows.extend(active_workers)
    reading = _read_live_members(candidates_rows, record, resolver=resolver, codex=codex, account=account)
    complete = complete and isinstance(reading, CompleteLiveRows) and not any(
        isinstance(state, (UncertainWorker, ProcessUnknownWorker))
        for state in workers.values())
    selected = reading.rows
    if isinstance(account, AllRestartAccounts):
        selected.extend(active_workers)
    positions = {row.id: index for index, row in enumerate(record.rows)}
    selected.sort(key=lambda row: positions[row.id])
    if save_tabs:
        try:
            client.save_layout()
        except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
            print(f"restart: layout anomaly: {str(error).splitlines()[0]}", file=sys.stderr)
        for row, pane in row_panes(selected, client):
            try:
                client.remember_row_tab(str(row.id), pane)
            except (OSError, ValueError, herdr_types.HerdrFailure, subprocess.SubprocessError) as error:
                _anomaly(row, error)
    return CompleteLiveRows(selected) if complete else PartialLiveRows(selected)


def prune_live_rows(reading: CompleteLiveRows | PartialLiveRows) -> None:
    """Prune recorded live rows after a complete read and a locked liveness check."""
    if isinstance(reading, PartialLiveRows):
        print("restart: partial liveness read; existing restart rows retained", file=sys.stderr)
        return
    saved = set(shutdown_record.read())
    for row in reading.rows:
        if str(row.id) not in saved:
            continue
        try:
            with staff.row_operation(str(row.id)):
                current = staff.initial_snapshot().by_id(row.id)
                if current is None:
                    continue
                observed = herdr.read_conversation(str(current.current_session))
                if isinstance(observed, herdr.LiveConversation):
                    shutdown_record.remove_started([str(row.id)])
                elif isinstance(observed, herdr.UnknownConversation):
                    _anomaly(row, observed.reason)
        except (OSError, ValueError, staff.StaffRefused) as error:
            _anomaly(row, error)


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
            place = herdr.herdr_place_of(pid)
            if isinstance(place, herdr.HerdrPlace):
                herdr.forget_pane_evidence(place.session, place.pane)
            process = herdr.native_process(pid, kind)
            if process.current_identity != identity:
                raise ValueError(f"pid {pid}: current conversation differs; original generation is unknown")
            state = sessions.process_liveness(pid, process.started)
            if isinstance(state, sessions.LiveSessionProcess):
                live.append(pid)
            elif isinstance(state, sessions.SessionLivenessUnknown):
                raise OSError(state.reason)
            else:
                place = herdr.herdr_place_of(pid)
                if isinstance(place, herdr.HerdrPlace):
                    herdr.forget_pane_evidence(place.session, place.pane)
                current = herdr.native_process(pid, kind)
                if current.started != process.started and current.current_identity != identity:
                    continue
                raise ValueError(f"pid {pid}: generation changed without proof that the target exited")
        except (OSError, ValueError, herdr_types.HerdrFailure) as error:
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


def stop_all(*, dry_run: bool = False, only: tuple[str, ...] = (),
             account: AllRestartAccounts | accounts.AccountSelection = ALL_RESTART_ACCOUNTS) -> int:
    return restart(stop_all=True, dry_run=dry_run, only=only, account=account, after_stop=AfterStop.KEEP_STOPPED)


@herdr.pane_evidence_run()
def restart(*, stop_all: bool = False, dry_run: bool = False, only: tuple[str, ...] = (),
            account: AllRestartAccounts | accounts.AccountSelection = ALL_RESTART_ACCOUNTS,
            after_stop: AfterStop = AfterStop.RESTART,
            why: str = DEFAULT_RESTART_CAUSE) -> int:
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
        reading: CompleteLiveRows | PartialLiveRows = CompleteLiveRows([])
        if stop_all:
            selection = (SelectedRestartRows(frozenset(row.id for reference in only
                if (row := record.by_member_reference(reference)) is not None)) if only else AllRestartRows.CURRENT)
            reading = live_rows(record, client, save_tabs=not dry_run, resolver=resolver, account=account,
                                observed=observed, selection=selection)
            selected = reading.rows
        select_only = bool(only) and not stop_all
        pending_ids = ([] if after_stop == AfterStop.KEEP_STOPPED else
                       list(only) if select_only else
                       [] if stop_all and (only or not isinstance(account, AllRestartAccounts)) else shutdown_record.read())
        if dry_run and not select_only and not stop_all and isinstance(account, AllRestartAccounts):
            path = shutdown_record.record_path()
            saved_at = time.strftime("%H:%M", time.localtime(path.stat().st_mtime)) if path.exists() else "unknown"
            print(f"restart record: {len(pending_ids)} rows saved at {saved_at}")
            for row in live_rows(record, client, resolver=resolver, observed=observed).rows:
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
            positions = {tab["staff_id"]: f"workspace {space['name']}, tab {index} ({record.rows_by_id[staff.StaffId(tab['staff_id'])].name})"
                         for space in herdr.saved_layout(client.session)["workspaces"]
                         for index, tab in enumerate(space["tabs"], 1) if staff.StaffId(tab["staff_id"]) in record.rows_by_id}
            visible = row_panes(list(record.rows) if stop_all and not only and isinstance(account, AllRestartAccounts) else selected, client)
            for row, pane in visible:
                if row not in selected:
                    continue
                try:
                    workspace, index, position = client.pane_tab(pane)
                    del position
                    positions[str(row.id)] = f"workspace {workspace}, tab {index} ({row.name})"
                except (OSError, ValueError, herdr_types.HerdrFailure) as error:
                    _anomaly(row, error)
            for row in selected:
                action = "; would stop and save" if after_stop == AfterStop.KEEP_STOPPED else ""
                print(f"{row.name}: {row.id}, {row.kind}, {positions.get(str(row.id), 'no tab')}{action}")
            for row in {row.id: row for row, _pane in visible if row not in selected}.values():
                print(f"{row.name}: left alone; no proved local restart target")
            return 0
    if stop_all and after_stop is AfterStop.KEEP_STOPPED and not only and isinstance(account, AllRestartAccounts):
        prune_live_rows(reading)
    errors = (OSError, ValueError, staff.StaffRefused, herdr_types.HerdrFailure, subprocess.SubprocessError)
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
                  and parent is not None and parent.id in selected_ids else row)
        holders[holder.id] = holder
        members_by_holder.setdefault(holder.id, []).append(str(row.id))
    callers = {os.environ.get("CLAUDE_CODE_SESSION_ID"), os.environ.get("CODEX_THREAD_ID")} - {None, ""}
    if stop_all and any(row.current_session in callers for row in selected):
        arguments = ["stop" if after_stop is AfterStop.KEEP_STOPPED else "restart",
                     "--all", "--only", ",".join(str(row.id) for row in selected)]
        if after_stop is AfterStop.RESTART:
            arguments.extend(("--why", why))
        if not isinstance(account, AllRestartAccounts):
            if after_stop is AfterStop.KEEP_STOPPED:
                arguments.append("--account")
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
            with ExitStack() as locks:
                _ = locks.enter_context(staff.row_operation(str(holder.id)))
                for member in members_by_holder[holder.id]:
                    if member != str(holder.id):
                        _ = locks.enter_context(staff.row_operation(member))
                shutdown_record.update(started=[], pending=members_by_holder[holder.id])
                recorded.set()
                workers: tuple[staff.StaffId, ...] = ()
                if worker_scope is WorkerRestartScope.FOLLOW_OWNER and holder.kind in ("unit", "showrunner"):
                    unresolved = stop_resume.stop_unit_workers(holder.id, record, codex=observed.codex)
                    if unresolved and holder.id in observed.pane_owners:
                        workers = tuple(child.id for child in record.find(kind="worker", parent=holder.id)
                                        if child.id in selected_ids)
                stopped = stop_row(holder, client, codex=observed.codex)
                if not stopped:
                    shutdown_record.remove_started([str(holder.id)])
                return stopped, workers
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
    observed = replace(observed, codex=herdr.CodexConversations(
        {identity: pids for identity, pids in observed.codex.live.items() if identity not in selected_sessions},
        observed.codex.unknown))
    present = False
    if holders:
        try:
            server = herdr.restore_server(client.session, int(herdr.PROGRAM_TIMEOUT_SECONDS * 1000),
                                     session_kind=herdr.security_session())
            present = isinstance(server, herdr.WindowServerCreated)
        except errors as error:
            print(f"restart: server anomaly: {str(error).splitlines()[0]}", file=sys.stderr)
    resume_checks = DirectorResumeChecks()

    def start_holder(holder: staff.StaffRow, focus: bool,
                     success_report: RestartSuccessReport = RestartSuccessReport.START_ROW) -> RestartDisposition:
        with ExitStack() as locks:
            _ = locks.enter_context(staff.row_operation(str(holder.id)))
            for member in members_by_holder[holder.id]:
                if member != str(holder.id):
                    _ = locks.enter_context(staff.row_operation(member))
            shutdown_record.update(started=[], pending=members_by_holder[holder.id])
            current_record = staff.initial_snapshot()
            current_record = replace(current_record, rows=tuple(row for row in current_record.rows if row.id not in blocked_workers))
            current = current_record.by_id(holder.id)
            if current is None:
                raise ValueError("staff row no longer exists")
            if current.kind in ("unit", "showrunner") and (
                    isinstance(current.paused, staff.Paused) or isinstance(current.exited, staff.Exited)):
                print(f"{current.name}: {'exited' if isinstance(current.exited, staff.Exited) else 'paused'}")
                return RestartDisposition.HELD
            if holder.id in unconfirmed:
                fresh = _after_unconfirmed_stop(current, pre_stop_codex)
                if isinstance(fresh, herdr.UnknownConversation):
                    raise OSError(fresh.reason)
                if isinstance(fresh, herdr.LiveConversation):
                    if worker_scope is WorkerRestartScope.FOLLOW_OWNER:
                        _ = _restore_workers(current, current_record, observed=observed)
                    print(f"{current.name}: unchanged")
                    shutdown_record.remove_started([member for member in members_by_holder[holder.id]
                                                    if staff.StaffId(member) not in blocked_workers])
                    return RestartDisposition.RESTORED
            facts = session_staff.RowResolver(current_record)
            opening = (herdr.OpeningPrompt(resume_message.resume_message(why, workers=resume_message.worker_mode()))
                       if current.kind in ("unit", "showrunner") else NO_RESTART_MESSAGE)
            launch_row = (start_row if success_report is RestartSuccessReport.START_ROW else
                          partial(start_row, success_report=success_report))
            if worker_scope is WorkerRestartScope.FOLLOW_OWNER:
                pane = launch_row(current, current_record, client, present=focus, resolver=facts, observed=observed,
                                  opening=opening, resume_checks=resume_checks)
            else:
                pane = launch_row(current, current_record, client, present=focus, resolver=facts,
                                  target_account="" if isinstance(account, AllRestartAccounts) else account,
                                  worker_scope=worker_scope, observed=observed, opening=opening, resume_checks=resume_checks)
            shutdown_record.remove_started([member for member in members_by_holder[holder.id]
                                            if staff.StaffId(member) not in blocked_workers])
            return RestartDisposition.STARTED if pane else RestartDisposition.RESTORED

    def record_start(holder: staff.StaffRow, disposition: RestartDisposition) -> None:
        members = members_by_holder[holder.id]
        if disposition is RestartDisposition.HELD:
            return
        started.extend(member for member in members if staff.StaffId(member) not in blocked_workers)

    failed_starts: list[FailedHolderStart] = []
    if holders:
        with ThreadPoolExecutor(max_workers=min(16, len(holders))) as pool:
            futures = [(holder, present and index == 0,
                        pool.submit(copy_context().run, start_holder, holder, present and index == 0))
                       for index, holder in enumerate(holders.values())]
            for holder, focus, future in futures:
                try:
                    record_start(holder, future.result())
                except ClaudeTrustWriteFailed:
                    exit_code = 1
                except staff.StaffRefused as error:
                    _anomaly(holder, error)
                except errors as error:
                    failed_starts.append(FailedHolderStart(holder, focus, error))
    for failed in failed_starts:
        holder = failed.holder
        fresh: herdr.LiveConversation | herdr.StoppedConversation | herdr.UnknownConversation
        fresh = herdr.UnknownConversation(str(failed.error))
        if holder.kind in ("unit", "showrunner"):
            try:
                fresh = herdr.read_conversation(str(holder.current_session))
            except errors:
                pass
        if not isinstance(fresh, herdr.StoppedConversation):
            _anomaly(holder, failed.error)
            continue
        try:
            disposition = start_holder(holder, failed.focus, RestartSuccessReport.CALLER)
            record_start(holder, disposition)
            if disposition is RestartDisposition.STARTED:
                print(f"{holder.name}: down; started")
        except ClaudeTrustWriteFailed:
            exit_code = 1
        except staff.StaffRefused as error:
            _anomaly(holder, error)
        except errors as error:
            print(f"{holder.name}: down: {_error_reason(error)}")
    if started:
        try:
            _ = client.sort_tabs()
        except errors as error:
            print(f"restart: layout anomaly: {str(error).splitlines()[0]}", file=sys.stderr)
    resume_checks.report()
    return exit_code


def main() -> int:
    """Run detached restart work and save its output."""
    command = cast(AgentsCommand, cast(object, import_module("scripts.agents.shutdown")))

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
