#!/usr/bin/env python3
"""Check staff against live identities and recorded ownership without repairing it."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import staff, staff_sources, unit_lookup, showrunners
from scripts.production import production_units
from scripts.lib import unit_runs
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
    notes: tuple[str, ...] = ()


def _codex_thread_host(arguments: list[str]) -> bool:
    return staff_sources.codex_thread_host(arguments)


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
    notes: list[str] = []
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
        identity = running[process.pid]["sessionId"] if process.pid in running else environment.get("CODEX_THREAD_ID", "")
        registered = running.get(process.pid)
        if claude:
            if registered is None:
                uncertainties.append(
                    f"pid {process.pid} uncertain: Claude process has no live registry identity"
                )
                continue
            key = "claude:" + registered["sessionId"]
        else:
            key = "codex:" + environment.get("CODEX_THREAD_ID", f"pid:{process.pid}")
        if not identity:
            if reason := staff_sources.codex_non_staff_reason(record, sources, environment):
                notes.append(f"staff sources: Codex process {process.pid}: {reason}")
                continue
            uncertainties.append(f"pid {process.pid} uncertain: Codex runtime identity is unavailable")
            continue
        observed.append(LiveStaffSession(process.pid, key, staff.StaffId(identity),
                                        registered.get("cwd", "") if registered is not None else ""))
    # The peer roster is the one remote runtime snapshot; an outage is uncertainty.
    from scripts.message import roster
    peer = roster.peer_snapshot(os.environ)
    if isinstance(peer, roster.PeerNotReached):
        uncertainties.append(f"{peer.host} uncertain: {peer.reason}")
    else:
        uncertainties.extend(f"peer uncertain: {problem}" for problem in peer.problems)
        for entry in peer.entries:
            if entry.role == "freestanding":
                notes.append(f"{entry.machine} freestanding {entry.name}: not staff")
                continue
            if isinstance(entry.session, roster.ClaudeSession):
                identity, family = entry.session.session_id, "claude"
            elif isinstance(entry.thread, roster.CodexThread):
                identity, family = entry.thread.thread_id, "codex"
            else:
                continue
            observed.append(LiveStaffSession(0, family + ":" + identity, staff.StaffId(identity), entry.cwd))
    for root in unit_runs.run_roots(os.environ.get("ROSTER_RUN_ROOT")):
        for run in sorted(root.glob("*")):
            try:
                publications = staff_sources.pending_publication_evidence(run)
                uncertainties.extend(f"{run} uncertain: {message}" for message in publications.diagnostics)
                pending = publications.workers
                if not pending:
                    continue
                from scripts.agents import codex_mesh
                import time
                loaded = codex_mesh.loaded_threads(run, time.monotonic() + codex_mesh.THREAD_RELEASE_TIMEOUT_SECS)
                for backend in pending:
                    if backend.identity in loaded:
                        observed.append(LiveStaffSession(0, "codex:" + backend.identity, backend.identity))
            except (OSError, ValueError, staff.StaffRefused) as error:
                uncertainties.append(f"{run} uncertain: {error}")
    workers = staff_sources.observe_workers(record, sources)
    return CheckEvidence(sources, tuple(observed), workers, tuple(uncertainties), tuple(notes))


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
        row = record.by_current_session(session.staff_id)
        if row is None:
            wrong(session.staff_id, f"pid {session.pid}", "live session names a removed or missing staff row")
            continue
        claims[session.identity].add(row.id)
    for identity, backend in evidence.workers.identities.items():
        row = record.by_id(identity)
        if row is not None:
            for family, value in backend.items():
                if value == row.current_session:
                    claims[f"{family}:{value}"].add(identity)
                else:
                    row_wrong(row, f"runtime identity differs from current session: {family}:{value}")
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
            row = record.by_current_session(session.staff_id)
            if row is not None and (row.kind != "worker" or row.parent != owner.id):
                row_wrong(row, f"live worker belongs under {owner.id} {owner.name}")
        for row in matches:
            for family, value in observation.identity.items():
                if row.current_session == value:
                    claims[f"{family}:{value}"].add(row.id)
                else:
                    row_wrong(row, f"runtime identity differs from current session: {family}:{value}")
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
    metadata = unit_lookup.unit_metadata(record, evidence.sources)
    for problem in metadata.problems:
        wrong(problem.unit_id, problem.name, problem.reason)
    timer_documents: dict[staff.StaffId, set[Path]] = defaultdict(set)
    for source in evidence.sources.productions.values():
        owner = record.by_doc(local_machine, source.timer.doc)
        if owner is None:
            owner = record.by_member_reference(source.timer.target.removeprefix("session:"))
        if owner is not None and owner.kind == "showrunner":
            timer_documents[owner.id].add(source.timer.doc)
        try:
            _ = production_units.unit_table(source.document.splitlines())
        except production_units.Refusal as error:
            if owner is not None:
                row_wrong(owner, f"uncertain: document unit table could not be read: {error}")
            continue
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
            bound = sorted(timer_documents[row.id])
            if not bound:
                row_wrong(row, "uncertain: no timer document bound to this showrunner")
            elif len(bound) != 1 or bound[0] != doc.path:
                row_wrong(row, "production doc belongs elsewhere: timer names " + ", ".join(map(str, bound)))
    for message in evidence.uncertainties:
        identity, _, reason = message.partition(" ")
        row = record.by_current_session(identity)
        wrong(row.id if row is not None else identity, row.name if row is not None else "session", reason)
    for message in evidence.workers.diagnostics:
        if staff_sources.is_non_staff_note(message):
            continue
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
    notes = {*evidence.notes, *(message for message in evidence.workers.diagnostics
                              if staff_sources.is_non_staff_note(message))}
    for note in sorted(notes):
        print("note: " + note.replace("\n", " ").replace("\r", " "))
    for line in lines:
        print(line.replace("\n", " ").replace("\r", " "))
    print(f"checked {len(record.rows)} rows; {len(lines)} wrong")
    return 1 if lines else 0


if __name__ == "__main__":
    raise SystemExit(main())
