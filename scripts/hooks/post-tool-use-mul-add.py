#!/usr/bin/env python3
"""Tell Claude when an applied Rust edit leaves a provable float multiply-add."""

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


def _log_block(tool: str, cwd: str, file: str, findings: list[dict[str, str | int]]) -> None:
    import json
    import os
    from datetime import datetime, timezone

    state = os.environ.get("MUL_ADD_HOOK_STATE") or os.path.join(
        os.path.expanduser("~"), ".local", "state", "mul-add-hook"
    )
    try:
        os.makedirs(state, mode=0o700, exist_ok=True)
        with open(os.path.join(state, "blocks.jsonl"), "a", encoding="utf-8") as output:
            for finding in findings:
                record = {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "agent": "claude", "tool": tool, "cwd": cwd, "file": file,
                    "line": finding["line"], "expression": finding["expression"],
                }
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
        with open(file, encoding="utf-8") as edited:
            source = edited.read()
        if "*" not in source or ("+" not in source and "-" not in source):
            return
        from mul_add_lib import float_mul_add_findings

        _, findings = float_mul_add_findings(file)
        if not findings:
            return
        try:
            shown = os.path.relpath(file, root) if os.path.commonpath((root, file)) == root else file
        except ValueError:
            shown = file
        descriptions = [
            f"{shown}:{finding.line}: write {finding.rewrite} for {finding.expr} "
            + "(clippy::suboptimal_flops)"
            for finding in findings
        ]
        _log_block(
            tool, cwd, file,
            [{"line": finding.line, "expression": finding.expr} for finding in findings],
        )
        print(json.dumps({
            "decision": "block",
            "reason": "; ".join(descriptions) + " The edit was applied.",
            "continue": True,
            "systemMessage": f"mul_add: {os.path.basename(file)} has {len(findings)} float multiply-add expression(s)",
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "\n".join(descriptions),
            },
        }))
    except Exception as error:
        import json

        print(json.dumps({"systemMessage": f"mul_add hook error: {type(error).__name__}: {error}"}))


if __name__ == "__main__":
    main()
