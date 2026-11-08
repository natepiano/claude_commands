#!/usr/bin/env python3
"""Check the documentation record for the selected checkpoint tree."""
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass
import json
import re
import sys



def decide(raw: str) -> str:
    try:
        from scripts.hooks.checkpoint_gate import decide as check
        return check(raw)
    except Exception as error:
        if re.search(r"\bgit\b.*\b(?:commit|merge)\b", raw) is None:
            return ""
        return f"Checkpoint doc gate failed: {type(error).__name__}: {error}. Repair the hook. Run ~/.claude/scripts/direct/verify.sh doc as the last command before the commit."


if __name__ == "__main__":
    raw = sys.stdin.read()
    try:
        reason = decide(raw) if any(word in raw for word in ("commit", "merge", "eval", "$", "`")) else ""
    except Exception:
        reason = ""
    if reason:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))
