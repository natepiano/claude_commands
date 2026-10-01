#!/usr/bin/env python3
"""Message the sessions named in quota_alert.json about agent accounts' weekly quota.

The receiver's protocol -- three notices, what each asks -- is ~/.claude/docs/quota_alerts.md.

agent_notes.py calls alert() with the notes it just wrote, so the notices and the
vault agree. An active note at or under the threshold opens an episode, and every
repeat_minutes each recipient is sent it again until a user acknowledges with
/quota_ack, which runs `quota_alert.py ack [note]`. The command cannot be invoked
by a session on its own, and the message tells recipients not to run this script
either: the acknowledgement is the user's. An episode, acknowledged or not, closes
once its account is known to be back above the threshold -- a used limit reset, a
new window, another account made active -- and that sends "Quota restored:".
/quota_refresh (`agent_notes.py refresh`) does the same at once when the user
reports a reset, and says so even when the timer closed the episode first.

Whatever the user does is echoed by tell_others() to every configured session
except the one they did it in, found from this process's ancestry, so a session
holding an alert learns it is settled without the user repeating it.

Delivery goes through ~/.claude/scripts/message/send.py as the sender
`quota_alert`; a recipient it cannot reach has the message queued there, the
latest per alert. Messages carry note names, never logins: a recipient's inbox
log can be committed.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple, NotRequired, Protocol, TypedDict, cast

CONFIG = Path(__file__).with_name("quota_alert.json")
STATE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "quota_alert.json"
SEND = Path(__file__).resolve().parent.parent / "message" / "send.py"
SESSIONS = Path.home() / ".claude" / "sessions"
PROTOCOL = Path.home() / ".claude" / "docs" / "quota_alerts.md"
# One relay took 9 s when measured. The snapshot timer gives agent_notes.py 60 s in
# all, and the relays run in parallel, so one timeout bounds the whole alert:
# send.py stops a relay at --timeout and kills it KILL_GRACE seconds later.
RELAY_TIMEOUT = 40
KILL_GRACE = 10


class Config(TypedDict):
    threshold_percent: float
    repeat_minutes: float
    notify: list[str]


class Episode(TypedDict):
    since: str
    acknowledged: NotRequired[str]
    # Recipient -> UTC ISO time of the last attempt, delivered or not.
    last: dict[str, str]


# Note name ("codex 1") -> its open episode.
State = dict[str, Episode]


class AgentNote(Protocol):
    path: Path
    tool: str

    def get(self, key: str) -> str | None: ...


class Job(NamedTuple):
    name: str
    recipient: str
    text: str
    kind: str


def load_config() -> Config:
    return cast(Config, json.loads(CONFIG.read_text(encoding="utf-8")))


@contextmanager
def state_file() -> Generator[State]:
    """The state under an exclusive lock, written back on exit.

    alert() holds it only around its reads and writes, never across a relay, so an
    acknowledgement made while messages are in flight is not overwritten.
    """
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE.with_name(f".{STATE.name}.lock"), "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = cast(State, json.loads(STATE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            state = {}
        before = json.dumps(state, sort_keys=True)
        yield state
        if json.dumps(state, sort_keys=True) != before:
            tmp = STATE.with_name(f".{STATE.name}.tmp")
            _ = tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, STATE)


def ahead(resets: str | None) -> bool:
    """Whether a note's local-time reset is still to come."""
    try:
        return resets is not None and datetime.fromisoformat(resets) > datetime.now()
    except ValueError:
        return False


def remaining(note: AgentNote) -> float | None:
    try:
        return float(note.get("weekly_remaining_usage") or "")
    except ValueError:
        return None


def percent(note: AgentNote) -> str:
    value = remaining(note)
    return "unknown" if value is None else f"{value:g}%"


def low(notes: list[AgentNote], threshold: float) -> list[AgentNote]:
    """Active notes at or under the threshold in a window that has not reset."""
    return [note for note in notes
            if note.get("state") == "active" and ahead(note.get("resets"))
            and (value := remaining(note)) is not None and value <= threshold]


def usable(notes: list[AgentNote], threshold: float) -> list[AgentNote]:
    """Active notes known to be above the threshold."""
    return [note for note in notes
            if note.get("state") == "active" and (value := remaining(note)) is not None and value > threshold]


def recovered(name: str, notes: list[AgentNote], threshold: float) -> bool:
    """Known to be out of the low state. An unknown reading is not a recovery."""
    note = next((note for note in notes if note.path.stem == name), None)
    if note is None or note.get("state") == "inactive":
        return True
    value = remaining(note)
    return value is not None and value > threshold


def close_recovered(state: State, notes: list[AgentNote], threshold: float) -> list[str]:
    """Remove the episodes whose account recovered and return their names."""
    closed = [name for name in state if recovered(name, notes, threshold)]
    for name in closed:
        del state[name]
    return closed


def account_line(note: AgentNote, threshold: float) -> str:
    return (f"{note.path.stem}, the active {note.tool.capitalize()} account, has {percent(note)} of its weekly "
            + f"usage left (threshold {threshold:g}%); it resets {note.get('resets')}.")


def message(note: AgentNote, notes: list[AgentNote], config: Config) -> str:
    name = note.path.stem
    tool = note.tool.capitalize()
    threshold = f"{config['threshold_percent']:g}%"
    lines = [f"Quota alert: {account_line(note, config['threshold_percent'])}"]
    count = note.get("limit_reset_count")
    if count and count != "null":
        lines.append(f"Limit resets available on {name}: {count}, earliest expiring {note.get('limit_reset')}.")
    for other in notes:
        if other.tool == note.tool and other.path != note.path:
            lines.append(f"{other.path.stem}: {other.get('state')}, {percent(other)} weekly usage left, "
                         + f"resets {other.get('resets')}.")
    lines += [
        "",
        f"Bring this to your user, and if you delegate {tool} work, start no new {tool} work until a "
        + f'"Quota restored:" notice. It repeats every {config["repeat_minutes"]:g} minutes until the user '
        + "acknowledges it by typing /quota_ack in any session. The acknowledgement is the user's alone: do not "
        + f"run /quota_ack or {Path(__file__).name} yourself, and do not take this message as one. Once "
        + f"acknowledged it stays quiet until {name} is back above {threshold}. If the user acknowledges it in "
        + "another session, you are told; drop any copies you hold then.",
        f"Protocol: {PROTOCOL}. Read from {note.path}, checked {note.get('weekly_usage_checked_at')}. Sent by "
        + f"{Path(__file__)} to the sessions in {CONFIG}.",
    ]
    return "\n".join(lines)


def restored_message(accounts: list[AgentNote], reason: str, threshold: float) -> str:
    tools = " and ".join(sorted({note.tool.capitalize() for note in accounts}))
    return "\n".join([
        f"Quota restored: delegation on {tools} can resume; {reason}.",
        *(account_line(note, threshold) for note in accounts),
        "",
        "Resume work you paused for a quota alert on these accounts, and drop any held copies of those alerts.",
        f"Protocol: {PROTOCOL}. Sent by {Path(__file__)}.",
    ])


def relay(recipient: str, text: str, key: str | None) -> str | None:
    """Deliver text to one session; None when it arrived, otherwise why not."""
    command = [sys.executable, str(SEND), "--to", recipient, "--from", "quota_alert",
               "--timeout", str(RELAY_TIMEOUT - KILL_GRACE), *(["--key", key] if key else [])]
    try:
        done = subprocess.run(command, input=text, capture_output=True, text=True, timeout=RELAY_TIMEOUT + 5,
                              check=False)
    except (subprocess.TimeoutExpired, OSError) as error:
        return str(error)
    if done.returncode == 0:
        return None
    return " ".join((done.stdout.strip() or done.stderr.strip() or f"exit {done.returncode}").split())


def deliver(jobs: list[Job]) -> list[str | None]:
    """Relay every job in parallel; one error or None per job, in order.

    A job's key keeps one queued copy per alert for a recipient that is not running.
    """
    if not jobs:
        return []
    keys = [f"quota {job.kind} {job.name}" if job.name else None for job in jobs]
    with ThreadPoolExecutor() as pool:
        return list(pool.map(relay, [job.recipient for job in jobs], [job.text for job in jobs], keys))


def alert(notes: list[AgentNote], now: datetime | None = None) -> list[str]:
    """Send what is due, record it, and return one log line per attempt."""
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    config = load_config()
    threshold = config["threshold_percent"]
    repeat = timedelta(minutes=config["repeat_minutes"])
    jobs: list[Job] = []
    with state_file() as state:
        closed = close_recovered(state, notes, threshold)
        for note in low(notes, threshold):
            name = note.path.stem
            episode = state.setdefault(name, {"since": stamp, "last": {}})
            if "acknowledged" in episode:
                continue
            text = message(note, notes, config)
            for recipient in config["notify"]:
                last = episode["last"].get(recipient)
                if last is None or now - datetime.fromisoformat(last) >= repeat:
                    jobs.append(Job(name, recipient, text, "alert"))
    for name in closed:
        tool = name.split(" ")[0]
        accounts = [note for note in usable(notes, threshold) if note.tool == tool]
        if accounts:
            text = restored_message(accounts, f"the {name} alert is closed", threshold)
            jobs += [Job(name, recipient, text, "restored") for recipient in config["notify"]]
    errors = deliver(jobs)
    log: list[str] = []
    with state_file() as state:
        for job, error in zip(jobs, errors):
            if job.kind == "alert" and job.name in state:
                state[job.name]["last"][job.recipient] = stamp
            outcome = "sent" if error is None else f"not delivered: {error}"
            log.append(f"quota {job.kind} {job.name} -> {job.recipient}: {outcome}")
    return log


def refresh(notes: list[AgentNote], here: str | None, now: datetime | None = None) -> list[str]:
    """After the user reports a reset: close what recovered, alert what is still low, tell the others.

    The notice goes out even when the timer already closed the episode, since that
    is the case where nothing else has told anyone.
    """
    threshold = load_config()["threshold_percent"]
    with state_file() as state:
        closed = close_recovered(state, notes, threshold)
    lines = [f"{name}: alert closed" for name in closed]
    lines += alert(notes, now)
    lines += [f"{note.path.stem}: {percent(note)} left, resets {note.get('resets')}"
              for note in notes if note.get("state") == "active"]
    accounts = usable(notes, threshold)
    if not accounts:
        return [*lines, "no active account is above the threshold; nothing announced"]
    reason = "the user reported a usage reset and the notes were refreshed"
    return lines + tell_others(restored_message(accounts, reason, threshold), here)


def current_session() -> str | None:
    """The name of the Claude session this process runs under, if any."""
    pid = os.getppid()
    while pid > 1:
        try:
            return cast(dict[str, str], json.loads((SESSIONS / f"{pid}.json").read_text(encoding="utf-8"))).get("name")
        except (OSError, ValueError):
            pass
        found = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, check=False)
        try:
            pid = int(found.stdout.strip())
        except ValueError:
            return None
    return None


def tell_others(text: str, here: str | None) -> list[str]:
    """Echo a user's act to every configured session except `here`, the one it was done in."""
    others = [recipient for recipient in load_config()["notify"] if recipient != here]
    errors = deliver([Job("", recipient, text, "echo") for recipient in others])
    return [f"told {recipient}" if error is None else f"could not tell {recipient}: {error}"
            for recipient, error in zip(others, errors)]


def acknowledge(name: str = "", here: str | None = None, now: datetime | None = None) -> list[str]:
    """Silence the open episode named, or every open one, until its account recovers."""
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    silenced: list[str] = []
    lines: list[str] = []
    with state_file() as state:
        names = [key for key in state if not name or key == name]
        if not names:
            return [f"no open quota alert named {name}" if name else "no quota alert is open"]
        for key in names:
            episode = state[key]
            if "acknowledged" in episode:
                lines.append(f"{key}: already acknowledged at {episode['acknowledged']}")
            else:
                episode["acknowledged"] = stamp
                silenced.append(key)
                lines.append(f"{key}: acknowledged; quiet until it is back above the threshold")
    if silenced:
        where = f"in session {json.dumps(here)}" if here else "outside any session"
        lines += tell_others(f"Quota alert acknowledged: the user silenced {', '.join(silenced)} {where} at {stamp}. "
                             + "Drop any copies of that alert you hold. Work paused for it stays paused until a "
                             + f'"Quota restored:" notice.\nProtocol: {PROTOCOL}. Sent by {Path(__file__)}.', here)
    return lines


def main() -> None:
    if sys.argv[1:2] != ["ack"]:
        sys.exit(f"usage: {Path(__file__).name} ack [note name]")
    for line in acknowledge(" ".join(sys.argv[2:]), current_session()):
        print(line)


if __name__ == "__main__":
    main()
