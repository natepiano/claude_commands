#!/usr/bin/env python3
"""Start a lower production's showrunner in a named herdr session, attached to this showrunner's production.

Usage: add_showrunner.py --production <child staff name> --model <id> --effort <level>
                         [--doc <path>] [--resume <session id> --cwd <dir> [--account <label> | --config-dir <dir>]] [--check]

The parent showrunner runs it. The session is named for the child's staff name and starts in the child's
checkout with `/showrunner:produce <doc>`, or `/showrunner:produce <doc> resume` with --resume. It
carries no unit mark. New and resumed sessions use the configured showrunners account. Once the child's showrunner runs, with an update
timer addressed to a live session, the child is attached to the caller's production. --check runs
every refusal and starts nothing.
The launch writes folder trust in the selected account before Claude starts.

Exit 0 when the child is attached, or --check found nothing in the way; 1 when the prompt was sent
but the showrunner did not appear or attach (the session is left running); 2 on a refusal;
3 (SESSION_NOT_READY) when the session is open but its opening prompt was not proven submitted.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass


import argparse
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from scripts.production import completion_receipt, staff
from scripts.shutdown.remote import local_machine
from scripts.production.staff_sources import ensure_staff_showrunner as ensure_staff_showrunner

from scripts.production import showrunners
from scripts.production import showrunner_tree
from scripts.production.production_doc import ProductionDocument, Refusal, read_new_production, read_production
from scripts.herdr import herdr

if TYPE_CHECKING:
    from scripts.accounts.accounts import ClaudeLaunchAccount

WAIT_S = float(os.environ.get("ADD_SHOWRUNNER_WAIT_S") or 180)
# The session registry has no notification interface; callers may choose its retry interval.
POLL_S = float(os.environ.get("ADD_SHOWRUNNER_POLL_S") or 2.0)
SESSION_NOT_READY = 3


@dataclass(frozen=True)
class EndingUnitTab:
    """The unit's tab that closes before the child's showrunner starts."""

    pane_id: str


@dataclass(frozen=True)
class NoEndingTab:
    """Every open tab remains in place when the child's showrunner starts."""


EndingTab = EndingUnitTab | NoEndingTab
NO_ENDING_TAB = NoEndingTab()


@dataclass(frozen=True)
class ShowrunnerLaunch:
    session_id: str
    config_dir: Path
    started_at: datetime

    @classmethod
    def from_receipt(cls, fields: dict[str, object]) -> ShowrunnerLaunch:
        session_id, config_dir, started_at = (fields.get(key) for key in ("session_id", "config_dir", "started_at"))
        if not all(isinstance(value, str) and value for value in (session_id, config_dir, started_at)):
            raise Refusal("showrunner receipt has no launch identity")
        assert isinstance(session_id, str) and isinstance(config_dir, str) and isinstance(started_at, str)
        return cls(session_id, Path(config_dir), datetime.fromisoformat(started_at))


@dataclass(frozen=True)
class NewShowrunnerLaunch:
    pass


NEW_SHOWRUNNER_LAUNCH = NewShowrunnerLaunch()


def launch_place(identity: ShowrunnerLaunch) -> herdr.HerdrPlace | herdr.NotInHerdr:
    from scripts.message import sessions

    observed = sessions.session_liveness(identity.session_id)
    if isinstance(observed, sessions.SessionLivenessUnknown):
        raise Refusal(f"cannot locate session {identity.session_id}: {observed.reason}")
    if isinstance(observed, sessions.SessionNotLive):
        return herdr.NotInHerdr()
    place = herdr.herdr_place_of(observed.pid)
    if isinstance(place, herdr.NotInHerdr):
        raise Refusal(f"session {identity.session_id} runs outside herdr")
    return place


def running(production_name: str) -> bool:
    """Whether the production's showrunner runs now: it has an update timer and that timer's session is live.

    A timer alone is not a running showrunner: it outlives a session that ended, which --resume restarts.
    Raises OSError when the session records cannot say.
    """
    return bool(showrunners.current_address(production_name))


def checked(child: ProductionDocument, caller: str, ending: EndingTab = NO_ENDING_TAB, start_in: Path | None = None, *, config_dir: Path) -> None:
    """Check whether `child` can start; raise Refusal naming what is in the way.

    `ending` identifies the unit's tab that closes before launch, or says no tab ends.
    """
    del start_in, config_dir

    if not caller:
        raise Refusal(f"this session runs no production, so it has nothing to attach {child.name} to")
    if child.name == caller:
        raise Refusal(f"{child.name} is this session's own production")
    if caller in showrunner_tree.descendants_of(child.name, record=staff.initial_snapshot()):
        raise Refusal(f"{caller} already reports to {child.name}, directly or through others")
    try:
        taken = running(child.name)
    except OSError as error:
        raise Refusal(str(error)) from error
    if taken:
        raise Refusal(f"{child.name} already has a running showrunner")
    try:
        if herdr_tab_open(child.name, ending):
            raise Refusal(f"a tab named {child.name} is already open in herdr session {herdr.shared_session()}")
    except herdr.HerdrFailure as error:
        raise Refusal(str(error)) from error


def herdr_tab_open(name: str, ending: EndingTab) -> bool:
    """Whether a tab named `name` remains in the shared herdr session after `ending`."""
    shared = herdr.shared_session()
    if not any(session["name"] == shared for session in herdr.live_sessions()):
        return False
    return any(pane.get("label") == name
               and (not isinstance(ending, EndingUnitTab) or pane["pane_id"] != ending.pane_id)
               for pane in herdr.Herdr(shared).panes())


def account_directory(selected: ClaudeLaunchAccount) -> Path:
    return selected.dir


def account_for_launch(child: ProductionDocument, resume: str | None, target: str = "", config_dir: str = "") -> ClaudeLaunchAccount:
    """Every launch uses the configured showrunners account, including resumes."""
    _ = child
    from scripts.accounts import accounts
    try:
        return accounts.account_for_group_launch("showrunner", resume=bool(resume),
                                                 target=target, config_dir=config_dir)
    except accounts.AccountsError as error:
        raise Refusal(str(error)) from error


def launch(child: ProductionDocument, model: str, effort: str, resume: str | None, cwd: Path, selected: ClaudeLaunchAccount,
           *, identity: ShowrunnerLaunch | NewShowrunnerLaunch = NEW_SHOWRUNNER_LAUNCH,
           before_prompt: Callable[[herdr.SessionTranscript], object] | None = None) -> herdr.PromptSubmitOutcome:
    """Start once and prove the opening prompt reached this showrunner's transcript."""
    from scripts.message import sessions
    from scripts.whoami.account import named_account_environment

    session_id = identity.session_id if isinstance(identity, ShowrunnerLaunch) else resume or ""
    transcript = herdr.SessionTranscript(selected.dir, session_id)
    resuming = bool(resume) or (isinstance(identity, ShowrunnerLaunch)
        and sessions.latest_transcript(transcript.session_id, transcript.config_dir / "projects") is not None)
    if not resuming:
        transcript = herdr.SessionTranscript(selected.dir, "")
    prompt = f"/showrunner:produce {child.doc}" + (" resume" if resuming else "")
    arguments = ["--model", model, "--effort", effort]
    if resuming:
        arguments.extend(["--resume", session_id])
    from scripts.shutdown.launch_names import LaunchNames

    name = LaunchNames.read().name(session_id, child.name)
    arguments.extend(["--remote-control", name, "-n", name, "--settings", '{"disableAgentView": true}'])
    environment = {"ENABLE_TOOL_SEARCH": "true"}
    environment.update(named_account_environment(selected.dir))
    host = herdr.shared_session()
    _ = herdr.start_server(host, 90_000)
    launched = herdr.launch_agent(host, child.name, name, str(cwd),
        environment, kind="claude", agent=name, agent_arguments=arguments,
        opening=herdr.OpeningPrompt(prompt, transcript,
            before_prompt if before_prompt is not None else
            lambda observed: ensure_staff_showrunner(child, runtime_id=observed.session_id)), timeout_ms=90_000)
    if isinstance(launched, herdr.nix_env.Refused):
        raise herdr.HerdrFailure(launched.reason)
    outcome = launched.outcome
    return (outcome if isinstance(outcome, (herdr.PromptSubmitted, herdr.PromptNotSubmitted))
            else herdr.PromptNotSubmitted("launcher supplied no submit proof"))


def showrunner_appeared(production_name: str) -> bool:
    """Wait for the started session to run the production.

    A session record being written as it is read makes one look unreadable, so an unreadable registry is
    tried again until the deadline (G1 live check, 2026-10-13: the wait gave up while the child opened).
    Raises OSError when the last look, at the deadline, still cannot say.
    """
    deadline = time.monotonic() + WAIT_S
    while True:
        unreadable: OSError | None = None
        try:
            if running(production_name):
                return True
        except OSError as error:
            unreadable = error
        if time.monotonic() >= deadline:
            if unreadable is not None:
                raise unreadable
            return False
        time.sleep(POLL_S)



def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Start a lower production's showrunner in herdr and attach it.")
    _ = parser.add_argument("--production", required=True)
    _ = parser.add_argument("--doc", type=Path, help="the production doc; needed only when the showrunner has no staff row yet")
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
    caller = showrunners.own_name()
    try:
        record = staff.initial_snapshot()
        name = cast(str, args.production)
        doc = cast(Path | None, args.doc)
        existing = any(row.kind == "showrunner" and (row.name == name or row.id == name) for row in record.rows)
        child = read_new_production(doc, name) if doc and not existing else read_production(name, record=record)
        where = cwd or child.checkout
        selected = account_for_launch(child, resume, cast(str, args.account), cast(str, args.config_dir))
        identity = ShowrunnerLaunch(resume or "", selected.dir, datetime.now(UTC))
        place: herdr.HerdrPlace | herdr.NotInHerdr = herdr.NotInHerdr()
        owner = record.by_doc(local_machine(), child.doc)
        saved: completion_receipt.OwnedReceipt | completion_receipt.UnpublishedReceipt | completion_receipt.NoReceipt = completion_receipt.NoReceipt()
        if owner is not None:
            receipt = showrunners.SHOWRUNNER_STATE_DIR / owner.id / "add_showrunner.json"
            saved = completion_receipt.read_launch_receipt(receipt, record)
        else:
            for receipt in sorted(showrunners.SHOWRUNNER_STATE_DIR.glob("*/add_showrunner.json")):
                candidate = completion_receipt.read_launch_receipt(receipt, record)
                if isinstance(candidate, completion_receipt.UnpublishedReceipt) and candidate.fields.get("doc") == str(child.doc):
                    if not isinstance(saved, completion_receipt.NoReceipt):
                        raise Refusal(f"{child.doc}: several unpublished launches")
                    saved = candidate
        if isinstance(saved, (completion_receipt.OwnedReceipt, completion_receipt.UnpublishedReceipt)) and not cast(bool, args.check):
            fields = saved.fields
            if ((isinstance(saved, completion_receipt.UnpublishedReceipt) or saved.owner.kind == "showrunner")
                    and fields.get("doc") == str(child.doc)
                    and fields.get("caller") == caller and fields.get("resume") == resume):
                identity = ShowrunnerLaunch.from_receipt(fields)
                if identity.config_dir != selected.dir:
                    raise Refusal(f"{child.name}: unfinished launch belongs to {identity.config_dir}")
                place = launch_place(identity)
                if isinstance(place, herdr.HerdrPlace):
                    observed = herdr.Herdr(place.session).native_session(place.pane, "claude", 90)
                    if observed != identity.session_id:
                        raise Refusal(f"{child.name}: pending backend {identity.session_id} differs from {observed}")
                if running(child.name):
                    _ = ensure_staff_showrunner(child, caller, runtime_id=identity.session_id)
                    print(f"{child.name} already started")
                    return showrunners.main(["attach", child.name, caller])
                if isinstance(place, herdr.HerdrPlace) and not any(
                    pane["pane_id"] == place.pane and pane.get("label") == child.name
                    for pane in herdr.Herdr(place.session).panes()
                ):
                    raise Refusal(f"session {identity.session_id} does not own {child.name}'s tab")
        ending = EndingUnitTab(place.pane) if isinstance(place, herdr.HerdrPlace) else NO_ENDING_TAB
        checked(child, caller, ending=ending, start_in=cwd, config_dir=account_directory(selected))
        if resume and isinstance(place, herdr.NotInHerdr):
            from scripts.message import sessions
            if any(item["sessionId"] == resume for item in sessions.live_sessions()):
                raise Refusal(f"session {resume} is still live")
    except (Refusal, OSError, herdr.HerdrFailure) as refusal:
        print(f"add_showrunner: {refusal}", file=sys.stderr)
        return 2
    if cast(bool, args.check):
        print(f"check: ok — would start {child.name} in {where} and attach it to {caller}")
        return 0
    def published(transcript: herdr.SessionTranscript) -> None:
        native = staff.native_id(transcript.session_id)
        existing = staff.initial_snapshot().by_current_session(native)
        if existing is None and isinstance(saved, completion_receipt.OwnedReceipt):
            retained = staff.by_id(saved.owner.id)
            if retained is not None:
                if retained.current_session != identity.session_id:
                    raise Refusal(f"{child.name}: unfinished launch owner changed")
                existing = staff.update_current_session(retained.current_session, native)
        owner = existing.id if existing is not None else native
        completion_receipt.write_receipt(showrunners.SHOWRUNNER_STATE_DIR / owner / "add_showrunner.json", owner, {"doc": str(child.doc), "caller": caller, "resume": resume,
                                  "session_id": transcript.session_id, "config_dir": str(identity.config_dir),
                                  "started_at": identity.started_at.isoformat()})
        _ = ensure_staff_showrunner(child, caller, runtime_id=native)
    try:
        if isinstance(place, herdr.HerdrPlace):
            prompt = f"/showrunner:produce {child.doc}" + (" resume" if resume else "")
            transcript = herdr.SessionTranscript(identity.config_dir, identity.session_id)
            outcome = herdr.submitted_prompt(transcript, prompt, identity.started_at)
            published(transcript)
            if not isinstance(outcome, herdr.PromptSubmitted):
                outcome = herdr.Herdr(place.session).submit_opening_prompt(
                    place.pane, herdr.OpeningPrompt(prompt, transcript), identity.started_at, 90_000)
        else:
            outcome = launch(child, cast(str, args.model), cast(str, args.effort), resume, where, selected,
                             identity=identity, before_prompt=published)
    except herdr.AgentNotReady as error:
        print(f"add_showrunner: the session is not ready:\n{error.screen}", file=sys.stderr)
        return SESSION_NOT_READY
    except (OSError, subprocess.SubprocessError, herdr.HerdrFailure) as error:
        said = cast(object, error.stderr) if isinstance(error, subprocess.CalledProcessError) else None
        detail = said.strip() if isinstance(said, str) and said.strip() else str(error)
        print(f"add_showrunner: the session did not start: {detail}", file=sys.stderr)
        return 2
    if isinstance(outcome, herdr.ClaudeTrustNotWritten):
        return SESSION_NOT_READY
    if isinstance(outcome, herdr.PromptNotSubmitted):
        print(f"add_showrunner: {child.name}: opening prompt not submitted: {outcome.reason}", file=sys.stderr)
        return SESSION_NOT_READY
    attach = herdr.attach_hint(child.name, child.name)
    print(f"started {child.name}: {attach}")
    later = (f"Its session is still running: look at it with `{attach}`, then run "
             + f"`showrunners.py attach {child.name} {caller}` once it has an update timer.")
    try:
        appeared = showrunner_appeared(child.name)
    except OSError as error:
        print(f"{child.name} is not attached: {error}. {later}")
        return 1
    if not appeared:
        print(f"{child.name} has no running showrunner after {WAIT_S:g} s, so it is not attached. {later}")
        return 1
    # A resumed child keeps the parent recorded for its stable staff id.
    if showrunner_tree.parent_of(child.name) == caller:
        print(f"{child.name} already reports to {caller}")
        return 0
    return showrunners.main(["attach", child.name, caller])


def main(argv: list[str]) -> int:
    def run() -> int:
        with staff.operation():
            return _main(argv)
    return staff.cli_result(run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
