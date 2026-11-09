#!/usr/bin/env python3
"""Publish one configuration value after the user approves the change."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import os
from pathlib import Path
import platform
import shlex
import sys

from scripts.lib import conf, main_publish
from scripts.lib.conf import Unusable, check
from scripts.lib.atomic_file import write_text_atomic
from scripts.build_hold.override_guard import refuse_overrides

SUBJECT = "tune("
FETCH_LIMIT = main_publish.CommandTimeLimit(10)
PUSH_LIMIT = main_publish.CommandTimeLimit(10)
MAC_PULL_LIMIT = main_publish.CommandTimeLimit(20)
FORWARD_LIMIT = main_publish.CommandTimeLimit(55)


class RequestedValueChange(argparse.Namespace):
    file: str = ""
    key: str = ""
    value: str = ""
    why: str = ""
    user_words: str = ""


def _one_line(name: str, value: str) -> None:
    if not value.strip() or "\n" in value or "\r" in value:
        raise main_publish.Refusal(f"{name} must be one nonempty line")


def _replace_line(source: str, index: int, value: str) -> str:
    """Keep the selected line's key, spacing, comment, and line ending."""
    lines = source.splitlines(keepends=True)
    line = lines[index]
    body = line.rstrip("\r\n")
    ending = line[len(body):]
    key, _, right = body.partition("=")
    old, marker, comment = right.partition("#")
    leading = old[:len(old) - len(old.lstrip())]
    trailing = old[len(old.rstrip()):] if old.strip() else ""
    lines[index] = f"{key}={leading}{value}{trailing}{marker}{comment}{ending}"
    return "".join(lines)


def _forward(arguments: list[str], ssh_program: str) -> int:
    command = ('"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/lib/tune.py" '
               + shlex.join(arguments))
    done = main_publish.ssh(command, ssh_program=ssh_program, host="natedev", time_limit=FORWARD_LIMIT)
    if isinstance(done, str):
        print(f"tune: natedev: {done}", file=sys.stderr)
        return 1
    print(done.stdout, end="")
    print(done.stderr, end="", file=sys.stderr)
    return done.returncode


def _publish(args: RequestedValueChange, checkout: Path, *, already: bool) -> int:
    pushed, detail = main_publish.push(checkout, time_limit=PUSH_LIMIT)
    if not already or not pushed:
        print(f"main: {detail}")
    if not pushed:
        return 1
    outcome = main_publish.pull_mac(checkout, ssh_program=os.environ.get("ZONE_SSH") or "ssh",
                                    time_limit=MAC_PULL_LIMIT)
    if isinstance(outcome, main_publish.MacPulled):
        main_publish.clear_pull_only()
        if not already:
            print("mac: pulled")
    else:
        main_publish.queue_pull_only(f"{args.file}.{args.key}", outcome.reason)
        print(f"mac: queued ({outcome.reason}); the 15-minute sync sends it when the Mac wakes")
    return 0


def change(args: RequestedValueChange) -> int:
    source = conf.path(args.file).resolve()
    _ = conf.editable_line(args.file, args.key)
    check(args.file, args.key, args.value)
    checkout = main_publish.checkout_of(source)
    main_publish.ready(checkout, fetch_limit=FETCH_LIMIT)
    # Fetch can advance the file. Select the exact key again before the edit.
    index, old = conf.editable_line(args.file, args.key)
    check(args.file, args.key, args.value)
    dirty = main_publish.git(checkout, "diff", "HEAD", "--", str(source))
    if dirty.returncode:
        raise main_publish.Refusal(f"git diff: {main_publish.first_line(dirty)}")
    if dirty.stdout:
        raise main_publish.Refusal(f"{source} has uncommitted changes")
    if old == args.value:
        print(f"already {args.key}={args.value}")
        return _publish(args, checkout, already=True)
    before = source.read_bytes().decode("utf-8")
    write_text_atomic(source, _replace_line(before, index, args.value))
    message = (f"tune({args.file}): {args.key} {old} -> {args.value}\n\n"
               + f"{args.why}\n\nUser-Approved: {args.user_words}")
    _ = main_publish.commit(checkout, source, message, main_publish.ExistingFile(before),
                            subject_prefix=SUBJECT, unchanged="the value is unchanged")
    return _publish(args, checkout, already=False)


def main(arguments: list[str] | None = None) -> int:
    refuse_overrides(Path(__file__), "lib/tune.py")
    argv = sys.argv[1:] if arguments is None else arguments
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("file", "key", "value"):
        _ = parser.add_argument(name)
    _ = parser.add_argument("--why", required=True)
    _ = parser.add_argument("--user-words", required=True)
    try:
        args = parser.parse_args(argv, namespace=RequestedValueChange())
        _one_line("--why", args.why)
        _one_line("--user-words", args.user_words)
        _one_line("file", args.file)
        _one_line("key", args.key)
        if "\n" in args.value or "\r" in args.value:
            raise main_publish.Refusal("value must fit on one line")
        if "#" in args.value or args.value != args.value.strip():
            raise main_publish.Refusal("value must have no comment marker or outer whitespace")
        if platform.system() == "Darwin":
            return _forward(argv, os.environ.get("ZONE_SSH") or "ssh")
        return change(args)
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 2
    except (Unusable, main_publish.Refusal, OSError, UnicodeError) as error:
        print(f"tune: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
