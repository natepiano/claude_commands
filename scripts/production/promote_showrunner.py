#!/usr/bin/env python3
"""Promote a running unit director to the showrunner of its own production, under this showrunner.

Usage: promote_showrunner.py <unit> --production <child production doc> --model <id> --effort <level> [--check]

The parent showrunner runs it, once the unit has written the child's production doc and committed it.
The unit gets `/exit`; once its Claude is gone its herdr tab is closed, and add_showrunner.py resumes
the same conversation as the child's showrunner: in a tab of the shared herdr session, with no unit mark, on the configured showrunners account,
attached to this showrunner's production. Its staff row becomes the child's showrunner row.
--check runs every refusal and stops nothing.

Exit 0 when the promoted session runs as the child's showrunner and is attached; 1 when the unit was
stopped and the promotion did not finish, with what to run printed; 2 on a refusal, with nothing stopped.
"""
from __future__ import annotations


import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.production import completion_receipt, staff, staff_sources, unit_config
from scripts.shutdown import remote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "whoami"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from atomic_file import write_text_atomic

import add_showrunner
from scripts.production import showrunners
import unit_effort
import unit_lookup
import sessions
import unit_runs
import herdr
from session_pane import HerdrFailure, read_place, type_place, where_place
from scripts.production.production_doc import Production, Refusal, read_production

STOP_WAIT_S = float(os.environ.get("PROMOTE_STOP_WAIT_S") or 60)
POLL_S = 0.5


class Promotion(NamedTuple):
    # The unit id, `<name>-unit`, as its row and its mark name it.
    unit: str
    parent: Production
    child: Production
    marked: unit_lookup.LabeledSession
    claude: unit_lookup.LiveClaude
    worktree: Path


def _git(where: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(where), *arguments], capture_output=True, text=True, check=False)


def run_marker(session_id: str) -> Path:
    return unit_runs.marker(session_id, unit_runs.environ("ACTIVE_DIR") or None)


def _session_status(pid: int) -> str:
    """The session record's `status`: `busy` while a turn runs."""
    record = sessions.read_session(unit_lookup.sessions_dir() / f"{pid}.json")
    if isinstance(record, sessions.UnreadableSessionRecord):
        raise Refusal("cannot read the unit's session record")
    return record.get("status", "")


def _quiet(unit: str, marked: unit_lookup.LabeledSession, claude: unit_lookup.LiveClaude) -> None:
    """Refuse a unit mid-turn or with a form, prompt or draft on screen: `/exit` would land in it."""
    if _session_status(claude.pid) == "busy":
        raise Refusal(f"{unit} is mid-turn; promote it once its turn ends")
    try:
        shown = read_place(marked.place, ansi=True)
    except (OSError, HerdrFailure) as error:
        raise Refusal(f"cannot read {unit}'s pane: {error}") from error
    verdict = unit_effort.verdict(shown)
    if isinstance(verdict, unit_effort.Skipped):
        raise Refusal(f"{unit}'s pane: {verdict.reason}")


def _settled(unit: str, claude: unit_lookup.LiveClaude, worktree: Path) -> None:
    """Refuse a unit with a run open, whose workers end only with the run, or with work uncommitted."""
    if run_marker(claude.session_id).exists():
        raise Refusal(f"{unit} has a /unit:direct run open, and its workers with it; the run finishes first")
    status = _git(worktree, "status", "--porcelain")
    if status.returncode != 0:
        raise Refusal(f"cannot read {unit}'s worktree {worktree}: {status.stderr.strip()}")
    if status.stdout.strip():
        raise Refusal(f"{unit}'s worktree {worktree} has uncommitted work; it checkpoints first")


def _committed(doc: Path) -> None:
    tracked = _git(doc.parent, "ls-files", "--error-unmatch", "--", doc.name)
    if tracked.returncode != 0 or _git(doc.parent, "status", "--porcelain", "--", doc.name).stdout.strip():
        raise Refusal(f"{doc} is not committed; the unit commits it first")


def checked(name: str, child_doc: Path, *,
            record: staff.StaffSnapshot | None = None) -> Promotion:
    """Everything the promotion needs. Raises Refusal naming what is in the way."""
    parent_doc = showrunners.production_of(staff.runtime_id())
    if parent_doc is None:
        raise Refusal("this session runs no production, so it has no unit to promote")
    parent = read_production(parent_doc)
    unit = name if name.replace("_", "-").endswith("-unit") else f"{name}-unit"
    pending = promotion_record(parent, unit)
    record = staff.snapshot() if record is None else record
    if pending.exists():
        fields = cast(dict[str, str], json.loads(pending.read_text(encoding="utf-8")))
        if Path(fields["child_doc"]).resolve() != child_doc.expanduser().resolve():
            raise Refusal(f"{unit} has an unfinished promotion to {fields['child_doc']}")
        owner = record.by_current_session(fields["session_id"])
        if owner is None:
            raise Refusal(f"{unit}: no staff row for the saved promotion session")
        if (owner.kind == "showrunner" and isinstance(owner.worktree, staff.WorktreeUnfilled)
                and owner.doc == staff.LiveDoc(remote.local_machine(), child_doc.expanduser().resolve())):
            # Promotion clears the location before child registration publishes it.
            worktree = Path(fields["worktree"])
        else:
            location = unit_lookup.local_worktree(staff_sources.MemberFound(owner))
            if not isinstance(location, staff.PresentWorktree):
                raise Refusal(f"{unit}: staff worktree is removed")
            worktree = location.path
        child = read_production(child_doc)
        claude = unit_lookup.LiveClaude(fields["session"], fields["session_id"], fields["socket"], int(fields["pid"]))
        marked = unit_lookup.LabeledSession(unit, claude, herdr.HerdrPlace(fields["session"], fields["pane"]))
        return Promotion(unit, parent, child, marked, claude, worktree)
    try:
        selected = staff_sources.member(staff_sources.UnitMember(parent.slug, unit), record)
        if isinstance(selected, staff_sources.NoSuchMember):
            raise Refusal(f"{unit} has no staff row under {parent.slug}")
        location = unit_lookup.local_worktree(selected)
        if not isinstance(location, staff.PresentWorktree):
            raise Refusal(f"{unit} is retired")
        worktree = location.path
        marked = unit_lookup.labeled_session(parent.slug, unit)
    except (OSError, UnicodeError) as error:
        raise Refusal(str(error)) from error
    if marked is None or not isinstance(marked.claude, unit_lookup.LiveClaude):
        raise Refusal(f"{unit} has no live session under {parent.slug}")
    claude = marked.claude
    _quiet(unit, marked, claude)
    _settled(unit, claude, worktree)
    child = read_production(child_doc)
    _committed(child.doc)
    # Only the unit's herdr tab will end before the same-named destination starts.
    directory = showrunner_account_directory()
    ending = add_showrunner.EndingUnitTab(marked.place.pane)
    _ = add_showrunner.checked(child, parent.slug, ending=ending, start_in=worktree, config_dir=directory)
    return Promotion(unit, parent, child, marked, claude, worktree)


def promotion_record(parent: Production, unit: str) -> Path:
    path = _git(parent.checkout, "rev-parse", "--git-path", f"promotions/{unit}.json")
    if path.returncode:
        raise Refusal(f"cannot find the promotion record: {path.stderr.strip()}")
    record = Path(path.stdout.strip())
    return record if record.is_absolute() else parent.checkout / record


def save_promotion(promotion: Promotion, target: Path) -> None:
    """Keep the conversation and destination before ending the unit."""
    path = promotion_record(promotion.parent, promotion.unit)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps({
        "unit": promotion.unit, "child_doc": str(promotion.child.doc), "target": str(target),
        "session_id": promotion.claude.session_id, "session": promotion.marked.place.session,
        "pane": promotion.marked.place.pane, "socket": promotion.claude.socket, "pid": str(promotion.claude.pid),
        "worktree": str(promotion.worktree),
        "launched_at": datetime.now(UTC).isoformat(),
    }) + "\n")


def finish_child(promotion: Promotion, target: Path, pending: Path, pid: int) -> int:
    """Finish our saved conversation in its child tab, without launching it again."""
    place = herdr.herdr_place_of(pid)
    if isinstance(place, herdr.NotInHerdr) or not any(
        pane["pane_id"] == place.pane and pane.get("label") == promotion.child.slug
        for pane in herdr.Herdr(place.session).panes()
    ):
        raise Refusal(f"session {promotion.claude.session_id} runs outside {promotion.child.slug}'s tab")
    fields = cast(dict[str, str], json.loads(pending.read_text(encoding="utf-8")))
    at = datetime.fromisoformat(fields["launched_at"])
    prompt = f"/showrunner:produce {promotion.child.doc} resume"
    transcript = herdr.SessionTranscript(target, promotion.claude.session_id)
    outcome = herdr.submitted_prompt(transcript, prompt, at)
    if not isinstance(outcome, herdr.PromptSubmitted):
        outcome = herdr.Herdr(place.session).submit_opening_prompt(
            place.pane, herdr.OpeningPrompt(prompt, transcript), at, 90_000)
    if isinstance(outcome, herdr.PromptNotSubmitted):
        return add_showrunner.SESSION_NOT_READY
    if not add_showrunner.showrunner_appeared(promotion.child.slug):
        return 1
    return showrunners.main(["attach", promotion.child.slug, promotion.parent.slug])


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop(promotion: Promotion) -> bool:
    """Type `/exit` and end its host session once Claude is gone; False when it outlived STOP_WAIT_S.

    Two processes on one session corrupt it, so nothing may resume it while this one runs.
    """
    place = promotion.marked.place
    try:
        type_place(place, "/exit", target=promotion.claude.session_id)
    except (OSError, HerdrFailure) as error:
        raise Refusal(f"cannot type /exit into {promotion.unit}'s pane: {error}") from error
    deadline = time.monotonic() + STOP_WAIT_S
    while _alive(promotion.claude.pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_S)
    try:
        herdr.close_agent(place.session, place.pane)
    except HerdrFailure as error:
        raise Refusal(f"cannot end {promotion.unit}'s session: {error}") from error
    return True


def showrunner_account_directory() -> Path:
    from scripts.accounts import accounts
    try:
        selected = accounts.account_for_group_launch("showrunner", resume=True)
    except accounts.AccountsError as error:
        raise Refusal(str(error)) from error
    return selected.dir


def account_line() -> str:
    return "showrunners account"


def restore_line(promotion: Promotion) -> str:
    """The command that resumes the stopped session as the unit it was, with the plan its .unit.toml names."""
    config = unit_config.read(promotion.worktree, cleanup=False)
    if isinstance(config, unit_config.ConfigMissing):
        return f"add_unit.py with --resume {promotion.claude.session_id}; {config.reason}"
    return "~/.claude/scripts/lib/py ~/.claude/scripts/production/add_unit.py " + shlex.join([
        "--production", str(promotion.parent.doc), promotion.unit.removesuffix("-unit"),
        *unit_config.plan_arguments(config.plan),
        "--resume", promotion.claude.session_id, "--cwd", str(promotion.worktree)])


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Promote a running unit to the showrunner of its own production.")
    _ = parser.add_argument("unit")
    _ = parser.add_argument("--production", required=True, type=Path, help="the child's production doc")
    _ = parser.add_argument("--model", required=True, help="pass your own model id")
    _ = parser.add_argument("--effort", required=True, help="pass your own effort level")
    _ = parser.add_argument("--check", action="store_true", help="run every refusal and stop nothing")
    args = parser.parse_args(argv)
    record = staff.snapshot()
    try:
        parent_doc = showrunners.production_of(staff.runtime_id())
        if parent_doc is not None:
            parent = read_production(parent_doc)
            unit_name = cast(str, args.unit)
            unit_name = unit_name if unit_name.replace("_", "-").endswith("-unit") else f"{unit_name}-unit"
            completed = promotion_record(parent, unit_name).with_suffix(".done.json")
            receipt = completion_receipt.read_receipt(completed, record)
            if isinstance(receipt, completion_receipt.OwnedReceipt):
                if isinstance(receipt.owner.doc, staff.WrappedDoc):
                    raise Refusal(f"{receipt.owner.name}: showrunner is wrapped; cannot complete this promotion")
                old = staff_sources.member(staff_sources.UnitMember(parent.slug, unit_name), record)
                child_doc = receipt.fields.get("child_doc")
                if (receipt.owner.kind == "showrunner" and isinstance(child_doc, str)
                        and Path(child_doc).resolve() == cast(Path, args.production).expanduser().resolve()
                        and (isinstance(old, staff_sources.NoSuchMember)
                             or isinstance(old.row.worktree, staff.RemovedWorktree))):
                    print(f"{unit_name} already promoted")
                    return 0
        promotion = checked(cast(str, args.unit), cast(Path, args.production), record=record)
        pending = promotion_record(promotion.parent, promotion.unit)
        target_directory = (Path(cast(dict[str, str], json.loads(pending.read_text(encoding="utf-8")))["target"])
                            if pending.exists() else showrunner_account_directory())
        moved = account_line()
    except Refusal as refusal:
        print(f"promote_showrunner: {refusal}", file=sys.stderr)
        return 2
    unit, slug = promotion.unit, promotion.child.slug
    if cast(bool, args.check):
        print(f"check: ok — would stop {unit} (herdr {promotion.marked.place.session}), resume it as the showrunner of "
              + f"{slug} in {promotion.worktree} under {promotion.parent.slug}, and preserve its native row; {moved}")
        return 0
    old_member = staff_sources.member(staff_sources.UnitMember(promotion.parent.slug, promotion.unit), record)
    parent_member = staff_sources.member(staff_sources.ShowrunnerMember(promotion.parent.slug), record)
    parent_row = parent_member.row if isinstance(parent_member, staff_sources.MemberFound) else None
    if parent_row is None:
        print(f"promote_showrunner: {promotion.parent.slug}: native showrunner identity is missing", file=sys.stderr)
        return 2
    _ = staff_sources.ensure_staff_showrunner(promotion.parent, runtime_id=parent_row.current_session)
    was_pending = pending.exists()
    if not was_pending:
        save_promotion(promotion, target_directory)
        import showrunner_parent
        showrunner_parent.save_notice(slug, showrunner_parent.PendingParentNotice("attach", promotion.parent.slug))
    try:
        current = unit_lookup.labeled_session(promotion.parent.slug, unit) if was_pending else promotion.marked
        if current is not None and isinstance(current.claude, unit_lookup.ClaudeUnknown):
            raise Refusal(f"cannot tell whether {unit} still runs: {current.claude.reason}")
        if current is not None and isinstance(current.claude, unit_lookup.LiveClaude):
            if current.claude.session_id != promotion.claude.session_id:
                raise Refusal(f"{unit} now runs a different conversation; kept its promotion record")
            stopped = stop(promotion._replace(marked=current, claude=current.claude))
        else:
            if current is not None:
                herdr.close_agent(current.place.session, current.place.pane)
            stopped = True
    except Refusal as refusal:
        print(f"promote_showrunner: {refusal}", file=sys.stderr)
        return 2
    if not stopped:
        print(f"{unit}'s Claude (pid {promotion.claude.pid}) still runs {STOP_WAIT_S:g} s after /exit, so nothing "
              + f"was restarted. Its pane is {where_place(promotion.marked.place)}; read it with "
              + f"`~/.claude/scripts/lib/py ~/.claude/scripts/production/session_pane.py read session {promotion.claude.session_id}`.")
        return 1
    native = staff.native_id(promotion.claude.session_id)
    name = showrunners.herdr_space(slug, str(promotion.child.doc))
    document = staff.LiveDoc(remote.local_machine(), promotion.child.doc.resolve())
    retained = record.by_current_session(native)
    if (retained is not None and retained.kind == "showrunner"
            and (retained.name, retained.parent, retained.doc) == (name, staff.ReportsTo(parent_row.id), document)):
        print("unchanged", file=sys.stderr)
        new_member = staff_sources.MemberFound(retained)
    else:
        new_member = staff_sources.MemberFound(staff.promote_unit(
            retained.id if retained is not None else native, name=name, parent=parent_row.id, doc=document))
    try:
        if was_pending and showrunners.current_name(slug):
            started = showrunners.main(["attach", slug, promotion.parent.slug])
        else:
            observed = sessions.session_liveness(promotion.claude.session_id) if was_pending else sessions.SessionNotLive()
            if isinstance(observed, sessions.SessionLivenessUnknown):
                raise Refusal(f"cannot tell whether session {promotion.claude.session_id} runs: {observed.reason}")
            if isinstance(observed, sessions.LiveSessionProcess):
                started = finish_child(promotion, target_directory, pending, observed.pid)
            else:
                started = add_showrunner.main(["--production", str(promotion.child.doc), "--model", cast(str, args.model),
                                               "--effort", cast(str, args.effort), "--resume", promotion.claude.session_id,
                                               "--cwd", str(promotion.worktree), "--config-dir", str(target_directory)])
    except (staff.StaffRefused, staff.StaffUnreadable):
        raise
    except (Refusal, OSError, herdr.HerdrFailure) as error:
        print(f"promote_showrunner: {error}", file=sys.stderr)
        return 1
    if started == 2:
        print(f"{unit} is stopped and did not start as a showrunner. "
              + f"Bring it back as the unit it was with: {restore_line(promotion)}")
        return 1
    code = finish(promotion, started, moved)
    if isinstance(old_member, staff_sources.MemberFound):
        workers = staff.find(kind="worker", parent=old_member.row.id)
        for worker in workers:
            print(f"kept worker {worker.id} {worker.name} under {old_member.row.name}")
        if workers:
            return 1
    if code == 0:
        fields = cast(dict[str, object], json.loads(pending.read_text(encoding="utf-8")))
        completion_receipt.write_receipt(pending.with_suffix(".done.json"), new_member.row.id, fields)
        pending.unlink(missing_ok=True)
    return code


def finish(promotion: Promotion, started: int, moved: str) -> int:
    """Say what is still owed by a unit that now runs as the child's session."""
    unit, slug = promotion.unit, promotion.child.slug
    if started == add_showrunner.SESSION_NOT_READY:
        print(f"{unit} is open as the session {slug}, but its showrunner prompt is not proven to have run. "
              + f"Look at it with `{herdr.attach_hint(showrunners.herdr_space(slug, str(promotion.child.doc)), slug)}`; unless it runs the production, type: "
              + f"/showrunner:produce {promotion.child.doc} resume")
        return 1
    print(f"{unit} is now the showrunner of {slug}; {moved}")
    return started


def main(argv: list[str]) -> int:
    def run() -> int:
        with staff.operation():
            return _main(argv)
    return staff.cli_result(run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
