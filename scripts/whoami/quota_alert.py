#!/usr/bin/env python3
"""Message configured sessions and the user about weekly quota.

The receiver's protocol -- three notices, what each asks -- is ~/.claude/docs/quota_alerts.md.

agent_notes.py calls alert() with the notes it just wrote, so the notices and the
vault agree. An active note at or under the threshold opens an episode, and every
repeat_minutes each recipient is sent it again until a user acknowledges with
/quota_ack, which runs `quota_alert.py ack [note]`. The command cannot be invoked
by a session on its own, and the message tells recipients not to run this script
either: the acknowledgement is the user's. An episode, acknowledged or not, closes
once its account is known to be back above the threshold -- a used limit reset, a
new window -- and that sends "Quota restored:". In multi-account mode, making
a Claude or Codex account spare preserves its episode and acknowledgement until its own
quota recovers; legacy account switches retain their existing behavior.
/quota_refresh (`agent_notes.py refresh`) does the same at once when the user
reports a reset, and says so even when the timer closed the episode first.

Opening an episode also messages the user directly once. A failed user delivery
stays due and the two-minute notes timer retries it; acknowledging the session
alert does not suppress it. Codex refusing work sends a second direct message once
per episode, retried only by another refusal after a failed delivery. Episodes
created before direct delivery have no due marker and do not send old news.

Reaching the threshold switches nothing, for either tool: a Codex account whose
weekly allowance is used up may keep working on its credits, so its alert says to
keep delegating (user, 2026-10-07). The switch waits for Codex to refuse work:
codex_mesh.py hands the serving account, limit window and time to `agent_notes.py blocked`.
A known 5-hour limit resetting in less than 20 minutes waits once; other refusals pick
another Codex account with room and move idle seats through the seats-swap path.
Only when no Codex account has room are Codex assignments switched to Claude.
A refusal never opens a weekly episode. A late refusal from an outgoing account
records that account's refusal and changes no assignments. Owed assignments return
when the selected account has room and its refusal is resolved. Each
switch and switch-back waits in the state until the user's next prompt in OWNER,
where `notice`, a UserPromptSubmit hook in /etc/nixos/.claude/settings.json,
shows it to them. Only this machine's registry is switched.

Whatever the user does is echoed by tell_others() to every configured session
except the one they did it in, found from this process's ancestry, so a session
holding an alert learns it is settled without the user repeating it.

Delivery goes through ~/.claude/scripts/message/send.py as the sender
`quota_alert`; it reaches both sessions and the user's configured channel. A
session it cannot reach has the message queued there, the latest per alert.
Messages carry note names, never logins: a recipient's inbox log can be committed.
"""

from __future__ import annotations

import fcntl
import asyncio
import copy
import json
import math
import os
import signal
import subprocess
import sys
import uuid
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, NotRequired, Protocol, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.user_zone import user_zone
from scripts.whoami import agent_accounts
from scripts.accounts import moves, refusals
import usage_store

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "notify"))
import review

from note_file import reset_time
from agent_accounts import (ClaudeAccountsInUse, CodexHomeSource, Report,
                            codex_can_work, CodexWorkAvailable, codex_email_on_disk,
                            codex_homes, codex_live, seats_home)

if TYPE_CHECKING:
    from ..notify.push_texts import RenderedPush
    from scripts.accounts import accounts
    from scripts.accounts.accounts import CodexSeatsAccount


@dataclass(frozen=True)
class CodexHomeReading:
    home: Path
    report: Report
    observed_at: datetime


@dataclass(frozen=True)
class CodexAlertPassReadings:
    seats: CodexSeatsAccount
    selected_account: agent_accounts.RefusingCodexAccount
    checked_homes: tuple[CodexHomeReading, ...]


@dataclass(frozen=True)
class ReadCurrentCodexReadings:
    """A standalone call collects one selection's live readings."""


READ_CURRENT_CODEX_READINGS = ReadCurrentCodexReadings()
CODEX_READING_MAX_AGE = timedelta(minutes=10)
UNKNOWN_REFUSAL_WINDOW = refusals.UnknownWindow()


def codex_alert_pass(home: Path, reports: list[Report], observed_at: datetime) -> CodexAlertPassReadings:
    from scripts.accounts.accounts import CodexSeatsAccount

    label = agent_accounts.codex_home_role_label(home)
    return CodexAlertPassReadings(
        CodexSeatsAccount(label, home), agent_accounts.codex_account(home),
        tuple(CodexHomeReading(report.source.home, copy.deepcopy(report), observed_at)
              for report in reports if isinstance(report.source, CodexHomeSource)),
    )


def resolve_codex_readings(value: CodexAlertPassReadings | ReadCurrentCodexReadings) -> CodexAlertPassReadings:
    if isinstance(value, CodexAlertPassReadings):
        return value
    home = seats_home()
    async def collect() -> list[Report]:
        return list(await asyncio.gather(*(codex_live(folder) for folder in codex_homes(home)
                                          if folder.is_dir() and codex_email_on_disk(folder))))
    return codex_alert_pass(home, asyncio.run(collect()), datetime.now(timezone.utc))


def selected_codex_report(readings: CodexAlertPassReadings, now: datetime) -> Report:
    identity = readings.selected_account
    if isinstance(identity, agent_accounts.KnownCodexAccount):
        for checked in readings.checked_homes:
            if (checked.home == readings.seats.home and checked.home == identity.home
                    and checked.report.email and checked.report.email.casefold() == identity.login.casefold()
                    and timedelta(0) <= now - checked.observed_at <= CODEX_READING_MAX_AGE):
                return checked.report
    return Report("Codex", problem="the selected account's current reading is unknown")


def codex_swap_line(readings: CodexAlertPassReadings, now: datetime) -> str:
    """Describe the refusal destination without asking the user to swap seats."""
    from scripts.accounts import accounts

    chosen = accounts.pick("codex", now, exclude=frozenset({readings.seats.label}), keep=accounts.NoKeep())
    if isinstance(chosen, accounts.PickedAccount):
        return f"seats move to {chosen.label} when refused"
    return "no other Codex account has room until " + reset_display(chosen.earliest_reset)


def reset_display(reset: refusals.KnownReset | refusals.NoResetKnown | accounts.EarliestReset) -> str:
    from scripts.accounts import accounts

    return (reset.at.astimezone(user_zone()).strftime("%a %Y-%m-%d %H:%M %Z")
            if isinstance(reset, (refusals.KnownReset, accounts.EarliestReset)) else "an unknown reset")


def _save_codex_readings(readings: CodexAlertPassReadings, now: datetime) -> None:
    held = usage_store.load()
    changed = False
    for checked in readings.checked_homes:
        if (not checked.report.problem and not checked.report.quota_problem
                and timedelta(0) <= now - checked.observed_at <= CODEX_READING_MAX_AGE):
            if checked.report.email:
                previous = held.get(usage_store.key("Codex", checked.report.email), {})
                if "weekly" in previous:
                    try:
                        if datetime.fromisoformat(previous["weekly"]["at"]) > checked.observed_at:
                            continue
                    except ValueError:
                        pass
            changed = usage_store.merge(held, [checked.report], checked.observed_at) or changed
    if changed:
        try:
            usage_store.write(held)
        except (OSError, ValueError) as error:
            print(f"usage store: {error}", file=sys.stderr)


def _reading_for_account(readings: CodexAlertPassReadings, identity: agent_accounts.KnownCodexAccount,
                         now: datetime) -> refusals.FreshReading | refusals.StaleReading | refusals.NoReading:
    reading = refusals.stored_reading("codex", identity.login, now)
    if isinstance(reading, refusals.FreshReading):
        for checked in readings.checked_homes:
            if (checked.home == identity.home and checked.report.email
                    and checked.report.email.casefold() == identity.login.casefold()
                    and not checked.report.problem and not checked.report.quota_problem
                    and timedelta(0) <= now - checked.observed_at <= CODEX_READING_MAX_AGE
                    # The store drops fractions of a second; recover this pass's exact observation time.
                    and reading.weekly.at == checked.observed_at.replace(microsecond=0)):
                return replace(reading, weekly=replace(reading.weekly, at=checked.observed_at),
                    five_hour=replace(reading.five_hour, at=checked.observed_at)
                    if isinstance(reading.five_hour, refusals.ReadingWindow) else reading.five_hour)
    return reading


def _refusal_window(window: refusals.KnownWindow | refusals.UnknownWindow,
                    reading: refusals.FreshReading | refusals.StaleReading | refusals.NoReading,
                    at: datetime) -> refusals.KnownWindow | refusals.UnknownWindow:
    if isinstance(window, refusals.KnownWindow):
        return window
    if isinstance(reading, refusals.FreshReading):
        exhausted = [name for name, value in (("weekly", reading.weekly), ("five_hour", reading.five_hour))
                     if isinstance(value, refusals.ReadingWindow) and value.at > at and value.left == 0]
        if len(exhausted) == 1:
            return refusals.KnownWindow(cast(Literal["weekly", "five_hour"], exhausted[0]))
    return refusals.UnknownWindow()


def _finish_seats_transitions(now: datetime) -> None:
    """The timer or a resumed seat closes each expired wait once."""
    entries = moves.read_moves()
    ended = {entry["move_id"] for entry in entries if entry["kind"] == "wait ended"}
    for entry in entries:
        if entry["tool"] != "codex" or entry["subject"] != "seats" or entry["kind"] != "waited" or entry["move_id"] in ended:
            continue
        reset = datetime.fromisoformat(entry["reset"])
        if reset <= now:
            moves.append_move(moves.WaitEndedMove(kind="wait ended", at=now.isoformat(), tool="codex",
                                                 subject="seats", move_id=entry["move_id"]))
            ended.add(entry["move_id"])


def _room_again(now: datetime) -> None:
    from scripts.accounts import accounts

    entries = moves.read_moves()
    closed = {entry["move_id"] for entry in entries if entry["kind"] == "room again"}
    pending = [entry for entry in entries if entry["tool"] == "codex" and entry["subject"] == "seats"
               and entry["kind"] == "no room" and entry["move_id"] not in closed]
    if pending:
        chosen = accounts.pick("codex", now, keep=accounts.NoKeep())
        if isinstance(chosen, accounts.PickedAccount):
            for entry in pending:
                moves.append_move(moves.RoomAgainMove(kind="room again", at=now.isoformat(), tool="codex",
                    subject="seats", move_id=entry["move_id"], label=chosen.label))


@dataclass(frozen=True)
class ReadCurrentClaudeSelection:
    """A standalone alert call resolves its account selection before doing any work."""


READ_CURRENT_CLAUDE_SELECTION = ReadCurrentClaudeSelection()


def _selection(value: ClaudeAccountsInUse | ReadCurrentClaudeSelection) -> ClaudeAccountsInUse:
    if isinstance(value, ReadCurrentClaudeSelection):
        # Resolve only standalone calls; injected selections are already fixed.
        from scripts.accounts import accounts

        return accounts.claude_account_selection()
    return value


from scripts.production.showrunners import (
    CONFIG as CONFIG,
    Showrunner as Showrunner,
    ShowrunnerSettings as ShowrunnerSettings,
    load_settings_from as load_settings_from,
    registered_showrunners as registered_showrunners,
    socket_for as socket_for,
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
    weekly_reset: NotRequired[str]
    acknowledged: NotRequired[str]
    # "due" until the first low-quota user message succeeds, then its UTC ISO time.
    push: NotRequired[str]
    # UTC ISO time of the successful Codex-refusal user message.
    refusal_push: NotRequired[str]
    # Why the editor refused the switch to Claude this Codex episode opened with.
    switch_failed: NotRequired[str]
    # Recipient -> UTC ISO time of the last attempt, delivered or not.
    last: dict[str, str]


@dataclass(frozen=True)
class NoCodexQuotaFallback:
    """No assignments are owed back to Codex."""


@dataclass(frozen=True)
class AccountCodexQuotaFallback:
    owner: agent_accounts.KnownCodexAccount
    at: str
    moved: tuple[str, ...]


@dataclass(frozen=True)
class UnknownOwnerCodexQuotaFallback:
    """A fallback saved before its refusing account was recorded."""

    at: str
    moved: tuple[str, ...]


CodexQuotaFallback = NoCodexQuotaFallback | AccountCodexQuotaFallback | UnknownOwnerCodexQuotaFallback
NO_CODEX_QUOTA_FALLBACK = NoCodexQuotaFallback()


class KnownRefusalNotice(TypedDict):
    kind: Literal["known"]
    login: str
    label: str
    push: NotRequired[str]
    switch_failed: NotRequired[str]
    failure_pushes: NotRequired[list[str]]


class UnknownRefusalNotice(TypedDict):
    kind: Literal["unknown"]


RefusalNotice = KnownRefusalNotice | UnknownRefusalNotice


class State(TypedDict):
    # Note name ("codex 1") -> its open episode.
    episodes: dict[str, Episode]
    refusal_notices: NotRequired[dict[str, RefusalNotice]]
    fallback: CodexQuotaFallback
    # One line per switch or switch-back the user has not been shown in OWNER.
    unseen: NotRequired[list[str]]


class StoredFallback(TypedDict):
    at: str
    moved: list[str]
    owner: NotRequired[agent_accounts.StoredCodexAccount]


class StoredState(TypedDict):
    episodes: dict[str, Episode]
    refusal_notices: NotRequired[dict[str, RefusalNotice]]
    switch: NotRequired[StoredFallback]
    unseen: NotRequired[list[str]]


def _read_fallback(value: object) -> CodexQuotaFallback:
    if not isinstance(value, dict):
        return NoCodexQuotaFallback()
    record = cast(dict[str, object], value)
    at, moved = record.get("at"), record.get("moved")
    if not isinstance(at, str) or not isinstance(moved, list):
        return NoCodexQuotaFallback()
    entries = tuple(item for item in cast(list[object], moved) if isinstance(item, str))
    owner = record.get("owner")
    if isinstance(owner, dict):
        fields = cast(dict[str, object], owner)
        home, login, label = fields.get("home"), fields.get("login"), fields.get("label")
        if isinstance(home, str) and isinstance(login, str) and isinstance(label, str):
            return AccountCodexQuotaFallback(agent_accounts.KnownCodexAccount(Path(home), login, label), at, entries)
    return UnknownOwnerCodexQuotaFallback(at, entries)


def _stored_state(state: State) -> StoredState:
    saved = StoredState(episodes=state["episodes"])
    if "refusal_notices" in state:
        saved["refusal_notices"] = state["refusal_notices"]
    if "unseen" in state:
        saved["unseen"] = state["unseen"]
    fallback = state["fallback"]
    if not isinstance(fallback, NoCodexQuotaFallback):
        record = StoredFallback(at=fallback.at, moved=list(fallback.moved))
        if isinstance(fallback, AccountCodexQuotaFallback):
            record["owner"] = agent_accounts.StoredCodexAccount(home=str(fallback.owner.home),
                login=fallback.owner.login, label=fallback.owner.label)
        saved["switch"] = record
    return saved


@dataclass(frozen=True)
class NoRestorationAccount:
    """A description assembled outside an actual Codex account restoration."""


NO_RESTORATION_ACCOUNT = NoRestorationAccount()


class SwitchBack(NamedTuple):
    moved: list[str]
    # Re-assigned by someone since the switch, so left alone.
    kept: list[str]
    # "key (why)" for each the editor refused.
    failed: list[str]
    restored_account: agent_accounts.KnownCodexAccount | NoRestorationAccount = NO_RESTORATION_ACCOUNT

    def describe(self) -> str:
        parts = [f"moved back from Claude to Codex: {', '.join(self.moved)}" if self.moved else "",
                 f"left alone, re-assigned since the switch: {', '.join(self.kept)}" if self.kept else "",
                 f"could not move back: {', '.join(self.failed)}" if self.failed else ""]
        return "; ".join(part for part in parts if part)


@dataclass(frozen=True)
class NoCodexFallbackRestoration:
    """Nothing is owed, or the selected account cannot yet work."""


CodexFallbackRestoration = NoCodexFallbackRestoration | SwitchBack
NO_CODEX_FALLBACK_RESTORATION = NoCodexFallbackRestoration()


class AgentNote(Protocol):
    path: Path
    tool: str

    def get(self, key: str) -> str | None: ...


class Job(NamedTuple):
    name: str
    recipient: str
    text: str
    kind: str


class Ask(NamedTuple):
    """What a direct user message asks: `action`, which the user decides while work goes on or takes before it can."""
    need: Literal["decision", "blocked"]
    action: str


class UserPush(NamedTuple):
    """An actionable quota notice delivered to the user's phone."""
    name: str
    rendered: RenderedPush

    @property
    def summary(self) -> str:
        return self.rendered.title

    @property
    def text(self) -> str:
        return self.rendered.body

    @property
    def ask(self) -> Ask:
        from user_action import ActionRequired
        assert isinstance(self.rendered.action, ActionRequired)
        assert self.rendered.need in ("decision", "blocked")
        return Ask(self.rendered.need, self.rendered.action.text)


class QuotaStatusNote(NamedTuple):
    """Quota information recorded in the log without a phone delivery."""
    name: str
    text: str


QuotaNotice = UserPush | QuotaStatusNote


def load_config() -> Config:
    return load_settings_from(CONFIG)


def showrunners_now() -> list[Showrunner]:
    """Every showrunner with an update timer. None when the session records cannot say who runs."""
    try:
        return registered_showrunners()
    except OSError as error:
        print(f"quota-alert: showrunners unknown: {error}", file=sys.stderr)
        return []


def not_live(names: list[str]) -> list[str]:
    """Those of `names` that no live session is called now. A lookup that cannot say leaves a name out."""
    missed: list[str] = []
    for name in names:
        try:
            if socket_for(name) is None:
                missed.append(name)
        except OSError:
            continue
    return missed


def recipients(config: Config, here: str | None = None) -> list[str]:
    """The configured names, offline or not, then each running showrunner by its name now.

    A showrunner that is not running has no name to send to; it hears the next repeat after it resumes.
    """
    names = [*config["always"], *(runner["session"] for runner in showrunners_now() if runner["session"])]
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
            saved = cast(StoredState, json.loads(STATE.read_text(encoding="utf-8")))
            state = State(episodes=saved.get("episodes", {}), fallback=_read_fallback(saved.get("switch")))
            if "refusal_notices" in saved:
                state["refusal_notices"] = saved["refusal_notices"]
            if "unseen" in saved:
                state["unseen"] = saved["unseen"]
        except (OSError, ValueError):
            state = State(episodes={}, fallback=NoCodexQuotaFallback())
        before = json.dumps(_stored_state(state), sort_keys=True)
        yield state
        if json.dumps(_stored_state(state), sort_keys=True) != before:
            tmp = STATE.with_name(f".{STATE.name}.tmp")
            _ = tmp.write_text(json.dumps(_stored_state(state), indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, STATE)


def ahead(resets: str | None) -> bool:
    """Whether a note's reset is still to come."""
    reset = reset_time(resets)
    return reset is not None and reset > datetime.now(timezone.utc)


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


def recovered(name: str, notes: list[AgentNote], threshold: float,
              selection: ClaudeAccountsInUse, now: datetime,
              codex_readings: CodexAlertPassReadings,
              episode: Episode) -> bool:
    """Known to be out of the low state. An unknown reading is not a recovery."""
    note = next((note for note in notes if note.path.stem == name), None)
    if name.startswith("codex "):
        reset_at = episode.get("weekly_reset")
        if reset_at:
            try:
                if datetime.fromisoformat(reset_at) <= now:
                    return True
            except ValueError:
                pass
        if note is None:
            return False
        for checked in codex_readings.checked_homes:
            report = checked.report
            if (report.email and report.email.casefold() == (note.get("login") or "").casefold()
                    and not report.problem
                    and timedelta(0) <= now - checked.observed_at <= CODEX_READING_MAX_AGE):
                value, reset = report.weekly_remaining, report.weekly_reset
                if value is not None and reset is not None and reset > now and value > threshold:
                    return True
        reset = reset_time(note.get("resets"))
        return reset is not None and reset <= now
    if selection.multiple_accounts and name.startswith("claude "):
        if note is None:
            return False
        value = remaining(note)
        if value is not None and math.isfinite(value) and value > threshold:
            return True
        reset = reset_time(note.get("resets"))
        return reset is not None and reset <= now
    if note is None or note.get("state") == "inactive":
        return True
    value = remaining(note)
    return value is not None and value > threshold


def close_recovered(state: State, notes: list[AgentNote], threshold: float,
                    selection: ClaudeAccountsInUse, now: datetime,
                    codex_readings: CodexAlertPassReadings) -> list[str]:
    """Remove the episodes whose account recovered and return their names."""
    episodes = state["episodes"]
    closed = [name for name in episodes if recovered(name, notes, threshold, selection, now, codex_readings, episodes[name])]
    for name in closed:
        del episodes[name]
        notices = state.get("refusal_notices", {})
        for home, notice in list(notices.items()):
            if notice["kind"] == "known" and notice["label"] == name:
                del notices[home]
    return closed


def local_time(stamp: str) -> str:
    return datetime.fromisoformat(stamp).astimezone(user_zone()).strftime("%Y-%m-%d %H:%M")


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


def switch_to_claude(state: State, note: AgentNote, stamp: str, owner: agent_accounts.KnownCodexAccount) -> str:
    """Move every [assignments] entry on codex to claude within `note`'s open episode; return the log line."""
    episode = state["episodes"].get(note.path.stem)
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
        if episode is not None:
            episode["switch_failed"] = error
        notice = state.get("refusal_notices", {}).get(str(owner.home))
        if notice is not None and notice["kind"] == "known":
            notice["switch_failed"] = error
        state.setdefault("unseen", []).append(
            f"Codex refused work for quota ({account}), and the automatic switch to Claude failed at "
            + f"{local_time(stamp)}: {error}")
        return f"switch to claude for {note.path.stem} failed: {error}"
    if not moved:
        return f"switch to claude for {note.path.stem}: nothing was on codex"
    state["fallback"] = AccountCodexQuotaFallback(owner, stamp, tuple(moved))
    state.setdefault("unseen", []).append(
        f"Codex refused work for quota ({account}): {', '.join(moved)} moved to Claude at {local_time(stamp)}, "
        + "and move back when Codex recovers.")
    return f"switched to claude for {note.path.stem}: {', '.join(moved)}"


def switch_back(state: State, now: datetime, readings: CodexAlertPassReadings) -> CodexFallbackRestoration:
    """Restore owed assignments when the selected account can work on allowance or credits."""
    _finish_seats_transitions(now)
    _room_again(now)
    fallback = state["fallback"]
    if isinstance(fallback, NoCodexQuotaFallback):
        return NoCodexFallbackRestoration()
    selected = readings.selected_account
    selected_can_work = isinstance(codex_can_work(selected_codex_report(readings, now), now), CodexWorkAvailable)
    if isinstance(selected, agent_accounts.KnownCodexAccount):
        refusal = refusals.load().get(refusals.key("codex", selected.login))
        selected_can_work = selected_can_work and (refusal is None or
            refusals.resolved(refusal, _reading_for_account(readings, selected, now), now))
    if not selected_can_work:
        from scripts.accounts import accounts

        chosen = accounts.pick("codex", now, keep=accounts.KeepAccount(readings.seats.label))
        if isinstance(chosen, accounts.NoAccountWithRoom):
            return NoCodexFallbackRestoration()
        if chosen.dir != readings.seats.home:
            move_id = uuid.uuid4().hex
            recovery_request: moves.RequestedMove = {"kind": "requested", "at": now.isoformat(),
                "tool": "codex", "subject": "seats", "move_id": move_id, "from": readings.seats.label,
                "to": chosen.label, "why": "Codex has room again", "window": "unknown"}
            moves.append_move(recovery_request)
            try:
                _ = accounts.swap("seats", chosen.label)
            except (accounts.AccountsError, OSError) as error:
                moves.append_move(moves.FailedMove(kind="failed", at=now.isoformat(), tool="codex",
                    subject="seats", move_id=move_id, reason=str(error)))
                return NoCodexFallbackRestoration()
            readings = CodexAlertPassReadings(accounts.CodexSeatsAccount(chosen.label, chosen.dir),
                agent_accounts.codex_account(chosen.dir), readings.checked_homes)
    selected = readings.selected_account
    if isinstance(selected, agent_accounts.KnownCodexAccount):
        refusal = refusals.load().get(refusals.key("codex", selected.login))
        if refusal is not None and not refusals.resolved(refusal, _reading_for_account(readings, selected, now), now):
            return NoCodexFallbackRestoration()
    if isinstance(fallback, NoCodexQuotaFallback) or not isinstance(
            codex_can_work(selected_codex_report(readings, now), now), CodexWorkAvailable):
        return NoCodexFallbackRestoration()
    restored_account = selected if isinstance(selected, agent_accounts.KnownCodexAccount) else NO_RESTORATION_ACCOUNT
    state["fallback"] = NoCodexQuotaFallback()
    if isinstance(fallback, AccountCodexQuotaFallback):
        _ = state.get("refusal_notices", {}).pop(str(fallback.owner.home), None)
    try:
        current = registry_assignments()
    except OSError as error:
        back = SwitchBack([], [], [f"{key} ({error})" for key in fallback.moved], restored_account)
    else:
        back = SwitchBack([], [], [], restored_account)
        for key in fallback.moved:
            if current.get(key) != "claude":
                back.kept.append(key)
            elif error := edit_registry("agents_set_assignment", key, "codex"):
                back.failed.append(f"{key} ({error})")
            else:
                back.moved.append(key)
    stamp = now.isoformat(timespec="seconds")
    state.setdefault("unseen", []).append(f"Codex recovered at {local_time(stamp)}: {back.describe()}.")
    return back


def _readings_after_restoration(readings: CodexAlertPassReadings, back: CodexFallbackRestoration) -> CodexAlertPassReadings:
    from scripts.accounts.accounts import CodexSeatsAccount

    if isinstance(back, SwitchBack) and isinstance(back.restored_account, agent_accounts.KnownCodexAccount):
        account = back.restored_account
        return CodexAlertPassReadings(CodexSeatsAccount(agent_accounts.codex_home_role_label(account.home), account.home),
            account, readings.checked_homes)
    return readings


def account_line(note: AgentNote, threshold: float, selection: ClaudeAccountsInUse) -> str:
    roles = claude_roles(note, selection)
    if note.tool == "claude" and selection.multiple_accounts:
        return (f"{note.path.stem} ({', '.join(roles) or 'spare'}) at {percent(note)} of its weekly "
                + f"usage left (threshold {threshold:g}%); it resets {note.get('resets')}.")
    return (f"{note.path.stem}, the active {note.tool.capitalize()} account, has {percent(note)} of its weekly "
            + f"usage left (threshold {threshold:g}%); it resets {note.get('resets')}.")


def claude_roles(note: AgentNote, selection: ClaudeAccountsInUse) -> tuple[str, ...]:
    """Describe account use without assigning a role to its login."""
    return ("in use",) if note.tool == "claude" and selection.is_in_use(note.get("login") or "") else ()


def unit_swap_line(note: AgentNote, _notes: list[AgentNote], selection: ClaudeAccountsInUse) -> str:
    """Forecast each group's destination and the destination for ungrouped owners."""
    from scripts.accounts import accounts

    login = (note.get("login") or "").casefold()
    excluded = {named.label for named in selection.named_accounts if named.identity.login.casefold() == login}
    if selection.is_default_login(login):
        excluded.add("default")
    excluded.update(accounts.account_labels_for_logins(frozenset({login})))
    now = datetime.now(timezone.utc)
    destinations = [accounts.placement(group, now, exclude=frozenset(excluded))
                    for group in accounts.CLAUDE_GROUPS]
    selected = accounts.pick("claude", now, exclude=frozenset(excluded), keep=accounts.NoKeep())
    label = note.path.stem

    def no_room(outcome: accounts.NoAccountWithRoom) -> str:
        reset = outcome.earliest_reset
        time = reset.at.astimezone(user_zone()).strftime("%a %Y-%m-%d %H:%M %Z") if isinstance(reset, accounts.EarliestReset) else "an unknown reset"
        return f"no other account has room until {time}"

    if isinstance(selected, accounts.NoAccountWithRoom) and all(
            isinstance(destination.reason, accounts.GroupNoRoom) for destination in destinations):
        return f"{label} is low; {no_room(selected)}"
    forecasts = [f"{destination.group}: " + (no_room(destination.reason.outcome)
                 if isinstance(destination.reason, accounts.GroupNoRoom) else f"move to {destination.label}")
                 for destination in destinations]
    forecasts.append("ungrouped sessions: " + (f"move to {selected.label}"
        if isinstance(selected, accounts.PickedAccount) else no_room(selected)))
    return f"{label} is low; when refused, " + "; ".join(forecasts)


def credit_line(note: AgentNote) -> str:
    """The credits Codex reports for the account, which carry work once its weekly allowance is used up."""
    name = note.path.stem
    balance = note.get("credit_balance")
    if balance == "unlimited":
        return f"Credits on {name}: unlimited."
    try:
        return f"Credits left on {name}: {float(balance or ''):,.0f}."
    except ValueError:
        return f"No credit balance is reported for {name}."


def user_time(stamp: str | None) -> str:
    """A machine-local note time rendered in the user's zone."""
    try:
        return datetime.fromisoformat(stamp or "").astimezone(user_zone()).strftime("%Y-%m-%d %H:%M %Z")
    except ValueError:
        return "an unknown time"


def run_out_push(note: AgentNote, selection: ClaudeAccountsInUse, notes: list[AgentNote]) -> QuotaNotice:
    """The actionable push or log note when this account's episode opens."""
    from push_texts import render

    name = note.path.stem
    roles = claude_roles(note, selection)
    display_name = f"{name}, used by live sessions," if roles else name
    first = f"The {display_name} account has {percent(note)} weekly usage left. It refills {user_time(note.get('resets'))}."
    if note.tool == "codex":
        return QuotaStatusNote(name, f"{first} Codex keeps working on its credits. {credit_line(note)}")
    candidates = [(value, candidate) for candidate in notes
                  if candidate.tool == "claude" and candidate.path != note.path
                  and (candidate.get("login") or "").casefold() != (note.get("login") or "").casefold()
                  and candidate.get("state") in ("active", "inactive")
                  and (value := remaining(candidate)) is not None and math.isfinite(value) and value > 0]
    if not candidates:
        return QuotaStatusNote(name, first + " No other Claude account has weekly usage left; work waits for usage to reset.")
    _, target = max(candidates, key=lambda candidate: (candidate[0], candidate[1].path.stem))
    return UserPush(name, render("quota.claude", account=name, remaining=percent(note),
                                 reset=user_time(note.get("resets")), target=target.path.stem,
                                 target_remaining=percent(target)))


def refusal_push(note: AgentNote, switch: CodexQuotaFallback, failed: str | None, already: bool) -> QuotaNotice:
    """The one direct user message sent when Codex refuses this episode."""
    from push_texts import render

    if not isinstance(switch, NoCodexQuotaFallback):
        verb = "had already moved" if already else "moved"
        outcome = f"New work {verb} to Claude, the other coding assistant"
    elif failed:
        outcome = "New work could not move to Claude, the other coding assistant"
    else:
        outcome = "No delegated work needed to move to Claude, the other coding assistant"
    name = note.path.stem
    credits = credit_line(note).replace(f" on {name}", "")
    if not isinstance(switch, NoCodexQuotaFallback) or not failed:
        return QuotaStatusNote(name, f"Codex refused work on {name}. {credits} {outcome}.")
    return UserPush(name, render("quota.codex_refused", account=name))


def instruction(tool: str, switch: CodexQuotaFallback, failed: str | None) -> str:
    """What the alert asks of its recipient, given the switch to Claude made or refused."""
    refused = f"The automatic switch from {tool} to Claude failed: {failed} " if failed else ""
    if isinstance(switch, NoCodexQuotaFallback) and tool == "Codex" and not failed:
        return ("Its weekly allowance is used up or nearly so, and Codex may keep working on the account's credits, "
                + "so nothing was switched to Claude: keep delegating Codex work. Bring this to your user, and if "
                + "Codex refuses work for quota, bring that to them at once.")
    if isinstance(switch, NoCodexQuotaFallback):
        return (f"{refused}Bring this to your user, and if you delegate {tool} work, start no new {tool} work "
                + 'until a "Quota restored:" notice.')
    return (f"{refused}Every function that ran on {tool} ({', '.join(switch.moved)}) was moved to Claude at "
            + f"{switch.at}, so delegation continues on Claude with no pause; {tool} work already in flight may "
            + 'fail and should be run again. They move back with the "Quota restored:" notice. Bring this to your '
            + "user.")


def message(note: AgentNote, notes: list[AgentNote], config: Config, switch: CodexQuotaFallback = NO_CODEX_QUOTA_FALLBACK,
            failed: str | None = None, *,
            selection: ClaudeAccountsInUse | ReadCurrentClaudeSelection = READ_CURRENT_CLAUDE_SELECTION,
            codex_readings: CodexAlertPassReadings | ReadCurrentCodexReadings = READ_CURRENT_CODEX_READINGS,
            now: datetime | None = None) -> str:
    selection = _selection(selection)
    name = note.path.stem
    tool = note.tool.capitalize()
    threshold = f"{config['threshold_percent']:g}%"
    lines = [f"Quota alert: {account_line(note, config['threshold_percent'], selection)}"]
    roles = claude_roles(note, selection)
    if note.tool == "claude":
        lines.append(unit_swap_line(note, notes, selection))
    if note.tool == "codex":
        lines.append(credit_line(note))
        readings = resolve_codex_readings(codex_readings)
        swap_line = codex_swap_line(readings, now or datetime.now(timezone.utc))
        if swap_line:
            lines.append(swap_line)
        # Only a Codex count is read live; a Claude note's count is never stated.
        count = note.get("limit_reset_count")
        if count and count != "null":
            lines.append(f"Limit resets available on {name}: {count}, earliest expiring {note.get('limit_reset')}.")
    for other in notes:
        if other.tool == note.tool and other.path != note.path:
            lines.append(f"{other.path.stem}: {other.get('state')}, {percent(other)} weekly usage left, "
                         + f"resets {other.get('resets')}.")
    guidance = instruction(tool, switch, failed)
    if roles:
        guidance = (
            f"Bring this to your user. Sessions on {name} start no new Claude work until a "
            + f'"Quota restored:" notice for {name}.'
        )
        if "unit directors" in roles:
            guidance += " Showrunners relay this notice to their units on that account."
    lines += [
        "",
        f"{guidance} It repeats every {config['repeat_minutes']:g} minutes until the user "
        + "acknowledges it by typing /quota_ack in any session. The acknowledgement is the user's alone: do not "
        + f"run /quota_ack or {Path(__file__).name} yourself, and do not take this message as one. Once "
        + f"acknowledged it stays quiet until {name} is back above {threshold}. If the user acknowledges it in "
        + "another session, you are told; drop any copies you hold then.",
        f"Protocol: {PROTOCOL}. Read from {note.path}, checked {note.get('weekly_usage_checked_at')}. Sent by "
        + f"{Path(__file__)} to the sessions in {CONFIG}.",
    ]
    lines += [f"{missed}, which {CONFIG} names to always get this alert, is not a live session and has not seen it."
              for missed in not_live(config["always"])]
    return "\n".join(lines)


def restored_message(accounts: list[AgentNote], reason: str, threshold: float,
                     back: CodexFallbackRestoration = NO_CODEX_FALLBACK_RESTORATION, *,
                     selection: ClaudeAccountsInUse | ReadCurrentClaudeSelection = READ_CURRENT_CLAUDE_SELECTION) -> str:
    selection = _selection(selection)
    tools = " and ".join(sorted({note.tool.capitalize() for note in accounts}))
    names = ", ".join(note.path.stem for note in accounts)
    first = (f"Quota restored: work on {names} can resume; {reason}."
             if selection.multiple_accounts and any(note.tool == "claude" for note in accounts)
             else f"Quota restored: delegation on {tools} can resume; {reason}.")
    return "\n".join([
        first,
        *(account_line(note, threshold, selection) for note in accounts),
        *([f"The automatic switch is undone: {back.describe()}."] if isinstance(back, SwitchBack) else []),
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


@dataclass(frozen=True)
class PushSent:
    """The user delivery completed successfully."""


@dataclass(frozen=True)
class PushFailed:
    why: str


PushDelivery = PushSent | PushFailed


def tell_user(push: RenderedPush) -> PushDelivery:
    """Convert the sender process result into the user delivery state."""
    from push_texts import user_send_argv

    user_send_timeout = review.user_send_seconds()
    command = user_send_argv(push, timeout=user_send_timeout)
    command[1] = str(SEND)
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=user_send_timeout + KILL_GRACE + 5,
                              check=False)
    except (subprocess.TimeoutExpired, OSError) as error:
        return PushFailed(str(error))
    if done.returncode == 0:
        return PushSent()
    return PushFailed(" ".join((done.stdout.strip() or done.stderr.strip() or f"exit {done.returncode}").split()))


def deliver(jobs: list[Job]) -> list[str | None]:
    """Relay every job in parallel; one error or None per job, in order.

    A job's key keeps one queued copy per alert for a recipient that is not running.
    """
    if not jobs:
        return []
    keys = [f"quota {job.kind} {job.name}" if job.name else None for job in jobs]
    with ThreadPoolExecutor() as pool:
        return list(pool.map(relay, [job.recipient for job in jobs], [job.text for job in jobs], keys))


def deliver_and_record(jobs: list[Job], stamp: str) -> list[str]:
    """Stamp each alert's attempt on its episode, then deliver every job; one log line per job.

    The stamp comes first: the snapshot timer kills a run that outlasts its budget, and a kill
    after the sends but before a later stamp sent every alert again on the next run. A kill now
    costs at most one repeat.
    """
    with state_file() as state:
        for job in jobs:
            if job.kind == "alert" and job.name in state["episodes"]:
                state["episodes"][job.name]["last"][job.recipient] = stamp
    errors = deliver(jobs)
    return [f"quota {job.kind} {job.name} -> {job.recipient}: "
            + ("sent" if error is None else f"not delivered: {error}") for job, error in zip(jobs, errors)]


def alert(notes: list[AgentNote], now: datetime | None = None, *,
          selection: ClaudeAccountsInUse | ReadCurrentClaudeSelection = READ_CURRENT_CLAUDE_SELECTION,
          codex_readings: CodexAlertPassReadings | ReadCurrentCodexReadings = READ_CURRENT_CODEX_READINGS) -> list[str]:
    """Send what is due, record it, and return one log line per attempt."""
    selection = _selection(selection)
    codex_readings = resolve_codex_readings(codex_readings)
    now = now or datetime.now(timezone.utc)
    _save_codex_readings(codex_readings, now)
    return _alert_for_saved_readings(notes, now, selection, codex_readings)


def _alert_for_saved_readings(notes: list[AgentNote], now: datetime, selection: ClaudeAccountsInUse,
                              codex_readings: CodexAlertPassReadings) -> list[str]:
    stamp = now.isoformat(timespec="seconds")
    config = load_config()
    threshold = config["threshold_percent"]
    repeat = timedelta(minutes=config["repeat_minutes"])
    jobs: list[Job] = []
    pushes: list[UserPush] = []
    log: list[str] = []
    with state_file() as state:
        episodes = state["episodes"]
        closed = close_recovered(state, notes, threshold, selection, now, codex_readings)
        back = switch_back(state, now, codex_readings)
        codex_readings = _readings_after_restoration(codex_readings, back)
        for note in [*low([note for note in notes if note.tool != "codex"], threshold),
                     *_low_codex_notes(notes, threshold, codex_readings, now)]:
            if note.tool == "claude" and not selection.is_in_use(note.get("login") or ""):
                continue
            name = note.path.stem
            if name not in episodes:
                episodes[name] = {"since": stamp, "push": "due", "last": {}}
                if note.tool == "codex":
                    reset = selected_codex_report(codex_readings, now).weekly_reset
                    if reset is not None:
                        episodes[name]["weekly_reset"] = reset.isoformat()
            episode = episodes[name]
            session_notes: list[str] = []
            if episode.get("push") == "due":
                push = run_out_push(note, selection, notes)
                if isinstance(push, QuotaStatusNote):
                    log.append(f"quota status {name}: {push.text}")
                    if note.tool == "claude":
                        session_notes.append(push.text)
                    episode["push"] = stamp
                else:
                    pushes.append(push)
            if "acknowledged" in episode:
                for text in session_notes:
                    jobs += [Job(name, recipient, text, "alert") for recipient in recipients(config)]
                continue
            switch = state["fallback"] if note.tool == "codex" else NoCodexQuotaFallback()
            text = message(note, notes, config, switch, episode.get("switch_failed"), selection=selection, codex_readings=codex_readings, now=now)
            if session_notes:
                text += "\n" + "\n".join(session_notes)
            for recipient in recipients(config):
                last = episode["last"].get(recipient)
                if last is None or now - datetime.fromisoformat(last) >= repeat:
                    jobs.append(Job(name, recipient, text, "alert"))
    if isinstance(back, SwitchBack):
        log.append(f"switch to claude undone: {back.describe()}")
        identity = codex_readings.selected_account
        if isinstance(identity, agent_accounts.KnownCodexAccount):
            selected_notes = [note for note in notes if note.tool == "codex"
                              and (note.get("login") or "").casefold() == identity.login.casefold()]
            text = restored_message(selected_notes, f"{identity.label} can work on allowance or credits",
                                    threshold, back, selection=selection)
            jobs += [Job(identity.label, recipient, text, "restored") for recipient in recipients(config)]
    for name in closed:
        tool = name.split(" ")[0]
        accounts = [note for note in usable(notes, threshold) if note.tool == tool]
        if tool == "codex" or tool == "claude" and selection.multiple_accounts:
            accounts = [note for note in notes if note.path.stem == name]
        if (tool == "codex" and isinstance(back, SwitchBack)
                and isinstance(codex_readings.selected_account, agent_accounts.KnownCodexAccount)
                and name == codex_readings.selected_account.label):
            continue
        if accounts:
            text = restored_message(accounts, f"the {name} alert is closed", threshold,
                                    back if tool == "codex" else NoCodexFallbackRestoration(), selection=selection)
            jobs += [Job(name, recipient, text, "restored") for recipient in recipients(config)]
    with ThreadPoolExecutor() as pool:
        relay_future = pool.submit(deliver_and_record, jobs, stamp)
        push_futures = [pool.submit(tell_user, push.rendered) for push in pushes]
        push_deliveries = [future.result() for future in push_futures]
        relay_log = relay_future.result()
    if pushes:
        with state_file() as state:
            for push, delivery in zip(pushes, push_deliveries):
                episode = state["episodes"].get(push.name)
                if isinstance(delivery, PushSent) and episode is not None and episode.get("push") == "due":
                    episode["push"] = stamp
                outcome = "sent" if isinstance(delivery, PushSent) else f"not delivered: {delivery.why}"
                log.append(f"quota push {push.name} -> user: {outcome}")
    return log + relay_log


@dataclass
class RefusingAccountNote:
    path: Path
    fields: dict[str, str]
    tool: str = "codex"

    def get(self, key: str) -> str | None:
        return self.fields.get(key)


def _low_codex_notes(notes: list[AgentNote], threshold: float,
                     readings: CodexAlertPassReadings, now: datetime) -> list[AgentNote]:
    identity = readings.selected_account
    report = selected_codex_report(readings, now)
    if isinstance(identity, agent_accounts.UnknownCodexAccount) or report.problem:
        return []
    left, reset = report.weekly_remaining, report.weekly_reset
    if left is None or not math.isfinite(left) or not 0 <= left <= threshold or reset is None or reset <= now:
        return []
    return [note for note in notes if note.tool == "codex"
            and (note.get("login") or "").casefold() == identity.login.casefold()]


@dataclass(frozen=True)
class SeatsTransitionDelivery:
    text: str


@dataclass(frozen=True)
class RefusedAt:
    at: datetime


@dataclass(frozen=True)
class RefusalTimeUnknown:
    """Use the alert pass's observation time when no refusal time was handed in."""


REFUSAL_TIME_UNKNOWN = RefusalTimeUnknown()


@dataclass(frozen=True)
class FallbackRefusalDelivery:
    lines: list[str]
    text: str
    quota_notice: QuotaNotice
    notice: KnownRefusalNotice
    failures_to_stamp: tuple[str, ...]


def blocked(notes: list[AgentNote], refusing_account: agent_accounts.RefusingCodexAccount,
            now: datetime | None = None, *,
            selection: ClaudeAccountsInUse | ReadCurrentClaudeSelection = READ_CURRENT_CLAUDE_SELECTION,
            codex_readings: CodexAlertPassReadings | ReadCurrentCodexReadings = READ_CURRENT_CODEX_READINGS,
            window: refusals.KnownWindow | refusals.UnknownWindow = UNKNOWN_REFUSAL_WINDOW,
            refused_at: RefusedAt | RefusalTimeUnknown = REFUSAL_TIME_UNKNOWN) -> list[str]:
    """A provider refusal waits briefly, moves seats, or opens the Claude fallback."""
    from scripts.accounts import accounts
    selection = _selection(selection)
    readings = resolve_codex_readings(codex_readings)
    now = now or datetime.now(timezone.utc)
    refusal_time = refused_at.at if isinstance(refused_at, RefusedAt) else now
    _save_codex_readings(readings, now)
    _finish_seats_transitions(now)
    stamp = now.isoformat(timespec="seconds")
    config = load_config()
    home = str(refusing_account.home)
    if isinstance(refusing_account, agent_accounts.UnknownCodexAccount):
        with state_file() as state:
            notices = state.setdefault("refusal_notices", {})
            previous = notices.get(home)
            if previous is not None and previous["kind"] == "unknown":
                return [f"Codex refusal at {home}: unknown account already reported"]
            notices[home] = UnknownRefusalNotice(kind="unknown")
        text = (f"Codex refused work for quota at {home}; refusing account unknown: "
                + f"{refusing_account.reason}. Nothing switched to Claude.")
        return [text, *deliver_and_record(
            [Job("", recipient, text, "refusal") for recipient in recipients(config)], stamp)]
    name = refusing_account.label
    reading = refusals.stored_reading("codex", refusing_account.login, now)
    window = _refusal_window(window, _reading_for_account(readings, refusing_account, now), refusal_time)
    reset: refusals.KnownReset | refusals.NoResetKnown = refusals.NoResetKnown()
    if isinstance(window, refusals.KnownWindow) and isinstance(reading, (refusals.FreshReading, refusals.StaleReading)):
        held_window = reading.weekly if window.name == "weekly" else reading.five_hour
        if isinstance(held_window, refusals.ReadingWindow):
            reset = held_window.reset
    refusals.write_refusal(refusals.AccountRefusal("codex", refusing_account.login, window, reset, refusal_time))
    note = next((note for note in notes if note.tool == "codex"
                 and (note.get("login") or "").casefold() == refusing_account.login.casefold()),
                RefusingAccountNote(Path(name + ".md"), {}))
    selected = readings.selected_account
    account_status = ("the outgoing account" if isinstance(selected, agent_accounts.KnownCodexAccount)
                      else "an account whose seats selection is unknown")
    transition_line = ""
    no_room = ""
    with state_file() as state:
        current_seats = accounts.role_account("seats")
        owns_role = (current_seats.home == refusing_account.home
                     and isinstance(selected, agent_accounts.KnownCodexAccount) and selected.home == refusing_account.home
                     and selected.login.casefold() == refusing_account.login.casefold()
                     and selected.home == readings.seats.home)
        if owns_role:
            entries = moves.read_moves()
            if (isinstance(window, refusals.KnownWindow) and window.name == "five_hour"
                    and isinstance(reset, refusals.KnownReset) and timedelta(0) < reset.at - now < timedelta(minutes=20)):
                if any(entry["tool"] == "codex" and entry["subject"] == "seats" and entry["kind"] == "waited"
                       and entry["reset"] == reset.at.isoformat() for entry in entries):
                    return [f"seats already wait for {name}'s 5-hour reset"]
                move_id = uuid.uuid4().hex
                moves.append_move(moves.WaitedMove(kind="waited", at=stamp, tool="codex", subject="seats",
                    move_id=move_id, window="five_hour", reset=reset.at.isoformat()))
                transition_line = f"seats wait for {name}'s 5-hour reset at {reset_display(reset)}"
            else:
                chosen = accounts.pick("codex", now, exclude=frozenset({readings.seats.label}), keep=accounts.NoKeep())
                if isinstance(chosen, accounts.PickedAccount):
                    move_id = uuid.uuid4().hex
                    window_name: Literal["weekly", "five_hour", "unknown"] = window.name if isinstance(window, refusals.KnownWindow) else "unknown"
                    reason = f"{name} refused ({window_name} limit until {reset_display(reset)})"
                    request: moves.RequestedMove = {"kind": "requested", "at": stamp, "tool": "codex",
                        "subject": "seats", "move_id": move_id, "from": readings.seats.label, "to": chosen.label,
                        "why": reason, "window": window_name}
                    if isinstance(reset, refusals.KnownReset):
                        request["reset"] = reset.at.isoformat()
                    moves.append_move(request)
                    try:
                        _ = accounts.swap("seats", chosen.label)
                    except (accounts.AccountsError, OSError) as error:
                        moves.append_move(moves.FailedMove(kind="failed", at=stamp, tool="codex", subject="seats",
                            move_id=move_id, reason=str(error)))
                        no_room = f"seats could not move from {name} to {chosen.label}: {error}; using Claude fallback"
                    else:
                        transition_line = f"seats moved from {name} to {chosen.label}: {reason}"
                else:
                    closed = {entry["move_id"] for entry in entries if entry["kind"] == "room again"}
                    pending = any(entry["tool"] == "codex" and entry["subject"] == "seats" and entry["kind"] == "no room"
                                  and entry["move_id"] not in closed for entry in entries)
                    if not pending:
                        moves.append_move(moves.NoRoomMove(kind="no room", at=stamp, tool="codex", subject="seats",
                            move_id=uuid.uuid4().hex, earliest_reset=chosen.earliest_reset.at.isoformat()
                            if isinstance(chosen.earliest_reset, accounts.EarliestReset) else ""))
                    no_room = accounts.no_room_line("codex", chosen)
        if transition_line:
            delivery: SeatsTransitionDelivery | FallbackRefusalDelivery = SeatsTransitionDelivery(transition_line)
        else:
            notices = state.setdefault("refusal_notices", {})
            previous = notices.get(home)
            if previous is not None and previous["kind"] == "unknown":
                del notices[home]
                previous = None
            episode = state["episodes"].get(name)
            notice = (previous if previous is not None and previous["kind"] == "known"
                      and previous["login"].casefold() == refusing_account.login.casefold()
                      else KnownRefusalNotice(kind="known", login=refusing_account.login, label=name))
            notices[home] = notice
            fallback = state["fallback"]
            already = not isinstance(fallback, NoCodexQuotaFallback)
            log: list[str] = [no_room] if owns_role else []
            if owns_role and not already:
                if episode is not None:
                    _ = episode.pop("switch_failed", None)
                _ = notice.pop("switch_failed", None)
                log.append(switch_to_claude(state, note, stamp, refusing_account))
                fallback = state["fallback"]
            elif owns_role:
                log.append(f"{name} refused work for quota; assignments already use Claude")
            else:
                log.append(f"{name} refused work for quota on {account_status}; nothing switched")
            moved = owns_role and not already and isinstance(fallback, AccountCodexQuotaFallback)
            failed = None
            if owns_role:
                failed = notice.get("switch_failed") or (episode.get("switch_failed") if episode is not None else None)
                text = message(note, notes, config, fallback, failed,
                               selection=selection, codex_readings=readings, now=now) + "\n" + no_room
                push = refusal_push(note, fallback, failed, already)
            else:
                text = f"Codex refused work on {name}, {account_status}. Nothing switched to Claude."
                status = QuotaStatusNote(name, text)
                if "push" not in notice:
                    log.append(f"quota status {status.name}: {status.text}")
                    notice["push"] = stamp
                return log
            swap_line = codex_swap_line(readings, now)
            if swap_line:
                text += "\n" + swap_line
            new_failure = owns_role and not already and failed is not None and failed not in notice.get("failure_pushes", [])
            if not moved and not new_failure and ("push" in notice or episode is not None and "refusal_push" in episode):
                return log
            delivery = FallbackRefusalDelivery(log, text, push, notice, (failed,) if failed is not None else ())
    if isinstance(delivery, SeatsTransitionDelivery):
        return [delivery.text, *deliver_and_record(
            [Job(name, recipient, delivery.text, "refusal") for recipient in recipients(config)], stamp)]
    log = delivery.lines
    log += deliver_and_record([Job(name, recipient, delivery.text, "refusal") for recipient in recipients(config)], stamp)
    quota_notice = delivery.quota_notice
    if isinstance(quota_notice, QuotaStatusNote):
        log.append(f"quota status {quota_notice.name}: {quota_notice.text}")
    else:
        push_delivery = tell_user(quota_notice.rendered)
        log.append(f"quota refusal {name} -> user: " + (
            "sent" if isinstance(push_delivery, PushSent) else f"not delivered: {push_delivery.why}"))
        if isinstance(push_delivery, PushFailed):
            return log
    with state_file() as state:
        notices = state.setdefault("refusal_notices", {})
        delivery.notice["push"] = stamp
        if delivery.failures_to_stamp:
            delivery.notice.setdefault("failure_pushes", []).extend(delivery.failures_to_stamp)
        notices[home] = delivery.notice
        if name in state["episodes"]:
            state["episodes"][name]["refusal_push"] = stamp
    return log


def refresh(notes: list[AgentNote], here: str | None, now: datetime | None = None, *,
            selection: ClaudeAccountsInUse | ReadCurrentClaudeSelection = READ_CURRENT_CLAUDE_SELECTION,
            codex_readings: CodexAlertPassReadings | ReadCurrentCodexReadings = READ_CURRENT_CODEX_READINGS) -> list[str]:
    """After the user reports a reset: close what recovered, alert what is still low, tell the others.

    The notice goes out even when the timer already closed the episode, since that
    is the case where nothing else has told anyone.
    """
    selection = _selection(selection)
    codex_readings = resolve_codex_readings(codex_readings)
    now = now or datetime.now(timezone.utc)
    _save_codex_readings(codex_readings, now)
    threshold = load_config()["threshold_percent"]
    with state_file() as state:
        closed = close_recovered(state, notes, threshold, selection, now, codex_readings)
        back = switch_back(state, now, codex_readings)
        codex_readings = _readings_after_restoration(codex_readings, back)
    lines = [f"{name}: alert closed" for name in closed]
    lines += [f"switch to claude undone: {back.describe()}"] if isinstance(back, SwitchBack) else []
    lines += _alert_for_saved_readings(notes, now, selection, codex_readings)
    lines += [f"{note.path.stem}: {percent(note)} left, resets {note.get('resets')}"
              for note in notes if note.get("state") == "active"]
    accounts = [note for note in usable(notes, threshold) if note.tool != "codex"]
    report = selected_codex_report(codex_readings, now)
    identity = codex_readings.selected_account
    if isinstance(identity, agent_accounts.KnownCodexAccount) and isinstance(codex_can_work(report, now), CodexWorkAvailable):
        accounts += [note for note in notes if note.tool == "codex"
                     and (note.get("login") or "").casefold() == identity.login.casefold()]
    if not accounts:
        return [*lines, "no active account is above the threshold; nothing announced"]
    reason = "the user reported a usage reset and the notes were refreshed"
    return lines + tell_others(restored_message(accounts, reason, threshold, back, selection=selection), here)


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
