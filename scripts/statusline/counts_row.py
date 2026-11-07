"""Write the status line's counts row for one production: its `For you: …` line, as its footer counts it.

Usage: counts_row.py <notifier instance dir>

statusline.sh runs this only when the cache is older than a file it was read from, or a deferred
question has fallen due, so a render runs no Python while nothing changed. The cache is
`<showrunner state>/counts/<staff id>`:

  line 1  the counts line, empty when nothing is counted;
  line 2  the epoch second the soonest deferred question falls due, empty when none is deferred;
  then    each file the counts were read from, one per line. A file that does not exist yet is
          named by its folder, which changes when the file appears.

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
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(SCRIPTS / "hooks"), str(SCRIPTS / "production")]
import showrunner_footer  # noqa: E402
from scripts.production import staff
from scripts.production.update_registration import QuestionStaff


def cache_path(name: str) -> Path:
    return showrunner_footer.state_root() / "counts" / name


def watched(path: Path) -> Path:
    """The path itself, or for one that does not exist yet its nearest folder that does."""
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def row(instance: Path, *, identities: QuestionStaff | None = None) -> tuple[str, str, list[Path]]:
    """The counts line, the epoch second it next changes by the clock, and the files it was read from."""
    sources = [instance / "conf"]
    try:
        doc = showrunner_footer.production_doc(instance)
    except (OSError, ValueError):
        doc = None
    production = showrunner_footer.read_production(instance)
    if doc is not None:
        sources.append(doc)
    if production is None:
        return "", "", sources
    from scripts.production import footer_counts
    from scripts.production.update_registration import QuestionStaff, RegistrationFailure

    identities = QuestionStaff.read() if identities is None else identities
    sources.extend(footer_counts.sources(production.doc, identities=identities))
    sources.extend(footer_counts.routing_sources(identities.record))
    try:
        owner = production.owner
        if identities.record.by_id(owner.id) is None:
            raise RegistrationFailure("questions", f"{production.doc}: no showrunner staff row")
    except (RegistrationFailure, staff.StaffRefused) as error:
        print(f"questions: unavailable — {error}", file=sys.stderr)
        line = footer_counts.counts_line(footer_counts.read_counts(production.doc, record=identities.record)) or ""
        return line, "", sources
    sources.append(showrunner_footer.outstanding_path(owner.id, identities=identities))
    questions = showrunner_footer.open_questions([production], identities=identities)
    line = footer_counts.counts_line(footer_counts.read_counts(
        production.doc, questions=questions or 0, record=identities.record)) or ""
    due = showrunner_footer.deferred_until([owner.id], production.zone, identities=identities)
    return line, "" if due is None else str(int(due.timestamp())), sources


def write(instance: Path) -> Path:
    cache = cache_path(instance.name.removeprefix("showrunner-"))
    cache.parent.mkdir(parents=True, exist_ok=True)
    began = time.time_ns()
    try:
        line, due, sources = row(instance)
    except (OSError, ValueError, showrunner_footer.FooterError):
        # Nothing readable to count: the row stays empty until the timer's conf changes.
        line, due, sources = "", "", [instance / "conf"]
    cache = cache_path(instance.name.removeprefix("showrunner-"))
    write_cache(cache, line, due, sources, began)
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
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    _ = write(Path(argv[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
