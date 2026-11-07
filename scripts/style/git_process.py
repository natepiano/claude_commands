"""Invoke Git with explicit answer statuses and preserve failures at the process boundary."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast


class LedgerError(ValueError):
    """A ledger operation cannot represent the requested check."""


class CheckCouldNotRun(LedgerError):
    """A Git read or another prerequisite prevented the requested check."""


class GitReadFailed(CheckCouldNotRun):
    """A Git read failed; command details stay available without entering a user refusal."""

    def __init__(self, command: tuple[str, ...], status: int, stderr: str) -> None:
        self.command: tuple[str, ...] = command
        self.status: int = status
        self.stderr: str = stderr
        self.operations: tuple[str, ...] = ()
        arguments = command[3:] if len(command) > 3 and command[1] == "-C" else command[1:]
        action = next(iter(arguments), "read")
        details = " " + repr(arguments[1:]) if len(arguments) > 1 else ""
        diagnosis = ": not a git repository" if "not a git repository" in stderr else ""
        super().__init__(f"git {action} failed (exit {status}){details}{diagnosis}")

    @classmethod
    def from_process(cls, error: subprocess.CalledProcessError) -> GitReadFailed:
        raw_command = cast(object, error.cmd)
        command = (raw_command,) if isinstance(raw_command, str) else tuple(cast(Sequence[str], raw_command))
        raw_stderr = cast(object, error.stderr)
        stderr = raw_stderr.decode(errors="replace") if isinstance(raw_stderr, bytes) else str(raw_stderr or "")
        return cls(command, error.returncode, stderr)


@dataclass(frozen=True)
class GitExitAnswers:
    """Exit statuses the caller recognizes as answers, including negative answers."""

    statuses: frozenset[int] = frozenset({0})


@dataclass(frozen=True)
class CaptureGitOutput:
    """Return Git stdout in memory instead of writing it to the caller's open file."""


SUCCESS_ONLY = GitExitAnswers()
CAPTURE_OUTPUT = CaptureGitOutput()


@dataclass(frozen=True)
class GitBlob:
    """A complete blob reply with its object id and bytes."""

    object_id: str
    content: bytes


@dataclass(frozen=True)
class MissingBlob:
    """Git cannot resolve the requested object."""


@dataclass(frozen=True)
class MalformedBlobReply:
    """The reply header does not describe a blob."""


@dataclass(frozen=True)
class TruncatedBlobReply:
    """The blob body or its final newline is incomplete."""


def read_batch_blob(replies: BinaryIO) -> GitBlob | MissingBlob | MalformedBlobReply | TruncatedBlobReply:
    """Read one blob reply without choosing the caller's failure policy."""
    header = replies.readline().removesuffix(b"\n")
    if header.endswith(b" missing"):
        return MissingBlob()
    fields = header.split(b" ")
    if len(fields) != 3 or not fields[2].isdigit():
        return MalformedBlobReply()
    try:
        object_id = fields[0].decode("ascii")
    except UnicodeDecodeError:
        return MalformedBlobReply()
    try:
        size = int(fields[2])
        content = replies.read(size)
    except (ValueError, OverflowError, MemoryError):
        return MalformedBlobReply()
    separator = replies.read(1)
    if fields[1] != b"blob":
        return MalformedBlobReply()
    if len(content) != size or separator != b"\n":
        return TruncatedBlobReply()
    return GitBlob(object_id, content)


@dataclass(frozen=True)
class TreeEntry:
    """One tree entry with its mode, object kind and object id."""

    mode: str
    kind: str
    object: str


def parse_tree_listing(listing: str) -> dict[str, TreeEntry]:
    """Parse a recursive, NUL-separated Git tree listing."""
    entries: dict[str, TreeEntry] = {}
    for entry in listing.split("\0"):
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        mode, kind, name = metadata.split()
        entries[path] = TreeEntry(mode, kind, name)
    return entries


def blob_contents(repo: Path, blobs: Mapping[str, str]) -> Iterator[tuple[str, bytes]]:
    """Read each path's blob through one Git batch process."""
    with tempfile.TemporaryFile() as contents:
        command = ("git", "-C", str(repo), "cat-file", "--batch")
        _ = run_git(command, input="".join(f"{blob}\n" for blob in blobs.values()).encode(), output=contents)
        _ = contents.seek(0)
        for path, blob in blobs.items():
            reply = read_batch_blob(contents)
            if isinstance(reply, TruncatedBlobReply):
                raise GitReadFailed(command, 0, f"{path}: incomplete Git blob response")
            if not isinstance(reply, GitBlob) or reply.object_id != blob:
                raise GitReadFailed(command, 0, f"{path}: invalid Git blob response")
            yield path, reply.content


def parse_status_z(output: str) -> set[str]:
    """Read changed paths, including both names of a rename or copy."""
    entries = iter(output.split("\0"))
    paths: set[str] = set()
    for entry in entries:
        if not entry:
            continue
        paths.add(entry[3:])
        if "R" in entry[:2] or "C" in entry[:2]:
            paths.add(next(entries))
    return paths


def run_git(command: tuple[str, ...], *, input: bytes = b"", answers: GitExitAnswers = SUCCESS_ONLY,
            output: CaptureGitOutput | BinaryIO = CAPTURE_OUTPUT) -> subprocess.CompletedProcess[bytes]:
    """Normalize only process failures; callers retain ownership of file reads and validation."""
    try:
        if isinstance(output, CaptureGitOutput):
            result = subprocess.run(command, input=input, capture_output=True, check=False)
        else:
            result = subprocess.run(command, input=input, stdout=output, stderr=subprocess.PIPE, check=False)
        if result.returncode not in answers.statuses:
            result.check_returncode()
    except OSError as error:
        raise GitReadFailed(command, -1, str(error)) from error
    except subprocess.CalledProcessError as error:
        raise GitReadFailed.from_process(error) from error
    return result
