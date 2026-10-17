#!/usr/bin/env python3
"""mesh_runs.py — Run a Codex worker's long command detached and message the worker its result.

A command a Codex worker starts in its shell dies with the worker's turn: the exec session's
process group and its pty go when the turn ends or the thread unloads. A worker that waits on a
build therefore keeps its turn open and spends a model call on every wait. Here the command runs
under a supervisor in its own session, outside that group and pty, and the worker ends its turn.
When the command exits, its result reaches the worker as its next message.

Only a mesh worker detaches. The caller is a mesh worker when CODEX_THREAD_ID names a thread a
run roster holds and has not ended. A Claude caller keeps running attached: its background task
notice already brings it the result.

Verbs:
  start      [--session-dir D --to NAME] [--director-dir D] [--label L]
             [--done-message-file F] -- command...
             Write the run record, spawn its supervisor and return at once. Exit 3 when the
             caller is not a mesh worker, so the caller runs the command attached.
  supervise  <record>
             Run the command, wait for it, deliver its result, then mark the record finished.

Delivery: when the worker's launcher is attached, the result is held for it and opens the
worker's next turn. When none is, the result goes to the run's unit director through send.py,
and the director decides when to dispatch the worker. A result no director can be found for is
held for the worker's next dispatch, so it is never lost.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Literal, NotRequired, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.agents import codex_mesh
from scripts.lib import unit_runs

# Exit status of `start` when the caller is not a mesh worker: run the command attached.
NOT_A_MESH_WORKER = 3
# Lines of the log the result message carries.
RESULT_TAIL_LINES = 20
# Where run folders and their active markers live; tests point this at a temporary folder.
RUN_ROOTS: tuple[Path, ...] = unit_runs.run_roots()
SEND_SCRIPT = codex_mesh.SEND_SCRIPT
SEND_TIMEOUT_SECS = 120
FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


class RunRecord(TypedDict):
    run_id: str
    status: Literal["running", "finished"]
    session_dir: str
    worker: str
    thread_id: str
    label: str
    command: list[str]
    cwd: str
    log: str
    started_at: float
    pid: int
    director_dir: str
    done_message_file: str
    exit_code: NotRequired[int]
    finished_at: NotRequired[str]
    delivery: NotRequired[str]


@dataclass(frozen=True)
class MeshCaller:
    """The worker seat a command came from."""

    session_dir: str
    name: str
    thread_id: str


@dataclass(frozen=True)
class NotMeshWorker:
    """The caller is not a live mesh worker, so its command runs attached."""


Caller = MeshCaller | NotMeshWorker


def _pool_state() -> Path:
    return Path(os.environ.get("REVIEW_POOL_STATE") or Path.home() / ".local/state/review-pool").expanduser()


def _roster_folders() -> list[Path]:
    """Every run's mesh folder: unit run folders, then review pool meshes."""
    folders = [path.parent for root in RUN_ROOTS for path in sorted(root.glob(f"*/{codex_mesh.ROSTER_FILE}"))]
    folders.extend(path.parent for path in sorted(_pool_state().glob(f"*/mesh/*/{codex_mesh.ROSTER_FILE}")))
    return folders


def caller_from_environment(environment: dict[str, str]) -> Caller:
    """The seat whose thread CODEX_THREAD_ID names. The server and port are never recorded:
    delivery looks the seat up again when the run finishes."""
    thread_id = environment.get("CODEX_THREAD_ID", "")
    if not thread_id:
        return NotMeshWorker()
    found: list[tuple[bool, MeshCaller]] = []
    for folder in _roster_folders():
        roster = codex_mesh._read_json_object(folder / codex_mesh.ROSTER_FILE)  # pyright: ignore[reportPrivateUsage]
        for name, value in roster.items():
            entry = codex_mesh._as_dict(value)  # pyright: ignore[reportPrivateUsage]
            if entry.get("thread_id") != thread_id or entry.get("status") == "ended":
                continue
            launcher = codex_mesh._pid_alive(codex_mesh._as_int(entry.get("launcher_pid")))  # pyright: ignore[reportPrivateUsage]
            found.append((launcher, MeshCaller(str(folder), name, thread_id)))
    if not found:
        return NotMeshWorker()
    found.sort(key=lambda item: not item[0])
    return found[0][1]


def _runs_dir(session_dir: str) -> Path:
    return Path(session_dir) / codex_mesh.DETACHED_RUNS_DIR


def _write_record(path: Path, record: RunRecord) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    _ = temporary.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _read_record(path: Path) -> RunRecord:
    raw = codex_mesh._read_json_object(path)  # pyright: ignore[reportPrivateUsage]
    if not raw.get("run_id") or not isinstance(raw.get("command"), list):
        raise SystemExit(f"mesh_runs: {path} is not a run record")
    return cast(RunRecord, cast(object, raw))


def command_start(args: argparse.Namespace) -> int:
    command = [str(part) for part in cast(list[object], getattr(args, "command", []) or [])]
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("mesh_runs: start needs a command after --", file=sys.stderr)
        return 2
    session_dir = str(getattr(args, "session_dir", "") or "")
    name = str(getattr(args, "to", "") or "")
    thread_id = ""
    if bool(session_dir) != bool(name):
        print("mesh_runs: --session-dir and --to go together", file=sys.stderr)
        return 2
    if not session_dir:
        caller = caller_from_environment(dict(os.environ))
        if isinstance(caller, NotMeshWorker):
            return NOT_A_MESH_WORKER
        session_dir, name, thread_id = caller.session_dir, caller.name, caller.thread_id
    label = str(getattr(args, "label", "") or "") or " ".join(command)
    done_file = str(getattr(args, "done_message_file", "") or "")
    director_dir = str(getattr(args, "director_dir", "") or "") or session_dir
    runs = _runs_dir(session_dir)
    runs.mkdir(parents=True, exist_ok=True)
    run_id = f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}-{secrets.token_hex(2)}"
    record_path = runs / f"{run_id}.json"
    log_path = runs / f"{run_id}.log"
    record: RunRecord = {
        "run_id": run_id, "status": "running", "session_dir": session_dir, "worker": name,
        "thread_id": thread_id, "label": label, "command": command, "cwd": os.getcwd(),
        "log": str(log_path), "started_at": time.time(), "pid": 0,
        "director_dir": director_dir, "done_message_file": done_file,
    }
    _write_record(record_path, record)
    with log_path.open("ab") as log:
        supervisor = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "supervise", str(record_path)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, cwd=os.getcwd(),
        )
    record["pid"] = supervisor.pid
    _write_record(record_path, record)
    print(f"run {run_id} started detached for {name} (pid {supervisor.pid}, log {log_path}); "
          + "its result arrives as your next message")
    return 0


def _exit_status(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def _tail(path: Path, count: int) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-count:]


def result_text(record: RunRecord, exit_code: int) -> str:
    verdict = "PASS (exit 0)" if exit_code == 0 else f"FAIL (exit {exit_code})"
    lines = [f"Run finished: {record['label']}", f"Result: {verdict}", f"Log: {record['log']}"]
    tail = _tail(Path(record["log"]), RESULT_TAIL_LINES)
    if tail:
        lines.extend(["", f"Last {len(tail)} log lines:", *tail])
    return "\n".join(lines)


def send_to_director(session_id: str, summary: str, text: str) -> bool:
    """One send.py call to a Claude session; tests replace this."""
    try:
        sent = subprocess.run(
            [sys.executable, str(SEND_SCRIPT), "--to", f"session:{session_id}", "--from", "mesh-runs",
             "--summary", summary, "--text", text],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=SEND_TIMEOUT_SECS, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"mesh_runs: could not tell session {session_id}: {exc}", file=sys.stderr)
        return False
    if sent.returncode:
        print(f"mesh_runs: could not tell session {session_id}: {sent.stderr.strip()}", file=sys.stderr)
        return False
    return True


def _director_sessions(record: RunRecord) -> list[str]:
    return codex_mesh._marker_sessions(RUN_ROOTS, Path(record["director_dir"]))  # pyright: ignore[reportPrivateUsage]


def deliver(record: RunRecord, exit_code: int) -> str:
    """Hand the result to the worker's attached launcher, else to its unit director.

    Returns the delivery outcome the record keeps.
    """
    done_file = record["done_message_file"]
    if done_file and exit_code != 0:
        return f"nothing delivered: exit {exit_code} with a done message"
    if done_file:
        try:
            text = Path(done_file).read_text(encoding="utf-8")
        except OSError as exc:
            text = f"Run finished: {record['label']}\nResult: PASS (exit 0)\n(done message {done_file} unreadable: {exc})"
    else:
        text = result_text(record, exit_code)
    session_dir, name = record["session_dir"], record["worker"]
    held = codex_mesh.hold_message(session_dir, name, text, without_launcher=False)
    if isinstance(held, (codex_mesh.HeldForLauncher, codex_mesh.HeldForNextDispatch)):
        return f"held for {name}'s attached launcher on thread {held.thread_id}"
    state = held.reason if isinstance(held, codex_mesh.WorkerEnded) else f"{name} is {held.status or 'unknown'}"
    note = (f"Seat {name}'s run {record['label']} finished (exit {exit_code}); no launcher is attached "
            + f"({state}), so the result comes to you. Dispatch the seat (implement.sh --to {name}) "
            + "when it should act on it.")
    message = "\n".join([note, f"Seat: {name}", f"Command: {' '.join(record['command'])}", "", text])
    told = [session for session in _director_sessions(record)
            if send_to_director(session, f"{name}: run {record['label']} finished (exit {exit_code})", message)]
    if told:
        return "sent to director " + ", ".join(told)
    if isinstance(held, codex_mesh.WorkerEnded):
        return f"nothing delivered: {held.reason} and no director was found"
    # No director to tell: hold the result so the worker's next dispatch still reads it.
    kept = codex_mesh.hold_message(session_dir, name, text, without_launcher=True)
    if isinstance(kept, codex_mesh.WorkerEnded):
        return f"nothing delivered: {kept.reason} and no director was found"
    return f"held for {name}'s next dispatch: no director was found"


def command_supervise(args: argparse.Namespace) -> int:
    record_path = Path(str(getattr(args, "record", "")))
    record = _read_record(record_path)
    environment = dict(os.environ)
    environment.update({
        "PLAN_DELEGATE_DETACHED_RUN": "1",
        "PLAN_DELEGATE_BOARD_DIR": record["session_dir"],
        "PLAN_DELEGATE_TEAM_ROLE": record["worker"].rsplit("-", 1)[-1],
    })
    log = Path(record["log"])
    try:
        with log.open("ab") as output:
            child = subprocess.Popen(record["command"], cwd=record["cwd"] or None, env=environment,
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
    except OSError as exc:
        with log.open("a", encoding="utf-8") as output:
            _ = output.write(f"mesh_runs: command did not start: {exc}\n")
        exit_code = 127
    else:
        def forward(signum: int, _frame: FrameType | None) -> None:
            with contextlib.suppress(OSError):
                child.send_signal(signum)

        for forwarded in FORWARDED_SIGNALS:
            _ = signal.signal(forwarded, forward)
        exit_code = _exit_status(child.wait())
    # Deliver before marking finished: a launcher that reads the run as finished closes its inbox
    # unless the result is already held.
    delivery = deliver(record, exit_code)
    record = _read_record(record_path)
    record.update({
        "status": "finished", "exit_code": exit_code,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "delivery": delivery,
    })
    _write_record(record_path, record)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    verbs = parser.add_subparsers(dest="verb", required=True)
    start = verbs.add_parser("start", help="run a command detached for the calling worker")
    _ = start.add_argument("--session-dir", default="")
    _ = start.add_argument("--to", default="")
    _ = start.add_argument("--director-dir", default="", help="the run folder whose director hears results")
    _ = start.add_argument("--label", default="")
    _ = start.add_argument("--done-message-file", default="")
    _ = start.add_argument("command", nargs=argparse.REMAINDER)
    start.set_defaults(handler=command_start)
    supervise = verbs.add_parser("supervise", help="run one recorded command and deliver its result")
    _ = supervise.add_argument("record")
    supervise.set_defaults(handler=command_supervise)
    args = parser.parse_args(argv)
    handler = cast("Callable[[argparse.Namespace], int]", getattr(args, "handler"))
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
