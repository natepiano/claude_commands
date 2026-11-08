#!/usr/bin/env python3
"""Prepare and validate a simplify review."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from pathlib import Path
import re
from string import Template
import shlex

from scripts.agents.claude_result import claude_result
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic


def log_errors(log: str) -> str:
    """Read plain launch errors from the review log."""
    return "\n".join(line.removeprefix("ERROR:").strip() for line in log.splitlines() if line.startswith("ERROR:"))


def failure_reason(log: str, exit_code: int) -> str:
    """Use launch errors or the exit code when validation gives no reason."""
    return log_errors(log) or f"the simplify review did not finish successfully (exit {exit_code})"


def well_formed(report: str) -> bool:
    """Accept Pass or item 1 after a heading or preamble."""
    return any(line.strip() == "Pass" or re.match(r"^1[.)]\s", line.strip()) is not None
               for line in report.strip().splitlines())


def prepare(diff: Path, source: Path, prompt: Path, report: Path, settings: conf.ConfigRead) -> None:
    """Clear the report and write the prompt with the standing instructions."""
    _ = report.write_text("")
    standing = Template((conf.directory() / "review/simplify.md").read_text()).substitute(
        agents=",".join(settings.words("SIMPLIFY_AGENTS")))
    user_words = source.read_text()
    write_text_atomic(prompt, f"/simplify {shlex.quote(str(diff))}\n\n" + standing + "\n\n" + user_words + "\n")


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

