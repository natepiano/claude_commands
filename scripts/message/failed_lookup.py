"""Record failed recipient lookups and tell the sender's owner."""
from __future__ import annotations

import fcntl
import json
import math
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import cast

from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.message import repeat_guard, send_budget
from scripts.message.send_result import ConfirmedSendOutcome, send_outcome
from scripts.production import staff


RECORD_FILE = "failed_lookups.json"
NOTICE_INTERVAL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class NoDeliveredNotice:
    pass


@dataclass(frozen=True)
class DeliveredNotice:
    started_at: float


@dataclass(frozen=True)
class NoNoticeInProgress:
    pass


@dataclass(frozen=True)
class NoticeInProgress:
    started_at: float


@dataclass(frozen=True)
class FailuresSinceNotice:
    count: int
    last_delivery: DeliveredNotice | NoDeliveredNotice
    notice: NoticeInProgress | NoNoticeInProgress = NoNoticeInProgress()


class NoticeOutcome(Enum):
    DELIVERED = "delivered"
    FAILED = "failed"


def configured_owners() -> dict[str, str]:
    """Read sender=owner pairs from the send configuration."""
    owners: dict[str, str] = {}
    for entry in shlex.split(conf.read("send").values().get("failed_lookup_owners", "")):
        sender, separator, owner = entry.partition("=")
        if not separator or not sender or not owner:
            raise ValueError("failed_lookup_owners requires sender=owner pairs")
        owners[sender] = owner
    return owners


def notice_owner(sender: str) -> str:
    """Select the sender's director, parent showrunner, or configured owner."""
    membership = staff.snapshot()
    reference = sender.removeprefix("session:").removeprefix("codex:")
    member = membership.by_member_reference(reference) or membership.by_path(sender)
    if member is None:
        matches = [row for row in membership.find() if row.name.casefold() == sender.casefold()]
        if len(matches) > 1:
            current = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
            matches = [row for row in matches if row.current_session == current]
        if len(matches) > 1:
            raise ValueError(f"ambiguous sender staff row for {sender!r}")
        member = next(iter(matches), None)
    if member is None:
        owner = configured_owners().get(sender)
        if owner:
            return owner
    else:
        parent = membership.parent_of(member)
        if member.kind == "worker" and parent is not None:
            return str(parent.id)
        visited = {member.id}
        while parent is not None and parent.id not in visited:
            if parent.kind == "showrunner":
                return str(parent.id)
            visited.add(parent.id)
            parent = membership.parent_of(parent)
    return conf.read("production").text("UNIT_ANOMALIES_TO")


def caller_command_line() -> str:
    """Read the parent process command line once."""
    try:
        result = subprocess.run(
            ["ps", "-ww", "-p", str(os.getppid()), "-o", "command="],
            capture_output=True, text=True, check=True,
            timeout=conf.read("send").number("delivery_timeout"),
        )
        return result.stdout.strip() or "unavailable"
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


def configuration_files(command: str) -> list[str]:
    """Name only configuration paths present in the caller's arguments."""
    try:
        arguments = shlex.split(command)
    except ValueError:
        return []
    paths: list[str] = []
    takes_path = False
    for argument in arguments:
        option, separator, value = argument.partition("=")
        value = value if separator else argument
        explicit = bool(separator) and option in {"--config", "--config-file"}
        if takes_path or explicit or Path(value).suffix.casefold() == ".conf":
            if value not in paths:
                paths.append(value)
        takes_path = argument in {"--config", "--config-file"}
    return paths


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object in failed_lookups.json")
    mapping = cast(dict[object, object], value)
    if any(not isinstance(key, str) for key in mapping):
        raise ValueError("expected string keys in failed_lookups.json")
    return cast(dict[str, object], mapping)


def _read_counts(path: Path) -> dict[str, dict[str, FailuresSinceNotice]]:
    try:
        raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    records: dict[str, dict[str, FailuresSinceNotice]] = {}
    for sender, names in _mapping(raw).items():
        records[sender] = {}
        for failed_name, value in _mapping(names).items():
            fields = _mapping(value)
            count, last_notice = fields.get("count"), fields.get("last_notice")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError("invalid failure count in failed_lookups.json")
            if (not isinstance(last_notice, (float, int)) or isinstance(last_notice, bool)
                    or not math.isfinite(last_notice) or last_notice < 0):
                raise ValueError("invalid notice time in failed_lookups.json")
            started = fields.get("notice_started", 0)
            if (not isinstance(started, (float, int)) or isinstance(started, bool)
                    or not math.isfinite(started) or started < 0):
                raise ValueError("invalid notice start in failed_lookups.json")
            delivery = DeliveredNotice(float(last_notice)) if last_notice else NoDeliveredNotice()
            notice = NoticeInProgress(float(started)) if started else NoNoticeInProgress()
            records[sender][failed_name] = FailuresSinceNotice(count, delivery, notice)
    return records


@contextmanager
def _locked_counts() -> Generator[dict[str, dict[str, FailuresSinceNotice]]]:
    root = repeat_guard.message_state()
    root.mkdir(parents=True, exist_ok=True)
    path = root / RECORD_FILE
    with (root / "failed_lookups.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = _read_counts(path)
        yield records
        saved: dict[str, dict[str, dict[str, int | float]]] = {}
        for sender, names in records.items():
            saved[sender] = {}
            for target, record in names.items():
                fields: dict[str, int | float] = {
                    "count": record.count,
                    "last_notice": (record.last_delivery.started_at
                                    if isinstance(record.last_delivery, DeliveredNotice) else 0),
                }
                if isinstance(record.notice, NoticeInProgress):
                    fields["notice_started"] = record.notice.started_at
                saved[sender][target] = fields
        write_text_atomic(path, json.dumps(saved) + "\n", new_file_mode=0o600)


def _finish_notice(sender: str, failed_name: str, started_at: float, reported_count: int,
                   outcome: NoticeOutcome) -> None:
    with _locked_counts() as records:
        previous = records[sender][failed_name]
        if not isinstance(previous.notice, NoticeInProgress) or previous.notice.started_at != started_at:
            return
        if outcome == NoticeOutcome.DELIVERED:
            records[sender][failed_name] = FailuresSinceNotice(
                previous.count - reported_count, DeliveredNotice(started_at))
        else:
            records[sender][failed_name] = replace(previous, notice=NoNoticeInProgress())


def send_notice(owner: str, text: str) -> None:
    """Send through the normal path without changing the failed send's output."""
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("send.py")),
         "--to", owner, "--from", "send.py", "--text", text],
        capture_output=True, text=True, check=False, timeout=send_budget.seconds(),
    )
    outcome = send_outcome(result.returncode, result.stdout)
    if isinstance(outcome, ConfirmedSendOutcome) and outcome.header.outcome in {"sent", "queued", "kept"}:
        return
    raise RuntimeError(f"notice to {owner}: {result.stderr or result.stdout or str(result.returncode)}")


def report(sender: str, failed_name: str, reason_line: str = "") -> None:
    """Keep failures until a notice succeeds and retry failed or interrupted notices."""
    if sender == "send.py":
        return
    try:
        with conf.diagnostics(report=False):
            with _locked_counts() as records:
                failures = records.setdefault(sender, {})
                previous = failures.get(failed_name, FailuresSinceNotice(0, NoDeliveredNotice()))
                current = replace(previous, count=previous.count + 1)
                failures[failed_name] = current
                at = time.time()
                if (isinstance(current.notice, NoticeInProgress)
                        and at - current.notice.started_at < send_budget.seconds()):
                    return
                if (isinstance(current.last_delivery, DeliveredNotice)
                        and at - current.last_delivery.started_at < NOTICE_INTERVAL_SECONDS):
                    return
                count = current.count
                failures[failed_name] = replace(current, notice=NoticeInProgress(at))
            try:
                owner = notice_owner(sender)
                caller = caller_command_line()
                reason = reason_line or f"FAILED: recipient lookup failed for {failed_name!r}"
                lines = [f"Recipient lookup failed for {failed_name}.", f"Sender: {sender}",
                         f"Failed name: {failed_name}", f"Failures since the last notice: {count}", f"Caller: {caller}"]
                lines.extend(f"Config file: {config}" for config in configuration_files(caller))
                lines.append(f"Reason: {reason}")
                send_notice(owner, "\n".join(lines))
            except (Exception, SystemExit):
                _finish_notice(sender, failed_name, at, count, NoticeOutcome.FAILED)
                raise
            _finish_notice(sender, failed_name, at, count, NoticeOutcome.DELIVERED)
    except (Exception, SystemExit) as error:
        print("failed lookup notice: " + " ".join(str(error).split()), file=sys.stderr)
