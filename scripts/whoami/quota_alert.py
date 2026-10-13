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
codex_mesh.py runs `agent_notes.py blocked` when the provider turns a launch away
for quota, and blocked() then moves every [assignments] entry on codex to claude
through agents_config.sh's validated editor and tells every recipient at once
what moved, or why the editor refused. Only a refusal from the account selected for seats while its week is at or under
the threshold, or its weekly episode is open, opens the fallback. Above the threshold
the refusal is a 5-hour limit or a stale reply: it is logged and switches nothing.
A refusal never opens a weekly episode.
A late refusal from an outgoing account names it and changes no assignments.
When the selected account can work on allowance or credits, owed entries still
on Claude return to Codex independently of weekly episodes. Each
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
import re
import signal
import subprocess
import sys
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, NotRequired, Protocol, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.user_zone import user_zone
from scripts.whoami import agent_accounts

from agent_accounts import (ClaudeAccountSelection, ClaudeRole, CodexHomeSource, Report,
                            codex_can_work, CodexWorkAvailable, codex_email_on_disk,
                            codex_homes, codex_live, seats_home)

if TYPE_CHECKING:
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
    """Recommend only a signed-in target accepted by the swap's two checks."""
    from scripts.accounts import accounts

    choices: list[tuple[float, str]] = []
    for checked in readings.checked_homes:
        if checked.home == readings.seats.home or not timedelta(0) <= now - checked.observed_at <= CODEX_READING_MAX_AGE:
            continue
        report = checked.report
        window = report.five_hour
        if (report.problem or report.quota_problem or window is None
                or window.remaining_percent is None or window.resets_at is None or window.resets_at <= now):
            continue
        label = agent_accounts.codex_home_role_label(checked.home)
        try:
            target = accounts.check_codex_target(label, readings.seats.home)
        except accounts.AccountsError:
            continue
        if (target.home != checked.home or not report.email
                or report.email.casefold() != target.login.casefold()
                or not isinstance(codex_can_work(report, now), CodexWorkAvailable)):
            continue
        weekly = report.weekly_remaining
        reset = report.weekly_reset
        if weekly is None or reset is None or reset <= now or report.credit_balance is None:
            continue
        command = "default" if label == "default" else json.dumps(label)
        display = accounts.codex_home_label(checked.home, target.login)
        choices.append((weekly, f"swap: /showrunner:accounts swap seats {command} — {display}: "
                        + f"{weekly:g}% weekly left; credits {report.credit_balance}"))
    return max(choices, default=(0, ""))[1]


@dataclass(frozen=True)
class ReadCurrentClaudeSelection:
    """A standalone alert call resolves its account selection before doing any work."""


READ_CURRENT_CLAUDE_SELECTION = ReadCurrentClaudeSelection()


def _selection(value: ClaudeAccountSelection | ReadCurrentClaudeSelection) -> ClaudeAccountSelection:
    if isinstance(value, ReadCurrentClaudeSelection):
        # Resolve only standalone calls; injected selections are already fixed.
        from scripts.accounts import accounts

        return accounts.claude_account_selection()
    return value


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# Direct script execution and unittest discovery import this as a top-level module.
if __package__ in (None, ""):
    __package__ = "scripts.whoami"

from ..production.showrunners import (
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
    name: str
    summary: str
    text: str
    # None for a note, which asks nothing of the user.
    ask: Ask | None


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


def recovered(name: str, notes: list[AgentNote], threshold: float,
              selection: ClaudeAccountSelection, now: datetime,
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
        try:
            return datetime.fromisoformat(note.get("resets") or "").astimezone() <= now
        except ValueError:
            return False
    if selection.multiple_accounts and name.startswith("claude "):
        if note is None:
            return False
        value = remaining(note)
        if value is not None and math.isfinite(value) and value > threshold:
            return True
        try:
            return datetime.fromisoformat(note.get("resets") or "").astimezone() <= now
        except ValueError:
            return False
    if note is None or note.get("state") == "inactive":
        return True
    value = remaining(note)
    return value is not None and value > threshold


def close_recovered(state: State, notes: list[AgentNote], threshold: float,
                    selection: ClaudeAccountSelection, now: datetime,
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
    fallback = state["fallback"]
    if isinstance(fallback, NoCodexQuotaFallback) or not isinstance(
            codex_can_work(selected_codex_report(readings, now), now), CodexWorkAvailable):
        return NoCodexFallbackRestoration()
    state["fallback"] = NoCodexQuotaFallback()
    if isinstance(fallback, AccountCodexQuotaFallback):
        _ = state.get("refusal_notices", {}).pop(str(fallback.owner.home), None)
    try:
        current = registry_assignments()
    except OSError as error:
        back = SwitchBack([], [], [f"{key} ({error})" for key in fallback.moved])
    else:
        back = SwitchBack([], [], [])
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


def account_line(note: AgentNote, threshold: float, selection: ClaudeAccountSelection) -> str:
    roles = claude_roles(note, selection)
    if note.tool == "claude" and selection.multiple_accounts:
        return (f"{note.path.stem} ({', '.join(roles) or 'spare'}) at {percent(note)} of its weekly "
                + f"usage left (threshold {threshold:g}%); it resets {note.get('resets')}.")
    return (f"{note.path.stem}, the active {note.tool.capitalize()} account, has {percent(note)} of its weekly "
            + f"usage left (threshold {threshold:g}%); it resets {note.get('resets')}.")


def claude_roles(note: AgentNote, selection: ClaudeAccountSelection) -> tuple[ClaudeRole, ...]:
    """Current roles, with legacy accounts retaining their original alert text."""
    if note.tool != "claude" or not selection.multiple_accounts:
        return ()
    return selection.roles_for_login(note.get("login") or "")


def unit_swap_line(note: AgentNote, notes: list[AgentNote], selection: ClaudeAccountSelection) -> str:
    """Suggest the fit spare with most measured quota; never perform a swap."""
    current = remaining(note)
    choices: list[tuple[float, str]] = []
    for named in selection.named_accounts:
        login = named.identity.login
        if selection.roles_for_login(login):
            continue
        for spare in notes:
            if (
                spare.tool != "claude"
                or (spare.get("login") or "").casefold() != login.casefold()
                or not ahead(spare.get("resets"))
            ):
                continue
            value = remaining(spare)
            if (
                value is not None and current is not None
                and math.isfinite(value) and 0 <= value <= 100 and value > current
            ):
                choices.append((value, named.label))
    if not choices:
        return "no spare account has more left"
    _, label = max(choices, key=lambda choice: (choice[0], choice[1]))
    return f"swap: /showrunner:accounts swap unit {json.dumps(label)}"


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
        return datetime.fromisoformat(stamp or "").astimezone(user_zone()).strftime("%a %m-%d %H:%M %Z")
    except ValueError:
        return "an unknown time"


def run_out_push(note: AgentNote, selection: ClaudeAccountSelection) -> UserPush:
    """The one direct user message sent when this account's episode opens."""
    name = note.path.stem
    roles = claude_roles(note, selection)
    display_name = f"{name} ({', '.join(roles)})" if roles else name
    summary = f"{name} weekly limit"
    first = f"{display_name} has {percent(note)} of its weekly usage left; it refills {user_time(note.get('resets'))}."
    if note.tool == "codex":
        return UserPush(name, summary, f"{first} Codex keeps working on its credits. {credit_line(note)}", None)
    count = note.get("limit_reset_count")
    resets = f" ({count} available)" if count is not None and count != "null" else ""
    return UserPush(name, summary, first,
                    Ask("decision", f"Log in to another Claude account, or redeem a limit reset{resets}."))


def refusal_push(note: AgentNote, switch: CodexQuotaFallback, failed: str | None, already: bool) -> UserPush:
    """The one direct user message sent when Codex refuses this episode."""
    if not isinstance(switch, NoCodexQuotaFallback):
        verb = "had already moved" if already else "moved"
        outcome = f"The automatic switch {verb} {', '.join(switch.moved)} to Claude"
    elif failed:
        reason = re.sub(r"/\S+", "the account registry", failed)
        outcome = f"The automatic switch to Claude failed: {reason}"
    else:
        outcome = "Nothing was on Codex, so the automatic switch moved nothing"
    name = note.path.stem
    return UserPush(name, "Codex refused work", f"Codex refused work on {name}. {credit_line(note)} {outcome}.",
                    Ask("decision", f"Add credits to {name} or redeem a limit reset to bring Codex back."))


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
            selection: ClaudeAccountSelection | ReadCurrentClaudeSelection = READ_CURRENT_CLAUDE_SELECTION,
            codex_readings: CodexAlertPassReadings | ReadCurrentCodexReadings = READ_CURRENT_CODEX_READINGS,
            now: datetime | None = None) -> str:
    selection = _selection(selection)
    name = note.path.stem
    tool = note.tool.capitalize()
    threshold = f"{config['threshold_percent']:g}%"
    lines = [f"Quota alert: {account_line(note, config['threshold_percent'], selection)}"]
    roles = claude_roles(note, selection)
    if "unit directors" in roles:
        lines.append(unit_swap_line(note, notes, selection))
    if note.tool == "codex":
        lines.append(credit_line(note))
        readings = resolve_codex_readings(codex_readings)
        swap_line = codex_swap_line(readings, now or datetime.now(timezone.utc))
        if swap_line:
            lines.append(swap_line)
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
                     selection: ClaudeAccountSelection | ReadCurrentClaudeSelection = READ_CURRENT_CLAUDE_SELECTION) -> str:
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


def tell_user(summary: str, text: str, ask: Ask | None) -> str | None:
    """Deliver text directly to the user, as a note when `ask` is None; None when it arrived, otherwise why not."""
    asked = ["--no-action"] if ask is None else ["--need", ask.need, "--action", ask.action]
    command = [sys.executable, str(SEND), "--to", "user", "--from", "quota_alert", "--summary", summary, *asked,
               "--timeout", str(RELAY_TIMEOUT - KILL_GRACE)]
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


def deliver_and_record(jobs: list[Job], stamp: str) -> list[str]:
    """Deliver every job, stamp each alert's attempt on its episode, and return one log line per job."""
    errors = deliver(jobs)
    log: list[str] = []
    with state_file() as state:
        for job, error in zip(jobs, errors):
            if job.kind == "alert" and job.name in state["episodes"]:
                state["episodes"][job.name]["last"][job.recipient] = stamp
            outcome = "sent" if error is None else f"not delivered: {error}"
            log.append(f"quota {job.kind} {job.name} -> {job.recipient}: {outcome}")
    return log


def alert(notes: list[AgentNote], now: datetime | None = None, *,
          selection: ClaudeAccountSelection | ReadCurrentClaudeSelection = READ_CURRENT_CLAUDE_SELECTION,
          codex_readings: CodexAlertPassReadings | ReadCurrentCodexReadings = READ_CURRENT_CODEX_READINGS) -> list[str]:
    """Send what is due, record it, and return one log line per attempt."""
    selection = _selection(selection)
    codex_readings = resolve_codex_readings(codex_readings)
    now = now or datetime.now(timezone.utc)
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
        for note in [*low([note for note in notes if note.tool != "codex"], threshold),
                     *_low_codex_notes(notes, threshold, codex_readings, now)]:
            if (note.tool == "claude" and selection.multiple_accounts
                    and not selection.roles_for_login(note.get("login") or "")):
                continue
            name = note.path.stem
            if name not in episodes:
                episodes[name] = {"since": stamp, "push": "due", "last": {}}
                if note.tool == "codex":
                    reset = selected_codex_report(codex_readings, now).weekly_reset
                    if reset is not None:
                        episodes[name]["weekly_reset"] = reset.isoformat()
            episode = episodes[name]
            if episode.get("push") == "due":
                push = run_out_push(note, selection)
                swap_line = codex_swap_line(codex_readings, now) if note.tool == "codex" else ""
                pushes.append(UserPush(push.name, push.summary, push.text + ("\n" + swap_line if swap_line else ""), push.ask))
            if "acknowledged" in episode:
                continue
            switch = state["fallback"] if note.tool == "codex" else NoCodexQuotaFallback()
            text = message(note, notes, config, switch, episode.get("switch_failed"), selection=selection, codex_readings=codex_readings, now=now)
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
        push_futures = [pool.submit(tell_user, push.summary, push.text, push.ask) for push in pushes]
        push_errors = [future.result() for future in push_futures]
        relay_log = relay_future.result()
    if pushes:
        with state_file() as state:
            for push, error in zip(pushes, push_errors):
                episode = state["episodes"].get(push.name)
                if error is None and episode is not None and episode.get("push") == "due":
                    episode["push"] = stamp
                outcome = "sent" if error is None else f"not delivered: {error}"
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


def blocked(notes: list[AgentNote], refusing_account: agent_accounts.RefusingCodexAccount,
            now: datetime | None = None, *,
            selection: ClaudeAccountSelection | ReadCurrentClaudeSelection = READ_CURRENT_CLAUDE_SELECTION,
            codex_readings: CodexAlertPassReadings | ReadCurrentCodexReadings = READ_CURRENT_CODEX_READINGS) -> list[str]:
    """Only a selected account's low weekly quota or open episode permits fallback."""
    selection = _selection(selection)
    readings = resolve_codex_readings(codex_readings)
    now = now or datetime.now(timezone.utc)
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
    note = next((note for note in notes if note.tool == "codex"
                 and (note.get("login") or "").casefold() == refusing_account.login.casefold()),
                RefusingAccountNote(Path(name + ".md"), {}))
    selected = readings.selected_account
    owns_role = (isinstance(selected, agent_accounts.KnownCodexAccount) and selected.home == refusing_account.home
                 and selected.login.casefold() == refusing_account.login.casefold()
                 and selected.home == readings.seats.home)
    account_status = ("the outgoing account" if isinstance(selected, agent_accounts.KnownCodexAccount)
                      else "an account whose seats selection is unknown")
    with state_file() as state:
        notices = state.setdefault("refusal_notices", {})
        previous = notices.get(home)
        if previous is not None and previous["kind"] == "unknown":
            del notices[home]
            previous = None
        episode = state["episodes"].get(name)
        report = selected_codex_report(readings, now)
        left, reset = report.weekly_remaining, report.weekly_reset
        low_week = (not report.problem and left is not None and math.isfinite(left)
                    and 0 <= left <= config["threshold_percent"] and reset is not None and reset > now)
        if owns_role and episode is None and not low_week:
            return [f"{name} refused work above the weekly threshold or with unknown weekly usage; nothing switched"]
        notice = (previous if previous is not None and previous["kind"] == "known"
                  and previous["login"].casefold() == refusing_account.login.casefold()
                  else KnownRefusalNotice(kind="known", login=refusing_account.login, label=name))
        notices[home] = notice
        fallback = state["fallback"]
        already = not isinstance(fallback, NoCodexQuotaFallback)
        log: list[str] = []
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
                           selection=selection, codex_readings=readings, now=now)
            push = refusal_push(note, fallback, failed, already)
        else:
            text = f"Codex refused work on {name}, {account_status}. Nothing switched to Claude."
            push = UserPush(name, f"{name} refused work", text, None)
        swap_line = codex_swap_line(readings, now)
        if swap_line:
            if not owns_role:
                text += "\n" + swap_line
            push = UserPush(push.name, push.summary, push.text + "\n" + swap_line, push.ask)
        new_failure = owns_role and not already and failed is not None and failed not in notice.get("failure_pushes", [])
        if not moved and not new_failure and ("push" in notice or episode is not None and "refusal_push" in episode):
            return log
    log += deliver_and_record([Job(name, recipient, text, "refusal") for recipient in recipients(config)], stamp)
    error = tell_user(push.summary, push.text, push.ask)
    if error is None:
        with state_file() as state:
            notices = state.setdefault("refusal_notices", {})
            notice["push"] = stamp
            if failed is not None:
                notice.setdefault("failure_pushes", []).append(failed)
            notices[home] = notice
            if name in state["episodes"]:
                state["episodes"][name]["refusal_push"] = stamp
    log.append(f"quota refusal {name} -> user: " + ("sent" if error is None else f"not delivered: {error}"))
    return log


def refresh(notes: list[AgentNote], here: str | None, now: datetime | None = None, *,
            selection: ClaudeAccountSelection | ReadCurrentClaudeSelection = READ_CURRENT_CLAUDE_SELECTION,
            codex_readings: CodexAlertPassReadings | ReadCurrentCodexReadings = READ_CURRENT_CODEX_READINGS) -> list[str]:
    """After the user reports a reset: close what recovered, alert what is still low, tell the others.

    The notice goes out even when the timer already closed the episode, since that
    is the case where nothing else has told anyone.
    """
    selection = _selection(selection)
    codex_readings = resolve_codex_readings(codex_readings)
    now = now or datetime.now(timezone.utc)
    threshold = load_config()["threshold_percent"]
    with state_file() as state:
        closed = close_recovered(state, notes, threshold, selection, now, codex_readings)
        back = switch_back(state, now, codex_readings)
    lines = [f"{name}: alert closed" for name in closed]
    lines += [f"switch to claude undone: {back.describe()}"] if isinstance(back, SwitchBack) else []
    lines += alert(notes, now, selection=selection, codex_readings=codex_readings)
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
