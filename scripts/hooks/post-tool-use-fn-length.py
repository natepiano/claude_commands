#!/usr/bin/env python3
"""Tell Claude when an applied Rust edit leaves a function over clippy's limit."""

from __future__ import annotations

import sys


def _payload(raw: str) -> tuple[str, str, str]:
    import json
    from typing import cast

    data = cast(object, json.loads(raw))
    if not isinstance(data, dict):
        return "", "", ""
    fields = cast(dict[str, object], data)
    tool = fields.get("tool_name")
    cwd = fields.get("cwd")
    tool_input = fields.get("tool_input")
    if not isinstance(tool, str) or not isinstance(cwd, str) or not isinstance(tool_input, dict):
        return "", "", ""
    file_path = cast(dict[str, object], tool_input).get("file_path")
    return tool, cwd, file_path if isinstance(file_path, str) else ""


def _log_block(
    tool: str, cwd: str, file: str, threshold: int, functions: list[dict[str, str | int]]
) -> None:
    import json
    import os
    from datetime import datetime, timezone

    state = os.environ.get("FN_LENGTH_HOOK_STATE") or os.path.join(
        os.path.expanduser("~"), ".local", "state", "fn-length-hook"
    )
    try:
        os.makedirs(state, mode=0o700, exist_ok=True)
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "agent": "claude",
            "tool": tool,
            "cwd": cwd,
            "file": str(file),
            "threshold": threshold,
            "functions": functions,
        }
        with open(os.path.join(state, "blocks.jsonl"), "a", encoding="utf-8") as output:
            _ = output.write(json.dumps(record) + "\n")
    except OSError:
        pass


def main() -> None:
    raw = sys.stdin.read()
    if ".rs" not in raw and "\\u" not in raw:
        return
    try:
        tool, cwd, file_path = _payload(raw)
    except (ValueError, UnicodeError):
        return
    if tool not in {"Edit", "MultiEdit", "Write"} or not file_path.endswith(".rs"):
        return
    try:
        import json
        import os

        root = os.path.realpath(cwd)
        file = os.path.realpath(os.path.join(root, file_path))
        if not os.path.isfile(file):
            return

        from fn_length_lib import long_functions

        scope, functions = long_functions(file)
        if not functions:
            return
        try:
            shown = os.path.relpath(file, root) if os.path.commonpath((root, file)) == root else file
        except ValueError:
            shown = file
        descriptions = [
            f"fn {function.name} at {shown}:{function.line} is {function.lines} lines "
            + f"(limit {scope.threshold})"
            for function in functions
        ]
        _log_block(
            tool, cwd, file, scope.threshold,
            [{"name": function.name, "line": function.line, "lines": function.lines}
             for function in functions],
        )
        print(
            json.dumps(
                {
                    "decision": "block",
                    "reason": "; ".join(descriptions) + ": split it now. The edit was applied.",
                    "continue": True,
                    "systemMessage": (
                        f"fn-length: {os.path.basename(file)} has {len(functions)} "
                        f"function(s) over {scope.threshold} lines"
                    ),
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": (
                            "\n".join(descriptions)
                            + "\nSplit each into named helpers that each do one thing; "
                            + "the limit is clippy's too_many_lines, which fails verify.sh lint."
                        ),
                    },
                }
            )
        )
    except Exception as error:
        import json

        print(json.dumps({"systemMessage": f"fn-length hook error: {type(error).__name__}: {error}"}))


if __name__ == "__main__":
    main()
