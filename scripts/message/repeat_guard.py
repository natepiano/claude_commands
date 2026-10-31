"""Local repeat decisions and a durable notice outbox; no delivery or discovery here.

The pure transition is also the log replay. One sender record serializes concurrent
receivers; one durable notice request per episode covers all of them.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import signal
import contextvars
import socket
import threading
import subprocess
import time
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from subprocess import Popen
from typing import Literal, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.atomic_file import write_text_atomic

CONFIG = Path(__file__).resolve().parents[2] / "config/send_repeat_guard.conf"
NOTICE_SENDER = "send-repeat-guard"
PHONE_TEXT = "message.repeat_flood"


@dataclass(frozen=True)
class Limits:
    window_seconds: int
    repeat_count: int
    sender_rate_count: int
    max_senders: int
    max_keys: int
    preview_characters: int
    notice_timeout_seconds: int
    drain_timeout_seconds: int
    notice_max_age_seconds: int


@dataclass(frozen=True)
class GuardUnavailable:
    reason: str


_reported: set[str] = set()


def warn(reason: str) -> None:
    if reason not in _reported:
        _reported.add(reason)
        print(f"send repeat guard: {reason}; delivery stays enabled", file=sys.stderr)


def configuration() -> Limits | GuardUnavailable:
    """Read at use. A bad config fails open, without invented code defaults."""
    path = Path(os.environ.get("SEND_REPEAT_CONFIG", str(CONFIG)))
    try:
        values: dict[str, int] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            key, separator, value = line.partition("=")
            if not separator or key.strip() in values:
                raise ValueError("expected unique key=value lines")
            values[key.strip()] = int(value.strip())
        if set(values) != set(Limits.__dataclass_fields__) or any(value <= 0 for value in values.values()):
            raise ValueError("all configured limits must be positive integers")
        if values["repeat_count"] < 3:
            raise ValueError("repeat_count must allow the first two copies")
        return Limits(**values)
    except (OSError, ValueError, TypeError) as error:
        reason = f"{path}: {error}"
        warn(reason)
        return GuardUnavailable(reason)


def normalize(text: str) -> str:
    """Unknown text is literal. Values in prose may be the message's meaning."""
    return text


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class Attempt:
    sender: str
    receiver: str
    text: str
    host: str
    at: float
    protected: bool = False


@dataclass
class Receiver:
    normalized: str
    first: float
    count: int
    last: float = 0


@dataclass
class Flood:
    text: str
    first: float
    last: float
    count: int = 0
    receivers: list[str] = field(default_factory=list)


@dataclass
class Notice:
    kind: Literal["owner", "phone"]
    text: str
    first: float
    last: float
    copies: int
    receivers: int
    id: str = ""
    status: Literal["pending", "claimed", "delivered", "queued", "unknown"] = "pending"
    created: float = 0


@dataclass
class SenderState:
    sender: str
    host: str
    last: float
    rate_first: float
    rate_count: int = 0
    receivers: dict[str, Receiver] = field(default_factory=dict)
    floods: dict[str, Flood] = field(default_factory=dict)
    notices: list[Notice] = field(default_factory=list)
    holds: list[float] = field(default_factory=list)
    identity_version: int = 2


@dataclass(frozen=True)
class Decision:
    held: bool = False
    copies: int = 0


def expire(state: SenderState, at: float, limits: Limits) -> None:
    """Counter expiry never discards unfinished outbox work before its age limit."""
    state.floods = {key: value for key, value in state.floods.items()
                    if at - value.last < limits.window_seconds}
    state.receivers = {key: value for key, value in state.receivers.items()
                       if at - value.last < limits.window_seconds}
    live = {digest(state.sender + "\0" + key + "\0" + str(value.first)) for key, value in state.floods.items()}
    state.notices = [notice for notice in state.notices
                     if (notice.status in ("delivered", "queued") and notice.id in live)
                     or (notice.status not in ("delivered", "queued")
                         and at - notice.created < limits.notice_max_age_seconds)]


def transition(state: SenderState, attempt: Attempt, limits: Limits) -> Decision:
    """Apply one attempt per (receiver, exact text); old masked counts are retired."""
    if attempt.protected or attempt.sender.casefold() == "user" or attempt.sender == NOTICE_SENDER or attempt.receiver == "user":
        return Decision()
    at = attempt.at
    if state.identity_version != 2:
        state.receivers.clear()
        state.floods.clear()
        state.holds.clear()
        state.rate_first, state.rate_count = at, 0
        state.identity_version = 2
    normalized = normalize(attempt.text)
    identity = digest("literal-v2\0" + normalized)
    state.last = at
    expire(state, at, limits)
    receiver_key = digest(attempt.receiver + "\0" + identity)
    if receiver_key not in state.receivers:
        if len(state.receivers) >= limits.max_keys:
            return Decision()
        state.receivers[receiver_key] = Receiver(identity, at, 0, at)
    receiver = state.receivers[receiver_key]
    receiver.count += 1
    receiver.last = at
    if identity not in state.floods:
        if len(state.floods) >= limits.max_keys:
            return Decision()
        state.floods[identity] = Flood(normalized[:limits.preview_characters], at, at)
    flood = state.floods[identity]
    flood.count += 1
    flood.last = at
    if attempt.receiver not in flood.receivers and len(flood.receivers) < limits.max_keys:
        flood.receivers.append(attempt.receiver)
    if at - state.rate_first >= limits.window_seconds:
        state.rate_first, state.rate_count = at, 0
    state.rate_count += 1
    held = receiver.count >= limits.repeat_count
    if not held and state.rate_count >= limits.sender_rate_count:
        rate = state.floods.setdefault("rate", Flood("Many distinct messages; latest: " + flood.text,
                                                       state.rate_first, at))
        rate.count, rate.last = state.rate_count, at
        rate.receivers = sorted({name for item in state.floods.values() for name in item.receivers})[:limits.max_keys]
        flood, identity = rate, "rate"
    episode = digest(state.sender + "\0" + identity + "\0" + str(flood.first))
    kinds: tuple[Literal["owner", "phone"], ...] = (("owner", "phone") if held else
        ("owner",) if state.rate_count >= limits.sender_rate_count else ())
    missing = [kind for kind in kinds if not any(notice.id == episode and notice.kind == kind for notice in state.notices)]
    if len(state.notices) + len(missing) > 2 * limits.max_keys:
        warn("notice outbox capacity reached")
        return Decision()
    for kind in kinds:
        if not any(notice.id == episode and notice.kind == kind for notice in state.notices):
            state.notices.append(Notice(kind, flood.text, flood.first, at, flood.count, len(flood.receivers), episode, created=at))
    if held:
        state.holds = [*state.holds, at][-limits.max_keys:]
    return Decision(held, receiver.count)


def decode(text: str) -> SenderState:
    """Validate every saved field, including nested values, before using any of it."""
    def object_map(value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise ValueError("expected state object")
        if not all(isinstance(key, str) for key in cast(dict[object, object], value)):
            raise ValueError("expected state object")
        return cast(dict[str, object], value)

    def number(value: object, *, integer: bool = False) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid finite nonnegative state number")
        if value > sys.maxsize or (integer and int(value) != value):
            raise ValueError("state number outside range")
        return value

    def string(value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("expected state string")
        return value

    raw = object_map(cast(object, json.loads(text)))
    state = SenderState(string(raw["sender"]), string(raw["host"]), number(raw["last"]),
                        number(raw["rate_first"]), int(number(raw["rate_count"], integer=True)))
    state.identity_version = int(number(raw.get("identity_version", 1), integer=True))
    if state.identity_version not in (1, 2):
        raise ValueError("unknown counter identity version")
    for key, value in object_map(raw["receivers"]).items():
        item = object_map(value)
        state.receivers[key] = Receiver(string(item["normalized"]), number(item["first"]),
            int(number(item["count"], integer=True)), number(item["last"]))
    for key, value in object_map(raw["floods"]).items():
        item = object_map(value)
        names = item["receivers"]
        if not isinstance(names, list):
            raise ValueError("expected receiver list")
        state.floods[key] = Flood(string(item["text"]), number(item["first"]), number(item["last"]),
            int(number(item["count"], integer=True)), [string(name) for name in cast(list[object], names)])
    notices = raw["notices"]
    holds = raw["holds"]
    if not isinstance(notices, list) or not isinstance(holds, list):
        raise ValueError("expected notices and holds lists")
    for value in cast(list[object], notices):
        item = object_map(value)
        kind, status = item["kind"], item["status"]
        if kind not in ("owner", "phone") or status not in ("pending", "claimed", "delivered", "queued", "unknown"):
            raise ValueError("invalid notice state")
        state.notices.append(Notice(kind, string(item["text"]),
            number(item["first"]), number(item["last"]), int(number(item["copies"], integer=True)),
            int(number(item["receivers"], integer=True)), string(item["id"]),
            status, number(item.get("created", item["last"]))))
    state.holds = [number(value) for value in cast(list[object], holds)]
    return state


@contextmanager
def sender_record(root: Path, sender: str) -> Generator[Path]:
    root.mkdir(parents=True, exist_ok=True)
    path = root / (digest(sender) + ".json")
    # The first hash byte is a stable lock shard, independent of reloadable capacity.
    directory = root / digest(sender)[:2]
    directory.mkdir(exist_ok=True)
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield path
    finally:
        os.close(descriptor)


def _stored_check(root: Path, attempt: Attempt, *, event: bool = False) -> Decision:
    if not math.isfinite(attempt.at) or attempt.at < 0:
        warn("invalid attempt timestamp")
        return Decision()
    limits = configuration()
    if isinstance(limits, GuardUnavailable):
        return Decision()
    try:
        if max(len(attempt.sender), len(attempt.receiver)) > limits.preview_characters:
            raise ValueError("routing identity exceeds configured capacity")
        with sender_record(root, attempt.sender) as path:
            if path.exists():
                try:
                    state = decode(path.read_text())
                    if state.sender != attempt.sender or len(state.receivers) > limits.max_keys or len(state.floods) > limits.max_keys:
                        raise ValueError("invalid sender or state capacity")
                except Exception:
                    path.unlink(missing_ok=True)
                    raise
            else:
                capacity = os.open(root, os.O_RDONLY)
                try:
                    fcntl.flock(capacity, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if len(list(root.glob("*.json"))) >= limits.max_senders:
                        raise ValueError("sender state capacity reached")
                    state = SenderState(attempt.sender, attempt.host, attempt.at, attempt.at)
                    write_text_atomic(path, json.dumps(asdict(state)))
                finally:
                    os.close(capacity)
            decision = transition(state, attempt, limits)
            write_text_atomic(path, json.dumps(asdict(state)))
            if event and decision.held:
                record_event(root.parent, attempt, "held")
            return decision
    except Exception as error:
        warn(str(error))
        return Decision()


# An I/O-free caller safety ceiling, not a holding policy or configuration default.
# Only the worker reads policy. On timeout the caller always admits delivery.
GUARD_IO_CEILING_SECONDS = 0.25
_ACCOUNTING_OWNER = contextvars.ContextVar("repeat_accounting_owner", default=True)
_REPORT_RECIPIENT = contextvars.ContextVar("repeat_report_recipient", default=False)


@contextmanager
def accounting_scope(*, owner: bool, report: bool = False) -> Generator[None]:
    ownership = _ACCOUNTING_OWNER.set(owner)
    reporting = _REPORT_RECIPIENT.set(report)
    try:
        yield
    finally:
        _ACCOUNTING_OWNER.reset(ownership)
        _REPORT_RECIPIENT.reset(reporting)


def isolated(command: list[str], request: str, budget: float) -> Decision:
    """Fresh interpreters avoid forking Python locks in threaded callers.

    The caller's deadline includes interpreter startup and every storage operation.
    Reaping a killed, uninterruptible worker never delays delivery.
    """
    started = time.monotonic()
    child = Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        output, errors = child.communicate(request, timeout=max(0, budget - (time.monotonic() - started)))
        for diagnostic in errors.splitlines():
            warn(diagnostic.removeprefix("send repeat guard: ").removesuffix("; delivery stays enabled"))
        if child.returncode != 0:
            raise ValueError(f"worker exited {child.returncode}")
        value = cast(object, json.loads(output))
        if not isinstance(value, dict):
            raise ValueError("invalid guard worker response")
        raw = cast(dict[str, object], value)
        if type(raw.get("held")) is not bool or type(raw.get("copies")) is not int:
            raise ValueError("invalid guard worker response")
        copies = cast(int, raw["copies"])
        if copies < 0:
            raise ValueError("invalid guard worker count")
        return Decision(raw["held"] is True, copies)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        warn("operation deadline exceeded")
        # Only subprocess creation is used: no Python state is forked by this caller.
        threading.Thread(target=child.communicate, daemon=True).start()
        return Decision()
    except Exception as error:
        warn(str(error))
        return Decision()


def bounded(operation: Literal["check", "event"], root: Path, attempt: Attempt, *,
            event: bool = False, outcome: str = "") -> Decision:
    request = json.dumps({"root": str(root), "attempt": asdict(attempt), "event": event, "outcome": outcome})
    return isolated([sys.executable, str(Path(__file__).resolve()), operation], request,
                    GUARD_IO_CEILING_SECONDS)


def check(root: Path, attempt: Attempt, *, event: bool = False) -> Decision:
    if attempt.protected or attempt.sender.casefold() == "user" or attempt.sender == NOTICE_SENDER or attempt.receiver == "user":
        return Decision()
    try:
        return bounded("check", root, attempt, event=event)
    except Exception as error:
        warn(str(error))
        return Decision()


def recipient_identity(address: str, *, thread_id: str = "") -> str:
    """The resolved Codex thread, or the resolved non-Codex address, is the receiver."""
    return "codex:" + thread_id if thread_id else address


def message_state() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "message"


def transport_attempt(sender: str, receiver: str, text: str, human: bool) -> Attempt:
    return Attempt(sender, receiver, text, socket.gethostname().split(".")[0], time.time(),
                   human or sender.casefold() == "user")


def transport_check(sender: str, receiver: str, text: str, *, human: bool = False) -> Decision:
    if _REPORT_RECIPIENT.get():
        print("SEND_RECIPIENT: " + receiver, file=sys.stderr)
    return check(message_state() / "repeats", transport_attempt(sender, receiver, text, human), event=_ACCOUNTING_OWNER.get())


def record_event(root: Path, attempt: Attempt, outcome: str) -> None:
    from datetime import datetime, timezone
    root.mkdir(parents=True, exist_ok=True)
    entry = {"time": datetime.fromtimestamp(attempt.at, timezone.utc).isoformat(), "machine": attempt.host,
             "from": attempt.sender, "to": attempt.receiver, "text": attempt.text, "human": attempt.protected,
             "outcome": outcome, "transport": "direct"}
    append_jsonl(root / "transports.jsonl", entry)


def append_jsonl(path: Path, entry: object, *, durable: bool = False) -> None:
    """One append owner and one nonblocking file lock for every message event."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _ = handle.write(json.dumps(entry) + "\n")
        if durable:
            handle.flush()
            os.fsync(handle.fileno())


def transport_result(sender: str, receiver: str, text: str, outcome: str, *, human: bool = False) -> None:
    if not _ACCOUNTING_OWNER.get():
        return
    attempt = transport_attempt(sender, receiver, text, human)
    try:
        _ = bounded("event", message_state(), attempt, outcome=outcome)
    except Exception as error:
        warn(str(error))


NoticeOutcome = Literal["delivered", "queued", "failed", "unknown"]


def drain(root: Path, deliver: Callable[[SenderState, Notice, Limits], NoticeOutcome], at: float, *,
          cleanup_seconds: float = 0) -> None:
    """The sender record owns one durable request per episode and notice kind.

    Unknown/interrupted delivery retries: at-least-once accepts a rare duplicate
    after provider acceptance, rather than silently losing a phone alert.
    """
    limits = configuration()
    if isinstance(limits, GuardUnavailable):
        return
    deadline = min(time.monotonic() + limits.drain_timeout_seconds,
                   float(os.environ.get("SEND_REPEAT_NOTICE_DEADLINE", "inf")))
    root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(root / "drain.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cursor = root / "cursor"
        previous = cursor.read_text() if cursor.exists() else ""
        paths = sorted(root.glob("*.json"), key=lambda path: (path.name <= previous, path.name))
        for path in paths:
            try:
                initial = decode(path.read_text())
                with sender_record(root, initial.sender) as locked_path:
                    state = decode(locked_path.read_text())
                    expire(state, at, limits)
                    if at - state.last >= limits.window_seconds and not state.notices:
                        locked_path.unlink()
                        continue
                    pending: list[Notice] = []
                    for notice in state.notices:
                        flood = next((item for item in state.floods.values()
                                      if item.first == notice.first and item.text == notice.text), None)
                        if notice.status == "claimed":
                            notice.status = "unknown"
                        if notice.status in ("pending", "unknown"):
                            if flood is not None:
                                notice.copies, notice.receivers, notice.last = flood.count, len(flood.receivers), flood.last
                            pending.append(notice)
                    write_text_atomic(locked_path, json.dumps(asdict(state)))
                for notice in pending:
                    # Progress records admitted attempts, never inspection: a budget
                    # deferral leaves the sender cursor and notice order unchanged.
                    if deadline - time.monotonic() < limits.notice_timeout_seconds + cleanup_seconds:
                        return
                    with sender_record(root, initial.sender) as locked_path:
                        latest = decode(locked_path.read_text())
                        selected = next(saved for saved in latest.notices
                                        if (saved.id, saved.kind) == (notice.id, notice.kind))
                        selected.status = "claimed"
                        latest.notices.remove(selected)
                        latest.notices.append(selected)
                        write_text_atomic(locked_path, json.dumps(asdict(latest)))
                        write_text_atomic(cursor, path.name)
                    try:
                        outcome = deliver(state, notice, limits)
                    except Exception as error:
                        warn(f"{state.sender}: {notice.kind}: {error}")
                        outcome = "unknown"
                    with sender_record(root, initial.sender) as locked_path:
                        latest = decode(locked_path.read_text())
                        for saved in latest.notices:
                            if (saved.id, saved.kind) == (notice.id, notice.kind):
                                saved.status = "pending" if outcome == "failed" else outcome
                        write_text_atomic(locked_path, json.dumps(asdict(latest)))
            except Exception as error:
                warn(f"{path.name}: {error}")
    except BlockingIOError:
        return
    finally:
        os.close(descriptor)


def notice_job(command: list[str], budget: float) -> None:
    """One supervisor owns the job group, including delivery children on both OSes."""
    child = Popen(command, start_new_session=True,
                  env={**os.environ, "SEND_REPEAT_NOTICE_JOB": "1",
                       "SEND_REPEAT_NOTICE_DEADLINE": str(time.monotonic() + budget)})
    try:
        _ = child.wait(timeout=budget)
    except subprocess.TimeoutExpired:
        warn("notice job deadline exceeded")
    finally:
        # Also clean descendants left behind after an apparently normal exit.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _ = child.wait()


def supervise_notices(command: list[str]) -> None:
    limits = configuration()
    if isinstance(limits, Limits):
        notice_job(command, limits.drain_timeout_seconds)


def worker(operation: str) -> Decision:
    """Decode the private subprocess boundary; faults never become hold decisions."""
    raw = cast(dict[str, object], json.loads(sys.stdin.read()))
    fields = cast(dict[str, object], raw["attempt"])
    attempt = Attempt(cast(str, fields["sender"]), cast(str, fields["receiver"]),
                      cast(str, fields["text"]), cast(str, fields["host"]),
                      cast(float, fields["at"]), fields["protected"] is True)
    root = Path(cast(str, raw["root"]))
    if operation == "check":
        return _stored_check(root, attempt, event=raw["event"] is True)
    if operation == "event":
        record_event(root, attempt, cast(str, raw["outcome"]))
        return Decision()
    raise ValueError("unknown guard worker operation")


if __name__ == "__main__":
    try:
        if sys.argv[1] == "notices":
            supervise_notices(sys.argv[2:])
        else:
            print(json.dumps(asdict(worker(sys.argv[1]))))
    except Exception as error:
        warn(str(error))
        print(json.dumps(asdict(Decision())))
