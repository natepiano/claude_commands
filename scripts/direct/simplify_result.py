#!/usr/bin/env python3
"""Validate, save, reuse, and report a simplify review result."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib.atomic_file import write_text_atomic


def json_object(text: str) -> dict[str, object]:
    """Read a JSON object without accepting another JSON value."""
    value = cast(object, json.loads(text))
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def failure_reason(reason: str, report: str, log: str, exit_code: int, fallback: str = "") -> str:
    """Keep a stated reason before using the review evidence."""
    if reason:
        return reason
    if report.startswith("ERROR:"):
        return report.removeprefix("ERROR:").strip()
    errors = [line.removeprefix("ERROR:").strip() for line in log.splitlines() if line.startswith("ERROR:")]
    return "\n".join(errors) or fallback or f"the simplify review did not finish successfully (exit {exit_code})"


def send_failure_once(session: Path, phase: str, reason: str) -> None:
    """Let send.py apply its repeat window for the recipient and failure key."""
    from scripts.lib import conf
    from scripts.lib.process_failure import failure_detail

    settings_read = conf.read('direct')
    try:
        state = json_object((session / "progress_history_state.json").read_text())
        unit = state.get("unit") or session.name
        target = settings_read.text("SIMPLIFY_FAILURE_TARGET")
        repeat = settings_read.whole("SIMPLIFY_FAILURE_REPEAT_MINUTES")
        digest = Path(os.environ.get("UNIT_REVIEW_CACHE_FILE", "")).stem
        key = hashlib.sha256(json.dumps([digest, reason]).encode()).hexdigest()
        sender = os.environ.get("SIMPLIFY_FAILURE_SENDER") or str(Path(__file__).resolve().parents[1] / "message/send.py")
        prefix = "PROOF, no action: " if os.environ.get("UNIT_REVIEW_PROOF") == "1" else ""
        message = f"{prefix}URGENT: simplify review failed. Unit: {unit}. Phase: {phase}. Reason: {reason}"
        delivered = subprocess.run([
            sys.executable, sender, "--to", target, "--from", "simplify review",
            "--summary", prefix + "URGENT: simplify review failed", "--key", f"simplify-{key}",
            "--repeat-minutes", str(repeat), "--text", message,
        ], capture_output=True, text=True, check=False)
        if delivered.returncode != 0:
            print("simplify: unable to send failure message: " + failure_detail(delivered), file=sys.stderr)
    except (OSError, ValueError) as error:
        print(f"simplify: unable to send failure message: {error}", file=sys.stderr)


def validate(report: str, log: str, agent_count: int) -> str:
    """Return the reason when the report or agent completion is invalid."""
    if not report.strip():
        return failure_reason("", report, log, 1, "Claude returned an empty simplify report")
    if report.startswith("ERROR:"):
        return failure_reason("", report, log, 1)
    stats: dict[str, object] = {}
    for line in log.splitlines():
        try:
            event = json_object(line)
        except ValueError:
            continue
        if event.get("type") == "result" and isinstance(event.get("subagent_stats"), dict):
            stats = cast(dict[str, object], event["subagent_stats"])
    if stats and (stats.get("completed") != agent_count or stats.get("failed") != 0):
        return f"the {agent_count} simplify agents did not all complete successfully"
    stripped = report.strip()
    if stripped.splitlines()[:1] == ["Pass"] or re.match(r"^1[.)]\s", stripped):
        return ""
    return "the simplify report must start with Pass or item 1"


def publish_summary(findings: Path, report: str, action: str) -> None:
    """Copy the summary and print the caller's result line."""
    summary = os.environ.get("UNIT_REVIEW_SUMMARY_FILE", "")
    if summary:
        _ = Path(summary).write_text(report)
    print(f"simplify: {action}; summary {findings}")


def reuse(directory: Path) -> bool:
    """Restore a saved successful review."""
    saved = os.environ.get("UNIT_REVIEW_CACHE_FILE", "")
    if not saved:
        return False
    cache = Path(saved)
    try:
        record = json_object(cache.read_text())
    except (OSError, ValueError):
        record = {}
    report = record.get("summary")
    if record.get("status") != "reviewed" or not isinstance(report, str):
        cache.unlink(missing_ok=True)
        return False
    findings = directory / "review_findings_1_simplify.txt"
    _ = findings.write_text(report)
    _ = (directory / "review_status_simplify").write_text("reviewed\n")
    publish_summary(findings, report, "reused")
    return True


def finish(directory: Path, session: Path, phase: str, reason: str, exit_code: int) -> None:
    """Save a finished review or report its failure through the shared sender."""
    status = directory / "review_status_simplify"
    findings = directory / "review_findings_1_simplify.txt"
    report = findings.read_text() if findings.exists() else ""
    previous = status.read_text().strip() if status.exists() else "error"
    action = "reviewed" if exit_code == 0 and previous == "reviewed" else "error"
    saved = os.environ.get("UNIT_REVIEW_CACHE_FILE", "")
    if action == "reviewed" and saved:
        try:
            write_text_atomic(Path(saved), json.dumps({"status": action, "summary": report}) + "\n")
        except OSError as error:
            reason = f"unable to publish the review cache: {error}"
            action = "error"
    _ = status.write_text(action + "\n")
    if action == "error":
        log_path = directory / "review_agent_1_simplify.log"
        log = log_path.read_text(errors="replace") if log_path.exists() else ""
        reason = failure_reason(reason, report, log, exit_code)
        print(f"simplify: error: {reason}", file=sys.stderr)
        send_failure_once(session, phase, reason)
    publish_summary(findings, report, action)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("reuse", "finish"):
        command = commands.add_parser(name)
        _ = command.add_argument("--phase-dir", type=Path, required=True)
        if name == "finish":
            _ = command.add_argument("--session-dir", type=Path, required=True)
            _ = command.add_argument("--phase", required=True)
            _ = command.add_argument("--reason", default="")
            _ = command.add_argument("--exit-code", type=int, required=True)
    check = commands.add_parser("check")
    _ = check.add_argument("--report-file", type=Path, required=True)
    _ = check.add_argument("--log-file", type=Path, required=True)
    args = parser.parse_args()
    command_name = cast(str, args.command)
    if command_name == "reuse":
        return 0 if reuse(cast(Path, args.phase_dir)) else 1
    if command_name == "finish":
        finish(cast(Path, args.phase_dir), cast(Path, args.session_dir), cast(str, args.phase),
               cast(str, args.reason), cast(int, args.exit_code))
        return 0
    from scripts.lib import conf

    report_path = cast(Path, args.report_file)
    log_path = cast(Path, args.log_file)
    report = report_path.read_text() if report_path.exists() else ""
    log = log_path.read_text(errors="replace") if log_path.exists() else ""
    reason = validate(report, log, conf.read("direct").whole("SIMPLIFY_AGENT_COUNT"))
    if reason:
        if not report.strip() or report.strip().splitlines()[:1] == ["Pass"] or re.match(r"^1[.)]\s", report.strip()):
            _ = report_path.write_text(f"ERROR: {reason}")
        print(reason)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
