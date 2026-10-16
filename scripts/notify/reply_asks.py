#!/usr/bin/env python3
"""Identify questions and recommended choices in reply prose without reviewer dependencies."""
from __future__ import annotations

import re
from dataclasses import dataclass


def question_prose(text: str) -> str:
    return " ".join(re.sub(r"(`+).*?\1", "", text).split())


@dataclass(frozen=True)
class ReplyAsks:
    sentences: tuple[str, ...]
    has_choices: bool
    choice_lines: tuple[str, ...] = ()


def reply_questions(text: str) -> ReplyAsks:
    """Separate asking sentences from choices, excluding code and quoted examples."""
    questions: list[str] = []
    choice_lines: list[str] = []
    fence = ""
    for line in text.splitlines():
        stripped = line.lstrip()
        marker = re.match(r"^(`{3,}|~{3,})", stripped)
        if marker:
            if not fence:
                fence = marker.group(1)
            elif stripped.startswith(fence):
                fence = ""
            continue
        if fence or stripped.startswith(">"):
            continue
        prose = question_prose(line)
        if "(recommended" in prose.lower():
            choice_lines.append(" ".join(line.split()))
        for sentence in re.split(r"(?<=[.!?])\s+", prose):
            sentence = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", sentence).strip()
            request = re.match(
                r"(?i)^(?:please\s+)?(?:reply|choose|pick|confirm|decide|approve|tell me|let me know|say|"
                + r"would you|do you want|shall I|should I)\b", sentence)
            if sentence.endswith("?") or request:
                questions.append(" ".join(sentence.split()))
    return ReplyAsks(tuple(questions), bool(choice_lines), tuple(choice_lines))
