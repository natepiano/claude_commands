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
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
import add_unit  # noqa: E402
import showrunners  # noqa: E402
import stall_watch  # noqa: E402
import tidy_wrapped  # noqa: E402
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
        name = add_unit.cell_value(raw)
        if name in ("", "—", "-", "permanent"):
            continue
        path = Path(name).expanduser()
        path = (path if path.is_absolute() else production.checkout / path).resolve()
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


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("action", choices=("plan", "apply"))
    _ = parser.add_argument("old")
    _ = parser.add_argument("new")
    args = parser.parse_args(argv)
    action, old, new = cast(str, args.action), cast(str, args.old), cast(str, args.new)
    try:
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
