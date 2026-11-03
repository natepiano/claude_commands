#!/usr/bin/env python3
"""Check review trailers before a commit or merge.

The shared command parser reads messages without executing shell text.
The review module owns the merge rule. The assertion guard checks checkpoint edits.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.hooks import checkpoint_gate as commands


class HookDecision(TypedDict):
    hookEventName: str
    permissionDecision: str
    permissionDecisionReason: str


class HookResponse(TypedDict):
    hookSpecificOutput: HookDecision


def staged(directory: Path, every: bool) -> tuple[str, ...]:
    """The files the commit takes: the index, or with -a every tracked change against HEAD."""
    result = subprocess.run(["git", "-C", str(directory), "diff", "--name-only", "HEAD" if every else "--cached"],
                            capture_output=True, text=True, check=False, timeout=10)
    return tuple(result.stdout.split()) if result.returncode == 0 else ()


def refusal(command: str, cwd: Path) -> str:
    try:
        for invocation in commands.git_invocations(command):
            if isinstance(invocation, commands.UnreadableShellCommand):
                if commands.possible_cargo_checkpoint(" ".join(invocation.words), cwd):
                    return invocation.cause
                continue
            flags = commands.git_flags(invocation.arguments, invocation.subcommand)
            from scripts.production import change_review

            location = commands.commit_directory(invocation.options, cwd)
            repo = location.path if isinstance(location, commands.KnownCommitDirectory) else cwd
            resolve_repository = not (invocation.earlier or invocation.environment_override) and isinstance(location, commands.KnownCommitDirectory)
            message = commands.invocation_message(invocation, repo, resolve_repository)
            if reason := commands.trailer_problem(invocation, repo, resolve_repository, message):
                return reason
            merging = commands.merging_commit(invocation, repo, flags)
            if isinstance(message, commands.UnreadableCommitMessage):
                if merging or commands.possible_cargo_checkpoint(command, repo):
                    return message.cause
                continue
            subject = message.text.partition("\n")[0]
            checkpoint = subject.startswith("checkpoint(")
            if checkpoint and (invocation.earlier or invocation.environment_override):
                return commands.OWN_CALL_FIX
            if checkpoint and isinstance(location, commands.UnknownCommitDirectory):
                return location.cause
            if reason := change_review.merge_review_problem(message.text, is_merge=merging):
                return reason
            if invocation.subcommand == "commit" and checkpoint:
                if reason := checkpoint_problem(repo, message.text, subject, bool(flags & {"-a", "--all"}), merging):
                    return reason
    except Exception as error:
        return f"Change-Review parser failed: {type(error).__name__}: {error}"
    return ""


def checkpoint_problem(repo: Path, text: str, subject: str, every: bool, merging: bool) -> str:
    from scripts.production import change_review
    from scripts.direct import assertion_guard

    files = staged(repo, every)
    problem = "" if merging else change_review.problem(subject, text, files or ("unknown",))
    if problem or not any(path.endswith((".rs", ".py")) for path in files):
        return problem
    arguments = [sys.executable, str(Path(__file__).resolve().parents[1] / "direct" / "assertion_guard.py"),
                 "--repo", str(repo), "--rulings", "-"]
    if not every:
        arguments.append("--staged")
    try:
        for base in assertion_guard.commit_bases(repo):
            arguments.extend(("--base", base))
        result = subprocess.run(arguments, input=text, capture_output=True, text=True, check=False, timeout=10)
    except (assertion_guard.GuardError, OSError, subprocess.TimeoutExpired) as error:
        return f"assertion guard could not run: {error}"
    if result.returncode == 1:
        return "\n".join(result.stdout.splitlines()[-20:])
    if result.returncode:
        return "assertion guard could not run: " + (result.stderr.strip() or f"exit {result.returncode}")
    return ""


def decide(raw: str) -> str:
    command, cwd = commands.bash_command(raw)
    reason = refusal(command, cwd)
    return f"Checkpoint refused: {reason}." if reason else ""


def main() -> None:
    try:
        reason = decide(sys.stdin.read())
    except Exception as error:
        print(f"change review gate: {type(error).__name__}: {error}", file=sys.stderr)
        return
    if reason:
        response: HookResponse = {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}
        print(json.dumps(response))


if __name__ == "__main__":
    main()
