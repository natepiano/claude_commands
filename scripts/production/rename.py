#!/usr/bin/env python3
"""Plan and resume a production rename, keeping its old merge branch until finish."""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import shlex
import subprocess
import sys
import time
from contextlib import contextmanager
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
import add_unit  # noqa: E402
import showrunners  # noqa: E402
import stall_watch  # noqa: E402
import tidy_wrapped  # noqa: E402
import unit_lookup  # noqa: E402
from rename_state import RenameRefused, _read_json, _write_json, _write_lines  # pyright: ignore[reportPrivateUsage]

REVIEW_POOL_STATE = Path(os.environ.get("REVIEW_POOL_STATE") or Path.home() / ".local/state/review-pool")


class PathMove(NamedTuple):
    source: Path
    destination: Path


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


def _git(checkout: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(checkout), *arguments], capture_output=True,
                            text=True, check=False)
    if result.returncode:
        raise RenameRefused(f"git {' '.join(arguments)}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.rstrip("\n")


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


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
            value = add_unit.cell_value(raw.strip())
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


def _plan_paths(cell: str, checkout: Path) -> tuple[Path, ...]:
    names = [match[1] for match in re.finditer(r"`([^`]+)`", cell)]
    if not names:
        if re.search(r"\s", cell.strip()):
            return ()
        names = [cell.strip()]
    paths: list[Path] = []
    for name in names:
        path = Path(name).expanduser()
        path = (path if path.is_absolute() else checkout / path).resolve()
        try:
            relative = path.relative_to(checkout)
        except ValueError:
            continue
        if relative.parts[:2] != ("docs", "plans") or path.suffix != ".md" or not path.is_file():
            continue
        if _git(checkout, "ls-files", "-z", "--", f":(literal){relative}"):
            paths.append(path)
    return tuple(paths)


def _build(old: str, new: str) -> RenamePlan:
    _validate_slug(new)
    old = showrunners.canonical_slug(old)
    _validate_slug(old)
    if old == new:
        raise RenameRefused(f"{new} is already the production's name")
    known = showrunners.timer_slugs()
    if (_exists(showrunners.NOTIFIER_STATE_DIR / f"showrunner-{new}")
            or new in known or showrunners.canonical_slug(new) != new):
        raise RenameRefused(f"{new} is already a current or former production slug")
    if old not in known:
        raise RenameRefused(f"{old} has no update timer")
    instance = showrunners.NOTIFIER_STATE_DIR / f"showrunner-{old}"
    checked = showrunners.checked_doc(instance)
    if isinstance(checked, showrunners.NoCheckedDoc):
        raise RenameRefused(f"{old} has no readable update timer: {checked.reason}")
    production = add_unit.read_production(checked.path)
    _ = _git(production.checkout, "check-ref-format", "--branch", new)
    if showrunners.production_slug(str(production.doc)) != old:
        raise RenameRefused(f"{old}'s timer names a different production doc: {production.doc}")
    lines = production.doc.read_text(encoding="utf-8").splitlines()
    units: list[str] = []
    plans: list[Path] = []
    for row in add_unit.unit_table(lines):
        unit = add_unit.cell_value(row.get("Unit", ""))
        if not unit:
            raise RenameRefused("Units row has no unit name")
        units.append(unit)
        raw = row.get("Plan", "")
        if add_unit.plan_cell_is_retired(raw):
            continue
        if raw.strip() in ("", "—", "-", "permanent"):
            continue
        for path in _plan_paths(raw, production.checkout):
            if path not in plans:
                plans.append(path)
    result = RenamePlan(
        old, new, production.doc, production.doc.with_name(f"{new}-production.md"), production.checkout,
        production.merge_branch, _git(production.checkout, "rev-parse", f"refs/heads/{production.merge_branch}"),
        tuple(units), tuple(plans), PathMove(instance, instance.with_name(f"showrunner-{new}")),
        tuple(PathMove(source, destination) for source, destination in
              zip(tidy_wrapped.owned_paths(old), tidy_wrapped.owned_paths(new), strict=True)),
        tuple(PathMove(stall_watch.stretch_path(old, unit), stall_watch.stretch_path(new, unit)) for unit in units),
        PathMove(REVIEW_POOL_STATE.expanduser() / old, REVIEW_POOL_STATE.expanduser() / new))
    for path in (result.old_doc, *result.plans):
        _ = _git(result.checkout, "ls-files", "--error-unmatch", "--", _relative(result, path))
        _ = path.read_text(encoding="utf-8")
    return result


def _move_record(move: PathMove) -> dict[str, str]:
    return {"source": str(move.source), "destination": str(move.destination)}


def _save(plan: RenamePlan, step: int) -> None:
    _write_json(plan.journal, {
        "version": 1, "old": plan.old, "new": plan.new, "old_doc": str(plan.old_doc),
        "new_doc": str(plan.new_doc), "checkout": str(plan.checkout), "merge_branch": plan.merge_branch,
        "base": plan.base, "units": list(plan.units), "plans": [str(path) for path in plan.plans],
        "timer": _move_record(plan.timer), "state": [_move_record(move) for move in plan.state],
        "stall": [_move_record(move) for move in plan.stall], "review_pool": _move_record(plan.review_pool),
        "step": step, "done": False,
    })


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


def _load(path: Path, old: str, new: str) -> tuple[RenamePlan, int]:
    fields = _object(_read_json(path))
    if fields.get("version") != 1 or fields.get("new") != new:
        raise RenameRefused(f"{path}: incompatible rename journal")
    recorded_old = _string(fields, "old")
    if old not in (recorded_old, new) and showrunners.canonical_slug(old) not in (recorded_old, new):
        raise RenameRefused(f"{path}: rename is from {recorded_old}, not {old}")
    state = _read_moves(fields.get("state"))
    step = fields.get("step")
    if len(state) != 4 or type(step) is not int or not 0 <= step <= 7 or not isinstance(fields.get("done"), bool):
        raise RenameRefused(f"{path}: invalid rename progress")
    result = RenamePlan(recorded_old, new, Path(_string(fields, "old_doc")), Path(_string(fields, "new_doc")),
                        Path(_string(fields, "checkout")), _string(fields, "merge_branch"), _string(fields, "base"),
                        _strings(fields, "units"), tuple(Path(item) for item in _strings(fields, "plans")),
                        _read_move(fields.get("timer")), state, _read_moves(fields.get("stall")),
                        _read_move(fields.get("review_pool")))
    if result.journal != path:
        raise RenameRefused(f"{path}: journal state directory changed")
    return result, step


def plan(old: str, new: str) -> RenamePlan:
    """Read the production and list its moves, without creating or rewriting anything."""
    _validate_slug(new)
    journal = tidy_wrapped.owned_paths(new)[-1] / "rename.json"
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
            *(stretches or ["5. stall-watch: no Units rows"]),
            f"6. review pool {moves(plan.review_pool)}",
            f"7. merge branch {plan.merge_branch} -> {plan.new}; commit {plan.message!r}; push origin {plan.new}; "
            + f"keep {plan.merge_branch} until finish"]


def _preflight(plan: RenamePlan) -> None:
    branch = _git(plan.checkout, "branch", "--show-current")
    if branch != plan.merge_branch:
        raise RenameRefused(f"showrunner checkout is on {branch or 'detached HEAD'}, not {plan.merge_branch}")
    if _git(plan.checkout, "status", "--porcelain"):
        raise RenameRefused(f"{plan.checkout}: checkout must be clean before rename")
    if _git(plan.checkout, "for-each-ref", "--format=%(refname)", f"refs/heads/{plan.new}"):
        raise RenameRefused(f"branch {plan.new} already exists")
    if _git(plan.checkout, "ls-remote", "--heads", "origin", f"refs/heads/{plan.new}"):
        raise RenameRefused(f"origin branch {plan.new} already exists")
    for move in (PathMove(plan.old_doc, plan.new_doc), plan.timer, *plan.state[:-1], *plan.stall, plan.review_pool):
        if _exists(move.destination):
            raise RenameRefused(f"destination already exists: {move.destination}")
    destination = plan.state[-1].destination
    if _exists(destination) and (destination.is_symlink() or not destination.is_dir()
                                 or any(path.name != "rename.lock" for path in destination.iterdir())):
        raise RenameRefused(f"destination already exists: {destination}")
    prior = plan.state[-1].source / "rename.json"
    if prior.exists() and _object(_read_json(prior)).get("done") is not True:
        raise RenameRefused(f"{prior}: finish the previous rename first")


def _move(move: PathMove) -> None:
    if not _exists(move.source):
        return
    if _exists(move.destination):
        raise RenameRefused(f"destination already exists: {move.destination}")
    move.destination.parent.mkdir(parents=True, exist_ok=True)
    _ = move.source.rename(move.destination)


def _rename_doc(plan: RenamePlan) -> None:
    if plan.old_doc.exists():
        text = _formerly(plan.old_doc.read_text(encoding="utf-8"), plan.old)
        if _exists(plan.new_doc) and plan.new_doc.read_text(encoding="utf-8") != text:
            raise RenameRefused(f"destination already exists: {plan.new_doc}")
        _write_lines(plan.old_doc, [text])
        # CHECK always names a readable doc, including while git mv removes the old path.
        _write_lines(plan.new_doc, [text])
        _rewrite_check(plan.timer.source, plan)
        _ = _git(plan.checkout, "mv", "-f", "--", _relative(plan, plan.old_doc), _relative(plan, plan.new_doc))
    elif not plan.new_doc.exists():
        raise RenameRefused(f"production doc is missing: {plan.old_doc}")


def _rewrite_plans(plan: RenamePlan) -> None:
    for path in plan.plans:
        text = path.read_text(encoding="utf-8")
        updated = _production_header(text, plan.old, plan.new)
        if updated != text:
            _write_lines(path, [updated])


def _rewrite_check(instance: Path, plan: RenamePlan) -> None:
    conf = instance / "conf"
    checked = showrunners.checked_doc(instance)
    if not isinstance(checked, showrunners.CheckedDoc) or checked.path not in (plan.old_doc, plan.new_doc):
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
                    if value == str(plan.old_doc):
                        updated_command = command[:token.start()] + shlex.quote(str(plan.new_doc)) + command[token.end():]
                        lines[index] = "CHECK=" + updated_command
                    break
                follows_script = Path(value).name == "production_check.sh"
    updated = "".join(lines)
    if updated != text:
        _write_lines(conf, [updated])
    checked = showrunners.checked_doc(instance)
    if not isinstance(checked, showrunners.CheckedDoc) or checked.path != plan.new_doc:
        raise RenameRefused(f"{conf}: CHECK does not name {plan.new_doc}")


def _rename_timer(plan: RenamePlan) -> None:
    instance = plan.timer.source if plan.timer.source.exists() else plan.timer.destination
    _rewrite_check(instance, plan)
    _move(plan.timer)


def _rename_showrunner_state(plan: RenamePlan) -> None:
    for move in plan.state[:-1]:
        _move(move)
    directory = plan.state[-1]
    if directory.source.exists():
        for path in sorted(directory.source.iterdir()):
            if path.name == "rename.lock":
                path.unlink()
                continue
            destination = directory.destination / path.name
            if path.name == "rename.json":
                destination = directory.destination / "rename-history" / f"{plan.old}.json"
            _move(PathMove(path, destination))
        directory.source.rmdir()
    for path in sorted(directory.destination.parent.glob("*/parent")):
        if path.read_text(encoding="utf-8").strip() == plan.old:
            _write_lines(path, [plan.new + "\n"])


def _rename_stall_watch(plan: RenamePlan) -> None:
    for move in plan.stall:
        _move(move)


def _rename_review_pool(plan: RenamePlan) -> None:
    _move(plan.review_pool)


def _check_edits(plan: RenamePlan) -> None:
    allowed = {_relative(plan, path) for path in (plan.old_doc, plan.new_doc, *plan.plans)}
    changed = set(_git(plan.checkout, "diff", "--name-only", "-z", "HEAD").split("\0")) - {""}
    changed.update(set(_git(plan.checkout, "ls-files", "--others", "--exclude-standard", "-z").split("\0")) - {""})
    if changed - allowed:
        raise RenameRefused("unrelated checkout changes: " + ", ".join(sorted(changed - allowed)))


def _rename_branch(plan: RenamePlan) -> None:
    if _git(plan.checkout, "rev-parse", f"refs/heads/{plan.merge_branch}") != plan.base:
        raise RenameRefused(f"{plan.merge_branch} moved during the rename")
    branch = _git(plan.checkout, "branch", "--show-current")
    if branch not in (plan.merge_branch, plan.new):
        raise RenameRefused(f"showrunner checkout switched to {branch}")
    _check_edits(plan)
    if not _git(plan.checkout, "for-each-ref", "--format=%(refname)", f"refs/heads/{plan.new}"):
        _ = _git(plan.checkout, "branch", plan.new, plan.base)
    tip = _git(plan.checkout, "rev-parse", f"refs/heads/{plan.new}")
    committed = (tip != plan.base and _git(plan.checkout, "show", "-s", "--format=%s", tip) == plan.message
                 and _git(plan.checkout, "rev-parse", f"{tip}^") == plan.base)
    if tip != plan.base and not committed:
        raise RenameRefused(f"branch {plan.new} moved during the rename")
    if branch != plan.new:
        _ = _git(plan.checkout, "switch", plan.new)
    if not committed:
        _write_lines(plan.new_doc, [_merge_line(plan.new_doc.read_text(encoding="utf-8"), plan.new)])
        paths = list(dict.fromkeys(_relative(plan, path) for path in (plan.old_doc, plan.new_doc, *plan.plans)))
        old_path = _relative(plan, plan.old_doc)
        if not _exists(plan.old_doc) and _git(plan.checkout, "ls-files", "-z", "--", old_path):
            _ = _git(plan.checkout, "rm", "--cached", "--quiet", "--", old_path)
        _ = _git(plan.checkout, "add", "--", *[path for path in paths if (plan.checkout / path).exists()])
        _ = _git(plan.checkout, "commit", "-m", plan.message)
    elif _git(plan.checkout, "status", "--porcelain"):
        raise RenameRefused("checkout changed after the rename commit")
    _ = _git(plan.checkout, "push", "--set-upstream", "origin", f"{plan.new}:refs/heads/{plan.new}")


def apply(old: str, new: str) -> RenamePlan:
    """Perform each atomic move and record completed steps so a refusal can be resumed."""
    result = plan(old, new)
    if not result.journal.exists():
        _preflight(result)
    result.journal.parent.mkdir(parents=True, exist_ok=True)
    with (result.journal.parent / "rename.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if result.journal.exists():
            result, completed = _load(result.journal, old, new)
        else:
            _preflight(result)
            _save(result, 0)
            completed = 0
        steps = (_rename_doc, _rewrite_plans, _rename_timer, _rename_showrunner_state,
                 _rename_stall_watch, _rename_review_pool, _rename_branch)
        for number, run in enumerate(steps, start=1):
            if number <= completed:
                continue
            run(result)
            _save(result, number)
    return result


@contextmanager
def _rename_journal(new: str) -> Generator[tuple[RenamePlan, dict[str, object]]]:
    new = showrunners.canonical_slug(new)
    _validate_slug(new)
    path = tidy_wrapped.owned_paths(new)[-1] / "rename.json"
    if not path.exists():
        raise RenameRefused(f"{new} has no rename journal")
    with (path.parent / "rename.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result, completed = _load(path, new, new)
        if completed != 7:
            raise RenameRefused(f"{new}: apply must complete step 7 first")
        yield result, _object(_read_json(path))


def _unit_row(plan: RenamePlan, name: str) -> dict[str, str]:
    rows = [row for row in add_unit.unit_table(plan.new_doc.read_text(encoding="utf-8").splitlines())
            if add_unit.cell_value(row.get("Unit", "")) == name]
    if len(rows) != 1:
        raise RenameRefused(f"{name}: expected one Units row")
    if add_unit.plan_cell_is_retired(rows[0].get("Plan", "")):
        raise RenameRefused(f"{name} is retired and has no live worktree")
    return rows[0]


def _stopped_unit(plan: RenamePlan, name: str) -> None:
    try:
        found = unit_lookup.marked_units(plan.new, every_host=True).get(name)
    except OSError as error:
        raise RenameRefused(f"{name}: cannot tell whether Claude is running: {error}") from error
    state = found.claude if found else unit_lookup.ClaudeNotRunning()
    if isinstance(state, unit_lookup.LiveClaude):
        raise RenameRefused(f"{name} has a live Claude; wait until its run stops")
    if isinstance(state, unit_lookup.ClaudeUnknown):
        raise RenameRefused(f"{name}: cannot tell whether Claude is running: {state.reason}")


def _branch_tip(checkout: Path, branch: str) -> str:
    return _git(checkout, "for-each-ref", "--format=%(objectname)", f"refs/heads/{branch}")


def _remote_tip(checkout: Path, branch: str) -> str:
    listed = _git(checkout, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    return listed.split("\t", 1)[0] if listed else ""


def _delete_remote_branch(plan: RenamePlan, branch: str, tip: str) -> None:
    remote = _remote_tip(plan.checkout, branch)
    if remote:
        if remote != tip:
            try:
                _ = _git(plan.checkout, "merge-base", "--is-ancestor", remote, tip)
            except RenameRefused as error:
                raise RenameRefused(f"origin branch {branch} has history outside the preserved tip") from error
        _ = _git(plan.checkout, "push", "origin", "--delete", branch)


def _delete_local_branch(plan: RenamePlan, branch: str, tip: str) -> None:
    local = _branch_tip(plan.checkout, branch)
    if local:
        if local != tip:
            raise RenameRefused(f"branch {branch} moved during the rename")
        _ = _git(plan.checkout, "branch", "-D", "--", branch)


def _delete_branch(plan: RenamePlan, branch: str, tip: str) -> None:
    _delete_remote_branch(plan, branch, tip)
    _delete_local_branch(plan, branch, tip)


def _prepare_unit(plan: RenamePlan, name: str, row: dict[str, str]) -> dict[str, object]:
    for field in ("Worktree", "Branch"):
        if field not in row:
            raise RenameRefused(f"{name}: Units table lacks {field} column")
    branch = add_unit.cell_value(row.get("Branch", ""))
    if not branch.startswith(plan.old + "-"):
        raise RenameRefused(f"{name}: branch {branch} does not start with {plan.old}-")
    stem = branch[len(plan.old) + 1:]
    worktree = Path(add_unit.cell_value(row.get("Worktree", ""))).expanduser().resolve()
    if not (worktree / ".git").is_file():
        raise RenameRefused(f"{name}: no linked worktree at {worktree}")
    new_branch = f"{plan.new}-{stem}"
    if _branch_tip(plan.checkout, new_branch) or _remote_tip(plan.checkout, new_branch):
        raise RenameRefused(f"branch {new_branch} already exists")
    suffix = f"-{plan.old}-{stem}"
    repository = (worktree.name[:-len(suffix)] if worktree.name.endswith(suffix) else plan.checkout.name)
    basename = f"{repository}-{plan.new}-{stem}"
    destination = worktree.with_name(basename)
    if _exists(destination):
        raise RenameRefused(f"destination already exists: {destination}")
    if _git(worktree, "branch", "--show-current") != branch:
        raise RenameRefused(f"{worktree} is not on {branch}")
    if _git(plan.checkout, "branch", "--show-current") != plan.new or _git(plan.checkout, "status", "--porcelain"):
        raise RenameRefused("showrunner checkout must be clean and on the new merge branch")
    return {"old_branch": branch, "new_branch": new_branch, "old_worktree": str(worktree),
            "new_worktree": str(destination), "tip": _git(worktree, "rev-parse", "HEAD"),
            "base": _git(plan.checkout, "rev-parse", "HEAD"), "step": 0}


def _check_unit_tip(plan: RenamePlan, record: dict[str, object]) -> None:
    branch = _string(record, "old_branch")
    tip = _branch_tip(plan.checkout, branch)
    if tip and tip != _string(record, "tip"):
        raise RenameRefused(f"branch {branch} moved during the rename")


def _push_unit_branch(plan: RenamePlan, record: dict[str, object]) -> None:
    _check_unit_tip(plan, record)
    branch, tip = _string(record, "new_branch"), _string(record, "tip")
    local, remote = _branch_tip(plan.checkout, branch), _remote_tip(plan.checkout, branch)
    if (local and local != tip) or (remote and remote != tip):
        raise RenameRefused(f"branch {branch} moved during the rename")
    if not local:
        _ = _git(plan.checkout, "branch", branch, tip)
    _ = _git(plan.checkout, "push", "--set-upstream", "origin", f"{branch}:refs/heads/{branch}")


def _move_unit_worktree(plan: RenamePlan, record: dict[str, object]) -> None:
    _check_unit_tip(plan, record)
    old = Path(_string(record, "old_worktree"))
    new = Path(_string(record, "new_worktree"))
    branch = _string(record, "new_branch")
    if old.exists():
        if _exists(new):
            raise RenameRefused(f"destination already exists: {new}")
        _ = _git(plan.checkout, "worktree", "move", str(old), str(new))
    if not (new / ".git").is_file():
        raise RenameRefused(f"unit worktree missing: {new}")
    _ = _git(plan.checkout, "worktree", "repair", str(new))
    current = _git(new, "branch", "--show-current")
    if current not in (_string(record, "old_branch"), branch):
        raise RenameRefused(f"{new} switched to {current}")
    _check_unit_tip(plan, record)
    _ = _git(new, "switch", branch)


def _config_dirs() -> list[Path]:
    directories = {path for path in Path.home().glob(".claude*") if path.is_dir()}
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        directories.add(Path(override).expanduser())
    return sorted(directories)


def _copy_trust(path: Path, old: str, new: str) -> None:
    config = _object(_read_json(path))
    projects = _object(config.get("projects", {}))
    if old not in projects:
        return
    trust = projects[old]
    for attempt in range(3):
        if attempt:
            config = _object(_read_json(path))
            projects = _object(config.get("projects", {}))
        _ = projects.setdefault(old, trust)
        projects[new] = trust
        config["projects"] = projects
        _write_json(path, config)
        time.sleep(2)
        verified = _object(_read_json(path))
        if new in _object(verified.get("projects", {})):
            return
    raise RenameRefused(f"{path}: new worktree trust entry did not persist; next launch needs a trust prompt")


def _move_unit_configs(record: dict[str, object]) -> None:
    old, new = _string(record, "old_worktree"), _string(record, "new_worktree")
    old_project = re.sub(r"[^A-Za-z0-9]", "-", old)
    new_project = re.sub(r"[^A-Za-z0-9]", "-", new)
    for directory in _config_dirs():
        _move(PathMove(directory / "projects" / old_project, directory / "projects" / new_project))
        config = (Path.home() / ".claude.json" if directory == Path.home() / ".claude"
                  else directory / ".claude.json")
        if config.exists():
            _copy_trust(config, old, new)


def _unit_doc(plan: RenamePlan, name: str, record: dict[str, object]) -> None:
    lines = plan.new_doc.read_text(encoding="utf-8").splitlines(keepends=True)
    headings = add_unit.unit_headings([line.rstrip("\n") for line in lines])
    for field in ("Worktree", "Branch"):
        if field not in headings:
            raise RenameRefused(f"{name}: Units table lacks {field} column")
    in_units = False
    for index, line in enumerate(lines):
        if line.startswith("## "):
            in_units = line.strip() == "## Units"
        if not in_units or not line.startswith("|"):
            continue
        cells = add_unit.row_cells(line)
        if cells and add_unit.cell_value(cells[0]) == name:
            for field, value in (("Worktree", _string(record, "new_worktree")),
                                 ("Branch", _string(record, "new_branch"))):
                column = headings.index(field)
                cells[column] = f"`{value}`"
            lines[index] = "| " + " | ".join(cells) + " |\n"
            _write_lines(plan.new_doc, lines)
            return
    raise RenameRefused(f"{name}: Units row disappeared")


def _commit_unit(plan: RenamePlan, name: str, record: dict[str, object]) -> None:
    _check_unit_tip(plan, record)
    message = f"chore({plan.new}): rename unit {name} from {plan.old}"
    if _git(plan.checkout, "branch", "--show-current") != plan.new:
        raise RenameRefused("showrunner checkout switched away from the new merge branch")
    path = _relative(plan, plan.new_doc)
    rows = add_unit.unit_table(_git(plan.checkout, "show", f"HEAD:{path}").splitlines())
    matching = [row for row in rows if add_unit.cell_value(row.get("Unit", "")) == name]
    committed = (len(matching) == 1
                 and add_unit.cell_value(matching[0].get("Worktree", "")) == _string(record, "new_worktree")
                 and add_unit.cell_value(matching[0].get("Branch", "")) == _string(record, "new_branch"))
    changed = set(_git(plan.checkout, "diff", "--name-only", "-z", "HEAD").split("\0")) - {""}
    changed.update(set(_git(plan.checkout, "diff", "--cached", "--name-only", "-z", "HEAD").split("\0")) - {""})
    untracked = _git(plan.checkout, "ls-files", "--others", "--exclude-standard")
    if changed - {path} or untracked or (committed and changed):
        raise RenameRefused("unrelated checkout changes during unit rename")
    if not committed:
        _unit_doc(plan, name, record)
        _ = _git(plan.checkout, "add", "--", path)
        _ = _git(plan.checkout, "commit", "-m", message)
    _check_unit_tip(plan, record)
    _ = _git(plan.checkout, "push", "origin", f"{plan.new}:refs/heads/{plan.new}")


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
            record = _prepare_unit(result, name, row)
            records[name] = record
            journal["unit_moves"] = records
            _write_json(result.journal, journal)
        completed = record.get("step")
        if type(completed) is not int or not 0 <= completed <= 5:
            raise RenameRefused(f"{name}: invalid unit rename progress")
        if completed < 5:
            _stopped_unit(result, name)
            _check_unit_tip(result, record)
        if completed < 1:
            _push_unit_branch(result, record)
            record["step"] = 1
            _write_json(result.journal, journal)
        if completed < 2:
            _stopped_unit(result, name)
            _move_unit_worktree(result, record)
            record["step"] = 2
            _write_json(result.journal, journal)
        if completed < 3:
            _move_unit_configs(record)
            record["step"] = 3
            _write_json(result.journal, journal)
        if completed < 4:
            _commit_unit(result, name, record)
            record["step"] = 4
            _write_json(result.journal, journal)
        if completed < 5:
            _check_unit_tip(result, record)
            if _remote_tip(result.checkout, _string(record, "new_branch")) != _string(record, "tip"):
                raise RenameRefused("new unit branch changed before old branch retirement")
            _delete_branch(result, _string(record, "old_branch"), _string(record, "tip"))
            record["step"] = 5
            _write_json(result.journal, journal)
        print(f"{name} moved to {_string(record, 'new_worktree')}; add_unit rewrites the tmux mark at next launch")


def finish(new: str) -> None:
    """Retire the recorded merge branch once all non-retired unit rows have moved."""
    with _rename_journal(new) as (result, journal):
        if journal["done"]:
            return
        rows = add_unit.unit_table(result.new_doc.read_text(encoding="utf-8").splitlines())
        remaining = [add_unit.cell_value(row.get("Unit", "")) for row in rows
                     if not add_unit.plan_cell_is_retired(row.get("Plan", ""))
                     and add_unit.cell_value(row.get("Branch", "")).startswith(result.old + "-")]
        if remaining:
            raise RenameRefused("units still use old branches: " + ", ".join(remaining))
        records = _object(journal.get("unit_moves", {}))
        if any(_object(value).get("step") != 5 for value in records.values()):
            raise RenameRefused("complete interrupted unit moves before finish")
        if _git(result.checkout, "branch", "--show-current") != result.new:
            raise RenameRefused("showrunner checkout must be on the new merge branch before finish")
        _delete_remote_branch(result, result.merge_branch, result.base)
        journal["finish_step"] = 1
        _write_json(result.journal, journal)
        _delete_local_branch(result, result.merge_branch, result.base)
        journal["finish_step"] = 2
        journal["done"] = True
        _write_json(result.journal, journal)
        print(f"{result.new} rename finished; retired {result.merge_branch}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("plan", "apply"):
        command = actions.add_parser(name)
        _ = command.add_argument("old")
        _ = command.add_argument("new")
    command = actions.add_parser("unit")
    _ = command.add_argument("new")
    _ = command.add_argument("unit")
    command = actions.add_parser("finish")
    _ = command.add_argument("new")
    args = parser.parse_args(argv)
    action, new = cast(str, args.action), cast(str, args.new)
    try:
        if action == "unit":
            unit(new, cast(str, args.unit))
        elif action == "finish":
            finish(new)
        else:
            old = cast(str, args.old)
            result = plan(old, new) if action == "plan" else apply(old, new)
            for line in _describe(result):
                print(line)
            if action == "apply":
                print(f"{result.old} is now {result.new}; keep {result.merge_branch} until finish")
    except (RenameRefused, add_unit.Refusal, tidy_wrapped.TidyRefused, OSError, UnicodeError) as error:
        print(f"rename: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
