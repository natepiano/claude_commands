"""Repair known reasons an eligible verify call waits while the Mac is idle."""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Iterable, Mapping
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
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from scripts.buildlog import store

SCRIPTS = Path(__file__).resolve().parents[1]

if TYPE_CHECKING:
    from scripts.build_queue import admission_records
    from scripts.direct import step_record
    from scripts.mac_test import mac_test
    from scripts.buildlog import mac_idle_alert
    from scripts.message.unit_labels import UnitLabels
else:
    from scripts.direct import step_record
    from scripts.buildlog import mac_idle_alert

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
        mac_idle_alert.state_dir(),
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
    updated: str = ""


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
    fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
    if fields[0] == "Z":
        raise ValueError("process has ended")
    argv = tuple(part.decode(errors="replace") for part in (entry / "cmdline").read_bytes().split(b"\0") if part)
    environment = dict(part.decode(errors="replace").split("=", 1)
                       for part in (entry / "environ").read_bytes().split(b"\0") if b"=" in part)
    return LiveProcess(int(entry.name), int(fields[1]), fields[19], argv, environment, str((entry / "cwd").readlink()))


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
            return process.start == call.start
        seen.add(process.pid)
        if process.parent not in live:
            return False
        process = live[process.parent]
    return False


@dataclass(frozen=True)
class AdmissionWaiters:
    waiters: list[admission_records.Waiter]


@dataclass(frozen=True)
class AdmissionQueryFailed:
    reason: str


def admission_waiters() -> AdmissionWaiters | AdmissionQueryFailed:
    from scripts.build_queue import admission_client, admission_protocol

    try:
        return AdmissionWaiters([row for _, row in admission_client.view().ordered])
    except admission_protocol.AdmissionUnavailable as error:
        return AdmissionQueryFailed(str(error))


def _waiting_evidence(call: VerifyCall, live: dict[int, LiveProcess],
                      waiters: list[admission_records.Waiter]) -> str:
    children = {pid: child for pid, child in live.items() if belongs_to(child, call, live)}
    for row in waiters:
        pid = row["pid"]
        if pid in children and children[pid].start == row["start_time"]:
            return "memory"
    if call.token_dir:
        if any(Path(call.token_dir).glob(f"*.{call.pid}")) or any(Path(call.token_dir).glob(f"*.{call.pid}.again")):
            return "cargo_token"
    if call.hold_waiter:
        row = read_record(Path(call.hold_waiter))
        pid = row.get("pid")
        if (row.get("call_id") == call.call_id and isinstance(pid, int) and pid in children
                and children[pid].start == row.get("proc_start")):
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
    old_diagnostics = read_record(paths.state / "step_diagnostics.json")
    diagnostics: dict[str, str] = {}
    for path in sorted(step_record.record_directory().glob("*.json")):
        record = step_record.read(path.stem)
        if isinstance(record, (step_record.StaleRecord, step_record.UnreadableRecord)):
            diagnostic = (f"ignored stale step record {record.call_id}" if isinstance(record, step_record.StaleRecord)
                          else f"ignored unreadable step record {record.call_id}: {record.reason}")
            diagnostics[path.name] = diagnostic
            if old_diagnostics.get(path.name) != diagnostic:
                print(f"Mac idle watch: {diagnostic}", file=sys.stderr)
            continue
        if not isinstance(record, step_record.StepRecord):
            continue
        disk_excluded = (isinstance(record.runs_on, step_record.AnyTarget) and bool(record.mac_disk_exclusions)) or (
            isinstance(record.runs_on, step_record.NatedevOnly) and record.runs_on.reason == "disk")
        if disk_excluded:
            if record.state not in (step_record.StepState.WAITING_HOLD, step_record.StepState.WAITING_MAC,
                                    step_record.StepState.ON_NATEDEV):
                continue
        elif (not step_record.mac_eligible(record)
              or record.state not in (step_record.StepState.WAITING_HOLD, step_record.StepState.WAITING_MAC)):
            continue
        process = live.get(record.pid)
        if process is None:
            try:
                process = read_process(paths.proc / str(record.pid))
            except (OSError, ValueError, IndexError):
                continue
            live[record.pid] = process
        if process.start != record.proc_start:
            continue
        try:
            call = registered_call(read_record(call_path(paths, record.call_id)))
        except ValueError:
            call = VerifyCall(record.call_id, record.pid, record.proc_start, str(record.repo), step_record.package_name(record.package),
                "", "", "", "", "", 0, False)
        if call.pid != record.pid or call.start != record.proc_start:
            call = replace(call, pid=record.pid, start=record.proc_start, marker_hook=False,
                           session="", temp_dir="", hold_waiter="", token_dir="")
        if call.local_reason and call.local_reason != "disk" and not disk_excluded:
            continue
        calls.append(replace(call, repo=str(record.repo), package=step_record.package_name(record.package), kind=record.kind,
                             wait_state=record.state, updated=record.updated,
                             local_reason="disk" if disk_excluded else call.local_reason))
    if diagnostics != old_diagnostics:
        write_record(paths.state / "step_diagnostics.json", diagnostics)
    return tuple(calls)


def session_is_live(call: VerifyCall, paths: MacIdlePaths, live: dict[int, LiveProcess]) -> bool:
    for path in paths.sessions.glob("*.json"):
        row = read_record(path)
        pid = row.get("pid")
        if (row.get("sessionId") == call.session and isinstance(pid, int)
                and pid in live and live[pid].start == row.get("procStart")):
            return True
    return False


@dataclass(frozen=True)
class OutageMessage:
    since: int
    now: float
    key: str


def outage_key(saved: mac_idle_alert.SavedAlert) -> str:
    return f"mac-idle-showrunner-{int(saved['since'])}-{int(saved['changed'])}"


class RepairMessage:
    """A per-call repair notice rather than an outage notification."""


REPAIR_MESSAGE = RepairMessage()


def outage_repeat_minutes(since: float, now: float, settings: mac_idle_alert.MacIdleConfig) -> float:
    """Keep a retry inside the sender's window even after a long watch interruption."""
    return max(settings.repeat_minutes, (now - since + settings.alert_seconds) / 60)


def notify_showrunner(text: str, *, identity: OutageMessage | RepairMessage = REPAIR_MESSAGE) -> bool:
    if TYPE_CHECKING:
        from scripts.message import send_budget, session_send
        from scripts.notify import push_texts
        from scripts.lib.process_failure import failure_detail
    else:
        from scripts.message import send_budget
        from scripts.message import session_send
        from scripts.notify import push_texts
        from scripts.lib.process_failure import failure_detail

    try:
        result = subprocess.run([str(SCRIPTS / "lib/py"), str(SCRIPTS / "production/showrunners.py"), "address", "builds"],
                                capture_output=True, text=True, timeout=send_budget.seconds(), check=False)
        address = result.stdout.strip()
        if result.returncode != 0 or not address:
            raise ValueError("builds showrunner has no address")
        command = session_send.send_argv("--to", address, "--from", "Mac idle watch", "--text", text)
        if isinstance(identity, OutageMessage):
            window = outage_repeat_minutes(identity.since, identity.now, mac_idle_alert.read_idle_config())
            command.extend(["--key", identity.key, "--repeat-minutes", str(window),
                            "--recipient-key", "showrunner:builds"])
        sent = subprocess.run(command, capture_output=True, text=True, timeout=send_budget.seconds(), check=False)
        if sent.returncode != push_texts.SEND_RETRY_EXIT and sent.returncode in (0, 1):
            return True
        reason = failure_detail(sent)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        reason = str(error)
    print(f"Mac idle watch: showrunner message failed: {mac_idle_alert.display_field(reason)}", file=sys.stderr)
    return False


def repair_message(call: VerifyCall, cause: str) -> str:
    return (f"Mac idle watch: {cause}\nRepo {call.repo}; call {call.call_id}; "
            + f"verify.sh {call.kind} {call.package}, pid {call.pid}. The Mac has no claim for at least "
            + f"{mac_idle_alert.read_idle_config().alert_seconds:g} seconds. "
            + "The call is still waiting on natedev. Check its wait and Mac result.")


@dataclass(frozen=True)
class NatedevJobOnMac:
    """A legacy job or a live claim whose Mac work has not gone idle."""


class FreeMac:
    """No block or live claim reserves the Mac."""


@dataclass(frozen=True)
class IdleReservation:
    cause: Literal["blocked", "claim_idle"]
    holder: str
    detail: str
    delivery: mac_test.MessageDelivery
    claims: tuple[mac_test.HeldClaim, ...] = ()


IdleMac = FreeMac | IdleReservation


@dataclass(frozen=True)
class MacUnreachable:
    until: float


@dataclass(frozen=True)
class MacStateUnreadable:
    reason: str


def mac_work(paths: MacIdlePaths, now: float) -> NatedevJobOnMac | IdleMac | MacUnreachable | MacStateUnreadable:
    """Use the claim owner's live rule and its saved work reading; never contact the Mac."""
    from scripts.mac_test import mac_test

    try:
        if (paths.mac_state / "run.json").exists():
            return NatedevJobOnMac()
        live = tuple(claim for claim in mac_test.read_claims(mac_test.StatePaths(paths.mac_state))
                     if mac_test.run_is_live(claim.record))
        block = mac_test.read_block(paths.mac_state / "block.json")
        if any("work_idle_since" not in claim.record for claim in live):
            return NatedevJobOnMac()
        try:
            until = float((paths.mac_state / "unreachable_until").read_text())
        except FileNotFoundError:
            until = 0.0
        if not math.isfinite(until):
            return MacStateUnreadable("unreachable_until is not finite")
        if until > now:
            return MacUnreachable(until)
        if not isinstance(block, mac_test.NoMacBlock):
            return IdleReservation("blocked", block.holder, block.reason, block.delivery, live)
        if live:
            claim = live[0].record
            holder = claim.get("holder_session", "")
            delivery = mac_test.RecordedSession(holder, holder) if holder else mac_test.HolderQueue("unknown")
            return IdleReservation("claim_idle", holder, claim["what"], delivery, live)
        return FreeMac()
    except (OSError, ValueError, UnicodeError) as error:
        return MacStateUnreadable(str(error))


def notify_outage(paths: MacIdlePaths, saved: mac_idle_alert.SavedAlert, now: float,
                  calls: tuple[VerifyCall, ...], live: dict[int, LiveProcess],
                  admission: AdmissionWaiters | AdmissionQueryFailed) -> bool:
    from scripts.lib.user_zone import user_zone

    line = mac_idle_alert.alert_line(paths.state / "alert.json", now, user_zone())
    waiters = admission.waiters if isinstance(admission, AdmissionWaiters) else []
    text = (line or "") + "".join(
        f"\nRepo {mac_idle_alert.display_field(call.repo)}; {call.kind} "
        + f"{mac_idle_alert.display_field(call.package)}; pid {call.pid}; "
        + f"wait {call.local_reason or _waiting_evidence(call, live, waiters)}" for call in calls)
    return notify_showrunner(text, identity=OutageMessage(int(saved["since"]), now, outage_key(saved)))


def push_outage(saved: mac_idle_alert.SavedAlert, now: float) -> bool:
    from scripts.notify import push_texts
    from scripts.notify.push_catalog import mac_idle_reservation
    from scripts.lib.process_failure import failure_detail
    from scripts.lib.user_zone import user_zone

    holder, detail = saved["holder"] or "none", saved["detail"] or "no block and no claim"
    blocked = saved["cause"] == "blocked"
    clears = (f"/build:mac unblock by {holder}" if blocked else f"release of the idle claim {detail}"
              if saved["cause"] == "claim_idle" else
              "the Mac has enough free disk space" if saved["cause"] == "disk" else
              "the builds showrunner checks why the waiting steps do not take the Mac")
    values = {"minutes": str(int((now - saved["since"]) // 60)), "waiting": str(saved["waiting"]),
              "since": mac_idle_alert.clock_text(saved["since"], user_zone(), include_zone=True), "clears": clears}
    values.update({"holder": holder, "detail": detail} if blocked else
                  {"reservation": mac_idle_reservation(holder, detail) if saved["cause"] == "claim_idle" else ""})
    rendered = push_texts.render("mac_idle.blocked" if blocked else "mac_idle.stuck", **values)
    since = str(int(saved["since"]))
    command = push_texts.user_send_argv(rendered, sender="Mac idle watch", key=f"mac-idle-{since}")
    if blocked:
        command.extend(["--clears-when", f"{SCRIPTS}/lib/py {SCRIPTS}/buildlog/mac_idle.py cleared --since {since}"])
    settings = mac_idle_alert.read_idle_config()
    window = outage_repeat_minutes(saved["since"], now, settings)
    command.extend(["--repeat-minutes", f"{window:g}", "--recipient-key", "user"])
    try:
        sent = subprocess.run(command, capture_output=True, text=True, timeout=settings.send_timeout_s, check=False)
        if sent.returncode != push_texts.SEND_RETRY_EXIT and sent.returncode == 0:
            return True
        reason = failure_detail(sent)
    except (OSError, subprocess.SubprocessError) as error:
        reason = str(error)
    print(f"Mac idle watch: phone push failed: {mac_idle_alert.display_field(reason)}", file=sys.stderr)
    return False


def record_case(call: VerifyCall, cause: str, action: str, wait: str, now: float) -> None:
    host = store.host_name()
    identity = hashlib.sha256(f"{host}:{call.call_id}:{cause}".encode()).hexdigest()
    store.append_line(store.host_file(host, now), {"kind": "mac_idle", "id": identity, "host": host,
        "at": store.utc_iso(now), "call_id": call.call_id, "pid": call.pid, "proc_start": call.start,
        "session": call.session, "repo": call.repo, "package": call.package, "wait": wait, "cause": cause, "action": action})


def unit_labels_for(session_ids: Iterable[str], environment: Mapping[str, str]) -> dict[str, UnitLabels]:
    """Load holder labels when the watch needs them. Keep the test patch point."""
    from scripts.message import unit_labels

    return unit_labels.unit_labels_for(session_ids, environment)


def calls_past_wait_limit(paths: MacIdlePaths, calls: tuple[VerifyCall, ...], now: float,
                         alert_seconds: float) -> tuple[VerifyCall, ...]:
    path = paths.state / "wait_starts.json"
    previous = read_record(path)
    starts: dict[str, str] = {}
    ready: list[VerifyCall] = []
    for call in calls:
        identity = json.dumps([call.call_id, call.start])
        starts[identity] = str(previous.get(identity, call.updated))
        try:
            started = datetime.fromisoformat(starts[identity].replace("Z", "+00:00")).timestamp()
        except ValueError:
            ready.append(call)
            continue
        if now - started >= alert_seconds:
            ready.append(call)
    if starts != previous:
        write_record(path, starts)
    return tuple(ready)


def watch_once(paths: MacIdlePaths, now: float, monotonic: float) -> None:
    """Save the one idle reading, notify its outage and repair free-Mac waits."""
    from scripts.mac_test import mac_test

    paths.state.mkdir(parents=True, exist_ok=True)
    with (paths.state / "watch.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        idle_file = paths.state / "idle.json"
        reading = mac_work(paths, now)
        has_steps = any(step_record.record_directory().glob("*.json"))
        live: dict[int, LiveProcess] = {}
        calls = waiting_calls(paths, live) if has_steps else ()
        settings = mac_idle_alert.read_idle_config()
        calls = calls_past_wait_limit(paths, calls, now, settings.alert_seconds)
        previous = mac_idle_alert.read_alert(paths.state / "alert.json")
        saved: mac_idle_alert.SavedAlert = {"version": 1, "written": now, "state": "busy", "since": 0.0, "changed": 0.0,
            "waiting": len(calls), "cause": "", "holder": "", "detail": "", "until": 0.0,
            "messaged": False, "pushed": False}
        kept: mac_idle_alert.SavedAlert | None = None
        if isinstance(reading, NatedevJobOnMac):
            idle_file.unlink(missing_ok=True)
        else:
            idle = read_record(idle_file)
            since = idle.get("since")
            since = float(cast(float, since)) if mac_idle_alert.nonnegative_time(since) else now
            if since > now:
                since = now
            if isinstance(reading, IdleReservation):
                latest_idle = since
                for claim in reading.claims:
                    if mac_test.aware_timestamp(claim.record["since"], claim.path, "since").timestamp() < since:
                        work_idle_since = mac_test.aware_timestamp(
                            claim.record.get("work_idle_since"), claim.path, "work_idle_since").timestamp()
                        latest_idle = max(latest_idle, min(now, work_idle_since))
                since = latest_idle
            kept = previous if isinstance(previous, dict) and int(previous["since"]) == int(since) else None
            saved["since"] = since
            if isinstance(reading, MacStateUnreadable):
                saved["state"], saved["detail"] = "unreadable", reading.reason
            elif isinstance(reading, MacUnreachable):
                saved["state"], saved["until"] = "unreachable", reading.until
            else:
                saved["state"] = "idle"
                if isinstance(reading, FreeMac):
                    saved["cause"] = "free"
                    write_record(idle_file, {"since": since})
                else:
                    saved["cause"], saved["detail"] = reading.cause, reading.detail
                    owner = {"holder": reading.holder,
                             "session": reading.delivery.name if isinstance(reading.delivery, mac_test.RecordedSession) else None}
                    if kept is not None and kept["cause"] == reading.cause and idle.get("owner") == owner:
                        saved["holder"] = kept["holder"]
                    else:
                        labels = (unit_labels_for([reading.delivery.name], os.environ)
                                  if isinstance(reading.delivery, mac_test.RecordedSession) else {})
                        label = labels.get(reading.delivery.name)
                        saved["holder"] = label.session if label is not None else reading.holder
                    write_record(idle_file, {"since": since, "owner": owner})
        if any(call.local_reason == "disk" for call in calls):
            if saved["cause"] == "free":
                saved["cause"] = "disk"
            saved["detail"] = (saved["detail"] + "; " if saved["detail"] else "") + "Mac disk exclusion"
        saved["changed"] = saved["since"]
        if kept is not None:
            saved["pushed"] = kept["pushed"]
            saved["changed"] = (kept["changed"]
                                if (kept["cause"], kept["holder"]) == (saved["cause"], saved["holder"]) else now)
            if outage_key(kept) == outage_key(saved):
                saved["messaged"] = kept["messaged"]
        write_record(paths.state / "alert.json", saved)
        if not isinstance(reading, IdleMac) or now - saved["since"] < settings.alert_seconds or not calls:
            return
        # Only wait evidence and repairs need the process ancestry.
        live = processes(paths.proc)
        admission = admission_waiters()
        if isinstance(admission, AdmissionQueryFailed):
            saved["admission_error"] = admission.reason
            print(f"Mac idle watch: admission query failed: {mac_idle_alert.display_field(admission.reason)}",
                  file=sys.stderr)
            write_record(paths.state / "alert.json", saved)
        if not saved["messaged"]:
            # The sender owns delivery deduplication. Retry unconfirmed attempts.
            saved["messaged"] = notify_outage(paths, saved, now, calls, live, admission)
            write_record(paths.state / "alert.json", saved)
        if now - saved["since"] >= settings.push_seconds and not saved["pushed"]:
            saved["pushed"] = push_outage(saved, now)
            write_record(paths.state / "alert.json", saved)
        if not isinstance(reading, FreeMac):
            return
        waiters = admission.waiters if isinstance(admission, AdmissionWaiters) else []
        for call in calls:
            if call.local_reason == "disk":
                continue
            case_file = call_path(paths, call.call_id).with_suffix(".cases")
            cases = read_record(case_file)
            wait = _waiting_evidence(call, live, waiters)
            causes: list[tuple[str, str]] = []
            session_dir = paths.session_root / call.session
            if (SESSION_ID.fullmatch(call.session) and call.temp_dir == str(session_dir) and not session_dir.exists()
                    and session_is_live(call, paths, live)):
                session_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
                causes.append(("missing_temp_root", "created_session_temp_root"))
            if not call.marker_hook:
                cause, action = "call_has_no_marker_hook", "message_showrunner"
            elif call.retry_after > monotonic:
                cause, action = "retry_delayed", "retry_marker"
            else:
                children = [child for child in live.values() if belongs_to(child, call, live)]
                no_hook = wait == "cargo_token" or any(
                    "wait-local" in child.argv and "--leave-hook" not in child.argv for child in children)
                if wait == "memory":
                    no_hook = no_hook or any(not child.environment.get("LINT_QUEUE_LEAVE_HOOK")
                        and any(row["pid"] == child.pid and row["start_time"] == child.start
                                for row in waiters) for child in children)
                cause, action = ("missing_leave_hook", "retry_marker") if no_hook else ("idle_with_waiter", "message_showrunner")
            causes.append((cause, action))
            for cause, action in causes:
                if cases.get(cause) == "message_pending":
                    if notify_showrunner(repair_message(call, cause)):
                        cases[cause] = "message_showrunner"
                        write_record(case_file, cases)
                    continue
                if cause in cases:
                    continue
                if action == "retry_marker":
                    _ = marker_path(paths, call.call_id).write_text(cause + "\n")
                elif action == "message_showrunner" and not notify_showrunner(repair_message(call, cause)):
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
    elif args[:2] == ["cleared", "--since"] and len(args) == 3:
        try:
            since = float(args[2])
        except ValueError:
            return 1
        if not math.isfinite(since):
            return 1
        saved = mac_idle_alert.read_alert(paths.state / "alert.json")
        try:
            freshness = mac_idle_alert.read_idle_config().stale_seconds
        except mac_idle_alert.MacIdleConfigError:
            return 1
        if not isinstance(saved, dict) or time.time() - saved["written"] > freshness:
            return 1
        return int(saved["state"] == "idle" and int(saved["since"]) == int(since) and saved["waiting"] > 0)
    elif args == ["watch"]:
        watch_once(paths, time.time(), time.monotonic())
    else:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
