#!/usr/bin/env python3
"""Prepare, validate, and report a simplify review."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
from string import Template
import shlex
import sys
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.agents.claude_result import claude_result
from scripts.lib import conf
from scripts.issue_score.selection import UseConfiguredValue
from scripts.lib.atomic_file import write_text_atomic


@dataclass(frozen=True)
class ReviewFiles:
    """Use the artifact paths that the launcher owns."""

    findings: Path
    status: Path
    log: Path
    summary: Path


def log_errors(log: str) -> str:
    """Read plain launch errors from the review log."""
    return "\n".join(line.removeprefix("ERROR:").strip() for line in log.splitlines() if line.startswith("ERROR:"))


def failure_reason(reason: str, report: str, log: Path, exit_code: int) -> str:
    """Read log evidence only after a stated or extracted reason."""
    if reason:
        return reason
    if report.startswith("ERROR:"):
        return report.removeprefix("ERROR:").strip()
    if log.exists():
        detail = log_errors(log.read_text(errors="replace"))
        if detail:
            return detail
    return f"the simplify review did not finish successfully (exit {exit_code})"


def well_formed(report: str) -> bool:
    """Accept Pass or item 1 after a heading or preamble."""
    return any(line.strip() == "Pass" or re.match(r"^1[.)]\s", line.strip()) is not None
               for line in report.strip().splitlines())


def prepare(diff: Path, cwd: Path, source: Path, prompt: Path, files: ReviewFiles, *,
            settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> Path:
    """Clear old evidence, check the diff, and write the prompt."""
    if isinstance(settings, UseConfiguredValue):
        settings = conf.read("direct")
    _ = files.findings.write_text("")
    _ = files.summary.write_text("")
    diff = (cwd / diff.expanduser()).resolve()
    with diff.open("rb") as stream:
        _ = stream.read(1)
    standing = Template((conf.directory() / "review/simplify.md").read_text()).substitute(
        agents=",".join(settings.words("SIMPLIFY_AGENTS")))
    if source.resolve() == prompt.resolve():
        prompt = prompt.with_name(f"{prompt.stem}.generated{prompt.suffix}")
    user_words = source.read_text()
    write_text_atomic(prompt, f"/simplify {shlex.quote(str(diff))}\n\n" + standing + "\n\n" + user_words + "\n")
    return prompt


def validate(report: str, log: str, agent_count: int) -> str:
    """Return the reason when the report or agent completion is invalid."""
    if report.startswith("ERROR:"):
        return report.removeprefix("ERROR:").strip()
    result = claude_result(log)
    if result.reason:
        return result.reason
    if not report.strip():
        return log_errors(log) or "Claude returned an empty simplify report"
    if result.stats and (result.stats.get("completed") != agent_count or result.stats.get("failed") != 0):
        return f"the {agent_count} simplify agents did not all complete successfully"
    return "" if well_formed(report) else "the simplify report must start with Pass or item 1"


def finish(files: ReviewFiles, reason: str, exit_code: int) -> str:
    """Copy a finished review and return its final state to the launcher."""
    report = files.findings.read_text() if files.findings.exists() else ""
    previous = files.status.read_text().splitlines()[0] if files.status.exists() else "error"
    action = "reviewed" if exit_code == 0 and previous == "reviewed" else "error"
    if action == "error":
        reason = failure_reason(reason, report, files.log, exit_code)
        report = f"ERROR: {reason}\n"
        _ = files.findings.write_text(report)
        print(f"simplify: error: {reason}", file=sys.stderr)
    _ = files.summary.write_text(report)
    return action


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "finish", "check"):
        command = commands.add_parser(name)
        _ = command.add_argument("--findings-file", type=Path, required=True)
        _ = command.add_argument("--status-file", type=Path, required=True)
        _ = command.add_argument("--log-file", type=Path, required=True)
        _ = command.add_argument("--summary-file", type=Path, required=True)
        if name == "prepare":
            _ = command.add_argument("--diff", type=Path, required=True)
            _ = command.add_argument("--cwd", type=Path, required=True)
            _ = command.add_argument("--source", type=Path, required=True)
            _ = command.add_argument("--prompt-file", type=Path, required=True)
        if name == "finish":
            _ = command.add_argument("--reason", default="")
            _ = command.add_argument("--exit-code", type=int, required=True)
    args = parser.parse_args()
    command_name = cast(str, args.command)
    files = ReviewFiles(cast(Path, args.findings_file), cast(Path, args.status_file), cast(Path, args.log_file),
                        cast(Path, args.summary_file))
    if command_name == "finish":
        print(finish(files, cast(str, args.reason), cast(int, args.exit_code)))
        return 0
    settings = conf.read("direct")
    if command_name == "prepare":
        try:
            print(prepare(cast(Path, args.diff), cast(Path, args.cwd), cast(Path, args.source),
                          cast(Path, args.prompt_file), files, settings=settings))
            return 0
        except (OSError, KeyError, ValueError) as error:
            print(f"unable to prepare the simplify review: {error}", file=sys.stderr)
            return 1
    report = files.findings.read_text() if files.findings.exists() else ""
    log = files.log.read_text(errors="replace") if files.log.exists() else ""
    reason = validate(report, log, len(settings.words("SIMPLIFY_AGENTS")))
    if reason:
        if not report.strip() or well_formed(report):
            _ = files.findings.write_text(f"ERROR: {reason}")
        print(reason)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
