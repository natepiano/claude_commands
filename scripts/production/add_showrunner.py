#!/usr/bin/env python3
"""Start a lower production's showrunner in a detached tmux session, attached to this showrunner's production.

Usage: add_showrunner.py --production <child production doc> --model <id> --effort <level>
                         [--resume <session id> --cwd <dir>] [--check]

The parent showrunner runs it. The session is named for the child's slug and starts in the child's
checkout with `/showrunner:produce <doc>`, or `/showrunner:produce <doc> resume` with --resume. It
carries no unit mark and no account prefix, and its environment holds no CLAUDE_* variable, so at
any depth it takes the showrunner account's login. Once the child's showrunner runs, with an update
timer addressed to a live session, the child is attached to the caller's production. --check runs
every refusal and starts nothing. The session must start in a folder the showrunner account's login
already trusts, since a showrunner never answers Claude's folder-trust prompt.

Exit 0 when the child is attached, or --check found nothing in the way; 1 when the session started
and no showrunner appeared in WAIT_S seconds (the session is left running); 2 on a refusal.
"""
from __future__ import annotations

import argparse
import json
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


def claude_json() -> Path:
    """The showrunner account's global config: no CLAUDE_CONFIG_DIR, so the login's `~/.claude.json`."""
    return Path(os.environ.get("ADD_SHOWRUNNER_CLAUDE_JSON") or Path.home() / ".claude.json")


def _project_keys(folder: Path) -> set[str]:
    """The paths Claude may keep the folder's trust under: the folder, its repository root, and for a
    worktree its main checkout, whose trust it shares."""
    keys = {str(folder)}
    for flag in ("--show-toplevel", "--git-common-dir"):
        found = subprocess.run(["git", "-C", str(folder), "rev-parse", "--path-format=absolute", flag],
                               capture_output=True, text=True, check=False)
        if found.returncode == 0 and found.stdout.strip():
            path = Path(found.stdout.strip())
            keys.add(str(path.parent if flag == "--git-common-dir" and path.name == ".git" else path))
    return keys


def trusted(folder: Path) -> bool:
    """Whether Claude's folder-trust prompt was accepted for `folder` on the showrunner account.

    Only `projects[<path>].hasTrustDialogAccepted` is read: the file also holds account details, and
    no message ever carries any of it. A trusted parent folder does not cover a repository below it
    (G1 live check, 2026-10-13: the child sat on the prompt). Raises Refusal when the file cannot be read.
    """
    path = claude_json()
    try:
        config = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError) as error:
        raise Refusal(f"cannot read {path} to see whether {folder} is trusted ({type(error).__name__})") from error
    projects = cast(dict[str, object], config).get("projects") if isinstance(config, dict) else None
    if not isinstance(projects, dict):
        return False
    entries = cast(dict[str, object], projects)
    for key in _project_keys(folder.resolve()):
        entry = entries.get(key)
        if isinstance(entry, dict) and cast(dict[str, object], entry).get("hasTrustDialogAccepted") is True:
            return True
    return False


def checked(child: Production, caller: str, ending: str = "", start_in: Path | None = None) -> str:
    """The tmux to start `child`'s showrunner with. Raises Refusal naming what is in the way.

    `ending` names a tmux session the caller ends before the start, so it is not in the way.
    `start_in` is the folder the session starts in, the child's checkout unless given.
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
    folder = start_in or child.checkout
    if not trusted(folder):
        raise Refusal(f"Claude is not trusted in {folder} on the showrunner account. The user trusts it first, "
                      + "by running `claude` there once and accepting: a showrunner never answers a trust prompt")
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
    """Wait for the started session to run the production.

    A session record being written as it is read makes one look unreadable, so an unreadable registry is
    tried again until the deadline (G1 live check, 2026-10-13: the wait gave up while the child opened).
    Raises OSError when the last look, at the deadline, still cannot say.
    """
    deadline = time.monotonic() + WAIT_S
    while True:
        unreadable: OSError | None = None
        try:
            if running(slug):
                return True
        except OSError as error:
            unreadable = error
        if time.monotonic() >= deadline:
            if unreadable is not None:
                raise unreadable
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
        tmux = checked(child, caller, start_in=cwd)
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
