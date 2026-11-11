"""Keep transcript summaries and resume checkpoints outside transcript folders."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import hashlib
import sqlite3
import sys
import time
from typing import Literal, cast, final

from scripts.lib import conf

Family = Literal["claude", "codex"]


@dataclass(frozen=True)
class TranscriptReadSettings:
    """The cache size and progress interval for one transcript read."""
    cache_max_megabytes: int
    progress_seconds: int


class TranscriptSettingsSource(Enum):
    """Read settings from this machine when the caller supplies no values."""
    LOCAL_CONFIG = "local_config"


def read_settings(settings: TranscriptReadSettings | TranscriptSettingsSource =
                  TranscriptSettingsSource.LOCAL_CONFIG) -> TranscriptReadSettings:
    if isinstance(settings, TranscriptReadSettings):
        return settings
    configuration = conf.read("context_economy")
    return TranscriptReadSettings(configuration.whole("cache_max_megabytes"), configuration.whole("progress_seconds"))


@dataclass(frozen=True)
class ParseCheckpoint:
    """Parser state after the last complete line."""
    offset: int = 0
    payload: str = "{}"


@dataclass(frozen=True)
class SummaryUnavailable:
    """A compressed transcript has no saved summary."""


def logical_path(path: Path) -> Path:
    resolved = path.resolve()
    return resolved.with_suffix("") if resolved.suffix == ".zst" else resolved


def prefix_hash(path: Path, offset: int) -> str:
    """Hash every byte before the saved complete-line offset in large blocks."""
    digest = hashlib.blake2b()
    remaining = offset
    with path.open("rb") as source:
        while remaining:
            block = source.read(min(remaining, 1024 * 1024))
            if not block:
                break
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


@final
class TranscriptCache:
    """Keep summaries after deletion and evict only reproducible summaries."""

    database: sqlite3.Connection
    limit: int

    def __init__(self, settings: TranscriptReadSettings | TranscriptSettingsSource =
                 TranscriptSettingsSource.LOCAL_CONFIG) -> None:
        self.limit = read_settings(settings).cache_max_megabytes * 1024 * 1024
        folder = Path.home() / ".cache/context_economy"
        folder.mkdir(parents=True, exist_ok=True)
        self.database = sqlite3.connect(folder / "summaries.sqlite3")
        self.database.row_factory = sqlite3.Row
        _ = self.database.execute("""CREATE TABLE IF NOT EXISTS summaries (
            path TEXT PRIMARY KEY, family TEXT NOT NULL, device INTEGER NOT NULL, inode INTEGER NOT NULL,
            size INTEGER NOT NULL, mtime INTEGER NOT NULL, ctime INTEGER NOT NULL,
            offset INTEGER NOT NULL, payload TEXT NOT NULL, accessed INTEGER NOT NULL, final INTEGER NOT NULL,
            prefix_hash TEXT NOT NULL DEFAULT ''
        )""")
        columns = {cast(str, row["name"]) for row in cast(list[sqlite3.Row], self.database.execute(
            "PRAGMA table_info(summaries)").fetchall())}
        if "prefix_hash" not in columns:
            with self.database:
                _ = self.database.execute("ALTER TABLE summaries ADD COLUMN prefix_hash TEXT NOT NULL DEFAULT ''")

    def paths(self, family: Family, roots: Sequence[Path], found: Sequence[Path], since: float) -> list[Path]:
        """Include retained summaries under the requested roots, using the last source mtime."""
        saved = {Path(cast(str, row["path"])): cast(int, row["mtime"]) / 1_000_000_000
                 for row in cast(list[sqlite3.Row], self.database.execute(
                     "SELECT path, mtime FROM summaries WHERE family = ?", (family,)).fetchall())}
        paths: set[Path] = set()
        for source in found:
            path = logical_path(source)
            stamp = saved.get(path, source.stat().st_mtime) if source.suffix == ".zst" else source.stat().st_mtime
            if stamp >= since:
                paths.add(path)
        resolved_roots = [root.resolve() for root in roots]
        for path, stamp in saved.items():
            if (stamp >= since and any(path.is_relative_to(root) for root in resolved_roots)
                    and not path.exists()):
                paths.add(path)
        return sorted(paths)

    def read(self, path: Path, family: Family,
             parse: Callable[[Path, ParseCheckpoint], ParseCheckpoint]) -> ParseCheckpoint | SummaryUnavailable:
        path = logical_path(path)
        row = cast(sqlite3.Row | None, self.database.execute(
            "SELECT * FROM summaries WHERE path = ? AND family = ?", (str(path), family)).fetchone())
        held = (ParseCheckpoint(cast(int, row["offset"]), cast(str, row["payload"]))
                if row is not None else ParseCheckpoint())
        if not path.exists():
            if row is None:
                return SummaryUnavailable()
            self.touch(path)
            return held
        stat = path.stat()
        resumed = (row is not None and cast(int, row["device"]) == stat.st_dev
                   and cast(int, row["inode"]) == stat.st_ino and stat.st_size >= held.offset
                   and stat.st_size >= cast(int, row["size"]))
        if resumed and row is not None and cast(int, row["size"]) == stat.st_size:
            if cast(int, row["mtime"]) == stat.st_mtime_ns and cast(int, row["ctime"]) == stat.st_ctime_ns:
                self.touch(path)
                return held
            resumed = False
        if resumed and row is not None:
            resumed = prefix_hash(path, held.offset) == cast(str, row["prefix_hash"])
        parsed = parse(path, held if resumed else ParseCheckpoint())
        digest = prefix_hash(path, parsed.offset)
        with self.database:
            _ = self.database.execute("""INSERT OR REPLACE INTO summaries
                (path, family, device, inode, size, mtime, ctime, offset, payload, accessed, final, prefix_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(path), family, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                 parsed.offset, parsed.payload, time.time_ns(), 0, digest))
        return parsed

    def touch(self, path: Path) -> None:
        with self.database:
            _ = self.database.execute("UPDATE summaries SET accessed = ? WHERE path = ?", (time.time_ns(), str(path)))

    def mark_final(self, path: Path) -> None:
        """Let the compressor mark the logical transcript's saved summary as final."""
        with self.database:
            _ = self.database.execute("UPDATE summaries SET final = 1 WHERE path = ?", (str(logical_path(path)),))

    def close(self) -> None:
        try:
            page_size = cast(int, self.database.execute("PRAGMA page_size").fetchone()[0])
            pages = cast(int, self.database.execute("PRAGMA page_count").fetchone()[0])
            free_pages = cast(int, self.database.execute("PRAGMA freelist_count").fetchone()[0])
            size = (pages - free_pages) * page_size
            removed = False
            for row in cast(list[sqlite3.Row], self.database.execute(
                    "SELECT path, length(CAST(payload AS BLOB)) AS bytes FROM summaries ORDER BY accessed").fetchall()):
                if size <= self.limit:
                    break
                path = Path(cast(str, row["path"]))
                if path.exists() or path.with_suffix(path.suffix + ".zst").exists():
                    _ = self.database.execute("DELETE FROM summaries WHERE path = ?", (str(path),))
                    size -= cast(int, row["bytes"])
                    removed = True
            self.database.commit()
            if removed or pages * page_size > self.limit and free_pages:
                _ = self.database.execute("VACUUM")
        finally:
            self.database.close()


@final
class FileProgress:
    """Report long reads without adding progress text to report output."""

    def __init__(self, machine: str, family: Family, total: int,
                 settings: TranscriptReadSettings | TranscriptSettingsSource = TranscriptSettingsSource.LOCAL_CONFIG) -> None:
        self.label = f"{machine} {family}"
        self.total = total
        self.done = 0
        self.skipped = 0
        self.last = time.monotonic()
        self.interval = read_settings(settings).progress_seconds
        self.shown = False

    def advance(self, *, skipped: bool = False) -> None:
        self.done += 1
        self.skipped += int(skipped)
        now = time.monotonic()
        if now - self.last >= self.interval or self.done == self.total and (self.shown or self.skipped):
            ending = "\r" if sys.stderr.isatty() and self.done < self.total else "\n"
            print(f"{self.label}: files {self.done} of {self.total}; skipped {self.skipped}",
                  end=ending, file=sys.stderr, flush=True)
            self.last, self.shown = now, True
