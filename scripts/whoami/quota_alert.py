#!/usr/bin/env python3
"""Message configured sessions about weekly quota.

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

Reaching the threshold switches nothing, for either tool: a Codex account whose
weekly allowance is used up may keep working on its credits, so its alert says to
keep delegating (user, 2026-10-07). switch_to_claude() moves every [assignments]
entry on codex to claude through agents_config.sh's validated editor; it is for a
caller that has seen Codex refuse work for quota, and alert() never calls it. A
later alert says what a switch moved, or why the editor refused. Once no Codex
episode is open, the entries a switch moved that are still on claude go back to
codex, and "Quota restored:" says which. Each
switch and switch-back waits in the state until the user's next prompt in OWNER,
where `notice`, a UserPromptSubmit hook in /etc/nixos/.claude/settings.json,
shows it to them. Only this machine's registry is switched.

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
import signal
import subprocess
import sys
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple, NotRequired, Protocol, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# Direct script execution and unittest discovery import this as a top-level module.
if __package__ in (None, ""):
    __package__ = "scripts.whoami"

from ..production.showrunners import (
    CONFIG as CONFIG,
    ShowrunnerSettings as ShowrunnerSettings,
    load_settings_from as load_settings_from,
)


Config = ShowrunnerSettings
STATE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "quota_alert.json"
SEND = Path(__file__).resolve().parent.parent / "message" / "send.py"
SESSIONS = Path.home() / ".claude" / "sessions"
PROTOCOL = Path.home() / ".claude" / "docs" / "quota_alerts.md"
AGENTS = Path(__file__).resolve().parent.parent / "agents" / "agents_config.sh"
# The same override agents_config.sh reads; edit_registry() passes it on explicitly.
REGISTRY = Path(os.environ.get("AGENTS_CONFIG_FILE") or Path.home() / ".claude" / "config" / "agents.conf")
# The session whose user is shown a switch on their next prompt.
OWNER = "natedev"
# One relay took 9 s when measured. The snapshot timer gives agent_notes.py 60 s in
# all, and the relays run in parallel, so one timeout bounds the whole alert:
# send.py stops a relay at --timeout and kills it KILL_GRACE seconds later.
RELAY_TIMEOUT = 40
KILL_GRACE = 10
# The whole switch took 2 s of wall time when measured idle and 8 s at load average 87; each entry
# switched back, 0.4 s idle.
EDIT_TIMEOUT = 30


class Episode(TypedDict):
    since: str
    acknowledged: NotRequired[str]
    # Why the editor refused the switch to Claude this Codex episode opened with.
    switch_failed: NotRequired[str]
    # Recipient -> UTC ISO time of the last attempt, delivered or not.
    last: dict[str, str]


class Switch(TypedDict):
    # UTC ISO time of the first switch still owed back.
    at: str
    # [assignments] keys moved from codex to claude and not yet moved back.
    moved: list[str]


class State(TypedDict):
    # Note name ("codex 1") -> its open episode.
    episodes: dict[str, Episode]
    switch: NotRequired[Switch]
    # One line per switch or switch-back the user has not been shown in OWNER.
    unseen: NotRequired[list[str]]


class SwitchBack(NamedTuple):
    moved: list[str]
    # Re-assigned by someone since the switch, so left alone.
    kept: list[str]
    # "key (why)" for each the editor refused.
    failed: list[str]

    def describe(self) -> str:
        parts = [f"moved back from Claude to Codex: {', '.join(self.moved)}" if self.moved else "",
                 f"left alone, re-assigned since the switch: {', '.join(self.kept)}" if self.kept else "",
                 f"could not move back: {', '.join(self.failed)}" if self.failed else ""]
        return "; ".join(part for part in parts if part)


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
    return load_settings_from(CONFIG)


def recipients(config: Config, here: str | None = None) -> list[str]:
    """Keep configured names even when their sessions are currently offline."""
    names = [*config["always"], *(runner["session"] for runner in config["showrunners"])]
    return list(dict.fromkeys(name for name in names if name != here))


@contextmanager
def state_file() -> Generator[State]:
    """The state under an exclusive lock, written back on exit.

    alert() holds it only around its reads, writes and registry edits, never across
    a relay, so an acknowledgement made while messages are in flight is not
    overwritten. Holding it across an edit is what makes a switch happen once.
    """
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE.with_name(f".{STATE.name}.lock"), "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state: State
        try:
            state = cast(State, json.loads(STATE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            state = {"episodes": {}}
        _ = state.setdefault("episodes", {})
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
    episodes = state["episodes"]
    closed = [name for name in episodes if recovered(name, notes, threshold)]
    for name in closed:
        del episodes[name]
    return closed


def local_time(stamp: str) -> str:
    return datetime.fromisoformat(stamp).astimezone().strftime("%Y-%m-%d %H:%M")


def registry_assignments() -> dict[str, str]:
    """[assignments] in the registry: function, or exact-task override, -> family."""
    found: dict[str, str] = {}
    section = ""
    for line in REGISTRY.read_text(encoding="utf-8").splitlines():
        text = line.split("#", 1)[0].strip()
        if text.startswith("[") and text.endswith("]"):
            section = text[1:-1]
        elif section == "assignments" and "=" in text:
            key, family = text.split("=", 1)
            found[key.strip()] = family.strip()
    return found


def edit_registry(editor: str, *args: str) -> str | None:
    """Run one of agents_config.sh's validated editors on REGISTRY; None when it took, otherwise why not.

    A timeout kills the editor's whole process group, so no orphaned mv lands after the caller has read
    the registry back.
    """
    command = ["bash", "-c", f'source "$0" && {editor} "$@"', str(AGENTS), *args]
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env={**os.environ, "AGENTS_CONFIG_FILE": str(REGISTRY)}, start_new_session=True)
    except OSError as error:
        return str(error)
    with process:
        try:
            out, err = process.communicate(timeout=EDIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            _ = process.communicate()
            return f"{editor} took longer than {EDIT_TIMEOUT} s and was stopped"
    if process.returncode == 0:
        return None
    return " ".join((err.strip() or out.strip() or f"exit {process.returncode}").split())


def switch_to_claude(state: State, note: AgentNote, stamp: str) -> str:
    """Move every [assignments] entry on codex to claude within `note`'s open episode; return the log line."""
    episode = state["episodes"][note.path.stem]
    account = f"{note.path.stem} at {percent(note)}, resets {note.get('resets')}"
    moved: list[str] = []
    try:
        on_codex = [key for key, family in registry_assignments().items() if family == "codex"]
        error = edit_registry("agents_set_all_assignments", "claude") if on_codex else None
        # The registry, not the exit status, says what moved: one mv writes the whole switch.
        moved = [key for key, family in registry_assignments().items() if key in on_codex and family == "claude"]
    except OSError as failure:
        error = str(failure)
    if not moved and error:
        episode["switch_failed"] = error
        state.setdefault("unseen", []).append(
            f"Codex ran out of quota ({account}), and the automatic switch to Claude failed at "
            + f"{local_time(stamp)}: {error}")
        return f"switch to claude for {note.path.stem} failed: {error}"
    if not moved:
        return f"switch to claude for {note.path.stem}: nothing was on codex"
    switch = state.setdefault("switch", {"at": stamp, "moved": []})
    switch["moved"] += [key for key in moved if key not in switch["moved"]]
    state.setdefault("unseen", []).append(
        f"Codex ran out of quota ({account}): {', '.join(moved)} moved to Claude at {local_time(stamp)}, "
        + "and move back when Codex recovers.")
    return f"switched to claude for {note.path.stem}: {', '.join(moved)}"


def switch_back(state: State, now: datetime) -> SwitchBack | None:
    """Once no Codex episode is open, return to codex what the switch moved; None when nothing is owed."""
    switch = state.get("switch")
    if switch is None or any(name.split(" ")[0] == "codex" for name in state["episodes"]):
        return None
    del state["switch"]
    try:
        current = registry_assignments()
    except OSError as error:
        back = SwitchBack([], [], [f"{key} ({error})" for key in switch["moved"]])
    else:
        back = SwitchBack([], [], [])
        for key in switch["moved"]:
            if current.get(key) != "claude":
                back.kept.append(key)
            elif error := edit_registry("agents_set_assignment", key, "codex"):
                back.failed.append(f"{key} ({error})")
            else:
                back.moved.append(key)
    stamp = now.isoformat(timespec="seconds")
    state.setdefault("unseen", []).append(f"Codex recovered at {local_time(stamp)}: {back.describe()}.")
    return back


def account_line(note: AgentNote, threshold: float) -> str:
    return (f"{note.path.stem}, the active {note.tool.capitalize()} account, has {percent(note)} of its weekly "
            + f"usage left (threshold {threshold:g}%); it resets {note.get('resets')}.")


def instruction(tool: str, switch: Switch | None, failed: str | None) -> str:
    """What the alert asks of its recipient, given the switch to Claude made or refused."""
    refused = f"The automatic switch from {tool} to Claude failed: {failed} " if failed else ""
    if switch is None and tool == "Codex" and not failed:
        return ("Its weekly allowance is used up or nearly so, and Codex may keep working on the account's credits, "
                + "so nothing was switched to Claude: keep delegating Codex work. Bring this to your user, and if "
                + "Codex refuses work for quota, bring that to them at once.")
    if switch is None:
        return (f"{refused}Bring this to your user, and if you delegate {tool} work, start no new {tool} work "
                + 'until a "Quota restored:" notice.')
    return (f"{refused}Every function that ran on {tool} ({', '.join(switch['moved'])}) was moved to Claude at "
            + f"{switch['at']}, so delegation continues on Claude with no pause; {tool} work already in flight may "
            + 'fail and should be run again. They move back with the "Quota restored:" notice. Bring this to your '
            + "user.")


def message(note: AgentNote, notes: list[AgentNote], config: Config, switch: Switch | None = None,
            failed: str | None = None) -> str:
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
        f"{instruction(tool, switch, failed)} It repeats every {config['repeat_minutes']:g} minutes until the user "
        + "acknowledges it by typing /quota_ack in any session. The acknowledgement is the user's alone: do not "
        + f"run /quota_ack or {Path(__file__).name} yourself, and do not take this message as one. Once "
        + f"acknowledged it stays quiet until {name} is back above {threshold}. If the user acknowledges it in "
        + "another session, you are told; drop any copies you hold then.",
        f"Protocol: {PROTOCOL}. Read from {note.path}, checked {note.get('weekly_usage_checked_at')}. Sent by "
        + f"{Path(__file__)} to the sessions in {CONFIG}.",
    ]
    return "\n".join(lines)


def restored_message(accounts: list[AgentNote], reason: str, threshold: float,
                     back: SwitchBack | None = None) -> str:
    tools = " and ".join(sorted({note.tool.capitalize() for note in accounts}))
    return "\n".join([
        f"Quota restored: delegation on {tools} can resume; {reason}.",
        *(account_line(note, threshold) for note in accounts),
        *([f"The automatic switch is undone: {back.describe()}."] if back else []),
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
    log: list[str] = []
    with state_file() as state:
        episodes = state["episodes"]
        closed = close_recovered(state, notes, threshold)
        for note in low(notes, threshold):
            name = note.path.stem
            if name not in episodes:
                episodes[name] = {"since": stamp, "last": {}}
            episode = episodes[name]
            if "acknowledged" in episode:
                continue
            switch = state.get("switch") if note.tool == "codex" else None
            text = message(note, notes, config, switch, episode.get("switch_failed"))
            for recipient in recipients(config):
                last = episode["last"].get(recipient)
                if last is None or now - datetime.fromisoformat(last) >= repeat:
                    jobs.append(Job(name, recipient, text, "alert"))
        back = switch_back(state, now)
    if back:
        log.append(f"switch to claude undone: {back.describe()}")
    for name in closed:
        tool = name.split(" ")[0]
        accounts = [note for note in usable(notes, threshold) if note.tool == tool]
        if accounts:
            text = restored_message(accounts, f"the {name} alert is closed", threshold,
                                    back if tool == "codex" else None)
            jobs += [Job(name, recipient, text, "restored") for recipient in recipients(config)]
    errors = deliver(jobs)
    with state_file() as state:
        for job, error in zip(jobs, errors):
            if job.kind == "alert" and job.name in state["episodes"]:
                state["episodes"][job.name]["last"][job.recipient] = stamp
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
        back = switch_back(state, now or datetime.now(timezone.utc))
    lines = [f"{name}: alert closed" for name in closed]
    lines += [f"switch to claude undone: {back.describe()}"] if back else []
    lines += alert(notes, now)
    lines += [f"{note.path.stem}: {percent(note)} left, resets {note.get('resets')}"
              for note in notes if note.get("state") == "active"]
    accounts = usable(notes, threshold)
    if not accounts:
        return [*lines, "no active account is above the threshold; nothing announced"]
    reason = "the user reported a usage reset and the notes were refreshed"
    return lines + tell_others(restored_message(accounts, reason, threshold, back), here)


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
    others = recipients(load_config(), here)
    errors = deliver([Job("", recipient, text, "echo") for recipient in others])
    return [f"told {recipient}" if error is None else f"could not tell {recipient}: {error}"
            for recipient, error in zip(others, errors)]


def acknowledge(name: str = "", here: str | None = None, now: datetime | None = None) -> list[str]:
    """Silence the open episode named, or every open one, until its account recovers."""
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    silenced: list[str] = []
    lines: list[str] = []
    with state_file() as state:
        episodes = state["episodes"]
        names = [key for key in episodes if not name or key == name]
        if not names:
            return [f"no open quota alert named {name}" if name else "no quota alert is open"]
        for key in names:
            episode = episodes[key]
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


def notice(here: Callable[[], str | None] = current_session) -> str | None:
    """The switches OWNER's user has not been shown, now marked seen; None when none are due here.

    It runs on every prompt of every session in /etc/nixos, so the session lookup,
    which walks the process tree, comes after a read of the state without its lock.
    """
    try:
        if not cast(State, json.loads(STATE.read_text(encoding="utf-8"))).get("unseen"):
            return None
    except (OSError, ValueError):
        return None
    if here() != OWNER:
        return None
    with state_file() as state:
        return " ".join(state.pop("unseen", [])) or None


def main() -> None:
    match sys.argv[1:2]:
        case ["ack"]:
            for line in acknowledge(" ".join(sys.argv[2:]), current_session()):
                print(line)
        case ["notice"] if len(sys.argv) == 2:
            if text := notice():
                print(json.dumps({"systemMessage": text, "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": f"The user was just shown this quota notice: {text} Mention it to them in "
                                         + "one line before anything else this turn; the gap it leaves is theirs "
                                         + "to address.",
                }}))
        case _:
            sys.exit(f"usage: {Path(__file__).name} ack [note name] | notice")


if __name__ == "__main__":
    main()
