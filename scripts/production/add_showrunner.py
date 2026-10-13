#!/usr/bin/env python3
"""Start a lower production's showrunner in a detached tmux session, attached to this showrunner's production.

Usage: add_showrunner.py --production <child production doc> --model <id> --effort <level>
                         [--resume <session id> --cwd <dir>] [--check]

The parent showrunner runs it. The session is named for the child's slug and starts in the child's
checkout with `/showrunner:produce <doc>`, or `/showrunner:produce <doc> resume` with --resume. It
carries no unit mark and no account prefix, and its environment holds no CLAUDE_* variable, so at
any depth it takes the showrunner account's login. Once the child's showrunner runs, with an update
timer addressed to a live session, the child is attached to the caller's production. --check runs
every refusal and starts nothing.

Exit 0 when the child is attached, or --check found nothing in the way; 1 when the session started
and no showrunner appeared in WAIT_S seconds (the session is left running); 2 on a refusal.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import showrunners
import unit_lookup
from add_unit import Production, Refusal, launch_environment, read_production, tmux_live

WAIT_S = float(os.environ.get("ADD_SHOWRUNNER_WAIT_S") or 180)
POLL_S = 2.0


def running(slug: str) -> bool:
    """Whether the production's showrunner runs now: it has an update timer and that timer's session is live.

    A timer alone is not a running showrunner: it outlives a session that ended, which --resume restarts.
    Raises OSError when the session records cannot say.
    """
    return bool(showrunners.current_address(slug))


def checked(child: Production, caller: str, ending: str = "") -> str:
    """The tmux to start `child`'s showrunner with. Raises Refusal naming what is in the way.

    `ending` names a tmux session the caller ends before the start, so it is not in the way.
    """
    if not caller:
        raise Refusal(f"this session runs no production, so it has nothing to attach {child.slug} to")
    if child.slug == caller:
        raise Refusal(f"{child.slug} is this session's own production")
    if caller in showrunners.descendants_of(child.slug):
        raise Refusal(f"{caller} already reports to {child.slug}, directly or through others")
    try:
        taken = running(child.slug)
    except OSError as error:
        raise Refusal(str(error)) from error
    if taken:
        raise Refusal(f"{child.slug} already has a running showrunner")
    try:
        tmux = unit_lookup.tmux_binary()
    except (OSError, subprocess.CalledProcessError) as error:
        raise Refusal(f"no tmux to start the session in: {error}") from error
    if child.slug != ending and tmux_live(tmux, child.slug):
        raise Refusal(f"a tmux session named {child.slug} is already live")
    return tmux


def launch(child: Production, tmux: str, model: str, effort: str, resume: str | None, cwd: Path) -> None:
    """Start the session. No mark and no account prefix: it is a showrunner, on the showrunner account."""
    prompt = f"/showrunner:produce {child.doc}" + (" resume" if resume else "")
    argv = ["claude", "--model", model, "--effort", effort]
    if resume:
        argv.extend(["--resume", resume])
    argv.extend(["--remote-control", child.slug, "-n", child.slug, "--settings", '{"disableAgentView": true}', prompt])
    command = "ENABLE_TOOL_SEARCH=true command " + shlex.join(argv) + "; exec zsh"
    scope = f"--unit={re.sub(r'[^A-Za-z0-9_.-]', '-', child.slug)}-{int(time.time())}"
    _ = subprocess.run(["systemd-run", "--user", "--scope", scope, tmux, "new-session", "-d", "-s", child.slug,
                        "-c", str(cwd), "zsh", "-ic", command],
                       env=launch_environment(), text=True, capture_output=True, check=True)


def showrunner_appeared(slug: str) -> bool:
    """Wait for the started session to run the production. Raises OSError when the session records cannot say."""
    deadline = time.monotonic() + WAIT_S
    while True:
        if running(slug):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_S)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Start a lower production's showrunner in tmux and attach it.")
    _ = parser.add_argument("--production", required=True, type=Path)
    _ = parser.add_argument("--model", required=True, help="pass your own model id")
    _ = parser.add_argument("--effort", required=True, help="pass your own effort level")
    _ = parser.add_argument("--resume", help="the session id to resume; needs --cwd")
    _ = parser.add_argument("--cwd", type=Path, help="with --resume: the directory that session ran in")
    _ = parser.add_argument("--check", action="store_true", help="run every refusal and start nothing")
    args = parser.parse_args(argv)
    resume = cast(str | None, args.resume)
    cwd = cast(Path | None, args.cwd)
    if (resume is None) != (cwd is None):
        parser.error("--resume and --cwd go together")
    caller = showrunners.own_slug()
    try:
        child = read_production(cast(Path, args.production))
        tmux = checked(child, caller)
    except Refusal as refusal:
        print(f"add_showrunner: {refusal}", file=sys.stderr)
        return 2
    where = cwd or child.checkout
    if cast(bool, args.check):
        print(f"check: ok — would start {child.slug} in {where} and attach it to {caller}")
        return 0
    try:
        launch(child, tmux, cast(str, args.model), cast(str, args.effort), resume, where)
    except (OSError, subprocess.CalledProcessError) as error:
        said = cast(object, error.stderr) if isinstance(error, subprocess.CalledProcessError) else None
        detail = said.strip() if isinstance(said, str) and said.strip() else str(error)
        print(f"add_showrunner: the session did not start: {detail}", file=sys.stderr)
        return 2
    print(f"started {child.slug} in tmux session {child.slug}")
    later = (f"Its session is still running: look at it with `tmux attach -t {child.slug}`, then run "
             + f"`showrunners.py attach {child.slug} {caller}` once it has an update timer.")
    try:
        appeared = showrunner_appeared(child.slug)
    except OSError as error:
        print(f"{child.slug} is not attached: {error}. {later}")
        return 1
    if not appeared:
        print(f"{child.slug} has no running showrunner after {WAIT_S:g} s, so it is not attached. {later}")
        return 1
    # A resumed child kept the parent it had: an edge is stored, and counts again once both ends have a timer.
    if showrunners.parent_of(child.slug) == caller:
        print(f"{child.slug} already reports to {caller}")
        return 0
    return showrunners.main(["attach", child.slug, caller])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
