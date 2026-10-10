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


def refused_at(action: UserAction, priority: int) -> ActionRefused | None:
    """Refuse action-free high or emergency alerts; emergency means act now."""
    if isinstance(action, NoActionRequired) and priority > 0:
        return ActionRefused(f"priority {priority} requires --action")
    return None


def first_line(action: UserAction) -> str:
    """The mandatory first line of a notification."""
    if isinstance(action, ActionRequired):
        return f"Action: {action.text}"
    return "No action needed."
