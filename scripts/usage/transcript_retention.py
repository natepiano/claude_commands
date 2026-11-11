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
    sample_files: int
    sample_bytes: int


@dataclass(frozen=True)
class TranscriptFile:
    path: Path
    saved: os.stat_result


@dataclass(frozen=True)
class DeletionCandidate(TranscriptFile):
    pass


@dataclass(frozen=True)
class CompressionCandidate(TranscriptFile):
    replaced: int


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
    return RetentionPolicy(timedelta(days=compress), timedelta(days=delete),
                           settings.whole("sample_files"), settings.whole("sample_bytes"))


def transcript_roots(home: Path) -> list[Path]:
    return sorted({root.resolve() for pattern in (".claude*/projects", ".codex*/sessions")
                   for root in home.glob(pattern) if root.is_dir()})


def retained_sessions() -> frozenset[str]:
    """Read local staff membership without contacting its remote owner."""
    return frozenset(str(row.current_session) for row in staff.local_snapshot().rows if row.current_session)


def staff_transcript(path: Path, retained: frozenset[str]) -> bool:
    """Match session folders, plain session names, and rollout name suffixes."""
    logical = uncompressed_path(path)
    stem = logical.stem
    return (any(part in retained for part in logical.parts) or stem in retained
            or any(stem[index + 1:] in retained for index, char in enumerate(stem) if char == "-"))


def retention_candidates(root: Path, policy: RetentionPolicy, retained: frozenset[str], now: datetime,
                         totals: RetentionTotals) -> list[DeletionCandidate | CompressionCandidate]:
    candidates: list[DeletionCandidate | CompressionCandidate] = []
    for path in sorted(root.rglob("*.jsonl*")):
        if not path.name.endswith((".jsonl", ".jsonl.zst")) or not path.is_file():
            continue
        if staff_transcript(path, retained):
            totals.skipped += 1
            continue
        saved = path.stat()
        age = now.timestamp() - saved.st_mtime
        if age >= policy.delete_after.total_seconds():
            candidates.append(DeletionCandidate(path, saved))
        elif age >= policy.compress_after.total_seconds() and path.suffix != ".zst":
            destination = path.with_suffix(path.suffix + ".zst")
            replaced = destination.stat().st_size if destination.exists() else 0
            candidates.append(CompressionCandidate(path, saved, replaced))
    replaced_archives = {item.path.with_suffix(item.path.suffix + ".zst")
                         for item in candidates if isinstance(item, CompressionCandidate)}
    return [item for item in candidates
            if not isinstance(item, DeletionCandidate) or item.path not in replaced_archives]


def compression_ratio(candidates: Sequence[CompressionCandidate], policy: RetentionPolicy) -> float:
    """Measure bounded prefixes through the same compressor as a real run."""
    original = compressed = 0
    if not candidates:
        return 1.0
    with tempfile.NamedTemporaryFile() as sample, tempfile.TemporaryFile() as output:
        for candidate in candidates[:policy.sample_files]:
            with candidate.path.open("rb") as source:
                data = source.read(policy.sample_bytes)
            if not data:
                continue
            _ = sample.seek(0)
            _ = sample.truncate()
            _ = sample.write(data)
            sample.flush()
            _ = output.seek(0)
            _ = output.truncate()
            compressed += compress_transcript(Path(sample.name), output)
            original += len(data)
    return compressed / original if original else 1.0


def retain_root(root: Path, policy: RetentionPolicy, retained: frozenset[str], now: datetime, *,
                dry_run: bool, finalize: Callable[[Path], None], files: bool = False) -> RetentionTotals:
    totals = RetentionTotals(root)
    candidates = retention_candidates(root, policy, retained, now, totals)
    if not dry_run:
        compressions = sum(isinstance(item, CompressionCandidate) for item in candidates)
        deletions = len(candidates) - compressions
        print(f"{root}: compressing {compressions} files, deleting {deletions} files", flush=True)
    ratio = (compression_ratio([item for item in candidates if isinstance(item, CompressionCandidate)], policy)
             if dry_run else 1.0)
    for candidate in candidates:
        path, saved = candidate.path, candidate.saved
        if not dry_run:
            try:
                current = path.stat()
            except FileNotFoundError:
                continue
            if current.st_mtime_ns != saved.st_mtime_ns or current.st_size != saved.st_size:
                continue
        if isinstance(candidate, DeletionCandidate):
            if files:
                print(f"{'would delete' if dry_run else 'delete'} {path}; frees {saved.st_size} bytes")
            if not dry_run:
                finalize(path)
                path.unlink()
            totals.deleted += 1
            totals.freed += saved.st_size
        else:
            destination = path.with_suffix(path.suffix + ".zst")
            if dry_run:
                freed = round(saved.st_size * (1 - ratio)) + candidate.replaced
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
                    try:
                        replaced = destination.stat().st_size
                    except FileNotFoundError:
                        replaced = 0
                    os.replace(temporary, destination)
                    sync_folder(path.parent)
                finally:
                    temporary.unlink(missing_ok=True)
                finalize(path)
                path.unlink()
                freed = saved.st_size + replaced - size
            if files:
                estimate = "about " if dry_run else ""
                print(f"{'would compress' if dry_run else 'compress'} {path} -> {destination}; frees {estimate}{freed} bytes")
            totals.compressed += 1
            totals.freed += freed
    return totals


def unchanged_summary(path: Path) -> None:
    """A dry run or an absent cache needs no summary write."""
    del path


def summary_line(totals: RetentionTotals, *, dry_run: bool) -> str:
    compressed = "would compress" if dry_run else "compressed"
    deleted = "would delete" if dry_run else "deleted"
    freed = "would free about" if dry_run else "freed"
    return (f"{totals.root}: {compressed} {totals.compressed} files; {deleted} {totals.deleted} files; "
            + f"{totals.skipped} staff files skipped; {freed} {totals.freed / 10**9:.2f} GB")


def run(*, dry_run: bool, files: bool = False) -> list[RetentionTotals]:
    policy = read_policy()
    retained = retained_sessions()
    now = datetime.now(timezone.utc)

    def process(finalize: Callable[[Path], None]) -> list[RetentionTotals]:
        totals: list[RetentionTotals] = []
        for root in transcript_roots(Path.home()):
            total = retain_root(root, policy, retained, now, dry_run=dry_run, finalize=finalize, files=files)
            print(summary_line(total, dry_run=dry_run), flush=True)
            totals.append(total)
        return totals

    cache = Path.home() / ".cache/context_economy/summaries.sqlite3"
    if not dry_run and cache.is_file():
        with closing(TranscriptCache()) as summaries:
            totals = process(summaries.mark_final)
    else:
        totals = process(unchanged_summary)
    combined = RetentionTotals(Path("total"), sum(item.compressed for item in totals),
                               sum(item.deleted for item in totals), sum(item.skipped for item in totals),
                               sum(item.freed for item in totals))
    print(summary_line(combined, dry_run=dry_run))
    return totals


class RetentionArguments(argparse.Namespace):
    dry_run: bool = False
    files: bool = False


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument("--files", action="store_true", help="also print each file action")
    options = parser.parse_args(arguments, namespace=RetentionArguments())
    try:
        _ = run(dry_run=options.dry_run, files=options.files)
    except (OSError, ValueError, subprocess.SubprocessError, staff.StaffUnreadable) as error:
        print(f"transcript retention: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
