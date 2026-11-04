"""Update document references and production logs during a production rename.

Each operation supports a retry after an interrupted rename.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.production import showrunners  # noqa: E402
import showrunner_tree
import unit_move  # noqa: E402
from rename_state import write_text  # noqa: E402
from unit_move import PathMove  # noqa: E402

PARENT_LINE = "- **Parent production:** "
LOG_LINE = "- **Log:** "


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
    moves = log_moves(old, new, doc)
    return [move.destination for move in moves if unit_move.exists(move.source)]


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
    lines: list[str] = []
    lines.extend(f"4. Parent production {doc}: {old_doc} -> {new_doc}"
                 for doc in _children_naming(old_doc, showrunner_tree.children_of(old)))
    if board_file(old).exists():
        lines.append(f"4. remove derived unit board {board_file(old)}")
    lines.extend(f"4. production log {move.source} -> {move.destination}"
                 for move in log_moves(old, new, old_doc if old_doc.exists() else new_doc) if unit_move.exists(move.source))
    return lines


def _rewrite_children(new: str, old_doc: Path, new_doc: Path) -> None:
    for doc in _children_naming(old_doc, showrunner_tree.children_of(new)):
        lines = doc.read_text(encoding="utf-8").splitlines(keepends=True)
        index = _parent_line(doc)
        if index is not None:
            lines[index] = PARENT_LINE + str(new_doc) + "\n"
            write_text(doc, "".join(lines))


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
    """Update outside references and production logs for the renamed document."""
    _rewrite_children(new, old_doc, new_doc)
    board_file(old).unlink(missing_ok=True)
    _move_log(old, new, new_doc)
