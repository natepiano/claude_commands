"""Production rename, apply step 4: the production's state outside the showrunner state folder.

Each part is safe to repeat, so a refused step 4 resumes by running every part again:

- /next pins: `next-`, `proposal-` and `question-<slug>` queues, and their all-below forms, take the
  new slug, and each item key `<old>: ...` becomes `<new>: ...`.
- change review: each Production cell on the production's list that names the old slug.
- children: a child production doc whose Parent production line names the old doc.
- bevy upgrade watch: its record, lock and notifier job, whose check names the doc.
- unit board: the old slug's board file is derived output; it is removed and rebuilt on demand.
- log: a Log line naming `<old>-log.md` takes `<new>-log.md`; the log and its `-recovered` copy move, and
  an info/exclude line naming an old one also names the new one. Both stay untracked (log_paths).
"""
from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
import change_review  # noqa: E402
import showrunners  # noqa: E402
import showrunner_tree
import unit_move  # noqa: E402
from rename_state import RenameRefused, read_json, write_json, write_text  # noqa: E402
from unit_move import PathMove  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import urgency  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bevy_upgrade"))
import upgrade_record  # noqa: E402
import upgrade_schedule  # noqa: E402

PIN_KINDS = ("next", "proposal", "question")
PARENT_LINE = "- **Parent production:** "
LOG_LINE = "- **Log:** "


def pin_moves(old: str, new: str) -> tuple[PathMove, ...]:
    """Each /next pin file named for the production, existing or not."""
    root = urgency.state_dir()
    return tuple(PathMove(root / f"{urgency.scope_queue(f'{kind}-{old}', below)}.json",
                          root / f"{urgency.scope_queue(f'{kind}-{new}', below)}.json")
                 for kind in PIN_KINDS for below in (False, True))


def bevy_job(old: str, new: str) -> PathMove:
    return PathMove(showrunners.NOTIFIER_STATE_DIR / upgrade_schedule.instance(old),
                    showrunners.NOTIFIER_STATE_DIR / upgrade_schedule.instance(new))


def bevy_record(old: str, new: str) -> PathMove:
    return PathMove(upgrade_record.record_path(old), upgrade_record.record_path(new))


def board_file(slug: str) -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return Path(os.environ.get("UNIT_BOARD_DIR") or state / "unit-board").expanduser() / f"{slug}.json"


RECOVERED = "-recovered"


def _log_line(doc: Path) -> Path | None:
    """The path the doc's Log line names, as written (relative to the checkout), or None."""
    line = next((line for line in doc.read_text(encoding="utf-8").splitlines() if line.startswith(LOG_LINE)), "")
    found = re.search(r"`([^`]+)`", line)
    return Path(found[1]).expanduser() if found else None


def _with_recovered(path: Path) -> tuple[Path, Path]:
    return path, path.with_name(f"{path.stem}{RECOVERED}{path.suffix}")


def log_paths(old: str, new: str, doc: Path) -> frozenset[str]:
    """The production's own log and its -recovered copy, under the old and the new name, relative to the
    checkout. A production log is never committed, so these may stand untracked in a clean checkout."""
    log = _log_line(doc)
    if log is None or log.is_absolute():
        return frozenset()
    names = ({log.with_name(f"{old}-log.md"), log.with_name(f"{new}-log.md")}
             if log.name in (f"{old}-log.md", f"{new}-log.md") else {log})
    return frozenset(str(path) for name in names for path in _with_recovered(name))


def log_moves(old: str, new: str, doc: Path) -> tuple[PathMove, ...]:
    """The log's and its -recovered copy's moves when the doc's Log line names `<old>-log.md`."""
    log = _log_line(doc)
    if log is None or log.name != f"{old}-log.md":
        return ()
    source = Path(unit_move.git(doc.parent, "rev-parse", "--show-toplevel")) / log
    return tuple(PathMove(path, renamed) for path, renamed in
                 zip(_with_recovered(source), _with_recovered(source.with_name(f"{new}-log.md")), strict=True))


def destinations(old: str, new: str, doc: Path) -> list[Path]:
    """The paths step 4 creates here; preflight refuses a rename when one already exists."""
    moves = (*pin_moves(old, new), bevy_job(old, new), bevy_record(old, new), *log_moves(old, new, doc))
    return [move.destination for move in moves if unit_move.exists(move.source)]


def _change_list(doc: Path) -> Path | None:
    try:
        return change_review.scope_list(doc)
    except (change_review.ChangeReviewError, OSError):
        return None


def _parent_line(doc: Path) -> int | None:
    lines = doc.read_text(encoding="utf-8").splitlines()
    return next((index for index, line in enumerate(lines) if line.startswith(PARENT_LINE)), None)


def _children_naming(old_doc: Path, children: list[str]) -> list[Path]:
    docs = showrunners.timer_docs()
    found: list[Path] = []
    for child in children:
        doc = docs.get(child)
        if doc is None or not doc.exists():
            continue
        index = _parent_line(doc)
        if index is None:
            continue
        value = doc.read_text(encoding="utf-8").splitlines()[index][len(PARENT_LINE):].strip().strip("`")
        if Path(value).expanduser().resolve() == old_doc.resolve():
            found.append(doc)
    return found


def describe(old: str, new: str, old_doc: Path, new_doc: Path) -> list[str]:
    """Plan lines for step 4's outside state; reads only."""
    lines = [f"4. /next pin {move.source} -> {move.destination}"
             for move in pin_moves(old, new) if unit_move.exists(move.source)]
    listed = _change_list(old_doc)
    rows = ([row["#"] for row in change_review.pending(listed) if row.get(change_review.PRODUCTION) == old]
            if listed is not None and listed.exists() else [])
    if rows:
        lines.append(f"4. change review {listed}: Production {old} -> {new} in {len(rows)} rows")
    lines.extend(f"4. Parent production {doc}: {old_doc} -> {new_doc}"
                 for doc in _children_naming(old_doc, showrunner_tree.children_of(old)))
    for move in (bevy_record(old, new), bevy_job(old, new)):
        if unit_move.exists(move.source):
            lines.append(f"4. bevy upgrade watch {move.source} -> {move.destination}")
    if board_file(old).exists():
        lines.append(f"4. remove derived unit board {board_file(old)}")
    lines.extend(f"4. production log {move.source} -> {move.destination}"
                 for move in log_moves(old, new, old_doc if old_doc.exists() else new_doc) if unit_move.exists(move.source))
    return lines


def _rename_keys(value: object, old: str, new: str) -> object:
    if isinstance(value, str):
        return new + value[len(old):] if value.startswith(f"{old}: ") else value
    if isinstance(value, list):
        return [_rename_keys(item, old, new) for item in cast(list[object], value)]
    if isinstance(value, dict):
        return {cast(str, _rename_keys(key, old, new)): _rename_keys(item, old, new)
                for key, item in cast(dict[str, object], value).items()}
    return value


def _move_pins(old: str, new: str) -> None:
    for move in pin_moves(old, new):
        unit_move.move(move)
        if move.destination.exists():
            pins = read_json(move.destination)
            renamed = _rename_keys(pins, old, new)
            if renamed != pins:
                write_json(move.destination, renamed)


def _rewrite_children(new: str, old_doc: Path, new_doc: Path) -> None:
    for doc in _children_naming(old_doc, showrunner_tree.children_of(new)):
        lines = doc.read_text(encoding="utf-8").splitlines(keepends=True)
        index = _parent_line(doc)
        if index is not None:
            lines[index] = PARENT_LINE + str(new_doc) + "\n"
            write_text(doc, "".join(lines))


def _move_bevy_watch(old: str, new: str, old_doc: Path, new_doc: Path) -> None:
    record = bevy_record(old, new)
    if record.source.exists():
        fields = unit_move.json_object(read_json(record.source))
        fields.update(slug=new, production=str(new_doc))
        write_json(record.destination, fields)
        record.source.unlink()
    (upgrade_record.state_dir() / f"{old}.lock").unlink(missing_ok=True)
    job = bevy_job(old, new)
    instance = job.source if job.source.exists() else job.destination
    conf = instance / "conf"
    if conf.exists():
        text = conf.read_text(encoding="utf-8")
        updated = "".join(
            f"FROM={upgrade_schedule.instance(new)}\n" if line.startswith("FROM=")
            else line.replace(shlex.quote(str(old_doc)), shlex.quote(str(new_doc))) if line.startswith("RUN=")
            else line for line in text.splitlines(keepends=True))
        if updated != text:
            write_text(conf, updated)
    unit_move.move(job)


def _move_log(old: str, new: str, doc: Path) -> None:
    moves = log_moves(old, new, doc)
    if not moves:
        return
    top = Path(unit_move.git(doc.parent, "rev-parse", "--show-toplevel"))
    exclude = top / unit_move.git(top, "rev-parse", "--git-path", "info/exclude")
    listed = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    added: list[str] = []
    for move in moves:
        unit_move.move(move)
        old_name, new_name = str(move.source.relative_to(top)), str(move.destination.relative_to(top))
        if old_name in listed and new_name not in listed:
            added.append(new_name)
    if added:
        write_text(exclude, "".join(f"{line}\n" for line in (*listed, *added)))
    text = doc.read_text(encoding="utf-8")
    write_text(doc, "".join(line.replace(f"{old}-log.md`", f"{new}-log.md`", 1) if line.startswith(LOG_LINE) else line
                            for line in text.splitlines(keepends=True)))


def apply(old: str, new: str, old_doc: Path, new_doc: Path) -> None:
    """Move the production's outside state to the new slug and doc. Runs after the parent files moved."""
    _move_pins(old, new)
    listed = _change_list(new_doc)
    if listed is not None:
        try:
            _ = change_review.rename_production(listed, new_doc, old, new)
        except change_review.ChangeReviewError as error:
            raise RenameRefused(f"change review: {error}") from error
    _rewrite_children(new, old_doc, new_doc)
    _move_bevy_watch(old, new, old_doc, new_doc)
    board_file(old).unlink(missing_ok=True)
    _move_log(old, new, new_doc)
