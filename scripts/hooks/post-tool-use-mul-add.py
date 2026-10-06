#!/usr/bin/env python3
"""Tell an editing agent when Rust contains a provable float multiply-add."""

from __future__ import annotations

import sys

TYPE_CHECKING = False

if TYPE_CHECKING:
    from fn_length_lib import AppliedRustEdits


def _log_block(edit: AppliedRustEdits, file: str, findings: list[dict[str, str | int]]) -> None:
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
                    "agent": edit.agent, "tool": edit.tool, "cwd": edit.cwd, "file": file,
                    "line": finding["line"], "expression": finding["expression"],
                }
                _ = output.write(json.dumps(record) + "\n")
    except OSError:
        pass


def main() -> None:
    raw = sys.stdin.read()
    if ".rs" not in raw and "\\u" not in raw:
        return
    from fn_length_lib import IgnoredEdit, applied_rust_edits

    try:
        edit = applied_rust_edits(raw)
    except (ValueError, UnicodeError):
        return
    if isinstance(edit, IgnoredEdit):
        return
    try:
        import json
        import os

        root = os.path.realpath(edit.cwd)
        descriptions: list[str] = []
        affected: list[tuple[str, int]] = []
        records: list[tuple[str, list[dict[str, str | int]]]] = []
        for file_path in edit.files:
            file = os.path.realpath(os.path.join(root, file_path))
            if not os.path.isfile(file):
                continue
            with open(file, encoding="utf-8") as edited:
                source = edited.read()
            if "*" not in source or ("+" not in source and "-" not in source):
                continue
            from mul_add_lib import float_mul_add_findings

            _, findings = float_mul_add_findings(file)
            if not findings:
                continue
            try:
                shown = os.path.relpath(file, root) if os.path.commonpath((root, file)) == root else file
            except ValueError:
                shown = file
            descriptions.extend(
                f"{shown}:{finding.line}: write {finding.rewrite} for {finding.expr} "
                + "(clippy::suboptimal_flops)"
                for finding in findings
            )
            affected.append((shown, len(findings)))
            records.append((
                file,
                [{"line": finding.line, "expression": finding.expr} for finding in findings],
            ))
        if not descriptions:
            return
        total = len(descriptions)
        if len(affected) == 1:
            summary = f"{os.path.basename(affected[0][0])} has {total} float multiply-add expression(s)"
        else:
            files = ", ".join(file for file, _ in affected)
            summary = f"{total} float multiply-add expression(s) in {files}"
        for file, findings in records:
            _log_block(edit, file, findings)
        print(json.dumps({
            "decision": "block",
            "reason": "; ".join(descriptions) + " The edit was applied.",
            "continue": True,
            "systemMessage": f"mul_add: {summary}",
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
