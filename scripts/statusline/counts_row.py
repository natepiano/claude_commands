"""Write the status line's counts row for one production: its `For you: …` line, as its footer counts it.

Usage: counts_row.py <notifier instance dir>

statusline.sh runs this only when the cache is older than a file it was read from, or a deferred
question has fallen due, so a render runs no Python while nothing changed. The cache is
`<showrunner state>/counts/<slug>`:

  line 1  the counts line, empty when nothing is counted;
  line 2  the epoch second the soonest deferred question falls due, empty when none is deferred;
  then    each file the counts were read from, one per line. A file that does not exist yet is
          named by its folder, which changes when the file appears.

The cache's time is when the reading began, so a file changed while it ran is newer than it.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(SCRIPTS / "hooks"), str(SCRIPTS / "production")]
import showrunner_footer  # noqa: E402


def cache_path(slug: str) -> Path:
    return showrunner_footer.state_root() / "counts" / slug


def watched(path: Path) -> Path:
    """The path itself, or for one that does not exist yet its nearest folder that does."""
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def row(instance: Path) -> tuple[str, str, list[Path]]:
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
    import footer_counts

    below = showrunner_footer.place_of(production.slug).below
    slugs = [production.slug, *below]
    sources.extend(footer_counts.sources(production.doc))
    sources.extend(showrunner_footer.outstanding_path(slug) for slug in slugs)
    questions = showrunner_footer.open_questions([production], {production.slug: below})
    line = showrunner_footer.counts_line([production], questions or 0) or ""
    due = showrunner_footer.deferred_until(slugs, production.zone)
    return line, "" if due is None else str(int(due.timestamp())), sources


def write(instance: Path) -> Path:
    began = time.time_ns()
    try:
        line, due, sources = row(instance)
    except (OSError, ValueError, showrunner_footer.FooterError):
        # Nothing readable to count: the row stays empty until the timer's conf changes.
        line, due, sources = "", "", [instance / "conf"]
    cache = cache_path(instance.name.removeprefix("showrunner-"))
    cache.parent.mkdir(parents=True, exist_ok=True)
    scratch = cache.with_name(f".{cache.name}.{os.getpid()}")
    named = dict.fromkeys(str(watched(path)) for path in sources)
    _ = scratch.write_text("\n".join([line, due, *named]) + "\n", encoding="utf-8")
    os.utime(scratch, ns=(began, began))
    _ = scratch.replace(cache)
    return cache


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    _ = write(Path(argv[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
