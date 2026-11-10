#!/usr/bin/env python3
"""Promote a unit director and restore its retained workers under the same staff id.

The parent showrunner runs this command after the unit commits the child production document.
The shared stop keeps conversations and worker rows for restore.
The configured showrunner account resumes the director as the child's showrunner.
The completion receipt follows worker restore.
Use --check to inspect the promotion without stopping sessions.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass


import argparse
import json
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, cast


from scripts.production import completion_receipt, staff, staff_sources, unit_config, worker
from scripts.agents import remote, stop_resume
from scripts.agents import worker_lifecycle

from scripts.lib.atomic_file import write_text_atomic

from scripts.production import add_showrunner
from scripts.production import showrunners
from scripts.production import unit_effort
from scripts.production import unit_lookup
from scripts.message import sessions
from scripts.lib import unit_runs
from scripts.herdr import herdr
from scripts.production.session_pane import HerdrFailure, read_place
from scripts.production.production_doc import Production, ProductionDocument, Refusal, read_new_production, read_production


class ProductionDocumentDefault(NamedTuple):
    """The request needs the unit's production document."""


class CheckAllFiles(NamedTuple):
    """The worktree check includes every file."""


CHECK_ALL_FILES = CheckAllFiles()


class Promotion(NamedTuple):
    # The unit name comes from its staff row.
    unit: str
    parent: Production
    child: ProductionDocument
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


def _settled(unit: str, claude: unit_lookup.LiveClaude, worktree: Path, *,
             skip_path: Path | CheckAllFiles = CHECK_ALL_FILES) -> None:
    """Refuse an open run or uncommitted work outside the skipped path."""
    if run_marker(claude.session_id).exists():
        raise Refusal(f"{unit} has a /unit:direct run open, and its workers with it; the run finishes first")
    paths = ["--", ".", f":(top,exclude,literal){skip_path.relative_to(worktree).as_posix()}"] if isinstance(skip_path, Path) else []
    status = _git(worktree, "status", "--porcelain", *paths)
    if status.returncode != 0:
        raise Refusal(f"cannot read {unit}'s worktree {worktree}: {status.stderr.strip()}")
    if status.stdout.strip():
        raise Refusal(f"{unit}'s worktree {worktree} has uncommitted work; it checkpoints first")


def _committed(doc: Path) -> None:
    tracked = _git(doc.parent, "ls-files", "--error-unmatch", "--", doc.name)
    if tracked.returncode != 0 or _git(doc.parent, "status", "--porcelain", "--", doc.name).stdout.strip():
        raise Refusal(f"{doc} is not committed; the unit commits it first")


def check_director(name: str, marked: unit_lookup.LabeledSession, claude: unit_lookup.LiveClaude,
                   worktree: Path, *, skip_path: Path | CheckAllFiles = CHECK_ALL_FILES) -> None:
    """Require an idle director and no uncommitted work outside the skipped path."""
    _quiet(name, marked, claude)
    _settled(name, claude, worktree, skip_path=skip_path)


def checked(name: str, child_name: str, doc: Path, *,
            record: staff.StaffSnapshot | None = None) -> Promotion:
    """Everything the promotion needs. Raises Refusal naming what is in the way."""
    parent_name = showrunners.own_name()
    if not parent_name:
        raise Refusal("this session runs no production, so it has no unit to promote")
    record = staff.snapshot() if record is None else record
    parent = read_production(parent_name, record=record)
    child = read_new_production(doc, child_name)
    pending = promotion_record(parent, name)
    if pending.exists():
        fields = cast(dict[str, str], json.loads(pending.read_text(encoding="utf-8")))
        if Path(fields["child_doc"]).resolve() != child.doc.expanduser().resolve():
            raise Refusal(f"{name} has an unfinished promotion to {fields['child_doc']}")
        owner = record.by_current_session(fields["session_id"])
        if owner is None:
            raise Refusal(f"{name}: no staff row for the saved promotion session")
        if (owner.kind == "showrunner" and isinstance(owner.worktree, staff.WorktreeUnfilled)
                and owner.doc == staff.LiveDoc(remote.local_machine(), child.doc.expanduser().resolve())):
            # Promotion clears the location before child registration publishes it.
            worktree = Path(fields["worktree"])
        else:
            location = unit_lookup.local_worktree(staff_sources.MemberFound(owner))
            if not isinstance(location, staff.PresentWorktree):
                raise Refusal(f"{name}: staff worktree is removed")
            worktree = location.path
        claude = unit_lookup.LiveClaude(fields["session"], fields["session_id"], fields["socket"], int(fields["pid"]))
        marked = unit_lookup.LabeledSession(name, claude, herdr.HerdrPlace(fields["session"], fields["pane"]))
        return Promotion(name, parent, child, marked, claude, worktree)
    try:
        selected = staff_sources.member(staff_sources.UnitMember(parent.name, name), record)
        if isinstance(selected, staff_sources.NoSuchMember):
            raise Refusal(f"{name} has no staff row under {parent.name}")
        location = unit_lookup.local_worktree(selected)
        if not isinstance(location, staff.PresentWorktree):
            raise Refusal(f"{name} is retired")
        worktree = location.path
        marked = unit_lookup.labeled_session(parent.name, name)
    except (OSError, UnicodeError) as error:
        raise Refusal(str(error)) from error
    if marked is None or not isinstance(marked.claude, unit_lookup.LiveClaude):
        raise Refusal(f"{name} has no live session under {parent.name}")
    claude = marked.claude
    check_director(name, marked, claude, worktree)
    _committed(child.doc)
    # Only the unit's herdr tab will end before the same-named destination starts.
    directory = showrunner_account_directory()
    ending = add_showrunner.EndingUnitTab(marked.place.pane)
    _ = add_showrunner.checked(child, parent.name, ending=ending, start_in=worktree, config_dir=directory)
    return Promotion(name, parent, child, marked, claude, worktree)


def promotion_record(parent: Production, unit: str) -> Path:
    return director_record(parent, unit, "promotions")


def director_record(parent: Production, name: str, operation: str) -> Path:
    """Locate a director transition record in the parent's Git directory."""
    path = _git(parent.checkout, "rev-parse", "--git-path", f"{operation}/{name}.json")
    if path.returncode:
        raise Refusal(f"cannot find the director record: {path.stderr.strip()}")
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
        pane["pane_id"] == place.pane and pane.get("label") == promotion.child.name
        for pane in herdr.Herdr(place.session).panes()
    ):
        raise Refusal(f"session {promotion.claude.session_id} runs outside {promotion.child.name}'s tab")
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
    if not add_showrunner.showrunner_appeared(promotion.child.name):
        return 1
    return showrunners.main(["attach", promotion.child.name, promotion.parent.name])


def stop(promotion: Promotion) -> bool:
    """Stop the director and retain its workers for the promoted holder."""
    return stop_director(promotion.unit, promotion.marked, promotion.claude)


def stop_director(name: str, marked: unit_lookup.LabeledSession, claude: unit_lookup.LiveClaude) -> bool:
    """Stop the director and retain its workers for the next role."""
    record = staff.snapshot()
    row = record.by_member_reference(claude.session_id)
    unresolved = stop_resume.stop_unit_workers(row.id, record) if row is not None else []
    result = stop_resume.stop(claude.session_id, deadline=worker_lifecycle.effective_deadline())
    stopped = isinstance(result, (stop_resume.Stopped, stop_resume.NotRunning))
    if stopped and not unresolved:
        place = marked.place
        client = herdr.Herdr(place.session)
        try:
            panes = client.panes()
            tabs = {pane.get("tab_id", "") for pane in panes if pane["pane_id"] == place.pane}
            workers: set[str] = {worker.name for worker in record.find(kind="worker", parent=row.id)} if row is not None else set()
            shells = [pane for pane in panes if pane.get("tab_id", "") in tabs and pane.get("label") in workers]
        except (OSError, ValueError, herdr.HerdrFailure) as error:
            print(f"anomaly: {name}: {' '.join(str(error).splitlines())}", file=sys.stderr)
            shells = []
        for pane in shells:
            try:
                if isinstance(client.pane_activity(pane["pane_id"]), herdr.PaneAtShell) and not client.close_tab_confirmed(
                        pane["pane_id"], terminal=pane.get("terminal_id", "")):
                    print(f"anomaly: {pane.get('label')}: old worker shell close failed", file=sys.stderr)
            except (OSError, ValueError, herdr.HerdrFailure) as error:
                print(f"anomaly: {pane.get('label')}: {' '.join(str(error).splitlines())}", file=sys.stderr)
        try:
            if not client.close_tab_confirmed(place.pane, target=claude.session_id):
                print(f"anomaly: {name}: old director tab close failed", file=sys.stderr)
        except (OSError, ValueError, herdr.HerdrFailure) as error:
            print(f"anomaly: {name}: {' '.join(str(error).splitlines())}", file=sys.stderr)
    return stopped


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
        "--production", promotion.parent.name, promotion.unit,
        *unit_config.plan_arguments(config.plan),
        "--resume", promotion.claude.session_id, "--cwd", str(promotion.worktree)])


def default_document(parent: Production, name: str, record: staff.StaffSnapshot) -> Path:
    """Find the unit's recorded production document before any role change."""
    pending = promotion_record(parent, name)
    if pending.is_file():
        fields = cast(dict[str, str], json.loads(pending.read_text()))
        return Path(fields["child_doc"])
    selected = staff_sources.member(staff_sources.UnitMember(parent.name, name), record)
    if isinstance(selected, staff_sources.MemberFound) and isinstance(selected.row.worktree, staff.PresentWorktree):
        worktree = selected.row.worktree.path
        saved = unit_config.read(worktree, cleanup=False)
        if isinstance(saved, unit_config.UnitConfig) and isinstance(saved.plan, unit_config.PlanFile):
            try:
                return read_new_production(worktree / saved.plan.path, name).doc
            except Refusal:
                pass
        found: list[Path] = []
        for folder, directories, files in worktree.walk():
            directories[:] = [directory for directory in directories if directory not in (".git", "target")]
            for filename in sorted(files):
                if Path(filename).suffix.casefold() != ".md":
                    continue
                try:
                    found.append(read_new_production(folder / filename, name).doc)
                except Refusal:
                    continue
        if len(found) == 1:
            return found[0]
        candidates = "; candidates: " + ", ".join(str(path) for path in sorted(found)) if found else ""
        raise Refusal(f"doc default is missing or ambiguous; looked in {worktree / '.unit.toml'} and {worktree}{candidates}")
    raise Refusal(f"doc default is missing; looked in staff row {name} under {parent.name}")


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Promote a running unit to the showrunner of its own production.")
    _ = parser.add_argument("unit")
    _ = parser.add_argument("--production", default="", help="the child's staff name")
    _ = parser.add_argument("--doc", type=Path, default=ProductionDocumentDefault(), help="the new production doc")
    _ = parser.add_argument("--model", required=True, help="pass your own model id")
    _ = parser.add_argument("--effort", required=True, help="pass your own effort level")
    _ = parser.add_argument("--check", action="store_true", help="run every refusal and stop nothing")
    args = parser.parse_args(argv)
    record = staff.snapshot()
    unit_name = cast(str, args.unit)
    child_name = cast(str, args.production) or unit_name
    document = cast(Path | ProductionDocumentDefault, args.doc)
    model = cast(str, args.model)
    effort = cast(str, args.effort)
    try:
        parent_name = showrunners.own_name()
        if parent_name:
            parent = read_production(parent_name, record=record)
            completed = promotion_record(parent, unit_name).with_suffix(".done.json")
            receipt = completion_receipt.read_receipt(completed, record)
            if isinstance(receipt, completion_receipt.OwnedReceipt):
                if isinstance(receipt.owner.doc, staff.WrappedDoc):
                    raise Refusal(f"{receipt.owner.name}: showrunner is wrapped; cannot complete this promotion")
                old = staff_sources.member(staff_sources.UnitMember(parent.name, unit_name), record)
                child_doc = receipt.fields.get("child_doc")
                if (receipt.owner.kind == "showrunner" and isinstance(child_doc, str)
                        and receipt.owner.name == child_name
                        and (isinstance(old, staff_sources.NoSuchMember)
                             or isinstance(old.row.worktree, staff.RemovedWorktree))):
                    print(f"{unit_name} already promoted")
                    return 0
        if isinstance(document, ProductionDocumentDefault):
            document = default_document(read_production(parent_name, record=record), unit_name, record)
        promotion = checked(unit_name, child_name, document, record=record)
        pending = promotion_record(promotion.parent, promotion.unit)
        target_directory = (Path(cast(dict[str, str], json.loads(pending.read_text(encoding="utf-8")))["target"])
                            if pending.exists() else showrunner_account_directory())
        moved = account_line()
    except Refusal as refusal:
        print(f"promote_showrunner: {refusal}", file=sys.stderr)
        return 2
    unit = promotion.unit
    if cast(bool, args.check):
        print(f"check: ok — would stop {unit} (herdr {promotion.marked.place.session}), resume it as the showrunner of "
              + f"{promotion.child.name} in {promotion.worktree} under {promotion.parent.name}, and preserve its native row; {moved}")
        return 0
    parent_member = staff_sources.member(staff_sources.ShowrunnerMember(promotion.parent.name), record)
    parent_row = parent_member.row if isinstance(parent_member, staff_sources.MemberFound) else None
    if parent_row is None:
        print(f"promote_showrunner: {promotion.parent.name}: native showrunner identity is missing", file=sys.stderr)
        return 2
    _ = staff_sources.ensure_staff_showrunner(promotion.parent, runtime_id=parent_row.current_session)
    was_pending = pending.exists()
    if not was_pending:
        save_promotion(promotion, target_directory)
    try:
        current = unit_lookup.labeled_session(promotion.parent.name, unit) if was_pending else promotion.marked
        if current is not None and isinstance(current.claude, unit_lookup.LiveClaude) and current.claude.session_id != promotion.claude.session_id:
            raise Refusal(f"{unit} now runs a different conversation; kept its promotion record")
        stopped = stop(promotion._replace(marked=current, claude=current.claude)
            if current is not None and isinstance(current.claude, unit_lookup.LiveClaude) else promotion)
    except Refusal as refusal:
        print(f"promote_showrunner: {refusal}", file=sys.stderr)
        return 2
    if not stopped:
        print(f"{unit}: stop is not proved; promotion remains pending")
        return 1
    native = staff.native_id(promotion.claude.session_id)
    document = staff.LiveDoc(remote.local_machine(), promotion.child.doc.resolve())
    retained = record.by_current_session(native)
    if (retained is not None and retained.kind == "showrunner"
            and (retained.name, retained.parent, retained.doc) == (promotion.child.name, staff.ReportsTo(parent_row.id), document)):
        print("unchanged", file=sys.stderr)
        new_member = staff_sources.MemberFound(retained)
    else:
        new_member = staff_sources.MemberFound(staff.promote_unit(
            retained.id if retained is not None else native, name=promotion.child.name, parent=parent_row.id, doc=document))
    try:
        from scripts.production import showrunner_parent
        showrunner_parent.save_notice(promotion.child.name, showrunner_parent.PendingParentNotice("attach", promotion.parent.name))
        if was_pending and showrunners.current_name(promotion.child.name):
            started = showrunners.main(["attach", promotion.child.name, promotion.parent.name])
        else:
            observed = sessions.session_liveness(promotion.claude.session_id) if was_pending else sessions.SessionNotLive()
            if isinstance(observed, sessions.SessionLivenessUnknown):
                raise Refusal(f"cannot tell whether session {promotion.claude.session_id} runs: {observed.reason}")
            if isinstance(observed, sessions.LiveSessionProcess):
                started = finish_child(promotion, target_directory, pending, observed.pid)
            else:
                started = add_showrunner.main(["--production", promotion.child.name, "--model", model,
                                               "--effort", effort, "--resume", promotion.claude.session_id,
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
    if code == 0:
        code = int(worker.restore(new_member.row.id))
    if code == 0:
        fields = cast(dict[str, object], json.loads(pending.read_text(encoding="utf-8")))
        completion_receipt.write_receipt(pending.with_suffix(".done.json"), new_member.row.id, fields)
        pending.unlink(missing_ok=True)
    return code


def finish(promotion: Promotion, started: int, moved: str) -> int:
    """Say what is still owed by a unit that now runs as the child's session."""
    unit = promotion.unit
    if started == add_showrunner.SESSION_NOT_READY:
        print(f"{unit} is open as the session {promotion.child.name}, but its showrunner prompt is not proven to have run. "
              + f"Look at it with `{herdr.attach_hint(promotion.child.name, promotion.child.name)}`; unless it runs the production, type: "
              + f"/showrunner:produce {promotion.child.doc} resume")
        return 1
    print(f"{unit} is now the showrunner of {promotion.child.name}; {moved}")
    return started


def main(argv: list[str]) -> int:
    def run() -> int:
        with staff.operation():
            return _main(argv)
    return staff.cli_result(run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
