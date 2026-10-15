"""The action a notification says the user takes.

Priority 1 is high and priority 2 is emergency. Both require an action. Emergency
is reserved for an action the user must take now because work cannot continue.
"""

from __future__ import annotations

from dataclasses import dataclass

FIX = ('Say what the user does with --action "<what they do>", or pass '
       "--no-action when nothing is needed (priority 0 only).")
REFUSED_BEFORE_SENDING = "refused before sending"

_PLACEHOLDERS = frozenset(("none", "n/a", "nothing", "no action", "-"))


@dataclass(frozen=True)
class ActionRequired:
    text: str


@dataclass(frozen=True)
class NoActionRequired:
    pass


UserAction = ActionRequired | NoActionRequired


@dataclass(frozen=True)
class ActionUnstated:
    pass


@dataclass(frozen=True)
class ActionRefused:
    reason: str


@dataclass(frozen=True)
class ActionAllowed:
    pass


def parse_user_action(action: str | None, no_action: bool) -> UserAction | ActionUnstated | ActionRefused:
    """Turn the two command-line forms into one explicit action state."""
    if action is not None and no_action:
        return ActionRefused("--action and --no-action cannot be used together")
    if action is None:
        return NoActionRequired() if no_action else ActionUnstated()
    text = action.strip()
    if not text:
        return ActionRefused("--action cannot be empty")
    if text.casefold() in _PLACEHOLDERS:
        return ActionRefused(f'--action "{text}" does not state an action; use --no-action')
    return ActionRequired(text)


def refused_at(action: UserAction, priority: int) -> ActionAllowed | ActionRefused:
    """Refuse action-free high or emergency alerts; emergency means act now."""
    if isinstance(action, NoActionRequired) and priority > 0:
        return ActionRefused(f"priority {priority} requires --action")
    return ActionAllowed()


@dataclass(frozen=True)
class SessionLine:
    text: str


@dataclass(frozen=True)
class NoSessionLine:
    pass


NotificationSession = SessionLine | NoSessionLine
NO_SESSION_LINE = NoSessionLine()


def session_prefix(session: NotificationSession) -> str:
    return f"Session: {session.text}\n" if isinstance(session, SessionLine) else ""


def split_session(message: str) -> tuple[NotificationSession, str]:
    line, separator, body = message.partition("\n")
    if line.startswith("Session: "):
        return SessionLine(line.removeprefix("Session: ")), body if separator else ""
    return NO_SESSION_LINE, message


def first_line(action: UserAction, session: NotificationSession = NO_SESSION_LINE) -> str:
    """The notification header, with its session when stated."""
    if isinstance(action, ActionRequired):
        return session_prefix(session) + f"Action: {action.text}"
    return session_prefix(session) + "No action needed."
