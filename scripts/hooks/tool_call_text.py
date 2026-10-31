"""The text of a tool call that the banned-word PostToolUse hook scans.

The hook scans only text a call writes, never the tool's output. Text a call
removes or searches for is not scanned: Edit and MultiEdit contribute
`new_string`, never `old_string`; a NotebookEdit that deletes a cell
contributes nothing; and in a Bash command, `mask_removed_text` blanks the
pattern side of each sed `s<d>old<d>new<d>` expression in a sed invocation,
the first argument of `.replace(...)` or `re.sub(...)` when it is a string
literal, and the pattern of a grep, rg or git grep call: each argument after
`-e`/`--regexp`, else the first argument not starting with `-`.
Removal- and search-shaped text is blanked by pattern, not by parsing, so
a lookalike (an echo or commit message quoting a sed expression, a `.replace`
call or a grep) is blanked too and a word in it goes unreported, in the
command. Text the patterns do not match is scanned whole, except simple
read-only commands whose arguments are inspection paths.
Blanking keeps newlines, so the reported line numbers still index the
original text.
"""

import re
from typing import TypedDict

from banned_words_lib import is_read_only_command


class EditEntry(TypedDict, total=False):
    new_string: str


class ToolInput(TypedDict, total=False):
    content: str
    cmd: str
    new_string: str
    edits: list[EditEntry]
    new_source: str
    edit_mode: str
    file_path: str
    notebook_path: str
    command: str
    description: str


class HookPayload(TypedDict, total=False):
    tool_name: str
    tool_input: ToolInput


# A sed or grep call at the start of a command, and its arguments up to the
# next unquoted `;`, `&`, `|`, redirection or newline.
_SHELL_WORD = r"""(?:'[^']*'|"(?:[^"\\]|\\.)*"|\\.|[^\s;&|<>'"\\])+"""
_CALL_START = r"(?:^|[;&|(]|\bdo|\bthen|\bxargs|-exec)[ \t]*"
_ARGS = rf"((?:[ \t]+{_SHELL_WORD})+)"
_SED_CALL = re.compile(rf"{_CALL_START}g?sed\b{_ARGS}", re.M)
_GREP_CALL = re.compile(rf"{_CALL_START}(?:git[ \t]+grep|grep|rg)\b{_ARGS}", re.M)
_WORD = re.compile(_SHELL_WORD)
# An `s` command with an optional line address; group 2 is the removed pattern.
_SED_S = re.compile(r"(?<!\w)(?:\d+|\$)?s([/|#,@:])((?:\\.|(?!\1)[^\\\n])*)\1(?:\\.|(?!\1)[^\\\n])*\1")
_PY_STRING = r"""[rRbBuUfF]{0,2}(?:\"\"\"(?:\\.|[^\\])*?\"\"\"|'''(?:\\.|[^\\])*?'''|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')"""
_PY_REMOVAL = re.compile(rf"(?:\.replace|\bre\.subn?)\(\s*({_PY_STRING})\s*,", re.S)


def scan_text(tool_name: str, tool_input: ToolInput) -> str:
    """Return written text, excluding removed text and search patterns."""
    parts: list[str] = []
    if tool_name == "Write":
        parts.append(tool_input.get("content", "") or "")
    elif tool_name == "Edit":
        parts.append(tool_input.get("new_string", "") or "")
    elif tool_name == "MultiEdit":
        edits: list[EditEntry] = tool_input.get("edits", []) or []
        parts.extend(e.get("new_string", "") or "" for e in edits)
    elif tool_name == "NotebookEdit":
        if tool_input.get("edit_mode") != "delete":
            parts.append(tool_input.get("new_source", "") or "")
    else:
        command = mask_removed_text(tool_input.get("command", "") or "")
        cmd = mask_removed_text(tool_input.get("cmd", "") or "")
        parts += [command, cmd, tool_input.get("description", "") or ""]
        parts.append(tool_input.get("content", "") or "")
        parts.append(tool_input.get("new_string", "") or "")
    return "\n".join(p for p in parts if p)


def mask_removed_text(command: str) -> str:
    """`command` with the text its replacements remove and its searches look for
    turned into spaces; simple inspection commands contribute no text."""
    if is_read_only_command(command):
        return re.sub(r"[^\n]", " ", command)
    spans: list[tuple[int, int]] = []
    for call in _SED_CALL.finditer(command):
        start = call.start(1)
        spans.extend((start + s.start(2), start + s.end(2)) for s in _SED_S.finditer(call.group(1)))
    for call in _GREP_CALL.finditer(command):
        words = list(_WORD.finditer(call.group(1)))
        after_e = [w for prev, w in zip(words, words[1:]) if prev.group(0) in ("-e", "--regexp")]
        first = [w for w in words if not w.group(0).startswith("-")][:1]
        spans.extend((call.start(1) + w.start(), call.start(1) + w.end()) for w in after_e or first)
    spans.extend((m.start(1), m.end(1)) for m in _PY_REMOVAL.finditer(command))
    if not spans:
        return command
    chars = list(command)
    for start, end in spans:
        for i in range(start, end):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)
