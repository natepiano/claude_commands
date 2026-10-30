"""Invoke Git with explicit answer statuses and preserve failures at the process boundary."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
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
