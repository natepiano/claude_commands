#!/usr/bin/env python3
"""Print who manages whom among this machine's sessions, for cargo-handler's census.

`--json` prints `{"schema": 1, "edges": [{"manager": ..., "member": ..., "role": ...}]}` and
exits 0. A manager and a showrunner, unit director or Claude worker member are Claude Code session
ids; a Codex worker member is its thread id, since a Codex worker has no Claude session. The edges:

- a showrunner to each child showrunner, role `showrunner`, from the showrunner parent files;
- a showrunner to each unit director marked with its production, role `unit`;
- a unit director to each Codex worker of its run, with the worker's role from `mesh_roster.json`;
- a unit director to each Claude worker of its run, its member that worker's Claude session id and
  its role the one `board.sh roles` reports for its slot.

Only local records are read, nothing over ssh. An edge is printed while its member runs on this
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
import showrunners  # noqa: E402
import unit_lookup  # noqa: E402
import unit_runs  # noqa: E402
from sessions import running_sessions  # noqa: E402

Role = Literal["showrunner", "unit", "impl", "test", "review", "fix"]
WORKER_ROLES: tuple[Role, ...] = ("impl", "test", "review", "fix")
# A worker in either state has ended its thread; every other status is a live thread.
ENDED_STATUSES = frozenset({"ended", "failed"})
ROSTER_FILE = "mesh_roster.json"
SERVER_FILE = "mesh_server.json"
# A Claude worker's short id and name, one `<id>\t<name>` line each, appended by agent_bg.sh.
WORKER_LEDGER = "workers"
BOARD = Path(__file__).resolve().parent.parent / "delegate" / "board.sh"


class Edge(TypedDict):
    manager: str
    member: str
    role: Role


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


def showrunner_edges(sessions: dict[str, str], running: set[str]) -> list[Edge]:
    return [
        Edge(manager=sessions[parent], member=sessions[child], role="showrunner")
        for child, parent in sorted(showrunners.edges(set(sessions)).items())
        if sessions[child] in running
    ]


def unit_edges(sessions: dict[str, str], running: set[str]) -> list[Edge]:
    try:
        marked = unit_lookup.every_marked_unit()
    except OSError as error:
        _warn(f"unit marks unreadable: {error}")
        return []
    edges: list[Edge] = []
    for slug, units in sorted(marked.units.items()):
        manager = sessions.get(slug, "")
        for unit in sorted(units):
            claude = units[unit].claude
            if manager and isinstance(claude, unit_lookup.LiveClaude) and claude.session_id in running:
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


def _worker_role(name: str, entry: dict[str, object]) -> Role | None:
    """The roster's role, else the role its name ends with, as older rosters name workers."""
    role = entry.get("role")
    suffix = name.rpartition("-")[2]
    return next((known for known in WORKER_ROLES if known in (role, suffix)), None)


def _codex_edges(director: str, run: Path) -> list[Edge]:
    edges: list[Edge] = []
    for name, value in sorted(_json_object(run / ROSTER_FILE).items()):
        entry = cast(dict[str, object], value) if isinstance(value, dict) else {}
        thread = entry.get("thread_id")
        role = _worker_role(name, entry)
        if isinstance(thread, str) and thread and role and entry.get("status") not in ENDED_STATUSES:
            edges.append(Edge(manager=director, member=thread, role=role))
    return edges


def _claude_workers(run: Path, running: set[str]) -> dict[str, str]:
    """Each running Claude worker's session id to its slot: from its launch record, else from the
    workers ledger, whose short id prefixes the session id and whose name ends with the slot."""
    workers: dict[str, str] = {}
    for path in sorted(run.glob("impl_worker_*.json")):
        record = _json_object(path)
        worker, slot = record.get("worker_id"), record.get("slot")
        if record.get("family") == "claude" and isinstance(worker, str) and worker in running and isinstance(slot, str):
            workers[worker] = slot
    try:
        ledger = (run / WORKER_LEDGER).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        ledger = []
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


def _claude_edges(director: str, run: Path, running: set[str]) -> list[Edge]:
    """A Claude-only run has no app-server, so these edges skip the server check."""
    workers = _claude_workers(run, running)
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


def worker_edges(running: set[str]) -> list[Edge]:
    edges: list[Edge] = []
    for active in unit_runs.active_dirs():
        try:
            markers = sorted(active.iterdir())
        except OSError:
            continue
        for marker in markers:
            if marker.name not in running:
                continue
            try:
                run = Path(marker.read_text(encoding="utf-8").splitlines()[0].strip())
            except (OSError, UnicodeError, IndexError):
                continue
            if _server_running(run):
                edges.extend(_codex_edges(marker.name, run))
            edges.extend(_claude_edges(marker.name, run, running))
    return edges


def tree() -> list[Edge]:
    running = {record["sessionId"] for record in running_sessions()}
    sessions = showrunner_sessions()
    return [*showrunner_edges(sessions, running), *unit_edges(sessions, running), *worker_edges(running)]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    _ = parser.add_argument("--json", action="store_true", required=True, help="print the edges as JSON")
    _ = parser.parse_args(argv)
    print(json.dumps({"schema": 1, "edges": tree()}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
