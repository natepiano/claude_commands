"""Classify what one model request did: work, wait on a running job, or post a status.

Standard library only: the collector ships this module to the Mac.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, cast


Activity = Literal["work", "wait", "status"]
# Rows stored before request activity was recorded keep this value.
UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class Working:
    pass


@dataclass(frozen=True)
class Waiting:
    action: str


@dataclass(frozen=True)
class PostingStatus:
    pass


RequestActivity = Working | Waiting | PostingStatus


def activity_name(value: RequestActivity) -> Activity:
    if isinstance(value, Waiting):
        return "wait"
    return "status" if isinstance(value, PostingStatus) else "work"


def wait_action(value: RequestActivity) -> str | None:
    return value.action if isinstance(value, Waiting) else None


def decode_activity(activity: object, action: object) -> RequestActivity:
    """Read a stored or transmitted pair strictly; raise ValueError for any other pair."""
    if activity == "wait":
        if not isinstance(action, str) or not action:
            raise ValueError("a waiting request needs its action")
        return Waiting(action)
    if action is not None:
        raise ValueError("only a waiting request carries an action")
    if activity == "work":
        return Working()
    if activity == "status":
        return PostingStatus()
    raise ValueError("unknown request activity")


@dataclass(frozen=True)
class WorkAtom:
    pass


@dataclass(frozen=True)
class WaitAtom:
    action: str


@dataclass(frozen=True)
class StatusAtom:
    pass


@dataclass(frozen=True)
class NeutralAtom:
    pass


Atom = WorkAtom | WaitAtom | StatusAtom | NeutralAtom


@dataclass
class RequestEvidence:
    """Atoms seen so far for one request; work outranks waiting, waiting outranks a status post."""

    worked: bool = False
    first_wait: Waiting | None = None
    posted_status: bool = False

    def add(self, atoms: Iterable[Atom]) -> None:
        for atom in atoms:
            if isinstance(atom, WorkAtom):
                self.worked = True
            elif isinstance(atom, WaitAtom):
                if self.first_wait is None:
                    self.first_wait = Waiting(atom.action)
            elif isinstance(atom, StatusAtom):
                self.posted_status = True

    def result(self) -> RequestActivity:
        if self.worked:
            return Working()
        if self.first_wait is not None:
            return self.first_wait
        return PostingStatus() if self.posted_status else Working()


# Shell commands.

HEREDOC = re.compile(r"(?<!<)<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
QUOTED = re.compile(r'"((?:\\.|[^"\\])*)"|\'([^\']*)\'', re.S)
QUOTED_SEPARATORS = re.compile(r"[\s;|&<>()]")
DISCARDED_REDIRECTION = re.compile(r"(?:&|\d*)>>?\s*/dev/null\b|\d*>&\d+-?")
STATEMENT_SEPARATOR = re.compile(r"&&|\|\||;|\n|&")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
STATUS_FILE = re.compile(r"\.(log|out|output|status|exit|rc|done|pid|code)\b|/proc/loadavg|sentinel|marker|exit_code")
CONTROL_WORDS = frozenset({"while", "until", "if", "elif", "then", "do", "!", "{", "(", "else"})
NEUTRAL_WORDS = frozenset({"done", "fi", "}", ")", "break", "continue", "exit", "echo", "printf", "true", ":",
                           "date", "cd", "set", "for", "return", "local", "export", "esac"})
FILTERS = frozenset({"grep", "head", "tail", "wc", "awk", "sed", "sort", "uniq", "cut", "tr", "jq", "rg", "cat"})
STATUS_READERS = frozenset({"cat", "grep", "rg", "wc", "stat", "head"})


def without_heredoc_bodies(command: str) -> str:
    """Keep each heredoc operator line and drop the body through its terminator."""
    kept: list[str] = []
    pending: list[tuple[str, bool]] = []
    for line in command.split("\n"):
        if pending:
            terminator, tabs_allowed = pending[0]
            if (line.lstrip("\t") if tabs_allowed else line) == terminator:
                _ = pending.pop(0)
            continue
        kept.append(line)
        pending = [(match.group(3), match.group(1) == "-") for match in HEREDOC.finditer(line)]
    return "\n".join(kept)


def quoted_text(match: re.Match[str]) -> str:
    content = match.group(1) if match.group(1) is not None else match.group(2)
    return QUOTED_SEPARATORS.sub("_", content)


def status_file_label(text: str) -> str | None:
    match = STATUS_FILE.search(text)
    if match is None:
        return None
    return f"*.{match.group(1)}" if match.group(1) else match.group(0)


def statement_atom(statement: str) -> Atom:
    words = statement.split()
    while words and words[0] in CONTROL_WORDS:
        words = words[1:]
    while words and ASSIGNMENT.match(words[0]) and "$(" not in words[0]:
        words = words[1:]
    if not words:
        return NeutralAtom()
    if ASSIGNMENT.match(words[0]):
        # x=$(command) runs the command.
        words = " ".join(words).split("$(", 1)[1].rstrip(")").split() or ["true"]
    name = os.path.basename(words[0])
    rest = " ".join(words[1:])
    if name in ("sleep", "wait"):
        return WaitAtom(name)
    if name in NEUTRAL_WORDS or words[0] in NEUTRAL_WORDS:
        return NeutralAtom()
    if name == "board.sh" or name == "bash" and len(words) > 1 and words[1].endswith("board.sh"):
        arguments = words[2:] if name == "bash" else words[1:]
        if arguments and arguments[0] == "read":
            return WaitAtom("board.sh read")
        if len(arguments) >= 4 and arguments[0] == "post" and arguments[3] == "status":
            return StatusAtom()
        return WorkAtom()
    label = status_file_label(rest)
    if name == "tail" and ">" not in rest:
        return WaitAtom(f"tail {label}" if label else "tail")
    if name in STATUS_READERS and label and ">" not in rest and "<<" not in rest:
        return WaitAtom(f"{name} {label}")
    if name in ("test", "[", "[["):
        return WaitAtom("test")
    if name in ("ps", "pgrep"):
        return WaitAtom(name)
    if name == "kill" and rest.startswith("-0"):
        return WaitAtom("kill -0")
    return WorkAtom()


def shell_atoms(command: str) -> tuple[Atom, ...]:
    """One atom per statement of a shell command."""
    visible = QUOTED.sub(quoted_text, without_heredoc_bodies(command))
    visible = DISCARDED_REDIRECTION.sub(" ", visible)
    atoms: list[Atom] = []
    for statement in STATEMENT_SEPARATOR.split(visible):
        if not statement.strip():
            continue
        first, *later = statement.split("|")
        # A pipe into anything but a filter (or a filter writing a file) does work.
        piped_work = any(
            (words := piece.split()) and (os.path.basename(words[0]) not in FILTERS or ">" in piece)
            for piece in later)
        atoms.append(WorkAtom() if piped_work else statement_atom(first))
    return tuple(atoms)


# Codex rollout records.

STRING_LITERAL = r"""(["'`])((?:\\.|(?!\1).)*)\1"""
TOOL_CALL = re.compile(r"tools\.(\w+)\(")
COMMAND_FIELD = re.compile(r"""["']?cmd["']?\s*:\s*""" + STRING_LITERAL, re.S)
CHARS_FIELD = re.compile(r"""["']?chars["']?\s*:\s*""" + STRING_LITERAL, re.S)
JAVASCRIPT_ESCAPE = re.compile(r"\\(.)", re.S)
IGNORED_ITEMS = frozenset({"message", "reasoning", "function_call_output", "custom_tool_call_output"})


def javascript_unescaped(text: str) -> str:
    return JAVASCRIPT_ESCAPE.sub(
        lambda match: {"n": "\n", "t": "\t"}.get(match.group(1), match.group(1)), text)


def exec_cell_atoms(source: str) -> tuple[Atom, ...]:
    """Atoms of one Codex exec cell, whose source is JavaScript calling tools.*.

    Order: timer, tool names, chars fields, then commands; the first wait atom names the request.
    """
    atoms: list[Atom] = []
    if "setTimeout(" in source:
        atoms.append(WaitAtom("setTimeout"))
    calls = cast(list[str], TOOL_CALL.findall(source))
    if not calls and not atoms:
        atoms.append(WorkAtom())
    if any(name not in ("exec_command", "write_stdin") for name in calls):
        atoms.append(WorkAtom())
    chars = [match.group(2) for match in CHARS_FIELD.finditer(source)]
    atoms.extend(WaitAtom("empty write_stdin") if not text else WorkAtom() for text in chars)
    if calls.count("write_stdin") > len(chars):
        atoms.append(WaitAtom("empty write_stdin"))
    for match in COMMAND_FIELD.finditer(source):
        atoms.extend(shell_atoms(javascript_unescaped(match.group(2))))
    return tuple(atoms)


def object_fields(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def codex_atoms(record: dict[str, object]) -> tuple[Atom, ...]:
    """Atoms of one rollout record; only response items carry any."""
    if record.get("type") != "response_item":
        return ()
    payload = object_fields(record.get("payload"))
    item = payload.get("type")
    if item == "custom_tool_call":
        tool_input = payload.get("input")
        if payload.get("name") == "exec" and isinstance(tool_input, str):
            return exec_cell_atoms(tool_input)
        return (WorkAtom(),)
    if item == "function_call":
        name = payload.get("name")
        if name == "sleep":
            return (WaitAtom("sleep tool"),)
        return (WaitAtom("wait tool"),) if name == "wait" else (WorkAtom(),)
    if item == "message":
        final = payload.get("role") == "assistant" and payload.get("phase") == "final_answer"
        return (WorkAtom(),) if final else ()
    return () if item in IGNORED_ITEMS else (WorkAtom(),)


# Claude transcript records.

def claude_atoms(record: dict[str, object]) -> tuple[Atom, ...]:
    """Atoms of one assistant transcript line; text and thinking blocks carry none."""
    content = object_fields(record.get("message")).get("content")
    if record.get("type") != "assistant" or not isinstance(content, list):
        return ()
    atoms: list[Atom] = []
    for value in cast(list[object], content):
        block = object_fields(value)
        if block.get("type") != "tool_use":
            continue
        name = block.get("name")
        tool_input = object_fields(block.get("input"))
        command = tool_input.get("command")
        path = tool_input.get("file_path")
        if name == "Bash" and isinstance(command, str):
            atoms.extend(shell_atoms(command))
        elif name == "Read" and isinstance(path, str) and (label := status_file_label(path)):
            atoms.append(WaitAtom(f"Read {label}"))
        else:
            atoms.append(WorkAtom())
    return tuple(atoms)
