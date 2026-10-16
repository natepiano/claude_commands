#!/usr/bin/env python3
"""Identify reply questions and their recipients without reviewer dependencies."""
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


def peer_question_lead(line: str) -> bool:
    """Recognise an explicit recipient heading or attributed message."""
    lead = re.sub(r"[*_]", "", question_prose(line)).strip()
    lead = re.sub(r"^(?:#{1,6}\s+|[-+]\s+|\d+[.)]\s+)", "", lead)
    heading = lead.rsplit(":", 1)[0] if lead.endswith(":") else lead.split(":", 1)[0]
    role = r"\b(?:unit(?: director)?|session|worker|agent|showrunner)\b"
    if not re.search(role, heading, re.IGNORECASE):
        return False
    if re.search(r"\b(?:you|user)\b", heading, re.IGNORECASE):
        return False
    recipient = (r"(?:the\s+)?(?:[\w./-]+\s+)?" + role
                 + r"(?::[\w./-]+)?")
    quoted_words = r"['’]s\s+(?:(?:own|exact)\s+)?(?:words|choice|quotation)"
    named_heading = re.fullmatch(
        r"(?i)(?:(?:to|for)\s+" + recipient + r"(?:" + quoted_words + r")?"
        + r"|" + recipient + quoted_words + r")", heading)
    if ":" not in lead:
        return bool(named_heading)
    relay = re.fullmatch(
        r"(?i)^(?:(?:also|already)\s+)?(?:(?:I|we)\s+)?"
        + r"(?:(?:sent|relayed|forwarded|message|question|one question)\s+(?:to|for)\s+"
        + r"|(?:to|for)\s+|asked\s+)" + recipient, heading)
    attribution = re.match(r"(?i)^(?:quoted\s+from|(?:a\s+)?(?:quote|choice|message)\s+from)\s+", heading)
    speaking = re.search(r"(?i)\b(?:said|wrote|asked|in (?:its|their|his|her) own words|(?:own|exact) words)\s*$", heading)
    return bool(relay or attribution or speaking or named_heading)


def user_reply_questions(text: str) -> ReplyAsks:
    """Apply recipient context only to the Stop hook's registration check."""
    user_lines: list[str] = []
    peer_paragraph = False
    peer_body_next = False
    fence = ""
    for line in text.splitlines():
        stripped = line.lstrip()
        marker = re.match(r"^(`{3,}|~{3,})", stripped)
        if marker:
            peer_body_next = False
            if not fence:
                fence = marker.group(1)
            elif stripped.startswith(fence):
                fence = ""
            user_lines.append(line)
            continue
        if fence or stripped.startswith(">"):
            peer_body_next = False
            user_lines.append(line)
            continue
        if not stripped:
            peer_paragraph = False
            user_lines.append(line)
            continue
        if not question_prose(line):
            peer_body_next = False
            user_lines.append(line)
            continue
        # Keep positions so the user's remaining text retains its inline code.
        lead = re.sub(r"(`+).*?\1", lambda match: " " * len(match.group()), line)
        lead = re.sub(r"[*_#]", " ", lead)
        peer_lead = peer_question_lead(line)
        user_return = re.search(
            r"(?i)\b(?:back to you|for you|to (?:you|the user)|question for (?:you|the user)|your decision):", lead)
        if user_return and (peer_paragraph or peer_body_next or peer_lead or not lead[:user_return.start()].strip()):
            peer_paragraph = peer_body_next = False
            user_lines.append(line[user_return.end():].lstrip(" *_\t"))
            continue
        if peer_lead:
            peer_paragraph = True
            peer_body_next = lead.rstrip().endswith(":") or ":" not in lead
        elif peer_body_next:
            peer_paragraph = True
            peer_body_next = False
        user_lines.append("" if peer_paragraph else line)
    return reply_questions("\n".join(user_lines))
