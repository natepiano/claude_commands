#!/usr/bin/env python3
"""Tell an editing agent when a Rust function exceeds clippy's line limit."""

from __future__ import annotations

import sys

TYPE_CHECKING = False

if TYPE_CHECKING:
    from fn_length_lib import AppliedRustEdits


def _log_block(
    edit: AppliedRustEdits, file: str, threshold: int, functions: list[dict[str, str | int]]
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
            "agent": edit.agent,
            "tool": edit.tool,
            "cwd": edit.cwd,
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
        from fn_length_lib import long_functions

        descriptions: list[str] = []
        affected: list[tuple[str, int, int]] = []
        records: list[tuple[str, int, list[dict[str, str | int]]]] = []
        for file_path in edit.files:
            file = os.path.realpath(os.path.join(root, file_path))
            if not os.path.isfile(file):
                continue
            scope, functions = long_functions(file)
            if not functions:
                continue
            try:
                shown = os.path.relpath(file, root) if os.path.commonpath((root, file)) == root else file
            except ValueError:
                shown = file
            descriptions.extend(
                f"fn {function.name} at {shown}:{function.line} is {function.lines} lines "
                + f"(limit {scope.threshold})"
                for function in functions
            )
            affected.append((shown, len(functions), scope.threshold))
            records.append((
                file, scope.threshold,
                [{"name": function.name, "line": function.line, "lines": function.lines}
                 for function in functions],
            ))
        if not descriptions:
            return
        total = sum(count for _, count, _ in affected)
        if len(affected) == 1:
            file, _, threshold = affected[0]
            summary = f"{os.path.basename(file)} has {total} function(s) over {threshold} lines"
        elif len({threshold for _, _, threshold in affected}) == 1:
            threshold = affected[0][2]
            files = ", ".join(file for file, _, _ in affected)
            summary = f"{total} function(s) over {threshold} lines in {files}"
        else:
            limits = "; ".join(
                f"{count} over {threshold} lines in {file}"
                for file, count, threshold in affected
            )
            summary = f"{total} function(s) over limits: {limits}"
        for file, threshold, functions in records:
            _log_block(edit, file, threshold, functions)
        print(
            json.dumps(
                {
                    "decision": "block",
                    "reason": "; ".join(descriptions) + ": split it now. The edit was applied.",
                    "continue": True,
                    "systemMessage": f"fn-length: {summary}",
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
