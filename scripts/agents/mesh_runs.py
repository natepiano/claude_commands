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
             Otherwise, config/mesh_runs.conf LIVE_RUN_LIMIT limits live runs for that worker.
             Exit 4 refuses a new run at the limit and names the live runs on stderr.
             Wait for a run's result as the next message, or put more files in one run.
             Finished records and records without their supervisor do not hold a place.
             One exclusive .start.lock protects the check, record creation, and supervisor publication.
             Orphan reaping takes the same lock before it reads or finishes records.
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
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import contextlib
import fcntl
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Literal, NotRequired, TypedDict, cast
from uuid import NAMESPACE_URL, uuid5

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.agents import codex_mesh, mesh_run_process, worker_lifecycle
from scripts.buildlog import treekey
from scripts.direct import assign_gate
from scripts.lib import conf, unit_runs
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_failure import failure_detail
from scripts.message import repeat_guard
from scripts.message import send_budget
from scripts.production import staff, session_staff, worker_admission

# Exit status of `start` when the caller is not a mesh worker: run the command attached.
NOT_A_MESH_WORKER = 3
# A worker at its live-run limit must keep its command pending, never run it attached.
LIVE_RUN_LIMIT_REACHED = 4
# Lines of the log the result message carries.
RESULT_TAIL_LINES = 20
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
    staff_id: str
    label: str
    command: list[str]
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


@dataclass(frozen=True)
class PaneCaller:
    """A published worker in a run pinned to herdr."""

    session_dir: str
    name: str
    staff_id: staff.StaffId


Caller = MeshCaller | PaneCaller | NotMeshWorker




def _roster_folders() -> Iterator[Path]:
    """Read unit runs first, before inspecting retained external meshes."""
    for root in RUN_ROOTS:
        for path in sorted(root.glob(f"*/{codex_mesh.ROSTER_FILE}")):
            yield path.parent
    from scripts.astra import design_roles
    yield from design_roles.durable_meshes()


def caller_from_environment(environment: dict[str, str]) -> Caller:
    """Find the caller through its current native session and the run's transport pin."""
    membership = staff.local_snapshot()
    native_member = membership.by_current_session(staff.runtime_id())
    if native_member is not None and native_member.kind == "worker":
        from scripts.herdr import worker_panes
        parent = membership.parent_of(native_member)
        if parent is not None:
            run = worker_panes.worker_run(parent)
            if unit_runs.worker_transport(run) == "herdr":
                return PaneCaller(str(run), native_member.name, native_member.id)
    thread_id = environment.get("CODEX_THREAD_ID") or environment.get("CLAUDE_CODE_SESSION_ID", "")
    if not thread_id:
        return NotMeshWorker()
    member = membership.by_current_session(thread_id)
    if member is None or member.kind != "worker":
        return NotMeshWorker()
    found: list[tuple[bool, MeshCaller]] = []
    for folder in _roster_folders():
        roster = session_staff.read_worker_roster(folder / codex_mesh.ROSTER_FILE)
        for name, value in roster.items():
            entry = codex_mesh._as_dict(value)  # pyright: ignore[reportPrivateUsage]
            reference = entry.get("staff_id")
            if (reference != member.id or entry.get("status") == "ended"):
                continue
            launcher = codex_mesh._pid_alive(codex_mesh._as_int(entry.get("launcher_pid")))  # pyright: ignore[reportPrivateUsage]
            caller = MeshCaller(str(folder), name, thread_id)
            found.append((launcher, caller))
    if not found:
        return NotMeshWorker()
    found.sort(key=lambda item: not item[0])
    return found[0][1]


def _worker_reference(session_dir: str, name: str) -> str:
    if unit_runs.worker_transport(Path(session_dir)) == "herdr":
        member = worker_admission.member(Path(session_dir), name)
        return str(member.id) if member is not None else ""
    roster = worker_lifecycle.read_worker_state(Path(session_dir) / codex_mesh.ROSTER_FILE)
    entry = codex_mesh._as_dict(roster.get(name))  # pyright: ignore[reportPrivateUsage]
    return str(entry["staff_id"])


def _run_worker(record: RunRecord, membership: staff.StaffSnapshot) -> staff.StaffRow | None:
    reference = record.get("staff_id", "")
    member = membership.by_id(staff.StaffId(reference))
    if member is None or member.kind != "worker":
        print(f"anomaly: {record['worker']}: detached run has no worker staff row", file=sys.stderr)
        return None
    return member


def _runs_dir(session_dir: str) -> Path:
    return Path(session_dir) / codex_mesh.DETACHED_RUNS_DIR


def _write_record(path: Path, record: RunRecord) -> None:
    write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))


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
    roster = worker_lifecycle.read_worker_state(Path(session_dir) / codex_mesh.ROSTER_FILE)
    if name in roster:
        return name
    matches = [seat for seat in roster if seat.endswith(f"-{name}")]
    return matches[0] if len(matches) == 1 else name


def _orphaned(path: Path, raw: dict[str, object], now: float) -> bool:
    """A running record that no supervisor runs. A record with no pid yet is still starting for its
    first minute: `start` writes the record before it spawns the supervisor."""
    if raw.get("status") != "running":
        return False
    pid = codex_mesh._as_int(raw.get("pid"))  # pyright: ignore[reportPrivateUsage]
    if pid > 0:
        return not mesh_run_process.supervises(pid, path)
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
    with (runs / ".start.lock").open("a") as admission:
        fcntl.flock(admission, fcntl.LOCK_EX)
        return _reap_orphans_locked(runs, worker)


def _reap_orphans_locked(runs: Path, worker: str = "") -> list[str]:
    """Reap orphan records while the caller holds the runs-folder admission lock."""
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
                        and other.get("staff_id") == record["staff_id"]
                        and codex_mesh._as_float(other.get("started_at"), 0.0) > record["started_at"]  # pyright: ignore[reportPrivateUsage]
                        for _, other in records)
            try:
                delivery = ("nothing delivered: the supervisor is gone and a newer run of the same command answers"
                            if newer else deliver(record, record.get("exit_code")))
            except RunDeliveryFailed as error:
                print(f"mesh_runs: {record['run_id']}: {error}", file=sys.stderr)
                continue
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


def live_twin(runs: Path, worker: str, command: list[str], tree_key: str) -> RunRecord | None:
    """A running record of the same worker, command, folder and tree key whose supervisor lives.
    After an interrupt on 2026-10-21, hana_catalyst-inspectors started its lint run three times
    on one tree; each run took memory, the cargo token and build time."""
    if not tree_key:
        return None
    for path in sorted(runs.glob("*.json")):
        raw = codex_mesh._read_json_object(path)  # pyright: ignore[reportPrivateUsage]
        if (raw.get("status") == "running" and raw.get("worker") == worker and raw.get("command") == command
                and raw.get("tree_key") == tree_key
                and mesh_run_process.supervises(codex_mesh._as_int(raw.get("pid")), path)):  # pyright: ignore[reportPrivateUsage]
            return cast(RunRecord, cast(object, raw))
    return None


def worker_runs(runs: Path, worker: str, *, since: float = 0) -> list[RunRecord]:
    """Read this worker's runs that started during the selected dispatch."""
    found: list[RunRecord] = []
    for path in sorted(runs.glob("*.json")):
        raw = codex_mesh._read_json_object(path)  # pyright: ignore[reportPrivateUsage]
        if raw.get("worker") == worker and codex_mesh._as_float(raw.get("started_at"), 0) >= since:  # pyright: ignore[reportPrivateUsage]
            found.append(cast(RunRecord, cast(object, raw)))
    return found


def delivered_worker_results(runs: Path, worker: str, *, since: float) -> set[str]:
    """Name finished results sent directly to this worker's pane."""
    return {record["run_id"] for record in worker_runs(runs, worker, since=since)
            if record["status"] == "finished" and record.get("delivery", "").startswith(
                (f"sent: worker {worker} at ", "already delivered: "))}


def live_runs(runs: Path, worker: str) -> list[RunRecord]:
    """Return this worker's running records whose supervisors still own them."""
    found: list[RunRecord] = []
    for path in sorted(runs.glob("*.json")):
        raw = codex_mesh._read_json_object(path)  # pyright: ignore[reportPrivateUsage]
        if (raw.get("status") == "running" and raw.get("worker") == worker
                and mesh_run_process.supervises(codex_mesh._as_int(raw.get("pid")), path)):  # pyright: ignore[reportPrivateUsage]
            found.append(cast(RunRecord, cast(object, raw)))
    return found


def command_start(args: argparse.Namespace) -> int:
    command = [str(part) for part in cast(list[object], getattr(args, "command", []) or [])]
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("mesh_runs: start needs a command after --", file=sys.stderr)
        return 2
    session_dir = str(getattr(args, "session_dir", "") or "")
    name = str(getattr(args, "to", "") or "")
    if bool(session_dir) != bool(name):
        print("mesh_runs: --session-dir and --to go together", file=sys.stderr)
        return 2
    reference = ""
    if not session_dir:
        caller = caller_from_environment(dict(os.environ))
        if isinstance(caller, NotMeshWorker):
            return NOT_A_MESH_WORKER
        session_dir, name = caller.session_dir, caller.name
        if isinstance(caller, PaneCaller):
            reference = caller.staff_id
    else:
        name = seat_name(session_dir, name)
    label = str(getattr(args, "label", "") or "") or " ".join(command)
    done_file = str(getattr(args, "done_message_file", "") or "")
    director_dir = str(getattr(args, "director_dir", "") or "") or session_dir
    runs = _runs_dir(session_dir)
    runs.mkdir(parents=True, exist_ok=True)
    cwd = os.getcwd()
    tree_key = treekey.tree_key(cwd) or ""
    with (runs / ".start.lock").open("a") as admission:
        fcntl.flock(admission, fcntl.LOCK_EX)
        _ = _reap_orphans_locked(runs)
        if twin := live_twin(runs, name, command, tree_key):
            started = time.strftime("%H:%M:%S", time.localtime(twin["started_at"]))
            print(f"run {twin['run_id']} is already running for {name} on these files (started {started}, "
                  + f"log {twin['log']}); started nothing; its result arrives as your next message")
            return 0
        try:
            limit = conf.read("mesh_runs").whole("LIVE_RUN_LIMIT")
        except conf.Unusable as error:
            print(f"mesh_runs: {error}", file=sys.stderr)
            return 2
        active = live_runs(runs, name)
        if len(active) >= limit:
            details = "\n".join(f"  {run['run_id']}: {run['label']}" for run in active)
            print(f"mesh_runs: live-run limit {limit} reached for {name}; started nothing.\n"
                  + f"Live runs:\n{details}\n"
                  + "Wait for a run's result; it arrives as your next message. "
                  + "Or put more files in one run.", file=sys.stderr)
            return LIVE_RUN_LIMIT_REACHED
        run_id = f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}-{secrets.token_hex(2)}"
        record_path = runs / f"{run_id}.json"
        log_path = runs / f"{run_id}.log"
        record: RunRecord = {
            "run_id": run_id, "status": "running", "session_dir": session_dir, "worker": name,
            "staff_id": reference or _worker_reference(session_dir, name), "label": label, "command": command,
            "log": str(log_path), "started_at": time.time(), "pid": 0,
            "director_dir": director_dir, "done_message_file": done_file, "tree_key": tree_key,
        }
        _write_record(record_path, record)
        with log_path.open("ab") as log:
            supervisor = subprocess.Popen(
                [str(Path(__file__).resolve().parents[1] / "lib/py"), str(Path(__file__).resolve()), "supervise", str(record_path)],
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
    limit = conf.read("mesh_runs").whole("RESULT_LINE_CHARS")
    if len(line) <= limit:
        return line
    return f"{line[:limit]} [cut: {len(line) - limit} more characters]"


def _tail(path: Path, count: int, budget: int) -> list[str]:
    """Keep the latest lines within the message budget. Collapse repeated memory waits."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    collapsed: list[str] = []
    for line in lines:
        if (collapsed and line.startswith("still waiting for memory")
                and collapsed[-1].startswith("still waiting for memory")):
            collapsed[-1] = line
        else:
            collapsed.append(line)
    kept: list[str] = []
    total = 0
    for line in map(_cut_line, reversed(collapsed[-count:])):
        total += len(line) + 1
        if total > budget:
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
    lines = [f"{heading}: ", f"Result: {verdict}", f"Log: {record['log']}"]
    cancellation = ""
    if exit_code == TREE_CHANGED_STATUS:
        # Keep the cancellation instruction when cleanup moves it outside the tail.
        try:
            with Path(record["log"]).open(encoding="utf-8", errors="replace") as log:
                cancellation = next((line.rstrip("\n") for line in log
                                     if line.startswith("verify.sh: cancelled — ")), "")
        except OSError:
            cancellation = ""
        if cancellation:
            lines.extend(["", cancellation])
    header = "\n".join(lines)
    limits = conf.read("mesh_runs")
    tail_heading = f"\n\nLast log lines (lines over {limits.whole('RESULT_LINE_CHARS')} characters are cut):\n"
    tail = _tail(Path(record["log"]), RESULT_TAIL_LINES,
                 limits.whole("RESULT_TEXT_CHARS") - len(header) - len(tail_heading))
    if cancellation:
        tail = [line for line in tail if line != _cut_line(cancellation)]
    body = tail_heading + "\n".join(tail) if tail else ""
    label_budget = max(0, limits.whole("RESULT_TEXT_CHARS") - len(header) - len(body))
    lines[0] += record["label"][:label_budget]
    header = "\n".join(lines)
    return header + body


@dataclass(frozen=True)
class DirectorNoticeQueued:
    reason: str


def send_to_director(session_id: str, summary: str, text: str) -> Literal["sent", "failed"] | DirectorNoticeQueued:
    """One send.py call to a Claude session; tests replace this."""
    try:
        sent = subprocess.run(
            [sys.executable, str(SEND_SCRIPT), "--to", f"session:{session_id}", "--from", "mesh-runs",
             "--summary", summary, "--text", text],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=send_budget.seconds(), check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"mesh_runs: could not tell session {session_id}: {exc}", file=sys.stderr)
        return "failed"
    detail = failure_detail(sent)
    if sent.returncode == 1:
        return DirectorNoticeQueued(detail.removeprefix("stdout: QUEUED:").strip())
    if sent.returncode:
        print(f"mesh_runs: could not tell session {session_id}: {detail}", file=sys.stderr)
        return "failed"
    return "sent"


def _director_sessions(record: RunRecord) -> list[str]:
    """Resolve the worker's current director from the staff record."""
    membership = staff.local_snapshot()
    member = _run_worker(record, membership)
    if member is None or not isinstance(member.parent, staff.ReportsTo):
        return []
    parent = membership.by_id(member.parent.id)
    return [parent.current_session] if parent is not None else []


class RunDeliveryFailed(RuntimeError):
    """The running record still needs result delivery."""


def _result_received(member: staff.StaffRow, membership: staff.StaffSnapshot, marker: str) -> bool:
    backend = membership.backends.get(member.id, staff.BACKEND_UNAVAILABLE)
    if isinstance(backend, staff.BackendUnavailable):
        raise ValueError("worker transcript backend is unavailable")
    source = worker_admission.transcript_source(member, backend)
    return any(isinstance(entry, str) and entry.startswith("user:\n") and marker in entry.splitlines()
               for entry in worker_admission.transcript_entries(source, set()))


def deliver(record: RunRecord, exit_code: int | None) -> str:
    """Deliver through the run's transport and return the delivery outcome.

    A missing exit code means the supervisor disappeared.
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
    membership = staff.local_snapshot()
    member = _run_worker(record, membership)
    if member is None:
        return "finished: worker retired"
    if unit_runs.worker_transport(Path(session_dir)) == "herdr":
        from scripts.message import send

        marker = f"Detached run: {record['run_id']}"
        runs = _runs_dir(session_dir)
        runs.mkdir(parents=True, exist_ok=True)
        with (runs / f".{record['run_id']}.delivery.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            reason = ""
            try:
                if _result_received(member, membership, marker):
                    return f"already delivered: {record['run_id']}"
            except (OSError, ValueError) as error:
                reason = f"worker transcript is unavailable: {error}"
            timeout = send_budget.seconds()
            result = send.deliver_worker_text(member, membership, marker + "\n" + text,
                                              sender="mesh-runs", timeout=timeout, reason=reason)
            if result.outcome in ("failed", "refused"):
                raise RunDeliveryFailed(f"{result.outcome}: {result.detail}")
            return f"{result.outcome}: {result.detail}"
    thread_id = member.current_session
    roster = worker_lifecycle.read_worker_state(Path(session_dir) / codex_mesh.ROSTER_FILE)
    entry = codex_mesh._as_dict(roster.get(name))  # pyright: ignore[reportPrivateUsage]
    from scripts.astra import design_roles
    design_id, role = entry.get("held_design_id"), entry.get("held_role")
    if isinstance(design_id, str) and design_id and role in ("designer", "reviewer"):
        receiver = repeat_guard.recipient_identity("", thread_id=thread_id)
        decision = repeat_guard.transport_check("mesh-runs", receiver, text)
        if decision.held:
            return f"held repeated run result: copy {decision.copies}"
        message_id = str(uuid5(NAMESPACE_URL, str(Path(session_dir).resolve()) + "/" + record["run_id"]))
        _ = design_roles.queue_message(design_id, role, "mesh_runs:" + record["run_id"],
                                text, message_id=message_id)
        repeat_guard.transport_result("mesh-runs", receiver, text, "queued")
        return f"queued for held role {name}; its holder controls the next assignment"
    durable = Path(session_dir).resolve() in {path.resolve() for path in design_roles.durable_meshes()}
    held = codex_mesh.hold_message(session_dir, name, text, without_launcher=durable, sender="mesh-runs")
    match held:
        case codex_mesh.RepeatHeld(copies):
            return f"held repeated run result: copy {copies}"
        case codex_mesh.HeldForLauncher(thread_id):
            return f"held for {name}'s attached launcher on thread {thread_id}"
        case codex_mesh.HeldForNextDispatch(thread_id):
            return f"held for {name}'s next dispatch on thread {thread_id}"
        case codex_mesh.WorkerEnded(reason):
            state = reason
        case codex_mesh.NoLauncherAttached(status=status):
            state = f"{name} is {status or 'unknown'}"
    assignment = codex_mesh.task_role(thread_id, codex_mesh.task_assignments(),
                                     default_role=str(entry.get("role", "")))
    task_role = assignment.role if isinstance(assignment, codex_mesh.AssignedWorkerRole) else "<role>"
    assigned_role = assign_gate.assign_role(task_role)
    note = (f"Worker {name}'s run {record['label']} finished ({_exit_words(exit_code)}); no launcher is attached "
            + f"({state}), so the result comes to you. Dispatch the worker (assign.sh --role {assigned_role} --to {name}) "
            + "when it should act on it.")
    message = "\n".join([note, f"Worker: {name}", f"Command: {' '.join(record['command'])}", "", text])
    for session in _director_sessions(record):
        delivery = send_to_director(session, f"{name}: run {record['label']} finished ({_exit_words(exit_code)})", message)
        if isinstance(delivery, DirectorNoticeQueued):
            return f"queued for session {session}: {delivery.reason}"
        if delivery == "sent":
            return f"sent to director {session}"
    if isinstance(held, codex_mesh.WorkerEnded):
        return f"nothing delivered: {held.reason} and no director was found"
    # No director to tell: hold the result so the worker's next dispatch still reads it.
    kept = codex_mesh.hold_message(session_dir, name, text, without_launcher=True, sender="mesh-runs")
    if isinstance(kept, codex_mesh.RepeatHeld):
        return f"held repeated run result: copy {kept.copies}"
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


def _run(command: list[str], environment: dict[str, str], log: Path) -> int:
    """Run the command with its output appended to the log, forward signals to it, and return its status."""
    try:
        with log.open("ab") as output:
            child = subprocess.Popen(command, env=environment,
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
    with (record_path.parent / ".start.lock").open("a") as admission:
        fcntl.flock(admission, fcntl.LOCK_EX)
        record = _read_record(record_path)
        if record["pid"] == 0:
            record["pid"] = os.getpid()
            _write_record(record_path, record)
    environment = dict(os.environ)
    environment.update({
        "UNIT_DETACHED_RUN": "1",
        "UNIT_RUN_DIR": record["session_dir"],
        "UNIT_TEAM_ROLE": record["worker"].rsplit("-", 1)[-1],
    })
    log = Path(record["log"])
    marker = log.with_suffix(".scope")
    prefix = scope_prefix(marker, environment)
    exit_code = _run(prefix + record["command"], environment, log)
    if prefix and not marker.exists():
        exit_code = _run(record["command"], environment, log)
    marker.unlink(missing_ok=True)
    # Deliver before marking finished: a launcher that reads the run as finished closes its inbox
    # unless the result is already held.
    record["exit_code"] = exit_code
    _write_record(record_path, record)
    try:
        delivery = deliver(record, exit_code)
    except RunDeliveryFailed as error:
        print(f"mesh_runs: {record['run_id']}: {error}", file=sys.stderr)
        return 1
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
