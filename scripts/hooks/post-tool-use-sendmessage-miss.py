#!/usr/bin/env python3
"""Point a failed native name lookup to send.py without sending or blocking."""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import cast


def context(text: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse", "additionalContext": text,
    }}))


def main() -> None:
    try:
        payload = cast(object, json.load(sys.stdin))
        if not isinstance(payload, dict):
            return
        fields = cast(dict[str, object], payload)
        if fields.get("tool_name") != "SendMessage":
            return
        supplied, response = fields.get("tool_input"), fields.get("tool_response")
        if not isinstance(supplied, dict):
            return
        name = cast(dict[str, object], supplied).get("to")
        if not isinstance(name, str) or not name or name.startswith("uds:"):
            return
        if isinstance(response, str):
            response = cast(object, json.loads(response))
        if not isinstance(response, dict):
            return
        result = cast(dict[str, object], response)
        message = result.get("message")
        if (result.get("success") is not False or not isinstance(message, str)
                or not re.match(rf"No agent named ['\"]{re.escape(name)}['\"] is reachable\.", message)):
            return

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "message"))
        import roster
        import send

        found = send.matching_recipients(name, send.LocalThenPeer(), roster.roster(os.environ, roster.ThisMachine()))
        if isinstance(found, (send.LocalRosterIncomplete, send.RecipientSearchFailed)):
            context("send.py roster error: " + "; ".join(found.problems))
            return
        lines: list[str] = []
        if found.entries:
            for row in found.entries:
                account = f", account {row.account.label}" if isinstance(row.account, roster.AccountLabel) else ""
                address = row.reach.address if isinstance(row.reach, roster.Messageable) else row.reach.reason
                lines.append(f"{row.name}: {row.kind} {row.role}{account}, {row.machine}, {address}.")
            target = shlex.quote(name)
            if len(found.entries) > 1:
                lines.append("Several sessions match; send to one listed address.")
                target = '"<one listed address>"'
            lines.append("Run with the sandbox off: python3 ~/.claude/scripts/message/send.py"
                         + f' --to {target} --from "<your session name>" --summary "<one line>" --file <message file>')
        else:
            lines.append(f"send.py's roster has no session named {name!r} on either machine either.")
        lines.extend("send.py roster error: " + problem for problem in found.problems)
        context("\n".join(lines))
    except Exception as error:
        context(f"send.py roster error: {type(error).__name__}: {' '.join(str(error).split())}")


if __name__ == "__main__":
    main()
