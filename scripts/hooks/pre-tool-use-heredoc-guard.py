#!/usr/bin/env python3
"""Refuse a Bash command whose unquoted heredoc body holds a command substitution.

With an unquoted delimiter (<<EOF) the shell expands the body, so `text` and
$(text) there run as commands, with no sign on the command line. A quoted
delimiter (<<'EOF') keeps the body literal. Plain $VAR expansion, escaped \\`
and \\$(, and $(( )) arithmetic are allowed. A command this scanner cannot
follow is allowed.
"""

from __future__ import annotations

import json
import sys
from typing import NamedTuple, TypedDict, cast

# Characters that end an unquoted word, and so end a heredoc delimiter.
WORD_END = frozenset(" \t\n;&|<>()")
# A `#` at the start of a word, after one of these, opens a comment.
COMMENT_START = frozenset(" \t\n;&|()")


class BashInput(TypedDict, total=False):
    command: str


class HookInput(TypedDict, total=False):
    """The PreToolUse fields this hook reads."""

    tool_name: str
    tool_input: BashInput


class Heredoc(NamedTuple):
    operator: str  # "<<" or "<<-"
    delimiter: str  # after quote removal
    quoted: bool


class HeredocScanner:
    """Walk a command, tracking quotes, substitutions and arithmetic, and collect
    each heredoc whose unquoted body would run a command."""

    def __init__(self, text: str) -> None:
        self.text: str = text
        self.pos: int = 0
        # Closers of the open contexts, innermost last: '"' double quote,
        # ")" command substitution or subshell, "`" backtick substitution,
        # "))" arithmetic, "(" a parenthesis inside arithmetic.
        self.stack: list[str] = []
        self.pending: list[Heredoc] = []
        self.findings: list[Heredoc] = []

    def scan(self) -> list[Heredoc]:
        while self.pos < len(self.text):
            context = self.stack[-1] if self.stack else ""
            if context == '"':
                self.step_double_quoted()
            elif context in ("))", "("):
                self.step_arithmetic(context)
            elif not self.step_code(context):
                break
        return self.findings

    def enter(self, closer: str, width: int) -> None:
        self.stack.append(closer)
        self.pos += width

    def leave(self, width: int) -> None:
        _ = self.stack.pop()
        self.pos += width

    def step_nested(self, context: str) -> bool:
        """Step over an escape or single-quoted text, or enter a quote or
        substitution, at pos. False when pos holds none of them."""
        text, pos = self.text, self.pos
        char = text[pos]
        if char == "\\":
            self.pos += 2
        elif char == "'":
            close = text.find("'", pos + 1)
            self.pos = len(text) if close < 0 else close + 1
        elif char == '"':
            self.enter('"', 1)
        elif char == "`" and context == "`":
            self.leave(1)
        elif char == "`":
            self.enter("`", 1)
        elif text.startswith("$((", pos):
            self.enter("))", 3)
        elif text.startswith("$(", pos):
            self.enter(")", 2)
        else:
            return False
        return True

    def step_double_quoted(self) -> None:
        text, pos = self.text, self.pos
        char = text[pos]
        if char == "\\":
            self.pos += 2
        elif char == '"':
            self.leave(1)
        elif char == "`" or text.startswith("$(", pos):
            _ = self.step_nested('"')
        else:
            self.pos += 1

    def step_arithmetic(self, context: str) -> None:
        text, pos = self.text, self.pos
        if self.step_nested(context):
            return
        char = text[pos]
        if char == "(":
            self.enter("(", 1)
        elif char == ")":
            self.leave(2 if context == "))" and text.startswith("))", pos) else 1)
        else:
            self.pos += 1

    def step_code(self, context: str) -> bool:
        """One step through command text. False when the text cannot be followed."""
        text, pos = self.text, self.pos
        if self.step_nested(context):
            return True
        char = text[pos]
        if char == "\n":
            self.pos += 1
            self.read_bodies()
        elif char == "#" and (pos == 0 or text[pos - 1] in COMMENT_START):
            newline = text.find("\n", pos)
            self.pos = len(text) if newline < 0 else newline
        elif text.startswith("<<<", pos):
            self.pos += 3
        elif text.startswith("<<", pos):
            return self.read_operator()
        elif text.startswith("((", pos):
            self.enter("))", 2)
        elif char == "(":
            self.enter(")", 1)
        elif char == ")" and context == ")":
            self.leave(1)
        else:
            self.pos += 1
        return True

    def read_operator(self) -> bool:
        """Read `<<WORD` or `<<-WORD` at pos. False when no word follows."""
        self.pos += 2
        operator = "<<"
        if self.text.startswith("-", self.pos):
            operator = "<<-"
            self.pos += 1
        while self.pos < len(self.text) and self.text[self.pos] in " \t":
            self.pos += 1
        word = self.read_word()
        if word is None:
            return False
        delimiter, quoted = word
        self.pending.append(Heredoc(operator, delimiter, quoted))
        return True

    def read_word(self) -> tuple[str, bool] | None:
        """The delimiter word at pos after quote removal, and whether any of it was quoted."""
        text = self.text
        parts: list[str] = []
        quoted = False
        while self.pos < len(text) and text[self.pos] not in WORD_END:
            char = text[self.pos]
            if char == "\\" and self.pos + 1 < len(text):
                quoted = True
                parts.append(text[self.pos + 1])
                self.pos += 2
            elif char in "'\"":
                close = text.find(char, self.pos + 1)
                if close < 0:
                    return None
                quoted = True
                parts.append(text[self.pos + 1 : close])
                self.pos = close + 1
            else:
                parts.append(char)
                self.pos += 1
        if not parts and not quoted:
            return None
        return "".join(parts), quoted

    def read_bodies(self) -> None:
        """Consume the body of each heredoc opened on the line that just ended."""
        for heredoc in self.pending:
            body = self.read_body(heredoc)
            if not heredoc.quoted and runs_command(body):
                self.findings.append(heredoc)
        self.pending.clear()

    def read_body(self, heredoc: Heredoc) -> str:
        """Lines from pos up to the delimiter line, or to the end of the text."""
        text = self.text
        lines: list[str] = []
        while self.pos < len(text):
            end = text.find("\n", self.pos)
            end = len(text) if end < 0 else end
            line = text[self.pos : end]
            self.pos = end + 1
            terminator = line.lstrip("\t") if heredoc.operator == "<<-" else line
            if terminator == heredoc.delimiter:
                break
            lines.append(line)
        return "\n".join(lines)


def runs_command(body: str) -> bool:
    """True when an expanded heredoc body holds `...` or $(...) that the shell would run."""
    pos = 0
    while pos < len(body):
        if body[pos] == "\\":
            pos += 2
        elif body[pos] == "`" or (body.startswith("$(", pos) and not body.startswith("$((", pos)):
            return True
        else:
            pos += 1
    return False


def refusal(findings: list[Heredoc]) -> str:
    shown = ", ".join(f"{heredoc.operator}{heredoc.delimiter}" for heredoc in findings)
    first = findings[0]
    return (
        f"Refused: unquoted heredoc delimiter {shown}; the shell would run the backticked "
        + "or $( ) text in the body as a command.\n"
        + f"Quote the delimiter ({first.operator}'{first.delimiter}') to keep the body literal, "
        + "or move the command out of the heredoc if it really is meant to run."
    )


def bash_command(raw: str) -> str | None:
    """The command of a Bash tool call, or None for any other input. JSON of
    another shape raises AttributeError on a `.get`, which also means None."""
    try:
        hook_input = cast(HookInput, json.loads(raw))
        if hook_input.get("tool_name") != "Bash":
            return None
        command = hook_input.get("tool_input", {}).get("command")
    except (ValueError, AttributeError):
        return None
    return command if isinstance(command, str) else None


def main() -> None:
    try:
        command = bash_command(sys.stdin.read())
    except UnicodeDecodeError:
        return
    if command is None or "<<" not in command:
        return
    if "`" not in command and "$(" not in command:
        return
    findings = HeredocScanner(command).scan()
    if not findings:
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": refusal(findings),
        },
    }))


if __name__ == "__main__":
    main()
