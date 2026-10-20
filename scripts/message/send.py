#!/usr/bin/env python3
"""Send one message to a Claude session, a Codex worker, or the user: the one way scripts do it.

Agents holding the SendMessage tool call it directly; scripts call this. The rules
every message follows are /message (~/.claude/commands/message.md).

  send.py --to NAME|uds:<path>|session:<id>|worker:<run>/<worker>|codex:<thread>
          [--from NAME] [--summary TEXT] [--key KEY [--repeat-minutes N]]
          [--here | --machine HOST] [--codex --session-dir DIR] [--timeout SECONDS]
          (--text TEXT | --file PATH | stdin)
  send.py --to user --summary TITLE [--need note|decision|blocked]
          (--action TEXT | --no-action) ...                              the user
  send.py ack KEY       later sends with KEY are skipped
  send.py reopen KEY    forget KEY: acknowledgement and repeat window
  send.py pending [NAME]  print and clear what is kept for this session, or for NAME's
  send.py retire --to ADDRESS --from SENDER --key KEY
                          remove obsolete queued messages for ADDRESS

Delivery. A Claude recipient is tried through the postmaster outbox first when
its heartbeat is fresh. Every path without confirmed delivery falls back to a
headless `claude -p` relay whose one job is a SendMessage call. It runs sonnet in auto mode (haiku cannot run in auto mode),
with hooks, MCP and slash commands off and the caller's CLAUDE_* variables
removed, so it never passes for the session that ran this. It is named --from,
which is the sender name the recipient sees. Delivery is read from the
SendMessage tool result in the relay's stream, never from what the model says;
the last stream per recipient stays in STATE/relay/<to>.jsonl.
`worker:` addresses queue through codex_mesh.py; `codex:` addresses queue through
the shared Codex daemon. A unique roster name resolves to either. `--codex`
with `--session-dir` still selects codex_mesh.py directly. Names and addresses
resolve here first, then on the peer. `--here` keeps resolution here; `--machine`
runs this script on HOST over ssh, where ~/.claude is this repo.

`--to user` reaches the user wherever they are; `--summary` is the title. `--need`
says what they must do: `note` nothing, `decision` decide while work goes on,
`blocked` act before work can go on, repeated until they acknowledge it. How
it reaches them is `user()`'s business: no caller names the channel.

`--key` names a message. With `--repeat-minutes`, a send with the same key to the
same recipient inside the window is skipped; `ack` skips every later send with
the key until `reopen`. The window counts from the last attempt, delivered or
not, as the queue keeps what did not arrive.

(user, 2026-10-05) Outcomes have a header followed by any payload lines and are logged with the full
message text as one JSON line in STATE/log.jsonl:
  SENT: how                                                       exit 0
  SKIPPED: why   acknowledged, or inside the repeat window        exit 0
  QUEUED: why    a Claude recipient not reached; kept in          exit 1
                 STATE/queue/, the latest per key, under the
                 recipient's Claude session id, so a rename loses
                 nothing; under the name as given when no live
                 session answers to it, which the outcome says
  FAILED: why    not delivered and not kept: a Codex recipient,   exit 3
                 the user, or ssh to HOST
  REPLACED: why  a newer version replaced this push; not sent     exit 0
  KEPT: why      the user's silence is on; posted when it ends    exit 0
  REFUSED: why   rejected before delivery; not kept              exit 2
  HELD: not sent, failed review; revise and resend, no fallback     exit 4
Usage errors, an unreadable --file and an empty message exit 2. STATE is $XDG_STATE_HOME/message, or ~/.local/state/message.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, NoReturn, NotRequired, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "notify"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import sessions  # noqa: E402
import postmaster  # noqa: E402
import codex_daemon  # noqa: E402
if TYPE_CHECKING:
    import roster
    from ..accounts import accounts
    from ..whoami.account import named_account_environment
import review  # noqa: E402
from user_zone import user_zone  # noqa: E402
from probe_session import PROBE_ENV  # noqa: E402
from user_action import (FIX, REFUSED_BEFORE_SENDING, ActionRefused, ActionRequired, ActionUnstated,
                         UserAction, NoActionRequired, first_line, parse_user_action,
                         SessionLine, split_session, session_prefix)  # noqa: E402

STATE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "message"
CLAUDE = Path.home() / ".local" / "bin" / "claude"
SESSIONS = Path.home() / ".claude" / "sessions"
CODEX_MESH = Path(__file__).resolve().parent.parent / "agents" / "codex_mesh.py"
USER = "user"
USER_CHANNEL = Path(__file__).resolve().parent.parent / "notify" / "pushover.py"
# On the remote host: its own interpreter shim and copy of this script.
REMOTE = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/message/send.py"'
MODEL = "sonnet"
PERMISSION_MODE = "auto"
# One relay took 9 s when measured; quota_alert.py's budget is built on this.
TIMEOUT = 40
KILL_GRACE = 10

Outcome = Literal["sent", "skipped", "queued", "failed", "refused", "held", "replaced", "kept"]
Need = Literal["note", "decision", "blocked"]
CHANNEL_PRIORITY: dict[Need, str] = {"note": "0", "decision": "1", "blocked": "2"}
EXIT: dict[Outcome, int] = {"sent": 0, "skipped": 0, "queued": 1, "failed": 3, "refused": 2, "held": 4, "replaced": 0, "kept": 0}

LogEntry = TypedDict("LogEntry", {"time": str, "machine": str, "from": str, "to": str, "key": str | None,
                                  "summary": str, "text": str, "outcome": Outcome, "detail": str,
                                  "via": NotRequired[Literal["postmaster", "relay", "queue"]],
                                  "postmaster": NotRequired[Literal["delivered", "unresolved"] | postmaster.PostmasterMiss],
                                  "seconds": NotRequired[float]})
KeyedMessage = TypedDict(
    "KeyedMessage",
    {
        "time": str,
        "from": str,
        "to": str,
        "key": str,
        "summary": str,
        "text": str,
        "reason": str,
    },
)
UnkeyedMessage = TypedDict(
    "UnkeyedMessage",
    {
        "time": str,
        "from": str,
        "to": str,
        "key": None,
        "summary": str,
        "text": str,
        "reason": str,
    },
)


QueuedMessage = KeyedMessage | UnkeyedMessage


class KeyState(TypedDict):
    # Recipient -> UTC ISO time of the last attempt, delivered or not.
    last: dict[str, str]
    acknowledged: NotRequired[str]


KeyStates = dict[str, KeyState]


class Message(NamedTuple):
    to: str
    sender: str
    summary: str
    text: str
    key: str | None


@dataclass(frozen=True)
class NoHeldRevision:
    pass


@dataclass(frozen=True)
class HeldRevision:
    source: str
    identity: str
    machine: str = ""


RevisionHold = NoHeldRevision | HeldRevision
NO_HELD_REVISION = NoHeldRevision()


@dataclass(frozen=True)
class FailedPush:
    title: str
    action: UserAction
    body: str
    hold: RevisionHold = NO_HELD_REVISION
    reviewed: bool = False
    need: Need = "note"
    held: bool = False

    @property
    def fallback_allowed(self) -> bool:
        return (self.reviewed and not self.held) or self.need == "blocked"

    @property
    def message(self) -> str:
        session, body = split_session(self.body)
        return first_line(self.action, session) + "\n" + body

    @property
    def text(self) -> str:
        line = " ".join((self.title + " — " + first_line(self.action, split_session(self.body)[0])).split())
        if len(line) <= 200:
            return line
        shortened = line[:199]
        if not line[199].isspace() and " " in shortened:
            shortened = shortened.rsplit(" ", 1)[0]
        return shortened.rstrip() + "…"


@dataclass(frozen=True)
class NoFailedPush:
    pass


FailureText = FailedPush | NoFailedPush
NO_FAILED_PUSH = NoFailedPush()


class ReviewedPushRecord(TypedDict):
    title: str
    action: str | None
    body: str
    held: NotRequired[bool]
    held_source: NotRequired[str]
    held_identity: NotRequired[str]
    held_machine: NotRequired[str]


class FailedPushRecord(ReviewedPushRecord):
    time: float
    message: str
    fallback: str
    reviewed: bool
    need: Need


class FailedPushTransportRecord(ReviewedPushRecord):
    reviewed: bool
    need: Need


UNREVIEWED_FALLBACK = ("This push was never reviewed, so it cannot go out as a fallback. "
    "Bring it to the user in your reply, or resend it with send.py once the failure is fixed.")


HELD_FALLBACK = "This push was not sent because it failed review. Revise it and resend it with send.py; no fallback is allowed."


def failure_detail(reason: str, failed: FailedPush) -> str:
    if failed.held and failed.need != "blocked":
        return reason.partition(" fallback text:")[0] + " " + HELD_FALLBACK
    reason = reason.partition(" fallback text:")[0].partition(" " + UNREVIEWED_FALLBACK)[0]
    return reason + (" fallback text:\n" + failed.text if failed.fallback_allowed else " " + UNREVIEWED_FALLBACK)


class Result(NamedTuple):
    outcome: Outcome
    detail: str
    failure: FailureText = NO_FAILED_PUSH


@dataclass(frozen=True)
class NoClaudeRoute:
    pass


@dataclass(frozen=True)
class ClaudeRoute:
    via: Literal["postmaster", "relay", "queue"]
    postmaster: Literal["delivered", "unresolved"] | postmaster.PostmasterMiss
    seconds: float


Route = NoClaudeRoute | ClaudeRoute
NO_CLAUDE_ROUTE = NoClaudeRoute()


class RelayAttempt(NamedTuple):
    message: Message


class SessionIdWithoutLiveRecipient(NamedTuple):
    address: str


RelayPlan = RelayAttempt | SessionIdWithoutLiveRecipient


@dataclass(frozen=True)
class NoUserRecipient:
    pass


MessageUserAction = UserAction | NoUserRecipient


@dataclass(frozen=True)
class ClaudeRecipient:
    address: str


@dataclass(frozen=True)
class CodexWorkerRecipient:
    address: str
    name: str
    session_dir: str


@dataclass(frozen=True)
class CodexSessionRecipient:
    address: str
    thread_id: str
    name: str


SessionRecipient = ClaudeRecipient | CodexWorkerRecipient | CodexSessionRecipient


@dataclass(frozen=True)
class UserDelivery:
    """Deliver through the user-notification gate."""


@dataclass(frozen=True)
class RemoteDelivery:
    host: str
    address: str


@dataclass(frozen=True)
class RecipientLookupFailed:
    reason: str


DeliveryRoute = SessionRecipient | UserDelivery | RemoteDelivery | RecipientLookupFailed


@dataclass(frozen=True)
class LocalOnly:
    """Resolve and deliver only here."""


@dataclass(frozen=True)
class LocalThenPeer:
    """Ask the peer only after proving this machine has no match."""


@dataclass(frozen=True)
class NamedMachine:
    host: str


RecipientResolutionScope = LocalOnly | LocalThenPeer | NamedMachine
LOCAL_THEN_PEER = LocalThenPeer()


@dataclass(frozen=True)
class RosterLookup:
    to: str


@dataclass(frozen=True)
class MeshWorker:
    session_dir: str
    worker: str


RecipientSelection = RosterLookup | MeshWorker


class Options(NamedTuple):
    selection: RecipientSelection
    sender: str | None
    summary: str | None
    key: str | None
    repeat_minutes: float | None
    scope: RecipientResolutionScope
    timeout: float
    need: Need
    action: MessageUserAction
    text: str | None
    file: str | None
    answerable: bool = False
    text_id: str = ""
    values: tuple[str, ...] = ()

    @property
    def to(self) -> str:
        return self.selection.to if isinstance(self.selection, RosterLookup) else self.selection.worker


# ---------------------------------------------------------------------------
# State: one lock guards the key states and the queues. It is never held across
# a delivery, so a slow relay blocks nobody.
# ---------------------------------------------------------------------------


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def machine() -> str:
    return socket.gethostname().split(".")[0]


def file_name(name: str) -> str:
    return "".join(char if char.isalnum() or char in "._- " else "_" for char in name) or "_"


def loads(text: str) -> object:
    """json.loads as an `object`, so callers narrow before use."""
    try:
        return json.loads(text)  # pyright: ignore[reportAny]
    except ValueError:
        return None


def as_dict(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def as_list(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    _ = tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def locked() -> Generator[None]:
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / ".lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


@contextmanager
def key_states() -> Generator[KeyStates]:
    """The key states, under the lock, written back on exit when changed."""
    path = STATE / "keys.json"
    with locked():
        try:
            states = cast(KeyStates, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            states = {}
        before = json.dumps(states, sort_keys=True)
        yield states
        if json.dumps(states, sort_keys=True) != before:
            write_atomic(path, json.dumps(states, indent=2, sort_keys=True) + "\n")


def queue_path(key: str) -> Path:
    return STATE / "queue" / f"{file_name(key)}.jsonl"


def session_key(session: sessions.SessionRecord) -> str:
    return f"session-{session['sessionId']}"


def queue_for(to: str) -> Path:
    """Where a message that did not reach `to` is kept.

    Under the Claude session id of the live session `to` means, so the session finds it whatever it
    is called later. Under `to` itself when no live session answers to it: there is no id to look up.
    """
    if to.startswith("session:") and len(to) > len("session:"):
        return queue_path(f"session-{to.removeprefix('session:')}")
    session = sessions.addressed(to, sessions.live_sessions())
    return queue_path(session_key(session) if session is not None else to)


def _queued_message(value: object) -> QueuedMessage | None:
    values = as_dict(value)
    string_fields = ("time", "from", "to", "summary", "text", "reason")
    if not all(isinstance(values.get(field), str) for field in string_fields):
        return None
    key = values.get("key")
    if key is None:
        return cast(UnkeyedMessage, cast(object, values))
    if isinstance(key, str):
        return cast(KeyedMessage, cast(object, values))
    return None


def read_queue(path: Path) -> list[QueuedMessage]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    messages = (_queued_message(loads(line)) for line in lines)
    return [message for message in messages if message is not None]


def enqueue(message: Message, reason: str, at: datetime) -> None:
    """Keep a message that did not arrive; a later one with the same key replaces it."""
    path = queue_for(message.to)
    if message.key is None:
        entry: QueuedMessage = UnkeyedMessage(
            time=at.isoformat(),
            to=message.to,
            key=None,
            summary=message.summary,
            text=message.text,
            reason=reason,
            **{"from": message.sender},
        )
    else:
        entry = KeyedMessage(
            time=at.isoformat(),
            to=message.to,
            key=message.key,
            summary=message.summary,
            text=message.text,
            reason=reason,
            **{"from": message.sender},
        )
    with locked():
        path.parent.mkdir(parents=True, exist_ok=True)
        kept = [old for old in read_queue(path) if message.key is None or old["key"] != message.key]
        write_atomic(path, "".join(json.dumps(item) + "\n" for item in [*kept, entry]))


def _rewrite_queue(path: Path, kept: list[QueuedMessage]) -> None:
    if kept:
        write_atomic(path, "".join(json.dumps(item) + "\n" for item in kept))
    else:
        path.unlink(missing_ok=True)


def _queue_paths_for_address(to: str) -> list[Path]:
    return list(dict.fromkeys((queue_for(to), queue_path(to))))


def remove_queued_key(to: str, key: str) -> int:
    """Remove a delivered keyed message's queued predecessor."""
    paths = _queue_paths_for_address(to)
    removed = 0
    with locked():
        for path in paths:
            entries = read_queue(path)
            kept = [entry for entry in entries if entry["key"] != key]
            _rewrite_queue(path, kept)
            removed += len(entries) - len(kept)
    return removed


def retire(to: str, sender: str, key: str) -> int:
    """Remove obsolete shutdown messages queued for one address."""
    paths = _queue_paths_for_address(to)
    removed = 0
    with locked():
        for path in paths:
            entries = read_queue(path)
            kept = [
                entry
                for entry in entries
                if not (
                    entry["key"] == key
                    or (entry["key"] is None and entry["from"] == sender)
                )
            ]
            _rewrite_queue(path, kept)
            removed += len(entries) - len(kept)
    return removed


def log(message: Message, result: Result, at: datetime, route: Route = NO_CLAUDE_ROUTE) -> None:
    entry: LogEntry = {"time": at.isoformat(), "machine": machine(), "from": message.sender, "to": message.to,
                       "key": message.key, "summary": message.text.strip().splitlines()[0] if message.text.strip()
                       else "", "text": message.text, "outcome": result.outcome, "detail": result.detail}
    if isinstance(route, ClaudeRoute):
        entry["via"] = route.via
        entry["postmaster"] = route.postmaster
        entry["seconds"] = round(route.seconds, 3)
    STATE.mkdir(parents=True, exist_ok=True)
    with locked(), open(STATE / "log.jsonl", "a", encoding="utf-8") as handle:
        _ = handle.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def claim(message: Message, repeat: timedelta | None, at: datetime) -> str | None:
    """Why this send is skipped, or None after recording it as the key's latest attempt.

    Recording before delivery, under the lock, keeps two overlapping runs with
    one key from both sending.
    """
    if message.key is None:
        return None
    with key_states() as states:
        state = states.setdefault(message.key, {"last": {}})
        if "acknowledged" in state:
            return f"key {message.key!r} acknowledged at {state['acknowledged']}; `send.py reopen` to resume"
        last = state["last"].get(message.to)
        if repeat is not None and last is not None and at - datetime.fromisoformat(last) < repeat:
            minutes = repeat.total_seconds() / 60
            return f"key {message.key!r} last sent to {message.to} at {last}, inside its {minutes:g}-minute window"
        state["last"][message.to] = at.isoformat()
    return None


def acknowledge(key: str) -> str:
    with key_states() as states:
        state = states.setdefault(key, {"last": {}})
        if "acknowledged" in state:
            return f"{key}: already acknowledged at {state['acknowledged']}"
        state["acknowledged"] = now().isoformat()
    return f"{key}: acknowledged; sends with it are skipped until `send.py reopen {shlex.quote(key)}`"


def reopen(key: str) -> str:
    with key_states() as states:
        if states.pop(key, None) is None:
            return f"{key}: no record; nothing to reopen"
    return f"{key}: reopened; its next send goes out"


def pending(to: str | None) -> str:
    """Print and clear what is kept for a session: the one `to` means, or the caller's own.

    It is found by session id. What was kept under a name is found too: the name `to`, the session's
    name now, and each name it once had that no live session has now.
    """
    records = sessions.live_sessions()
    if to is None:
        own = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        session = next((record for record in records if own and record["sessionId"] == own), None)
        if session is None:
            raise ValueError("pending without a name runs inside a live Claude session")
    else:
        session = sessions.addressed(to, records)
    keys = [] if to is None else [to]
    if session is not None:
        taken = {record["name"] for record in records if record["sessionId"] != session["sessionId"]}
        keys = [session_key(session), session["name"], f"uds:{session['messagingSocketPath']}",
                *(name for name in session["formerNames"] if name not in taken), *keys]
    paths = list(dict.fromkeys(queue_path(key) for key in keys if key))
    with locked():
        entries = sorted((entry for path in paths for entry in read_queue(path)), key=lambda entry: entry["time"])
        for path in paths:
            path.unlink(missing_ok=True)
    return "\n\n".join(f"Queued message {index} of {len(entries)}, from {entry['from']} at {entry['time']}:\n"
                       + entry["text"].rstrip() for index, entry in enumerate(entries, 1))


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def _print_roster_problems(problems: list[str]) -> None:
    for problem in problems:
        print(f"send.py: roster: {problem}", file=sys.stderr)


def _messageable_recipient(entry: roster.RosterEntry) -> SessionRecipient:
    import roster

    if not isinstance(entry.reach, roster.Messageable):
        raise AssertionError("a messageable recipient needs an address")
    address = entry.reach.address
    if address.startswith("worker:"):
        if not isinstance(entry.assignment, roster.WorkerAssignment):
            usage_error(f"{address} is not assigned to a Codex worker")
        return CodexWorkerRecipient(address, entry.name, entry.assignment.session_dir)
    if address.startswith("codex:"):
        return CodexSessionRecipient(
            address,
            address.removeprefix("codex:"),
            entry.name,
        )
    return ClaudeRecipient(address)


def _cannot_receive_message(entry: roster.RosterEntry) -> NoReturn:
    import roster

    usage_error(roster.cannot_receive_message_text(entry))


def _ambiguous_name(name: str, entries: list[roster.RosterEntry]) -> NoReturn:
    import roster

    lines = [f"{name} matches {len(entries)} sessions:"]
    for entry in entries:
        recipient = (
            entry.reach.address
            if isinstance(entry.reach, roster.Messageable)
            else f"(cannot take messages: {entry.reach.reason})"
        )
        lines.append(f"  {recipient} ({entry.role})")
    usage_error("\n".join(lines))


def _recipient_matches(to: str, entries: list[roster.RosterEntry]) -> list[roster.RosterEntry]:
    import roster

    if to.startswith(("session:", "worker:", "codex:")):
        return [entry for entry in entries
                if isinstance(entry.reach, roster.Messageable) and entry.reach.address == to]
    return [entry for entry in entries if entry.name == to]


def _matched_recipient(to: str, matches: list[roster.RosterEntry]) -> SessionRecipient:
    import roster

    if len(matches) > 1:
        _ambiguous_name(to, matches)
    entry = matches[0]
    if isinstance(entry.reach, roster.CannotReceiveMessages):
        _cannot_receive_message(entry)
    return _messageable_recipient(entry)


def resolve_recipient(
    to: str, scope: RecipientResolutionScope = LOCAL_THEN_PEER,
) -> DeliveryRoute:
    """Resolve locally first; a remote row supplies the address sent to its machine."""
    if to.startswith("uds:"):
        return ClaudeRecipient(to)
    if isinstance(scope, NamedMachine):
        return RemoteDelivery(scope.host, to)
    if to.startswith("codex:"):
        thread_id = to.removeprefix("codex:")
        if not thread_id:
            usage_error("codex: needs a thread id")
    # Acknowledgements, explicit sockets and named-machine sends need no local roster.
    import roster

    result = roster.roster(os.environ, roster.ThisMachine())
    _print_roster_problems(result.problems)
    matches = _recipient_matches(to, result.entries)
    if matches:
        return _matched_recipient(to, matches)
    addressed = to.startswith(("session:", "worker:", "codex:"))
    if isinstance(scope, LocalThenPeer):
        if result.problems and not addressed:
            usage_error(f"absence of {to} here is not proven; use an address or --machine")
        peer = roster.peer_roster(os.environ)
        _print_roster_problems(peer.problems)
        matches = _recipient_matches(to, peer.entries)
        if matches:
            recipient = _matched_recipient(to, matches)
            return RemoteDelivery(roster.remote.other_machine(), recipient.address)
        if peer.problems:
            return RecipientLookupFailed("; ".join(peer.problems))
    if to.startswith("worker:"):
        usage_error(f"no roster session has address {to}")
    if to.startswith("codex:"):
        return CodexSessionRecipient(to, to.removeprefix("codex:"), to.removeprefix("codex:"))
    return ClaudeRecipient(to)


def run(command: list[str], stdin: str, timeout: float, env: dict[str, str] | None = None,
        cwd: Path | None = None) -> tuple[int | None, str, str]:
    """Run command; (exit code or None on timeout, stdout, stderr).

    A timeout stops the whole process group, so a relay's children go with it.
    """
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, env=env, cwd=cwd, start_new_session=True) as process:
        try:
            out, err = process.communicate(stdin, timeout=timeout)
            return process.returncode, out, err
        except subprocess.TimeoutExpired:
            for sig, wait in ((signal.SIGTERM, KILL_GRACE), (signal.SIGKILL, None)):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
                try:
                    out, err = process.communicate(timeout=wait)
                    return None, out, err
                except subprocess.TimeoutExpired:
                    continue
            return None, "", ""


def relay_prompt(message: Message) -> str:
    lines = [
        "You are a relay that delivers one message. Make exactly one SendMessage call:",
        f"- to: {json.dumps(message.to)}",
        f"- summary: {json.dumps(message.summary)}",
        "- message: the text between the BEGIN and END lines below, copied exactly. Keep every character, space, "
        + "line break, backtick and placeholder as written. Add nothing, drop nothing, reword nothing. Leave out "
        + "the BEGIN and END lines.",
    ]
    if not message.to.startswith("uds:"):
        lines.append("If that call fails because the name matched no session or more than one, call ListAgents once, "
                     + f"then make the call once more with `to` set to the row whose name is exactly "
                     + f"{json.dumps(message.to)}, followed by its [ref]. If no row has that name, stop.")
    lines += ["Then stop: reply with one line, the SendMessage result. Call no other tool.",
              "----- BEGIN MESSAGE -----", message.text, "----- END MESSAGE -----"]
    return "\n".join(lines)


def one_line(text: str, limit: int = 300) -> str:
    return " ".join(text.split())[:limit]


def result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    return " ".join(as_str(as_dict(part).get("text")) for part in as_list(content))


def describe(answer: str) -> str:
    """A SendMessage result as one line: its message and msg_id when it is JSON."""
    fields = as_dict(loads(answer))
    said = as_str(fields.get("message")) or answer
    msg_id = as_str(fields.get("msg_id"))
    return one_line(f"{said} (msg_id {msg_id})" if msg_id else said)


def read_relay(stdout: str, text: str) -> Result:
    """Judge a relay by its SendMessage calls and their results, not by its words."""
    events = [as_dict(loads(line)) for line in stdout.splitlines()]
    mode = next((as_str(event.get("permissionMode")) for event in events
                 if event.get("type") == "system" and event.get("subtype") == "init"), "unknown")
    contents = [as_dict(part) for event in events if event.get("type") in ("assistant", "user")
                for part in as_list(as_dict(event.get("message")).get("content"))]
    sends = [part for part in contents if part.get("type") == "tool_use" and part.get("name") == "SendMessage"]
    results = {as_str(part.get("tool_use_id")): part for part in contents if part.get("type") == "tool_result"}
    denials = sum(len(as_list(event.get("permission_denials"))) for event in events if event.get("type") == "result")
    flags = [] if mode == PERMISSION_MODE else [f"MODE {mode}, NOT {PERMISSION_MODE}"]
    if denials:
        flags.append(f"{denials} permission denials")
    if len(sends) > 1:
        flags.append(f"{len(sends)} sends")
    failures: list[str] = []
    for send in sends:
        result = results.get(as_str(send.get("id")))
        if result is None:
            failures.append("SendMessage returned no result")
            continue
        answer = result_text(result.get("content"))
        if result.get("is_error") is True or as_dict(loads(answer)).get("success") is False:
            failures.append(describe(answer))
            continue
        message = as_str(as_dict(send.get("input")).get("message"))
        verbatim = "verbatim" if message.rstrip() == text.rstrip() else "NOT VERBATIM"
        return Result("sent", "; ".join([f"mode {mode}", verbatim, *flags, describe(answer)]))
    if failures:
        return Result("queued", "; ".join([*flags, failures[-1]]))
    said = next((as_str(event.get("result")) for event in reversed(events) if event.get("type") == "result"), "")
    return Result("queued", "; ".join([*flags, f"the relay made no SendMessage call: {one_line(said) or 'no output'}"]))


def claude_binary() -> str | None:
    if os.access(CLAUDE, os.X_OK):
        return str(CLAUDE)
    return shutil.which("claude")


def relay(message: Message, timeout: float) -> Result:
    claude = claude_binary()
    if claude is None:
        return Result("queued", "no claude binary found")
    tools = "SendMessage" if message.to.startswith("uds:") else "SendMessage,ListAgents"
    command = [claude, "-p", "--model", MODEL, "--permission-mode", PERMISSION_MODE, "--permission-prompts", "none",
               "--tools", tools, "--allowedTools", tools,
               "--setting-sources", "user", "--settings", json.dumps({"disableAllHooks": True}),
               "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
               "-n", message.sender, "--output-format", "stream-json", "--verbose"]
    # An inherited CLAUDE_* variable makes the relay pass for the caller's session:
    # CLAUDE_CODE_MESSAGING_SOCKET, for one, is that session's inbox.
    env = {name: value for name, value in os.environ.items() if not name.startswith("CLAUDE")}
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    if not TYPE_CHECKING:
        from scripts.accounts import accounts
        from scripts.whoami.account import named_account_environment

    try:
        selected = accounts.account_for_unscoped_launch(os.environ)
    except accounts.AccountsError as error:
        return Result("queued", str(error))
    if not isinstance(selected, accounts.LegacyHomeLaunch):
        env.update(named_account_environment(selected.dir))
    # A Mac keeps Claude's login in its keychain, which ssh cannot open; the user's
    # `claude setup-token` token, encrypted behind gpg-agent, stands in for it.
    token_file = Path.home() / ".config" / "claude" / "oauth-token.gpg"
    if token_file.exists():
        token = subprocess.run(["gpg", "--pinentry-mode", "error", "--quiet", "--decrypt", str(token_file)],
                               capture_output=True, text=True, timeout=10, check=False)
        if token.returncode != 0:
            return Result("queued", f"gpg-agent is cold on {machine()}: run github-warmup there")
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token.stdout.strip()
    code, out, err = run(command, relay_prompt(message), timeout, env, Path.home())
    raw = STATE / "relay" / f"{file_name(message.to)}.jsonl"
    raw.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(raw, out + err)
    if code is None:
        return Result("queued", f"relay timed out after {timeout:g} s")
    result = read_relay(out, message.text)
    if result.outcome == "queued" and code != 0:
        return Result("queued", f"relay exit {code}: {result.detail}; {one_line(err, 200)}")
    return result


def relay_plan(message: Message) -> RelayPlan:
    """Resolve a session-id address locally while preserving the caller's message."""
    if not message.to.startswith("session:"):
        return RelayAttempt(message)
    session = sessions.addressed(message.to, sessions.live_sessions())
    if session is None:
        return SessionIdWithoutLiveRecipient(message.to)
    return RelayAttempt(
        Message(
            f"uds:{session['messagingSocketPath']}",
            message.sender,
            message.summary,
            message.text,
            message.key,
        )
    )


def codex(message: Message, session_dir: str, timeout: float) -> Result:
    command = [sys.executable, str(CODEX_MESH), "send", "--session-dir", session_dir, "--to", message.to,
               "--message", message.text]
    code, out, err = run(command, "", timeout)
    if code == 0:
        return Result("sent", one_line(out) or "queued on the codex thread")
    why = one_line(err or out) or ("timed out" if code is None else f"exit {code}")
    return Result("failed", f"codex_mesh.py send: {why}")


def codex_session(
    message: Message,
    recipient: CodexSessionRecipient,
    timeout: float,
) -> Result:
    queued = codex_daemon.queue(recipient.thread_id, message.text, timeout=timeout)
    if isinstance(queued, codex_daemon.Queued):
        return Result("sent", f"queued on Codex session {recipient.name}")
    if isinstance(queued, codex_daemon.QueueTimedOut):
        return Result("failed", f"timed out after {queued.seconds:g} s")
    return Result("failed", queued.reason)


def action_arguments(action: UserAction) -> list[str]:
    if isinstance(action, ActionRequired):
        return ["--action", action.text]
    return ["--no-action"]


def reviewed_failure(stdout: str, original: FailedPush, *,
                     prefix: str = "pushover: reviewed JSON: ", machine: str = "",
                     remote_record: bool = False) -> FailedPush:
    for line in stdout.splitlines():
        if not line.startswith(prefix):
            continue
        record = as_dict(loads(line.removeprefix(prefix)))
        title, body = record.get("title"), record.get("body")
        action = parse_user_action(optional_str(record.get("action")), record.get("action") is None)
        if isinstance(title, str) and isinstance(body, str) and isinstance(action, (ActionRequired, NoActionRequired)):
            source = record.get("held_source")
            identity = record.get("held_identity")
            hold: RevisionHold = (HeldRevision(source, identity, machine)
                                  if isinstance(source, str) and source and isinstance(identity, str) and identity
                                  else NO_HELD_REVISION)
            reviewed = record.get("reviewed") is True if remote_record else True
            need = record.get("need") if remote_record else original.need
            if need not in ("note", "decision", "blocked"):
                need = original.need
            session = record.get("session")
            if isinstance(session, str):
                body = session_prefix(SessionLine(session)) + body
            return FailedPush(title, action, body, hold, reviewed=reviewed, need=need, held=record.get("held") is True)
        return original
    return original


def user(message: Message, need: Need, action: UserAction, timeout: float, *, answerable: bool = False, text_id: str = "", values: tuple[str, ...] = ()) -> Result:
    """Reach the user. The channel is this function's business alone."""
    command = [sys.executable, str(USER_CHANNEL), "--priority", CHANNEL_PRIORITY[need], "--source", message.sender,
               *(["--answerable"] if answerable else []), *action_arguments(action), message.summary, message.text]
    if text_id:
        command = [sys.executable, str(USER_CHANNEL), "--priority", CHANNEL_PRIORITY[need], "--source", message.sender,
                   "--text-id", text_id]
        for value in values:
            command.extend(["--value", value])
    code, out, err = run(command, "", timeout)
    if code == 6:
        failed = reviewed_failure(out, FailedPush(message.summary, action, message.text, need=need, held=True))
        return Result("held", "not sent, failed review: " + (err.removeprefix("pushover: not sent: ").strip() or "review failed"), failed)
    if code == 5:
        return Result("replaced", "a newer version replaced this push; nothing was sent")
    if code == 4:
        return Result("held", err.removesuffix("\n") or out.removesuffix("\n"))
    if code == 0:
        kept = next((line.removeprefix("pushover: kept: ") for line in out.splitlines()
                     if line.startswith("pushover: kept: ")), "")
        for line in out.splitlines():
            if line.startswith("pushover: reviewed JSON: "):
                record = as_dict(loads(line.removeprefix("pushover: reviewed JSON: ")))
                if record.get("sent_despite_failed_review") is True:
                    if text_id:
                        template_fix_notice(record)
                    rules = record.get("failed_rules", [])
                    if not kept:
                        return Result("sent", "to the user, sent despite failed review: " + str(rules))
        if kept:
            return Result("kept", f"for the user: {kept}; it goes out when silence ends")
        before, separator, sent = out.partition("pushover: sent as rewritten:\n")
        del before
        return Result("sent", "to the user, rewritten:\n" + sent.removesuffix("\n") if separator else "to the user")
    why = one_line(err or out) or ("timed out" if code is None else f"exit {code}")
    if code == 2 and f"{REFUSED_BEFORE_SENDING}:" in err:
        return Result("refused", why)
    failed = reviewed_failure(out, FailedPush(message.summary, action, message.text, need=need))
    return Result("failed", failure_detail(f"the user was not reached: {why}", failed), failed)


class TemplateFixErrorRecord(TypedDict):
    time: str
    text_id: str
    error: str


def template_fix_notice(record: dict[str, object]) -> None:
    """Launch one session notice per template per day without waiting for another claim."""
    text_id = as_str(record.get("text_id"))
    if not text_id:
        return
    directory = review.state_path() / "template-fixes"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        text_key = hashlib.sha256(text_id.encode()).hexdigest()
        key = now().astimezone(user_zone()).date().isoformat() + "-" + text_key
        stamp = directory / key
        with (directory / (text_key + ".lock")).open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            if stamp.exists():
                return
            _ = stamp.write_text(text_id, encoding="utf-8")
        recipient = "natedev"
        try:
            for line in review.NOTIFY_CONFIG.read_text(encoding="utf-8").splitlines():
                name, separator, value = line.partition("#")[0].partition("=")
                if separator and name.strip() == "TEMPLATE_FIX_SESSION" and value.strip():
                    recipient = value.strip()
        except OSError:
            pass
        text = "Fix registry push " + text_id + " today: it was sent despite failed review.\n" + json.dumps(record, ensure_ascii=False)
        command = [sys.executable, str(Path(__file__).resolve()), "template-fix-deliver", text_id, recipient, text]
        # Delivery and its error record run in the detached child.
        _ = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as error:
        print(f"send.py: template fix notice failed for {text_id}: {error}", file=sys.stderr)
        log_template_fix_error(text_id, str(error))


def log_template_fix_error(text_id: str, error: str) -> None:
    try:
        review.state_path().mkdir(parents=True, exist_ok=True)
        with (review.state_path() / "template-fix-errors.jsonl").open("a", encoding="utf-8") as output:
            entry: TemplateFixErrorRecord = {"time": now().isoformat(), "text_id": text_id, "error": error}
            _ = output.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def deliver_template_fix(text_id: str, recipient: str, text: str) -> None:
    """Record recipient refusals as well as transport failures, with no retry."""
    command = [sys.executable, str(Path(__file__).resolve()), "--to", recipient, "--from", "push template review",
               "--summary", "Fix push template " + text_id, "--text", text]
    try:
        code, out, err = run(command, "", TIMEOUT + 40)
        if code != 0:
            log_template_fix_error(text_id, (err or out).strip() or ("timed out" if code is None else f"exit {code}"))
    except OSError as error:
        log_template_fix_error(text_id, str(error))


def remote(message: Message, host: str, options: Options) -> Result:
    """Run this script on host; its own log and queue record the attempt there."""
    forwarded = ["--here", "--to", message.to, "--from", message.sender, "--summary", message.summary,
                 "--timeout", f"{options.timeout:g}"]
    if message.key is not None:
        forwarded += ["--key", message.key]
    if message.to == USER:
        forwarded += ["--need", options.need]
        if options.text_id:
            forwarded += ["--text-id", options.text_id]
            for value in options.values:
                forwarded += ["--value", value]
        if options.answerable:
            forwarded += ["--answerable"]
        if isinstance(options.action, NoUserRecipient):
            raise AssertionError("a user message must state its action")
        forwarded += action_arguments(options.action)
    if isinstance(options.selection, MeshWorker):
        forwarded += ["--codex", "--session-dir", options.selection.session_dir]
    if options.repeat_minutes is not None:
        forwarded += ["--repeat-minutes", f"{options.repeat_minutes:g}"]
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, f"{'CLAUDE_PID= ' if message.to == USER else ''}{REMOTE} {shlex.join(forwarded)}"]
    code, out, err = run(command, message.text, options.timeout + KILL_GRACE + 30)
    failure: FailureText = NO_FAILED_PUSH
    if message.to == USER and "send.py: failed push JSON: " in err:
        if isinstance(options.action, NoUserRecipient):
            raise AssertionError("a user message must state its action")
        failure = reviewed_failure(err, FailedPush(message.summary, options.action, message.text, need=options.need),
                                   prefix="send.py: failed push JSON: ", machine=host, remote_record=True)
    lines = out.removesuffix("\n").split("\n")
    headers = [(index, line.partition(": ")) for index, line in enumerate(lines)
               if line.partition(": ")[0] in ("SENT", "HELD", "SKIPPED", "QUEUED", "FAILED", "REFUSED", "REPLACED", "KEPT")]
    if not headers:
        why = one_line(err or out) or ("timed out" if code is None else f"exit {code}")
        return Result("held" if isinstance(failure, FailedPush) and failure.held else "failed", f"ssh {host}: {why}", failure)
    index, (word, _, detail) = headers[0]
    payload = "\n".join(lines[index + 1:])
    if payload:
        detail += "\n" + payload
    return Result(cast(Outcome, word.lower()), f"on {host}: {detail}", failure)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def user_failure(message: Message, action: MessageUserAction, result: Result, need: Need) -> Result:
    if isinstance(action, NoUserRecipient):
        raise AssertionError("a user message must state its action")
    failed = result.failure
    if isinstance(failed, NoFailedPush):
        failed = FailedPush(message.summary, action, message.text, need=need)
    result = Result("held" if failed.held else "failed", failure_detail(result.detail, failed), failed)
    try:
        pid = os.environ.get("CLAUDE_PID", "")
        if not pid:
            return Result(result.outcome, result.detail, failed)
        record = as_dict(loads((SESSIONS / f"{pid}.json").read_text(encoding="utf-8")))
        session_id = as_str(record.get("sessionId"))
        if session_id and Path(session_id).name == session_id and session_id not in (".", ".."):
            entry: FailedPushRecord = {"time": time.time(), "title": failed.title,
                "action": failed.action.text if isinstance(failed.action, ActionRequired) else None,
                "body": failed.body, "message": failed.message, "fallback": failed.text,
                "reviewed": failed.reviewed, "need": failed.need, "held": failed.held}
            if isinstance(failed.hold, HeldRevision):
                entry["held_source"] = failed.hold.source
                entry["held_identity"] = failed.hold.identity
                entry["held_machine"] = failed.hold.machine
            directory = Path(os.environ.get("NOTIFY_STATE_DIR", str(Path.home() / ".local/state/notify"))) / "failed"
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / ".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                write_atomic(directory / f"{session_id}.json", json.dumps(entry))
    except OSError as error:
        print(f"send.py: could not write fallback marker: {error}", file=sys.stderr)
    return Result(result.outcome, result.detail, failed)


def default_sender() -> str:
    """send.py, and the Claude session it runs under when there is one."""
    try:
        record = as_dict(loads((SESSIONS / f"{os.environ['CLAUDE_PID']}.json").read_text(encoding="utf-8")))
    except (KeyError, OSError):
        return "send.py"
    name = as_str(record.get("name"))
    return f"send.py in {name}" if name else "send.py"


def attr(args: argparse.Namespace, name: str) -> object:
    return getattr(args, name)  # pyright: ignore[reportAny]


def optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def parse(argv: list[str]) -> Options:
    parser = argparse.ArgumentParser(prog="send.py",
                                     description=(
                                         "Send one message to a roster name, uds:<path>, "
                                         "session:<id>, worker:<run id>/<worker name>, "
                                         "codex:<thread id>, or user."
                                     ),
                                     epilog="Also: send.py ack KEY | reopen KEY | pending [NAME]")
    _ = parser.add_argument(
        "--to",
        required=True,
        help=(
            "a roster name, uds:<path>, session:<id>, "
            "worker:<run id>/<worker name>, codex:<thread id>, or user"
        ),
    )
    _ = parser.add_argument("--text-id", default="", help="registered script push")
    _ = parser.add_argument("--value", action="append", default=[], help="registry template key=value")
    _ = parser.add_argument("--answerable", action="store_true", help=argparse.SUPPRESS)
    _ = parser.add_argument("--from", dest="sender", help="the sender name the recipient sees")
    _ = parser.add_argument("--summary", help="SendMessage's short label, default the first line; the title to `user`")
    _ = parser.add_argument("--priority", choices=("0", "1", "2"), help=argparse.SUPPRESS)
    _ = parser.add_argument("--need", choices=list(CHANNEL_PRIORITY), default="note",
                            help="to `user`: what they must do (default note: nothing)")
    _ = parser.add_argument("--action", help="to `user`: what the user does")
    _ = parser.add_argument("--no-action", action="store_true", help="to `user`: nothing is needed")
    _ = parser.add_argument("--key", help="names the message for --repeat-minutes, ack and the queue")
    _ = parser.add_argument("--repeat-minutes", type=float, help="skip a send with --key inside this window")
    location = parser.add_mutually_exclusive_group()
    _ = location.add_argument("--machine", help="deliver from this ssh host instead (mac)")
    _ = location.add_argument("--here", action="store_true", help="resolve and deliver only here")
    _ = parser.add_argument("--codex", action="store_true", help="the recipient is a codex_mesh.py thread")
    _ = parser.add_argument("--session-dir", help="the codex_mesh.py session directory")
    _ = parser.add_argument("--timeout", type=float, default=TIMEOUT, help=f"delivery seconds (default {TIMEOUT})")
    body = parser.add_mutually_exclusive_group()
    _ = body.add_argument("--text")
    _ = body.add_argument("--file")
    args = parser.parse_args(argv)
    to = as_str(attr(args, "to"))
    text_id = as_str(attr(args, "text_id"))
    raw_values = cast(list[str], attr(args, "value"))
    if text_id:
        if to != USER:
            parser.error("--text-id goes with --to user")
        import push_texts
        values: dict[str, str] = {}
        for value in raw_values:
            key, separator, item = value.partition("=")
            if not separator or key in values:
                parser.error("--value needs distinct key=value pairs")
            values[key] = item
        try:
            rendered = push_texts.render(text_id, **values)
        except ValueError as error:
            parser.error(str(error))
        args.summary, args.text = rendered.title, session_prefix(rendered.session) + rendered.body
        args.need = "blocked" if as_str(attr(args, "need")) == "blocked" else rendered.need
        args.action = rendered.action.text if isinstance(rendered.action, ActionRequired) else None
        args.no_action = isinstance(rendered.action, NoActionRequired)
        args.sender = optional_str(attr(args, "sender")) or rendered.sender
    elif raw_values:
        parser.error("--value needs --text-id")
    priority = optional_str(attr(args, "priority"))
    if priority is not None:
        args.need = {"0": "note", "1": "decision", "2": "blocked"}[priority]
    action_text = optional_str(attr(args, "action"))
    no_action = attr(args, "no_action") is True
    if to != USER:
        if attr(args, "answerable") is True:
            parser.error("--answerable goes with --to user")
        if action_text is not None or no_action:
            parser.error("--action and --no-action go with --to user")
        action: MessageUserAction = NoUserRecipient()
    else:
        parsed_action = parse_user_action(action_text, no_action)
        if isinstance(parsed_action, ActionRefused):
            parser.error(f"{parsed_action.reason}. {FIX}")
        if isinstance(parsed_action, ActionUnstated):
            parser.error(FIX)
        action = parsed_action
    session_dir = optional_str(attr(args, "session_dir"))
    is_codex = attr(args, "codex") is True
    if is_codex != (session_dir is not None):
        parser.error("--codex and --session-dir go together")
    selection: RecipientSelection = MeshWorker(session_dir, to) if session_dir is not None else RosterLookup(to)
    host = optional_str(attr(args, "machine"))
    scope: RecipientResolutionScope = (
        NamedMachine(host) if host is not None else
        LocalOnly() if attr(args, "here") is True else LocalThenPeer()
    )
    get = {name: attr(args, name) for name in Options._fields if name not in {"action", "selection", "scope", "values"}}
    options = Options(selection=selection, sender=optional_str(get["sender"]), summary=optional_str(get["summary"]),
                      key=optional_str(get["key"]), repeat_minutes=optional_float(get["repeat_minutes"]),
                      scope=scope, timeout=optional_float(get["timeout"]) or TIMEOUT,
                      need=cast(Need, get["need"]), action=action, text=optional_str(get["text"]),
                      file=optional_str(get["file"]), answerable=get["answerable"] is True, text_id=text_id, values=tuple(raw_values))
    if options.to == USER and (options.summary is None or isinstance(options.selection, MeshWorker)):
        parser.error("--to user needs --summary, its title, and takes no --codex")
    if options.to != USER and options.need != "note":
        parser.error("--need goes with --to user")
    if options.to == USER and options.need != "note" and not isinstance(options.action, ActionRequired):
        parser.error(f"--need {options.need} requires --action. {FIX}")
    if options.repeat_minutes is not None and options.key is None:
        parser.error("--repeat-minutes needs --key")
    return options


class RetireRequest(NamedTuple):
    to: str
    sender: str
    key: str


def parse_retire(argv: list[str]) -> RetireRequest:
    parser = argparse.ArgumentParser(prog="send.py retire")
    _ = parser.add_argument("--to", required=True)
    _ = parser.add_argument("--from", dest="sender", required=True)
    _ = parser.add_argument("--key", required=True)
    args = parser.parse_args(argv)
    return RetireRequest(
        as_str(attr(args, "to")),
        as_str(attr(args, "sender")),
        as_str(attr(args, "key")),
    )


def usage_error(text: str) -> NoReturn:
    print(f"send.py: {text}", file=sys.stderr)
    sys.exit(2)


def body_text(options: Options) -> str:
    if options.text is not None:
        return options.text
    if options.file is not None:
        try:
            return Path(options.file).read_text(encoding="utf-8")
        except OSError as error:
            usage_error(f"cannot read --file: {error}")
    if sys.stdin.isatty():
        usage_error("give the message with --text, --file or stdin")
    return sys.stdin.read()


def delivered_text(message: Message, entry_id: str) -> str:
    return f"Message from {message.sender} via postmaster: {message.summary} [{entry_id[:8]}]\n\n{message.text}"


def send(options: Options) -> Result:
    if options.to == USER:
        options = options._replace(timeout=max(options.timeout, review.user_send_seconds()))
    text = body_text(options).rstrip("\n")
    if not text.strip():
        usage_error("the message is empty")
    first = text.strip().splitlines()[0]
    summary = options.summary or (first if len(first) <= 60 else first[:59] + "…")
    if isinstance(options.selection, MeshWorker):
        route: DeliveryRoute = (
            RemoteDelivery(options.scope.host, options.to) if isinstance(options.scope, NamedMachine)
            else CodexWorkerRecipient(options.to, options.to, options.selection.session_dir)
        )
    elif options.to == USER:
        route = (RemoteDelivery(options.scope.host, USER) if isinstance(options.scope, NamedMachine)
                 else UserDelivery())
    else:
        route = resolve_recipient(options.to, options.scope)
    destination = route.address if isinstance(route, SessionRecipient | RemoteDelivery) else options.to
    sender = options.sender or default_sender()
    if options.to == USER and options.sender is None and sender != "send.py":
        options = options._replace(answerable=True)
    message = Message(destination, sender, summary, text, options.key)
    started = now()
    window = None if options.repeat_minutes is None else timedelta(minutes=options.repeat_minutes)
    skip = claim(message, window, started)
    log_route: Route = NO_CLAUDE_ROUTE
    if skip is not None:
        result = Result("skipped", skip)
    elif isinstance(route, RecipientLookupFailed):
        result = Result("failed", route.reason)
    elif isinstance(route, RemoteDelivery):
        result = remote(message, route.host, options)
    elif isinstance(route, UserDelivery):
        if isinstance(options.action, NoUserRecipient):
            raise AssertionError("a user message must state its action")
        result = user(message, options.need, options.action, options.timeout, answerable=options.answerable, text_id=options.text_id, values=options.values)
    elif isinstance(route, CodexWorkerRecipient):
        transport = Message(
            route.name,
            message.sender,
            message.summary,
            message.text,
            message.key,
        )
        result = codex(transport, route.session_dir, options.timeout)
    elif isinstance(route, CodexSessionRecipient):
        result = codex_session(message, route, options.timeout)
    else:
        claude_started = time.monotonic()
        def relay_and_queue(relay_message: Message, timeout: float) -> Result:
            relayed_result = relay(relay_message, timeout)
            if relayed_result.outcome == "queued":
                enqueue(message, relayed_result.detail, started)
                if sessions.addressed(message.to, sessions.live_sessions()) is None:
                    return Result("queued", f"{relayed_result.detail}; no live session answers to {message.to},"
                                  + " so it is kept under that name")
            return relayed_result

        plan = relay_plan(message)
        if isinstance(plan, SessionIdWithoutLiveRecipient):
            result = Result(
                "queued",
                f"no live session answers to {plan.address}, so it is kept under that name",
            )
            enqueue(message, result.detail, started)
            log_route = ClaudeRoute("queue", "unresolved", time.monotonic() - claude_started)
        else:
            live = sessions.live_sessions()
            registered = sessions.addressed(plan.message.to, live)
            postmaster_recipient: postmaster.PostmasterRecipient = (
                postmaster.RecipientNotInRegistry() if registered is None
                else postmaster.RegisteredClaudeRecipient(registered)
            )
            if isinstance(postmaster_recipient, postmaster.RegisteredClaudeRecipient):
                entry_id = postmaster.new_entry_id()
                delivery = postmaster.attempt(STATE, postmaster_recipient.session, message.sender, entry_id,
                                              delivered_text(message, entry_id), live)
                if isinstance(delivery, postmaster.Delivered):
                    result = Result("sent", f"postmaster; {'verbatim' if delivery.verbatim else 'NOT VERBATIM'}; "
                                    + f"session:{postmaster_recipient.session['sessionId']}")
                    log_route = ClaudeRoute("postmaster", "delivered", time.monotonic() - claude_started)
                else:
                    result = relay_and_queue(plan.message, max(1.0, options.timeout - delivery.seconds))
                    log_route = ClaudeRoute("relay", delivery.reason, time.monotonic() - claude_started)
            else:
                result = relay_and_queue(plan.message, options.timeout)
                log_route = ClaudeRoute("relay", "unresolved", time.monotonic() - claude_started)
    if result.outcome == "sent" and message.key is not None:
        for recipient in dict.fromkeys((message.to, options.to)):
            _ = remove_queued_key(recipient, message.key)
    if message.to == USER and (result.outcome == "failed" or (result.outcome == "held" and isinstance(result.failure, FailedPush))):
        result = user_failure(message, options.action, result, options.need)
    log(message, result, started, log_route)
    return result


def main(argv: list[str]) -> int:
    match argv:
        case ["template-fix", failure_json]:
            template_fix_notice(as_dict(loads(failure_json)))
        case ["template-fix-deliver", text_id, recipient, text]:
            deliver_template_fix(text_id, recipient, text)
        case ["retire", *arguments]:
            request = parse_retire(arguments)
            removed = retire(request.to, request.sender, request.key)
            print(f"retired {removed} queued message{'s' if removed != 1 else ''}")
        case ["ack", key]:
            print(acknowledge(key))
        case ["reopen", key]:
            print(reopen(key))
        case ["pending", *named] if len(named) <= 1:
            try:
                text = pending(named[0] if named else None)
            except ValueError as error:
                usage_error(str(error))
            if text:
                print(text)
        case ["ack" | "reopen" | "pending", *_]:
            usage_error(f"usage: send.py {argv[0]} {'[NAME]' if argv[0] == 'pending' else 'KEY'}")
        case _:
            options = parse(argv)
            if options.to == USER and os.environ.get(PROBE_ENV) == "1":
                print("not sent: probe sessions never push to the phone")
                return 0
            result = send(options)
            print(f"{result.outcome.upper()}: {result.detail}")
            if isinstance(result.failure, FailedPush):
                failed = result.failure
                record: FailedPushTransportRecord = {"title": failed.title,
                    "action": failed.action.text if isinstance(failed.action, ActionRequired) else None,
                    "body": failed.body, "reviewed": failed.reviewed, "need": failed.need, "held": failed.held}
                if isinstance(failed.hold, HeldRevision):
                    record["held_source"] = failed.hold.source
                    record["held_identity"] = failed.hold.identity
                print("send.py: failed push JSON: " + json.dumps(record), file=sys.stderr)
            return EXIT[result.outcome]
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
