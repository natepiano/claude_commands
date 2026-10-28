#!/usr/bin/env python3
"""Require a durable rustdoc pass for the exact tree of every Rust checkpoint.
In every repo, a checkpoint commit must be the first command of its own Bash call.

Commit and message recognition comes from the change-review hook. Unlike other
hooks, an error checking a Rust checkpoint refuses it: this gate fails closed.
"""
import sys

# Every Bash call reaches this hook. Do not load even json until it might commit.
if __name__ == "__main__":
    RAW_PAYLOAD = sys.stdin.read()
    if "commit" not in RAW_PAYLOAD:
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
from pathlib import Path
from typing import Protocol, cast


class CheckpointParser(Protocol):
    COMMIT: re.Pattern[str]
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


def excluded_files(directory: Path, every: bool) -> bool:
    if git(directory, "ls-files", "--others", "--exclude-standard"):
        return True
    return not every and bool(git(directory, "diff", "--name-only"))


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


def commit_words(invocation: str, offset: int) -> list[str]:
    """Read shell words only up to this commit's first command boundary."""
    return [" ".join(shlex.split(raw)) for raw in raw_commit_words(invocation, offset)]


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


SHELL_WORD = re.compile(r'''(?:'[^']*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`|\$\([^)]*\)|\\.|[^\s;&|'"`\\])+''')
SUBJECT_FIX = ("Write the subject as literal -m text, a -F file or the heredoc form, "
               "or, in a repo with a Cargo.toml at its top, run ~/.claude/scripts/direct/verify.sh doc first.")
OWN_CALL_FIX = ("Run the checkpoint commit as the first command of its own Bash call; "
                "put cd, git add and other commands in earlier calls.")
CARGO_DOC_FIX = ("In a repo with a Cargo.toml at its top, also run "
                 "~/.claude/scripts/direct/verify.sh doc in an earlier call.")


def raw_commit_words(invocation: str, offset: int) -> list[str]:
    words: list[str] = []
    for match in SHELL_WORD.finditer(invocation, offset):
        gap = invocation[offset:match.start()]
        if any(boundary in gap for boundary in (";", "&", "|", "\n")):
            break
        words.append(match.group())
        offset = match.end()
    return words


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
        return KnownCommitDirectory(directory)
    except (ValueError, StopIteration) as error:
        return UnknownCommitDirectory(f"git directory options could not be parsed: {type(error).__name__}: {error}")


def commit_message(invocation: str, offset: int,
                   directory: Path, resolve_repository: bool) -> ReadableCommitMessage | UnreadableCommitMessage:
    """Read external messages; shell-generated message text stays unknown."""
    raw_words = raw_commit_words(invocation, offset)
    words = commit_words(invocation, offset)
    arguments: list[tuple[str, str, str]] = []
    skip_value = False
    for number, word in enumerate(words):
        if skip_value:
            skip_value = False
            continue
        if word.startswith("<<"):
            break
        if word in {"--fixup", "--squash"} or word.startswith(("--fixup=", "--squash=")):
            return ReadableCommitMessage("fixup! or squash!")
        if word in {"--author", "--date", "--trailer", "--cleanup"}:
            skip_value = True
            continue
        if dynamic_argument(raw_words[number]) and not word.startswith(("-m", "-F", "--message=", "--file=")):
            return UnreadableCommitMessage("a commit option contains a shell expansion")
        if word in {"-m", "--message", "-F", "--file", "-C", "-c", "--reuse-message", "--reedit-message"} or (
                word.startswith("-") and not word.startswith("--") and word.endswith("m")):
            if number + 1 >= len(words):
                return UnreadableCommitMessage(f"{word} has no readable argument")
            arguments.append((word, raw_words[number + 1], words[number + 1]))
            skip_value = True
        elif word.startswith(("--message=", "--file=", "--reuse-message=", "--reedit-message=")):
            kind, value = word.split("=", 1)
            arguments.append((kind, raw_words[number], value))
        elif word.startswith(("-m", "-F", "-C", "-c")):
            arguments.append((word[:2], raw_words[number], word[2:]))
    revision = ""
    message_parts: list[str] = []
    literal_message = False
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
            # Checkpoint text in literal -m arguments is covered by the whole
            # command rule, without guessing argument or heredoc positions.
            literal_message = True
    if literal_message or message_parts:
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


def commit_options(parser: CheckpointParser, invocation: str, offset: int) -> tuple[bool, list[str]]:
    words = commit_words(invocation, offset)
    every = False
    options: list[str] = []
    skip_value = False
    for word in words:
        if skip_value:
            skip_value = False
            continue
        if word.startswith("<<"):
            break
        if word in {"-m", "--message", "-F", "--file", "--author", "--date", "--trailer", "--cleanup",
                    "-C", "-c", "--reuse-message", "--reedit-message"}:
            skip_value = True
            continue
        if not word.startswith("-"):
            raise ValueError("checkpoint path arguments can change its tree; stage the files and commit without paths")
        # Attached message/revision/file text is not a cluster of Git switches.
        flags = re.split("[mFCc]", word[1:], maxsplit=1)[0] if not word.startswith("--") else ""
        if parser.ALL.search(" --all" if word == "--all" else " -" + flags):
            every = True
        if word.startswith("-") and not word.startswith("--") and word.endswith("m"):
            skip_value = True
        options.append(word)
    return every, options


def refusal(command: str, cwd: Path) -> str:
    parser = checkpoint_parser()
    # Conservatively join text that shell quote removal can turn into a
    # checkpoint, without choosing which argument or heredoc is the message.
    normalized_command = command.replace("\\\n", "").translate(str.maketrans("", "", "'\"\\"))
    checkpoint_text = "checkpoint(" in normalized_command
    commits = list(parser.COMMIT.finditer(command))
    for number, commit in enumerate(commits):
        end = commits[number + 1].start() if number + 1 < len(commits) else len(command)
        invocation = command[commit.start():end]
        prefix = command[:commit.start()].strip()
        redirected = re.search(r"--(?:git-dir|work-tree)(?:=|\s)|\bGIT_(?:DIR|INDEX_FILE|WORK_TREE)=",
                               command[:commit.end()])
        location = commit_directory(commit.group("options"), cwd)
        directory = location.path if isinstance(location, KnownCommitDirectory) else cwd
        try:
            message = commit_message(invocation, commit.end() - commit.start(), directory,
                                     not bool(prefix or redirected) and isinstance(location, KnownCommitDirectory))
        except Exception as error:
            message = UnreadableCommitMessage(f"{type(error).__name__}: {error}")
        if not checkpoint_text and isinstance(message, ReadableCommitMessage) and not any(
                parser.SUBJECT.match(line.lstrip()) is not None for line in message.text.splitlines()):
            continue
        if redirected:
            return ("Checkpoint refused: --git-dir, --work-tree and GIT_DIR= / GIT_INDEX_FILE= / "
                    f"GIT_WORK_TREE= overrides prevent judging the commit repository. {OWN_CALL_FIX} {CARGO_DOC_FIX}")
        if prefix:
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
        extra = ""
        try:
            every, options = commit_options(parser, invocation, commit.end() - commit.start())
            if any(option in {"--", "-o", "--only", "-i", "--include", "-p", "--patch", "--interactive"}
                   or option.startswith("--pathspec") for option in options):
                raise ValueError("checkpoint must commit the staged index (or tracked changes with -a); stage its files and omit path selection")
            if excluded_files(top, every):
                extra = " Commit or remove untracked or unstaged files left out of this commit; the judged tree must equal the committed tree."
            check_record(top, commit_tree(top, every))
        except Exception as error:
            unknown = f" Subject is unknown: {message.cause}. {SUBJECT_FIX}" if isinstance(message, UnreadableCommitMessage) else ""
            return f"Checkpoint refused: {type(error).__name__}: {error}.{extra}{unknown} {FIX}"
    return ""


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
    if re.search(r"\bgit\b[^\n;&|]*\bcommit\b", command) is None:
        return ""
    cwd = payload.get("cwd")
    try:
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
