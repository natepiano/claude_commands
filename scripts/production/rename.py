#!/usr/bin/env python3
"""Rename a production: write the name, move the folders, run git branch -m, and set the displays."""
from __future__ import annotations

import argparse
import re
import shlex
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.production.production_doc import Refusal, cell_value, read_production  # noqa: E402
from scripts.production import showrunners  # noqa: E402
import showrunner_tree
import tidy_wrapped  # noqa: E402
import unit_lookup  # noqa: E402
import unit_move  # noqa: E402
from unit_move import PathMove  # noqa: E402
import rename_checkout  # noqa: E402
import rename_outside  # noqa: E402
from rename_state import RenameRefused, write_text  # noqa: E402

from scripts.astra import design_roles  # noqa: E402
from scripts.herdr import herdr  # noqa: E402
from scripts.production import staff, staff_sources, unit_config, unit_names


@dataclass(frozen=True)
class RenamePlan:
    old: str
    new: str
    old_doc: Path
    new_doc: Path
    checkout: Path
    merge_branch: str
    units: tuple[str, ...]
    plans: tuple[Path, ...]
    timer: PathMove
    state: tuple[PathMove, ...]
    staff_id: staff.StaffId

    @property
    def message(self) -> str:
        return f"chore({self.new}): rename production from {self.old}"

    @property
    def renames_branch(self) -> bool:
        """Whether the merge branch is named for the production and so takes the new name.

        A merge branch such as `main` or `init/catalyst` is not the production's name and is kept.
        """
        return self.merge_branch == self.old

    @property
    def branch(self) -> str:
        """The merge branch after the rename."""
        return self.new if self.renames_branch else self.merge_branch


def _validate_slug(slug: str) -> None:
    if not tidy_wrapped.SLUG.fullmatch(slug) or slug in tidy_wrapped.SHARED:
        raise RenameRefused(f"{slug!r} is not a production slug")


def _context(lines: list[str]) -> tuple[int, int]:
    try:
        start = lines.index("## Production Context\n")
    except ValueError as error:
        raise RenameRefused("production doc lacks Production Context") from error
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    return start + 1, end


def _formerly(text: str, old: str) -> str:
    lines = text.splitlines(keepends=True)
    start, end = _context(lines)
    for index in range(start, end):
        if lines[index].startswith("- **Formerly:**"):
            names = cast(list[str], re.findall(r"`([^`]+)`", lines[index]))
            names = [old, *(name for name in names if name != old)]
            lines[index] = "- **Formerly:** " + ", ".join(f"`{name}`" for name in names) + "\n"
            break
    else:
        insertion = next((index for index in range(start, end) if lines[index].startswith("- ")), start)
        while insertion < end and not lines[insertion].strip():
            insertion += 1
        lines.insert(insertion, f"- **Formerly:** `{old}`\n")
    return "".join(lines)


def _merge_line(text: str, branch: str) -> str:
    lines = text.splitlines(keepends=True)
    start, end = _context(lines)
    for index in range(start, end):
        if lines[index].startswith("- **Merge branch:**"):
            prefix = "- **Merge branch:**"
            raw = lines[index][len(prefix):]
            value = cell_value(raw.strip())
            updated = raw.replace(f"`{value}`", f"`{branch}`", 1) if "`" in raw else raw.replace(value, branch, 1)
            lines[index] = prefix + updated
            return "".join(lines)
    raise RenameRefused("production doc lacks Merge branch")


def _production_header(text: str, old: str, new: str) -> str:
    # Both forms are in unit plans: Production: old** and Production:** old.
    lines = text.splitlines(keepends=True)
    found = False
    for index, line in enumerate(lines):
        if not line.startswith("> **Production:"):
            continue
        updated = re.sub(rf"^(> \*\*Production:\s*){re.escape(old)}(?=\*\*)", rf"\g<1>{new}", line)
        updated = re.sub(rf"^(> \*\*Production:\*\*\s*)`?{re.escape(old)}`?(?=\s|$)",
                         rf"\g<1>{new}", updated)
        # The same metadata header often includes the production doc's path.
        updated = updated.replace(f"/{old}-production.md", f"/{new}-production.md")
        lines[index] = updated
        found = True
    if not found:
        lines.insert(1 if lines else 0, f"\n> **Production:** {new}\n")
    return "".join(lines)


def _relative(plan: RenamePlan, path: Path) -> str:
    try:
        return str(path.relative_to(plan.checkout))
    except ValueError as error:
        raise RenameRefused(f"{path} is outside the showrunner checkout {plan.checkout}") from error


def plan(old: str, new: str) -> RenamePlan:
    """Read the production and list its moves, without creating or rewriting anything.

    After the timer moves, `old` resolves to the new name, and the latest former name is the old name.
    """
    _validate_slug(new)
    record = staff.snapshot()
    rows = record.find(kind="showrunner")
    found = [row for row in rows if row.name == old] or [row for row in rows if row.name == new]
    if not found:
        raise staff.StaffRefused(f"{old}: no showrunner staff row")
    member = found[0]
    if isinstance(member.doc, staff.DocUnfilled):
        raise ValueError(f"{member.name}: the staff document is unfilled")
    slug = showrunners.canonical_slug(member.doc.path.name.removesuffix("-production.md"))
    _validate_slug(slug)
    known = showrunners.timer_slugs()
    if slug != new and new in known:
        raise RenameRefused(f"{new} is already a production")
    if slug not in known:
        raise RenameRefused(f"{old} has no update timer")
    instance = showrunners.NOTIFIER_STATE_DIR / f"showrunner-{slug}"
    checked = showrunners.checked_doc(instance)
    if isinstance(checked, showrunners.NoCheckedDoc):
        raise RenameRefused(f"{slug} has no readable update timer: {checked.reason}")
    production = read_production(checked.path)
    old = slug
    if slug == new:
        former = showrunners.former_slugs(production.doc.read_text(encoding="utf-8"))
        if not former:
            raise RenameRefused(f"{new} is already the production's name")
        old = former[0]
    _ = unit_move.git(production.checkout, "check-ref-format", "--branch", new)
    new_doc = production.doc.with_name(f"{new}-production.md")
    old_doc = production.doc.with_name(f"{old}-production.md") if production.doc == new_doc else production.doc
    staff_id = member.id
    rows = record.find(kind="unit", parent=staff_id)
    units = tuple(unit_names.unit_id(row.name) for row in rows)
    return RenamePlan(
        old, new, old_doc, new_doc, production.checkout, production.merge_branch,
        units, _plans(rows, production.checkout), PathMove(instance.with_name(f"showrunner-{old}"), instance.with_name(f"showrunner-{new}")),
        tuple(PathMove(source, destination) for source, destination in
              zip(tidy_wrapped.owned_paths(old), tidy_wrapped.owned_paths(new), strict=True)),
        staff_id)


def _plans(rows: list[staff.StaffRow], checkout: Path) -> tuple[Path, ...]:
    """The tracked plan files that the active units' `.unit.toml` files name."""
    plans: list[Path] = []
    for row in rows:
        if not isinstance(row.worktree, staff.PresentWorktree):
            continue
        config = unit_config.read(row.worktree.path, cleanup=False)
        if isinstance(config, unit_config.UnitConfig) and isinstance(config.plan, unit_config.PlanFile):
            plans.extend(path for path in unit_move.plan_paths(str(config.plan.path), checkout) if path not in plans)
    return tuple(plans)


def _describe(plan: RenamePlan) -> list[str]:
    def moves(move: PathMove) -> str:
        return f"{move.source} -> {move.destination}"

    parents = [plan.state[-1].source.parent / child / "parent"
               for child in showrunner_tree.stored_children(plan.old)]
    headers = [f"2. Production header {path}: {plan.old} -> {plan.new}" for path in plan.plans]
    return [f"1. prepare doc/CHECK; git mv {plan.old_doc} -> {plan.new_doc}; Formerly: `{plan.old}`",
            *(headers or ["2. Production headers: no unit plans"]),
            f"3. notifier {moves(plan.timer)}; CHECK {plan.old_doc} -> {plan.new_doc}",
            *(f"4. showrunner state {moves(move)}" for move in plan.state),
            f"4. parent files {plan.state[-1].source.parent}/*/parent: {plan.old} -> {plan.new}",
            *(f"4. parent {path}: {plan.old} -> {plan.new}" for path in parents),
            f"4. held design roles: holder {plan.old} showrunner -> {plan.new} showrunner",
            *rename_outside.describe(plan.old, plan.new, plan.old_doc, plan.new_doc),
            (f"6. merge branch {plan.merge_branch} -> {plan.new} (git branch -m); commit {plan.message!r}; "
             + f"push origin {plan.new}" if plan.renames_branch else
             f"6. merge branch {plan.merge_branch} kept (not named for the production); commit {plan.message!r}; "
             + f"push origin {plan.merge_branch}")]


def _rename_doc(plan: RenamePlan) -> None:
    if plan.old_doc.exists():
        text = _formerly(plan.old_doc.read_text(encoding="utf-8"), plan.old)
        text = re.sub(r"(?m)^(- \*\*Showrunner session:\*\*)[^\n]*$", rf"\1 `{plan.new}`", text)
        if unit_move.exists(plan.new_doc) and plan.new_doc.read_text(encoding="utf-8") != text:
            raise RenameRefused(f"destination already exists: {plan.new_doc}")
        write_text(plan.old_doc, text)
        # CHECK always names a readable doc, including while git mv removes the old path.
        write_text(plan.new_doc, text)
        _rewrite_check(plan.timer.source, plan)
        _ = unit_move.git(plan.checkout, "mv", "-f", "--", _relative(plan, plan.old_doc), _relative(plan, plan.new_doc))
    row = staff.by_id(plan.staff_id)
    if row is not None and not isinstance(row.doc, staff.DocUnfilled):
        _ = staff.set_doc(plan.staff_id, replace(row.doc, path=plan.new_doc))


def _rewrite_plans(plan: RenamePlan) -> None:
    for path in plan.plans:
        text = path.read_text(encoding="utf-8")
        updated = _production_header(text, plan.old, plan.new)
        if updated != text:
            write_text(path, updated)


def _rewrite_check(instance: Path, plan: RenamePlan) -> None:
    conf = instance / "conf"
    # Compared resolved: a doc under /tmp is /private/tmp on the Mac, and either spelling may be the one recorded.
    docs = (plan.old_doc.resolve(), plan.new_doc.resolve())
    text = conf.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("CHECK="):
            command = line[len("CHECK="):]
            tokens = list(re.finditer(r'''(?:[^\s'"\\]+|'[^']*'|"(?:[^"\\]|\\.)*"|\\.)+''', command))
            follows_script = False
            for token in tokens:
                value = shlex.split(token[0])[0]
                if follows_script:
                    if Path(value).resolve() == docs[0]:
                        updated_command = command[:token.start()] + shlex.quote(str(plan.new_doc)) + command[token.end():]
                        lines[index] = "CHECK=" + updated_command
                    break
                follows_script = Path(value).name == "production_check.sh"
    updated = "".join(lines)
    if updated != text:
        write_text(conf, updated)
    checked = showrunners.checked_doc(instance)
    if not isinstance(checked, showrunners.CheckedDoc) or checked.path != plan.new_doc:
        raise RenameRefused(f"{conf}: CHECK does not name {plan.new_doc}")


def _rename_timer(plan: RenamePlan) -> None:
    instance = plan.timer.source if plan.timer.source.exists() else plan.timer.destination
    _rewrite_check(instance, plan)
    unit_move.move(plan.timer)


def _rename_showrunner_state(plan: RenamePlan) -> None:
    for move in plan.state[:-1]:
        unit_move.move(move)
    directory = plan.state[-1]
    if directory.source.exists():
        for path in sorted(directory.source.iterdir()):
            unit_move.move(PathMove(path, directory.destination / path.name))
        directory.source.rmdir()
    for child in showrunner_tree.stored_children(plan.new):
        write_text(directory.destination.parent / child / "parent", plan.new + "\n")
    _move_held_roles(design_roles.ShowrunnerHolder(plan.old), design_roles.ShowrunnerHolder(plan.new))
    rename_outside.apply(plan.old, plan.new, plan.old_doc, plan.new_doc)


def _move_held_roles(old: design_roles.Holder, new: design_roles.Holder, old_checkout: str = "", new_checkout: str = "") -> None:
    """Held design roles name the renamed production as holder, so resume, rebind and end accept it."""
    try:
        design_roles.move_holder(old, new, old_checkout, new_checkout, reason="production rename")
    except (OSError, ValueError, SystemExit) as error:
        raise RenameRefused(f"held roles: {error}") from error


def _rename_unit_workers(plan: RenamePlan) -> None:
    from scripts.direct import workers
    root = workers.ledger_path(plan.old, "unused").parent
    units = {path.name.removesuffix(".move.json") if path.name.endswith(".move.json") else path.stem
             for path in root.glob("*.json")}
    for unit in sorted(units):
        workers.move_unit(plan.old, plan.new, unit)


def _rename_branch(plan: RenamePlan) -> None:
    """Run git branch -m on a merge branch named for the production, then commit and push the renamed documents."""
    if plan.renames_branch:
        if unit_move.branch_tip(plan.checkout, plan.old):
            _ = unit_move.git(plan.checkout, "branch", "-m", plan.old, plan.new)
        write_text(plan.new_doc, _merge_line(plan.new_doc.read_text(encoding="utf-8"), plan.new))
    paths = list(dict.fromkeys(_relative(plan, path) for path in (plan.old_doc, plan.new_doc, *plan.plans)))
    if unit_move.git(plan.checkout, "status", "--porcelain", "--", *paths):
        old_path = _relative(plan, plan.old_doc)
        if not unit_move.exists(plan.old_doc) and unit_move.git(plan.checkout, "ls-files", "-z", "--", old_path):
            _ = unit_move.git(plan.checkout, "rm", "--cached", "--quiet", "--", old_path)
        _ = unit_move.git(plan.checkout, "add", "--", *[path for path in paths if (plan.checkout / path).exists()])
        _ = unit_move.git(plan.checkout, "commit", "-m", plan.message)
    _ = unit_move.git(plan.checkout, "push", "--set-upstream", "origin", f"{plan.branch}:refs/heads/{plan.branch}")


def _set_displays(plan: RenamePlan) -> None:
    """Show the new name on the showrunner's herdr tab."""
    row = staff.by_id(plan.staff_id)
    client = herdr.Herdr(herdr.shared_session())
    for pane in client.panes():
        if row is not None and pane.get("session_id") == row.current_session:
            _ = client.retitle(pane["pane_id"], row.name)


STEPS = (_rename_doc, _rewrite_plans, _rename_timer, _rename_showrunner_state,
         _rename_unit_workers, _rename_branch, _set_displays)


def _pending(plan: RenamePlan) -> bool:
    """Whether a copy of the old name is left: the staff row, a file, a plan header, the branch or its push."""
    row = staff.by_id(plan.staff_id)
    moves = (PathMove(plan.old_doc, plan.new_doc), plan.timer, *plan.state)
    paths = [_relative(plan, path) for path in (plan.old_doc, plan.new_doc, *plan.plans)]
    return (row is None or row.name != plan.new
            or (not isinstance(row.doc, staff.DocUnfilled) and row.doc.path != plan.new_doc)
            or any(unit_move.exists(move.source) for move in moves)
            or any(_production_header(text := path.read_text(encoding="utf-8"), plan.old, plan.new) != text
                   for path in plan.plans)
            or bool(plan.renames_branch and unit_move.branch_tip(plan.checkout, plan.old))
            or bool(unit_move.git(plan.checkout, "status", "--porcelain", "--", *paths))
            or unit_move.remote_tip(plan.checkout, plan.branch) != unit_move.branch_tip(plan.checkout, plan.branch))


def apply(old: str, new: str) -> RenamePlan:
    """Write the name, then do each step; a step that fails prints one anomaly line and the next step runs."""
    from scripts.direct.workers import WorkerUnavailable
    with staff.operation(), design_roles.record_operation(), unit_move.production_lock():
        result = plan(old, new)
        if not _pending(result):
            print("unchanged")
            return result
        _ = staff.rename(result.staff_id, result.new)
        for step in STEPS:
            try:
                step(result)
            except (OSError, ValueError, Refusal, tidy_wrapped.TidyRefused,
                    staff.StaffRefused, herdr.HerdrFailure, WorkerUnavailable) as error:
                print(f"anomaly: {step.__name__.lstrip('_')}: {error}", file=sys.stderr)
        print(f"{result.old} is now {result.new}")
    return result


def _unit_row(plan: RenamePlan, name: str) -> staff_sources.MemberFound:
    found = staff_sources.member(staff_sources.UnitMember(plan.new, name), staff.snapshot())
    if isinstance(found, staff_sources.NoSuchMember):
        raise RenameRefused(f"{plan.new}/{name}: no unit staff row")
    if isinstance(found.row.worktree, staff.RemovedWorktree):
        raise RenameRefused(f"{name} is retired and has no live worktree")
    return found


def _destination(source: Path, old_branch: str, new_branch: str) -> Path:
    """The unit folder named for the new branch; a folder not named for the old branch keeps its path."""
    suffix = f"-{old_branch}"
    return source.with_name(source.name.removesuffix(suffix) + f"-{new_branch}") if source.name.endswith(suffix) else source


def _unit_branches(plan: RenamePlan, name: str, source: Path) -> tuple[str, str]:
    """The unit's old and new branch names, from the branch checked out at the staff folder or its renamed folder."""
    # A rerun after an interrupted folder move finds no folder at the staff path, so read from the checkout.
    listed = unit_move.git(plan.checkout, "for-each-ref", "--format=%(refname:short)%00%(worktreepath)",
                           f"refs/heads/{plan.old}-*", f"refs/heads/{plan.new}-*")
    for line in listed.splitlines():
        branch, _, path = line.partition("\0")
        stem = branch.removeprefix(plan.new + "-").removeprefix(plan.old + "-")
        old_branch, new_branch = f"{plan.old}-{stem}", f"{plan.new}-{stem}"
        if path and Path(path).resolve() in (source.resolve(), _destination(source, old_branch, new_branch).resolve()):
            return old_branch, new_branch
    raise RenameRefused(f"{name}: no {plan.old}- or {plan.new}- branch is checked out at {source}")


def unit(new: str, name: str) -> None:
    """Move one unit's folder (the folder move stops and starts it), then run git branch -m."""
    with staff.operation(), design_roles.record_operation(), unit_move.production_lock():
        current = showrunners.canonical_slug(new)
        result = plan(current, current)
        found = _unit_row(result, name)
        source = unit_lookup.local_worktree(found).path
        old_branch, new_branch = _unit_branches(result, name, source)
        destination = _destination(source, old_branch, new_branch)
        if destination != source:
            unit_move.move_folder(found.row.id, destination)
            _move_held_roles(design_roles.UnitHolder(result.old, name), design_roles.UnitHolder(result.new, name),
                             str(source), str(destination))
        renamed = unit_move.git(destination, "branch", "--show-current") == old_branch
        if renamed:
            _ = unit_move.git(destination, "branch", "-m", old_branch, new_branch)
        changed = destination != source or renamed
        print(f"{name}: {'moved to ' + str(destination) if changed else 'unchanged'}")


def main(argv: list[str]) -> int:
    return staff.cli_result(lambda: _main(argv))


def _main(argv: list[str]) -> int:
    from scripts.direct.workers import WorkerUnavailable
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("plan", "apply"):
        command = actions.add_parser(name)
        _ = command.add_argument("old")
        _ = command.add_argument("new")
    command = actions.add_parser("unit")
    _ = command.add_argument("new")
    _ = command.add_argument("unit")
    command = actions.add_parser("checkout", help="move the showrunner checkout through a shutdown restart")
    _ = command.add_argument("new", metavar="slug")
    _ = command.add_argument("path", nargs="?", default="")
    args = parser.parse_args(argv)
    action, new = cast(str, args.action), cast(str, args.new)
    try:
        if action == "unit":
            unit(new, cast(str, args.unit))
        elif action == "checkout":
            rename_checkout.checkout(new, cast(str, args.path))
        elif action == "plan":
            for line in _describe(plan(cast(str, args.old), new)):
                print(line)
        else:
            _ = apply(cast(str, args.old), new)
    except (staff.StaffRefused, staff.StaffNotFound, staff.StaffUnreadable):
        raise
    except (RenameRefused, Refusal, tidy_wrapped.TidyRefused, WorkerUnavailable,
            herdr.HerdrFailure, OSError, UnicodeError) as error:
        print(f"rename: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
