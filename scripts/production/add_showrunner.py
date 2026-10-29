#!/usr/bin/env python3
"""Start a lower production's showrunner in a named herdr session, attached to this showrunner's production.

Usage: add_showrunner.py --production <child production doc> --model <id> --effort <level>
                         [--resume <session id> --cwd <dir> [--account <label> | --config-dir <dir>]] [--check]

The parent showrunner runs it. The session is named for the child's slug and starts in the child's
checkout with `/showrunner:produce <doc>`, or `/showrunner:produce <doc> resume` with --resume. It
carries no unit mark. New and resumed sessions use the configured showrunners account. Once the child's showrunner runs, with an update
timer addressed to a live session, the child is attached to the caller's production. --check runs
every refusal and starts nothing. The session must start in a folder the selected account's login
already trusts, since a showrunner never answers Claude's folder-trust prompt.

Exit 0 when the child is attached, or --check found nothing in the way; 1 when the prompt was sent
but the showrunner did not appear or attach (the session is left running); 2 on a refusal;
3 (SESSION_NOT_READY) when the session is open but its opening prompt was not proven submitted.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import showrunners
from add_unit import Production, read_production
from production_units import Refusal
import herdr

if TYPE_CHECKING:
    from scripts.accounts.accounts import ClaudeLaunchAccount

WAIT_S = float(os.environ.get("ADD_SHOWRUNNER_WAIT_S") or 180)
# The session registry has no notification interface; callers may choose its retry interval.
POLL_S = float(os.environ.get("ADD_SHOWRUNNER_POLL_S") or 2.0)
SESSION_NOT_READY = 3


def running(slug: str) -> bool:
    """Whether the production's showrunner runs now: it has an update timer and that timer's session is live.

    A timer alone is not a running showrunner: it outlives a session that ended, which --resume restarts.
    Raises OSError when the session records cannot say.
    """
    return bool(showrunners.current_address(slug))


def claude_json(config_dir: Path) -> Path:
    """The configuration of the account this launch will use."""
    return Path(os.environ.get("ADD_SHOWRUNNER_CLAUDE_JSON") or config_dir / ".claude.json")


def checked(child: Production, caller: str, ending: str = "", start_in: Path | None = None, *, config_dir: Path) -> None:
    """Check whether `child` can start; raise Refusal naming what is in the way.

    `ending` names a session the caller ends before the start, so it is not in the way.
    `start_in` is the folder the session starts in, the child's checkout unless given.
    """
    from scripts.accounts.accounts import NamedAccount, account_dir, trust_command
    from scripts.herdr.herdr import Untrusted, UnreadableTrustFile, claude_trust

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
    folder = start_in or child.checkout
    label = str(config_dir)
    numbered = re.fullmatch(r"\.claude([0-9]+)", config_dir.name)
    if numbered and config_dir == account_dir(f"claude{numbered.group(1)}"):
        label = f"claude{numbered.group(1)}"
    selected = NamedAccount(label, config_dir)
    trust = claude_trust(folder, claude_json(config_dir))
    if isinstance(trust, UnreadableTrustFile):
        raise Refusal(f"{child.slug}: {trust}")
    if isinstance(trust, Untrusted):
        raise Refusal(f"{child.slug}: Claude is not trusted in {folder} on {label}. "
                      + f"Run: {trust_command(folder, selected)}")
    try:
        if child.slug != ending and herdr_tab_open(child.slug):
            raise Refusal(f"a tab named {child.slug} is already open in herdr session {herdr.shared_session()}")
    except herdr.HerdrFailure as error:
        raise Refusal(str(error)) from error


def herdr_tab_open(name: str) -> bool:
    """Whether the shared herdr session already holds a tab named `name`."""
    shared = herdr.shared_session()
    if not any(session["name"] == shared for session in herdr.live_sessions()):
        return False
    return any(pane.get("label") == name for pane in herdr.Herdr(shared).panes())


def account_directory(selected: ClaudeLaunchAccount) -> Path:
    return selected.dir


def account_for_launch(child: Production, resume: str | None, target: str = "", config_dir: str = "") -> ClaudeLaunchAccount:
    """Every launch uses the configured showrunners account, including resumes."""
    _ = child
    from scripts.accounts import accounts
    try:
        return accounts.account_for_group_launch("showrunner", resume=bool(resume),
                                                 target=target, config_dir=config_dir)
    except accounts.AccountsError as error:
        raise Refusal(str(error)) from error


def launch(child: Production, model: str, effort: str, resume: str | None, cwd: Path, selected: ClaudeLaunchAccount) -> herdr.PromptSubmitOutcome:
    """Start once and prove the opening prompt reached this showrunner's transcript."""
    from scripts.whoami.account import named_account_environment

    prompt = f"/showrunner:produce {child.doc}" + (" resume" if resume else "")
    arguments = ["--model", model, "--effort", effort]
    session_id = resume or str(uuid4())
    arguments.extend(["--resume" if resume else "--session-id", session_id])
    arguments.extend(["--remote-control", child.slug, "-n", child.slug, "--settings", '{"disableAgentView": true}'])
    environment = {"ENABLE_TOOL_SEARCH": "true"}
    environment.update(named_account_environment(selected.dir))
    host = herdr.shared_session()
    _ = herdr.start_server(host, 90_000)
    launched = herdr.launch_agent(host, showrunners.herdr_space(child.slug, str(child.doc)), child.slug, str(cwd),
        environment, kind="claude", agent=child.slug, agent_arguments=arguments,
        opening=herdr.OpeningPrompt(prompt, herdr.SessionTranscript(selected.dir, session_id)), timeout_ms=90_000)
    if isinstance(launched, herdr.nix_env.Refused):
        raise herdr.HerdrFailure(launched.reason)
    outcome = launched.outcome
    return (outcome if isinstance(outcome, (herdr.PromptSubmitted, herdr.PromptNotSubmitted))
            else herdr.PromptNotSubmitted("launcher supplied no submit proof"))


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
    parser = argparse.ArgumentParser(description="Start a lower production's showrunner in herdr and attach it.")
    _ = parser.add_argument("--production", required=True, type=Path)
    _ = parser.add_argument("--model", required=True, help="pass your own model id")
    _ = parser.add_argument("--effort", required=True, help="pass your own effort level")
    _ = parser.add_argument("--resume", help="the session id to resume; needs --cwd")
    _ = parser.add_argument("--account", default="", help="with --resume: retained or selected account")
    _ = parser.add_argument("--config-dir", default="", help="with --resume: retained config directory")
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
        selected = account_for_launch(child, resume, cast(str, args.account), cast(str, args.config_dir))
        checked(child, caller, start_in=cwd, config_dir=account_directory(selected))
        if resume:
            from scripts.message import sessions
            if any(item["sessionId"] == resume for item in sessions.live_sessions()):
                raise Refusal(f"session {resume} is still live")
    except (Refusal, OSError) as refusal:
        print(f"add_showrunner: {refusal}", file=sys.stderr)
        return 2
    where = cwd or child.checkout
    if cast(bool, args.check):
        print(f"check: ok — would start {child.slug} in {where} and attach it to {caller}")
        return 0
    try:
        outcome = launch(child, cast(str, args.model), cast(str, args.effort), resume, where, selected)
    except herdr.AgentNotReady as error:
        print(f"add_showrunner: the session is not ready:\n{error.screen}", file=sys.stderr)
        return SESSION_NOT_READY
    except (OSError, subprocess.SubprocessError, herdr.HerdrFailure) as error:
        said = cast(object, error.stderr) if isinstance(error, subprocess.CalledProcessError) else None
        detail = said.strip() if isinstance(said, str) and said.strip() else str(error)
        print(f"add_showrunner: the session did not start: {detail}", file=sys.stderr)
        return 2
    if isinstance(outcome, herdr.PromptNotSubmitted):
        print(f"add_showrunner: {child.slug}: opening prompt not submitted: {outcome.reason}", file=sys.stderr)
        return SESSION_NOT_READY
    attach = herdr.attach_hint(showrunners.herdr_space(child.slug, str(child.doc)), child.slug)
    print(f"started {child.slug}: {attach}")
    later = (f"Its session is still running: look at it with `{attach}`, then run "
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
