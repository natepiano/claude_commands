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
             caller is not a mesh worker, so the caller runs the command attached. NAME may be
             the worker's role, the last part of its roster name; the record keeps the roster name.
             When the worker already has a live run of the same command in the same folder and the
             files did not change since it started (same tree key), start starts nothing: it
             names that run, whose result still arrives as the worker's next message.
  supervise  <record>
             Run the command, wait for it, deliver its result, then mark the record finished.
  reap       --session-dir D [--worker NAME]
             End the running records whose supervisor is gone (see below).

A record that says running while no supervisor runs it lost its supervisor to a kill or a crash,
so nothing delivered its result. `start` and the worker's launcher end such a record: it is
marked finished and the worker gets a FAIL result with no exit status, unless a newer run of the
same command in the same folder answers for it.

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
import shutil
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
from uuid import NAMESPACE_URL, uuid5

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.agents import codex_mesh
from scripts.buildlog import treekey
from scripts.lib import unit_runs

# Exit status of `start` when the caller is not a mesh worker: run the command attached.
NOT_A_MESH_WORKER = 3
# Lines of the log the result message carries.
RESULT_TAIL_LINES = 20
# Characters the result message keeps of one log line, and of the whole tail: the message
# is a Codex turn input, which Codex refuses over 1,048,576 characters.
RESULT_LINE_CHARS = 2_000
RESULT_TAIL_CHARS = 20_000
# verify.sh's VERIFY_SUPERSEDED_STATUS: a newer call of the same verb from the same worker in the
# same worktree replaced this one while it waited for the cargo token. It ran nothing, so it is
# neither a pass nor a failure, and the newer call's result answers instead.
SUPERSEDED_STATUS = 76
TREE_CHANGED_STATUS = 77
TREE_CHANGED_VERDICT = "CANCELLED (exit 77): resubmit after your edits."
SUPERSEDED_VERDICT = ("SUPERSEDED (exit 76): a newer call of the same verb replaced this one before it built. "
                      + "Nothing failed and nothing needs a repair; read the newer call's result.")
# The verdict of a run whose supervisor is gone: it reported no exit status.
ORPHAN_VERDICT = ("FAIL (no exit status): the run's supervisor process is gone, so the command was killed or "
                  + "crashed before it reported. Start the run again if you still need its result.")
# Where run folders and their active markers live; tests point this at a temporary folder.
RUN_ROOTS: tuple[Path, ...] = unit_runs.run_roots()
SEND_SCRIPT = codex_mesh.SEND_SCRIPT
SEND_TIMEOUT_SECS = 120
FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
# On natedev the command runs in a scope in builds.slice, beside the steps scripts/lint/invoke.sh
# places there, so its compiles take the builds' memory limit and CPU weight, below the sessions.
# Worker cargo builds ran at the panes' weight in herdr-agents.service (2026-10-20). The scope
# writes the marker just before the command starts; a marker missing after the scope exits proves
# the scope never ran it (no user manager, a systemd that rejects an option), so the command runs
# plainly. BUILDLOG_SCOPE=0 turns the scope off, as it does for invoke.sh.
SCOPE_SCRIPT = ': > "$0" || exit 125; exec "$@"'


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
    # The tree key of cwd at start, "" when unknown; records before 2026-10-21 have none.
    tree_key: NotRequired[str]
    exit_code: NotRequired[int]
    finished_at: NotRequired[str]
    delivery: NotRequired[str]
    # Set when the run ended without its supervisor: "supervisor gone".
    ended: NotRequired[str]


@dataclass(frozen=True)
class MeshCaller:
    """The worker a command came from."""

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
    from scripts.guilds import roles
    folders.extend(roles.durable_meshes())
    return folders


def caller_from_environment(environment: dict[str, str]) -> Caller:
    """The worker whose thread CODEX_THREAD_ID names. The server and port are never recorded:
    delivery looks the worker up again when the run finishes."""
    thread_id = environment.get("CODEX_THREAD_ID", "")
    if not thread_id:
        return NotMeshWorker()
    found: list[tuple[bool, MeshCaller]] = []
    for folder in _roster_folders():
        roster = codex_mesh._read_json_object(folder / codex_mesh.ROSTER_FILE)  # pyright: ignore[reportPrivateUsage]
        for name, value in roster.items():
            entry = codex_mesh._as_dict(value)  # pyright: ignore[reportPrivateUsage]
            if entry.get("thread_id") != thread_id or entry.get("status") == "ended" or entry.get("transferring_role") or entry.get("role_redirect"):
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


def seat_name(session_dir: str, name: str) -> str:
    """The roster's name for the worker `--to` names. A worker may call itself by its role, the last
    part of its roster name: on 2026-10-20 tool-based-ui-trunk-box started five runs with `--to box`,
    no roster entry matched at delivery, and each result was lost. A name the roster does not hold,
    such as a reviewer that registers after its run starts, stays as given."""
    roster = codex_mesh._read_json_object(Path(session_dir) / codex_mesh.ROSTER_FILE)  # pyright: ignore[reportPrivateUsage]
    if name in roster:
        return name
    matches = [seat for seat in roster if seat.endswith(f"-{name}")]
    return matches[0] if len(matches) == 1 else name


def _process_args(pid: int) -> list[str]:
    """The pid's command line; ps where there is no /proc (the Mac). Empty when it is gone."""
    if Path("/proc/self").exists():
        try:
            return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
        except OSError:
            return []
    shown = subprocess.run(["ps", "-p", str(pid), "-o", "args="], capture_output=True, text=True, check=False)
    return shown.stdout.split()


def supervises(pid: int, record_path: Path) -> bool:
    """Whether pid is the `supervise` process of this record, so a reused pid does not match."""
    if pid <= 0:
        return False
    parts = _process_args(pid)
    if "supervise" not in parts or not any(part.endswith("mesh_runs.py") for part in parts):
        return False
    expected = record_path.resolve()
    return any(Path(part).resolve() == expected for part in parts if part)


def _orphaned(path: Path, raw: dict[str, object], now: float) -> bool:
    """A running record that no supervisor runs. A record with no pid yet is still starting for its
    first minute: `start` writes the record before it spawns the supervisor."""
    if raw.get("status") != "running":
        return False
    pid = codex_mesh._as_int(raw.get("pid"))  # pyright: ignore[reportPrivateUsage]
    if pid > 0:
        return not supervises(pid, path)
    started = codex_mesh._as_float(raw.get("started_at"), now)  # pyright: ignore[reportPrivateUsage]
    return now - started >= codex_mesh.DETACHED_RUN_START_GRACE_SECS


def reap_orphans(session_dir: str, worker: str = "") -> list[str]:
    """End each running record whose supervisor is gone, of one worker or of all, and return their run ids.

    Its worker gets a FAIL result with no exit status, unless a newer run of the same command in
    the same folder answers for it. On 2026-10-22 a unit stopped 23 queued runs by their process
    group; a supervisor that dies that way delivers nothing, and its worker waited for a result
    that never came. A claim file keeps two callers from delivering one result twice.
    """
    runs = _runs_dir(session_dir)
    if not runs.is_dir():
        return []
    now = time.time()
    records = [(path, raw) for path in sorted(runs.glob("*.json"))
               if (raw := codex_mesh._read_json_object(path)).get("run_id")]  # pyright: ignore[reportPrivateUsage]
    reaped: list[str] = []
    for path, raw in records:
        if (worker and raw.get("worker") != worker) or not _orphaned(path, raw, now):
            continue
        claim = path.with_suffix(".reap")
        try:
            os.close(os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            continue
        try:
            record = _read_record(path)
            if record["status"] != "running":
                continue
            newer = any(other.get("worker") == record["worker"] and other.get("command") == record["command"]
                        and other.get("cwd") == record["cwd"]
                        and codex_mesh._as_float(other.get("started_at"), 0.0) > record["started_at"]  # pyright: ignore[reportPrivateUsage]
                        for _, other in records)
            delivery = ("nothing delivered: the supervisor is gone and a newer run of the same command answers"
                        if newer else deliver(record, None))
            record = _read_record(path)
            record.update({
                "status": "finished", "ended": "supervisor gone", "delivery": delivery,
                "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            _write_record(path, record)
            reaped.append(record["run_id"])
        finally:
            claim.unlink(missing_ok=True)
    return reaped


def live_twin(runs: Path, worker: str, command: list[str], cwd: str, tree_key: str) -> RunRecord | None:
    """A running record of the same worker, command, folder and tree key whose supervisor lives.
    After an interrupt on 2026-10-21, hana_catalyst-inspectors started its lint run three times
    on one tree; each run took memory, the cargo token and build time."""
    if not tree_key:
        return None
    for path in sorted(runs.glob("*.json")):
        raw = codex_mesh._read_json_object(path)  # pyright: ignore[reportPrivateUsage]
        if (raw.get("status") == "running" and raw.get("worker") == worker and raw.get("command") == command
                and raw.get("cwd") == cwd and raw.get("tree_key") == tree_key
                and supervises(codex_mesh._as_int(raw.get("pid")), path)):  # pyright: ignore[reportPrivateUsage]
            return cast(RunRecord, cast(object, raw))
    return None


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
    else:
        name = seat_name(session_dir, name)
    label = str(getattr(args, "label", "") or "") or " ".join(command)
    done_file = str(getattr(args, "done_message_file", "") or "")
    director_dir = str(getattr(args, "director_dir", "") or "") or session_dir
    runs = _runs_dir(session_dir)
    runs.mkdir(parents=True, exist_ok=True)
    _ = reap_orphans(session_dir)
    cwd = os.getcwd()
    tree_key = treekey.tree_key(cwd) or ""
    if twin := live_twin(runs, name, command, cwd, tree_key):
        started = time.strftime("%H:%M:%S", time.localtime(twin["started_at"]))
        print(f"run {twin['run_id']} is already running for {name} on these files (started {started}, "
              + f"log {twin['log']}); started nothing; its result arrives as your next message")
        return 0
    run_id = f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}-{secrets.token_hex(2)}"
    record_path = runs / f"{run_id}.json"
    log_path = runs / f"{run_id}.log"
    record: RunRecord = {
        "run_id": run_id, "status": "running", "session_dir": session_dir, "worker": name,
        "thread_id": thread_id, "label": label, "command": command, "cwd": cwd,
        "log": str(log_path), "started_at": time.time(), "pid": 0,
        "director_dir": director_dir, "done_message_file": done_file, "tree_key": tree_key,
    }
    _write_record(record_path, record)
    with log_path.open("ab") as log:
        supervisor = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "supervise", str(record_path)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, cwd=cwd,
        )
    record["pid"] = supervisor.pid
    _write_record(record_path, record)
    print(f"run {run_id} started detached for {name} (pid {supervisor.pid}, log {log_path}); "
          + "its result arrives as your next message")
    return 0


def _exit_status(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def _cut_line(line: str) -> str:
    if len(line) <= RESULT_LINE_CHARS:
        return line
    return f"{line[:RESULT_LINE_CHARS]} [cut: {len(line) - RESULT_LINE_CHARS} more characters]"


def _tail(path: Path, count: int) -> list[str]:
    """The log's last `count` lines, each cut to RESULT_LINE_CHARS, newest first kept within RESULT_TAIL_CHARS."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    kept: list[str] = []
    total = 0
    for line in map(_cut_line, reversed(lines[-count:])):
        total += len(line) + 1
        if total > RESULT_TAIL_CHARS:
            break
        kept.append(line)
    return kept[::-1]


def _exit_words(exit_code: int | None) -> str:
    """`exit N`, or `no exit status` for a run whose supervisor is gone."""
    return "no exit status" if exit_code is None else f"exit {exit_code}"


def result_text(record: RunRecord, exit_code: int | None) -> str:
    verdict = (ORPHAN_VERDICT if exit_code is None else "PASS (exit 0)" if exit_code == 0
               else SUPERSEDED_VERDICT if exit_code == SUPERSEDED_STATUS
               else TREE_CHANGED_VERDICT if exit_code == TREE_CHANGED_STATUS else f"FAIL (exit {exit_code})")
    heading = "Run ended with no result" if exit_code is None else "Run finished"
    lines = [f"{heading}: {record['label']}", f"Result: {verdict}", f"Log: {record['log']}"]
    tail = _tail(Path(record["log"]), RESULT_TAIL_LINES)
    if exit_code == TREE_CHANGED_STATUS:
        # Keep verify's resubmit instruction verbatim even if cleanup pushed it
        # out of the bounded tail. Delivery queues this same text for the worker.
        try:
            with Path(record["log"]).open(encoding="utf-8", errors="replace") as log:
                cancellation = next((line.rstrip("\n") for line in log
                                     if line.startswith("verify.sh: cancelled — ")), "")
        except OSError:
            cancellation = ""
        if cancellation and cancellation not in tail:
            lines.extend(["", cancellation])
    if tail:
        lines.extend(["", f"Last {len(tail)} log lines (lines over {RESULT_LINE_CHARS} characters are cut):", *tail])
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
    """The sessions whose run marker names the run folder, then the director the folder records.
    A director that runs two folders has one marker, naming the other folder, so only the
    recorded director finds it (trunk's phase 140 folder, 2026-10-20)."""
    folder = Path(record["director_dir"])
    marked = codex_mesh._marker_sessions(RUN_ROOTS, folder)  # pyright: ignore[reportPrivateUsage]
    recorded = unit_runs.director(folder)
    return [*marked, recorded] if recorded and recorded not in marked else marked


def deliver(record: RunRecord, exit_code: int | None) -> str:
    """Hand the result to the worker's attached launcher, else to its unit director.

    exit_code is None for a run whose supervisor is gone. Returns the delivery outcome the record keeps.
    """
    done_file = record["done_message_file"]
    if done_file and exit_code not in (0, SUPERSEDED_STATUS, TREE_CHANGED_STATUS):
        return f"nothing delivered: {_exit_words(exit_code)} with a done message"
    if done_file and exit_code == 0:
        try:
            text = Path(done_file).read_text(encoding="utf-8")
        except OSError as exc:
            text = f"Run finished: {record['label']}\nResult: PASS (exit 0)\n(done message {done_file} unreadable: {exc})"
    else:
        text = result_text(record, exit_code)
    session_dir, name = record["session_dir"], record["worker"]
    roster = codex_mesh._read_json_object(Path(session_dir) / codex_mesh.ROSTER_FILE)  # pyright: ignore[reportPrivateUsage]
    entry = codex_mesh._as_dict(roster.get(name))  # pyright: ignore[reportPrivateUsage]
    from scripts.guilds import roles
    destination = roles.transfer_route(record["thread_id"])
    if destination:
        destination_roster = codex_mesh._read_json_object(Path(destination) / codex_mesh.ROSTER_FILE)  # pyright: ignore[reportPrivateUsage]
        for value in destination_roster.values():
            candidate = codex_mesh._as_dict(value)  # pyright: ignore[reportPrivateUsage]
            if candidate.get("thread_id") == record["thread_id"]:
                entry = candidate
                break
    design_id, role = entry.get("held_design_id"), entry.get("held_role")
    if isinstance(design_id, str) and design_id and role in ("designer", "reviewer"):
        message_id = str(uuid5(NAMESPACE_URL, str(Path(session_dir).resolve()) + "/" + record["run_id"]))
        _ = roles.queue_message(design_id, role, "mesh_runs:" + record["run_id"],
                                text, message_id=message_id)
        return f"queued for held role {name}; its holder controls the next assignment"
    held = codex_mesh.hold_message(session_dir, name, text, without_launcher=False)
    if isinstance(held, codex_mesh.HeldForLauncher):
        return f"held for {name}'s attached launcher on thread {held.thread_id}"
    if isinstance(held, codex_mesh.HeldForNextDispatch):
        return f"held for {name}'s next dispatch on thread {held.thread_id}"
    state = held.reason if isinstance(held, codex_mesh.WorkerEnded) else f"{name} is {held.status or 'unknown'}"
    note = (f"Worker {name}'s run {record['label']} finished ({_exit_words(exit_code)}); no launcher is attached "
            + f"({state}), so the result comes to you. Dispatch the worker (implement.sh --to {name}) "
            + "when it should act on it.")
    message = "\n".join([note, f"Worker: {name}", f"Command: {' '.join(record['command'])}", "", text])
    told = [session for session in _director_sessions(record)
            if send_to_director(session, f"{name}: run {record['label']} finished ({_exit_words(exit_code)})", message)]
    if told:
        return "sent to director " + ", ".join(told)
    if isinstance(held, codex_mesh.WorkerEnded):
        return f"nothing delivered: {held.reason} and no director was found"
    # No director to tell: hold the result so the worker's next dispatch still reads it.
    kept = codex_mesh.hold_message(session_dir, name, text, without_launcher=True)
    if isinstance(kept, codex_mesh.WorkerEnded):
        return f"nothing delivered: {kept.reason} and no director was found"
    return f"held for {name}'s next dispatch: no director was found"


def scope_prefix(marker: Path, environment: dict[str, str]) -> list[str]:
    """The argv that runs a command in a builds.slice scope, or nothing where no scope is possible."""
    runtime = environment.get("XDG_RUNTIME_DIR", "")
    if (environment.get("BUILDLOG_SCOPE", "1") == "0" or not runtime
            or not Path(runtime, "systemd", "private").is_socket() or shutil.which("systemd-run") is None):
        return []
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--expand-environment=no",
            "--slice=builds.slice", "--", "/bin/sh", "-c", SCOPE_SCRIPT, str(marker)]


def _run(command: list[str], cwd: str, environment: dict[str, str], log: Path) -> int:
    """Run the command with its output appended to the log, forward signals to it, and return its status."""
    try:
        with log.open("ab") as output:
            child = subprocess.Popen(command, cwd=cwd or None, env=environment,
                                     stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
    except OSError as exc:
        with log.open("a", encoding="utf-8") as output:
            _ = output.write(f"mesh_runs: command did not start: {exc}\n")
        return 127

    def forward(signum: int, _frame: FrameType | None) -> None:
        with contextlib.suppress(OSError):
            child.send_signal(signum)

    for forwarded in FORWARDED_SIGNALS:
        _ = signal.signal(forwarded, forward)
    return _exit_status(child.wait())


def command_supervise(args: argparse.Namespace) -> int:
    record_path = Path(str(getattr(args, "record", "")))
    record = _read_record(record_path)
    environment = dict(os.environ)
    environment.update({
        "UNIT_DETACHED_RUN": "1",
        "UNIT_RUN_DIR": record["session_dir"],
        "UNIT_TEAM_ROLE": record["worker"].rsplit("-", 1)[-1],
    })
    log = Path(record["log"])
    marker = log.with_suffix(".scope")
    prefix = scope_prefix(marker, environment)
    exit_code = _run(prefix + record["command"], record["cwd"], environment, log)
    if prefix and not marker.exists():
        exit_code = _run(record["command"], record["cwd"], environment, log)
    marker.unlink(missing_ok=True)
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


def command_reap(args: argparse.Namespace) -> int:
    for run_id in reap_orphans(str(getattr(args, "session_dir", "")), str(getattr(args, "worker", "") or "")):
        print(f"run {run_id} ended: its supervisor is gone")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    verbs = parser.add_subparsers(dest="verb", required=True)
    start = verbs.add_parser("start", help="run a command detached for the calling worker, unless its same run is live")
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
    reap = verbs.add_parser("reap", help="end the running records whose supervisor is gone")
    _ = reap.add_argument("--session-dir", required=True)
    _ = reap.add_argument("--worker", default="")
    reap.set_defaults(handler=command_reap)
    args = parser.parse_args(argv)
    handler = cast("Callable[[argparse.Namespace], int]", getattr(args, "handler"))
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
