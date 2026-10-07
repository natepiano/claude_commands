#!/usr/bin/env python3
"""Count production CI points, verify their runs, and send one review alert."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, cast

from add_unit import Production, Refusal, cell_value, read_production
from merge_checkpoint import CodeCheckpoint, MergeEntry, git, merge_branch_history


class CiDue(NamedTuple):
    pass


class CiNotDue(NamedTuple):
    merges_since: int


class CiNotUsed(NamedTuple):
    pass


class CiRecord(NamedTuple):
    merges_collected: int
    tip: str


class NoCiRecord(NamedTuple):
    pass


class WatchFirstAlert(NamedTuple):
    line: str
    table: str


class WatchRepeat(NamedTuple):
    line: str


class WatchBelowThreshold(NamedTuple):
    line: str


class CiRun(NamedTuple):
    repo: str
    run_id: str
    tip: str


class PointFailure(Exception):
    def __init__(self, step: str, detail: str) -> None:
        super().__init__(detail)
        self.step: str = step
        self.detail: str = detail


def report(step: str, state: str, detail: str) -> None:
    print(f"{step}: {state} — {detail.replace(chr(10), '; ')}", flush=True)


def command(step: str, *argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    except OSError as error:
        raise PointFailure(step, str(error)) from error


def required(result: subprocess.CompletedProcess[str], step: str) -> str:
    if result.returncode:
        raise PointFailure(step, result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
    return result.stdout.strip()


def branch_tip(production: Production) -> str:
    return required(git(production.checkout, "rev-parse", production.merge_branch), "ci collect")


def ci_record(state_dir: Path) -> CiRecord | NoCiRecord:
    path = state_dir / "ci_points.json"
    if not path.exists():
        return NoCiRecord()
    raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise PointFailure("due", f"invalid {path}")
    fields = cast(dict[str, object], raw)
    count, tip = fields.get("count"), fields.get("tip")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0 or not isinstance(tip, str):
        raise PointFailure("due", f"invalid {path}")
    return CiRecord(count, tip)


def due(production: Production, state_dir: Path, no_ci: bool) -> CiDue | CiNotDue | CiNotUsed:
    if no_ci:
        return CiNotUsed()
    count = merge_branch_history(production.checkout, production.merge_branch).code_merge_count()
    record = ci_record(state_dir)
    since = count - (record.merges_collected if isinstance(record, CiRecord) else 0)
    if since < 0:
        raise PointFailure("due", "merge history predates the last collected CI point")
    return CiDue() if since >= 5 else CiNotDue(since)


def write_json(path: Path, fields: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    _ = temporary.write_text(json.dumps(fields) + "\n", encoding="utf-8")
    _ = temporary.replace(path)


def read_run(state_dir: Path) -> CiRun:
    path = state_dir / "ci_run.json"
    if not path.exists():
        raise PointFailure("ci collect", "no started CI point; run ci start")
    raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise PointFailure("ci collect", f"invalid {path}")
    fields = cast(dict[str, object], raw)
    repo, run_id, tip = fields.get("repo"), fields.get("run_id"), fields.get("tip")
    if not all(isinstance(value, str) and value for value in (repo, run_id, tip)):
        raise PointFailure("ci collect", f"invalid {path}")
    return CiRun(cast(str, repo), cast(str, run_id), cast(str, tip))


def handoff(output: str) -> CiRun:
    block = output.rpartition("=== CI HANDOFF TO AGENT ===")[2]
    fields = dict(re.findall(r"(?m)^(repo|sha|run_id):\s*(\S+)", block))
    if any(key not in fields for key in ("repo", "sha", "run_id")) or fields["run_id"] == "none":
        raise PointFailure("ci start", "validation did not provide a CI run; rerun ci start")
    return CiRun(fields["repo"], fields["run_id"], fields["sha"])


def ci_start(production: Production, state_dir: Path, no_ci: bool, cancel_prior: bool) -> None:
    if no_ci:
        report("ci start", "ok", "skipped: no CI")
        return
    script = Path(os.environ.get("CI_POINTS_VALIDATE_SCRIPT", str(Path.home() / ".claude/scripts/validate_and_push/validate_and_push.sh")))
    history = merge_branch_history(production.checkout, production.merge_branch)
    last_code = next((entry for entry in history.entries if isinstance(entry.kind, CodeCheckpoint)), None)
    checkpoint = (f"{last_code.unit} phase {last_code.phase}" if isinstance(last_code, MergeEntry)
                  else "production wrap")
    argv = ("bash", str(script), "--to", production.merge_branch, "--fix-commit",
            f"ci({production.slug}): validation fixes after {checkpoint}")
    if cancel_prior:
        argv += ("--cancel-prior",)
    result = command("validation", *argv, cwd=production.checkout)
    if result.returncode:
        raise PointFailure("validation", result.stderr.strip() or result.stdout.strip() or "validation failed")
    report("validation", "ok", "local validation passed")
    run = handoff(result.stdout)
    tip = branch_tip(production)
    if run.tip != tip:
        raise PointFailure("ci start", f"validation ran on {run.tip}; merge branch tip is {tip}")
    write_json(state_dir / "ci_run.json", {"repo": run.repo, "run_id": run.run_id, "tip": run.tip})
    report("ci start", "ok", f"run {run.run_id} on {run.tip} ({run.repo})")


def run_view(run: CiRun, checkout: Path) -> dict[str, object]:
    output = required(command("ci collect", "gh", "run", "view", run.run_id, "--repo", run.repo,
                              "--json", "headSha,status,conclusion,jobs", cwd=checkout), "ci collect")
    raw = cast(object, json.loads(output))
    if not isinstance(raw, dict):
        raise PointFailure("ci collect", "invalid GitHub run result")
    return cast(dict[str, object], raw)


def ci_collect(production: Production, state_dir: Path, no_ci: bool) -> None:
    if no_ci:
        report("ci collect", "ok", "skipped: no CI")
        return
    run = read_run(state_dir)
    tip = branch_tip(production)
    count = merge_branch_history(production.checkout, tip).code_merge_count()
    _ = command("ci collect", "gh", "run", "watch", run.run_id, "--repo", run.repo,
                "--exit-status", cwd=production.checkout)
    view = run_view(run, production.checkout)
    head = view.get("headSha")
    if not isinstance(head, str) or not head:
        raise PointFailure("ci collect", f"rerun CI on {tip}: CI result names no head commit")
    ran_on = head
    if ran_on != run.tip or ran_on != tip:
        raise PointFailure("ci collect", f"rerun CI on {tip}: CI ran on {ran_on}")
    if view.get("status") != "completed":
        raise PointFailure("ci collect", f"rerun CI on {tip}: CI on {ran_on} is unfinished")
    raw_jobs = view.get("jobs")
    if not isinstance(raw_jobs, list):
        raise PointFailure("ci collect", f"rerun CI on {tip}: CI on {ran_on} has no job results")
    jobs = cast(list[object], raw_jobs)
    required_linux = False
    for item in jobs:
        if not isinstance(item, dict):
            raise PointFailure("ci collect", f"rerun CI on {tip}: invalid job result on {ran_on}")
        job = cast(dict[str, object], item)
        name = job.get("name")
        conclusion = job.get("conclusion")
        if name == "Test Suite":
            required_linux = conclusion == "success"
        if conclusion not in ("success", "skipped") or (name == "Test Suite" and not required_linux):
            raise PointFailure("ci collect", f"rerun {name if isinstance(name, str) else 'job'} on {tip}: CI ran on {ran_on} ({conclusion or 'unfinished'})")
    if not required_linux or view.get("conclusion") != "success":
        raise PointFailure("ci collect", f"rerun {'Test Suite' if not required_linux else 'CI'} on {tip}: CI ran on {ran_on} ({view.get('conclusion') or 'unfinished'})")
    write_json(state_dir / "ci_points.json", {"count": count, "tip": tip})
    report("ci collect", "ok", f"run {run.run_id} green on {tip}; {count} code merges recorded")
    print(f"--ci-green {tip}")


def review_watch(production: Production, state_dir: Path) -> WatchFirstAlert | WatchRepeat | WatchBelowThreshold:
    review_script = Path(os.environ.get("CI_POINTS_REVIEW_REGIME", os.environ.get(
        "DAILIES_REVIEW_REGIME", str(Path(__file__).with_name("review_regime.py")))))
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "review_watch.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        watch_result = command("watch", sys.executable, str(review_script), "watch")
        if watch_result.returncode not in (0, 3):
            raise PointFailure("watch", watch_result.stderr.strip() or watch_result.stdout.strip())
        line = watch_result.stdout.strip()
        if watch_result.returncode != 3:
            return WatchBelowThreshold(line)
        record = state_dir / "review_watch.json"
        if record.exists():
            return WatchRepeat(line)
        table = required(command("watch", sys.executable, str(review_script), "report", "--since", "2026-09-28"), "watch")
        notice = f"{line}; the table is in this session"
        push = Path.home() / ".claude/scripts/notify/pushover.py"
        _ = required(command("watch", sys.executable, str(push), "--priority", "1", "Hana: review watch", notice), "watch")
        moment = datetime.now(production.zone).strftime("%H:%M %Z")
        production.log.parent.mkdir(parents=True, exist_ok=True)
        with production.log.open("a", encoding="utf-8") as log:
            _ = log.write(f"- {moment}: review watch: {line}\n")
        write_json(record, {"alerted": datetime.now(production.zone).isoformat(timespec="seconds"), "line": line})
        return WatchFirstAlert(line, table)


def gates(production: Production, gate_id: str) -> tuple[str, str, str]:
    lines = production.doc.read_text(encoding="utf-8").splitlines()
    if "## Gates" not in lines:
        raise PointFailure("notice", "production doc has no Gates table")
    start = lines.index("## Gates") + 1
    for line in lines[start:]:
        if line.startswith("## "):
            break
        if not line.startswith("|"):
            continue
        cells = [cell_value(cell.strip()) for cell in line.strip("|").split("|")]
        if len(cells) < 3 or cells[0] != gate_id:
            continue
        waiting = re.fullmatch(r"(.+?) phase (\S+)", cells[1], re.IGNORECASE)
        target = re.fullmatch(r"(.+?) phase (\S+)", cells[2], re.IGNORECASE)
        if waiting is None or target is None:
            raise PointFailure("notice", f"invalid gate {gate_id}")
        return waiting.group(1), target.group(1), target.group(2)
    raise PointFailure("notice", f"gate {gate_id} not found")


def notice(production: Production, gate_id: str, kind: str, log: Path | None) -> None:
    waiting, target, phase = gates(production, gate_id)
    if kind == "lift":
        if log is None:
            raise PointFailure("notice lift", "--log is required")
        message = f"From the showrunner: {gate_id} lifted — your tests pass without {target} phase {phase} ({log}). Continue."
    else:
        history = merge_branch_history(production.checkout, production.merge_branch)
        landed = next((entry for entry in history.entries if entry.unit == target and entry.phase == phase
                       and isinstance(entry.kind, CodeCheckpoint)), None)
        if not isinstance(landed, MergeEntry):
            raise PointFailure("notice clear", f"{target} phase {phase} has no code merge on {production.merge_branch}")
        message = (f"From the showrunner: {gate_id} clear — {target} phase {phase} is on "
                   f"{production.merge_branch} as {landed.merge_hash}. Merge {production.merge_branch} and continue.")
    report(f"notice {kind}", "ok", f"send {waiting}: {message}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    due_parser = commands.add_parser("due")
    ci_parser = commands.add_parser("ci")
    ci_commands = ci_parser.add_subparsers(dest="ci_action", required=True)
    start_parser = ci_commands.add_parser("start")
    _ = start_parser.add_argument("--cancel-prior", action="store_true")
    collect_parser = ci_commands.add_parser("collect")
    watch_parser = commands.add_parser("watch")
    notice_parser = commands.add_parser("notice")
    notice_commands = notice_parser.add_subparsers(dest="notice_action", required=True)
    clear_parser = notice_commands.add_parser("clear")
    lift_parser = notice_commands.add_parser("lift")
    for subparser in (due_parser, start_parser, collect_parser, watch_parser, clear_parser, lift_parser):
        _ = subparser.add_argument("--production", required=True, type=Path)
    for subparser in (due_parser, start_parser, collect_parser, watch_parser):
        _ = subparser.add_argument("--state-dir", required=True, type=Path)
    for subparser in (due_parser, start_parser, collect_parser):
        _ = subparser.add_argument("--no-ci", action="store_true")
    for subparser in (clear_parser, lift_parser):
        _ = subparser.add_argument("gate_id")
    _ = lift_parser.add_argument("--log", type=Path)
    args = parser.parse_args()
    action = cast(str, args.action)
    step = action + (f" {cast(str, args.ci_action)}" if action == "ci" else
                     f" {cast(str, args.notice_action)}" if action == "notice" else "")
    try:
        production = read_production(cast(Path, args.production))
        if action == "due":
            state = due(production, cast(Path, args.state_dir), cast(bool, args.no_ci))
            if isinstance(state, CiNotUsed):
                report("due", "ok", "skipped: no CI")
            elif isinstance(state, CiDue):
                report("due", "ok", "CI point due")
            else:
                report("due", "ok", f"{state.merges_since} of 5 code merges since last CI point")
        elif action == "ci":
            if cast(str, args.ci_action) == "start":
                ci_start(production, cast(Path, args.state_dir), cast(bool, args.no_ci),
                         cast(bool, args.cancel_prior))
            else:
                ci_collect(production, cast(Path, args.state_dir), cast(bool, args.no_ci))
        elif action == "watch":
            state = review_watch(production, cast(Path, args.state_dir))
            detail = f"repeat: {state.line}" if isinstance(state, WatchRepeat) else state.line
            report("watch", "ok", detail or "no open review watch")
            if isinstance(state, WatchFirstAlert):
                print(state.table)
        else:
            notice(production, cast(str, args.gate_id), cast(str, args.notice_action),
                   cast(Path | None, getattr(args, "log", None)))
        return 0
    except (PointFailure, Refusal, OSError, ValueError, json.JSONDecodeError) as error:
        report(error.step if isinstance(error, PointFailure) else step, "failed", str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
