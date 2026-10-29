#!/usr/bin/env python3
"""Keep a cargo project's target directory under a size budget.

invoke.sh's run() starts `lint sweep` in the background after every build
step, at most once every 5 minutes per working directory, and cargo-port runs
it last in every per-project lint run; `invoke.sh` hands the call here. When
the target directory is over budget, this removes the least recently used
build output until it fits:

    build unit        every entry under a build tree's .fingerprint/, build/,
                      deps/ and examples/ that carries one unit's 16-hex-digit
                      hash, and the copies cargo made of its output (below),
                      removed together
    incremental dir   one direct child of a build tree's incremental/

The budget is the first of: LINT_SWEEP_BUDGET_GIB in the environment;
sweep_budget_gib.<repo> in config/lint.conf, where <repo> is the name of the
directory holding the git common dir, so every worktree of a repo shares it;
sweep_budget_gib there; 24 GiB. It is summed over every file under the target
and build directories, so output the budget sweep never
removes (test-run folders, files it cannot match to a build unit) still
counts. doc/ has its own rule, below. --dry-run reports what would go and
removes nothing. --target-dir DIR sweeps DIR in place of the directories
cargo metadata reports for the working directory: hana's CI runners call it
that way after every job, with no workspace to ask (/etc/nixos,
modules/linux/hana-runners.nix).

Copied-up output. For a binary, an example, a dylib or a library named on the
command line, cargo also puts the unit's file under its unhashed name, from
deps/ into the build tree itself and within examples/, and writes a dep-info
file beside it (<name without extension>.d) whose first line names that copy.
It hard-links on Linux and copies on macOS (clonefile, cargo #10060), and does
so again on every build of the unit, fresh or not. A copy belongs to the unit
whose file has its inode or, for a copy that is not a link, the same name and
size; it and its dep-info go with that unit. A copy no unit claims, holding
its only link, that its dep-info names, is output of a unit already removed:
the next build of that unit replaces it, so every run removes it, over budget
or not. Before this rule a copy outlived its unit and its blocks counted
against the budget for good: 51 GiB of examples in one hana worktree
(2026-10-02), so each sweep there evicted units the next lint run rebuilt.
Any other unhashed file stays.

Why hana's is 96. A budget below the working set evicts output the next lint run needs,
and because this runs after every lint run, that rebuild repeats on every
save. Replaying hana's cycle into an empty target (2026-09-15: scoped and
workspace clippy, doc and mend, the nextest test build, the external-client
fixture, the app build) came to 56.9 GiB, of which the test build alone was
33.5 GiB; the clerestory-tests suite adds 9.4 GiB. A rerun rebuilt nothing,
but sweeping that target to 48 GiB made the next cycle rebuild nearly every
workspace unit. 96 covers that working set with room for feature and profile
variants. The 24 GiB default covers the other projects' whole targets
(cargo-liner 12 GiB, nateroids 11 GiB on 2026-10-02) twice over.

The disk floor. Each budget fits the disk; together they do not (hana 96 GiB
in each of 9 worktrees, 24 in 7 other repos, CI 160 x 2), and natedev's disk
filled on 2026-10-03. When lint.conf sets sweep_free_floor_gib.<host> or
sweep_free_floor_gib, the shortfall below that free-space floor sets how much
build output goes. A target is a directory under FLOOR_ROOTS holding
.rustc_info.json, or cargo's CACHEDIR.TAG beside a build tree: a folder cargo
creates carries the tag from its first moment, and .rustc_info.json may come
only when the first build ends, or never. Cargo's registry carries the same
tag with no build tree. A folder made before a build with
CARGO_CACHE_RUSTC_INFO=0 has neither and stays unseen. Orphaned files go first
from every idle target. Then idle targets under SCRATCH_ROOTS lose
output, then the least recently used target, then by build-unit use within
each target. On 2026-10-08 the tag rule found 76 targets where
.rustc_info.json alone found 41: 31 finished scratch targets carried only the
tag, and one in its first build had reached 22 GiB unseen. A target's last use
is the newer of its .lint-sweep-used stamp and its units' last uses. A target
whose cargo locks a build holds is never touched. One sweep holds the floor at
a time and keeps every idle target's cargo locks while scanning and removing,
so a build starting there waits.
--floor-only skips the workspace sweep for disk-floor.nix's 2-minute timer.
Each such pass first runs tmp_sweep.py once an hour, which deletes agent
scratch under /tmp/claude and /tmp/claude-<uid>/<project>/ that no process
uses and where nothing changed in 4 hours, before the floor takes any build
cache. Gate copies and scratch builds there had stayed until the disk filled.
CI's targets are outside FLOOR_ROOTS, and its accounts cannot read lint.conf.
By 15:23 PDT on 2026-10-06 that timer had taken 801 GiB, from units last
used a median 3.0 h earlier. In a 60 GiB shortfall simulation, target-first order spared the
active hana targets that the prior global unit order reached.

The doc index. rustdoc rewrites doc/search.index, doc/trait.impl and
doc/type.impl when a crate finishes, and its peak memory tracks what those
already hold rather than the crate it is documenting. They only grow: twelve
consecutive `cargo doc --no-deps -p hana_video` runs, each preceded by
appending a comment line that adds no documented item, grew search.index by
313 KB per run while its file count held near 1850 (2026-09-22). hana's whole
24-crate workspace documented into an empty target comes to a 21 MiB index and
never peaks above 3 GiB, but the 1.18 GiB index that a week of per-save lint
runs had left in place made that same single-crate build peak at 24.2 GiB —
near 19 GiB of resident memory per GiB of index. That overflowed an 8 GiB swap
file and held IO pressure at 90% while the CPU sat idle. So when those three
directories together pass LINT_SWEEP_DOC_INDEX_MIB (default 250, which keeps
the peak near 6 GiB), the whole doc tree goes and the next doc run rebuilds it
in one pass. Removal takes doc/.lock, the only lock a rustdoc writing HTML
holds: cargo's build locks cover compilation, which a doc build has left
behind by then.

Why a budget and not an age window. An age window only removes what active
development has stopped touching, and active development touches almost
everything: hana's target reached 123 GiB in five days (2026-09-10 to
2026-09-15), all of it inside any window that keeps the cache useful. Scoped
lint runs (-p <changed members>) change feature unification for shared
dependencies, so each package set compiles its own copies (36 syn builds in
hana's deps/), and a rustflags change strands a whole second copy of the tree
(24.5 GiB there when mold landed). cargo-sweep cannot enforce a budget either:
--maxsize counts incremental/ against the cap but never removes from it.

Last use. cargo reads a unit's fingerprint JSON whenever the unit is in a
build graph, so the newest atime of the files in .fingerprint/<unit>/ is its
last use. relatime refreshes an atime at most once a day, so eviction orders
by whole days since last use, then by compile time (newest mtime) inside a
day. A no-op build writes nothing under target/, so only the target's use
stamp records that step reliably. An incremental dir's mtime moves on every
compile of its crate. Anything that reads every fingerprint JSON resets every
atime and erases that order;
cargo-sweep --installed does exactly that, and running it before --time kept
this sweep's predecessor from removing anything at all. Listing a directory
refreshes the directory's own atime the same way, so this scan, or a du,
would mark every unit used if that atime counted; it does not.

Concurrency. cargo 1.98.1 holds flock() on .cargo-lock, .cargo-build-lock
and .cargo-artifact-lock for the whole build (checked 2026-09-15 by probing
each with LOCK_EX|LOCK_NB while a build script slept). This takes all of them
without waiting, in every build tree, before scanning, and skips the sweep if
any is held: a delete under a running build leaves cargo failing to write
into fingerprint dirs that vanished. Holding them makes a build that starts
mid-sweep wait until the sweep finishes. A test run holds none of those locks:
nextest executes its binaries straight from deps/ after the build, and on
2026-10-15 a floor sweep at test 2105 of 4072 left 1965 hana tests failing to
exec theirs. So a target is also busy while a live process runs from it, works
in it or maps a file under it (/proc/<pid>/exe, cwd, maps), or while a
cargo-nextest runs whose workspace owns it, which covers the moments between two
test binaries. A running app counts too, so its target waits for it to exit.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeGuard, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from user_zone import user_zone  # noqa: E402
if TYPE_CHECKING:
    from . import tmp_sweep
    from .config_values import config_values as config_values
    from ..notify.push_texts import RenderedPush
    from ..notify import review
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import tmp_sweep
    from config_values import config_values as config_values

GIB = 1 << 30
MIB = 1 << 20
DAY_SECONDS = 86_400
DEFAULT_BUDGET_GIB = 24.0
BUDGET_ENV = "LINT_SWEEP_BUDGET_GIB"
BUDGET_KEY = "sweep_budget_gib"
CONFIG_ENV = "LINT_CONFIG_FILE"
DEFAULT_CONFIG = os.path.join("~", ".claude", "config", "lint.conf")
GIT_DIR = ".git"
GITDIR_PREFIX = "gitdir:"
COMMONDIR_FILE = "commondir"
ALTERNATES_FILE = os.path.join("objects", "info", "alternates")
DEFAULT_DOC_INDEX_MIB = 250.0
DOC_INDEX_ENV = "LINT_SWEEP_DOC_INDEX_MIB"
DOC_DIR = "doc"
DOC_LOCK_NAME = ".lock"
# The cross-crate stores every rustdoc run reads and rewrites at its end.
MERGE_DIRS = ("search.index", "trait.impl", "type.impl")
TARGET_DIR_FLAG = "--target-dir"
LOCK_NAMES = (".cargo-lock", ".cargo-build-lock", ".cargo-artifact-lock")
PROC_DIR = "/proc"
PROC_LOCKS = "/proc/locks"
# The kernel writes /proc/locks one page per read and drops its lock between pages; a lock freed earlier in the
# list moves the next page past one line. Each extra read gives a skipped line another chance to show.
PROC_LOCKS_READS = 3
# The kernel adds this to an exe, cwd or maps path whose file was removed.
DELETED_SUFFIX = " (deleted)"
# cargo runs a subcommand as `cargo-nextest nextest …`; a wrapper may rename argv[0].
NEXTEST_ARG = "nextest"
CARGO_TARGET_DIR_ENV = "CARGO_TARGET_DIR"
HASHED_DIRS = (".fingerprint", "build", "deps", "examples")
FINGERPRINT_DIR = ".fingerprint"
INCREMENTAL_DIR = "incremental"
# The hashed directory a unit's own file stays in, and the directory, relative
# to the build tree, that cargo copies it up into.
COPY_DIRS = {"deps": "", "examples": "examples"}
DEP_INFO_SUFFIX = ".d"
# A dep-info file's first line starts with the output it describes.
DEP_INFO_HEAD = 4096
# target/debug, target/<triple>/debug, target/<custom>/<triple>/debug.
MAX_TREE_DEPTH = 3
FLOOR_KEY = "sweep_free_floor_gib"
FLOOR_ONLY_FLAG = "--floor-only"
USE_STAMP = ".lint-sweep-used"
# Every cargo target directory on natedev was under one of these (2026-10-21). ~/.cache came in when
# 421 GiB of idle targets under ~/.cache/trace-targets, where hana_catalyst agents point
# CARGO_TARGET_DIR, had stayed unseen. ~/.cache/sccache holds no .rustc_info.json and no cargo
# cache tag, so is_cargo_target never takes it.
FLOOR_ROOTS = ("~/rust", "~/.local/state", "~/.cache", "/tmp")
# Targets under these roots are scratch builds: below the floor they lose output before any other target.
SCRATCH_ROOTS = ("/tmp", "~/.cache/trace-targets")
FLOOR_LOCK = os.path.join("~", ".local", "state", "lint-sweep", "floor.lock")
# Claude Code keeps each session's scratch (scratchpad/, tasks/) in <root>/<project>/<session id>/,
# where <project> is the session's working directory with every / as -.
FLOOR_STATE_ENV = "LINT_SWEEP_STATE_DIR"
BUILDLOG_DIR_ENV = "BUILDLOG_DIR"
CI_TARGETS = ("/var/lib/hana-ci/hana-linux-1", "/var/lib/hana-ci/hana-linux-2")
GROWTH_WINDOW_SECONDS = 30 * 60
SNAPSHOT_MAX_AGE_SECONDS = 15 * 60
ALERT_INTERVAL_SECONDS = 60 * 60
# A phone push stays under this many characters (test_push_texts pins it for every catalog example).
PUSH_TEXT_LIMIT = 500
# The page names at most this many paths.
PAGE_PATHS = 4
NO_PAGE_PATHS = "No path is known: the disk measurement names no folder."
# Three trace-driven sweeps on 2026-10-04 followed about 30 GiB of unexplained
# loss each; their smallest removal was 35.2 GiB, versus 29.2 GiB at most otherwise.
UNEXPLAINED_FALL_BYTES = 10 * GIB
LARGE_REMOVAL_BYTES = 32 * GIB
# A nightly worktree's target is 5 below its root, a scratch crate's under
# /tmp/claude-<uid>/<project>/<session>/scratchpad/ up to 7.
FLOOR_SEARCH_DEPTH = 8
FLOOR_SKIP = frozenset({".git", "node_modules"})
# cargo writes this at a target directory's root: early in a build into a
# folder that already exists; into a folder it creates, when the first build
# ends or never (31 finished scratch targets had none on 2026-10-08). It
# writes CACHEDIR.TAG at once, but only into a folder it creates (mend's
# wrapper makes hana worktrees' first), so each marker covers the other's gap.
RUSTC_INFO = ".rustc_info.json"
CACHE_TAG = "CACHEDIR.TAG"
CARGO_TAG_LINE = "# This file is a cache directory tag created by cargo."
HASH_LENGTH = 16
HEX_DIGITS = frozenset("0123456789abcdef")

InodeKey = tuple[int, int]
GroupKind = Literal["unit", "incremental", "orphan"]


class CargoMetadata(TypedDict, total=False):
    target_directory: str
    build_directory: str


@dataclass
class Group:
    """One removable piece of build output and the inode links it holds."""

    kind: GroupKind
    build_tree: str
    entries: list[str] = field(default_factory=list)
    inodes: list[InodeKey] = field(default_factory=list)
    last_used: float = 0.0
    compiled: float = 0.0


# Sort key for build units; the unit with the smallest key is removed first.
RemovalOrder = Callable[[Group], tuple[float, ...]]


@dataclass
class Scan:
    blocks: dict[InodeKey, int] = field(default_factory=dict)
    links: dict[InodeKey, int] = field(default_factory=dict)
    groups: list[Group] = field(default_factory=list)
    orphans: list[Group] = field(default_factory=list)


@dataclass(frozen=True)
class FloorRecord:
    measured_at: float
    free_bytes: int
    build_cache_bytes: int
    alert_history: NoDeliveredAlert | DeliveredAlert
    push_history: NoDeliveredAlert | DeliveredAlert


@dataclass(frozen=True)
class NoFloorRecord:
    pass


NO_FLOOR_RECORD = NoFloorRecord()


@dataclass(frozen=True)
class NoDeliveredAlert:
    pass


@dataclass(frozen=True)
class DeliveredAlert:
    at: float


@dataclass(frozen=True)
class NoEarlierDiskMeasurement:
    pass


@dataclass(frozen=True)
class EarlierDiskMeasurement:
    measured_at: datetime


@dataclass(frozen=True)
class NoLargestGrowingChild:
    pass


@dataclass(frozen=True)
class LargestGrowingChild:
    path: str
    growth_bytes: int


@dataclass(frozen=True)
class OutsideCacheDirectory:
    path: str
    bytes: int
    growth_bytes: int
    largest_child: NoLargestGrowingChild | LargestGrowingChild


@dataclass(frozen=True)
class AvailableDiskMeasurement:
    measured_at: datetime
    comparison: NoEarlierDiskMeasurement | EarlierDiskMeasurement
    directories: list[OutsideCacheDirectory]
    outside_cache_growth_bytes: int


@dataclass(frozen=True)
class UnavailableDiskMeasurement:
    reason: Literal["missing", "unreadable", "old shape"]


DiskMeasurement = AvailableDiskMeasurement | UnavailableDiskMeasurement


class FloorAlertChannels(Enum):
    NATEDEV = "natedev"
    NATEDEV_AND_PHONE = "natedev_and_phone"


def unit_hash(name: str) -> str | None:
    """The unit hash cargo appends to a per-unit file or directory name."""
    stem = name.split(".", 1)[0]
    _, separator, suffix = stem.rpartition("-")
    if not separator or len(suffix) != HASH_LENGTH or not set(suffix) <= HEX_DIGITS:
        return None
    return suffix


def copied_name(name: str) -> str:
    """name without its unit hash, '-' read as '_': cargo names a copy after
    its target, hyphens kept, and the unit's own file after the crate."""
    stem, dot, rest = name.partition(".")
    if unit_hash(name) is not None:
        stem = stem[: -HASH_LENGTH - 1]
    return (stem + dot + rest).replace("-", "_")


def cargo_roots() -> list[str]:
    """Target and build directories of the workspace in the working directory."""
    proc = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--no-deps"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return []
    metadata = cast(CargoMetadata, json.loads(proc.stdout))
    candidates = [
        os.path.realpath(path)
        for path in (metadata.get("target_directory"), metadata.get("build_directory"))
        if path and os.path.isdir(path)
    ]
    roots: list[str] = []
    for path in sorted(set(candidates)):
        if not any(path.startswith(root + os.sep) for root in roots):
            roots.append(path)
    return roots


def build_trees(root: str) -> list[str]:
    """Directories under root that hold a .fingerprint/ dir."""
    trees: list[str] = []
    frontier = [root]
    for _ in range(MAX_TREE_DEPTH + 1):
        next_frontier: list[str] = []
        for directory in frontier:
            if os.path.isdir(os.path.join(directory, FINGERPRINT_DIR)):
                trees.append(directory)
                continue
            try:
                with os.scandir(directory) as entries:
                    next_frontier.extend(
                        entry.path for entry in entries if entry.is_dir(follow_symlinks=False)
                    )
            except OSError:
                continue
        frontier = next_frontier
    return trees


def is_cargo_target(directory: str) -> bool:
    """Whether cargo built into directory."""
    if os.path.isfile(os.path.join(directory, RUSTC_INFO)):
        return True
    # Cost: one more stat per directory without rustc info; reads and tree
    # searches happen only where that stat finds a tag.
    tag = os.path.join(directory, CACHE_TAG)
    if not os.path.isfile(tag):
        return False
    try:
        with open(tag, "rb") as handle:
            lines = handle.read(512).decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    return CARGO_TAG_LINE in lines and bool(build_trees(directory))


def lock_trees(trees: list[str]) -> tuple[list[int], str | None]:
    """Take every cargo lock without waiting; on a held one, release and name it."""
    held: list[int] = []
    for tree in trees:
        for name in LOCK_NAMES:
            path = os.path.join(tree, name)
            try:
                fd = os.open(path, os.O_RDONLY)
            except FileNotFoundError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                release(held)
                return [], path
            held.append(fd)
    return held, None


def release(held: list[int]) -> None:
    for fd in held:
        os.close(fd)


@dataclass(frozen=True)
class TargetUse:
    """A live process using a target directory, and how."""

    pid: int
    how: str


@dataclass(frozen=True)
class LockHeld:
    """A cargo lock a build holds in a target."""

    lock: str


@dataclass(frozen=True)
class FloorHolder:
    """A busy target the floor cannot take: its size, and the process and session that hold it when known."""

    path: str
    bytes: int
    pid: int | None
    how: str
    session: str | None


def mapped_files(maps: bytes, prefix: str) -> list[str]:
    """Each file under prefix that a /proc/<pid>/maps listing maps."""
    # A maps line ends in the mapped file's path, after a space.
    needle = b" " + os.fsencode(prefix)
    files: list[str] = []
    start = maps.find(needle)
    while start >= 0:
        end = maps.find(b"\n", start)
        end = len(maps) if end < 0 else end
        files.append(maps[start + 1:end].decode(errors="replace"))
        start = maps.find(needle, end)
    return files


def process_uses(targets: Sequence[str], proc: str = PROC_DIR) -> dict[str, TargetUse]:
    """Each target a live process uses, with one such process: its exe, its working directory or a
    file it maps lies under the target, or it is a cargo-nextest run whose workspace owns the target.

    nextest's run phase holds none of cargo's locks: it executes test binaries straight from
    deps/, and between two of them no child may be running from the target, so the nextest
    process itself counts. Without /proc (the Mac) no use can be seen.

    An exe or mapped file that reads "(deleted)" is not a use: its blocks come back only when the
    process exits, and the sweep can neither take nor break it. So a stale process whose exe and
    mappings under a target are all deleted, working outside it, does not keep the target busy: a
    hung cargo-berth resolve held 26 GiB of cargo-liner's target that way for three days (2026-10-21).
    """
    prefixes = {target: os.path.realpath(target) + os.sep for target in targets}
    found: dict[str, TargetUse] = {}
    try:
        names = [name for name in os.listdir(proc) if name.isdigit()]
    except OSError:
        return found
    for name in names:
        left = [target for target in prefixes if target not in found]
        if not left:
            break
        base = os.path.join(proc, name)
        try:
            exe = os.readlink(os.path.join(base, "exe"))
            cwd = os.readlink(os.path.join(base, "cwd"))
            with open(os.path.join(base, "maps"), "rb") as handle:
                maps = handle.read()
            with open(os.path.join(base, "cmdline"), "rb") as handle:
                argv = [arg.decode(errors="replace") for arg in handle.read().split(b"\0") if arg]
        except OSError:
            continue
        pid = int(name)
        for target in left:
            prefix = prefixes[target]
            if exe.startswith(prefix) and not exe.endswith(DELETED_SUFFIX):
                found[target] = TargetUse(pid, f"runs {exe}")
            elif (cwd + os.sep).startswith(prefix):
                found[target] = TargetUse(pid, f"works in {cwd}")
            else:
                path = next((path for path in mapped_files(maps, prefix) if not path.endswith(DELETED_SUFFIX)), None)
                if path is not None:
                    found[target] = TargetUse(pid, f"maps {path}")
        if NEXTEST_ARG in argv[1:2] or (argv and os.path.basename(argv[0]) == f"cargo-{NEXTEST_ARG}"):
            for target in nextest_targets(argv, cwd, os.path.join(base, "environ"), left):
                _ = found.setdefault(target, TargetUse(pid, f"runs cargo-nextest in {cwd}"))
    return found


def lock_holder(lock: str, locks: str = PROC_LOCKS) -> int | None:
    """The pid that holds flock() on lock, read from /proc/locks; None without /proc or holder.

    A read under load can skip a line (PROC_LOCKS_READS), so no holder is the answer only after every read agrees.
    """
    try:
        stat = os.stat(lock)
    except OSError:
        return None
    # "1: FLOCK  ADVISORY  WRITE <pid> <major>:<minor>:<inode> 0 EOF"; a waiter's line has "->".
    device = f"{os.major(stat.st_dev):02x}:{os.minor(stat.st_dev):02x}:{stat.st_ino}"
    for _ in range(PROC_LOCKS_READS):
        try:
            with open(locks, encoding="ascii", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            return None
        for line in lines:
            fields = line.split()
            if len(fields) > 5 and fields[1] == "FLOCK" and fields[5] == device and fields[4].isdigit():
                return int(fields[4])
    return None


def holder_sessions(pids: Sequence[int]) -> dict[int, str]:
    """The live Claude session each pid runs under: the nearest ancestor a session record names."""
    if not pids:
        return {}
    if TYPE_CHECKING:
        from ..message import process_table
        from ..message.sessions import live_sessions
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "message"))
        import process_table
        from sessions import live_sessions
    try:
        table = process_table.snapshot()
    except (OSError, subprocess.CalledProcessError):
        return {}
    names = {record["pid"]: record["name"] for record in live_sessions() if record["name"]}
    found: dict[int, str] = {}
    for pid in pids:
        process = table.get(pid)
        name = process_table.nearest_named(process, table, names) if process is not None else None
        if name is not None:
            found[pid] = name
    return found


def floor_holders(busy: dict[str, TargetUse | LockHeld], sizes: dict[str, int],
                  locks: str = PROC_LOCKS) -> list[FloorHolder]:
    """Each busy target, largest first, with the pid that holds it and that pid's session when known."""
    uses: list[tuple[str, int | None, str]] = []
    for root, use in busy.items():
        if isinstance(use, LockHeld):
            uses.append((root, lock_holder(use.lock, locks), f"holds the cargo lock {use.lock}"))
        else:
            uses.append((root, use.pid, use.how))
    sessions = holder_sessions([pid for _, pid, _ in uses if pid is not None])
    holders = [FloorHolder(root, sizes.get(root, 0), pid, how, sessions.get(pid) if pid is not None else None)
               for root, pid, how in uses]
    return sorted(holders, key=lambda holder: (-holder.bytes, holder.path))


def nextest_targets(argv: list[str], cwd: str, environ: str, targets: list[str]) -> list[str]:
    """The targets a nextest run in cwd builds into: the one --target-dir or CARGO_TARGET_DIR names,
    or else each target beside a directory that holds cwd (a workspace's default target)."""
    named = next((arg.split("=", 1)[1] for arg in argv if arg.startswith(f"{TARGET_DIR_FLAG}=")), None)
    if named is None and TARGET_DIR_FLAG in argv[:-1]:
        named = argv[argv.index(TARGET_DIR_FLAG) + 1]
    if named is None:
        try:
            with open(environ, "rb") as handle:
                entries = handle.read().split(b"\0")
        except OSError:
            entries = []
        prefix = f"{CARGO_TARGET_DIR_ENV}=".encode()
        named = next((entry[len(prefix):].decode(errors="replace") for entry in entries if entry.startswith(prefix)), None)
    if named:
        chosen = os.path.realpath(os.path.join(cwd, named))
        return [target for target in targets if os.path.realpath(target) == chosen]
    return [target for target in targets
            if (cwd + os.sep).startswith(os.path.dirname(os.path.realpath(target)) + os.sep)]


def newest_times(directory: str) -> tuple[float, float]:
    """Newest atime-or-mtime and newest mtime over a directory and its direct
    children, leaving out the directory's own atime: listing it, as this scan
    and du do, refreshes that."""
    modified = os.lstat(directory).st_mtime
    used = modified
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                child = entry.stat(follow_symlinks=False)
                used = max(used, child.st_atime, child.st_mtime)
                modified = max(modified, child.st_mtime)
    except OSError:
        pass
    return used, modified


def listing(directory: str) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as entries:
            return list(entries)
    except OSError:
        return []


def copied_up(directory: str) -> list[os.DirEntry[str]]:
    """Regular files in directory without a unit hash: the copies and their dep-info."""
    return [
        entry
        for entry in listing(directory)
        if not entry.name.startswith(".")
        and unit_hash(entry.name) is None
        and entry.is_file(follow_symlinks=False)
    ]


def dep_info_output(path: str, directory: str) -> str | None:
    """Name of the copy in directory that a dep-info file says it describes."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(DEP_INFO_HEAD)
    except OSError:
        return None
    target, separator, _ = head.partition(b":")
    if not separator:
        return None
    parent, name = os.path.split(os.fsdecode(target).replace("\\ ", " "))
    stem = os.path.basename(path)[: -len(DEP_INFO_SUFFIX)]
    if os.path.basename(parent) != os.path.basename(directory) or os.path.splitext(name)[0] != stem:
        return None
    return name


def twin(copy: os.stat_result, candidates: list[tuple[os.DirEntry[str], Group]]) -> Group | None:
    """The unit a copy came from: its file has the copy's inode, or the same size
    when cargo copied rather than linked."""
    same_size: Group | None = None
    for entry, group in candidates:
        try:
            stat = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if (stat.st_dev, stat.st_ino) == (copy.st_dev, copy.st_ino):
            return group
        if same_size is None and stat.st_size == copy.st_size:
            same_size = group
    return same_size


def attach(path: str, group: Group, roots: dict[str, Group]) -> None:
    group.entries.append(path)
    roots[path] = group


def claim_copies(
    directory: str,
    entries: list[os.DirEntry[str]],
    candidates: dict[str, list[tuple[os.DirEntry[str], Group]]],
    roots: dict[str, Group],
    orphans: Group,
) -> None:
    """Give each copy in directory, with its dep-info, to the unit it came from,
    or to orphans when that unit is gone; leave a file that matches neither."""
    dep_infos = {
        entry.name[: -len(DEP_INFO_SUFFIX)]: entry for entry in entries if entry.name.endswith(DEP_INFO_SUFFIX)
    }
    owners: dict[str, Group] = {}
    kept: set[str] = set()
    for entry in entries:
        if entry.name.endswith(DEP_INFO_SUFFIX):
            continue
        stem = os.path.splitext(entry.name)[0]
        try:
            stat = entry.stat(follow_symlinks=False)
        except OSError:
            kept.add(stem)
            continue
        group = twin(stat, candidates.get(copied_name(entry.name), []))
        if (
            group is None
            and stat.st_nlink == 1
            and stem in dep_infos
            and dep_info_output(dep_infos[stem].path, directory) is not None
        ):
            group = orphans
        if group is None:
            kept.add(stem)
            continue
        _ = owners.setdefault(stem, group)
        attach(entry.path, group, roots)
    for stem, entry in dep_infos.items():
        if stem in kept:
            continue
        group = owners.get(stem)
        if group is None:
            name = dep_info_output(entry.path, directory)
            if name is None or os.path.lexists(os.path.join(directory, name)):
                continue
            group = orphans
        attach(entry.path, group, roots)


def group_roots(trees: list[str], scan: Scan) -> dict[str, Group]:
    """Map each removable entry's path to its group, with last-use times filled in."""
    roots: dict[str, Group] = {}
    for tree in trees:
        directories = {source: os.path.normpath(os.path.join(tree, copied)) for source, copied in COPY_DIRS.items()}
        copies = {source: copied_up(directory) for source, directory in directories.items()}
        # The hashed files each copy could have come from, by copied_name.
        candidates: dict[str, dict[str, list[tuple[os.DirEntry[str], Group]]]] = {
            source: {copied_name(entry.name): [] for entry in entries if not entry.name.endswith(DEP_INFO_SUFFIX)}
            for source, entries in copies.items()
        }
        units: dict[str, Group] = {}
        for hashed in HASHED_DIRS:
            for entry in listing(os.path.join(tree, hashed)):
                digest = unit_hash(entry.name)
                if digest is None:
                    continue
                group = units.get(digest)
                if group is None:
                    group = Group(kind="unit", build_tree=tree)
                    units[digest] = group
                    scan.groups.append(group)
                attach(entry.path, group, roots)
                if hashed == FINGERPRINT_DIR and entry.is_dir(follow_symlinks=False):
                    used, compiled = newest_times(entry.path)
                    group.last_used = max(group.last_used, used)
                    group.compiled = max(group.compiled, compiled)
                if hashed in candidates:
                    sources = candidates[hashed].get(copied_name(entry.name))
                    if sources is not None:
                        sources.append((entry, group))
        for group in units.values():
            if group.last_used == 0.0:
                # No fingerprint dir survives for this hash, so nothing reads
                # these entries any more; their own mtimes are all there is.
                mtimes = [os.lstat(path).st_mtime for path in group.entries]
                group.last_used = group.compiled = max(mtimes)
        orphans = Group(kind="orphan", build_tree=tree)
        for source, directory in directories.items():
            claim_copies(directory, copies[source], candidates[source], roots, orphans)
        if orphans.entries:
            scan.orphans.append(orphans)
        for entry in listing(os.path.join(tree, INCREMENTAL_DIR)):
            _, modified = newest_times(entry.path)
            group = Group(kind="incremental", entries=[entry.path], last_used=modified,
                          compiled=modified, build_tree=tree)
            scan.groups.append(group)
            roots[entry.path] = group
    return roots


def walk(directory: str, owner: Group | None, roots: dict[str, Group], scan: Scan) -> None:
    try:
        with os.scandir(directory) as entries:
            listed = list(entries)
    except OSError:
        return
    for entry in listed:
        group = roots.get(entry.path, owner)
        try:
            stat = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if entry.is_dir(follow_symlinks=False):
            walk(entry.path, group, roots, scan)
            continue
        key = (stat.st_dev, stat.st_ino)
        scan.blocks[key] = stat.st_blocks * 512
        scan.links[key] = stat.st_nlink
        if group is not None:
            group.inodes.append(key)


def freed_by(group: Group, scan: Scan, remaining: dict[InodeKey, int]) -> int:
    """Blocks that come back once a group's links go, counting them off remaining.

    A file's blocks come back only when its last link goes, so a file with a
    link outside the group frees nothing until that link goes too.
    """
    freed = 0
    for key in group.inodes:
        remaining[key] -= 1
        if remaining[key] == 0:
            freed += scan.blocks[key]
    return freed


def unit_age(group: Group, now: float) -> tuple[float, float]:
    """Sorts the build unit unused for the most whole days first, then the oldest compile."""
    return (-int((now - group.last_used) // DAY_SECONDS), group.compiled)


def choose(
    scan: Scan, total: int, budget: int, remaining: dict[InodeKey, int], removal_order: RemovalOrder
) -> tuple[list[tuple[Group, int]], int]:
    """Groups to remove, first in removal_order first, and the size left once they go."""
    chosen: list[tuple[Group, int]] = []
    for group in sorted(scan.groups, key=removal_order):
        if total <= budget:
            break
        freed = freed_by(group, scan, remaining)
        chosen.append((group, freed))
        total -= freed
    return chosen, total


def remove(groups: list[Group]) -> int:
    failures = 0
    for group in groups:
        for path in group.entries:
            try:
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                else:
                    os.unlink(path)
            except FileNotFoundError:
                continue
            except OSError as error:
                failures += 1
                print(f"lint sweep: could not remove {path}: {error}", file=sys.stderr)
    return failures


def directory_blocks(directory: str, seen: set[InodeKey] | None = None) -> int:
    """Disk blocks under a directory, counting inodes already in seen once."""
    if seen is None:
        seen = set()
    total = 0
    frontier = [directory]
    while frontier:
        try:
            with os.scandir(frontier.pop()) as entries:
                listed = list(entries)
        except OSError:
            continue
        for entry in listed:
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if entry.is_dir(follow_symlinks=False):
                frontier.append(entry.path)
                continue
            key = (stat.st_dev, stat.st_ino)
            if key in seen:
                continue
            seen.add(key)
            total += stat.st_blocks * 512
    return total


def try_lock(path: str) -> int | None:
    """An exclusive lock on path, created if missing, without waiting; None when
    another process holds it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def prune_doc_index(roots: list[str], budget: int, dry_run: bool) -> int:
    """Remove a doc tree whose cross-crate index has grown past the budget."""
    failures = 0
    for root in roots:
        doc = os.path.join(root, DOC_DIR)
        if not os.path.isdir(doc):
            continue
        index = sum(directory_blocks(os.path.join(doc, name)) for name in MERGE_DIRS)
        if index <= budget:
            if dry_run:
                print(f"lint sweep: {doc} index is {mib(index)}, within the {mib(budget)} budget")
            continue
        # rustdoc's own lock.
        held = try_lock(os.path.join(doc, DOC_LOCK_NAME))
        if held is None:
            print(f"lint sweep: a rustdoc run holds {doc}/{DOC_LOCK_NAME}; doc kept")
            continue
        try:
            size = directory_blocks(doc)
            if not dry_run:
                try:
                    shutil.rmtree(doc)
                except OSError as error:
                    print(f"lint sweep: could not remove {doc}: {error}", file=sys.stderr)
                    failures += 1
                    continue
            verb = "would remove" if dry_run else "removed"
            print(
                f"lint sweep: {doc} index is {mib(index)}, over the {mib(budget)} budget;"
                + f" {verb} {doc} ({mib(size)}), rebuilt by the next doc run"
            )
        finally:
            os.close(held)
    return 1 if failures else 0


def gib(size: int) -> str:
    return f"{size / GIB:.1f} GiB"


def mib(size: int) -> str:
    return f"{size / MIB:.1f} MiB"


def when(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, user_zone()).strftime("%Y-%m-%d %H:%M")


def scan_roots(roots: list[str], trees: list[str]) -> Scan:
    scan = Scan()
    owners = group_roots(trees, scan)
    for root in roots:
        walk(root, None, owners, scan)
    return scan


def sweep(roots: list[str], trees: list[str], budget: int, source: str, dry_run: bool) -> int:
    scan = scan_roots(roots, trees)
    total = sum(scan.blocks.values())
    label = ", ".join(roots)
    state = "within" if total <= budget else "over"
    print(f"lint sweep: {label} is {gib(total)}, {state} the {gib(budget)} budget ({source})")
    now = time.time()
    left, failures, _ = shrink(scan, total, budget, dry_run, lambda group: unit_age(group, now))
    if left > budget:
        print(
            f"lint sweep: {gib(left)} remains over budget in output this sweep never removes"
            + " (test-run folders, files it cannot match to a build unit, doc/ under its own budget)"
        )
    return 1 if failures else 0


def shrink(
    scan: Scan, total: int, budget: int, dry_run: bool, removal_order: RemovalOrder
) -> tuple[int, int, list[tuple[Group, int]]]:
    """Remove the orphans, then groups in removal_order until total fits budget,
    and say what went; return the size left, failures, and groups taken."""
    verb, result = ("would remove", "would leave") if dry_run else ("removed", "left")
    remaining = dict(scan.links)
    failures = 0
    taken: list[tuple[Group, int]] = []
    if scan.orphans:
        orphaned = 0
        for group in scan.orphans:
            freed = freed_by(group, scan, remaining)
            orphaned += freed
            if dry_run:
                taken.append((group, freed))
            else:
                failed = remove([group])
                failures += failed
                if not failed:
                    taken.append((group, freed))
        total -= orphaned
        print(
            f"lint sweep: {verb} {sum(len(group.entries) for group in scan.orphans)} orphaned files ({gib(orphaned)}),"
            + f" copied-up output whose build unit is gone; {result} {gib(total)}"
        )
    if total <= budget:
        return total, failures, taken
    chosen, left = choose(scan, total, budget, remaining, removal_order)
    for group, freed in chosen:
        if dry_run:
            taken.append((group, freed))
        else:
            failed = remove([group])
            failures += failed
            if not failed:
                taken.append((group, freed))
    if chosen:
        units = sum(1 for group, _ in chosen if group.kind == "unit")
        uses = [group.last_used for group, _ in chosen]
        print(
            f"lint sweep: {verb} {units} build units and {len(chosen) - units} incremental dirs"
            + f" ({gib(total - left)}), last used {when(min(uses))}"
            + f" to {when(max(uses))}; {result} {gib(left)}"
        )
    return left, failures, taken


def target_dirs(roots: Sequence[str]) -> list[str]:
    """Every cargo target directory under roots."""
    found: set[str] = set()
    for root in roots:
        frontier = [(os.path.expanduser(root), 0)]
        while frontier:
            directory, depth = frontier.pop()
            if is_cargo_target(directory):
                found.add(os.path.realpath(directory))
                continue
            if depth < FLOOR_SEARCH_DEPTH:
                frontier.extend(
                    (entry.path, depth + 1)
                    for entry in listing(directory)
                    if entry.name not in FLOOR_SKIP and entry.is_dir(follow_symlinks=False)
                )
    return sorted(found)


def is_scratch_target(root: str, scratch_roots: Sequence[str]) -> bool:
    """Whether the target directory root lies under one of scratch_roots."""
    target = os.path.realpath(root)
    for scratch_root in scratch_roots:
        scratch = os.path.realpath(os.path.expanduser(scratch_root))
        try:
            if os.path.commonpath((target, scratch)) == scratch:
                return True
        except ValueError:
            continue
    return False


def target_last_use(root: str, groups: list[Group]) -> float:
    """Newest workspace step or build-unit use in a target."""
    try:
        stamp_used = os.stat(os.path.join(root, USE_STAMP)).st_mtime
    except OSError:
        stamp_used = 0.0
    return max(stamp_used, max((group.last_used for group in groups), default=0.0))


def free_bytes(path: str) -> int:
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize


def floor_bytes(values: dict[str, str], host: str) -> tuple[int | None, str | None]:
    """The free-space floor and the key it came from: (None, None) when none is
    set, (None, key) when its value is malformed."""
    for key in (f"{FLOOR_KEY}.{host}", FLOOR_KEY):
        if values.get(key):
            return size_bytes(values[key], GIB), key
    return None, None


def floor_record_path() -> Path:
    state = os.environ.get(FLOOR_STATE_ENV) or "~/.local/state/lint-sweep"
    return Path(os.path.expanduser(state)) / "floor.json"


def _finite_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def read_floor_record(path: Path) -> NoFloorRecord | FloorRecord:
    try:
        value = cast(object, json.loads(path.read_text()))
    except (OSError, ValueError):
        return NoFloorRecord()
    if not isinstance(value, dict):
        return NoFloorRecord()
    data = cast(dict[str, object], value)
    measured = data.get("measured_at")
    free = data.get("free_bytes")
    caches = data.get("build_cache_bytes")
    alert = data.get("last_alert_at")
    push = data.get("last_push_at")
    if not _finite_number(measured) or not _finite_number(free) or not _finite_number(caches):
        return NoFloorRecord()
    if alert is not None and not _finite_number(alert):
        return NoFloorRecord()
    if push is not None and not _finite_number(push):
        return NoFloorRecord()
    history = DeliveredAlert(float(alert)) if alert is not None else NoDeliveredAlert()
    push_history = DeliveredAlert(float(push)) if push is not None else NoDeliveredAlert()
    return FloorRecord(float(measured), int(free), int(caches), history, push_history)


def write_floor_record(path: Path, record: FloorRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    alert_at = record.alert_history.at if isinstance(record.alert_history, DeliveredAlert) else None
    push_at = record.push_history.at if isinstance(record.push_history, DeliveredAlert) else None
    _ = partial.write_text(json.dumps({
        "measured_at": record.measured_at,
        "free_bytes": record.free_bytes,
        "build_cache_bytes": record.build_cache_bytes,
        "last_alert_at": alert_at,
        "last_push_at": push_at,
    }) + "\n")
    _ = partial.replace(path)


def read_disk_measurement() -> DiskMeasurement:
    directory = os.environ.get(BUILDLOG_DIR_ENV) or "~/.local/state/buildlog"
    path = Path(os.path.expanduser(directory)) / "disk.json"
    try:
        raw = cast(object, json.loads(path.read_text()))
    except FileNotFoundError:
        return UnavailableDiskMeasurement("missing")
    except (OSError, ValueError):
        return UnavailableDiskMeasurement("unreadable")
    if not isinstance(raw, dict) or "outside_build_caches" not in raw or "previous_measured_at" not in raw or "outside_build_cache_totals" not in raw:
        return UnavailableDiskMeasurement("old shape")
    data = cast(dict[str, object], raw)
    try:
        measured_raw = data["measured_at"]
        previous_raw = data["previous_measured_at"]
        directories_raw = data["outside_build_caches"]
        totals_raw = data["outside_build_cache_totals"]
        if not isinstance(measured_raw, str) or not isinstance(directories_raw, list) or not isinstance(totals_raw, list):
            raise ValueError("invalid measurement")
        measured = datetime.fromisoformat(measured_raw.replace("Z", "+00:00"))
        if measured.tzinfo is None:
            raise ValueError("measurement lacks zone")
        comparison: NoEarlierDiskMeasurement | EarlierDiskMeasurement = NoEarlierDiskMeasurement()
        if previous_raw is not None:
            if not isinstance(previous_raw, str):
                raise ValueError("invalid previous measurement")
            earlier = datetime.fromisoformat(previous_raw.replace("Z", "+00:00"))
            if earlier.tzinfo is None:
                raise ValueError("previous measurement lacks zone")
            comparison = EarlierDiskMeasurement(earlier)
        directories: list[OutsideCacheDirectory] = []
        for item_raw in cast(list[object], directories_raw):
            item = item_raw
            if not isinstance(item, dict):
                raise ValueError("invalid directory")
            directory = cast(dict[str, object], item)
            name, size, growth = directory["path"], directory["bytes"], directory["growth_bytes"]
            child, child_growth = directory["largest_child_path"], directory["largest_child_growth_bytes"]
            if not isinstance(name, str) or not isinstance(size, int) or not isinstance(growth, int):
                raise ValueError("invalid directory figures")
            if child is not None and not isinstance(child, str):
                raise ValueError("invalid child")
            if child_growth is not None and not isinstance(child_growth, int):
                raise ValueError("invalid child growth")
            if (child is None) != (child_growth is None):
                raise ValueError("incomplete child")
            largest = LargestGrowingChild(child, child_growth) if child is not None and child_growth is not None else NoLargestGrowingChild()
            directories.append(OutsideCacheDirectory(name, size, growth, largest))
        growth_total = 0
        for total_raw in cast(list[object], totals_raw):
            if not isinstance(total_raw, dict):
                raise ValueError("invalid folder total")
            total = cast(dict[str, object], total_raw)
            if (not isinstance(total.get("label"), str) or not isinstance(total.get("bytes"), int)
                    or not isinstance(total.get("growth_bytes"), int)):
                raise ValueError("invalid folder figures")
            growth_total += cast(int, total["growth_bytes"])
        return AvailableDiskMeasurement(measured, comparison, directories, growth_total)
    except (KeyError, TypeError, ValueError, OverflowError):
        return UnavailableDiskMeasurement("old shape")


def pacific_time(timestamp: float | datetime) -> str:
    value = timestamp.astimezone(user_zone()) if isinstance(timestamp, datetime) else datetime.fromtimestamp(timestamp, user_zone())
    return value.strftime("%Y-%m-%d %H:%M %Z")


def movement(amount: int, increase: str, decrease: str) -> str:
    if amount > 0:
        return f"{increase} {gib(amount)}"
    if amount < 0:
        return f"{decrease} {gib(-amount)}"
    return f"stayed level at {gib(0)}"


def alert_path(path: str, limit: int) -> str:
    home = os.path.expanduser("~")
    if path == home or path.startswith(home + os.sep):
        path = "~" + path[len(home):]
    if len(path) <= limit:
        return path
    leaf = path.rsplit("/", 1)[-1]
    prefix = path[:max(0, limit - len(leaf) - 2)]
    return f"{prefix}…/{leaf}"


def disk_measurement_lines(measurement: DiskMeasurement, free_fall: int, cache_growth: int,
                           now: float, explain_fall: bool = True, path_limit: int = 256,
                           previous: NoFloorRecord | FloorRecord = NO_FLOOR_RECORD) -> list[str]:
    if isinstance(measurement, UnavailableDiskMeasurement):
        return [f"buildlog-disk measurement unavailable ({measurement.reason})."]
    age = now - measurement.measured_at.timestamp()
    measured_before_fall = (explain_fall and isinstance(previous, FloorRecord)
                            and measurement.measured_at.timestamp() <= previous.measured_at)
    stale = f"; stale by {int(age // 60)} minutes" if age > SNAPSHOT_MAX_AGE_SECONDS else ""
    if isinstance(measurement.comparison, NoEarlierDiskMeasurement):
        lines = [f"buildlog-disk measured {pacific_time(measurement.measured_at)}{stale}."]
        lines.append("No earlier buildlog-disk measurement to compare; largest directories outside build caches:")
        ranked = sorted(measurement.directories, key=lambda item: item.bytes, reverse=True)[:3]
    else:
        lines = [f"buildlog-disk measured {pacific_time(measurement.comparison.measured_at)} to {pacific_time(measurement.measured_at)}{stale}."]
        lines.append("Directories outside build caches that grew most:")
        ranked = sorted((item for item in measurement.directories if item.growth_bytes > 0), key=lambda item: item.growth_bytes, reverse=True)[:3]
        lines.append(f"Measured folders {movement(measurement.outside_cache_growth_bytes, 'grew', 'shrank')} outside build caches.")
        if explain_fall and not measured_before_fall:
            unaccounted = max(0, free_fall - cache_growth)
            covered = min(max(measurement.outside_cache_growth_bytes, 0), unaccounted)
            remainder = unaccounted - covered
            coverage = ("cover all of it" if remainder == 0
                        else f"cover {gib(covered)}; {gib(remainder)} came after {pacific_time(measurement.measured_at)} or outside measured folders")
            lines.append(f"Of {gib(unaccounted)} of the fall beyond cache growth, measured folders {coverage}.")
    if measured_before_fall:
        unaccounted = max(0, free_fall - cache_growth)
        lines.append(f"The {gib(unaccounted)} fall beyond cache growth came after buildlog-disk's last measurement at {pacific_time(measurement.measured_at)}.")
    for item in ranked:
        detail = (f"grew {gib(item.growth_bytes)}, size {gib(item.bytes)}"
                  if isinstance(measurement.comparison, EarlierDiskMeasurement) else f"size {gib(item.bytes)}")
        lines.append(f"- {alert_path(item.path, path_limit)}: {detail}")
        if (isinstance(measurement.comparison, EarlierDiskMeasurement)
                and isinstance(item.largest_child, LargestGrowingChild)
                and item.largest_child.growth_bytes > item.growth_bytes / 2):
            lines.append(f"  largest child {alert_path(item.largest_child.path, path_limit)} grew {gib(item.largest_child.growth_bytes)}")
    if not ranked:
        lines.append("No directory growth is listed in this measurement.")
    return lines


def floor_alert_text(now: float, removed: int, previous: NoFloorRecord | FloorRecord,
                     free_fall: int, cache_growth: int, reasons: list[str],
                     measurement: DiskMeasurement) -> str:
    comparison = (f"Since the previous sweep at {pacific_time(previous.measured_at)}, free space "
                  + f"{movement(-free_fall, 'rose', 'fell')} while build caches {movement(cache_growth, 'grew', 'shrank')}."
                  if isinstance(previous, FloorRecord) else "No earlier floor sweep to compare.")
    for path_limit in (256, 128, 96, 72, 56, 40, 24, 16):
        message = "\n".join([
            f"Disk floor sweep at {pacific_time(now)} removed {gib(removed)} of build caches.",
            comparison,
            f"Alert threshold: {'; '.join(reasons)}.",
            *disk_measurement_lines(measurement, free_fall, cache_growth, now,
                                    isinstance(previous, FloorRecord), path_limit, previous),
        ])
        if len(message) <= 1024:
            return message
    return message


def holder_text(holder: FloorHolder, path_limit: int = 256) -> str:
    """One busy target: its path and size, and what holds it."""
    who = f"pid {holder.pid}" if holder.pid is not None else "a process"
    return f"{alert_path(holder.path, path_limit)} ({gib(holder.bytes)}): {who} {holder.how}"


def floor_out_of_reach_text(now: float, removed: int, free: int, floor: int,
                            holders: Sequence[FloorHolder], told: frozenset[str], ci_bytes: int,
                            measurement: DiskMeasurement, failures: int) -> str:
    """The natedev report when no idle build output is left: what holds the space, and who was told."""
    def holder_line(holder: FloorHolder, path_limit: int) -> str:
        if holder.path in told:
            outcome = f"session {holder.session} was told"
        elif holder.session is not None:
            outcome = f"the message to session {holder.session} failed"
        else:
            outcome = "no session found"
        return f"- {holder_text(holder, path_limit)}; {outcome}"

    if told:
        closing = ["Each session above was told. The user gets no page while a session can end a use."]
    elif holders:
        closing = ["No session above could be told, so the user also gets a page with these paths."]
    else:
        closing = ["No target is in use, so the user also gets a page with the largest paths below."]
    for path_limit in (256, 128, 96, 72, 56, 40, 24, 16):
        message = "\n".join([
            f"Build cache cleanup removed {gib(removed)}, but natedev still has only {gib(free)} free. Builds need {gib(floor)} free. No idle build cache is left to remove.",
            *(["These target dirs are in use; the sweep removes their output when that use ends:"] if holders else []),
            *(holder_line(holder, path_limit) for holder in holders),
            f"Not swept: CI's targets ({gib(ci_bytes)}).",
            *([f"Could not remove {failures} {'path' if failures == 1 else 'paths'} this sweep; the next sweep tries again."] if failures else []),
            *closing,
            *disk_measurement_lines(measurement, 0, 0, now, False, path_limit),
        ])
        if len(message) <= 1024:
            return message
    return message


def holder_message_text(holder: FloorHolder, free: int, floor: int) -> str:
    """The message to the session whose process holds a target the floor needs."""
    return "\n".join([
        f"natedev has {gib(free)} free, and builds need {gib(floor)} free. The disk floor sweep removed all idle build output.",
        f"{holder_text(holder)}. This process is in your session, so the sweep cannot remove that output.",
        "When the process ends, the next sweep removes the output. If the process is stale, stop it.",
    ])


def page_paths(holders: Sequence[FloorHolder], measurement: DiskMeasurement) -> list[str]:
    """What holds the space, for the page: each busy target, then the largest measured folders
    outside build caches. A folder that holds another listed folder is left out, so each path is exact."""
    paths = [f"{holder.path}: frees {gib(holder.bytes)}. Process {holder.pid} uses it now and can fail." if holder.pid is not None
             else f"{holder.path}: frees {gib(holder.bytes)}. A build uses it now and can fail." for holder in holders]
    if isinstance(measurement, AvailableDiskMeasurement):
        names = [item.path for item in measurement.directories]
        leaves = [item for item in measurement.directories
                  if not any(name.startswith(item.path.rstrip(os.sep) + os.sep) for name in names)]
        paths.extend(f"{item.path}: frees {gib(item.bytes)}."
                     for item in sorted(leaves, key=lambda item: item.bytes, reverse=True))
    return paths


@dataclass(frozen=True)
class NoPhoneAlert:
    """This cleanup report goes only to the machine's session."""


NO_PHONE_ALERT = NoPhoneAlert()


def disk_floor_push(removed: int, free: int, floor: int, paths: Sequence[str]) -> RenderedPush:
    """The page sent when nothing else is left: as many of paths, in order, as fit one push."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "notify"))
    if TYPE_CHECKING:
        from ..notify.push_texts import render
    else:
        from push_texts import render

    def page(listed: str) -> RenderedPush:
        return render("sweep.disk_floor", removed=gib(removed), free=gib(free), floor=gib(floor),
                      shortfall=gib(max(0, floor - free)), paths=listed)

    for count in range(min(len(paths), PAGE_PATHS), 0, -1):
        push = page("\n\n".join(paths[:count]))
        if len(push.message) < PUSH_TEXT_LIMIT:
            return push
    return page(paths[0] if paths else NO_PAGE_PATHS)


def session_send_argv(to: str) -> list[str]:
    """send.py's argv for a disk floor message to the session named to; the text goes on stdin."""
    return [sys.executable, str(Path(__file__).resolve().parents[1] / "message/send.py"),
            "--to", to, "--from", "disk_floor", "--timeout", "30"]


def send_holder_message(session: str, message: str) -> bool:
    """Tell a session that its process holds a target the floor needs; True when send.py delivered
    the message or queued it (exit 1)."""
    try:
        result = subprocess.run(session_send_argv(session), input=message, text=True, capture_output=True, timeout=40)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"lint sweep: message to {session} failed: {error}", file=sys.stderr)
        return False
    if result.returncode in (0, 1):
        print(f"lint sweep: message to {session} {'queued' if result.returncode == 1 else 'delivered'}")
        return True
    print(f"lint sweep: message to {session} failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}",
          file=sys.stderr)
    return False


def send_floor_alert(message: str, channels: FloorAlertChannels, *,
                     phone: RenderedPush | NoPhoneAlert = NO_PHONE_ALERT) -> bool:
    scripts = Path(__file__).resolve().parents[1]
    if not TYPE_CHECKING:
        # Imported here, so modules that only read sweep's config never load the reviewer.
        sys.path.insert(0, str(scripts / "notify"))
        import review
    commands = [("message", session_send_argv("natedev"))]
    if channels is FloorAlertChannels.NATEDEV_AND_PHONE:
        if TYPE_CHECKING:
            from ..notify.push_texts import RenderedPush, user_send_argv
        else:
            from push_texts import RenderedPush, user_send_argv

        if not isinstance(phone, RenderedPush):
            raise ValueError("phone channel requires a rendered disk alert")
        command = user_send_argv(phone)
        command[1] = str(scripts / "message/send.py")
        commands.append(("phone", command))
    delivered = False
    for channel, command in commands:
        try:
            timeout = review.user_send_seconds() + 15 if channel == "phone" else 40
            result = subprocess.run(command, input=message if channel == "message" else None, text=True, capture_output=True, timeout=timeout)
            if result.returncode == 0 or (channel == "message" and result.returncode == 1):
                delivered = True
                state = "queued" if result.returncode == 1 else "delivered"
                print(f"lint sweep: {channel} alert {state}")
            else:
                print(f"lint sweep: {channel} alert failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}", file=sys.stderr)
        except (OSError, subprocess.TimeoutExpired) as error:
            print(f"lint sweep: {channel} alert failed: {error}", file=sys.stderr)
    return delivered


if TYPE_CHECKING:
    from ..build_hold import build_hold
    from ..message import sessions
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build_hold"))
    import build_hold
    import sessions


@dataclass(frozen=True)
class BuildHoldTarget:
    """A target reserved for a named build hold holder, including between steps."""

    holder: str


def build_hold_targets(targets: Sequence[str], known_trees: dict[str, list[str]]) -> dict[str, BuildHoldTarget]:
    """Protect session workspaces and recently written build trees without reading build output."""
    state = build_hold.read_holders(build_hold.holder_directory())
    if isinstance(state, build_hold.NoHolders):
        return {}
    records = sessions.live_sessions()
    protected: dict[str, BuildHoldTarget] = {}
    for holder in state.holders:
        folders: list[Path] = []
        for record in records:
            if record["sessionId"] != holder.key:
                continue
            try:
                folder = Path(os.readlink(Path(PROC_DIR) / str(record["pid"]) / "cwd")).resolve(strict=True)
            except OSError:
                continue
            if folder.is_dir():
                folders.append(folder)
        if not folders:
            print(f"lint sweep: {holder.name} holds natedev builds; its working folder was not found")
        # Allow three minutes for clock differences and filesystem timestamp granularity.
        changed_since = holder.since.timestamp() - 3 * 60
        for root in targets:
            if root in protected:
                continue
            if any(Path(root).resolve().is_relative_to(folder) for folder in folders):
                protected[root] = BuildHoldTarget(holder.name)
                continue
            if root not in known_trees:
                known_trees[root] = build_trees(root)
            paths = [os.path.join(root, USE_STAMP)]
            for tree in known_trees[root]:
                paths.extend([tree, *(os.path.join(tree, name) for name in (*HASHED_DIRS, INCREMENTAL_DIR))])
            for path in paths:
                try:
                    changed = os.stat(path).st_mtime
                except OSError:
                    continue
                if changed >= changed_since:
                    protected[root] = BuildHoldTarget(holder.name)
                    break
    return protected


def hold_floor(
    floor: int,
    dry_run: bool,
    roots: Sequence[str] = FLOOR_ROOTS,
    lock: str = FLOOR_LOCK,
    scratch_roots: Sequence[str] = SCRATCH_ROOTS,
) -> int:
    """Below the floor, take the shortfall from the least used idle targets."""
    home = os.path.expanduser("~")
    free = free_bytes(home)
    if free >= floor:
        return 0
    lock_path = os.path.expanduser(lock)
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    guard = try_lock(lock_path)
    if guard is None:
        print(f"lint sweep: {gib(free)} free, under the {gib(floor)} floor; another sweep is holding it")
        return 0
    held: list[int] = []
    try:
        record_path = floor_record_path()
        previous = read_floor_record(record_path)
        idle: list[str] = []
        trees: list[str] = []
        tree_targets: dict[str, str] = {}
        busy: dict[str, TargetUse | LockHeld | BuildHoldTarget] = {}
        targets = target_dirs(roots)
        known_trees: dict[str, list[str]] = {}
        hold_targets = build_hold_targets(targets, known_trees)
        uses = process_uses(targets)
        for root in targets:
            if root in hold_targets:
                reason = hold_targets[root]
                print(f"lint sweep: {reason.holder} holds natedev builds; {root} left alone")
                busy[root] = reason
                continue
            if root in uses:
                print(f"lint sweep: pid {uses[root].pid} {uses[root].how}; {root} left alone")
                busy[root] = uses[root]
                continue
            root_trees = known_trees[root] if root in known_trees else build_trees(root)
            root_held, blocked = lock_trees(root_trees)
            if blocked is not None:
                busy[root] = LockHeld(blocked)
                continue
            held.extend(root_held)
            idle.append(root)
            trees.extend(root_trees)
            tree_targets.update({tree: root for tree in root_trees})
        scan = scan_roots(idle, trees)
        target_groups: dict[str, list[Group]] = {root: [] for root in idle}
        for group in scan.groups:
            target_groups[tree_targets[group.build_tree]].append(group)
        target_uses = {root: target_last_use(root, groups) for root, groups in target_groups.items()}
        scratch_targets = {root: is_scratch_target(root, scratch_roots) for root in idle}
        total = sum(scan.blocks.values())
        # A held target is not walked even for accounting; its bytes stay unmeasured this pass.
        busy_bytes = {path: 0 if isinstance(use, BuildHoldTarget) else directory_blocks(path)
                      for path, use in busy.items()}
        held_bytes = sum(busy_bytes.values())
        ci_bytes = sum(directory_blocks(path) for path in CI_TARGETS)
        unchanged_caches = held_bytes + ci_bytes
        before_caches = total + unchanged_caches
        # The growth check compares this pair with the previous record. The scan takes minutes, so
        # free is read again after it: a target another process deletes during the scan is then out
        # of both figures, not out of before_caches alone. On 2026-10-25 the read from before the
        # scan reported a 122.6 GiB fall while 123 GiB was freed.
        scanned_free = free_bytes(home)
        print(
            f"lint sweep: {gib(free)} free, under the {gib(floor)} floor; choosing from {len(idle)}"
            + f" target dirs ({gib(total)}), {len(busy)} left alone while a build or test run uses them"
        )
        # The shortfall comes from the read after the scan too: on 2026-10-25 the read from before it
        # took 6.5 GiB of cache while another process had lifted free from 293.5 to 416 GiB.
        budget = total - max(0, floor - scanned_free)
        left, failures = total, 0
        chosen: list[tuple[Group, int]] = []
        if scanned_free < floor:
            ordered_at = time.time()
            left, failures, chosen = shrink(
                scan,
                total,
                budget,
                dry_run,
                lambda group: (
                    0.0 if scratch_targets[tree_targets[group.build_tree]] else 1.0,
                    target_uses[tree_targets[group.build_tree]],
                    *unit_age(group, ordered_at),
                ),
            )
        else:
            print(f"lint sweep: {gib(scanned_free)} free after the scan, not under the {gib(floor)} floor;"
                  + " nothing taken")
        taken: dict[str, int] = {}
        for group, freed in chosen:
            root = tree_targets[group.build_tree]
            taken[root] = taken.get(root, 0) + freed
        for root, size in sorted(taken.items(), key=lambda item: (-item[1], item[0])):
            if size:
                verb = "would take" if dry_run else "took"
                print(f"lint sweep: the floor {verb} {gib(size)} from {root},"
                      + f" last used {when(target_uses[root])}")
        after_free = free_bytes(home)
        print(f"lint sweep: {gib(after_free)} free")
        if not dry_run:
            now = time.time()
            # The record's free is read before its caches and the next sweep's free after its scan,
            # so a deletion during either walk can only make the computed fall smaller.
            after_caches = sum(directory_blocks(path) for path in idle) + unchanged_caches
            removed = max(0, before_caches - after_caches)
            history = previous.alert_history if isinstance(previous, FloorRecord) else NoDeliveredAlert()
            push_history = previous.push_history if isinstance(previous, FloorRecord) else NoDeliveredAlert()
            push_due = isinstance(push_history, NoDeliveredAlert) or now - push_history.at >= ALERT_INTERVAL_SECONDS
            # (user, 2026-10-05) Only an exhausted sweep still under the floor reaches the phone; other alerts reach natedev alone.
            # (2026-10-21) It never asks the user to free space by hand. Each session whose process
            # holds a busy target is told; the user gets a page, with exact paths, only when no
            # session could be told.
            if left > budget and after_free < floor and push_due:
                holders = floor_holders({root: use for root, use in busy.items()
                                         if not isinstance(use, BuildHoldTarget)}, busy_bytes)
                holders.extend(FloorHolder(root, 0, None, f"{use.holder} holds natedev builds", None)
                               for root, use in busy.items() if isinstance(use, BuildHoldTarget))
                told = frozenset(holder.path for holder in holders if holder.session is not None
                                 and send_holder_message(holder.session, holder_message_text(holder, after_free, floor)))
                measurement = read_disk_measurement()
                message = floor_out_of_reach_text(now, removed, after_free, floor, holders, told,
                                                  ci_bytes, measurement, failures)
                if told:
                    delivered = send_floor_alert(message, FloorAlertChannels.NATEDEV)
                else:
                    page = disk_floor_push(removed, after_free, floor, page_paths(holders, measurement))
                    delivered = send_floor_alert(message, FloorAlertChannels.NATEDEV_AND_PHONE, phone=page)
                if delivered or told:
                    push_history = DeliveredAlert(time.time())
            elif removed > 0 and (isinstance(history, NoDeliveredAlert) or now - history.at >= ALERT_INTERVAL_SECONDS):
                free_fall = previous.free_bytes - scanned_free if isinstance(previous, FloorRecord) else 0
                cache_growth = before_caches - previous.build_cache_bytes if isinstance(previous, FloorRecord) else 0
                recent = isinstance(previous, FloorRecord) and 0 <= now - previous.measured_at <= GROWTH_WINDOW_SECONDS
                growth_fired = recent and free_fall - cache_growth > UNEXPLAINED_FALL_BYTES
                removal_fired = removed > LARGE_REMOVAL_BYTES
                if growth_fired or removal_fired:
                    reasons: list[str] = []
                    if growth_fired:
                        reasons.append("free space fell more than 10 GiB beyond build-cache growth")
                    if removal_fired:
                        reasons.append("removed more than 32 GiB in one sweep")
                    message = floor_alert_text(now, removed, previous, free_fall, cache_growth,
                                               reasons, read_disk_measurement())
                    if send_floor_alert(message, FloorAlertChannels.NATEDEV):
                        history = DeliveredAlert(time.time())
            write_floor_record(record_path, FloorRecord(now, after_free, after_caches, history, push_history))
        return 1 if failures else 0
    finally:
        release(held)
        os.close(guard)


def size_bytes(raw: str, unit: int) -> int | None:
    """raw as a non-negative number of units, in bytes; None for anything else."""
    try:
        value = float(raw)
    except ValueError:
        return None
    return int(value * unit) if math.isfinite(value) and value >= 0 else None


def repo_name(directory: str) -> str | None:
    """Name of the directory holding the git common dir of the repo directory is
    in: every worktree of a repo shares it, and a scratch clone takes the name of
    the repo it was cloned from (source_git_dir). Read from the .git files rather
    than from git, which a bare PATH may not reach."""
    common = git_common_dir(directory)
    if common is None:
        return None
    return os.path.basename(os.path.dirname(source_git_dir(common))) or None


def git_common_dir(directory: str) -> str | None:
    """The git dir every worktree of directory's repo shares."""
    current = os.path.abspath(directory)
    while not os.path.lexists(os.path.join(current, GIT_DIR)):
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    common = os.path.join(current, GIT_DIR)
    if os.path.isfile(common):
        # A linked worktree: .git names its private git dir, whose commondir
        # file leads back to the shared one.
        try:
            with open(common, encoding="utf-8") as handle:
                pointer = handle.readline().strip()
        except OSError:
            return None
        if not pointer.startswith(GITDIR_PREFIX):
            return None
        common = os.path.join(current, pointer[len(GITDIR_PREFIX) :].strip())
        try:
            with open(os.path.join(common, COMMONDIR_FILE), encoding="utf-8") as handle:
                common = os.path.join(common, handle.readline().strip())
        except FileNotFoundError:
            pass
        except OSError:
            return None
    return os.path.normpath(common)


def source_git_dir(common: str) -> str:
    """The git dir a scratch clone came from, followed to its end: a
    `git clone --shared` copy borrows its objects (alternates), and a plain local
    clone names its source as a local origin. A merge check's clone of hana is
    hana for the Mac mirror and the sweep budget. A git dir with neither is its
    own answer."""
    seen = {common}
    while True:
        source = borrowed_git_dir(common) or local_origin_git_dir(common)
        if source is None or source in seen:
            return common
        seen.add(source)
        common = source


def borrowed_git_dir(common: str) -> str | None:
    try:
        with open(os.path.join(common, ALTERNATES_FILE), encoding="utf-8") as handle:
            objects = handle.readline().strip()
    except OSError:
        return None
    if not objects:
        return None
    return os.path.dirname(os.path.normpath(os.path.join(common, "objects", objects)))


def local_origin_git_dir(common: str) -> str | None:
    """The shared git dir of origin when origin is a directory on this machine."""
    try:
        with open(os.path.join(common, "config"), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    in_origin = False
    for line in lines:
        text = line.strip()
        if text.startswith("["):
            in_origin = text == '[remote "origin"]'
        elif in_origin and text.partition("=")[0].strip() == "url":
            url = text.partition("=")[2].strip().removeprefix("file://")
            if not os.path.isabs(url) or not os.path.isdir(url):
                return None
            return git_common_dir(url)
    return None


def budget_bytes(environ: dict[str, str], config: str, repo: str | None) -> tuple[int | None, str]:
    """The sweep budget, None when its value is malformed, and where it came from."""
    raw = environ.get(BUDGET_ENV, "")
    if raw:
        return size_bytes(raw, GIB), f"{BUDGET_ENV} in the environment"
    values = config_values(config)
    keys = [f"{BUDGET_KEY}.{repo}", BUDGET_KEY] if repo else [BUDGET_KEY]
    for key in keys:
        if values.get(key):
            return size_bytes(values[key], GIB), f"{key} in {config}"
    return int(DEFAULT_BUDGET_GIB * GIB), f"the default, no {BUDGET_KEY} in {config}"


def doc_index_bytes() -> int | None:
    raw = os.environ.get(DOC_INDEX_ENV, "")
    if not raw:
        return int(DEFAULT_DOC_INDEX_MIB * MIB)
    return size_bytes(raw, MIB)


def main(argv: list[str]) -> int:
    dry_run = False
    floor_only = False
    target_dir: str | None = None
    args = iter(argv)
    for arg in args:
        if arg == "--dry-run":
            dry_run = True
        elif arg == FLOOR_ONLY_FLAG:
            floor_only = True
        elif arg == TARGET_DIR_FLAG:
            target_dir = next(args, None)
            if target_dir is None:
                print(f"lint sweep: {TARGET_DIR_FLAG} needs a directory", file=sys.stderr)
                return 2
        else:
            print(f"lint sweep: unknown argument {arg}", file=sys.stderr)
            return 2
    config = os.path.expanduser(os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG)
    floor, floor_key = floor_bytes(config_values(config), socket.gethostname())
    if floor is None and floor_key is not None:
        print(f"lint sweep: {floor_key} in {config} must be a non-negative number of GiB", file=sys.stderr)
        return 2
    status = 0 if floor_only else sweep_workspace(target_dir, config, dry_run)
    if floor_only:
        status = tmp_sweep.run_if_due(dry_run) or status
    if floor is not None:
        status = hold_floor(floor, dry_run) or status
    return status


def sweep_workspace(target_dir: str | None, config: str, dry_run: bool) -> int:
    """Sweep target_dir, or the working directory's workspace, to its budget."""
    budget, source = budget_bytes(dict(os.environ), config, repo_name(os.getcwd()))
    if budget is None:
        print(f"lint sweep: {source} must be a non-negative number of GiB", file=sys.stderr)
        return 2
    doc_budget = doc_index_bytes()
    if doc_budget is None:
        print(f"lint sweep: {DOC_INDEX_ENV} must be a non-negative number of MiB", file=sys.stderr)
        return 2
    if target_dir is None:
        roots = cargo_roots()
        if not dry_run:
            for root in roots:
                try:
                    Path(root, USE_STAMP).touch()
                except OSError:
                    pass
    else:
        roots = [os.path.realpath(target_dir)] if os.path.isdir(target_dir) else []
    if not roots:
        print("lint sweep: no target directory to sweep")
        return 0
    uses = process_uses(roots)
    if uses:
        root, use = next(iter(uses.items()))
        print(f"lint sweep: pid {use.pid} {use.how}; {root} skipped")
        return 0
    trees = [tree for root in roots for tree in build_trees(root)]
    held, blocked = lock_trees(trees)
    if blocked is not None:
        print(f"lint sweep: a cargo build holds {blocked}; skipped")
        return 0
    try:
        doc_status = prune_doc_index(roots, doc_budget, dry_run)
        return sweep(roots, trees, budget, source, dry_run) or doc_status
    finally:
        release(held)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
