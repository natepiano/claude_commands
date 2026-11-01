#!/usr/bin/env python3
"""Check staff against live identities and recorded ownership without repairing it."""
from __future__ import annotations

from collections import defaultdict
from contextlib import redirect_stderr
from dataclasses import dataclass
import io
from pathlib import Path
import shlex
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import staff, staff_sources, unit_lookup, showrunners
from scripts.production import production_units
from scripts.message import process_table
from scripts.shutdown import remote
import herdr
import sessions


@dataclass(frozen=True)
class LiveStaffSession:
    """A live agent's observed staff mark; no name-based replacement is allowed."""

    pid: int
    identity: str
    staff_id: staff.StaffId
    cwd: str = ""


@dataclass(frozen=True)
class CheckEvidence:
    sources: unit_lookup.MemberSources
    sessions: tuple[LiveStaffSession, ...]
    workers: staff_sources.WorkerEvidence
    uncertainties: tuple[str, ...] = ()


def _codex_thread_host(arguments: list[str]) -> bool:
    """An app-server hosts threads; its inherited staff mark is not a session."""
    index = 2 if arguments and Path(arguments[0]).name in ("node", "nodejs") else 1
    while index < len(arguments):
        option = arguments[index]
        if option in ("-c", "--config", "--enable", "--disable", "--remote",
                      "--remote-auth-token-env", "--local-provider", "-m", "--model",
                      "-p", "--profile", "-s", "--sandbox", "-a", "--ask-for-approval",
                      "-C", "--cd", "--add-dir", "-i", "--image"):
            index += 2
        elif option.startswith("-") and option != "--":
            index += 1
        else:
            return option == "app-server"
    return False


def read_evidence(record: staff.StaffSnapshot) -> CheckEvidence:
    """Use strict public readers; unreadable evidence never becomes absence."""
    try:
        sources = unit_lookup.member_sources(unreadable=showrunners.UnreadableTimerPolicy.STRICT)
    except (OSError, ValueError, KeyError) as error:
        raise staff.StaffUnreadable(f"production timers / pane launches: {error}") from error
    registry = sessions.read_registry()
    if registry.unreadable:
        raise staff.StaffUnreadable("Claude registry: " + ", ".join(map(str, registry.unreadable)))
    try:
        table = process_table.snapshot()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise staff.StaffUnreadable(f"process table: {error}") from error
    running = {entry.record["pid"]: entry.record for entry in registry.entries
               if sessions.running_session(entry.record)}
    records = {entry.record["pid"]: entry.record for entry in registry.entries}
    observed: list[LiveStaffSession] = []
    uncertainties: list[str] = []
    for process in sorted(table.values(), key=lambda entry: entry.pid):
        try:
            arguments = shlex.split(process.arguments)
        except ValueError:
            arguments = [process.command]
        claude = process.pid in running or herdr.is_claude_process(arguments)
        codex = herdr.is_codex_process(arguments)
        if not claude and not codex:
            continue
        if codex and _codex_thread_host(arguments):
            continue
        registered = records.get(process.pid)
        if claude and sessions.headless_process(
            process.pid, registered.get("entrypoint", "") if registered is not None else ""
        ):
            continue
        try:
            environment = herdr.process_environment(process.pid)
        except (OSError, herdr.HerdrFailure, subprocess.SubprocessError) as error:
            raise staff.StaffUnreadable(f"process {process.pid} environment: {error}") from error
        identity = environment.get("STAFF_ID", "")
        if not identity:
            continue
        registered = running.get(process.pid)
        if claude:
            if registered is None:
                uncertainties.append(
                    f"{identity} uncertain: Claude process pid {process.pid} has no live registry session (STAFF_ID={identity})"
                )
                continue
            key = "claude:" + registered["sessionId"]
        else:
            key = "codex:" + environment.get("CODEX_THREAD_ID", f"pid:{process.pid}")
        observed.append(LiveStaffSession(process.pid, key, staff.StaffId(identity),
                                        registered.get("cwd", "") if registered is not None else ""))
    workers = staff_sources.observe_workers(record, sources)
    return CheckEvidence(sources, tuple(observed), workers, tuple(uncertainties))


def wrong_rows(record: staff.StaffSnapshot, evidence: CheckEvidence) -> list[str]:
    """Compare observations with the graph; retain every uncertain fact for review."""
    problems: dict[tuple[str, str], list[str]] = defaultdict(list)

    def wrong(identity: str, name: str, reason: str) -> None:
        if reason not in problems[identity, name]:
            problems[identity, name].append(reason)

    def row_wrong(row: staff.StaffRow, reason: str) -> None:
        wrong(row.id, row.name, reason)

    claims: dict[str, set[staff.StaffId]] = defaultdict(set)
    by_identity: dict[str, LiveStaffSession] = {}
    for session in evidence.sessions:
        by_identity[session.identity] = session
        row = record.by_id(session.staff_id)
        if row is None:
            wrong(session.staff_id, f"pid {session.pid}", "live session names a removed or missing staff row")
            continue
        claims[session.identity].add(row.id)
    for identity, backend in evidence.workers.identities.items():
        if record.by_id(identity) is not None:
            for family, value in backend.items():
                claims[f"{family}:{value}"].add(identity)
    for observation in evidence.workers.workers:
        owner = observation.owner.match
        if not isinstance(owner, staff.StaffRow):
            wrong("?", observation.name, "uncertain: worker has no existing staff parent")
            continue
        if isinstance(observation.identity, staff_sources.BackendUnavailable):
            wrong(owner.id, owner.name, f"uncertain: worker {observation.name} backend is unavailable")
            continue
        matches = [row for row in record.find(kind="worker", parent=owner.id) if row.name == observation.name]
        actual = [by_identity[key] for family, value in observation.identity.items()
                  if (key := f"{family}:{value}") in by_identity]
        if not matches:
            wrong("?", observation.name, f"live worker has no row under {owner.id} {owner.name}")
        for session in actual:
            row = record.by_id(session.staff_id)
            if row is not None and (row.kind != "worker" or row.parent != owner.id):
                row_wrong(row, f"live worker belongs under {owner.id} {owner.name}")
        for row in matches:
            for family, value in observation.identity.items():
                claims[f"{family}:{value}"].add(row.id)
    live: dict[staff.StaffId, set[str]] = defaultdict(set)
    for identity, claimed_rows in sorted(claims.items()):
        if len(claimed_rows) > 1:
            for claimed in sorted(claimed_rows):
                row = record.by_id(claimed)
                if row is not None:
                    others = ", ".join(sorted(claimed_rows - {claimed}))
                    row_wrong(row, f"uncertain: session also names staff row {others}: {identity}")
            continue
        for claimed in claimed_rows:
            live[claimed].add(identity)
    for row in record.rows:
        identities = {identity for identity, claimed_rows in claims.items() if row.id in claimed_rows}
        if len(identities) > 1:
            row_wrong(row, "uncertain: multiple live sessions: " + ", ".join(sorted(identities)))
        parent = record.by_id(row.parent) if row.parent is not None else None
        if parent is not None:
            if live[row.id] and not live[parent.id]:
                row_wrong(row, f"live row has no live parent {parent.id} {parent.name}")
        if live[row.id] and (isinstance(row.worktree, staff.RemovedWorktree) or isinstance(row.doc, staff.WrappedDoc)):
            row_wrong(row, "live session is attached to a retired row")
    local_machine = remote.local_machine()
    metadata_errors = io.StringIO()
    with redirect_stderr(metadata_errors):
        metadata = unit_lookup.unit_metadata(record, evidence.sources)
    runners = evidence.sources.showrunner_members(record)
    for row in record.rows:
        if row.kind == "unit":
            location = row.worktree
            if isinstance(location, staff.RemovedWorktree):
                continue
            if isinstance(location, staff.WorktreeUnfilled):
                row_wrong(row, "worktree is unfilled")
                continue
            if location.machine != local_machine:
                row_wrong(row, f"uncertain: worktree is on {location.machine}: {location.path}")
                continue
            if not location.path.is_dir():
                row_wrong(row, f"worktree does not exist: {location.path}")
            elif not production_units.worktree_is_linked(str(location.path)):
                row_wrong(row, f"not a linked git worktree: {location.path}")
            cells = metadata.get(row.id)
            declared = production_units.cell_value(cells.get("Worktree", "")) if cells is not None else ""
            if not declared or declared in ("—", "-"):
                row_wrong(row, "uncertain: no document worktree bound to this unit")
            elif Path(declared).expanduser() != location.path:
                row_wrong(row, f"worktree belongs elsewhere: document names {declared}, staff names {location.path}")
        elif row.kind == "showrunner":
            doc = row.doc
            if isinstance(doc, staff.DocUnfilled):
                row_wrong(row, "production doc is unfilled")
                continue
            if doc.machine != local_machine:
                row_wrong(row, f"uncertain: production doc is on {doc.machine}: {doc.path}")
                continue
            if not doc.path.is_file():
                row_wrong(row, f"production doc does not exist: {doc.path}")
            if isinstance(doc, staff.WrappedDoc):
                continue
            bound = [evidence.sources.productions[slug].timer.doc for slug, runner in runners.items() if runner.id == row.id]
            if not bound:
                row_wrong(row, "uncertain: no timer document bound to this showrunner")
            elif len(bound) != 1 or bound[0] != doc.path:
                row_wrong(row, "production doc belongs elsewhere: timer names " + ", ".join(map(str, bound)))
    for message in evidence.uncertainties:
        identity, _, reason = message.partition(" ")
        row = record.by_id(staff.StaffId(identity))
        wrong(identity, row.name if row is not None else "session", reason)
    for message in evidence.workers.diagnostics:
        wrong("?", "sources", "uncertain: " + message)
    return [f"{identity} {name}: {'; '.join(reasons)}" for (identity, name), reasons in sorted(problems.items())]


def main() -> int:
    try:
        try:
            record = staff.snapshot()
        except (OSError, ValueError) as error:
            raise staff.StaffUnreadable(f"staff record: {error}") from error
        evidence = read_evidence(record)
        lines = wrong_rows(record, evidence)
    except (OSError, ValueError, KeyError, herdr.HerdrFailure, subprocess.SubprocessError) as error:
        print("staff check: cannot read source: " + str(error).replace("\n", " "))
        return 2
    for line in lines:
        print(line.replace("\n", " ").replace("\r", " "))
    print(f"checked {len(record.rows)} rows; {len(lines)} wrong")
    return 1 if lines else 0


if __name__ == "__main__":
    raise SystemExit(main())
