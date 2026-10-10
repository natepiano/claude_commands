#!/usr/bin/env python3
"""Send one message to a Claude session, a Codex seat, or the user: the one way scripts do it.

Agents holding the SendMessage tool call it directly; scripts call this. The rules
every message follows are /message (~/.claude/commands/message.md).

  send.py --to NAME|session:<id> [--from NAME] [--summary TEXT] [--key KEY [--repeat-minutes N]]
          [--machine HOST] [--codex --session-dir DIR] [--timeout SECONDS]
          (--text TEXT | --file PATH | stdin)
  send.py --to user --summary TITLE [--need note|decision|blocked] ...   the user
  send.py ack KEY       later sends with KEY are skipped
  send.py reopen KEY    forget KEY: acknowledgement and repeat window
  send.py pending [NAME]  print and clear what is kept for this session, or for NAME's

Delivery. The session-to-session channel is a tool, not a command, so a Claude
recipient is reached through a headless `claude -p` relay whose one job is a
SendMessage call. It runs sonnet in auto mode (haiku cannot run in auto mode),
with hooks, MCP and slash commands off and the caller's CLAUDE_* variables
removed, so it never passes for the session that ran this. It is named --from,
which is the sender name the recipient sees. Delivery is read from the
SendMessage tool result in the relay's stream, never from what the model says;
the last stream per recipient stays in STATE/relay/<to>.jsonl.
`--codex` queues the text on a codex_mesh.py thread instead, and `--machine`
runs this script on HOST over ssh, where ~/.claude is this repo.

`--to user` reaches the user wherever they are; `--summary` is the title. `--need`
says what they must do: `note` nothing, `decision` decide while work goes on,
`blocked` act before work can go on, repeated until they acknowledge it. How
it reaches them is `user()`'s business: no caller names the channel.

`--key` names a message. With `--repeat-minutes`, a send with the same key to the
same recipient inside the window is skipped; `ack` skips every later send with
the key until `reopen`. The window counts from the last attempt, delivered or
not, as the queue keeps what did not arrive.

(user, 2026-10-05) Outcomes are printed as one line and logged with the full
message text as one JSON line in STATE/log.jsonl:
  SENT: how                                                       exit 0
  SKIPPED: why   acknowledged, or inside the repeat window        exit 0
  QUEUED: why    a Claude recipient not reached; kept in          exit 1
                 STATE/queue/, the latest per key, under the
                 recipient's Claude session id, so a rename loses
                 nothing; under the name as given when no live
                 session answers to it, which the outcome says
  FAILED: why    not delivered and not kept: a Codex seat, which  exit 3
                 no queue reader reaches, the user, or ssh to HOST
Usage errors, an unreadable --file and an empty message exit 2. STATE is $XDG_STATE_HOME/message, or ~/.local/state/message.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, NamedTuple, NoReturn, NotRequired, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sessions  # noqa: E402

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

Outcome = Literal["sent", "skipped", "queued", "failed"]
Need = Literal["note", "decision", "blocked"]
CHANNEL_PRIORITY: dict[Need, str] = {"note": "0", "decision": "1", "blocked": "2"}
EXIT: dict[Outcome, int] = {"sent": 0, "skipped": 0, "queued": 1, "failed": 3}

LogEntry = TypedDict("LogEntry", {"time": str, "machine": str, "from": str, "to": str, "key": str | None,
                                  "summary": str, "text": str, "outcome": Outcome, "detail": str})
Queued = TypedDict("Queued", {"time": str, "from": str, "to": str, "key": str | None, "summary": str,
                              "text": str, "reason": str})


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


class Result(NamedTuple):
    outcome: Outcome
    detail: str


class RelayAttempt(NamedTuple):
    message: Message


class SessionIdWithoutLiveRecipient(NamedTuple):
    address: str


RelayPlan = RelayAttempt | SessionIdWithoutLiveRecipient


class Options(NamedTuple):
    to: str
    sender: str | None
    summary: str | None
    key: str | None
    repeat_minutes: float | None
    machine: str | None
    codex: bool
    session_dir: str | None
    timeout: float
    need: Need
    text: str | None
    file: str | None


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
    session = sessions.addressed(to, sessions.live_sessions())
    return queue_path(session_key(session) if session is not None else to)


def read_queue(path: Path) -> list[Queued]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [cast("Queued", cast("object", entry)) for line in lines if isinstance(entry := loads(line), dict)]


def enqueue(message: Message, reason: str, at: datetime) -> None:
    """Keep a message that did not arrive; a later one with the same key replaces it."""
    path = queue_for(message.to)
    entry: Queued = {"time": at.isoformat(), "from": message.sender, "to": message.to, "key": message.key,
                     "summary": message.summary, "text": message.text, "reason": reason}
    with locked():
        path.parent.mkdir(parents=True, exist_ok=True)
        kept = [old for old in read_queue(path) if message.key is None or old["key"] != message.key]
        write_atomic(path, "".join(json.dumps(item) + "\n" for item in [*kept, entry]))


def log(message: Message, result: Result, at: datetime) -> None:
    entry: LogEntry = {"time": at.isoformat(), "machine": machine(), "from": message.sender, "to": message.to,
                       "key": message.key, "summary": message.text.strip().splitlines()[0] if message.text.strip()
                       else "", "text": message.text, "outcome": result.outcome, "detail": result.detail}
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


def user(message: Message, need: Need, timeout: float) -> Result:
    """Reach the user. The channel is this function's business alone."""
    command = [sys.executable, str(USER_CHANNEL), "--priority", CHANNEL_PRIORITY[need], message.summary, message.text]
    code, out, err = run(command, "", timeout)
    if code == 0:
        return Result("sent", "to the user")
    why = one_line(err or out) or ("timed out" if code is None else f"exit {code}")
    return Result("failed", f"the user was not reached: {why}")


def remote(message: Message, host: str, options: Options) -> Result:
    """Run this script on host; its own log and queue record the attempt there."""
    forwarded = ["--to", message.to, "--from", message.sender, "--summary", message.summary,
                 "--timeout", f"{options.timeout:g}"]
    if message.key is not None:
        forwarded += ["--key", message.key]
    if message.to == USER:
        forwarded += ["--need", options.need]
    if options.session_dir is not None:
        forwarded += ["--codex", "--session-dir", options.session_dir]
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, f"{REMOTE} {shlex.join(forwarded)}"]
    code, out, err = run(command, message.text, options.timeout + KILL_GRACE + 30)
    line = (out.strip().splitlines() or [""])[-1]
    word, _, detail = line.partition(": ")
    outcome = cast(Outcome, word.lower()) if word in ("SENT", "SKIPPED", "QUEUED", "FAILED") else None
    if outcome is None:
        why = one_line(err or out) or ("timed out" if code is None else f"exit {code}")
        return Result("failed", f"ssh {host}: {why}")
    return Result(outcome, f"on {host}: {detail}")


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


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
                                     description="Send one message to a Claude session, a Codex seat or the user.",
                                     epilog="Also: send.py ack KEY | reopen KEY | pending [NAME]")
    _ = parser.add_argument("--to", required=True, help="the recipient's name as ListAgents prints it, or `user`")
    _ = parser.add_argument("--from", dest="sender", help="the sender name the recipient sees")
    _ = parser.add_argument("--summary", help="SendMessage's short label, default the first line; the title to `user`")
    _ = parser.add_argument("--need", choices=list(CHANNEL_PRIORITY), default="note",
                            help="to `user`: what they must do (default note: nothing)")
    _ = parser.add_argument("--key", help="names the message for --repeat-minutes, ack and the queue")
    _ = parser.add_argument("--repeat-minutes", type=float, help="skip a send with --key inside this window")
    _ = parser.add_argument("--machine", help="deliver from this ssh host instead (mac)")
    _ = parser.add_argument("--codex", action="store_true", help="the recipient is a codex_mesh.py thread")
    _ = parser.add_argument("--session-dir", help="the codex_mesh.py session directory")
    _ = parser.add_argument("--timeout", type=float, default=TIMEOUT, help=f"delivery seconds (default {TIMEOUT})")
    body = parser.add_mutually_exclusive_group()
    _ = body.add_argument("--text")
    _ = body.add_argument("--file")
    args = parser.parse_args(argv)
    get = {name: attr(args, name) for name in Options._fields}
    options = Options(to=as_str(get["to"]), sender=optional_str(get["sender"]), summary=optional_str(get["summary"]),
                      key=optional_str(get["key"]), repeat_minutes=optional_float(get["repeat_minutes"]),
                      machine=optional_str(get["machine"]), codex=get["codex"] is True,
                      session_dir=optional_str(get["session_dir"]), timeout=optional_float(get["timeout"]) or TIMEOUT,
                      need=cast(Need, get["need"]), text=optional_str(get["text"]), file=optional_str(get["file"]))
    if options.to == USER and (options.summary is None or options.codex):
        parser.error("--to user needs --summary, its title, and takes no --codex")
    if options.to != USER and options.need != "note":
        parser.error("--need goes with --to user")
    if options.codex != (options.session_dir is not None):
        parser.error("--codex and --session-dir go together")
    if options.repeat_minutes is not None and options.key is None:
        parser.error("--repeat-minutes needs --key")
    return options


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


def send(options: Options) -> Result:
    text = body_text(options).rstrip("\n")
    if not text.strip():
        usage_error("the message is empty")
    first = text.strip().splitlines()[0]
    summary = options.summary or (first if len(first) <= 60 else first[:59] + "…")
    message = Message(options.to, options.sender or default_sender(), summary, text, options.key)
    started = now()
    window = None if options.repeat_minutes is None else timedelta(minutes=options.repeat_minutes)
    skip = claim(message, window, started)
    if skip is not None:
        result = Result("skipped", skip)
    elif options.machine is not None:
        result = remote(message, options.machine, options)
    elif message.to == USER:
        result = user(message, options.need, options.timeout)
    elif options.session_dir is not None:
        result = codex(message, options.session_dir, options.timeout)
    else:
        plan = relay_plan(message)
        if isinstance(plan, SessionIdWithoutLiveRecipient):
            result = Result(
                "queued",
                f"no live session answers to {plan.address}, so it is kept under that name",
            )
            enqueue(message, result.detail, started)
        else:
            result = relay(plan.message, options.timeout)
            if result.outcome == "queued":
                enqueue(message, result.detail, started)
                if sessions.addressed(message.to, sessions.live_sessions()) is None:
                    result = Result("queued", f"{result.detail}; no live session answers to {message.to},"
                                    + " so it is kept under that name")
    log(message, result, started)
    return result


def main(argv: list[str]) -> int:
    match argv:
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
            result = send(parse(argv))
            print(f"{result.outcome.upper()}: {result.detail}")
            return EXIT[result.outcome]
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
