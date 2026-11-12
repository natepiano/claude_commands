"""Write the status line's counts row for one production: its `For you: …` line, as its footer counts it.

Usage: counts_row.py <notifier instance dir>
       counts_row.py --unit <staff id>

statusline.sh runs this when a source file changes or a cached deadline passes.
The cache is `<showrunner state>/counts/<staff id>`:

  line 1  the counts line, empty when nothing is counted;
  line 2  the epoch second for a deferred question or a failed read retry, empty when neither applies;
  then    each file the counts were read from, one per line. A file that does not exist yet is
          named by its folder, which changes when the file appears.
  last    `source=unit:<staff id>` for a unit cache.

The cache's time is when the reading began, so a file changed while it ran is newer than it.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.lib import conf
from scripts.hooks import showrunner_footer  # noqa: E402
from scripts.production import staff
from scripts.production.update_registration import QuestionStaff


def cache_path(name: str) -> Path:
    return showrunner_footer.state_root() / "counts" / name


def watched(path: Path) -> Path:
    """The path itself, or for one that does not exist yet its nearest folder that does."""
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def retry_due(sources: list[Path]) -> str:
    """Return a retry deadline, or watch the configuration when it is unusable."""
    try:
        return str(int(time.time()) + conf.read("statusline").whole("counts.retry_seconds"))
    except conf.Unusable:
        sources.append(conf.path("statusline"))
        return ""


def row(instance: Path, sources: list[Path], *, identities: QuestionStaff | None = None,
        source: str = "", require_complete: bool = False) -> tuple[str, str]:
    """Return counts and sources. Cache writes require complete reads; library callers retain available counts."""
    doc = showrunner_footer.production_doc(instance)
    if doc is not None:
        sources.append(doc)
    production = showrunner_footer.read_production(instance)
    if production is None:
        return "", ""
    from scripts.production import footer_counts
    from scripts.production.update_registration import (
        DeferredUntil, OutstandingReminder, QuestionStaff, RegistrationFailure, due, read_open_items,
    )

    read_sources = sources if require_complete else None
    if identities is None:
        identities = QuestionStaff.read() if read_sources is None else QuestionStaff.read(sources=read_sources)
    if require_complete:
        _ = footer_counts.sources(production.doc, identities=identities, read_sources=read_sources)
    else:
        sources.extend(footer_counts.sources(production.doc, identities=identities))
    sources.extend(footer_counts.routing_sources(identities.record, sources=read_sources))
    try:
        owner = production.owner
        if identities.record.by_id(owner.id) is None:
            raise RegistrationFailure("questions", f"{production.doc}: no showrunner staff row")
    except (RegistrationFailure, staff.StaffRefused) as error:
        print(f"questions: unavailable — {error}", file=sys.stderr)
        if require_complete:
            raise showrunner_footer.FooterError(str(error)) from error
        line = footer_counts.counts_line(footer_counts.read_counts(
            production.doc, record=identities.record, source=source)) or ""
        return line, ""
    outstanding = showrunner_footer.outstanding_path(owner.id, identities=identities)
    sources.append(outstanding)
    if require_complete:
        items = read_open_items(outstanding, document=production.doc, identities=identities, sources=read_sources)
        if source:
            items[:] = [item for item in items if not isinstance(item, OutstandingReminder) and item.asked_by == source]
        zone = ZoneInfo(production.zone)
        now = datetime.now(zone).replace(tzinfo=None)
        questions = len(due(items, now))
        afters = [item.timing.at for item in items if isinstance(item.timing, DeferredUntil) and item.timing.at > now]
        deadline = "" if not afters else str(int(min(afters).replace(tzinfo=zone).timestamp()))
    else:
        questions = showrunner_footer.open_questions([production], identities=identities, source=source)
        after = showrunner_footer.deferred_until([owner.id], production.zone, identities=identities)
        deadline = "" if after is None else str(int(after.timestamp()))
    line = footer_counts.counts_line(footer_counts.read_counts(
        production.doc, questions=questions or 0, record=identities.record, source=source, sources=read_sources)) or ""
    return line, deadline


def write(instance: Path) -> Path:
    cache = cache_path(instance.name.removeprefix("showrunner-"))
    cache.parent.mkdir(parents=True, exist_ok=True)
    began = time.time_ns()
    sources = [instance / "conf", staff.record_path()]
    try:
        line, due = row(instance, sources, require_complete=True)
    except (OSError, ValueError, showrunner_footer.FooterError):
        line = ""
        due = retry_due(sources)
    write_cache(cache, line, due, sources, began)
    return cache


def write_unit(identity: str) -> Path:
    """Write the unit's counts from its parent's production document."""
    cache = cache_path(identity)
    began = time.time_ns()
    sources = [staff.record_path()]
    try:
        identities = QuestionStaff.read(sources=sources)
        unit = identities.record.by_id(staff.StaffId(identity))
        owner = identities.record.parent_of(unit) if unit is not None else None
        if owner is None:
            raise ValueError(f"{identity}: no parent production")
        instance = Path(os.environ.get("NOTIFIER_STATE_DIR") or Path.home() / ".local/state/notifier") / f"showrunner-{owner.id}"
        sources.append(instance / "conf")
        line, due = row(instance, sources, identities=identities, source=identity, require_complete=True)
    except (OSError, ValueError, staff.StaffRefused, showrunner_footer.FooterError):
        line = ""
        due = retry_due(sources)
    write_cache(cache, line, due, sources, began, details=f"source=unit:{identity}")
    return cache


def write_cache(cache: Path, line: str, due: str, sources: list[Path], began: int, details: str = "") -> None:
    """Replace the cache with the answer, deadline, source paths, and optional result details."""
    cache.parent.mkdir(parents=True, exist_ok=True)
    scratch = cache.with_name(f".{cache.name}.{os.getpid()}")
    named = dict.fromkeys(str(watched(path)) for path in sources)
    _ = scratch.write_text("\n".join([line, due, *named, *([details] if details else [])]) + "\n", encoding="utf-8")
    os.utime(scratch, ns=(began, began))
    _ = scratch.replace(cache)


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "--unit":
        _ = write_unit(argv[1])
        return 0
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    _ = write(Path(argv[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
