#!/usr/bin/env python3
"""Record argv and replay configured responses without starting external tools.

FAKE_HERDR_RESPONSES maps space-joined argv (excluding the executable) to
{stdout, code} entries, optionally with stderr and write_files (path-to-text
report fixtures). The "default" entry is used for unmatched argv. A list of entries replays successive responses, keeping
the final response for subsequent calls.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import NotRequired, TypedDict, cast


class CommandResponse(TypedDict):
    stdout: str
    code: int
    stderr: NotRequired[str]
    write_files: NotRequired[dict[str, str]]


def main(argv: list[str]) -> int:
    log = Path(os.environ["FAKE_HERDR_LOG"])
    previous = log.read_text().splitlines() if log.exists() else []
    with log.open("a") as output:
        _ = output.write(json.dumps(argv) + "\n")
    responses = cast(
        dict[str, CommandResponse | list[CommandResponse]],
        json.loads(Path(os.environ["FAKE_HERDR_RESPONSES"]).read_text()),
    )
    response = responses.get(
        " ".join(argv), responses.get("default", {"stdout": "", "code": 0})
    )
    if isinstance(response, list):
        seen = sum(cast(list[str], json.loads(line)) == argv for line in previous)
        response = response[min(seen, len(response) - 1)]
    for file, text in response.get("write_files", {}).items():
        _ = Path(file).write_text(text)
    _ = sys.stdout.write(response["stdout"])
    _ = sys.stderr.write(response.get("stderr", ""))
    return response["code"]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
