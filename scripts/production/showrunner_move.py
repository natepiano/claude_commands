#!/usr/bin/env python3
"""Stop and resume local Claude sessions on a target account.

    showrunner_move.py session <session id|name> <claudeN> [--why <text>]
    showrunner_move.py units|showrunners <claudeN> [--dry-run] [--why <text>]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Literal, cast

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.accounts import accounts  # noqa: E402
from scripts.whoami import note_file  # noqa: E402


class CommandLine(argparse.Namespace):
    kind: Literal["session", "units", "showrunners"] = "session"
    rest: list[str] = []
    dry_run: bool = False
    why: str = ""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _ = parser.add_argument("kind", choices=("session", "units", "showrunners"))
    _ = parser.add_argument("rest", nargs="+")
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--why", default="")
    options = cast(CommandLine, parser.parse_args(argv))
    wanted_args = 2 if options.kind == "session" else 1
    if len(options.rest) != wanted_args:
        parser.error(f"{options.kind} takes " + ("<session> <target>" if wanted_args == 2 else "<target>"))
    target = note_file.canonical_label(options.rest[-1])
    arguments = ["move", options.rest[0] if options.kind == "session" else options.kind, target]
    if options.dry_run:
        arguments.append("--dry-run")
    if options.why:
        arguments.extend(("--why", options.why))
    return accounts.main(arguments)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
