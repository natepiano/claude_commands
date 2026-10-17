#!/usr/bin/env python3
"""Notify standing style productions and record workspace lint coverage."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import cast

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS / "production"))

from add_style import NoStyleProduction, standing_style_production
from production_units import Refusal
import showrunners
import style_ledger

PYTHON = SCRIPTS / "lib/py"
SEND = SCRIPTS / "message/send.py"
SHOWRUNNERS = SCRIPTS / "production/showrunners.py"
VERIFY = SCRIPTS / "delegate/verify.sh"
LEDGER = SCRIPTS / "style/style_ledger.py"
TOOLS: tuple[style_ledger.LintTool, ...] = ("clippy", "mend", "rustfmt")


@dataclass(frozen=True)
class ToolLintRecord:
    tool: style_ledger.LintTool
    passed: bool
    diagnostics: Mapping[str, int]


def notify(repo_dir: Path, branch: str, tip: str) -> None:
    """A failed notification must never turn a completed merge into a failure."""
    try:
        standing = standing_style_production(repo_dir)
        branch = branch.removeprefix("refs/heads/")
        if isinstance(standing, NoStyleProduction) or standing.branch_point != branch:
            return
        address = subprocess.run([str(PYTHON), str(SHOWRUNNERS), "address", standing.slug],
                                 capture_output=True, text=True, check=False, timeout=10)
        if address.returncode or not address.stdout.strip():
            raise Refusal(address.stderr.strip() or f"no address for {standing.slug}")
        caller_doc = showrunners.production_of(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
        caller = showrunners.production_slug(str(caller_doc)) if caller_doc else "style_sync"
        sent = subprocess.run([
            str(PYTHON), str(SEND), "--to", address.stdout.strip(), "--from", caller,
            "--text", f"From {caller}: branch point {branch} moved to {tip[:7]}; sync.",
            "--key", f"style-sync:{standing.slug}",
            "--timeout", "10",
        ], capture_output=True, text=True, check=False, timeout=15)
        if sent.returncode:
            raise Refusal(sent.stderr.strip() or sent.stdout.strip() or f"send.py exited {sent.returncode}")
    except (Refusal, OSError, ValueError, subprocess.TimeoutExpired) as error:
        detail = " ".join(str(error).splitlines()).removeprefix("style_sync: ")
        print("style_sync: notify: " + detail)


def lint_records(output: str, checkout: Path) -> tuple[ToolLintRecord, ...]:
    """Read verify's workspace tool boundaries, retaining diagnostic file counts."""
    passed = dict.fromkeys(TOOLS, False)
    diagnostics: dict[style_ledger.LintTool, dict[str, int]] = {tool: {} for tool in TOOLS}
    active: style_ledger.LintTool | None = None
    for line in re.sub(r"\x1b\[[0-9;]*m", "", output).splitlines():
        marker = re.fullmatch(r"style-lint: (clippy|mend|rustfmt) (start|passed)", line)
        if marker:
            tool = cast(style_ledger.LintTool, marker[1])
            active = tool if marker[2] == "start" else None
            if marker[2] == "passed":
                passed[tool] = True
            continue
        if active is None:
            continue
        location = re.search(r"(?:-->|:::|Diff in)\s+(.+?):\d+(?::\d+)?", line)
        if location:
            path = Path(location[1])
            if path.is_absolute():
                try:
                    path = path.relative_to(checkout)
                except ValueError:
                    continue
            name = path.as_posix().removeprefix("./")
            diagnostics[active][name] = diagnostics[active].get(name, 0) + 1
    return tuple(ToolLintRecord(tool, passed[tool], MappingProxyType(diagnostics[tool])) for tool in TOOLS)


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True, check=False)
    if result.returncode:
        raise Refusal(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout.strip()


def lint(repo: Path, checkout: Path, tip: str) -> int:
    """Judge the requested commit, never a style branch with additional fixes."""
    tip = git(checkout, "rev-parse", "--verify", f"{tip}^{{commit}}")
    if git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir") != git(
        checkout, "rev-parse", "--path-format=absolute", "--git-common-dir",
    ):
        raise Refusal("--repo and --checkout must belong to the same repository")
    directory = Path(tempfile.mkdtemp(prefix="style-sync-lint-"))
    snapshot = directory / "checkout"
    try:
        _ = git(checkout, "worktree", "add", "--detach", str(snapshot), tip)
        result = subprocess.run(
            ["bash", str(VERIFY), "lint", "--workspace", "--check", "--no-cache"], cwd=snapshot,
            env={**os.environ, "PLAN_DELEGATE_DETACHED_RUN": "1"},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False,
        )
        print(result.stdout, end="")
        rewritten = bool(git(snapshot, "status", "--porcelain"))
        records = lint_records(result.stdout, snapshot)
        for record in records:
            passed = record.passed and not rewritten
            diagnostic_file = directory / f"{record.tool}.json"
            _ = diagnostic_file.write_text(json.dumps(dict(record.diagnostics), sort_keys=True))
            recorded = subprocess.run([
                str(PYTHON), str(LEDGER), "lint-record", "--repo", str(repo), "--tip", tip,
                "--tool", record.tool, "--passed", str(int(passed)),
                "--diagnostics", str(diagnostic_file),
            ], capture_output=True, text=True, check=False)
            if recorded.returncode:
                raise Refusal(recorded.stderr.strip() or f"lint-record exited {recorded.returncode}")
        if rewritten:
            print("style_sync: lint rewrote the checked tree; no successful coverage recorded")
        return int(result.returncode != 0 or rewritten
                   or not all(record.passed and not record.diagnostics for record in records))
    finally:
        try:
            if snapshot.exists():
                _ = git(checkout, "worktree", "remove", "--force", str(snapshot))
        finally:
            shutil.rmtree(directory)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    notice = commands.add_parser("notify")
    _ = notice.add_argument("--repo-dir", type=Path, required=True)
    _ = notice.add_argument("--branch", required=True)
    _ = notice.add_argument("--tip", required=True)
    run_lint = commands.add_parser("lint")
    _ = run_lint.add_argument("--repo", type=Path, required=True)
    _ = run_lint.add_argument("--checkout", type=Path, required=True)
    _ = run_lint.add_argument("--tip", required=True)
    args = parser.parse_args(argv)
    if cast(str, args.command) == "notify":
        notify(cast(Path, args.repo_dir), cast(str, args.branch), cast(str, args.tip))
        return 0
    try:
        return lint(cast(Path, args.repo), cast(Path, args.checkout), cast(str, args.tip))
    except (Refusal, OSError, ValueError) as error:
        print("style_sync: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
