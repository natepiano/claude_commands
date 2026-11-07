"""Describe the source evidence for one submitted prompt."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptEvidence:
    session_id: str = ""
    pane_id: str = ""
    prompt_hash: str = ""
    evidence_kind: str = "classifier"


@dataclass(frozen=True)
class UserTypedPrompt(PromptEvidence):
    """The classifier found user input without a controlled injection marker."""


@dataclass(frozen=True)
class PeerPrompt(PromptEvidence):
    """Another session supplied the prompt."""


@dataclass(frozen=True)
class ScriptPrompt(PromptEvidence):
    """A script supplied the prompt."""


@dataclass(frozen=True)
class HarnessNotice(PromptEvidence):
    """The harness supplied the prompt."""


@dataclass(frozen=True)
class UnknownPrompt(PromptEvidence):
    """The prompt has no usable source evidence."""


type PromptActivity = UserTypedPrompt | PeerPrompt | ScriptPrompt | HarnessNotice | UnknownPrompt

SOURCES: dict[str, type[PromptActivity]] = {
    kind.__name__: kind for kind in (UserTypedPrompt, PeerPrompt, ScriptPrompt, HarnessNotice, UnknownPrompt)
}
