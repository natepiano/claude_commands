#!/usr/bin/env python3
"""Refuse a Bash command or file write that makes a new loose folder in /tmp, $TMPDIR, /tmp/claude,
~/rust or ~/rust/scratch.

Agents made repo copies, repros and test trees in those folders, and nothing removed them
(user, 2026-10-26: "i need a more robust solution"). A scratch folder now belongs to a task and
goes when the task ends: `scripts/lib/scratch.py`, or the session's own scratchpad.

The commands judged are mkdir, mktemp -d, git clone, git worktree add, cp -r/-R/-a, rsync,
cargo new/init, and tar extraction (-C or the working folder); a Write or NotebookEdit judges its
file's path. A command is refused when it makes a folder directly in one of the roots that does not
exist yet. Folders in the roots that stay allowed: the session scratchpads (/tmp/claude-<uid>),
/tmp/claude/unit, /tmp/claude/delegate, the session temp roots (/tmp/claude/session) and the two
scratch roots (/tmp/claude/scratch, ~/rust/repro). A TMPDIR inside a session temp root or a run
folder is the session's or run's own, so it is no root, and mktemp -d there is allowed; this hook
also restores the session temp folder from the hook input's session id. ~/worktrees is no root, and a
make_a_worktree command is never judged. A path this scanner cannot follow (a variable other
than HOME or TMPDIR, a command substitution, a command inside `bash -c` or ssh) is allowed: the
hourly tmp sweep is the backstop.

Claude Code and Codex both send a shell command as tool `Bash`; Codex accepts only a refusal
from a PreToolUse hook, so an allow is an exit 0 with no output.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shlex
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol, TypedDict, cast


class SessionTempPaths(Protocol):
    """The session-start hook's path and session id rules."""

    SESSION_ID: re.Pattern[str]

    def temp_root(self, session_id: str) -> Path: ...


SESSION_TEMP = cast(SessionTempPaths, cast(object, importlib.import_module("session-start-temp-root")))

PUNCTUATION = ";&|(){}\n"
HEREDOC = re.compile(r"(?<!<)<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
ENVIRONMENT_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
SESSION_SCRATCHPAD = re.compile(r"claude-[0-9]+")
OWN_TEMP_PARENTS = ("/tmp/claude/session", "/tmp/claude/unit", "/tmp/claude/delegate")
WRITE_TOOLS = {"Write": "file_path", "NotebookEdit": "notebook_path"}
KNOWN_VARIABLES = re.compile(r"\$(?:\{(HOME|TMPDIR)\}|(HOME|TMPDIR)\b)")
# Options of the judged commands that take a value as the next word.
GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
CLONE_VALUE_OPTIONS = frozenset({"-b", "--branch", "-o", "--origin", "--depth", "--reference", "-u",
                                 "--upload-pack", "-c", "--config", "--separate-git-dir", "--filter",
                                 "-j", "--jobs", "--template", "--shallow-since", "--shallow-exclude",
                                 "--bundle-uri", "--server-option", "--reference-if-able"})
WORKTREE_VALUE_OPTIONS = frozenset({"-b", "-B", "--reason"})
CARGO_VALUE_OPTIONS = frozenset({"--name", "--vcs", "--edition", "--registry", "--config", "-Z"})


class BashInput(TypedDict, total=False):
    command: str | list[str]
    file_path: str
    notebook_path: str


class HookInput(TypedDict, total=False):
    """The PreToolUse fields this hook reads."""

    tool_name: str
    tool_input: BashInput
    cwd: str
    session_id: str


def real(path: str) -> Path:
    return Path(os.path.realpath(os.path.expanduser(path)))


def own_temp(temp: str) -> bool:
    """Whether TMPDIR is this session's or run's own: in a session temp root, a run folder or a
    scratchpad (a sandbox can point it there)."""
    path = real(temp)
    return (any(real(parent) in path.parents for parent in OWN_TEMP_PARENTS)
            or any(SESSION_SCRATCHPAD.fullmatch(part) for part in path.parts))


def roots() -> list[Path]:
    """The folders in which a new loose folder is refused, as real paths, nested ones included."""
    found = [real("/tmp"), real("/tmp/claude"), real("~/rust"), real("~/rust/scratch")]
    temp = os.environ.get("TMPDIR")
    if temp and not own_temp(temp):
        found.append(real(temp))
    return list(dict.fromkeys(found))


def allowed(top: Path) -> bool:
    """A folder directly in a root that may be made: a scratchpad, the run folders, a scratch root."""
    if top.parent == real("/tmp") and (top.name == "claude" or SESSION_SCRATCHPAD.fullmatch(top.name)):
        return True
    if top.parent == real("/tmp/claude") and top.name in ("unit", "delegate", "scratch", "session"):
        return True
    return top == real("~/rust/repro")


def loose_folder(path: Path) -> Path | None:
    """The new folder that making `path` puts directly in a root, or None when there is none."""
    for root in roots():
        if path == root or root not in path.parents:
            continue
        top = root / path.relative_to(root).parts[0]
        if not allowed(top) and not os.path.lexists(top):
            return top
    return None


def expanded(word: str, cwd: Path) -> Path | None:
    """`word` as a real path from `cwd`, or None when it holds an expansion this hook cannot follow."""
    text = KNOWN_VARIABLES.sub(lambda match: os.environ.get(match[1] or match[2], ""), word)
    if "$" in text or "`" in text or any(char in text for char in "*?["):
        return None
    if text.startswith("~"):
        text = os.path.expanduser(text)
    return Path(os.path.realpath(cwd / text))


def without_heredoc_bodies(text: str) -> str:
    """`text` with each heredoc body dropped: a body is data, never a command."""
    kept: list[str] = []
    waiting: list[tuple[str, bool]] = []
    for line in text.split("\n"):
        if waiting:
            delimiter, tabs = waiting[0]
            if (line.lstrip("\t") if tabs else line) == delimiter:
                _ = waiting.pop(0)
            continue
        kept.append(line)
        waiting = [(match[2], match[0].startswith("<<-")) for match in HEREDOC.finditer(line)]
    return "\n".join(kept)


def commands(text: str) -> Iterator[list[str]]:
    """Each simple command of `text` as words; nothing when the text cannot be split."""
    lexer = shlex.shlex(without_heredoc_bodies(text), posix=True, punctuation_chars=PUNCTUATION)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""  # a comment's words start with `#`, which is no command
    words: list[str] = []
    try:
        for token in lexer:
            if set(token) <= set(PUNCTUATION):
                if words:
                    yield words
                words = []
            else:
                words.append(token)
    except ValueError:
        return
    if words:
        yield words


def operands(words: list[str], value_options: frozenset[str]) -> list[str]:
    """The words that are not options, skipping each option's value."""
    found: list[str] = []
    skip = False
    ended = False
    for word in words:
        if skip:
            skip = False
        elif ended or not word.startswith("-") or word == "-":
            found.append(word)
        elif word == "--":
            ended = True
        elif word in value_options:
            skip = True
    return found


def copy_targets(words: list[str], cwd: Path) -> list[Path | None]:
    """The folders a recursive cp or an rsync makes: the target, or each source's name inside it."""
    target_folder = next((words[index + 1] for index, word in enumerate(words[:-1])
                          if word in ("-t", "--target-directory")), None)
    names = operands(words, frozenset({"-t", "--target-directory", "-e", "--rsh", "--exclude",
                                       "--include", "--filter", "-f"}))
    if target_folder is None:
        if len(names) < 2:
            return []
        sources, target_word = names[:-1], names[-1]
    else:
        sources, target_word = names, target_folder
    target = expanded(target_word, cwd)
    if target is None:
        return [None]
    if not target.is_dir():
        return [target]
    return [target / Path(source.rstrip("/")).name if not source.endswith("/") else target
            for source in sources]


def mktemp_target(words: list[str]) -> list[Path | None]:
    if not any(word == "-d" or word == "--directory" or (re.fullmatch(r"-[a-zA-Z]*d[a-zA-Z]*", word))
               for word in words):
        return []
    folder: str | None = None
    for index, word in enumerate(words):
        if word in ("-p", "--tmpdir") and index + 1 < len(words):
            folder = words[index + 1]
        elif word.startswith("--tmpdir="):
            folder = word.split("=", 1)[1]
    names = operands(words, frozenset({"-p", "--tmpdir", "--suffix"}))
    template = names[0] if names else "tmp.XXXXXXXXXX"
    if "/" in template and folder is None:
        return [expanded(template, Path("/"))]
    base = folder or os.environ.get("TMPDIR") or "/tmp"
    resolved = expanded(base, Path("/"))
    return [None if resolved is None else resolved / template]


def tar_targets(words: list[str], cwd: Path) -> list[Path | None]:
    """A tar extraction into a root: every entry of the archive lands loose in it."""
    flags = words[0] if words and not words[0].startswith("-") else ""
    extracting = "x" in flags or any(word in ("-x", "--extract", "--get") or re.fullmatch(r"-[a-zA-Z]*x[a-zA-Z]*", word)
                                     for word in words)
    if not extracting:
        return []
    folder = next((words[index + 1] for index, word in enumerate(words[:-1]) if word in ("-C", "--directory")),
                  next((word.split("=", 1)[1] for word in words if word.startswith("--directory=")), None))
    target = expanded(folder, cwd) if folder is not None else cwd
    if target is None:
        return [None]
    return [target / "(archive entries)"] if target in roots() else []


def git_targets(words: list[str], cwd: Path) -> list[Path | None]:
    index = 0
    while index < len(words) and words[index].startswith("-"):
        if words[index] == "-C" and index + 1 < len(words):
            moved = expanded(words[index + 1], cwd)
            if moved is None:
                return [None]
            cwd = moved
        index += 2 if words[index] in GIT_VALUE_OPTIONS else 1
    rest = words[index:]
    if rest[:1] == ["clone"]:
        names = operands(rest[1:], CLONE_VALUE_OPTIONS)
        if not names:
            return []
        folder = names[1] if len(names) > 1 else re.sub(r"(\.git)?/*$", "", names[0]).rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        return [expanded(folder, cwd)]
    if rest[:2] == ["worktree", "add"]:
        names = operands(rest[2:], WORKTREE_VALUE_OPTIONS)
        return [expanded(names[0], cwd)] if names else []
    return []


def targets(words: list[str], cwd: Path) -> list[Path | None]:
    """The folders the command makes; None for one this hook cannot follow."""
    verb, arguments = Path(words[0]).name, words[1:]
    if verb == "mkdir":
        return [expanded(word, cwd) for word in operands(arguments, frozenset({"-m", "--mode", "-Z", "--context"}))]
    if verb == "mktemp":
        return mktemp_target(arguments)
    if verb == "git":
        return git_targets(arguments, cwd)
    if verb == "cp" and any(word in ("--recursive", "--archive") or re.fullmatch(r"-[a-zA-Z]*[rRa][a-zA-Z]*", word)
                            for word in arguments):
        return copy_targets(arguments, cwd)
    if verb == "rsync":
        return copy_targets(arguments, cwd) if not any(":" in word.split("/", 1)[0] for word in arguments[-1:]) else []
    if verb == "cargo" and arguments[:1] in (["new"], ["init"]):
        names = operands(arguments[1:], CARGO_VALUE_OPTIONS)
        return [expanded(names[0], cwd)] if names else []
    if verb in ("tar", "gtar", "bsdtar"):
        return tar_targets(arguments, cwd)
    return []


def loose_folders(text: str, cwd: Path) -> list[Path]:
    """Every new loose folder the command makes, following `cd` between its commands."""
    found: list[Path] = []
    for words in commands(text):
        while words and ENVIRONMENT_WORD.fullmatch(words[0]):
            words = words[1:]
        if not words:
            continue
        verb = Path(words[0]).name
        if verb.startswith("make_a_worktree"):
            return []
        if verb == "cd":
            moved = expanded(words[1], cwd) if len(words) > 1 else real("~")
            if moved is None:
                return found
            cwd = moved
            continue
        for path in targets(words, cwd):
            top = loose_folder(path) if path is not None else None
            if top is not None and top not in found:
                found.append(top)
    return found


def refusal(folders: list[Path]) -> str:
    shown = ", ".join(str(folder) for folder in folders)
    return (
        f"Refused: this makes a new loose folder ({shown}). Nothing removes a loose folder in "
        + "/tmp, /tmp/claude, ~/rust or ~/rust/scratch, so they piled up.\n"
        + "Make scratch that belongs to a task, and it goes when the task ends:\n"
        + "  ~/.claude/scripts/lib/py ~/.claude/scripts/lib/scratch.py new [--task <run:<id> | "
        + "phase:<unit>/<phase> | gate:<id> | row:<showrunner>-C<n>> | --until <YYYY-MM-DD [HH:MM]>] "
        + "[--name <word>] [--visible]\n"
        + "With no task it lasts 24 h. It prints the folder to use. For temp files, use $TMPDIR (mktemp -d "
        + "with no path); a new project goes through its own command (add_unit, ~/worktrees)."
    )


def written_folders(raw: str) -> list[Path]:
    """The new loose folder a Write or NotebookEdit makes for its file, if any."""
    try:
        hook_input = cast(HookInput, json.loads(raw))
        key = WRITE_TOOLS.get(hook_input.get("tool_name", ""))
        file_path = hook_input.get("tool_input", {}).get(key) if key else None
    except (ValueError, AttributeError):
        return []
    if not isinstance(file_path, str) or not file_path:
        return []
    path = real(file_path)
    top = loose_folder(path)
    # A file directly in a root makes no folder.
    return [top] if top is not None and top != path else []


def bash_input(raw: str) -> tuple[str, Path] | None:
    """The command and working folder of a Bash tool call, or None for any other input."""
    try:
        hook_input = cast(HookInput, json.loads(raw))
        if hook_input.get("tool_name") != "Bash":
            return None
        command = hook_input.get("tool_input", {}).get("command")
        cwd = hook_input.get("cwd") or os.getcwd()
    except (ValueError, AttributeError):
        return None
    if isinstance(command, list):
        words = [word for word in cast(list[object], command) if isinstance(word, str)]
        # Codex may send argv: `bash -lc "<script>"` carries the script last.
        command = words[-1] if len(words) >= 3 and words[-2] in ("-c", "-lc") else shlex.join(words)
    if not isinstance(command, str):
        return None
    return command, real(cwd)


def remake_session_temp(raw: str) -> None:
    """Restore only the input session's temp folder; an error leaves the call allowed."""
    try:
        hook_input = cast(HookInput, json.loads(raw))
        session_id = hook_input.get("session_id")
    except (ValueError, AttributeError):
        return
    if not isinstance(session_id, str) or not SESSION_TEMP.SESSION_ID.fullmatch(session_id):
        return
    path = SESSION_TEMP.temp_root(session_id)
    try:
        _ = os.stat(path)
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            pass
    except OSError:
        pass


def main() -> None:
    try:
        raw = sys.stdin.read()
    except UnicodeDecodeError:
        return
    remake_session_temp(raw)
    found = bash_input(raw)
    folders = loose_folders(*found) if found is not None else written_folders(raw)
    if not folders:
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": refusal(folders),
        },
    }))


if __name__ == "__main__":
    main()
