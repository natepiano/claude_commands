#!/usr/bin/env python3
"""Promote a running unit director to the showrunner of its own production, under this showrunner.

Usage: promote_showrunner.py <unit> --production <child production doc> --model <id> --effort <level> [--check]

The parent showrunner runs it, once the unit has written the child's production doc and committed it.
The unit gets `/exit`; once its Claude is gone its herdr tab is closed, and add_showrunner.py resumes
the same conversation as the child's showrunner: in a tab of the shared herdr session, with no unit mark, on the configured showrunners account,
attached to this showrunner's production. The unit's Units row is then retired and committed in this
showrunner's checkout, also when the session opened but its opening prompt is not proven to have run.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "whoami"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from atomic_file import write_text_atomic

import add_showrunner
import showrunners
import unit_effort
import unit_lookup
import sessions
import unit_runs
import herdr
from session_pane import HerdrFailure, read_place, type_place, where_place
from add_unit import commit_paths
from production_units import Production, Refusal, cell_value, plan_cell_is_retired, read_production, unit_headings, unit_rows, unit_table

STOP_WAIT_S = float(os.environ.get("PROMOTE_STOP_WAIT_S") or 60)
POLL_S = 0.5


class Promotion(NamedTuple):
    # The unit id, `<name>-unit`, as its row and its mark name it.
    unit: str
    parent: Production
    child: Production
    marked: unit_lookup.MarkedUnit
    claude: unit_lookup.LiveClaude
    worktree: Path
    # The unit's Plan cell value: the line that brings it back as a unit names it.
    plan: str


def _git(where: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(where), *arguments], capture_output=True, text=True, check=False)


def run_marker(session_id: str) -> Path:
    return unit_runs.marker(session_id, unit_runs.environ("ACTIVE_DIR") or None)


def _unit_row(lines: list[str], unit: str) -> dict[str, str]:
    for cells in unit_table(lines):
        if cell_value(cells.get("Unit", "")) == unit:
            if plan_cell_is_retired(cells.get("Plan", "")):
                raise Refusal(f"{unit} is retired")
            return cells
    raise Refusal(f"{unit} has no row in the Units table")


def _session_status(pid: int) -> str:
    """The session record's `status`: `busy` while a turn runs."""
    try:
        record = cast(object, json.loads((unit_lookup.sessions_dir() / f"{pid}.json").read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        raise Refusal(f"cannot read the unit's session record: {error}") from error
    status = cast(dict[str, object], record).get("status") if isinstance(record, dict) else None
    return status if isinstance(status, str) else ""


def _quiet(unit: str, marked: unit_lookup.MarkedUnit, claude: unit_lookup.LiveClaude) -> None:
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


def checked(name: str, child_doc: Path) -> Promotion:
    """Everything the promotion needs. Raises Refusal naming what is in the way."""
    parent_doc = showrunners.production_of(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    if parent_doc is None:
        raise Refusal("this session runs no production, so it has no unit to promote")
    parent = read_production(parent_doc)
    unit = name if name.replace("_", "-").endswith("-unit") else f"{name}-unit"
    pending = promotion_record(parent, unit)
    if pending.exists():
        fields = cast(dict[str, str], json.loads(pending.read_text(encoding="utf-8")))
        if Path(fields["child_doc"]).resolve() != child_doc.expanduser().resolve():
            raise Refusal(f"{unit} has an unfinished promotion to {fields['child_doc']}")
        child = read_production(child_doc)
        claude = unit_lookup.LiveClaude(fields["session"], fields["session_id"], fields["socket"], int(fields["pid"]))
        marked = unit_lookup.MarkedUnit(unit, claude, herdr.HerdrPlace(fields["session"], fields["pane"]))
        return Promotion(unit, parent, child, marked, claude, Path(fields["worktree"]), fields["plan"])
    try:
        cells = _unit_row(parent.doc.read_text(encoding="utf-8").splitlines(), unit)
        marked = unit_lookup.marked_unit(parent.slug, unit)
    except (OSError, UnicodeError) as error:
        raise Refusal(str(error)) from error
    if marked is None or not isinstance(marked.claude, unit_lookup.LiveClaude):
        raise Refusal(f"{unit} has no live session under {parent.slug}")
    claude = marked.claude
    _quiet(unit, marked, claude)
    worktree = Path(cell_value(cells.get("Worktree", ""))).expanduser()
    _settled(unit, claude, worktree)
    child = read_production(child_doc)
    _committed(child.doc)
    # Only the unit's herdr tab will end before the same-named destination starts.
    directory = showrunner_account_directory()
    ending = add_showrunner.EndingUnitTab(marked.place.pane)
    _ = add_showrunner.checked(child, parent.slug, ending=ending, start_in=worktree, config_dir=directory)
    return Promotion(unit, parent, child, marked, claude, worktree, cell_value(cells.get("Plan", "")))


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
        "worktree": str(promotion.worktree), "plan": promotion.plan,
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
        type_place(place, "/exit")
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


def retire(promotion: Promotion) -> None:
    """Mark the unit's Units row retired, promoted to the child's showrunner, and commit the doc."""
    doc = promotion.parent.doc
    lines = doc.read_text(encoding="utf-8").split("\n")
    headings = unit_headings(lines)
    _, rows = unit_rows(lines)
    plan_at = headings.index("Plan") + 1
    for index, line in enumerate(lines):
        if line not in rows:
            continue
        parts = line.split("|")
        if cell_value(parts[headings.index("Unit") + 1].strip()) != promotion.unit:
            continue
        if plan_cell_is_retired(parts[plan_at]):
            break
        date = datetime.now(promotion.parent.zone).strftime("%Y-%m-%d")
        note = f"(retired {date}: promoted to showrunner of {promotion.child.slug})"
        parts[plan_at] = f" {note} {parts[plan_at].strip()} "
        lines[index] = "|".join(parts)
        break
    write_text_atomic(doc, "\n".join(lines))
    commit_paths(promotion.parent, [doc], f"production({promotion.parent.slug}): retire {promotion.unit}, "
                 + f"promoted to showrunner of {promotion.child.slug}")


def restore_line(promotion: Promotion) -> str:
    """The command that resumes the stopped session as the unit it was."""
    return "~/.claude/scripts/lib/py ~/.claude/scripts/production/add_unit.py " + shlex.join([
        "--production", str(promotion.parent.doc), promotion.unit.removesuffix("-unit"), "--plan", promotion.plan,
        "--resume", promotion.claude.session_id, "--cwd", str(promotion.worktree)])


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Promote a running unit to the showrunner of its own production.")
    _ = parser.add_argument("unit")
    _ = parser.add_argument("--production", required=True, type=Path, help="the child's production doc")
    _ = parser.add_argument("--model", required=True, help="pass your own model id")
    _ = parser.add_argument("--effort", required=True, help="pass your own effort level")
    _ = parser.add_argument("--check", action="store_true", help="run every refusal and stop nothing")
    args = parser.parse_args(argv)
    try:
        promotion = checked(cast(str, args.unit), cast(Path, args.production))
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
              + f"{slug} in {promotion.worktree} under {promotion.parent.slug}, and retire its row; {moved}")
        return 0
    was_pending = pending.exists()
    if not was_pending:
        save_promotion(promotion, target_directory)
    try:
        current = unit_lookup.marked_unit(promotion.parent.slug, unit) if was_pending else promotion.marked
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
    except (Refusal, OSError, herdr.HerdrFailure) as error:
        print(f"promote_showrunner: {error}", file=sys.stderr)
        return 1
    if started == 2:
        print(f"{unit} is stopped and did not start as a showrunner; its Units row is unchanged. "
              + f"Bring it back as the unit it was with: {restore_line(promotion)}")
        return 1
    result = finish(promotion, started, moved)
    if result == 0:
        pending.unlink(missing_ok=True)
    return result


def finish(promotion: Promotion, started: int, moved: str) -> int:
    """Retire the Units row of a unit that now runs as the child's session, and say what is still owed.

    A session open without a proven prompt is no longer the unit either, so its row is retired too.
    """
    unit, slug = promotion.unit, promotion.child.slug
    retired, row = True, "its Units row is retired"
    try:
        retire(promotion)
    except (OSError, subprocess.CalledProcessError, Refusal) as error:
        retired, row = False, f"its Units row was not retired and is owed: {error}"
    if started == add_showrunner.SESSION_NOT_READY:
        print(f"{unit} is open as the session {slug}, but its showrunner prompt is not proven to have run; {row}. "
              + f"Look at it with `{herdr.attach_hint(showrunners.herdr_space(slug, str(promotion.child.doc)), slug)}`; unless it runs the production, type: "
              + f"/showrunner:produce {promotion.child.doc} resume")
        return 1
    if not retired:
        print(f"{unit} now runs as the showrunner of {slug}, but {row}")
        return 1
    print(f"{unit} is now the showrunner of {slug}; {moved}")
    return started


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
