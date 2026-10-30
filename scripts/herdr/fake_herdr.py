#!/usr/bin/env python3
"""Record argv, validate agent names and workspace ids, and replay responses without external tools.

FAKE_HERDR_RESPONSES maps space-joined argv (excluding the executable) to
{stdout, code} entries, optionally with stderr and write_files (path-to-text
report fixtures), require_paths and remove_paths (fixture lifecycle checks). The "default" entry is used for unmatched argv. A list of entries replays successive responses, keeping
the final response for subsequent calls.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import NotRequired, TypedDict, cast


class CommandResponse(TypedDict):
    stdout: str
    code: int
    stderr: NotRequired[str]
    write_files: NotRequired[dict[str, str]]
    require_paths: NotRequired[list[str]]
    remove_paths: NotRequired[list[str]]


def herdr_listener(proc: Path, pid: int, name: str, environment: Mapping[str, str]) -> None:
    """Give a fake server ownership of its session's listening socket."""
    fixture_home = Path(environment.get("HOME", str(Path.home())))
    config = Path(environment.get("XDG_CONFIG_HOME") or fixture_home / ".config") / "herdr"
    directory = config if name == "default" else config / "sessions" / name
    table = proc / "net/unix"
    table.parent.mkdir(parents=True, exist_ok=True)
    line = f"0: 2 0 00010000 0001 01 {pid} {directory}/herdr.sock\n"
    existing = table.read_text() if table.exists() else ""
    if line not in existing.splitlines(keepends=True):
        _ = table.write_text(existing + line)
    descriptors = proc / str(pid) / "fd"
    descriptors.mkdir(parents=True, exist_ok=True)
    listener = descriptors / "3"
    listener.unlink(missing_ok=True)
    listener.symlink_to(f"socket:[{pid}]")


def main(argv: list[str]) -> int:
    log = Path(os.environ["FAKE_HERDR_LOG"])
    previous = log.read_text().splitlines() if log.exists() else []
    with log.open("a") as output:
        _ = output.write(json.dumps(argv) + "\n")
    if argv[2:4] == ["agent", "start"] and (len(argv) < 5 or re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", argv[4]) is None):
        _ = sys.stderr.write(json.dumps({"error": {
            "code": "invalid_agent_name", "message": "invalid agent name",
        }}) + "\n")
        return 1
    for option, workspace in zip(argv, argv[1:]):
        if option == "--workspace" and re.fullmatch(r"w[0-9A-Z]+", workspace) is None:
            _ = sys.stderr.write(json.dumps({"error": {
                "code": "workspace_not_found", "message": f"workspace {workspace} not found",
            }, "id": "cli:tab:list"}) + "\n")
            return 1
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
    for required in response.get("require_paths", []):
        if not Path(required).exists():
            _ = sys.stderr.write(f"fixture required path missing: {required}\n")
            return 98
    for removed in response.get("remove_paths", []):
        path = Path(removed)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    for file, text in response.get("write_files", {}).items():
        _ = Path(file).write_text(text)
    _ = sys.stdout.write(response["stdout"])
    _ = sys.stderr.write(response.get("stderr", ""))
    return response["code"]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
