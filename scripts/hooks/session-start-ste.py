#!/usr/bin/env python3
"""Load the shared STE rule into session and subagent context.

Read the rule from this checkout on each call. Missing or unreadable rules never
prevent a session from starting.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TypedDict, cast


class StartContext(TypedDict):
    hookEventName: str
    additionalContext: str


class StartOutput(TypedDict):
    hookSpecificOutput: StartContext


def main() -> int:
    try:
        payload = cast(object, json.loads(sys.stdin.read()))
        if not isinstance(payload, dict):
            return 0
        event = cast(dict[str, object], payload).get("hook_event_name")
        if not isinstance(event, str) or event not in {"SessionStart", "SubagentStart"}:
            return 0
        rule = Path(__file__).resolve().parents[2] / "config" / "simplified-technical-english.md"
        context = rule.read_text(encoding="utf-8")
    except (OSError, RecursionError, ValueError):
        return 0
    output: StartOutput = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
