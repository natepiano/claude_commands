"""Repair known reasons an eligible verify call waits while the Mac is idle."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from ..lib import process_start
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.lib import process_start

import store

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS.parent))

if TYPE_CHECKING:
    from ..direct import step_record
else:
    from scripts.direct import step_record

IDLE_SECONDS = 180
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


@dataclass(frozen=True)
class MacIdlePaths:
    state: Path
    mac_state: Path
    session_root: Path
    admission: Path
    proc: Path = Path("/proc")
    sessions: Path = Path.home() / ".claude/sessions"


def watch_paths() -> MacIdlePaths:
    return MacIdlePaths(
        Path(os.environ.get("MAC_IDLE_STATE_DIR", str(store.root().parent / "mac-idle"))).expanduser(),
        Path(os.environ.get("MAC_TEST_STATE_DIR", "~/.local/state/mac-test")).expanduser(),
        Path(os.environ.get("SESSION_TEMP_ROOT", "/tmp/claude/session")),
        store.root() / "admission",
        proc=Path(os.environ.get("VERIFY_PROC_ROOT", "/proc")),
        sessions=Path(os.environ.get("NOTIFIER_SESSIONS_DIR", str(Path.home() / ".claude/sessions"))),
    )


@dataclass(frozen=True)
class LiveProcess:
    pid: int
    parent: int
    start: str
    argv: tuple[str, ...]
    environment: dict[str, str]
    cwd: str
    proc_root: Path = process_start.PROC_ROOT


@dataclass(frozen=True)
class VerifyCall:
    call_id: str
    pid: int
    start: str
    repo: str
    package: str
    session: str
    temp_dir: str
    hold_waiter: str
    token_dir: str
    settings: str
    retry_after: float
    marker_hook: bool
    local_reason: str = ""
    kind: str = "test"
    wait_state: step_record.StepState = step_record.StepState.WAITING_MAC


def read_record(path: Path) -> dict[str, object]:
    try:
        value = cast(object, json.loads(path.read_text()))
        return cast(dict[str, object], value) if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_record(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    _ = temporary.write_text(json.dumps(value) + "\n")
    os.replace(temporary, path)


def call_path(paths: MacIdlePaths, call_id: str) -> Path:
    return paths.state / "calls" / (hashlib.sha256(call_id.encode()).hexdigest() + ".json")


def marker_path(paths: MacIdlePaths, call_id: str) -> Path:
    return call_path(paths, call_id).with_suffix(".retry")


def read_process(entry: Path) -> LiveProcess:
    stat = process_start.read_stat(int(entry.name), entry.parent)
    if isinstance(stat, process_start.ProcessGone) or stat.state in ("Z", "X", "x"):
        raise ValueError("process has ended")
    argv = tuple(part.decode(errors="replace") for part in (entry / "cmdline").read_bytes().split(b"\0") if part)
    environment = dict(part.decode(errors="replace").split("=", 1)
                       for part in (entry / "environ").read_bytes().split(b"\0") if b"=" in part)
    return LiveProcess(int(entry.name), stat.ppid, stat.start.text, argv, environment, str((entry / "cwd").readlink()), entry.parent)


def processes(proc: Path) -> dict[int, LiveProcess]:
    found: dict[int, LiveProcess] = {}
    for entry in proc.iterdir():
        if entry.name.isdecimal():
            try:
                process = read_process(entry)
                found[process.pid] = process
            except (OSError, ValueError, IndexError):
                continue
    return found


def belongs_to(process: LiveProcess, call: VerifyCall, live: dict[int, LiveProcess]) -> bool:
    seen: set[int] = set()
    while process.pid not in seen:
        if process.pid == call.pid:
            return process_start.check_owner(process.pid, process_start.parse_start(call.start), process.proc_root) is process_start.OwnerCheck.ALIVE
        seen.add(process.pid)
        if process.parent not in live:
            return False
        process = live[process.parent]
    return False


def waiting_evidence(call: VerifyCall, paths: MacIdlePaths, live: dict[int, LiveProcess]) -> str:
    """Read wait records and match their process lifetimes to the call."""
    children = {pid: child for pid, child in live.items() if belongs_to(child, call, live)}
    for path in paths.admission.glob("*.waiting"):
        row = read_record(path)
        pid = row.get("pid")
        if (isinstance(pid, int) and pid in children and isinstance(row.get("start_time"), str) and bool(row.get("start_time"))
                and process_start.check_owner(pid, process_start.parse_start(str(row["start_time"])), paths.proc) is process_start.OwnerCheck.ALIVE):
            return "memory"
    if call.token_dir:
        if any(Path(call.token_dir).glob(f"*.{call.pid}")) or any(Path(call.token_dir).glob(f"*.{call.pid}.again")):
            return "cargo_token"
    if call.hold_waiter:
        row = read_record(Path(call.hold_waiter))
        pid = row.get("pid")
        if (row.get("call_id") == call.call_id and isinstance(pid, int) and pid in children
                and isinstance(row.get("proc_start"), str) and bool(row.get("proc_start"))
                and process_start.check_owner(pid, process_start.parse_start(str(row["proc_start"])), paths.proc) is process_start.OwnerCheck.ALIVE):
            return "build_hold"
    for child in children.values():
        if (child.environment.get("BUILDLOG_CALL_ID") == call.call_id
                and "wait-local" in child.argv and any(Path(word).name == "build_targets.py" for word in child.argv)):
            return "build_hold"
        if (call.call_id in child.argv and any(Path(word).name == "local_release_wait.py" for word in child.argv)):
            return "routing"
    return call.wait_state.value


def registered_call(row: dict[str, object]) -> VerifyCall:
    """Validate the stored call at the JSON boundary."""
    text_fields = ("call_id", "start", "repo", "package", "session", "temp_dir", "hold_waiter", "token_dir", "settings")
    if not all(isinstance(row.get(name), str) for name in text_fields):
        raise ValueError("invalid Mac wait call fields")
    pid, retry_after = row.get("pid"), row.get("retry_after")
    if not isinstance(pid, int) or pid <= 0 or not isinstance(retry_after, (float, int)) or not math.isfinite(retry_after):
        raise ValueError("invalid Mac wait call identity or deadline")
    return VerifyCall(cast(str, row["call_id"]), pid, cast(str, row["start"]), cast(str, row["repo"]),
                       cast(str, row["package"]), cast(str, row["session"]), cast(str, row["temp_dir"]),
                       cast(str, row["hold_waiter"]), cast(str, row["token_dir"]), cast(str, row["settings"]),
                       float(retry_after), row.get("marker_hook") is True, str(row.get("local_reason", "")))


def waiting_calls(paths: MacIdlePaths, live: dict[int, LiveProcess]) -> tuple[VerifyCall, ...]:
    calls: list[VerifyCall] = []
    for path in sorted(step_record.record_directory().glob("*.json")):
        record = step_record.read(path.stem)
        if isinstance(record, step_record.StaleRecord):
            print(f"Mac idle watch: ignored stale step record {record.call_id}", file=sys.stderr)
            continue
        if isinstance(record, step_record.UnreadableRecord):
            print(f"Mac idle watch: ignored unreadable step record {record.call_id}: {record.reason}", file=sys.stderr)
            continue
        if not isinstance(record, step_record.StepRecord):
            continue
        if (not isinstance(record.runs_on, step_record.MacAllowed)
                or record.state not in (step_record.StepState.WAITING_HOLD, step_record.StepState.WAITING_MAC)):
            continue
        process = live.get(record.pid)
        if process is None or process_start.check_owner(record.pid, process_start.parse_start(record.proc_start), paths.proc) is not process_start.OwnerCheck.ALIVE:
            continue
        try:
            call = registered_call(read_record(call_path(paths, record.call_id)))
        except ValueError:
            call = VerifyCall(record.call_id, record.pid, record.proc_start, str(record.repo), record.package or "",
                "", "", "", "", "", 0, False)
        if call.pid != record.pid or call.start != record.proc_start:
            call = replace(call, pid=record.pid, start=record.proc_start, marker_hook=False,
                           session="", temp_dir="", hold_waiter="", token_dir="")
        calls.append(replace(call, repo=str(record.repo), package=record.package or "", kind=record.kind,
                             wait_state=record.state))
    return tuple(calls)


def session_is_live(call: VerifyCall, paths: MacIdlePaths, live: dict[int, LiveProcess]) -> bool:
    for path in paths.sessions.glob("*.json"):
        row = read_record(path)
        pid = row.get("pid")
        if (row.get("sessionId") == call.session and isinstance(pid, int)
                and pid in live and isinstance(row.get("procStart"), str) and bool(row.get("procStart"))
                and process_start.check_owner(pid, process_start.parse_start(str(row["procStart"])), paths.proc) is process_start.OwnerCheck.ALIVE):
            return True
    return False


def notify_director(call: VerifyCall, cause: str) -> bool:
    try:
        result = subprocess.run([sys.executable, str(SCRIPTS / "message/roster.py"), "--json"],
                                capture_output=True, text=True, timeout=45, check=False)
        raw = cast(object, json.loads(result.stdout))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    entries: object = cast(dict[str, object], raw).get("entries") if isinstance(raw, dict) else ()
    if not isinstance(entries, list):
        return False
    for value in cast(list[object], entries):
        if not isinstance(value, dict):
            continue
        entry = cast(dict[str, object], value)
        address = entry.get("address")
        if (entry.get("role") == "unit director" and entry.get("unit") == "builds-unit"
                and entry.get("production") == "build-followups" and isinstance(address, str) and address):
            try:
                sent = subprocess.run([sys.executable, str(SCRIPTS / "message/send.py"), "--to", address,
                    "--from", "Mac idle watch", "--text", f"Mac idle watch: {cause}\nRepo {call.repo}; call {call.call_id}; "
                    + f"verify.sh {call.kind} {call.package}, pid {call.pid}. The Mac has no claim for at least 3 minutes. "
                    + "The call is still waiting on natedev. Check its wait and Mac result."],
                    capture_output=True, text=True, timeout=45, check=False)
            except (OSError, subprocess.SubprocessError):
                return False
            return sent.returncode in (0, 1)
    return False


def record_case(call: VerifyCall, cause: str, action: str, wait: str, now: float) -> None:
    host = store.host_name()
    identity = hashlib.sha256(f"{host}:{call.call_id}:{cause}".encode()).hexdigest()
    store.append_line(store.host_file(host, now), {"kind": "mac_idle", "id": identity, "host": host,
        "at": store.utc_iso(now), "call_id": call.call_id, "pid": call.pid, "proc_start": call.start,
        "session": call.session, "repo": call.repo, "package": call.package, "wait": wait, "cause": cause, "action": action})


def watch_once(paths: MacIdlePaths, now: float, monotonic: float) -> None:
    """Change only watch state, retry markers and missing session temp roots."""
    paths.state.mkdir(parents=True, exist_ok=True)
    with (paths.state / "watch.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        idle_file = paths.state / "idle.json"
        # A block stays effective after its requested expiry until Mac policy
        # removes it. Never expire it here. A legacy global claim also counts.
        try:
            claims = tuple((paths.mac_state / "claims").iterdir())
            claims_changed = (paths.mac_state / "claims").stat().st_mtime
        except FileNotFoundError:
            claims = ()
            claims_changed = 0.0
        except OSError:
            idle_file.unlink(missing_ok=True)
            return
        if ((paths.mac_state / "run.json").exists() or claims
                or (paths.mac_state / "block.json").exists()):
            idle_file.unlink(missing_ok=True)
            return
        try:
            until = float((paths.mac_state / "unreachable_until").read_text())
        except FileNotFoundError:
            until = 0.0
        except (OSError, ValueError):
            return
        if not math.isfinite(until) or until > now:
            idle_file.unlink(missing_ok=True)
            return
        since = read_record(idle_file).get("since")
        if not isinstance(since, (float, int)) or since > now:
            write_record(idle_file, {"since": now})
            return
        if claims_changed > since:
            since = min(now, claims_changed)
            write_record(idle_file, {"since": since})
        if now - since < IDLE_SECONDS:
            return
        live = processes(paths.proc)
        for call in waiting_calls(paths, live):
            case_file = call_path(paths, call.call_id).with_suffix(".cases")
            cases = read_record(case_file)
            wait = waiting_evidence(call, paths, live)
            causes: list[tuple[str, str]] = []
            session_dir = paths.session_root / call.session
            if (SESSION_ID.fullmatch(call.session) and call.temp_dir == str(session_dir) and not session_dir.exists()
                    and session_is_live(call, paths, live)):
                session_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
                causes.append(("missing_temp_root", "created_session_temp_root"))
            if not call.marker_hook:
                cause, action = "call_has_no_marker_hook", "message_builds_unit"
            elif call.retry_after > monotonic:
                cause, action = "retry_delayed", "retry_marker"
            else:
                children = [child for child in live.values() if belongs_to(child, call, live)]
                no_hook = wait == "cargo_token" or any(
                    "wait-local" in child.argv and "--leave-hook" not in child.argv for child in children)
                if wait == "memory":
                    no_hook = no_hook or any(not child.environment.get("LINT_QUEUE_LEAVE_HOOK")
                        and (paths.admission / f"{child.pid}-{child.start}.waiting").exists() for child in children)
                cause, action = ("missing_leave_hook", "retry_marker") if no_hook else ("idle_with_waiter", "message_builds_unit")
            causes.append((cause, action))
            for cause, action in causes:
                if cases.get(cause) == "message_pending":
                    if notify_director(call, cause):
                        cases[cause] = "message_builds_unit"
                        write_record(case_file, cases)
                    continue
                if cause in cases:
                    continue
                if action == "retry_marker":
                    _ = marker_path(paths, call.call_id).write_text(cause + "\n")
                elif action == "message_builds_unit" and not notify_director(call, cause):
                    action = "message_pending"
                record_case(call, cause, action, wait, now)
                cases[cause] = action
                write_record(case_file, cases)


def main(args: list[str]) -> int:
    paths = watch_paths()
    if args[:1] == ["register"] and len(args) == 12:
        _, call_id, pid_text, repo, package, settings, retry, hold_waiter, token_dir, session, temp_dir, marker = args
        pid = int(pid_text)
        process = read_process(paths.proc / pid_text)
        call = VerifyCall(call_id, pid, process.start, repo, package, session, temp_dir, hold_waiter, token_dir,
                           settings, float(retry), marker == "1")
        write_record(call_path(paths, call_id), asdict(call))
        print(marker_path(paths, call_id))
    elif args[:1] == ["remove"] and len(args) == 2:
        call_path(paths, args[1]).unlink(missing_ok=True)
        marker_path(paths, args[1]).unlink(missing_ok=True)
    elif args[:1] == ["exclude"] and len(args) == 3:
        path = call_path(paths, args[1])
        call = registered_call(read_record(path))
        write_record(path, asdict(replace(call, local_reason=args[2])))
        marker_path(paths, args[1]).unlink(missing_ok=True)
    elif args == ["watch"]:
        watch_once(paths, time.time(), time.monotonic())
    else:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
