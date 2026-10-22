#!/usr/bin/env python3
"""Refuse a sed or awk read, or a change or copy, of saved tool output that would wait on a permission prompt.

A session's large tool results are saved under `~/.claude*/projects/**/tool-results/`.
Claude Code asks before it runs `awk`, or a sed script with a `$` or `/re/` address, because
such scripts can also run commands or write files. That prompt waits on the user. The Read
tool, `grep -n`, `tail -n +N`, `head -N | tail -M` and `sed -n 'N,Mp'` read the same lines
with no prompt (probed 2026-10-18), so this hook refuses the call and names them.

`rm`, `cp`, `mv` and `tee` with a saved-output path, and a `>` redirect into one, also ask, and
units have waited hours on them (2026-10-21). Nothing needs deleting, so this hook refuses them
and names `cat <file> > /tmp/claude/<name>` to keep a copy. A command this hook cannot split is
allowed.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from typing import TypedDict, cast

TOOL_OUTPUT = re.compile(r"\.claude\d*/projects/\S*tool-results/")
# The sed scripts that run with no prompt: print one line, or a range of lines, by number.
LINE_PRINT = re.compile(r"\d+(,\d+)?p")
SEPARATORS = frozenset(("|", "||", "&&", ";", "&", "|&", "(", ")", "\n"))
AWK = frozenset(("awk", "gawk", "mawk", "nawk"))
WRITERS = frozenset(("rm", "cp", "mv", "tee"))


class BashInput(TypedDict, total=False):
    command: str


class HookInput(TypedDict, total=False):
    """The PreToolUse fields this hook reads."""

    tool_name: str
    tool_input: BashInput


def bash_command(raw: str) -> str | None:
    """The command of a Bash tool call, or None for any other input."""
    try:
        hook_input = cast(HookInput, json.loads(raw))
        if hook_input.get("tool_name") != "Bash":
            return None
        command = hook_input.get("tool_input", {}).get("command")
    except (ValueError, AttributeError):
        return None
    return command if isinstance(command, str) else None


def segments(command: str) -> list[list[str]] | None:
    """The simple commands of a command line, as words, or None when it cannot be split."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace = " \t"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    found: list[list[str]] = [[]]
    for token in tokens:
        if token in SEPARATORS:
            found.append([])
        else:
            found[-1].append(token)
    return [words for words in found if words]


def sed_scripts(arguments: list[str]) -> list[str]:
    """The scripts a sed call runs: each `-e` value, else its first operand."""
    scripts: list[str] = []
    operands: list[str] = []
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word in ("-e", "--expression") and index + 1 < len(arguments):
            scripts.append(arguments[index + 1])
            index += 2
            continue
        if word.startswith("--expression="):
            scripts.append(word.split("=", 1)[1])
        elif word.startswith("-e") and len(word) > 2:
            scripts.append(word[2:])
        elif not word.startswith("-"):
            operands.append(word)
        index += 1
    return scripts or operands[:1]


def asks_permission(words: list[str]) -> str | None:
    """The program name when this simple command is a read that prompts, else None."""
    program = words[0].rsplit("/", 1)[-1]
    if program in AWK:
        return program
    if program == "sed" and not all(LINE_PRINT.fullmatch(script.strip()) for script in sed_scripts(words[1:])):
        return program
    return None


def changes_output(words: list[str]) -> str | None:
    """The program or redirect when this simple command changes or copies saved tool output, else None."""
    program = words[0].rsplit("/", 1)[-1]
    if program in WRITERS and any(TOOL_OUTPUT.search(word) for word in words[1:]):
        return program
    for operator, target in zip(words, words[1:]):
        if ">" in operator and set(operator) <= set("&>|") and TOOL_OUTPUT.search(target):
            return operator
    return None


def change_refusal(program: str) -> str:
    return (
        f"Refused: this {program} on saved tool output asks for permission and waits on the user.\n"
        + "Leave saved output alone: nothing needs deleting. "
        + "To keep a copy, use `cat <file> > /tmp/claude/<name>`."
    )


def refusal(program: str) -> str:
    return (
        f"Refused: this {program} read of saved tool output asks for permission and waits on the user.\n"
        + "Read the same lines with no prompt: the Read tool with offset and limit, `grep -n PATTERN FILE` "
        + "(with -A, -B or -C), `tail -n +N FILE`, `head -N FILE | tail -M`, or `sed -n 'N,Mp' FILE`."
    )


def main() -> None:
    try:
        command = bash_command(sys.stdin.read())
    except UnicodeDecodeError:
        return
    if command is None or not TOOL_OUTPUT.search(command):
        return
    for words in segments(command) or []:
        changer = changes_output(words)
        reader = asks_permission(words)
        reason = (
            change_refusal(changer) if changer is not None
            else refusal(reader) if reader is not None
            else None
        )
        if reason is not None:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                },
            }))
            return


if __name__ == "__main__":
    main()
