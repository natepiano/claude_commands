#!/usr/bin/env python3
"""Print who manages whom among this machine's sessions, for cargo-handler's census.

`--json` prints `{"schema": 1, "edges": [{"manager": ..., "member": ..., "role": ...}]}` and
exits 0. A manager and a showrunner, unit director or Claude worker member are Claude Code session
ids; a Codex worker member is its thread id, since a Codex worker has no Claude session. The edges:

- a showrunner to each child showrunner, role `showrunner`, from the staff record;
- a showrunner to each live unit director, role `unit`, with its parent from the staff record;
- a unit director to each Codex worker of its run, with the worker's role from `mesh_roster.json`;
- a unit director to each Claude worker of its run, its member that worker's Claude session id and
  its role the one `board.sh roles` reports for its slot;
- a unit director to each pool reviewer it leases, role `review`, its member the reviewer's thread
  id, from the review pool's `pool.json`. It also carries `unit` (the lease's unit), `thread_id`,
  `state` (`busy` while its `busy_pid` runs, else `idle`) and `type`. Templates are left out.
- a Claude manager to each local Astra thread it holds, role `astra-designer` or
  `astra-reviewer`, from the Astra role records. Each adds `codex_home` and `state`
  (`busy` while its mesh worker runs or waits for capacity, or its exec pid runs; else `idle`).
  A thread with an Astra role has no Codex worker edge.

Runs are found by scanning both run roots, not through the session markers: a director runs
independent phases side by side, each in its own run folder, and its one marker names only one of
them. A run's director is the one `unit_runs.director` reads from the run folder.

Staff membership is read once from its owner; session and worker records are local.
An edge is printed while its member runs on this
machine; its manager may run on another one, since cargo-handler joins session ids across machines,
so a showrunner is named by its production's update timer wherever it runs. cargo-handler polls this every 30
seconds, so it never prompts and answers well under a second. A source that cannot be read
leaves its edges out and says why on stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Literal, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "message"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.astra import astra_roles  # noqa: E402
from scripts.production import staff, staff_sources
import showrunners  # noqa: E402
import showrunner_tree
import unit_lookup  # noqa: E402
import unit_runs  # noqa: E402
from sessions import running_sessions  # noqa: E402
import roster  # noqa: E402

Role = Literal["showrunner", "unit", "impl", "test", "review", "fix", "astra-designer", "astra-reviewer"]
WORKER_ROLES: tuple[Role, ...] = ("impl", "test", "review", "fix")
# A worker in either state has ended its thread; every other status is a live thread.
ENDED_STATUSES = frozenset({"ended", "failed"})
ROSTER_FILE = "mesh_roster.json"
SERVER_FILE = "mesh_server.json"
# A Claude worker's short id and name, one `<id>\t<name>` line each, appended by agent_bg.sh.
WORKER_LEDGER = "workers"
BOARD = Path(__file__).resolve().parent.parent / "direct" / "board.sh"


class Edge(TypedDict):
    manager: str
    member: str
    role: Role


class ReviewerEdge(Edge):
    unit: str
    thread_id: str
    state: Literal["busy", "idle"]
    type: str


class AstraEdge(Edge):
    codex_home: str
    state: Literal["busy", "idle"]


def pool_root() -> Path:
    return Path(os.environ.get("REVIEW_POOL_STATE", "~/.local/state/review-pool")).expanduser()


def _warn(text: str) -> None:
    print(f"production_tree: {text}", file=sys.stderr)


def _json_object(path: Path) -> dict[str, object]:
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError):
        return {}
    return cast(dict[str, object], raw) if isinstance(raw, dict) else {}


def showrunner_sessions() -> dict[str, str]:
    """Each production slug to its showrunner's session id, from its update timer, wherever it runs."""
    found: dict[str, str] = {}
    for instance in sorted(showrunners.NOTIFIER_STATE_DIR.glob("showrunner-*")):
        try:
            lines = (instance / "conf").read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        target = next((line.removeprefix("TARGET=") for line in lines if line.startswith("TARGET=")), "")
        session_id = target.removeprefix("session:") if target.startswith("session:") else ""
        if session_id:
            found[instance.name.removeprefix("showrunner-")] = session_id
    return found


def showrunner_edges(sessions: dict[str, str], running: set[str], record: staff.StaffSnapshot) -> list[Edge]:
    return [
        Edge(manager=sessions[parent], member=sessions[child], role="showrunner")
        for child, parent in sorted(showrunner_tree.edges(set(sessions), record=record).items())
        if sessions[child] in running
    ]


def marked_units(*, record: staff.StaffSnapshot | None = None) -> unit_lookup.MarkedProductions | None:
    try:
        return unit_lookup.every_marked_unit(record=record)
    except OSError as error:
        _warn(f"unit marks unreadable: {error}")
        return None


def unit_edges(sessions: dict[str, str], running: set[str], marked: unit_lookup.MarkedProductions,
               record: staff.StaffSnapshot) -> list[Edge]:
    managers: dict[staff.StaffId, str] = {}
    for slug, row in staff_sources.showrunner_members(record).items():
        if slug in sessions:
            managers[row.id] = sessions[slug]
    edges: list[Edge] = []
    for slug, units in sorted(marked.units.items()):
        for unit in sorted(units):
            claude = units[unit].claude
            if not isinstance(claude, unit_lookup.LiveClaude) or claude.session_id not in running:
                continue
            found = staff_sources.member(staff_sources.ObservedUnit(unit, units[unit].staff_id), record)
            if isinstance(found, staff_sources.NoSuchMember) or found.row.parent is None:
                continue
            manager = managers.get(found.row.parent, "")
            if manager:
                edges.append(Edge(manager=manager, member=claude.session_id, role="unit"))
    return edges


def _server_running(run: Path) -> bool:
    pid = _json_object(run / SERVER_FILE).get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _worker_role(entry: dict[str, object]) -> roster.WorkerRoleState:
    """The stored assignment; a name supplies no assignment."""
    return roster.worker_role(entry)


def _codex_edges(director: str, run: Path) -> list[Edge]:
    edges: list[Edge] = []
    records = _json_object(run / ROSTER_FILE)
    for name in sorted(records):
        value = records[name]
        entry = cast(dict[str, object], value) if isinstance(value, dict) else {}
        thread = entry.get("thread_id")
        role = _worker_role(entry)
        if isinstance(thread, str) and thread and isinstance(role, str) and entry.get("status") not in ENDED_STATUSES:
            edges.append(Edge(manager=director, member=thread, role=role))
    return edges


def _claude_workers(run: Path, running: set[str]) -> dict[str, str]:
    """Each running Claude worker's session id to its slot: from its launch record, else from the
    workers ledger, whose short id prefixes the session id and whose name ends with the slot.

    implement.sh puts every Claude worker it launches in the ledger, so a run without one has
    none, and the scan of every run folder skips its launch records unread."""
    try:
        ledger = (run / WORKER_LEDGER).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}
    workers: dict[str, str] = {}
    for path in sorted(run.glob("impl_worker_*.json")):
        record = _json_object(path)
        worker, slot = record.get("worker_id"), record.get("slot")
        if record.get("family") == "claude" and isinstance(worker, str) and worker in running and isinstance(slot, str):
            workers[worker] = slot
    for line in ledger:
        short, _, name = line.partition("\t")
        short = short.strip()
        session = next((session for session in sorted(running) if short and session.startswith(short)), "")
        if session and session not in workers:
            workers[session] = name.strip().rpartition("-")[2]
    return workers


def _board_roles(run: Path) -> dict[str, str]:
    """Each slot's role now, as `board.sh roles` reads it off the run's board.log."""
    try:
        shown = subprocess.run(["bash", str(BOARD), "roles", str(run)], capture_output=True, text=True,
                               check=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as error:
        _warn(f"board roles unreadable for {run}: {error}")
        return {}
    return dict(line.split("\t", 1) for line in shown.stdout.splitlines() if "\t" in line)


def _claude_edges(director: str, run: Path, workers: dict[str, str]) -> list[Edge]:
    """A Claude-only run has no app-server, so these edges skip the server check."""
    if not workers:
        return []
    roles = _board_roles(run)
    known: dict[str, Role] = {role: role for role in WORKER_ROLES}
    edges: list[Edge] = []
    for worker, slot in sorted(workers.items()):
        role = known.get(roles.get(slot, ""))
        if role:
            edges.append(Edge(manager=director, member=worker, role=role))
    return edges


def _pid_running(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def reviewer_edges(running: set[str], marked: unit_lookup.MarkedProductions) -> list[ReviewerEdge]:
    """Each leased pool reviewer under the running unit director that holds its lease."""
    edges: list[ReviewerEdge] = []
    for slug, units in sorted(marked.units.items()):
        reviewers = _json_object(pool_root() / slug / "pool.json").get("reviewers")
        if not isinstance(reviewers, dict):
            continue
        for _name, raw in sorted(cast(dict[str, object], reviewers).items()):
            if not isinstance(raw, dict):
                continue
            reviewer = cast(dict[str, object], raw)
            lease = reviewer.get("lease")
            leased = cast(dict[str, object], lease).get("unit") if isinstance(lease, dict) else None
            thread, kind = reviewer.get("thread_id"), reviewer.get("type")
            director = units.get(leased) if isinstance(leased, str) else None
            claude = director.claude if director is not None else None
            if (isinstance(leased, str) and isinstance(thread, str) and thread and isinstance(kind, str)
                    and isinstance(claude, unit_lookup.LiveClaude) and claude.session_id in running):
                edges.append(ReviewerEdge(manager=claude.session_id, member=thread, role="review", unit=leased,
                                          thread_id=thread,
                                          state="busy" if _pid_running(reviewer.get("busy_pid")) else "idle",
                                          type=kind))
    return edges


def astra_edges() -> list[AstraEdge]:
    """Each live Astra thread under its Claude manager, wherever that manager runs."""
    return [AstraEdge(manager=record["manager"], member=record["thread_id"],
                      role="astra-designer" if record["role"] == "designer" else "astra-reviewer",
                      codex_home=record["codex_home"], state=record["state"])
            for record in astra_roles.live_roles()]


def worker_edges(running: set[str], astra_threads: set[str] | None = None) -> list[Edge]:
    if astra_threads is None:
        astra_threads = {record["thread_id"] for record in astra_roles.live_roles()}
    edges: list[Edge] = []
    for root in unit_runs.run_roots():
        try:
            runs = sorted(path for path in root.iterdir() if path.is_dir())
        except OSError:
            continue
        for run in runs:
            server = _server_running(run)
            claude = _claude_workers(run, running)
            if not server and not claude:
                continue
            director = unit_runs.director(run)
            if director not in running:
                continue
            if server:
                edges.extend(edge for edge in _codex_edges(director, run) if edge["member"] not in astra_threads)
            edges.extend(_claude_edges(director, run, claude))
    return edges


def tree() -> list[Edge]:
    running = {record["sessionId"] for record in running_sessions()}
    sessions = showrunner_sessions()
    marked = None
    astra = astra_edges()
    showrunner: list[Edge] = []
    directors: list[Edge] = []
    try:
        record = staff.snapshot()
        marked = marked_units(record=record)
        showrunner = showrunner_edges(sessions, running, record)
        if marked is not None:
            directors = unit_edges(sessions, running, marked, record)
    except staff.StaffUnreadable as error:
        _warn(f"staff unreadable: {error}")
    reviewers = reviewer_edges(running, marked) if marked is not None else []
    return [*showrunner, *directors, *reviewers,
            *worker_edges(running, {edge["member"] for edge in astra}), *astra]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    _ = parser.add_argument("--json", action="store_true", required=True, help="print the edges as JSON")
    _ = parser.parse_args(argv)
    print(json.dumps({"schema": 1, "edges": tree()}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
