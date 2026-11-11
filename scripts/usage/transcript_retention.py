#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Compress old transcripts and delete expired transcripts on this machine."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

from scripts.lib import conf
from scripts.lib.atomic_file import sync_folder
from scripts.lib.transcripts import compress_transcript, uncompressed_path
from scripts.production import staff
from scripts.usage.context_economy_cache import TranscriptCache


@dataclass(frozen=True)
class RetentionPolicy:
    compress_after: timedelta
    delete_after: timedelta


@dataclass
class RetentionTotals:
    root: Path
    compressed: int = 0
    deleted: int = 0
    skipped: int = 0
    freed: int = 0


def read_policy() -> RetentionPolicy:
    settings = conf.read("transcript_retention")
    compress = settings.whole("compress_after_days")
    delete = settings.whole("delete_after_days")
    if not 0 < compress < delete:
        raise ValueError(f"{conf.path('transcript_retention')}: require 0 < compress_after_days < delete_after_days")
    return RetentionPolicy(timedelta(days=compress), timedelta(days=delete))


def transcript_roots(home: Path) -> list[Path]:
    return sorted({root.resolve() for pattern in (".claude*/projects", ".codex*/sessions")
                   for root in home.glob(pattern) if root.is_dir()})


def retained_sessions() -> frozenset[str]:
    """Read local staff membership without contacting its remote owner."""
    return frozenset(str(row.current_session) for row in staff.local_snapshot().rows if row.current_session)


def retain_root(root: Path, policy: RetentionPolicy, retained: frozenset[str], now: datetime, *,
                dry_run: bool, finalize: Callable[[Path], None]) -> RetentionTotals:
    totals = RetentionTotals(root)
    for path in sorted(root.rglob("*.jsonl*")):
        if not path.name.endswith((".jsonl", ".jsonl.zst")) or not path.is_file():
            continue
        logical = uncompressed_path(path)
        if any(logical.stem == identity or logical.stem.endswith("-" + identity)
               or identity in logical.parts for identity in retained):
            totals.skipped += 1
            continue
        saved = path.stat()
        age = now.timestamp() - saved.st_mtime
        if age >= policy.delete_after.total_seconds():
            print(f"{'would delete' if dry_run else 'delete'} {path}; frees {saved.st_size} bytes")
            if not dry_run:
                finalize(path)
                path.unlink()
            totals.deleted += 1
            totals.freed += saved.st_size
        elif age >= policy.compress_after.total_seconds() and path.suffix != ".zst":
            destination = path.with_suffix(path.suffix + ".zst")
            replaced = destination.stat().st_size if destination.exists() else 0
            if dry_run:
                with tempfile.TemporaryFile() as output:
                    size = compress_transcript(path, output)
            else:
                descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
                temporary = Path(name)
                try:
                    with os.fdopen(descriptor, "wb") as output:
                        size = compress_transcript(path, output)
                        output.flush()
                        os.fsync(output.fileno())
                        os.fchmod(output.fileno(), stat.S_IMODE(saved.st_mode))
                    os.utime(temporary, ns=(saved.st_atime_ns, saved.st_mtime_ns))
                    os.replace(temporary, destination)
                    sync_folder(path.parent)
                finally:
                    temporary.unlink(missing_ok=True)
                finalize(path)
                path.unlink()
            freed = saved.st_size + replaced - size
            print(f"{'would compress' if dry_run else 'compress'} {path} -> {destination}; frees {freed} bytes")
            totals.compressed += 1
            totals.freed += freed
    return totals


def unchanged_summary(path: Path) -> None:
    """A dry run or an absent cache needs no summary write."""
    del path


def run(*, dry_run: bool) -> list[RetentionTotals]:
    policy = read_policy()
    retained = retained_sessions()
    now = datetime.now(timezone.utc)

    def process(finalize: Callable[[Path], None]) -> list[RetentionTotals]:
        return [retain_root(root, policy, retained, now, dry_run=dry_run, finalize=finalize)
                for root in transcript_roots(Path.home())]

    cache = Path.home() / ".cache/context_economy/summaries.sqlite3"
    if not dry_run and cache.is_file():
        with closing(TranscriptCache()) as summaries:
            totals = process(summaries.mark_final)
    else:
        totals = process(unchanged_summary)
    for total in totals:
        print(f"{total.root}: {total.compressed} compressed; {total.deleted} deleted; "
              + f"{total.skipped} staff transcripts skipped; {'would free' if dry_run else 'freed'} {total.freed} bytes")
    print(f"total: {'would free' if dry_run else 'freed'} {sum(total.freed for total in totals)} bytes; "
          + f"{sum(total.skipped for total in totals)} staff transcripts skipped")
    return totals


class RetentionArguments(argparse.Namespace):
    dry_run: bool = False


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args(arguments, namespace=RetentionArguments())
    try:
        _ = run(dry_run=options.dry_run)
    except (OSError, ValueError, subprocess.SubprocessError, staff.StaffUnreadable) as error:
        print(f"transcript retention: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
