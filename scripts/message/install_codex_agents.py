#!/usr/bin/env python3
"""Install the repository's messaging instructions in Codex AGENTS.md.

commands/message.md carries the same words as codex/AGENTS-messaging.md.
After editing either, run this installer once here and once with --machine mac.
"""

from __future__ import annotations

import argparse
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from typing import cast
from uuid import uuid4

BEGIN_MARKER = b"<!-- claude-messaging: begin -->"
END_MARKER = b"<!-- claude-messaging: end -->"


def install_agents(agents_file: Path, text: bytes) -> bool:
    """Replace only the marked block, preserving every byte outside it."""
    try:
        previous = agents_file.read_bytes()
    except FileNotFoundError:
        previous = b""
    block = BEGIN_MARKER + b"\n" + text
    if not block.endswith(b"\n"):
        block += b"\n"
    block += END_MARKER
    if BEGIN_MARKER not in previous and END_MARKER not in previous:
        separator = b"\n" if previous and not previous.endswith(b"\n") else b""
        updated = previous + separator + block + b"\n"
    else:
        if previous.count(BEGIN_MARKER) != 1 or previous.count(END_MARKER) != 1:
            raise ValueError("AGENTS.md must have exactly one begin marker and one end marker")
        start = previous.index(BEGIN_MARKER)
        end = previous.index(END_MARKER)
        if end < start:
            raise ValueError("AGENTS.md messaging end marker precedes its begin marker")
        updated = previous[:start] + block + previous[end + len(END_MARKER):]
    if updated == previous:
        return False
    agents_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = agents_file.with_name(f".{agents_file.name}.{uuid4().hex}.tmp")
    output = temporary.open("xb")
    try:
        with output:
            _ = output.write(updated)
        try:
            permissions = stat.S_IMODE(agents_file.stat().st_mode)
        except FileNotFoundError:
            pass
        else:
            temporary.chmod(permissions)
        os.replace(temporary, agents_file)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def install_remote(host: str, text: bytes, arguments: list[str]) -> int:
    """Run the same installer on the host without needing a remote checkout."""
    source = Path(__file__).read_text(encoding="utf-8")
    command = ('"$HOME/.claude/scripts/lib/py" -c ' + shlex.quote(source)
               + " " + shlex.join(["--from-stdin", *arguments]))
    try:
        done = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
            input=text, capture_output=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"install_codex_agents.py: ssh {host}: {error}", file=sys.stderr)
        return 1
    _ = sys.stdout.buffer.write(done.stdout)
    _ = sys.stderr.buffer.write(done.stderr)
    return 0 if done.returncode == 0 else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--machine", help="install on this SSH host instead")
    _ = parser.add_argument("--agents-file", type=Path, help="target file (default: ~/.codex/AGENTS.md)")
    _ = parser.add_argument("--from-stdin", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    host = cast(str | None, options.machine)
    agents_file = cast(Path | None, options.agents_file)
    from_stdin = cast(bool, options.from_stdin)
    if host is not None and from_stdin:
        parser.error("--from-stdin cannot be used with --machine")
    try:
        text = (sys.stdin.buffer.read() if from_stdin else
                (Path(__file__).resolve().parents[2] / "codex" / "AGENTS-messaging.md").read_bytes())
        if host is not None:
            arguments = [] if agents_file is None else ["--agents-file", str(agents_file)]
            return install_remote(host, text, arguments)
        target = agents_file if agents_file is not None else Path.home() / ".codex" / "AGENTS.md"
        changed = install_agents(target, text)
    except (OSError, ValueError) as error:
        print(f"install_codex_agents.py: {error}", file=sys.stderr)
        return 1
    print(f"{'Installed' if changed else 'Unchanged'} messaging block in {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
