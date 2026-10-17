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
ROSTER = SCRIPTS / "message/roster.py"
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


def runner_value(action: str, slug: str) -> str:
    result = subprocess.run(
        [str(PYTHON), str(SHOWRUNNERS), action, slug],
        capture_output=True, text=True, check=False, timeout=10,
    )
    if result.returncode:
        raise Refusal(result.stderr.strip() or f"showrunners.py {action} exited {result.returncode}")
    return result.stdout.strip()


def handoff_notice(slug: str, parent: str, checkpoint: str) -> None:
    address = runner_value("address", parent)
    if not address:
        raise Refusal(f"no address for {parent}")
    result = subprocess.run([
        str(PYTHON), str(SEND), "--to", address, "--from", slug,
        "--text", f"From the showrunner of {slug}: hand-off {checkpoint}",
        "--timeout", "10",
    ], capture_output=True, text=True, check=False, timeout=15)
    if result.returncode:
        raise Refusal(result.stderr.strip() or result.stdout.strip() or f"send.py exited {result.returncode}")


@dataclass(frozen=True)
class WaitingUnitDirector:
    unit: str
    address: str


def _waiting_directors(slug: str) -> tuple[WaitingUnitDirector, ...]:
    try:
        result = subprocess.run(
            [str(PYTHON), str(ROSTER), "--json"],
            capture_output=True, text=True, check=False, timeout=15,
        )
        if result.returncode not in (0, 1):
            raise Refusal(result.stderr.strip() or f"roster.py exited {result.returncode}")
        payload = cast(object, json.loads(result.stdout))
        if not isinstance(payload, dict):
            raise Refusal("expected an object")
        entries = cast(dict[str, object], payload).get("entries")
        if not isinstance(entries, list):
            raise Refusal("expected entries list")
        directors: list[WaitingUnitDirector] = []
        for entry in cast(list[object], entries):
            if not isinstance(entry, dict):
                raise Refusal("invalid entry")
            fields = cast(dict[str, object], entry)
            if fields.get("role") != "unit director" or fields.get("production") != slug:
                continue
            unit, address = fields.get("unit"), fields.get("address")
            if not isinstance(unit, str) or not isinstance(address, str):
                raise Refusal("invalid director unit or address")
            if address:
                directors.append(WaitingUnitDirector(unit, address))
        return tuple(directors)
    except (Refusal, OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise Refusal("roster: " + " ".join(str(error).splitlines())) from error


def wake_waiters(repo: Path, ticket_id: str) -> None:
    """Retry waiting directors after a merge, retaining undelivered wakes."""
    with style_ledger.open_ledger(repo) as ledger:
        waits = ledger.sync_waits()
        if not waits:
            return
        try:
            standing = standing_style_production(repo)
        except (Refusal, OSError, ValueError, subprocess.TimeoutExpired) as error:
            for wait in waits:
                print(f"style_ledger: wake {wait.unit}: " + " ".join(str(error).splitlines()))
            return
        if isinstance(standing, NoStyleProduction):
            return
        try:
            directors = _waiting_directors(standing.slug)
        except Refusal as error:
            for wait in waits:
                print(f"style_ledger: wake {wait.unit}: {error}")
            return
        for wait in waits:
            try:
                addresses = [director.address for director in directors if director.unit == wait.unit]
                if not addresses:
                    raise Refusal("no live director")
                if len(addresses) != 1:
                    raise Refusal(f"{len(addresses)} directors")
                result = subprocess.run([
                    str(PYTHON), str(SEND), "--to", addresses[0], "--from", standing.slug,
                    "--text", f"From the showrunner of {standing.slug}: ticket {ticket_id} merged; refill.",
                    "--timeout", "10",
                ], capture_output=True, text=True, check=False, timeout=15)
                if result.returncode:
                    raise Refusal(result.stderr.strip() or result.stdout.strip() or f"send.py exited {result.returncode}")
            except (Refusal, OSError, ValueError, subprocess.TimeoutExpired) as error:
                print(f"style_ledger: wake {wait.unit}: " + " ".join(str(error).splitlines()))
                continue
            ledger.acknowledge_sync_wait(wait)


def replay_handoffs(ledger: style_ledger.Ledger, slug: str, parent: str) -> int:
    failed = False
    for row in ledger.pending_handoffs():
        checkpoint = row["checkpoint"]
        if not parent:
            print(f"root: merge {checkpoint[:7]} with --root-self")
            continue
        try:
            handoff_notice(slug, parent, checkpoint)
        except (Refusal, OSError, ValueError, subprocess.TimeoutExpired) as error:
            print("style_sync: hand-off: " + " ".join(str(error).splitlines()))
            failed = True
            continue
        if row["state"] == "pending":
            ledger.handoff_send(checkpoint, parent)
        elif row["sent_to"] != parent:
            ledger.handoff_reroute(checkpoint, parent)
    return int(failed)


def resume(repo: Path, slug: str, branch_point: str, style_branch: str) -> int:
    with style_ledger.open_ledger(repo) as ledger:
        for change in ledger.reconcile(branch_point, style_branch):
            print(change, flush=True)
        return replay_handoffs(ledger, slug, runner_value("parent", slug))


def send_handoff(repo: Path, slug: str, checkpoint: str) -> int:
    with style_ledger.open_ledger(repo) as ledger:
        checkpoint = git(repo, "rev-parse", "--verify", f"{checkpoint}^{{commit}}")
        ledger.handoff_add(checkpoint)
        parent = runner_value("parent", slug)
        if not parent:
            print(f"root: merge {checkpoint[:7]} with --root-self")
            return 0
        try:
            handoff_notice(slug, parent, checkpoint)
        except (Refusal, OSError, ValueError, subprocess.TimeoutExpired) as error:
            print("style_sync: hand-off: " + " ".join(str(error).splitlines()))
            return 1
        row = next((row for row in ledger.pending_handoffs() if row["checkpoint"] == checkpoint), None)
        if row is not None:
            if row["state"] == "pending":
                ledger.handoff_send(checkpoint, parent)
            elif row["sent_to"] != parent:
                ledger.handoff_reroute(checkpoint, parent)
        return 0


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
    replay = commands.add_parser("resume")
    handoff = commands.add_parser("send-handoff")
    wake = commands.add_parser("wake-waiters")
    _ = wake.add_argument("--repo", type=Path, required=True)
    _ = wake.add_argument("--ticket", required=True)
    for command in (replay, handoff):
        _ = command.add_argument("--repo", type=Path, required=True)
        _ = command.add_argument("--slug", required=True)
    _ = replay.add_argument("--branch-point", required=True)
    _ = replay.add_argument("--style-branch", required=True)
    _ = handoff.add_argument("--checkpoint", required=True)
    args = parser.parse_args(argv)
    if cast(str, args.command) == "notify":
        notify(cast(Path, args.repo_dir), cast(str, args.branch), cast(str, args.tip))
        return 0
    try:
        if cast(str, args.command) == "wake-waiters":
            wake_waiters(cast(Path, args.repo), cast(str, args.ticket))
            return 0
        if cast(str, args.command) == "resume":
            return resume(cast(Path, args.repo), cast(str, args.slug),
                          cast(str, args.branch_point), cast(str, args.style_branch))
        if cast(str, args.command) == "send-handoff":
            return send_handoff(cast(Path, args.repo), cast(str, args.slug), cast(str, args.checkpoint))
        return lint(cast(Path, args.repo), cast(Path, args.checkout), cast(str, args.tip))
    except (Refusal, style_ledger.LedgerError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print("style_sync: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
