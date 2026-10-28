#!/usr/bin/env python3
"""Catch the commit forms an agent writes in ordinary use. This is not a shell
interpreter and does not stop deliberate hiding in a script file, an alias or
a function defined in an earlier call. Where it cannot read a command that
holds checkpoint text, it refuses.

Require a durable rustdoc pass for the exact tree of every Rust checkpoint.
In every repo, a checkpoint commit must be the first command of its own Bash call.

verify.sh doc judges a stable, locked checkout of the staged index and records its
write-tree identity; unstaged and untracked files do not affect that verdict.
Ordinary commits use the index, -a adds tracked working changes, --only/-o and
positional paths use HEAD plus those working paths, and --include/-i uses the
index plus those paths. Every form requires a record for exactly its committed
tree. Interactive (-p/--patch/--interactive) selection and unreadable pathspecs
are refused because their eventual tree cannot be known before execution.

Shell words respect quotes and escapes; heredoc bodies are never commands.
Only a command word git (or a path ending in /git), after assignment prefixes
and env/command/exec/sudo/nice/time wrappers, can introduce commit or merge.
Segments start at the call boundary, ;, &&, ||, |, parentheses, command
substitution or backquotes, and shell condition, loop, group, case and function
bodies. Git global options are skipped before its subcommand. Option parsing
ends at --; all later words are paths, including --fixup and --squash. Short
clusters stop at their first value-taking letter; its attached remainder (or
the next word) is the value. -S and commit -u take only optional attached values.
Unknown short letters make the invocation unreadable under the same scope rules.
Checkpoint text belongs to that invocation's message, never another program's
quoted argument. Conditions, ordinary for loops, case arms, groups, !, time and
function bodies are modeled and never use quote-removed fallback text.
Opaque commands (eval, expanded command words, sh/bash/zsh -c, unmodeled env
options, coproc, select, [[ ]], (( )) and arithmetic for) refuse in every repo
when their own quote-removed words hold checkpoint( and git with commit/merge;
without checkpoint( they refuse only in a Cargo cwd with git commit/merge words.
Unrelated opaque commands pass. env -C/--chdir and git -C resolve the target
directory. Repository/index overrides, earlier commands and unreadable trees
fail closed for checkpoints. Unreadable messages count only in Cargo repos.
A merge without a message option uses git's ordinary default subject; --log
does not supply a message. Literal merge messages use the checkpoint rules in
every repo; -e/--edit and unreadable -F messages count only in Cargo repos.
A Cargo checkpoint merge must finish before a separately judged checkpoint
commit, because its resulting tree is not yet available.
Subject matching comes from the change-review hook; checking errors fail closed.
"""
import sys

# Every Bash call reaches this hook. Do not load even json until it might commit.
if __name__ == "__main__":
    RAW_PAYLOAD = sys.stdin.read()
    if not any(word in RAW_PAYLOAD for word in ("commit", "merge", "eval", "$", "`")):
        raise SystemExit(0)

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, cast


class CheckpointParser(Protocol):
    SUBJECT: re.Pattern[str]
    ALL: re.Pattern[str]


def checkpoint_parser() -> CheckpointParser:
    path = Path(__file__).with_name("pre-tool-use-change-review-gate.py")
    specification = importlib.util.spec_from_file_location("checkpoint_change_review_parser", path)
    if specification is None or specification.loader is None:
        raise ImportError(f"cannot load checkpoint parser: {path}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return cast(CheckpointParser, cast(object, module))


FIX = "Run ~/.claude/scripts/direct/verify.sh doc as the last command before the commit."


def git(directory: Path, *arguments: str, environment: Mapping[str, str] = os.environ) -> str:
    result = subprocess.run(["git", "-C", str(directory), *arguments], env=environment,
                            capture_output=True, text=True, check=False, timeout=10)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(arguments)} failed: {result.stderr.strip() or result.returncode}")
    return result.stdout.strip()


def commit_tree(directory: Path, every: bool) -> str:
    """Write a tree using a private index; -a adds only tracked working changes."""
    with tempfile.TemporaryDirectory(prefix="checkpoint-doc-", dir="/tmp") as temporary:
        index = Path(git(directory, "rev-parse", "--git-path", "index"))
        if not index.is_absolute():
            index = directory / index
        private_index = Path(temporary) / "index"
        if index.exists():
            _ = shutil.copyfile(index, private_index)
        environment = {**os.environ, "GIT_INDEX_FILE": str(private_index)}
        if every:
            _ = git(directory, "add", "-u", "--", ".", environment=environment)
        return git(directory, "write-tree", environment=environment)


def path_commit_tree(directory: Path, paths: tuple[str, ...], include_staged: bool) -> str:
    """Select working paths over HEAD (--only) or over the index (--include)."""
    with tempfile.TemporaryDirectory(prefix="checkpoint-paths-", dir="/tmp") as temporary:
        private_index = Path(temporary) / "index"
        environment = {**os.environ, "GIT_INDEX_FILE": str(private_index)}
        if include_staged:
            index = Path(git(directory, "rev-parse", "--git-path", "index"))
            if not index.is_absolute():
                index = directory / index
            if index.exists():
                _ = shutil.copyfile(index, private_index)
        else:
            _ = git(directory, "read-tree", "HEAD", environment=environment)
        _ = git(directory, "add", "--", *paths, environment=environment)
        return git(directory, "write-tree", environment=environment)


def check_record(directory: Path, tree: str) -> None:
    record_path = Path.home() / ".local/state/verify/doc_pass" / f"{tree}.json"
    try:
        loaded = cast(object, json.loads(record_path.read_text(encoding="utf-8")))
    except FileNotFoundError as error:
        raise ValueError(f"no doc pass record for committed tree {tree}") from error
    if not isinstance(loaded, dict):
        raise ValueError(f"invalid doc pass record: {record_path}")
    record = cast(dict[str, object], loaded)
    if record.get("repo_top") != str(directory) or record.get("tree_id") != tree:
        raise ValueError(f"doc pass record does not match repository and committed tree: {record_path}")
    if not isinstance(record.get("time"), str) or not isinstance(record.get("call_id"), str):
        raise ValueError(f"doc pass record lacks time or build-log call id: {record_path}")


@dataclass(frozen=True)
class ReadableCommitMessage:
    text: str


@dataclass(frozen=True)
class UnreadableCommitMessage:
    cause: str


@dataclass(frozen=True)
class KnownCommitDirectory:
    path: Path


@dataclass(frozen=True)
class UnknownCommitDirectory:
    cause: str


SUBJECT_FIX = ("Write the subject as literal -m text, a -F file or the heredoc form, "
               "or, in a repo with a Cargo.toml at its top, run ~/.claude/scripts/direct/verify.sh doc first.")
OWN_CALL_FIX = ("Run the checkpoint commit as the first command of its own Bash call; "
                "put cd, git add and other commands in earlier calls.")
CARGO_DOC_FIX = ("In a repo with a Cargo.toml at its top, also run "
                 "~/.claude/scripts/direct/verify.sh doc in an earlier call.")


@dataclass(frozen=True)
class ShellCommand:
    words: tuple[str, ...]
    earlier: bool


@dataclass(frozen=True)
class GitInvocation:
    options: str
    arguments: str
    earlier: bool
    environment_override: bool
    subcommand: str


@dataclass(frozen=True)
class UnreadableShellCommand:
    cause: str
    words: tuple[str, ...]


def shell_text(raw: str) -> str:
    return " ".join(shlex.split(raw.replace("\\\n", "")))


def shell_word(command: str, start: int, terminator: str) -> tuple[str, int, list[ShellCommand]]:
    """Keep raw quoting for expansion checks, and separately inspect executable substitutions."""
    position = start
    quote = ""
    nested: list[ShellCommand] = []
    while position < len(command):
        character = command[position]
        if not quote and (character.isspace() or character in ";&|()<>" or character == terminator):
            break
        if character == "\\" and quote != "'":
            position += 2
            continue
        if character in "'\"" and (not quote or quote == character):
            quote = "" if quote else character
            position += 1
            continue
        if quote != "'" and (command.startswith("$(", position) or character == "`"):
            substitution = command.startswith("$(", position)
            children, position = scan_shell(command, position + (2 if substitution else 1),
                                             ")" if substitution else "`")
            nested.extend(ShellCommand(child.words, True) for child in children)
            continue
        position += 1
    if quote:
        raise ValueError("git directory options could not be parsed: unterminated shell quote")
    return command[start:position], position, nested


def scan_shell(command: str, start: int, terminator: str) -> tuple[list[ShellCommand], int]:
    """Split executable segments; consume heredocs at the declaring line's newline."""
    segments: list[ShellCommand] = []
    nested: list[ShellCommand] = []
    words: list[str] = []
    heredocs: list[tuple[str, bool]] = []
    pending_heredoc = ""
    position = start

    def finish() -> None:
        if words:
            segments.append(ShellCommand(tuple(words), bool(segments)))
            words.clear()

    while position < len(command):
        character = command[position]
        if terminator and character == terminator:
            finish()
            return segments + nested, position + 1
        if character == "\\" and command.startswith("\\\n", position):
            position += 2
            continue
        if character.isspace():
            position += 1
            if character == "\n":
                finish()
                for delimiter, strip_tabs in heredocs:
                    while position < len(command):
                        end = command.find("\n", position)
                        end = len(command) if end < 0 else end
                        line = command[position:end]
                        position = min(end + 1, len(command))
                        if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                            break
                heredocs.clear()
            continue
        if character == "#":
            end = command.find("\n", position)
            position = len(command) if end < 0 else end
            continue
        if command.startswith("((", position):
            end = command.find("))", position + 2)
            end = len(command) if end < 0 else end + 2
            words.append(command[position:end])
            position = end
            continue
        if character in ";&|()":
            finish()
            position += 1
            continue
        if character in "<>":
            end = position + 1
            while end < len(command) and command[end] in "<>&-":
                end += 1
            operator = command[position:end]
            words.append(operator)
            if operator in {"<<", "<<-"}:
                pending_heredoc = operator
            position = end
            continue
        raw, position, children = shell_word(command, position, terminator)
        words.append(raw)
        nested.extend(children)
        if pending_heredoc:
            heredocs.append((shell_text(raw), pending_heredoc == "<<-"))
            pending_heredoc = ""
    finish()
    return segments + nested, position


def shell_commands(command: str) -> list[ShellCommand]:
    return scan_shell(command, 0, "")[0]


ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")
REDIRECTIONS = {"<", ">", ">>", "<<", "<<-", "<<<", "<&", ">&", "<>"}
WRAPPER_ARGUMENTS = {
    "env": {"-u", "--unset"},
    "command": set[str](),
    "exec": {"-a"},
    "sudo": {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt", "-C", "-T"},
    "nice": {"-n", "--adjustment"},
    "time": {"-f", "--format", "-o", "--output"},
}


def without_redirections(words: tuple[str, ...]) -> list[str]:
    result: list[str] = []
    position = 0
    while position < len(words):
        raw = words[position]
        if raw.isdigit() and position + 1 < len(words) and words[position + 1] in REDIRECTIONS:
            position += 1
            raw = words[position]
        if raw in REDIRECTIONS:
            position += 2
        else:
            result.append(raw)
            position += 1
    return result


def command_scopes(command: str) -> list[ShellCommand]:
    """Keep opaque compound bodies together; modeled commands stay separate."""
    scopes: list[ShellCommand] = []
    pending: list[str] = []
    closing = ""
    depth = 0
    earlier = False
    for segment in shell_commands(command):
        words = segment.words
        if not pending and words:
            position = 0
            while position < len(words) and words[position] in {"then", "do", "else", "elif", "if", "while", "until", "!", "{", "}", "time"}:
                position += 1
            body = words[position:]
            if body and (body[0] == "select" or (body[0] == "for" and any(word.startswith("((") for word in body))):
                closing = "done"
                depth = 1
            elif body and body[0] == "[[":
                closing = "]]"
            elif body and body[0] == "coproc" and "{" in body:
                closing = "}"
            else:
                scopes.append(segment)
                continue
            earlier = segment.earlier
        elif not pending:
            scopes.append(segment)
            continue
        else:
            if closing == "done" and words and words[0] in {"for", "select", "while", "until"}:
                depth += 1
        pending.extend(words)
        if closing in words:
            depth -= 1
            if depth <= 0:
                scopes.append(ShellCommand(tuple(pending), earlier))
                pending = []
    if pending:
        scopes.append(ShellCommand(tuple(pending), earlier))
    return scopes


def git_invocations(command: str) -> list[GitInvocation | UnreadableShellCommand]:
    invocations: list[GitInvocation | UnreadableShellCommand] = []
    for segment in command_scopes(command):
        words = without_redirections(segment.words)
        position = 0
        compound = False
        directory_options: list[str] = []
        try:
            if words and words[0] == "function":
                if "{" in words:
                    position = words.index("{") + 1
                    compound = True
                else:
                    invocations.append(UnreadableShellCommand("the shell function body is not readable; use a separate literal git invocation", tuple(words)))
                    continue
            while position < len(words):
                raw = words[position]
                word = shell_text(raw)
                if raw in {"then", "do", "else", "elif", "if", "while", "until", "!", "{", "}"}:
                    compound = True
                    position += 1
                    continue
                if raw in {"for", "case"} and not any(value.startswith("((") for value in words[position + 1:]):
                    # These are declarations/patterns, not command words. The
                    # scanner exposes their bodies after do or a case ')'.
                    position = len(words)
                    break
                if raw in {"select", "coproc", "[[", "function", "for"} or raw.startswith("(("):
                    invocations.append(UnreadableShellCommand("the shell construct cannot be fully judged; use a separate literal git invocation", tuple(words)))
                    position = len(words)
                    break
                if ASSIGNMENT.match(raw):
                    position += 1
                    continue
                if dynamic_argument(raw) or word == "eval":
                    invocations.append(UnreadableShellCommand("the command word is expanded or eval-generated; use a literal git invocation", tuple(words)))
                    position = len(words)
                    break
                wrapper = word.rsplit("/", 1)[-1]
                if wrapper in {"sh", "bash", "zsh"} and any(
                        option == "--command" or re.fullmatch(r"-[^-]*c[^-]*", option)
                        for option in (shell_text(value) for value in words[position + 1:])):
                    invocations.append(UnreadableShellCommand("the shell -c command is opaque; use a literal git invocation", tuple(words)))
                    position = len(words)
                    break
                if wrapper not in WRAPPER_ARGUMENTS:
                    break
                position += 1
                while position < len(words) and shell_text(words[position]).startswith("-"):
                    option = shell_text(words[position])
                    position += 1
                    if option == "--":
                        break
                    if wrapper == "command" and option in {"-v", "-V"}:
                        position = len(words)
                        break
                    if wrapper == "env" and (option in {"-C", "--chdir"} or option.startswith(("--chdir=", "-C"))):
                        if option in {"-C", "--chdir"}:
                            if position >= len(words):
                                raise ValueError("env chdir has no directory")
                            directory_options.extend(("-C", words[position]))
                            position += 1
                        else:
                            directory_options.append("-C" + words[position - 1][len("--chdir=") if option.startswith("--chdir=") else 2:])
                        continue
                    if wrapper == "env":
                        if option in {"-u", "--unset"}:
                            position += 1
                            continue
                        if option.startswith("--unset="):
                            continue
                        invocations.append(UnreadableShellCommand("the env option is not modeled; use a literal git invocation", tuple(words)))
                        position = len(words)
                        break
                    if option in WRAPPER_ARGUMENTS[wrapper]:
                        position += 1
            if position >= len(words):
                continue
            program = shell_text(words[position])
            if program != "git" and not program.endswith("/git"):
                continue
            options_start = position + 1
            position = options_start
            while position < len(words):
                word = shell_text(words[position])
                if word in {"commit", "merge"}:
                    invocations.append(GitInvocation(
                        " ".join(directory_options + words[options_start:position]), " ".join(words[position + 1:]),
                        segment.earlier or compound,
                        any(re.match(r"GIT_(?:DIR|INDEX_FILE|WORK_TREE)=", shell_text(raw)) for raw in words[:options_start]),
                        word,
                    ))
                    break
                if not word.startswith("-"):
                    if dynamic_argument(words[position]):
                        invocations.append(UnreadableShellCommand("the git subcommand is expanded; use a literal subcommand", tuple(words)))
                    break
                position += 1
                if word in {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--exec-path"}:
                    position += 1
        except ValueError as error:
            invocations.append(UnreadableShellCommand(f"git directory options could not be parsed: {error}", tuple(words)))
    return invocations


def raw_commit_words(invocation: str, offset: int) -> list[str]:
    segments = shell_commands(invocation[offset:])
    return list(segments[0].words) if segments else []


def dynamic_argument(raw: str) -> bool:
    """Shell expansions outside single quotes prevent knowing the eventual text."""
    single = False
    escaped = False
    double = False
    for character in raw:
        if escaped:
            escaped = False
        elif character == "\\" and not single:
            escaped = True
        elif character == "'" and not double:
            single = not single
        elif character == '"' and not single:
            double = not double
        elif character in "$`" and not single:
            return True
    return False


def uncertain_directory_word(raw: str) -> bool:
    """Reject expansions whose resulting directory words cannot be judged."""
    single = False
    double = False
    escaped = False
    for character in raw:
        if escaped:
            escaped = False
        elif character == "\\" and not single:
            escaped = True
        elif character == "'" and not double:
            single = not single
        elif character == '"' and not single:
            double = not double
        elif not single and (character in "$`" or (not double and character in "*?[{")):
            return True
    return False


def commit_directory(options: str, cwd: Path) -> KnownCommitDirectory | UnknownCommitDirectory:
    """Join adjacent quoted/unquoted pieces as the shell does, then apply -C."""
    try:
        words = iter(zip(shlex.split(options), raw_commit_words(options, 0), strict=True))
        if dynamic_argument(options):
            return UnknownCommitDirectory("the git -C directory or global options contain a shell expansion; use a literal directory")
        directory = cwd.resolve()
        for word, raw in words:
            if word in {"--git-dir", "--work-tree"} or word.startswith(("--git-dir=", "--work-tree=")):
                return UnknownCommitDirectory("--git-dir and --work-tree overrides prevent judging the commit repository")
            if word in {"-c", "--config-env", "--namespace"}:
                if not next(words)[0]:
                    return UnknownCommitDirectory(f"{word} has an empty argument")
                continue
            if word == "-C":
                value, raw = next(words)
            elif word.startswith("-C"):
                value = word[2:]
            else:
                continue
            if uncertain_directory_word(raw):
                return UnknownCommitDirectory("the git -C directory contains a shell expansion; use a literal directory")
            if value.startswith("~"):
                account = value.split("/", 1)[0]
                if raw != account and not raw.startswith(account + "/"):
                    return UnknownCommitDirectory("a quoted or attached tilde directory cannot be expanded safely; use a literal directory")
            directory = (directory / os.path.expanduser(value)).resolve()
            if not directory.is_dir():
                return UnknownCommitDirectory("the -C directory does not exist or is not a directory")
        return KnownCommitDirectory(directory)
    except (ValueError, StopIteration) as error:
        return UnknownCommitDirectory(f"git directory options could not be parsed: {type(error).__name__}: {error}")


class ShortOptionArgument(Enum):
    FLAG = "flag"
    REQUIRED = "required value"
    ATTACHED_ONLY = "optional attached value"


# Confirmed against `git commit -h` and `git merge -h`. A value consumes the
# remainder of its cluster; optional values never consume the following word.
GIT_SHORT_OPTIONS: dict[str, dict[str, ShortOptionArgument]] = {
    "commit": {
        **dict.fromkeys("aenqvsoipz", ShortOptionArgument.FLAG),
        **dict.fromkeys("mFCctU", ShortOptionArgument.REQUIRED),
        **dict.fromkeys("Su", ShortOptionArgument.ATTACHED_ONLY),
    },
    "merge": {
        **dict.fromkeys("enqv", ShortOptionArgument.FLAG),
        **dict.fromkeys("mFsX", ShortOptionArgument.REQUIRED),
        "S": ShortOptionArgument.ATTACHED_ONLY,
    },
}
GIT_LONG_VALUE_OPTIONS: dict[str, set[str]] = {
    "commit": {"--message", "--file", "--reuse-message", "--reedit-message",
               "--template", "--author", "--date", "--trailer", "--cleanup",
               "--fixup", "--squash", "--pathspec-from-file", "--unified",
               "--inter-hunk-context"},
    "merge": {"--message", "--file", "--strategy", "--strategy-option",
              "--cleanup", "--into-name"},
}


@dataclass(frozen=True)
class GitOptionWord:
    text: str
    raw: str


def parse_git_short_options(raw_words: list[str], subcommand: str) -> list[GitOptionWord]:
    """Expand short clusters in order without interpreting values or path words."""
    result: list[GitOptionWord] = []
    words = iter(raw_words)
    for raw in words:
        word = shell_text(raw)
        if word == "--" or word.startswith("<<"):
            result.append(GitOptionWord(word, raw))
            result.extend(GitOptionWord(shell_text(value), value) for value in words)
            break
        if word in GIT_LONG_VALUE_OPTIONS[subcommand]:
            result.append(GitOptionWord(word, raw))
            value = next(words, None)
            if value is None:
                raise ValueError(f"{word} has no readable argument")
            result.append(GitOptionWord(shell_text(value), value))
        elif word.startswith("--") or not word.startswith("-") or word == "-":
            result.append(GitOptionWord(word, raw))
        else:
            for position, letter in enumerate(word[1:], 2):
                mode = GIT_SHORT_OPTIONS[subcommand].get(letter)
                if mode is None:
                    raise ValueError(f"unknown git {subcommand} short option -{letter}")
                if mode is ShortOptionArgument.FLAG:
                    result.append(GitOptionWord("-" + letter, raw))
                    continue
                value = word[position:]
                if mode is ShortOptionArgument.ATTACHED_ONLY:
                    result.append(GitOptionWord("-" + letter + value, raw))
                else:
                    result.append(GitOptionWord("-" + letter, "-" + letter))
                    if value:
                        result.append(GitOptionWord(value, raw))
                    else:
                        argument = next(words, None)
                        if argument is None:
                            raise ValueError(f"-{letter} has no readable argument")
                        result.append(GitOptionWord(shell_text(argument), argument))
                break
    return result


def commit_message(invocation: str, offset: int,
                   directory: Path, resolve_repository: bool) -> ReadableCommitMessage | UnreadableCommitMessage:
    """Read external messages; shell-generated message text stays unknown."""
    parsed = parse_git_short_options(raw_commit_words(invocation, offset), "commit")
    raw_words = [word.raw for word in parsed]
    words = [word.text for word in parsed]
    arguments: list[tuple[str, str, str]] = []
    skip_value = False
    for number, word in enumerate(words):
        if skip_value:
            skip_value = False
            continue
        if word == "--" or word.startswith("<<"):
            break
        if word in {"--fixup", "--squash"} or word.startswith(("--fixup=", "--squash=")):
            return ReadableCommitMessage("fixup! or squash!")
        if word in {"--author", "--date", "--trailer", "--cleanup", "-t", "--template", "-U", "--unified", "--inter-hunk-context"}:
            skip_value = True
            continue
        if dynamic_argument(raw_words[number]) and not word.startswith(("-m", "-F", "--message=", "--file=")):
            return UnreadableCommitMessage("a commit option contains a shell expansion")
        if word in {"-m", "--message", "-F", "--file", "-C", "-c", "--reuse-message", "--reedit-message"}:
            if number + 1 >= len(words):
                return UnreadableCommitMessage(f"{word} has no readable argument")
            arguments.append((word, raw_words[number + 1], words[number + 1]))
            skip_value = True
        elif word.startswith(("--message=", "--file=", "--reuse-message=", "--reedit-message=")):
            kind, value = word.split("=", 1)
            arguments.append((kind, raw_words[number], value))
    revision = ""
    message_parts: list[str] = []
    for kind, raw, value in arguments:
        if dynamic_argument(raw):
            return UnreadableCommitMessage("the message argument contains a shell expansion")
        if kind in {"-C", "-c", "--reuse-message", "--reedit-message"}:
            revision = value
            continue
        if kind in {"-F", "--file"}:
            if not resolve_repository:
                return UnreadableCommitMessage("an earlier command or repository override can change the message file")
            if value == "-":
                return UnreadableCommitMessage("the standard-input message is supplied by the shell")
            message_parts.append((directory / value).read_text(encoding="utf-8"))
        else:
            message_parts.append(value.replace("\\\n", ""))
    if message_parts:
        return ReadableCommitMessage("\n".join(message_parts))
    if not resolve_repository:
        return UnreadableCommitMessage("an earlier command can change the repository or message")
    if revision or "--amend" in words:
        return ReadableCommitMessage(git(directory, "log", "-1", "--format=%s", revision or "HEAD", "--"))
    for name in ("MERGE_MSG", "SQUASH_MSG"):
        path = Path(git(directory, "rev-parse", "--git-path", name))
        if not path.is_absolute():
            path = directory / path
        if path.exists():
            message_parts.append(path.read_text(encoding="utf-8"))
    if message_parts:
        return ReadableCommitMessage("\n".join(message_parts))
    return UnreadableCommitMessage("the commit has no readable message argument")


def merge_message(arguments: str, directory: Path, resolve_repository: bool) -> ReadableCommitMessage | UnreadableCommitMessage:
    """Git supplies an ordinary merge subject unless a message option changes it."""
    message_words: list[str] = []
    words = iter(parse_git_short_options(raw_commit_words(arguments, 0), "merge"))
    for option in words:
        word = option.text
        if word == "--":
            break
        if word in {"-e", "--edit"}:
            return UnreadableCommitMessage("the merge message can be changed by an editor")
        if word in {"-m", "--message", "-F", "--file"}:
            value = next(words)
            if dynamic_argument(value.raw):
                return UnreadableCommitMessage("the message argument contains a shell expansion")
            message_words.extend((word, shlex.quote(value.text)))
        elif word.startswith(("--message=", "--file=")):
            if dynamic_argument(option.raw):
                return UnreadableCommitMessage("the message argument contains a shell expansion")
            message_words.append(shlex.quote(word))
        elif word in {"-s", "-X", "--strategy", "--strategy-option", "--cleanup", "--into-name"}:
            _ = next(words)
    if message_words:
        return commit_message(" ".join(message_words), 0, directory, resolve_repository)
    return ReadableCommitMessage("Merge branch (git default message)")


def commit_options(parser: CheckpointParser, invocation: str, offset: int) -> tuple[bool, list[str]]:
    words = [word.text for word in parse_git_short_options(raw_commit_words(invocation, offset), "commit")]
    every = False
    skip_value = False
    for word in words:
        if skip_value:
            skip_value = False
            continue
        if word.startswith("<<"):
            break
        if word == "--":
            break
        if word in {"-m", "--message", "-F", "--file", "--author", "--date", "--trailer", "--cleanup",
                    "-C", "-c", "--reuse-message", "--reedit-message", "-t", "--template", "-U", "--unified", "--inter-hunk-context"}:
            skip_value = True
            continue
        if not word.startswith("-"):
            continue
        if parser.ALL.search(" " + word) and word in {"-a", "--all"}:
            every = True
    return every, words


def literal_tree_arguments(arguments: str) -> None:
    """Only tree-affecting arguments must be literal; message expansions stay unknown."""
    skip_value = False
    positional = False
    for option in parse_git_short_options(raw_commit_words(arguments, 0), "commit"):
        raw = option.raw
        if skip_value:
            skip_value = False
            continue
        word = option.text
        if not positional and word == "--":
            positional = True
            continue
        if not positional:
            if word in {"-m", "--message", "-F", "--file", "--author", "--date", "--trailer", "--cleanup",
                        "-C", "-c", "--reuse-message", "--reedit-message", "-t", "--template", "-U", "--unified", "--inter-hunk-context"}:
                skip_value = True
                continue
            if word.startswith(("--message=", "--file=", "--author=", "--date=", "--trailer=", "--cleanup=",
                                "--reuse-message=", "--reedit-message=")):
                continue
        if uncertain_directory_word(raw) or raw.startswith("~"):
            raise ValueError("commit paths or options contain shell expansions; use literal paths")


def selected_commit_tree(directory: Path, every: bool, words: list[str]) -> str:
    """Resolve literal path selection without touching the caller's index."""
    paths: list[str] = []
    include_staged = False
    only = False
    positional = False
    skip_value = False
    for word in words:
        if skip_value:
            skip_value = False
            continue
        if positional:
            paths.append(word)
            continue
        if word == "--":
            positional = True
            continue
        if word in {"-m", "--message", "-F", "--file", "--author", "--date", "--trailer", "--cleanup",
                    "-C", "-c", "--reuse-message", "--reedit-message", "-t", "--template", "-U", "--unified", "--inter-hunk-context"}:
            skip_value = True
            continue
        if word in {"--patch", "--interactive"} or word.startswith("--pathspec"):
            raise ValueError("interactive selection or pathspec files cannot be judged before the commit; use literal paths or stage files first")
        if word == "--only":
            only = True
        elif word == "--include":
            include_staged = True
        elif word.startswith("-") and not word.startswith("--"):
            if word == "-p":
                raise ValueError("interactive selection (-p/--patch/--interactive) cannot be judged before the commit; stage files first")
            only = only or word == "-o"
            include_staged = include_staged or word == "-i"
        elif not word.startswith("-"):
            paths.append(word)
    if (only and include_staged) or (every and (paths or only or include_staged)):
        raise ValueError("conflicting commit selection options prevent judging the tree")
    if paths:
        return path_commit_tree(directory, tuple(paths), include_staged)
    if include_staged or (only and "--amend" not in words):
        raise ValueError("path selection needs literal paths")
    if only:
        # --only --amend without paths preserves HEAD and ignores the index.
        return git(directory, "rev-parse", "HEAD^{tree}")
    top = Path(git(directory, "rev-parse", "--show-toplevel"))
    return commit_tree(top, every)


def refusal(command: str, cwd: Path) -> str:
    parser = checkpoint_parser()
    for invocation in git_invocations(command):
        if isinstance(invocation, UnreadableShellCommand):
            if possible_cargo_checkpoint(" ".join(invocation.words), cwd):
                return f"Checkpoint refused: {invocation.cause}. {OWN_CALL_FIX} {CARGO_DOC_FIX}"
            continue
        location = commit_directory(invocation.options, cwd)
        redirected = invocation.environment_override or (
            isinstance(location, UnknownCommitDirectory)
            and location.cause.startswith("--git-dir and --work-tree")
        )
        directory = location.path if isinstance(location, KnownCommitDirectory) else cwd
        try:
            resolve_repository = not bool(invocation.earlier or redirected) and isinstance(location, KnownCommitDirectory)
            message = (merge_message(invocation.arguments, directory, resolve_repository)
                       if invocation.subcommand == "merge" else
                       commit_message(invocation.arguments, 0, directory, resolve_repository))
        except Exception as error:
            message = UnreadableCommitMessage(f"{type(error).__name__}: {error}")
        if isinstance(message, UnreadableCommitMessage) and isinstance(location, KnownCommitDirectory) and not cargo_repository(directory):
            continue
        if isinstance(message, ReadableCommitMessage) and not any(
                parser.SUBJECT.match(line.lstrip()) is not None for line in message.text.splitlines()):
            continue
        if redirected:
            return ("Checkpoint refused: --git-dir, --work-tree and GIT_DIR= / GIT_INDEX_FILE= / "
                    f"GIT_WORK_TREE= overrides prevent judging the commit repository. {OWN_CALL_FIX} {CARGO_DOC_FIX}")
        if invocation.earlier:
            unknown = f" {SUBJECT_FIX}" if isinstance(message, UnreadableCommitMessage) else ""
            return f"Checkpoint refused: an earlier command can change the directory or index. {OWN_CALL_FIX} {CARGO_DOC_FIX}{unknown}"
        if isinstance(location, UnknownCommitDirectory):
            return f"Checkpoint refused: {location.cause}. {CARGO_DOC_FIX}"
        try:
            top = Path(git(directory, "rev-parse", "--show-toplevel")).resolve()
        except Exception as error:
            # A checkpoint outside git is not a Cargo checkpoint, unless its
            # requested directory itself declares Cargo and git is broken.
            if not (directory / "Cargo.toml").exists():
                continue
            return f"Checkpoint refused: {type(error).__name__}: {error}. {FIX}"
        if not (top / "Cargo.toml").exists():
            continue
        try:
            if invocation.subcommand == "merge":
                raise ValueError("a merge's resulting tree is not yet available; finish the merge, then judge and commit its staged tree")
            every, options = commit_options(parser, invocation.arguments, 0)
            literal_tree_arguments(invocation.arguments)
            check_record(top, selected_commit_tree(directory, every, options))
        except Exception as error:
            unknown = f" Subject is unknown: {message.cause}. {SUBJECT_FIX}" if isinstance(message, UnreadableCommitMessage) else ""
            return f"Checkpoint refused: {type(error).__name__}: {error}.{unknown} {FIX}"
    return ""


def cargo_repository(directory: Path) -> bool:
    try:
        top = Path(git(directory, "rev-parse", "--show-toplevel"))
    except (OSError, RuntimeError, subprocess.SubprocessError):
        top = directory
    return (top / "Cargo.toml").is_file()


def possible_cargo_checkpoint(command: str, cwd: Path) -> bool:
    """An opaque target is unknown; explicit checkpoint text applies in any cwd."""
    normalized = command.replace("\\\n", "").translate(str.maketrans("", "", "'\"\\"))
    possible = re.search(r"\bgit\b.*\b(?:commit|merge)\b", normalized, re.IGNORECASE | re.DOTALL)
    return possible is not None and ("checkpoint(" in normalized or cargo_repository(cwd))


def decide(raw: str) -> str:
    payload = cast(dict[str, object], json.loads(raw))
    if payload.get("tool_name") != "Bash":
        return ""
    tool_input = cast(dict[str, object], payload.get("tool_input", {}))
    command = tool_input.get("command")
    if not isinstance(command, str):
        return ""
    # Keep this recognizer independent of the parser: a broken parser must not
    # refuse unrelated calls just because their cwd or ordinary text says commit.
    cwd = payload.get("cwd")
    try:
        if not git_invocations(command):
            return ""
        return refusal(command, Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd())
    except Exception as error:
        return f"Checkpoint doc gate failed: {type(error).__name__}: {error}. Repair the hook. {FIX}"


def main() -> None:
    try:
        reason = decide(RAW_PAYLOAD)
    except Exception:
        # A malformed envelope has not established that its command can commit.
        return
    if reason:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))


if __name__ == "__main__":
    main()
