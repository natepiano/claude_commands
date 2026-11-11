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
from scripts.lib.transcripts import open_transcript, uncompressed_path

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
    return uncompressed_path(resolved)


ANCHOR_BYTES = 4096


@dataclass(frozen=True)
class PrefixAnchor:
    """Hashes of the first block and the block before a saved offset."""
    first: str
    last: str


def prefix_anchor(path: Path, offset: int) -> PrefixAnchor:
    """Read at most two fixed-size blocks before the saved offset."""
    with open_transcript(path) as source:
        first = source.read(min(offset, ANCHOR_BYTES))
        _ = source.seek(max(0, offset - ANCHOR_BYTES))
        last = source.read(min(offset, ANCHOR_BYTES))
    return PrefixAnchor(hashlib.blake2b(first).hexdigest(), hashlib.blake2b(last).hexdigest())


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
        _ = self.database.execute("PRAGMA journal_mode=WAL")
        self.database.row_factory = sqlite3.Row
        self.accessed: dict[str, int] = {}
        _ = self.database.execute("""CREATE TABLE IF NOT EXISTS summaries (
            path TEXT PRIMARY KEY, family TEXT NOT NULL, device INTEGER NOT NULL, inode INTEGER NOT NULL,
            size INTEGER NOT NULL, mtime INTEGER NOT NULL, ctime INTEGER NOT NULL,
            offset INTEGER NOT NULL, payload TEXT NOT NULL, accessed INTEGER NOT NULL, final INTEGER NOT NULL,
            first_hash TEXT NOT NULL DEFAULT '', last_hash TEXT NOT NULL DEFAULT ''
        )""")

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
            "SELECT device, inode, size, mtime, ctime, offset, payload, first_hash, last_hash "
            + "FROM summaries WHERE path = ? AND family = ?", (str(path), family)).fetchone())
        held = (ParseCheckpoint(cast(int, row["offset"]), cast(str, row["payload"]))
                if row is not None else ParseCheckpoint())
        if not path.exists():
            if row is not None:
                self.mark_final(path)
                self.touch(path)
                return held
            compressed = path.with_suffix(path.suffix + ".zst")
            if not compressed.exists():
                return SummaryUnavailable()
            source = compressed
        else:
            source = path
        stat = source.stat()
        resumed = (row is not None and cast(int, row["device"]) == stat.st_dev
                   and cast(int, row["inode"]) == stat.st_ino and stat.st_size >= held.offset
                   and stat.st_size >= cast(int, row["size"]))
        if resumed and row is not None and cast(int, row["size"]) == stat.st_size:
            if cast(int, row["mtime"]) == stat.st_mtime_ns and cast(int, row["ctime"]) == stat.st_ctime_ns:
                self.touch(path)
                return held
            resumed = False
        if resumed and row is not None:
            resumed = prefix_anchor(path, held.offset) == PrefixAnchor(
                cast(str, row["first_hash"]), cast(str, row["last_hash"]))
        parsed = parse(source, held if resumed else ParseCheckpoint())
        anchor = prefix_anchor(path, parsed.offset)
        with self.database:
            _ = self.database.execute("""INSERT OR REPLACE INTO summaries
                (path, family, device, inode, size, mtime, ctime, offset, payload, accessed, final, first_hash, last_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(path), family, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                 parsed.offset, parsed.payload, time.time_ns(), int(source.suffix == ".zst"), anchor.first, anchor.last))
        _ = self.accessed.pop(str(path), 0)
        return parsed

    def touch(self, path: Path) -> None:
        self.accessed[str(path)] = time.time_ns()

    def mark_final(self, path: Path) -> None:
        """Let the compressor mark the logical transcript's saved summary as final."""
        with self.database:
            _ = self.database.execute("UPDATE summaries SET final = 1 WHERE path = ?", (str(logical_path(path)),))

    def close(self) -> None:
        try:
            with self.database:
                _ = self.database.executemany("UPDATE summaries SET accessed = ? WHERE path = ?",
                                             ((at, path) for path, at in self.accessed.items()))
            page_size = cast(int, self.database.execute("PRAGMA page_size").fetchone()[0])
            pages = cast(int, self.database.execute("PRAGMA page_count").fetchone()[0])
            free_pages = cast(int, self.database.execute("PRAGMA freelist_count").fetchone()[0])
            size = (pages - free_pages) * page_size
            removed = False
            if size <= self.limit:
                if pages * page_size > self.limit and free_pages:
                    _ = self.database.execute("VACUUM")
                return
            for row in cast(list[sqlite3.Row], self.database.execute(
                    "SELECT path, length(CAST(payload AS BLOB)) AS bytes FROM summaries ORDER BY accessed").fetchall()):
                if size <= self.limit:
                    break
                path = Path(cast(str, row["path"]))
                if path.exists():
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
