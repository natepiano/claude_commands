#!/usr/bin/env python3
"""Extract a Claude print result and retain execution errors."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import cast


@dataclass(frozen=True)
class ClaudeResult:
    """Keep the final answer, execution errors, and agent completion evidence."""

    report: str
    reason: str
    stats: dict[str, object]


def json_object(text: str) -> dict[str, object]:
    """Read a JSON object without accepting another JSON value."""
    value = cast(object, json.loads(text))
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def claude_result(log: str) -> ClaudeResult:
    """Read result events once and retain errors across later empty events."""
    report = ""
    errors: list[str] = []
    stats: dict[str, object] = {}
    for line in log.splitlines():
        if not re.search(r'"type"\s*:\s*"result"', line):
            continue
        try:
            event = json_object(line)
        except ValueError:
            continue
        if event.get("type") != "result":
            continue
        reasons = event.get("errors")
        if isinstance(reasons, list):
            errors.extend(reason for reason in cast(list[object], reasons) if isinstance(reason, str) and reason.strip())
        if event.get("is_error"):
            reason = event.get("result") or event.get("api_error") or event.get("subtype") or "Claude returned an error"
            errors.append(str(reason))
        if isinstance(event.get("result"), str):
            report = cast(str, event["result"])
        if isinstance(event.get("subagent_stats"), dict):
            stats = cast(dict[str, object], event["subagent_stats"])
    return ClaudeResult(report, "; ".join(dict.fromkeys(errors)), stats)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("log", type=Path)
    _ = parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        log = cast(Path, args.log).read_text(encoding="utf-8", errors="replace")
    except OSError:
        log = ""
    result = claude_result(log)
    reason = result.reason
    report = "ERROR: " + reason if reason else result.report
    try:
        _ = cast(Path, args.output).write_text(report, encoding="utf-8")
    except OSError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1 if reason else 2
    return 1 if reason else 0


if __name__ == "__main__":
    raise SystemExit(main())
