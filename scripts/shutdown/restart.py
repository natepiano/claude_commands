"""Stop live staff rows, then resume their current sessions in their saved tabs."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS.parent))
for dependency in ("herdr", "message", "lib", "shutdown"):
    sys.path.insert(0, str(SCRIPTS / dependency))

import herdr
import sessions
from scripts.production import staff, unit_move
from scripts.lib import nix_env
from scripts.shutdown import shutdown_record, stop_resume, pane_record
from scripts.herdr import pane_launch
from scripts.whoami.account import named_account_environment


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



def row_folder(row: staff.StaffRow, record: staff.StaffSnapshot) -> tuple[str, Path]:
    member = row
    while True:
        if isinstance(member.worktree, (staff.PresentWorktree, staff.RemovedWorktree)):
            return member.worktree.machine, member.worktree.path
        if isinstance(member.doc, staff.LiveDoc):
            return member.doc.machine, member.doc.path.parent
        parent = record.by_id(member.parent) if member.parent is not None else None
        if parent is None:
            raise ValueError(f"{row.name}: no worktree in staff")
        member = parent


def _anomaly(row: staff.StaffRow, error: object) -> None:
    reason = str(error).splitlines()
    print(f"{row.name}: restart anomaly: {reason[0] if reason else type(error).__name__}", file=sys.stderr)


def stop_row(row: staff.StaffRow, client: herdr.Herdr) -> bool:
    panes = [pane for pane in client.panes() if pane["session_id"] == row.current_session]
    if panes and (len(panes) != 1 or client.native_session(
            panes[0]["pane_id"], "claude", herdr.PROGRAM_TIMEOUT_SECONDS) != row.current_session):
        _anomaly(row, "pane runs another conversation; no signal sent")
        return False
    result = stop_resume.stop(str(row.current_session))
    if isinstance(result, (stop_resume.Stopped, stop_resume.NotRunning)):
        return True
    _anomaly(row, result.reason if isinstance(result, stop_resume.StopFailed) else "session not found")
    return False


def session_launch(identity: str) -> tuple[Literal["claude", "codex"], dict[str, str], list[str]]:
    try:
        entry = pane_record.launch_for_session(identity)
    except (OSError, ValueError) as error:
        print(f"{identity}: launch record anomaly: {str(error).splitlines()[0]}", file=sys.stderr)
        entry = None
    if entry is not None:
        kind = pane_launch.agent_kind(entry)
        if kind == "codex":
            home = entry["environment"].get("CODEX_HOME", entry["config_dir"])
            return kind, {"CODEX_HOME": home}, ["resume", identity]
        return kind, named_account_environment(Path(entry["config_dir"])), ["--resume", identity]
    for directory in unit_move.config_dirs():
        if any((directory / "projects").glob(f"*/{identity}.jsonl")):
            return "claude", named_account_environment(directory), ["--resume", identity]
    homes = list(Path.home().glob(".codex*"))
    if home := os.environ.get("CODEX_HOME"):
        homes.insert(0, Path(home))
    for directory in homes:
        if any((directory / "sessions").glob(f"**/rollout-*{identity}.jsonl")):
            return "codex", {"CODEX_HOME": str(directory)}, ["resume", identity]
    raise OSError(f"no transcript for {identity}")


def start_row(row: staff.StaffRow, record: staff.StaffSnapshot, client: herdr.Herdr, *, present: bool = False) -> None:
    live = sessions.session_liveness(str(row.current_session))
    if isinstance(live, sessions.LiveSessionProcess):
        print(f"{row.name}: unchanged")
        return
    if isinstance(live, sessions.SessionLivenessUnknown):
        raise OSError(live.reason)
    machine, folder = row_folder(row, record)
    import remote
    if machine != remote.local_machine():
        raise ValueError(f"{row.name}: session is on {machine}")
    kind, environment, arguments = session_launch(str(row.current_session))
    if kind == "codex" and row.current_session in herdr.running_codex_threads(thread_host=lambda _arguments: False):
        print(f"{row.name}: unchanged")
        return
    destination = client.saved_row_tab(str(row.id), row.name)
    prepared = herdr.prepare_session_environment(str(row.current_session), str(folder), environment,
                                                  herdr.NO_ENVIRONMENT_REMOVALS)
    if isinstance(prepared, nix_env.Refused):
        raise OSError(str(prepared))
    panes = [pane for pane in client.panes() if pane["session_id"] == row.current_session]
    target: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane = destination
    if len(panes) == 1:
        pane = panes[0]
        if not isinstance(client.pane_activity(pane["pane_id"]), herdr.PaneAtShell):
            raise OSError("pane has other work; no keystroke sent")
        target = herdr.ExistingPane(pane["pane_id"], pane.get("terminal_id", ""))
    # Read liveness again immediately before the launch.
    live = sessions.session_liveness(str(row.current_session))
    if not isinstance(live, sessions.SessionNotLive):
        if isinstance(live, sessions.LiveSessionProcess):
            print(f"{row.name}: unchanged")
            return
        raise OSError(live.reason)
    if kind == "codex" and row.current_session in herdr.running_codex_threads(thread_host=lambda _arguments: False):
        print(f"{row.name}: unchanged")
        return
    if kind == "claude":
        arguments.extend(["--name", row.name])
    result = herdr.launch_prepared_agent(client.session, target, prepared, kind,
        arguments, herdr.NoOpeningPrompt(),
        int(herdr.PROGRAM_TIMEOUT_SECONDS * 1000), agent=row.name)
    try:
        client.remember_row_tab(str(row.id), result.pane)
        if present:
            client.focus_tab_of(result.pane)
            client.close_startup_workspaces(result.pane.split(":", 1)[0])
    except (OSError, ValueError, herdr.HerdrFailure, subprocess.SubprocessError) as error:
        _anomaly(row, error)
    print(f"{row.name}: restarted")


def restart(*, stop_all: bool = False, dry_run: bool = False, only: tuple[str, ...] = ()) -> int:
    import remote

    record = staff.initial_snapshot()
    client = herdr.Herdr(herdr.shared_session())
    selected: list[staff.StaffRow] = []
    if stop_all:
        try:
            codex_live = herdr.running_codex_threads(thread_host=lambda _arguments: False)
        except (OSError, ValueError, herdr.HerdrFailure) as error:
            print(f"restart: Codex liveness anomaly: {error}", file=sys.stderr)
            codex_live = set[str]()
        for row in record.rows:
            try:
                machine, _folder = row_folder(row, record)
                if machine == remote.local_machine() and (isinstance(
                        sessions.session_liveness(str(row.current_session)), sessions.LiveSessionProcess)
                        or row.current_session in codex_live):
                    selected.append(row)
            except (OSError, ValueError) as error:
                _anomaly(row, error)
    else:
        for reference in only or tuple(shutdown_record.read()):
            row = record.by_member_reference(reference)
            if row is None:
                matches = [item for item in record.rows if item.name == reference]
                row = matches[0] if len(matches) == 1 else None
            if row is None:
                print(f"{reference}: unknown or ambiguous staff row", file=sys.stderr)
                continue
            selected.append(row)
    if dry_run:
        positions = {tab.get("row_id"): f"workspace {space['name']}, tab {index} ({tab['name']})"
                     for space in herdr.saved_layout(client.session)["workspaces"]
                     for index, tab in enumerate(space["tabs"], 1) if tab.get("row_id")}
        for row in selected:
            print(f"{row.name}: {row.id}, {row.kind}, {positions.get(str(row.id), 'no saved tab')}")
        return 0
    errors = (OSError, ValueError, herdr.HerdrFailure, subprocess.SubprocessError)
    pending: list[str] = []
    started: list[str] = []
    with ExitStack() as locks:
        locked: list[staff.StaffRow] = []
        for row in selected:
            try:
                _ = locks.enter_context(staff.row_operation(str(row.id)))
                current = staff.initial_snapshot().by_id(row.id)
                if current is not None:
                    locked.append(current)
            except (OSError, staff.StaffRefused) as error:
                _anomaly(row, error)
        if stop_all:
            try:
                client.save_layout()
                panes = client.panes()
                for row in locked:
                    for pane in panes:
                        if pane["session_id"] == row.current_session:
                            client.remember_row_tab(str(row.id), pane["pane_id"])
            except errors as error:
                print(f"restart: layout anomaly: {str(error).splitlines()[0]}", file=sys.stderr)
            for row in locked:
                try:
                    _ = stop_row(row, client)
                except errors as error:
                    _anomaly(row, error)
        present = False
        if locked:
            try:
                server = herdr.restore_server(client.session, int(herdr.PROGRAM_TIMEOUT_SECONDS * 1000),
                                         session_kind=herdr.security_session())
                present = isinstance(server, herdr.WindowServerCreated)
            except errors as error:
                print(f"restart: server anomaly: {str(error).splitlines()[0]}", file=sys.stderr)
        for row in locked:
            try:
                record = staff.initial_snapshot()
                current = record.by_id(row.id)
                if current is None:
                    raise ValueError("staff row no longer exists")
                start_row(current, record, client, present=present)
                started.append(str(row.id))
                present = False
            except errors as error:
                pending.append(str(row.id))
                _anomaly(row, error)
        if started or pending:
            shutdown_record.update(started=started, pending=pending)
    return 0
