"""Stop live staff rows, then resume their current sessions in their saved tabs."""
from __future__ import annotations

import math
import os
import subprocess
import sys
import time
from collections.abc import Iterable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS.parent))
for dependency in ("herdr", "message", "lib", "shutdown"):
    sys.path.insert(0, str(SCRIPTS / dependency))

from scripts.herdr import herdr
from scripts.message import sessions
from scripts.production import staff, session_staff
from scripts.lib import nix_env, operation_deadline
from scripts.shutdown import shutdown_record, stop_resume, pane_record
from scripts.shutdown.codex_trust import trust_arguments
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
        result = stop_resume.stop(str(row.current_session))
        if isinstance(result, (stop_resume.Stopped, stop_resume.NotRunning)):
            return True
        _anomaly(row, result.reason if isinstance(result, stop_resume.StopFailed) else "session not found")
        return False


def session_launch(identity: str, name: str) -> tuple[Literal["claude", "codex"], dict[str, str], list[str]]:
    try:
        entry = pane_record.launch_for_session(identity)
    except (OSError, ValueError) as error:
        print(f"{identity}: launch record anomaly: {str(error).splitlines()[0]}", file=sys.stderr)
        entry = None
    if entry is not None:
        projected = entry.copy()
        projected["session_id"] = identity
        projected["name"] = name
        arguments = pane_record.resume_arguments(projected)
        kind = pane_launch.agent_kind(entry)
        if kind == "codex":
            home = entry["environment"].get("CODEX_HOME", entry["config_dir"])
            return kind, {"CODEX_HOME": home}, arguments
        return kind, named_account_environment(Path(entry["config_dir"])), arguments
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


def start_row(row: staff.StaffRow, record: staff.StaffSnapshot, client: herdr.Herdr, *,
              present: bool = False, target_account: str = "", deadline: float = math.inf,
              resolver: session_staff.RowResolver | None = None) -> str:
    with operation_deadline.within(deadline):
        live = herdr.read_conversation(str(row.current_session))
        if isinstance(live, herdr.LiveConversation):
            print(f"{row.name}: unchanged")
            return ""
        if isinstance(live, herdr.UnknownConversation):
            herdr.conversation_note(live)
        machine, folder = shutdown_record.row_folder(row, record, resolver=resolver)
        from scripts.shutdown import remote
        if machine != remote.local_machine():
            raise ValueError(f"{row.name}: session is on {machine}")
        kind, environment, arguments = session_launch(str(row.current_session), row.name)
        if kind == "claude" and not target_account and row.kind in ("unit", "showrunner"):
            from scripts.accounts import accounts

            target_account = accounts.role_account(row.kind).label
        if target_account and kind == "claude":
            from scripts.accounts import accounts

            target_dir = accounts.account_dir(target_account)
            _ = stop_resume.heal_account_trust(folder, target_dir,
                Path(environment.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"), row.name)
            environment = named_account_environment(target_dir)
        destination = client.saved_row_tab(str(row.id), row.name)
        prepared = herdr.prepare_session_environment(str(row.current_session), str(folder), environment,
                                                      herdr.NO_ENVIRONMENT_REMOVALS)
        if isinstance(prepared, nix_env.Refused):
            raise OSError(str(prepared))
        target: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane = destination
        try:
            panes = [pane for pane in client.panes() if pane["session_id"] == row.current_session]
            if len(panes) > 1:
                _anomaly(row, "duplicate panes retained")
            elif panes and not panes[0].get("terminal_id"):
                _anomaly(row, "pane has no terminal id; pane retained")
            elif panes:
                target = herdr.ExistingPane(panes[0]["pane_id"], panes[0].get("terminal_id", ""))
        except (OSError, ValueError, herdr.HerdrFailure) as error:
            _anomaly(row, error)
        # Read liveness again immediately before the launch.
        current = herdr.read_conversation(str(row.current_session), kind)
        if isinstance(current, herdr.LiveConversation):
            print(f"{row.name}: unchanged")
            return ""
        if isinstance(current, herdr.UnknownConversation) and not isinstance(live, herdr.UnknownConversation):
            herdr.conversation_note(current)
        operation_deadline.check()
        result = herdr.launch_prepared_agent(client.session, target, prepared, kind,
            trust_arguments(kind, str(folder), arguments), herdr.NoOpeningPrompt(),
            int(operation_deadline.remaining(herdr.PROGRAM_TIMEOUT_SECONDS) * 1000), agent=row.name,
            reported_unknown=(current.reason if isinstance(current, herdr.UnknownConversation) else
                              live.reason if isinstance(live, herdr.UnknownConversation) else ""))
        try:
            client.remember_row_tab(str(row.id), result.pane)
            if present:
                client.focus_tab_of(result.pane)
                client.close_startup_workspaces(result.pane.split(":", 1)[0])
        except (OSError, ValueError, herdr.HerdrFailure, subprocess.SubprocessError) as error:
            _anomaly(row, error)
        print(f"{row.name}: restarted")
        return result.pane


def row_panes(rows: list[staff.StaffRow], client: herdr.Herdr) -> list[tuple[staff.StaffRow, str]]:
    try:
        panes = client.panes()
        return [(row, pane["pane_id"]) for row in rows for pane in panes if pane["session_id"] == row.current_session]
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
    selected = live_members(record.rows, record, resolver=resolver, codex=codex)
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
    else:
        pending_ids = list(only) if only else shutdown_record.read()
        if dry_run and not only:
            path = shutdown_record.record_path()
            saved_at = time.strftime("%H:%M", time.localtime(path.stat().st_mtime)) if path.exists() else "unknown"
            print(f"restart record: {len(pending_ids)} rows saved at {saved_at}")
            for row in live_rows(record, client, resolver=resolver):
                if str(row.id) not in pending_ids:
                    print(f"{row.name}: live, not in the restart record")
        for reference in pending_ids:
            row = record.by_member_reference(reference) if only else record.by_id(staff.StaffId(reference))
            if only and row is None:
                matches = [item for item in record.rows if item.name == reference]
                row = matches[0] if len(matches) == 1 else None
            if row is None:
                print(f"{reference}: unknown or ambiguous staff row" if only else
                      f"{' '.join(reference.split())}: restart anomaly: in the restart record, no fixed staff id", file=sys.stderr)
                continue
            selected.append(row)
    if dry_run:
        positions = {tab.get("row_id"): f"workspace {space['name']}, tab {index} ({tab['name']})"
                     for space in herdr.saved_layout(client.session)["workspaces"]
                     for index, tab in enumerate(space["tabs"], 1) if tab.get("row_id")}
        for row, pane in row_panes(selected, client):
            try:
                workspace, index, tab = client.pane_tab(pane)
                positions[str(row.id)] = f"workspace {workspace}, tab {index} ({tab['name']})"
            except (OSError, ValueError, herdr.HerdrFailure) as error:
                _anomaly(row, error)
        for row in selected:
            print(f"{row.name}: {row.id}, {row.kind}, {positions.get(str(row.id), 'no tab')}")
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
                resolver.record = record
                _ = start_row(current, record, client, present=present, resolver=resolver)
                started.append(str(row.id))
                present = False
            except errors as error:
                pending.append(str(row.id))
                _anomaly(row, error)
        if started or pending:
            shutdown_record.update(started=started, pending=pending)
    return 0
