#!/usr/bin/env python3
"""Run a worker command detached and deliver its result through the native worker route.

Start writes a run record and returns after it starts the supervisor.
The supervisor runs the command and delivers its result to the current worker.
Reap finishes records whose supervisor disappeared and delivers their results.
Stop a run through its supervisor's process group, including scoped commands.
Exit 3 means that no registered worker owns the caller.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable, Generator

import argparse
import contextlib
import fcntl
import json
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

from scripts.agents import detached_run_process, worker_lifecycle
from scripts.buildlog import treekey
from scripts.lib import conf, process_start, unit_runs
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.cancellation import CANCELLATION_SIGNALS
from scripts.message import send_budget
from scripts.production import staff, worker_admission

# Exit status of `start` when the caller is not a registered worker: run the command attached.
NOT_A_DETACHED_WORKER = 3
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
from scripts.agents.worker_lifecycle import DETACHED_RUNS_DIR as DETACHED_RUNS_DIR, DETACHED_RUN_START_GRACE_SECS
# On natedev the command runs in builds-detached.slice, a child of builds.slice.
# The parent slice's memory limit and CPU weight also apply to the detached command.
# The scope writes the marker before the command starts.
# A missing marker after the scope exits proves that the command did not start there.
# The command then runs without a scope. BUILDLOG_SCOPE=0 disables the scope, as it does for invoke.sh.
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
    supervisor_started: NotRequired[str]
    command_pid: NotRequired[int]
    command_started: NotRequired[str]
    director_dir: str
    done_message_file: str
    # The tree key of cwd at start, "" when unknown; records before 2026-10-21 have none.
    tree_key: NotRequired[str]
    exit_code: NotRequired[int]
    finished_at: NotRequired[str]
    delivery: NotRequired[str]
    # Set when the run ended without its supervisor: "supervisor gone".
    completion_pending: NotRequired[bool]
    ended: NotRequired[str]
    stop_reason: NotRequired[str]


@dataclass(frozen=True)
class NotDetachedWorker:
    """The caller is not a registered worker, so its command runs attached."""


@dataclass(frozen=True)
class PaneCaller:
    """A worker with a published staff parent."""

    session_dir: str
    name: str
    staff_id: staff.StaffId


Caller = PaneCaller | NotDetachedWorker


def caller_from_environment(environment: dict[str, str]) -> Caller:
    """Find the worker through its current native session and staff parent."""
    membership = staff.local_snapshot()
    member = membership.by_current_session(staff.runtime_id())
    if member is None or member.kind != "worker":
        identity = environment.get("CODEX_THREAD_ID") or environment.get("CLAUDE_CODE_SESSION_ID", "")
        member = membership.by_current_session(identity)
    if member is not None and member.kind == "worker":
        from scripts.herdr import worker_panes

        parent = membership.parent_of(member)
        if parent is not None:
            return PaneCaller(str(worker_panes.worker_run(member, record=membership)), member.name, member.id)
    return NotDetachedWorker()


def _worker_reference(session_dir: str, name: str) -> str:
    member = worker_admission.member(Path(session_dir), name)
    return str(member.id) if member is not None else ""


@dataclass(frozen=True)
class RunWorkerUnavailable:
    reason: str


def _run_worker(record: RunRecord, membership: staff.StaffSnapshot) -> staff.StaffRow | RunWorkerUnavailable:
    member = membership.by_id(staff.StaffId(record.get("staff_id", "")))
    if member is None or member.kind != "worker":
        reason = f"{record['worker']}: detached run has no worker staff row"
        return RunWorkerUnavailable(reason)
    return member


def _runs_dir(session_dir: str) -> Path:
    return Path(session_dir) / DETACHED_RUNS_DIR


@contextlib.contextmanager
def record_lock(path: Path) -> Generator[None]:
    """Serialize supervisor updates and drain stop reasons for one record."""
    with path.with_suffix(".record.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _write_record(path: Path, record: RunRecord) -> RunRecord:
    with record_lock(path):
        current = worker_lifecycle.read_json_object(path)
        fields: dict[str, object] = {**current, **record}
        if current.get("status") == "finished" and record["status"] == "running":
            fields.update(current)
        if current:
            if "stop_reason" in current:
                fields["stop_reason"] = current["stop_reason"]
            else:
                _ = fields.pop("stop_reason", "")
        write_text_atomic(path, json.dumps(fields, indent=2, sort_keys=True))
        return cast(RunRecord, cast(object, fields))


def _read_record(path: Path) -> RunRecord:
    raw = worker_lifecycle.read_json_object(path)
    if not raw.get("run_id") or not isinstance(raw.get("command"), list):
        raise SystemExit(f"detached_runs: {path} is not a run record")
    return cast(RunRecord, cast(object, raw))


def stop_with_reason(
    path: Path, reason: str, *, still_current: Callable[[], bool], signal_run: Callable[[], None],
) -> bool:
    """Save the reason and complete the stop decision under one record lock."""
    with record_lock(path):
        record = worker_lifecycle.read_json_object(path)
        record["stop_reason"] = reason
        write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))
        sent = False
        try:
            if still_current():
                signal_run()
                sent = True
            return sent
        finally:
            if not sent:
                del record["stop_reason"]
                write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))


def seat_name(session_dir: str, name: str) -> str:
    """Resolve the current staff worker through its active recorder slot."""
    run = Path(session_dir)
    membership = staff.local_snapshot()
    owner = worker_admission.parent(membership, run=run)
    state = worker_lifecycle.read_json_object(run / unit_runs.STATE_FILE)
    passes = state.get("pass")
    current = cast(dict[str, object], passes).get(name) if isinstance(passes, dict) else None
    member = None
    if isinstance(current, dict):
        dispatch = cast(dict[str, object], current)
        if dispatch.get("status") == "active":
            name = str(dispatch.get("worker_name", "")) or name
            member = membership.by_id(staff.StaffId(str(dispatch.get("worker_id", ""))))
    if member is None:
        member = worker_admission.find_worker(membership, owner.id, name)
    return member.name if member is not None and member.kind == "worker" and member.reports_to(owner.id) else name


def _integer(value: object) -> int:
    return value if type(value) is int else 0


def _number(value: object, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _group_is_live(group: int) -> bool:
    """Keep the run pending while its group has live processes or cannot be read."""
    try:
        result = subprocess.run(["ps", "-A", "-o", "pgid=", "-o", "stat="],
                                capture_output=True, text=True, check=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return True
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[0].isdigit():
            return True
        if int(fields[0]) == group and not fields[1].startswith("Z"):
            return True
    return False


def _saved_process(pid: object, started: object) -> process_start.ProcessStart:
    """Read the saved process identity and reject a reused PID."""
    if not isinstance(started, str) or not started or _integer(pid) <= 0:
        return process_start.Gone()
    observed = process_start.process_start(_integer(pid))
    if isinstance(observed, (process_start.Started, process_start.Zombie)) and observed.start != started:
        return process_start.Gone()
    return observed


def _orphaned(path: Path, raw: dict[str, object], now: float) -> bool:
    """Recover a running record after its command and owned group processes end."""
    if raw.get("status") != "running":
        return False
    pid = _integer(raw.get("pid"))
    if pid > 0:
        if detached_run_process.supervises(pid, path):
            return False
        command = _saved_process(raw.get("command_pid"), raw.get("command_started"))
        if isinstance(command, (process_start.Started, process_start.Unreadable)):
            return False
        supervisor = _saved_process(pid, raw.get("supervisor_started"))
        owns_group = isinstance(supervisor, (process_start.Started, process_start.Zombie))
        if not owns_group and isinstance(command, process_start.Zombie):
            try:
                owns_group = os.getpgid(_integer(raw.get("command_pid"))) == pid
            except ProcessLookupError:
                pass
            except OSError:
                return False
        return not owns_group or not _group_is_live(pid)
    started = _number(raw.get("started_at"), now)
    return now - started >= DETACHED_RUN_START_GRACE_SECS


def reap_orphans(session_dir: str, worker: str = "") -> list[str]:
    """Recover results after the command and owned group processes end.

    Deliver the saved command status when available.
    A claim file prevents duplicate delivery.
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
               if (raw := worker_lifecycle.read_json_object(path)).get("run_id")]
    reaped: list[str] = []
    for path, raw in records:
        if worker and raw.get("worker") != worker:
            continue
        if raw.get("status") == "finished" and raw.get("completion_pending"):
            if not detached_run_process.supervises(_integer(raw.get("pid")), path):
                _finish_record(path, _read_record(path))
            continue
        if not _orphaned(path, raw, now):
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
                        and _number(other.get("started_at"), 0.0) > record["started_at"]
                        for _, other in records)
            try:
                delivery = ("nothing delivered: the supervisor is gone and a newer run of the same command answers"
                            if newer else deliver(record, command_result(record)))
            except RunDeliveryFailed as error:
                print(f"detached_runs: {record['run_id']}: {error}", file=sys.stderr)
                continue
            record = _read_record(path)
            record.update({
                "status": "finished", "ended": "supervisor gone", "delivery": delivery,
                "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            _finish_record(path, record)
            reaped.append(record["run_id"])
        finally:
            claim.unlink(missing_ok=True)
    return reaped


def _finish_record(path: Path, record: RunRecord) -> None:
    """Keep a failed completion event available without blocking other records."""
    record["completion_pending"] = True
    record = _write_record(path, record)
    try:
        _complete_worker_dispatch(record)
    except Exception as error:
        print(f"anomaly: {record['run_id']}: completion event failed: {error}", file=sys.stderr)
    else:
        record["completion_pending"] = False
        _ = _write_record(path, record)


def _complete_worker_dispatch(record: RunRecord) -> None:
    """Recheck the pass after the finished record becomes visible."""
    member = _run_worker(record, staff.local_snapshot())
    if isinstance(member, staff.StaffRow):
        complete = cast(Callable[[str], None], import_module("scripts.herdr.turn_end").run_finished)
        complete(str(member.current_session))


def live_twin(runs: Path, worker: str, command: list[str], tree_key: str) -> RunRecord | None:
    """A running record of the same worker, command, folder and tree key whose supervisor lives.
    After an interrupt on 2026-10-21, hana_catalyst-inspectors started its lint run three times
    on one tree; each run took memory, the cargo token and build time."""
    if not tree_key:
        return None
    for path in sorted(runs.glob("*.json")):
        raw = worker_lifecycle.read_json_object(path)
        if (raw.get("status") == "running" and raw.get("worker") == worker and raw.get("command") == command
                and raw.get("tree_key") == tree_key
                and detached_run_process.supervises(_integer(raw.get("pid")), path)):
            return cast(RunRecord, cast(object, raw))
    return None


def worker_runs(runs: Path, worker: str, *, since: float = 0) -> list[RunRecord]:
    """Read this worker's runs that started during the selected dispatch."""
    found: list[RunRecord] = []
    for path in sorted(runs.glob("*.json")):
        raw = worker_lifecycle.read_json_object(path)
        if raw.get("worker") == worker and _number(raw.get("started_at"), 0) >= since:
            found.append(cast(RunRecord, cast(object, raw)))
    return found


def delivered_worker_results(runs: Path, worker: str, *, since: float) -> set[str]:
    """Name finished results sent directly to this worker's pane."""
    return {record["run_id"] for record in worker_runs(runs, worker, since=since)
            if record["status"] == "finished" and record.get("delivery", "").startswith(
                (f"sent: worker {worker} at ", "already delivered: "))}


def live_runs(runs: Path, worker: str) -> list[RunRecord]:
    """Return running records whose supervisor still owns the record."""
    return _live_runs(runs, lambda raw: raw.get("worker") == worker)


def live_runs_for_staff(runs: Path, identities: frozenset[staff.StaffId]) -> list[RunRecord]:
    """Read live records for fixed staff identities, including renamed members."""
    return _live_runs(runs, lambda raw: isinstance(identity := raw.get("staff_id"), str) and identity in identities)


def _live_runs(runs: Path, owns: Callable[[dict[str, object]], bool]) -> list[RunRecord]:
    """Read each record once and check its owner and supervisor."""
    found: list[RunRecord] = []
    for path in sorted(runs.glob("*.json")):
        raw = worker_lifecycle.read_json_object(path)
        if (raw.get("status") == "running" and owns(raw)
                and detached_run_process.supervises(_integer(raw.get("pid")), path)):
            found.append(cast(RunRecord, cast(object, raw)))
    return found


def resume_context(runs: Path, worker: str) -> str:
    """Name live runs and their persistent result files for a resumed worker."""
    lines: list[str] = []
    for record in live_runs(runs, worker):
        result = runs / f"{record['run_id']}.json"
        command = shlex.join(["cat", "--", str(result), record["log"]])
        lines.append(f"Detached run {record['run_id']} remains live. "
                     + f"Result: {result}; log: {record['log']}. Read it with: `{command}`. "
                     + "Its result follows this worker after the move.")
    return "\n".join(lines)


def command_start(args: argparse.Namespace) -> int:
    command = [str(part) for part in cast(list[object], getattr(args, "command", []) or [])]
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("detached_runs: start needs a command after --", file=sys.stderr)
        return 2
    session_dir = str(getattr(args, "session_dir", "") or "")
    name = str(getattr(args, "to", "") or "")
    caller = caller_from_environment(dict(os.environ))
    reference = ""
    if isinstance(caller, PaneCaller):
        if name and name != caller.name:
            print(f"detached_runs: --to {name} ignored; run belongs to worker {caller.name}", file=sys.stderr)
        session_dir, name = caller.session_dir, caller.name
        reference = caller.staff_id
    else:
        if bool(session_dir) != bool(name):
            print("detached_runs: --session-dir and --to go together", file=sys.stderr)
            return 2
        if not session_dir:
            return NOT_A_DETACHED_WORKER
        name = seat_name(session_dir, name)
    session_dir = str(Path(session_dir).resolve())
    label = str(getattr(args, "label", "") or "") or " ".join(command)
    done_file = str(getattr(args, "done_message_file", "") or "")
    director_dir = str(getattr(args, "director_dir", "") or "") or session_dir
    runs = _runs_dir(session_dir).resolve()
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
            limit = conf.read("detached_runs").whole("LIVE_RUN_LIMIT")
        except conf.Unusable as error:
            print(f"detached_runs: {error}", file=sys.stderr)
            return 2
        active = live_runs(runs, name)
        if len(active) >= limit:
            details = "\n".join(f"  {run['run_id']}: {run['label']}" for run in active)
            print(f"detached_runs: live-run limit {limit} reached for {name}; started nothing.\n"
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
        _ = _write_record(record_path, record)
        with log_path.open("ab") as log:
            supervisor = subprocess.Popen(
                [str(Path(__file__).resolve().parents[1] / "lib/py"), str(Path(__file__).resolve()), "supervise", str(record_path)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, cwd=cwd,
            )
        record["pid"] = supervisor.pid
        _ = _write_record(record_path, record)
    print(f"run {run_id} started detached for {name} (pid {supervisor.pid}, log {log_path}); "
          + "its result arrives as your next message")
    return 0


def _exit_status(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def _cut_line(line: str) -> str:
    limit = conf.read("detached_runs").whole("RESULT_LINE_CHARS")
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


@dataclass(frozen=True)
class CompletedCommand:
    code: int


@dataclass(frozen=True)
class SupervisorLost:
    """The supervisor disappeared before it reported an exit status."""


CommandResult = CompletedCommand | SupervisorLost


def command_result(record: RunRecord) -> CommandResult:
    code = record.get("exit_code")
    return CompletedCommand(code) if isinstance(code, int) else SupervisorLost()


def _exit_words(result: CommandResult) -> str:
    return f"exit {result.code}" if isinstance(result, CompletedCommand) else "no exit status"


def result_text(record: RunRecord, result: CommandResult) -> str:
    exit_code = result.code if isinstance(result, CompletedCommand) else result
    verdict = (ORPHAN_VERDICT if isinstance(exit_code, SupervisorLost) else "PASS (exit 0)" if exit_code == 0
               else SUPERSEDED_VERDICT if exit_code == SUPERSEDED_STATUS
               else TREE_CHANGED_VERDICT if exit_code == TREE_CHANGED_STATUS else f"FAIL (exit {exit_code})")
    heading = "Run ended with no result" if isinstance(exit_code, SupervisorLost) else "Run finished"
    lines = [f"{heading}: ", f"Result: {verdict}", f"Log: {record['log']}"]
    stop_reason = record.get("stop_reason", "")
    if stop_reason:
        lines.append(f"Stop reason: {stop_reason}")
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
    limits = conf.read("detached_runs")
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


class RunDeliveryFailed(RuntimeError):
    """The running record still needs result delivery."""


def _result_received(member: staff.StaffRow, membership: staff.StaffSnapshot, marker: str) -> bool:
    backend = membership.backends.get(member.id, staff.BACKEND_UNAVAILABLE)
    if isinstance(backend, staff.BackendUnavailable):
        raise ValueError("worker transcript backend is unavailable")
    source = worker_admission.transcript_source(member, backend)
    return any(isinstance(entry, str) and entry.startswith("user:\n") and marker in entry.splitlines()
               for entry in worker_admission.transcript_entries(source, set()))


def deliver(record: RunRecord, result: CommandResult) -> str:
    """Deliver the command result to its current worker."""
    exit_code = result.code if isinstance(result, CompletedCommand) else result
    done_file = record["done_message_file"]
    if done_file and exit_code not in (0, SUPERSEDED_STATUS, TREE_CHANGED_STATUS) and not record.get("stop_reason"):
        return f"nothing delivered: {_exit_words(result)} with a done message"
    if done_file and exit_code == 0 and not record.get("stop_reason"):
        try:
            text = Path(done_file).read_text(encoding="utf-8")
        except OSError as exc:
            text = f"Run finished: {record['label']}\nResult: PASS (exit 0)\n(done message {done_file} unreadable: {exc})"
    else:
        text = result_text(record, result)
    session_dir = record["session_dir"]
    membership = staff.local_snapshot()
    member = _run_worker(record, membership)
    if isinstance(member, RunWorkerUnavailable):
        print(f"anomaly: {member.reason}", file=sys.stderr)
        return "finished: worker retired"
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
        delivery = send.deliver_worker_text(member, membership, marker + "\n" + text,
                                          sender="detached-runs", timeout=timeout, reason=reason)
        if delivery.outcome in ("failed", "refused"):
            raise RunDeliveryFailed(f"{delivery.outcome}: {delivery.detail}")
        return f"{delivery.outcome}: {delivery.detail}"


def command_deliver(args: argparse.Namespace) -> int:
    record_path = Path(str(getattr(args, "record", "")))
    record = _read_record(record_path)
    try:
        delivery = deliver(record, command_result(record))
    except RunDeliveryFailed as error:
        print(f"detached_runs: {record['run_id']}: {error}", file=sys.stderr)
        return 1
    record = _read_record(record_path)
    record["delivery"] = delivery
    _ = _write_record(record_path, record)
    return 0


def deliver_in_fresh_process(record_path: Path) -> int:
    """Import delivery code after the recorded command ends."""
    script = Path(__file__).resolve()
    try:
        return subprocess.run(
            [str(script.parents[1] / "lib/py"), str(script), "deliver", str(record_path)],
            stdin=subprocess.DEVNULL, check=False,
        ).returncode
    except OSError as error:
        print(f"detached_runs: delivery process did not start: {error}", file=sys.stderr)
        return 1


def scope_prefix(marker: Path, environment: dict[str, str]) -> list[str]:
    """Start the command in builds-detached.slice when a scope is available."""
    runtime = environment.get("XDG_RUNTIME_DIR", "")
    if (environment.get("BUILDLOG_SCOPE", "1") == "0" or not runtime
            or not Path(runtime, "systemd", "private").is_socket() or shutil.which("systemd-run") is None):
        return []
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--expand-environment=no",
            "--slice=builds-detached.slice", "--", "/bin/sh", "-c", SCOPE_SCRIPT, str(marker)]


def _run(command: list[str], environment: dict[str, str], log: Path) -> int:
    """Publish the child identity before the command runs in the supervisor's group."""
    # The shell keeps its PID across exec and waits for identity publication.
    gate = 'read -r ready || exit 125; exec 0</dev/null; exec "$@"'
    try:
        with log.open("ab") as output:
            child = subprocess.Popen(["/bin/sh", "-c", gate, "detached-command", *command],
                                     env=environment, process_group=os.getpgrp(),
                                     stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT)
    except OSError as exc:
        with log.open("a", encoding="utf-8") as output:
            _ = output.write(f"detached_runs: command did not start: {exc}\n")
        return 127

    assert child.stdin is not None
    with child.stdin:
        started = process_start.process_start(child.pid)
        if not isinstance(started, process_start.Started):
            with log.open("a", encoding="utf-8") as output:
                _ = output.write("detached_runs: command identity is unavailable; command did not start\n")
        else:
            path = Path(environment["UNIT_DETACHED_RECORD"])
            with record_lock(path):
                record = _read_record(path)
                record.update(command_pid=child.pid, command_started=started.start)
                supervisor = process_start.process_start(os.getpid())
                if isinstance(supervisor, process_start.Started):
                    record["supervisor_started"] = supervisor.start
                write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))
            _ = child.stdin.write(b"start\n")
    return _exit_status(child.wait())


def command_supervise(args: argparse.Namespace) -> int:
    record_path = Path(str(getattr(args, "record", "")))
    with (record_path.parent / ".start.lock").open("a") as admission:
        fcntl.flock(admission, fcntl.LOCK_EX)
        record = _read_record(record_path)
        if record["pid"] == 0:
            record["pid"] = os.getpid()
            record = _write_record(record_path, record)
    environment = dict(os.environ)
    from scripts.lib.python_startup.sitecustomize import inherited_paths

    paths = inherited_paths(environment.get("PYTHONPATH", ""))
    if paths:
        environment["PYTHONPATH"] = os.pathsep.join(paths)
    else:
        _ = environment.pop("PYTHONPATH", None)
    environment.update({
        "UNIT_DETACHED_RUN": "1",
        "UNIT_RUN_DIR": record["session_dir"],
        "UNIT_TEAM_ROLE": record["worker"].rsplit("-", 1)[-1],
        "UNIT_DETACHED_RECORD": str(record_path.resolve()),
        "BUILDLOG_OWNER_STAFF_ID": record["staff_id"],
    })
    log = Path(record["log"])
    marker = log.with_suffix(".scope")
    prefix = scope_prefix(marker, environment)
    exit_code = _run(prefix + record["command"], environment, log)
    record = _read_record(record_path)
    if prefix and not marker.exists() and not record.get("stop_reason"):
        exit_code = _run(record["command"], environment, log)
        record = _read_record(record_path)
    marker.unlink(missing_ok=True)
    # Deliver before marking finished: a launcher that reads the run as finished closes its inbox
    # unless the result is already held.
    record["exit_code"] = exit_code
    record = _write_record(record_path, record)
    if deliver_in_fresh_process(record_path) != 0:
        return 1
    record = _read_record(record_path)
    record.update({
        "status": "finished", "exit_code": exit_code,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    _finish_record(record_path, record)
    return 0


def command_record_exit(args: argparse.Namespace) -> int:
    """Save the command status before its orphan record is completed."""
    path = Path(str(getattr(args, "record", "")))
    caller = os.getppid()
    with record_lock(path):
        record = _read_record(path)
        if record["status"] != "running" or record.get("command_pid") != caller:
            return 0
        started = _saved_process(caller, record.get("command_started"))
        if not isinstance(started, process_start.Started):
            return 0
        record["exit_code"] = int(getattr(args, "exit_code", 0))
        write_text_atomic(path, json.dumps(record, indent=2, sort_keys=True))
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
    record_exit = verbs.add_parser("record-exit", help="save a command status without completing its run")
    _ = record_exit.add_argument("record")
    _ = record_exit.add_argument("exit_code", type=int, choices=range(256))
    record_exit.set_defaults(handler=command_record_exit)
    delivery = verbs.add_parser("deliver", help="deliver one recorded result from the current code tree")
    _ = delivery.add_argument("record")
    delivery.set_defaults(handler=command_deliver)
    reap = verbs.add_parser("reap", help="end the running records whose supervisor is gone")
    _ = reap.add_argument("--session-dir", required=True)
    _ = reap.add_argument("--worker", default="")
    reap.set_defaults(handler=command_reap)
    args = parser.parse_args(argv)
    handler = cast("Callable[[argparse.Namespace], int]", getattr(args, "handler"))
    return handler(args)


if __name__ == "__main__":
    if sys.argv[1:2] == ["supervise"]:
        for number in CANCELLATION_SIGNALS:
            _ = signal.signal(number, signal.SIG_DFL)
    sys.exit(main())
