#!/usr/bin/env python3
"""Plan and resume a production rename, keeping its old merge branch until finish."""
from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from contextlib import contextmanager, nullcontext
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
import add_unit  # noqa: E402
import production_units  # noqa: E402
import showrunners  # noqa: E402
import stall_watch  # noqa: E402
import tidy_wrapped  # noqa: E402
import unit_lookup  # noqa: E402
import unit_move  # noqa: E402
from unit_move import PathMove  # noqa: E402
import rename_checkout  # noqa: E402
import rename_outside  # noqa: E402
import rename_state  # noqa: E402
import rename_unit  # noqa: E402
from rename_state import RenameRefused, read_json, write_json, write_text  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.guilds import roles  # noqa: E402

if TYPE_CHECKING:
    from scripts.shutdown import record as shutdown_record
    from scripts.shutdown.record import SessionShutdown
else:
    import record as shutdown_record
    from record import SessionShutdown


class StoppedUnitRestore(TypedDict):
    kind: Literal["stopped"]
    login: str
    session_id: str


class UnitWithoutRestore(TypedDict):
    kind: Literal["absent"]


UnitRestore = StoppedUnitRestore | UnitWithoutRestore


REVIEW_POOL_STATE = Path(os.environ.get("REVIEW_POOL_STATE") or Path.home() / ".local/state/review-pool")


@dataclass(frozen=True)
class RenamePlan:
    old: str
    new: str
    old_doc: Path
    new_doc: Path
    checkout: Path
    merge_branch: str
    base: str
    units: tuple[str, ...]
    plans: tuple[Path, ...]
    timer: PathMove
    state: tuple[PathMove, ...]
    stall: tuple[PathMove, ...]
    review_pool: PathMove

    @property
    def journal(self) -> Path:
        return self.state[-1].destination / "rename.json"

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


def _check_unit_tip(plan: RenamePlan, record: dict[str, object]) -> None:
    unit_move.check_tip(plan.checkout, _string(record, "old_branch"), _string(record, "tip"))


def _push_unit_branch(plan: RenamePlan, record: dict[str, object]) -> None:
    unit_move.push_branch(plan.checkout, _string(record, "old_branch"), _string(record, "new_branch"),
                          _string(record, "tip"), (plan.merge_branch, plan.branch))


def _move_unit_worktree(plan: RenamePlan, record: dict[str, object]) -> None:
    unit_move.move_worktree(plan.checkout, _string(record, "old_branch"), _string(record, "new_branch"),
                            Path(_string(record, "old_worktree")), Path(_string(record, "new_worktree")),
                            _string(record, "tip"))


def _move_unit_configs(record: dict[str, object]) -> None:
    old, new = Path(_string(record, "old_worktree")), Path(_string(record, "new_worktree"))
    unit_move.move_configs(str(old), str(new))
    run = unit_move.newest_run(old) or unit_move.newest_run(new)
    if run is not None:
        unit_move.move_run(run, old, new, _string(record, "new_branch"))


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
            value = production_units.cell_value(raw.strip())
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


def _build(old: str, new: str) -> RenamePlan:
    _validate_slug(new)
    old = showrunners.canonical_slug(old)
    _validate_slug(old)
    if old == new:
        raise RenameRefused(f"{new} is already the production's name")
    known = showrunners.timer_slugs()
    if (unit_move.exists(showrunners.NOTIFIER_STATE_DIR / f"showrunner-{new}")
            or new in known or showrunners.canonical_slug(new) != new):
        raise RenameRefused(f"{new} is already a current or former production slug")
    if old not in known:
        raise RenameRefused(f"{old} has no update timer")
    instance = showrunners.NOTIFIER_STATE_DIR / f"showrunner-{old}"
    checked = showrunners.checked_doc(instance)
    if isinstance(checked, showrunners.NoCheckedDoc):
        raise RenameRefused(f"{old} has no readable update timer: {checked.reason}")
    production = add_unit.read_production(checked.path)
    _ = unit_move.git(production.checkout, "check-ref-format", "--branch", new)
    if showrunners.production_slug(str(production.doc)) != old:
        raise RenameRefused(f"{old}'s timer names a different production doc: {production.doc}")
    lines = production.doc.read_text(encoding="utf-8").splitlines()
    units: list[str] = []
    plans: list[Path] = []
    for row in production_units.unit_table(lines):
        unit = production_units.cell_value(row.get("Unit", ""))
        if not unit:
            raise RenameRefused("Units row has no unit name")
        units.append(unit)
        raw = row.get("Plan", "")
        if production_units.plan_cell_is_retired(raw):
            continue
        if raw.strip() in ("", "—", "-", "permanent"):
            continue
        for path in unit_move.plan_paths(raw, production.checkout):
            if path not in plans:
                plans.append(path)
    result = RenamePlan(
        old, new, production.doc, production.doc.with_name(f"{new}-production.md"), production.checkout,
        production.merge_branch, unit_move.git(production.checkout, "rev-parse", f"refs/heads/{production.merge_branch}"),
        tuple(units), tuple(plans), PathMove(instance, instance.with_name(f"showrunner-{new}")),
        tuple(PathMove(source, destination) for source, destination in
              zip(tidy_wrapped.owned_paths(old), tidy_wrapped.owned_paths(new), strict=True)),
        tuple(PathMove(stall_watch.stretch_path(old, unit), stall_watch.stretch_path(new, unit)) for unit in units),
        PathMove(REVIEW_POOL_STATE.expanduser() / old, REVIEW_POOL_STATE.expanduser() / new))
    for path in (result.old_doc, *result.plans):
        _ = unit_move.git(result.checkout, "ls-files", "--error-unmatch", "--", _relative(result, path))
        _ = path.read_text(encoding="utf-8")
    return result


def _move_record(move: PathMove) -> dict[str, str]:
    return {"source": str(move.source), "destination": str(move.destination)}


def _save(plan: RenamePlan, completed: tuple[str, ...]) -> None:
    fields = _object(read_json(plan.journal)) if plan.journal.exists() else {}
    fields.update({
        "version": 1, "old": plan.old, "new": plan.new, "old_doc": str(plan.old_doc),
        "new_doc": str(plan.new_doc), "checkout": str(plan.checkout), "merge_branch": plan.merge_branch,
        "base": plan.base, "units": list(plan.units), "plans": [str(path) for path in plan.plans],
        "timer": _move_record(plan.timer), "state": [_move_record(move) for move in plan.state],
        "stall": [_move_record(move) for move in plan.stall], "review_pool": _move_record(plan.review_pool),
        "completed": list(completed),
    })
    if "done" not in fields:
        fields["done"] = False
    write_json(plan.journal, fields)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RenameRefused("invalid rename journal: expected an object")
    return cast(dict[str, object], value)


def _string(fields: dict[str, object], key: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str) or not value:
        raise RenameRefused(f"invalid rename journal: {key}")
    return value


def _strings(fields: dict[str, object], key: str) -> tuple[str, ...]:
    value = fields.get(key)
    if not isinstance(value, list):
        raise RenameRefused(f"invalid rename journal: {key}")
    items = cast(list[object], value)
    if not all(isinstance(item, str) and item for item in items):
        raise RenameRefused(f"invalid rename journal: {key}")
    return tuple(cast(str, item) for item in items)


def _read_move(value: object) -> PathMove:
    fields = _object(value)
    return PathMove(Path(_string(fields, "source")), Path(_string(fields, "destination")))


def _read_moves(value: object) -> tuple[PathMove, ...]:
    if not isinstance(value, list):
        raise RenameRefused("invalid rename journal: expected moves")
    return tuple(_read_move(item) for item in cast(list[object], value))


def _completed_steps(fields: dict[str, object]) -> tuple[str, ...] | None:
    """Return the completed apply step names, or None when the record is not valid."""
    if "completed" in fields:
        names = fields["completed"]
        if not isinstance(names, list) or not all(isinstance(name, str) and name for name in cast(list[object], names)):
            return None
        return tuple(cast(list[str], names))
    # A journal written before apply recorded step names holds a count of the 7-step list.
    step = fields.get("step")
    if type(step) is not int or not 0 <= step <= len(COUNTED_APPLY_STEP_NAMES):
        return None
    return COUNTED_APPLY_STEP_NAMES[:step]


def _load(path: Path, old: str, new: str) -> tuple[RenamePlan, tuple[str, ...]]:
    fields = _object(read_json(path))
    if fields.get("version") != 1 or fields.get("new") != new:
        raise RenameRefused(f"{path}: incompatible rename journal")
    recorded_old = _string(fields, "old")
    if old not in (recorded_old, new) and showrunners.canonical_slug(old) not in (recorded_old, new):
        raise RenameRefused(f"{path}: rename is from {recorded_old}, not {old}")
    state = _read_moves(fields.get("state"))
    completed = _completed_steps(fields)
    if len(state) != len(tidy_wrapped.owned_paths(new)) or completed is None or not isinstance(fields.get("done"), bool):
        raise RenameRefused(f"{path}: invalid rename progress")
    result = RenamePlan(recorded_old, new, Path(_string(fields, "old_doc")), Path(_string(fields, "new_doc")),
                        Path(_string(fields, "checkout")), _string(fields, "merge_branch"), _string(fields, "base"),
                        _strings(fields, "units"), tuple(Path(item) for item in _strings(fields, "plans")),
                        _read_move(fields.get("timer")), state, _read_moves(fields.get("stall")),
                        _read_move(fields.get("review_pool")))
    if result.journal != path:
        raise RenameRefused(f"{path}: journal state directory changed")
    return result, completed


def plan(old: str, new: str) -> RenamePlan:
    """Read the production and list its moves, without creating or rewriting anything."""
    _validate_slug(new)
    journal = rename_state.production_journal(new)
    if journal.exists():
        return _load(journal, old, new)[0]
    return _build(old, new)


def _describe(plan: RenamePlan) -> list[str]:
    def moves(move: PathMove) -> str:
        return f"{move.source} -> {move.destination}"

    parents = [path for path in sorted(plan.state[-1].source.parent.glob("*/parent"))
               if path.read_text(encoding="utf-8").strip() == plan.old]
    headers = [f"2. Production header {path}: {plan.old} -> {plan.new}" for path in plan.plans]
    stretches = [f"5. stall-watch {unit}: {moves(move)}"
                 for unit, move in zip(plan.units, plan.stall, strict=True)]
    return [f"1. prepare doc/CHECK; git mv {plan.old_doc} -> {plan.new_doc}; Formerly: `{plan.old}`",
            *(headers or ["2. Production headers: no unit plans"]),
            f"3. notifier {moves(plan.timer)}; CHECK {plan.old_doc} -> {plan.new_doc}",
            *(f"4. showrunner state {moves(move)}" for move in plan.state),
            f"4. parent files {plan.state[-1].source.parent}/*/parent: {plan.old} -> {plan.new}",
            *(f"4. parent {path}: {plan.old} -> {plan.new}" for path in parents),
            f"4. held guild roles: holder {plan.old} showrunner -> {plan.new} showrunner",
            *rename_outside.describe(plan.old, plan.new, plan.old_doc, plan.new_doc),
            *(stretches or ["5. stall-watch: no Units rows"]),
            f"6. review pool {moves(plan.review_pool)}",
            (f"7. merge branch {plan.merge_branch} -> {plan.new}; commit {plan.message!r}; push origin {plan.new}; "
             + f"keep {plan.merge_branch} until finish" if plan.renames_branch else
             f"7. merge branch {plan.merge_branch} kept (not named for the production); commit {plan.message!r}; "
             + f"push origin {plan.merge_branch}"),
            f"8. phase notes: {plan.old} -> {plan.new_doc}"]


def _dirty(plan: RenamePlan) -> list[str]:
    """The checkout's changes and untracked files, less the production's own log, which is never committed."""
    doc = plan.new_doc if plan.new_doc.exists() else plan.old_doc
    logs = rename_outside.log_paths(plan.old, plan.new, doc)
    tracked = unit_move.git(plan.checkout, "status", "--porcelain", "--untracked-files=no").splitlines()
    untracked = unit_move.git(plan.checkout, "ls-files", "--others", "--exclude-standard").splitlines()
    return [*tracked, *(path for path in untracked if path not in logs)]


def _preflight(plan: RenamePlan) -> None:
    branch = unit_move.git(plan.checkout, "branch", "--show-current")
    if branch != plan.merge_branch:
        raise RenameRefused(f"showrunner checkout is on {branch or 'detached HEAD'}, not {plan.merge_branch}")
    if _dirty(plan):
        raise RenameRefused(f"{plan.checkout}: checkout must be clean before rename")
    if plan.renames_branch and unit_move.git(plan.checkout, "for-each-ref", "--format=%(refname)", f"refs/heads/{plan.new}"):
        raise RenameRefused(f"branch {plan.new} already exists")
    if plan.renames_branch and unit_move.git(plan.checkout, "ls-remote", "--heads", "origin", f"refs/heads/{plan.new}"):
        raise RenameRefused(f"origin branch {plan.new} already exists")
    for move in (PathMove(plan.old_doc, plan.new_doc), plan.timer, *plan.state[:-1], *plan.stall, plan.review_pool):
        if unit_move.exists(move.destination):
            raise RenameRefused(f"destination already exists: {move.destination}")
    for path in rename_outside.destinations(plan.old, plan.new, plan.old_doc):
        if unit_move.exists(path):
            raise RenameRefused(f"destination already exists: {path}")
    destination = plan.state[-1].destination
    if unit_move.exists(destination) and (destination.is_symlink() or not destination.is_dir()
                                 or any(path.name != "rename.lock" for path in destination.iterdir())):
        raise RenameRefused(f"destination already exists: {destination}")
    prior = plan.state[-1].source / "rename.json"
    if prior.exists() and _object(read_json(prior)).get("done") is not True:
        raise RenameRefused(f"{prior}: finish the previous rename first")


def _rename_doc(plan: RenamePlan) -> None:
    if plan.old_doc.exists():
        text = _formerly(plan.old_doc.read_text(encoding="utf-8"), plan.old)
        if unit_move.exists(plan.new_doc) and plan.new_doc.read_text(encoding="utf-8") != text:
            raise RenameRefused(f"destination already exists: {plan.new_doc}")
        write_text(plan.old_doc, text)
        # CHECK always names a readable doc, including while git mv removes the old path.
        write_text(plan.new_doc, text)
        _rewrite_check(plan.timer.source, plan)
        _ = unit_move.git(plan.checkout, "mv", "-f", "--", _relative(plan, plan.old_doc), _relative(plan, plan.new_doc))
    elif not plan.new_doc.exists():
        raise RenameRefused(f"production doc is missing: {plan.old_doc}")


def _rewrite_plans(plan: RenamePlan) -> None:
    for path in plan.plans:
        text = path.read_text(encoding="utf-8")
        updated = _production_header(text, plan.old, plan.new)
        if updated != text:
            write_text(path, updated)


def _rewrite_check(instance: Path, plan: RenamePlan) -> None:
    conf = instance / "conf"
    checked = showrunners.checked_doc(instance)
    # Compared resolved: a doc under /tmp is /private/tmp on the Mac, and either spelling may be the one recorded.
    docs = (plan.old_doc.resolve(), plan.new_doc.resolve())
    if not isinstance(checked, showrunners.CheckedDoc) or checked.path.resolve() not in docs:
        raise RenameRefused(f"{conf}: CHECK no longer names this production")
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
            if path.name == "rename.lock":
                path.unlink()
                continue
            destination = directory.destination / path.name
            if path.name == "rename.json":
                destination = directory.destination / "rename-history" / f"{plan.old}.json"
            unit_move.move(PathMove(path, destination))
        directory.source.rmdir()
    for path in sorted(directory.destination.parent.glob("*/parent")):
        if path.read_text(encoding="utf-8").strip() == plan.old:
            write_text(path, plan.new + "\n")
    _move_held_roles(roles.ShowrunnerHolder(plan.old), roles.ShowrunnerHolder(plan.new))
    rename_outside.apply(plan.old, plan.new, plan.old_doc, plan.new_doc)


def _move_held_roles(old: roles.Holder, new: roles.Holder, old_checkout: str = "", new_checkout: str = "") -> None:
    """Held guild roles name the renamed production as holder, so resume, rebind and end accept it."""
    try:
        roles.move_holder(old, new, old_checkout, new_checkout, reason="production rename")
    except (OSError, ValueError, SystemExit) as error:
        raise RenameRefused(f"held roles: {error}") from error


def _rename_stall_watch(plan: RenamePlan) -> None:
    for move in plan.stall:
        unit_move.move(move)


def _rename_review_pool(plan: RenamePlan) -> None:
    unit_move.move(plan.review_pool)


def _rename_unit_workers(plan: RenamePlan) -> None:
    from scripts.direct import workers
    root = workers.ledger_path(plan.old, "unused").parent
    units = {path.name.removesuffix(".move.json") if path.name.endswith(".move.json") else path.stem
             for path in root.glob("*.json")}
    for unit in sorted(units):
        workers.move_unit(plan.old, plan.new, unit)


def _check_edits(plan: RenamePlan) -> None:
    allowed = {_relative(plan, path) for path in (plan.old_doc, plan.new_doc, *plan.plans)}
    changed = set(unit_move.git(plan.checkout, "diff", "--name-only", "-z", "HEAD").split("\0")) - {""}
    changed.update(set(unit_move.git(plan.checkout, "ls-files", "--others", "--exclude-standard", "-z").split("\0"))
                   - {""} - rename_outside.log_paths(plan.old, plan.new, plan.new_doc))
    if changed - allowed:
        raise RenameRefused("unrelated checkout changes: " + ", ".join(sorted(changed - allowed)))


def _rename_branch(plan: RenamePlan) -> None:
    if unit_move.git(plan.checkout, "rev-parse", f"refs/heads/{plan.merge_branch}") != plan.base:
        raise RenameRefused(f"{plan.merge_branch} moved during the rename")
    branch = unit_move.git(plan.checkout, "branch", "--show-current")
    if branch not in (plan.merge_branch, plan.branch):
        raise RenameRefused(f"showrunner checkout switched to {branch}")
    _check_edits(plan)
    if not unit_move.git(plan.checkout, "for-each-ref", "--format=%(refname)", f"refs/heads/{plan.branch}"):
        _ = unit_move.git(plan.checkout, "branch", plan.branch, plan.base)
    tip = unit_move.git(plan.checkout, "rev-parse", f"refs/heads/{plan.branch}")
    committed = (tip != plan.base and unit_move.git(plan.checkout, "show", "-s", "--format=%s", tip) == plan.message
                 and unit_move.git(plan.checkout, "rev-parse", f"{tip}^") == plan.base)
    if tip != plan.base and not committed:
        raise RenameRefused(f"branch {plan.branch} moved during the rename")
    if branch != plan.branch:
        _ = unit_move.git(plan.checkout, "switch", plan.branch)
    if not committed:
        if plan.renames_branch:
            write_text(plan.new_doc, _merge_line(plan.new_doc.read_text(encoding="utf-8"), plan.new))
        paths = list(dict.fromkeys(_relative(plan, path) for path in (plan.old_doc, plan.new_doc, *plan.plans)))
        old_path = _relative(plan, plan.old_doc)
        if not unit_move.exists(plan.old_doc) and unit_move.git(plan.checkout, "ls-files", "-z", "--", old_path):
            _ = unit_move.git(plan.checkout, "rm", "--cached", "--quiet", "--", old_path)
        _ = unit_move.git(plan.checkout, "add", "--", *[path for path in paths if (plan.checkout / path).exists()])
        _ = unit_move.git(plan.checkout, "commit", "-m", plan.message)
    elif _dirty(plan):
        raise RenameRefused("checkout changed after the rename commit")
    _ = unit_move.git(plan.checkout, "push", "--set-upstream", "origin", f"{plan.branch}:refs/heads/{plan.branch}")


def _retarget_phase_notes(plan: RenamePlan) -> None:
    from scripts.direct import phase_table

    _ = phase_table.retarget_phase_notes(plan.old, plan.new_doc)


APPLY_STEPS = (_rename_doc, _rewrite_plans, _rename_timer, _rename_showrunner_state,
               _rename_stall_watch, _rename_review_pool, _rename_unit_workers, _rename_branch,
               _retarget_phase_notes)
APPLY_STEP_NAMES = tuple(step.__name__ for step in APPLY_STEPS)
# The step list of journals that recorded a count; its order maps a count to step names.
COUNTED_APPLY_STEP_NAMES = ("_rename_doc", "_rewrite_plans", "_rename_timer", "_rename_showrunner_state",
                            "_rename_stall_watch", "_rename_review_pool", "_rename_branch")


def apply(old: str, new: str) -> RenamePlan:
    """Perform each atomic move and record completed steps so a refusal can be resumed."""
    result = plan(old, new)
    if not result.journal.exists():
        _preflight(result)
    result.journal.parent.mkdir(parents=True, exist_ok=True)
    with unit_move.production_lock():
        for slug in {result.old, result.new}:
            if rename_state.unfinished(rename_state.unit_journal(slug)):
                raise RenameRefused(f"{slug}: a unit rename is unfinished; finish it first")
        if result.journal.exists():
            result, completed = _load(result.journal, old, new)
        else:
            _preflight(result)
            completed = ()
            _save(result, completed)
        for run in APPLY_STEPS:
            if run.__name__ in completed:
                continue
            run(result)
            completed = (*completed, run.__name__)
            _save(result, completed)
    return result


@contextmanager
def _rename_journal(new: str) -> Generator[tuple[RenamePlan, dict[str, object]]]:
    new = showrunners.canonical_slug(new)
    _validate_slug(new)
    path = rename_state.production_journal(new)
    if not path.exists():
        raise RenameRefused(f"{new} has no rename journal")
    with unit_move.production_lock():
        result, completed = _load(path, new, new)
        missing = [name for name in APPLY_STEP_NAMES if name not in completed]
        if missing:
            raise RenameRefused(f"{new}: apply must complete {', '.join(missing)} first")
        yield result, _object(read_json(path))


def _unit_row(plan: RenamePlan, name: str) -> dict[str, str]:
    rows = [row for row in production_units.unit_table(plan.new_doc.read_text(encoding="utf-8").splitlines())
            if production_units.cell_value(row.get("Unit", "")) == name]
    if len(rows) != 1:
        raise RenameRefused(f"{name}: expected one Units row")
    if production_units.plan_cell_is_retired(rows[0].get("Plan", "")):
        raise RenameRefused(f"{name} is retired and has no live worktree")
    return rows[0]


def _stopped_unit(plan: RenamePlan, name: str) -> None:
    try:
        found = unit_lookup.marked_unit(plan.new, name)
    except OSError as error:
        raise RenameRefused(f"{name}: cannot tell whether Claude is running: {error}") from error
    state = found.claude if found else unit_lookup.ClaudeNotRunning()
    if isinstance(state, unit_lookup.LiveClaude):
        raise RenameRefused(f"{name} has a live Claude; wait until its run stops")
    if isinstance(state, unit_lookup.ClaudeUnknown):
        raise RenameRefused(f"{name}: cannot tell whether Claude is running: {state.reason}")


def _stopped_restore(plan: RenamePlan, name: str) -> UnitRestore:
    records = [*rename_unit.shutdown_records(plan.old, (name,)),
               *rename_unit.shutdown_records(plan.new, (name,))]
    if not records:
        return UnitWithoutRestore(kind="absent")
    if len(records) != 1:
        raise RenameRefused(f"{name} has {len(records)} shutdown records; settle them first")
    saved = records[0]
    if saved["progress"]["kind"] not in rename_unit.STOPPED:
        raise RenameRefused(f"{name}'s shutdown record reads {saved['progress']['kind']}; settle it first")
    return StoppedUnitRestore(kind="stopped", login=saved["login"], session_id=saved["session"]["session_id"])


def _saved_restore(record: dict[str, object]) -> UnitRestore:
    restore = _object(record["restore"])
    if restore.get("kind") == "absent":
        return UnitWithoutRestore(kind="absent")
    if restore.get("kind") != "stopped":
        raise RenameRefused("invalid unit restore in rename journal")
    return StoppedUnitRestore(kind="stopped", login=_string(restore, "login"),
                              session_id=_string(restore, "session_id"))


def _repoint_unit_restore(plan: RenamePlan, name: str, record: dict[str, object],
                          restore: StoppedUnitRestore) -> None:
    import herdr

    old, new = _string(record, "old_worktree"), _string(record, "new_worktree")
    branch = _string(record, "new_branch")
    note = (f"While you were paused the production moved your unit: your worktree is now {new} "
            f"on branch {branch}. {old} no longer exists; wherever your instructions name it, use {new}.")
    host_names = {herdr.generated_session_name(_string(record, "old_branch")):
                  herdr.generated_session_name(branch)}

    def change(saved: SessionShutdown) -> None:
        host = saved["session"]["host"]
        if host["kind"] != "unit" or host["unit"] != name or host["production"] not in (plan.old, plan.new):
            raise RenameRefused(f"shutdown record {restore['session_id']} is not {name}'s")
        rename_unit.repoint_shutdown_worktree(saved, old, new, host_names, note)
        host["production"] = plan.new
        if host["doc"] == str(plan.old_doc):
            host["doc"] = str(plan.new_doc)

    try:
        _ = shutdown_record.update_session(restore["login"], restore["session_id"], change)
    except (shutdown_record.NoLiveRecord, shutdown_record.InvalidRecord, OSError) as error:
        raise RenameRefused(f"{name}'s shutdown record: {error}") from error


def _prepare_unit(plan: RenamePlan, name: str, row: dict[str, str]) -> dict[str, object]:
    for field in ("Worktree", "Branch"):
        if field not in row:
            raise RenameRefused(f"{name}: Units table lacks {field} column")
    branch = production_units.cell_value(row.get("Branch", ""))
    if not branch.startswith(plan.old + "-"):
        raise RenameRefused(f"{name}: branch {branch} does not start with {plan.old}-")
    stem = branch[len(plan.old) + 1:]
    worktree = Path(production_units.cell_value(row.get("Worktree", ""))).expanduser().resolve()
    if not (worktree / ".git").is_file():
        raise RenameRefused(f"{name}: no linked worktree at {worktree}")
    new_branch = f"{plan.new}-{stem}"
    if unit_move.branch_tip(plan.checkout, new_branch) or unit_move.remote_tip(plan.checkout, new_branch):
        raise RenameRefused(f"branch {new_branch} already exists")
    suffix = f"-{plan.old}-{stem}"
    repository = (worktree.name[:-len(suffix)] if worktree.name.endswith(suffix) else plan.checkout.name)
    basename = f"{repository}-{plan.new}-{stem}"
    destination = worktree.with_name(basename)
    if unit_move.exists(destination):
        raise RenameRefused(f"destination already exists: {destination}")
    if unit_move.git(worktree, "branch", "--show-current") != branch:
        raise RenameRefused(f"{worktree} is not on {branch}")
    if unit_move.git(plan.checkout, "branch", "--show-current") != plan.branch or _dirty(plan):
        raise RenameRefused(f"showrunner checkout must be clean and on the merge branch {plan.branch}")
    return {"old_branch": branch, "new_branch": new_branch, "old_worktree": str(worktree),
            "new_worktree": str(destination), "tip": unit_move.git(worktree, "rev-parse", "HEAD"),
            "base": unit_move.git(plan.checkout, "rev-parse", "HEAD"), "step": 0}


def _unit_doc(plan: RenamePlan, name: str, record: dict[str, object]) -> None:
    lines = plan.new_doc.read_text(encoding="utf-8").splitlines(keepends=True)
    headings = production_units.unit_headings([line.rstrip("\n") for line in lines])
    for field in ("Worktree", "Branch"):
        if field not in headings:
            raise RenameRefused(f"{name}: Units table lacks {field} column")
    in_units = False
    for index, line in enumerate(lines):
        if line.startswith("## "):
            in_units = line.strip() == "## Units"
        if not in_units or not line.startswith("|"):
            continue
        cells = production_units.row_cells(line)
        if cells and production_units.cell_value(cells[0]) == name:
            for field, value in (("Worktree", _string(record, "new_worktree")),
                                 ("Branch", _string(record, "new_branch"))):
                column = headings.index(field)
                cells[column] = f"`{value}`"
            lines[index] = "| " + " | ".join(cells) + " |\n"
            write_text(plan.new_doc, "".join(lines))
            return
    raise RenameRefused(f"{name}: Units row disappeared")


def _commit_unit(plan: RenamePlan, name: str, record: dict[str, object]) -> None:
    _check_unit_tip(plan, record)
    message = f"chore({plan.new}): rename unit {name} from {plan.old}"
    if unit_move.git(plan.checkout, "branch", "--show-current") != plan.branch:
        raise RenameRefused(f"showrunner checkout switched away from the merge branch {plan.branch}")
    path = _relative(plan, plan.new_doc)
    rows = production_units.unit_table(unit_move.git(plan.checkout, "show", f"HEAD:{path}").splitlines())
    matching = [row for row in rows if production_units.cell_value(row.get("Unit", "")) == name]
    committed = (len(matching) == 1
                 and production_units.cell_value(matching[0].get("Worktree", "")) == _string(record, "new_worktree")
                 and production_units.cell_value(matching[0].get("Branch", "")) == _string(record, "new_branch"))
    changed = set(unit_move.git(plan.checkout, "diff", "--name-only", "-z", "HEAD").split("\0")) - {""}
    changed.update(set(unit_move.git(plan.checkout, "diff", "--cached", "--name-only", "-z", "HEAD").split("\0")) - {""})
    untracked = [path for path in unit_move.git(plan.checkout, "ls-files", "--others", "--exclude-standard").splitlines()
                 if path not in rename_outside.log_paths(plan.old, plan.new, plan.new_doc)]
    if changed - {path} or untracked or (committed and changed):
        raise RenameRefused("unrelated checkout changes during unit rename")
    if not committed:
        _unit_doc(plan, name, record)
        _ = unit_move.git(plan.checkout, "add", "--", path)
        _ = unit_move.git(plan.checkout, "commit", "-m", message)
    _check_unit_tip(plan, record)
    _ = unit_move.git(plan.checkout, "push", "origin", f"{plan.branch}:refs/heads/{plan.branch}")


def _repoint_unit_workers(plan: RenamePlan, name: str, record: dict[str, object], *, dry_run: bool = False) -> None:
    from scripts.direct import workers
    changes = workers.repoint_unit_worktree(
        plan.new, name, Path(_string(record, "old_worktree")), Path(_string(record, "new_worktree")),
        _string(record, "new_branch"), from_production=plan.old, dry_run=dry_run)
    for change in changes:
        print(f"{name}: {'would change' if dry_run else 'changed'} {change}")
    if not changes:
        print(f"{name}: workers have nothing to change")


def repair_workers(new: str, *, dry_run: bool = False) -> None:
    """Repair saved worker paths from journaled moves or current Units rows."""
    from scripts.direct import workers
    new = showrunners.canonical_slug(new)
    _validate_slug(new)
    path = rename_state.production_journal(new)
    with nullcontext() if dry_run else unit_move.production_lock():
        journal = _object(read_json(path)) if path.exists() else {}
        if journal:
            doc = Path(_string(journal, "new_doc"))
        else:
            checked = showrunners.checked_doc(showrunners.NOTIFIER_STATE_DIR / f"showrunner-{new}")
            if isinstance(checked, showrunners.NoCheckedDoc):
                raise RenameRefused(f"{new}: no production doc: {checked.reason}")
            doc = checked.path
        production = add_unit.read_production(doc)
        text = doc.read_text(encoding="utf-8")
        former_names = [_string(journal, "old")] if journal else []
        for line in text.splitlines():
            if line.startswith("- **Formerly:**"):
                former_names.extend(cast(list[str], re.findall(r"`([^`]+)`", line)))
        former = tuple(dict.fromkeys(former_names))
        units = tuple(workers.UnitWorktree(new, production_units.cell_value(row.get("Unit", "")),
            production.checkout / Path(production_units.cell_value(row.get("Worktree", ""))).expanduser(),
            production_units.cell_value(row.get("Branch", "")))
            for row in production_units.unit_table(text.splitlines())
            if not production_units.plan_cell_is_retired(row.get("Plan", ""))
            and production_units.cell_value(row.get("Worktree", "")) not in ("", "—", "-"))
        found = workers.find_moved_unit_runs(units, former)
        for skipped in found.skipped:
            print(skipped)
        records = _object(journal.get("unit_moves", {}))
        journaled: set[tuple[str, Path]] = set()
        reported: set[str] = set()
        for name, raw in sorted(records.items()):
            record = _object(raw)
            completed = record.get("step")
            if type(completed) is not int or completed < 2:
                continue
            old = Path(_string(record, "old_worktree"))
            changes = workers.repoint_unit_worktree(new, name, old, Path(_string(record, "new_worktree")),
                _string(record, "new_branch"), from_production=former[0] if former else new, dry_run=dry_run)
            for change in changes:
                print(f"{name}: {'would change' if dry_run else 'changed'} {change}")
            if not changes:
                print(f"{name}: workers have nothing to change")
            reported.add(name)
            journaled.add((name, old))
            if not dry_run and not record.get("workers_repointed", False):
                record["workers_repointed"] = True
                write_json(path, journal)
        for move in found.moves:
            name = move.current.unit
            if (name, move.old) in journaled:
                continue
            changes = workers.repoint_moved_unit_runs(move, from_production=former[0] if former else new, dry_run=dry_run)
            for change in changes:
                print(f"{name}: {'would change' if dry_run else 'changed'} {change}")
            if not changes and name not in reported:
                print(f"{name}: workers have nothing to change")
            reported.add(name)
        if not reported:
            print(f"{new}: no moved unit was found: no unit run names an old worktree"
                  + (" with one matching Units row" if found.skipped else ""))
        for current in units:
            if current.unit not in reported:
                print(f"{current.unit}: workers have nothing to change")


def unit(new: str, name: str) -> None:
    """Move one stopped unit, resuming recorded branch, worktree and configuration steps."""
    with _rename_journal(new) as (result, journal):
        row = _unit_row(result, name)
        records = _object(journal.get("unit_moves", {}))
        if name in records:
            record = _object(records[name])
        else:
            if journal["done"]:
                raise RenameRefused("rename is already finished")
            if any(_object(value).get("step") != 5 for value in records.values()):
                raise RenameRefused("complete interrupted unit moves before starting another unit")
            _stopped_unit(result, name)
            restore = _stopped_restore(result, name)
            record = _prepare_unit(result, name, row)
            record["restore"] = restore
            records[name] = record
            journal["unit_moves"] = records
            write_json(result.journal, journal)
        if "restore" not in record:
            record["restore"] = _stopped_restore(result, name)
            write_json(result.journal, journal)
        restore = _saved_restore(record)
        if not record.get("restore_repointed", False):
            # Check again before changing a branch or worktree on a resumed move.
            if _stopped_restore(result, name) != restore:
                raise RenameRefused(f"{name}'s shutdown record changed during the move")
        completed = record.get("step")
        if type(completed) is not int or not 0 <= completed <= 5:
            raise RenameRefused(f"{name}: invalid unit rename progress")
        _retarget_phase_notes(result)
        if completed < 5:
            _stopped_unit(result, name)
            _check_unit_tip(result, record)
        if completed < 1:
            _push_unit_branch(result, record)
            record["step"] = 1
            write_json(result.journal, journal)
        if completed < 2:
            _stopped_unit(result, name)
            _move_unit_worktree(result, record)
            record["step"] = 2
            write_json(result.journal, journal)
        if not record.get("workers_repointed", False):
            _repoint_unit_workers(result, name, record)
            record["workers_repointed"] = True
            write_json(result.journal, journal)
        if not record.get("restore_repointed", False):
            if restore["kind"] == "stopped":
                _repoint_unit_restore(result, name, record, restore)
            record["restore_repointed"] = True
            write_json(result.journal, journal)
        if completed < 3:
            _move_unit_configs(record)
            _move_held_roles(roles.UnitHolder(result.old, name), roles.UnitHolder(result.new, name),
                             _string(record, "old_worktree"), _string(record, "new_worktree"))
            record["step"] = 3
            write_json(result.journal, journal)
        if completed < 4:
            _commit_unit(result, name, record)
            record["step"] = 4
            write_json(result.journal, journal)
        if completed < 5:
            _check_unit_tip(result, record)
            if unit_move.remote_tip(result.checkout, _string(record, "new_branch")) != _string(record, "tip"):
                raise RenameRefused("new unit branch changed before old branch retirement")
            unit_move.delete_branch(result.checkout, _string(record, "old_branch"), _string(record, "tip"))
            record["step"] = 5
            write_json(result.journal, journal)
        if restore["kind"] == "stopped":
            print(f"relaunch: shutdown.py restart --only {shlex.quote(restore['session_id'])}")
        else:
            print(f"{name} has no shutdown record to relaunch from")
        print(f"{name} moved to {_string(record, 'new_worktree')}; add_unit writes the herdr mark at next launch")


def finish(new: str) -> None:
    """Finish once all non-retired unit rows have moved: retire the old merge branch when it took the new name."""
    with _rename_journal(new) as (result, journal):
        if journal["done"]:
            return
        rows = production_units.unit_table(result.new_doc.read_text(encoding="utf-8").splitlines())
        remaining = [production_units.cell_value(row.get("Unit", "")) for row in rows
                     if not production_units.plan_cell_is_retired(row.get("Plan", ""))
                     and production_units.cell_value(row.get("Branch", "")).startswith(result.old + "-")]
        if remaining:
            raise RenameRefused("units still use old branches: " + ", ".join(remaining))
        records = _object(journal.get("unit_moves", {}))
        if any(_object(value).get("step") != 5 for value in records.values()):
            raise RenameRefused("complete interrupted unit moves before finish")
        if unit_move.git(result.checkout, "branch", "--show-current") != result.branch:
            raise RenameRefused(f"showrunner checkout must be on the merge branch {result.branch} before finish")
        if result.renames_branch:
            unit_move.delete_remote_branch(result.checkout, result.merge_branch, result.base)
            journal["finish_step"] = 1
            write_json(result.journal, journal)
            unit_move.delete_local_branch(result.checkout, result.merge_branch, result.base)
        journal["finish_step"] = 2
        journal["done"] = True
        write_json(result.journal, journal)
        print(f"{result.new} rename finished; " + (f"retired {result.merge_branch}" if result.renames_branch
                                                   else f"kept merge branch {result.merge_branch}"))


def repair_notes(old: str, new: str) -> list[Path]:
    """Repair phase notes without changing the production or its rename journal."""
    from scripts.direct import phase_table

    _validate_slug(old)
    new = showrunners.canonical_slug(new)
    _validate_slug(new)
    doc = showrunners.timer_docs().get(new)
    if doc is None:
        raise RenameRefused(f"{new} has no production doc")
    return phase_table.retarget_phase_notes(old, doc)


def main(argv: list[str]) -> int:
    from scripts.direct.workers import WorkerUnavailable
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("plan", "apply", "repair-notes"):
        command = actions.add_parser(name)
        _ = command.add_argument("old")
        _ = command.add_argument("new")
    command = actions.add_parser("unit")
    _ = command.add_argument("new")
    _ = command.add_argument("unit")
    command = actions.add_parser("finish")
    _ = command.add_argument("new")
    command = actions.add_parser("repair-workers", help="repair worker paths for units moved before this fix")
    _ = command.add_argument("new", metavar="production")
    _ = command.add_argument("--dry-run", action="store_true")
    command = actions.add_parser("checkout", help="move the showrunner checkout through a shutdown restart")
    _ = command.add_argument("new", metavar="slug")
    _ = command.add_argument("path", nargs="?", default="")
    args = parser.parse_args(argv)
    action, new = cast(str, args.action), cast(str, args.new)
    try:
        if action == "repair-notes":
            paths = repair_notes(cast(str, args.old), new)
            for path in paths:
                print(path)
            if not paths:
                print("no phase notes to repair")
        elif action == "unit":
            unit(new, cast(str, args.unit))
        elif action == "repair-workers":
            repair_workers(new, dry_run=cast(bool, args.dry_run))
        elif action == "finish":
            finish(new)
        elif action == "checkout":
            rename_checkout.checkout(new, cast(str, args.path))
        else:
            old = cast(str, args.old)
            result = plan(old, new) if action == "plan" else apply(old, new)
            for line in _describe(result):
                print(line)
            if action == "apply":
                print(f"{result.old} is now {result.new}; " + (f"keep {result.merge_branch} until finish" if result.renames_branch else f"merge branch {result.merge_branch} kept"))
    except (RenameRefused, production_units.Refusal, tidy_wrapped.TidyRefused, WorkerUnavailable, OSError, UnicodeError) as error:
        print(f"rename: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
