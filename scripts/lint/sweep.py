#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
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
build output goes. A target is a directory under the configured sweep_floor_roots holding
.rustc_info.json, or Cargo's cache tag beside a build tree: a folder cargo
creates carries the tag from its first moment, and .rustc_info.json may come
only when the first build ends, or never. Cargo's registry carries the same
tag with no build tree. A folder made before a build with
CARGO_CACHE_RUSTC_INFO=0 has neither and stays unseen. Orphaned files go first
from every idle target. Then whole takes and build-unit batches share target
last-use order, oldest first, then build-unit use within each target. Recent targets are not
held back. Each scan under the floor records its shortfall and idle targets.
On 2026-10-08 the tag rule found 76 targets where
.rustc_info.json alone found 41: 31 finished scratch targets carried only the
tag, and one in its first build had reached 22 GiB unseen. A target's last use
is the newer of its use stamp and its units' last uses. A target
whose cargo locks a build holds is never touched. One sweep holds the floor at
a time. Candidate and size scans take no cargo locks. Up to four independent target
scans run together, and their inode accounting is combined before selection. Only one target's locks
are taken for deletion, without waiting, and released before the next target.
The pass stops once successful deletions cover the shortfall. Changed output
and targets used since candidate discovery began are skipped. Output checks
compare identity, size and mtime; unlinking another hard link does not change them.
For real floor and budget passes, each unlink uses the current link count:
only a successful last-link unlink credits file blocks. A failed or partial
removal credits only the files it removed. Dry runs use the scan's link counts
as an estimate and do not delete files.
--floor-only skips the workspace sweep for disk-floor.nix's 2-minute timer.
Each such pass first runs tmp_sweep.py once an hour, which deletes agent
scratch under /tmp/claude and /tmp/claude-<uid>/<project>/ that no process
uses and where nothing changed in 4 hours, before the floor takes any build
cache. Gate copies and scratch builds there had stayed until the disk filled.
CI's targets are outside the configured sweep_floor_roots, and its accounts cannot read lint.conf.
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
last use. relatime refreshes an atime at most once a day. Within one target,
the floor and budget order by whole days since last use, then by compile time
(newest mtime) inside a day. The budget puts recent units last. A no-op build writes nothing under target/, so only the target's use
stamp records that step reliably. An incremental dir's mtime moves on every
compile of its crate. Anything that reads every fingerprint JSON resets every
atime and erases that order;
cargo-sweep --installed does exactly that, and running it before --time kept
this sweep's predecessor from removing anything at all. Listing a directory
refreshes the directory's own atime the same way, so this scan, or a du,
would mark every unit used if that atime counted; it does not.

Removal evidence. Budget and floor takes append one sweep_trim build-log row
per target, with confirmed freed bytes, whole or partial extent and the cause.
Each floor row is written after that target's deletions, before its locks are
released. Dry runs write no trim rows. The read-only rebuilds --days N command
combines them with the journal and legacy removals.jsonl rows; nothing writes
the legacy file.

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
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import fcntl
import hashlib
import io
import json
import math
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Collection, Generator, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, redirect_stdout
from contextvars import ContextVar
from copy import copy
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from functools import cache, cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeGuard, cast, overload, override

from scripts.message import send_budget, send_result, session_send
from scripts.message.session_send import session_send_argv
from scripts.lib import file_lock
from scripts.lib.file_lock import PROC_LOCKS as PROC_LOCKS
from scripts.lib import conf
from scripts.lib.cargo_target_config import (
    CargoFolderSetting,
    cargo_folder_settings,
    cargo_home_folder,
    cargo_workspace_manifest,
)
from scripts.lib.process_failure import failure_detail
from scripts.lib.user_zone import user_zone  # noqa: E402
if TYPE_CHECKING:
    from scripts.lint import tmp_sweep
    from scripts.copies import copies
    from scripts.lib import scratch
    from scripts.lib.atomic_file import write_text_atomic
    from scripts.buildlog import record, store
    from scripts.buildlog.step_outputs import (AbsoluteRecordedRoot, KnownStepStart, RecordedStepOutput, RecordedStepOutputs,
                                              TargetFolderOverride, recorded_output_roots)
    from scripts.notify.push_texts import RenderedPush
    from scripts.notify import review
else:
    from scripts.copies import copies
    from scripts.lib import scratch
    from scripts.lib.atomic_file import write_text_atomic
    from scripts.buildlog import store
    from scripts.buildlog.step_outputs import (AbsoluteRecordedRoot, KnownStepStart, RecordedStepOutput, RecordedStepOutputs,
                                              TargetFolderOverride, recorded_output_roots)

from scripts.lib.conf import UseConfiguredValue
from scripts.lib import copy_record
from scripts.lib.cargo_output import CargoOutputRoots, CargoOutputUnavailable, read_cargo_output_roots
from scripts.mac_test import floor_claims
from scripts.mac_test.mirror_root import MacMirrorRoot

GIB = 1 << 30
MIB = 1 << 20
DAY_SECONDS = 86_400
BUDGET_ENV = "LINT_SWEEP_BUDGET_GIB"
BUDGET_KEY = "sweep_budget_gib"
GIT_DIR = ".git"
GITDIR_PREFIX = "gitdir:"
COMMONDIR_FILE = "commondir"
ALTERNATES_FILE = os.path.join("objects", "info", "alternates")
DOC_INDEX_ENV = "LINT_SWEEP_DOC_INDEX_MIB"
DOC_DIR = "doc"
DOC_LOCK_NAME = ".lock"
# The cross-crate stores every rustdoc run reads and rewrites at its end.
MERGE_DIRS = ("search.index", "trait.impl", "type.impl")
TARGET_DIR_FLAG = "--target-dir"
# natedev's installed ~/.claude reflog first contains 2ae242c18 at this time:
# 1548db836 HEAD@{2026-11-05T03:31:36-05:00}: merge 1548db8365b32844fb8543dcaa73cfa14a2b8d20: Fast-forward
TARGET_OVERRIDE_RECORDER_CUT = datetime(2026, 11, 5, 8, 31, 36, tzinfo=UTC)
LOCK_NAMES = (".cargo-lock", ".cargo-build-lock", ".cargo-artifact-lock")
PROC_DIR = "/proc"
# The kernel adds this to an exe, cwd or maps path whose file was removed.
DELETED_SUFFIX = " (deleted)"
# cargo runs a subcommand as `cargo-nextest nextest …`; a wrapper may rename argv[0].
NEXTEST_ARG = "nextest"
HASHED_DIRS = (".fingerprint", "build", "deps", "examples")
FINGERPRINT_DIR = ".fingerprint"
INCREMENTAL_DIR = "incremental"
# The hashed directory a unit's own file stays in, and the directory, relative
# to the build tree, that cargo copies it up into.
COPY_DIRS = {"deps": "", "examples": "examples"}
DEP_INFO_SUFFIX = ".d"
# A dep-info file's first line starts with the output it describes.
# target/debug, target/<triple>/debug, target/<custom>/<triple>/debug.
FLOOR_KEY = "sweep_free_floor_gib"
RECENT_KEY = "recent_minutes"
FLOOR_LOCK_ALERT_KEY = "floor_lock_alert_minutes"
FLOOR_ONLY_FLAG = "--floor-only"
USE_STAMP = scratch.USE_STAMP
# Every cargo target directory on natedev was under one of these (2026-10-21). ~/.cache came in when
# 421 GiB of idle targets under ~/.cache/trace-targets, where hana_catalyst agents point
# CARGO_TARGET_DIR, had stayed unseen. ~/.cache/sccache holds no .rustc_info.json and no cargo
# cache tag, so is_cargo_target never takes it.
# Claude Code keeps each session's scratch (scratchpad/, tasks/) in <root>/<project>/<session id>/,
# where <project> is the session's working directory with every / as -.
# A phone push stays under this many characters (test_push_texts pins it for every catalog example).
# The page names at most this many paths.
# Three trace-driven sweeps on 2026-10-04 followed about 30 GiB of unexplained
# loss each; their smallest removal was 35.2 GiB, versus 29.2 GiB at most otherwise.
# A nightly worktree's target is 5 below its root, a scratch crate's under
# /tmp/claude-<uid>/<project>/<session>/scratchpad/ up to 7.
# cargo writes this at a target directory's root: early in a build into a
# folder that already exists; into a folder it creates, when the first build
# ends or never (31 finished scratch targets had none on 2026-10-08). It
# writes its cache tag at once, but only into a folder it creates (mend's
# wrapper makes hana worktrees' first), so each marker covers the other's gap.
RUSTC_INFO = ".rustc_info.json"
CACHE_TAG = scratch.CACHE_TAG
CARGO_TAG_LINE = scratch.CARGO_TAG_LINE
HASH_LENGTH = 16
HEX_DIGITS = frozenset("0123456789abcdef")

InodeKey = tuple[int, int]
GroupKind = Literal["unit", "incremental", "orphan"]


@dataclass(frozen=True)
class NoRecordedEvidence:
    """The floor cannot prove that a queue step built this target."""

    path: str


@dataclass(frozen=True)
class CargoOutputIndex:
    workspaces: tuple[CargoOutputRoots, ...] = ()
    history: tuple[CargoOutputRoots, ...] = ()
    no_recorded_evidence: tuple[NoRecordedEvidence, ...] = ()
    known_trees: dict[str, list[str]] = field(default_factory=dict)

    @cached_property
    def linked_roots(self) -> dict[str, tuple[str, ...]]:
        """Connected output roots, shared by all lookups in this index."""
        connected: dict[str, set[str]] = {}
        for mapping in self.workspaces:
            target, build = str(mapping.target_directory), str(mapping.build_directory)
            linked = connected.get(target, {target}) | connected.get(build, {build})
            for root in linked:
                connected[root] = linked
        return {root: tuple(sorted(linked)) for root, linked in connected.items()}

    def roots_for(self, root: str) -> tuple[str, ...]:
        return tuple(path for path in self.linked_roots.get(root, (root,)) if os.path.isdir(path))

    @cached_property
    def output_owners(self) -> dict[Path, tuple[int, ...]]:
        owners: dict[Path, list[int]] = {}
        for position, mapping in enumerate(self.workspaces):
            for root in {mapping.target_directory, mapping.build_directory}:
                owners.setdefault(root, []).append(position)
        return {root: tuple(positions) for root, positions in owners.items()}

    def mappings_for(self, tree: Path) -> tuple[CargoOutputRoots, ...]:
        positions = {position for root in (tree, *tree.parents)
                     for position in self.output_owners.get(root, ())}
        return tuple(self.workspaces[position] for position in sorted(positions))

    def output_roots(self) -> set[str]:
        return {str(path) for item in self.workspaces
                for path in (item.target_directory, item.build_directory) if path.is_dir()}


OUTPUT_ROOTS: ContextVar[CargoOutputIndex] = ContextVar("cargo_output_roots", default=CargoOutputIndex())


@dataclass(frozen=True)
class FilesystemFloor:
    device: int
    probe_path: str
    floor_bytes: int
    targets: tuple[str, ...]
    root_device: bool
    legacy_history_safe: bool = True

    @property
    def key(self) -> str:
        return f"{os.major(self.device)}-{os.minor(self.device)}"

    @property
    def record_stem(self) -> str:
        return "floor" if self.root_device else f"floor-{self.key}"


FLOOR_FILESYSTEM: ContextVar[FilesystemFloor | UseConfiguredValue] = ContextVar(
    "floor_filesystem", default=UseConfiguredValue.CURRENT)


def on_floor_filesystem(path: str) -> bool:
    """Keep paths on the current floor filesystem. A missing target is on no filesystem."""
    filesystem = FLOOR_FILESYSTEM.get()
    try:
        return not isinstance(filesystem, FilesystemFloor) or output_device(path) == filesystem.device
    except FileNotFoundError:
        return False


def cargo_output_cache_path() -> Path:
    from scripts.lint import tmp_sweep

    return tmp_sweep.state_dir() / "cargo-output-roots.json"


def _cargo_mapping_rows(rows: object) -> tuple[CargoOutputRoots, ...]:
    if not isinstance(rows, list):
        return ()
    mappings: list[CargoOutputRoots] = []
    for value in cast(list[object], rows):
        if not isinstance(value, dict):
            continue
        row = cast(dict[str, object], value)
        paths = [row.get(key) for key in ("workspace_root", "target_directory", "build_directory")]
        if all(isinstance(path, str) and os.path.isabs(path) for path in paths):
            mappings.append(CargoOutputRoots(*(Path(cast(str, path)) for path in paths)))
    return tuple(mappings)


def _recorded_mapping_rows(rows: Iterable[RecordedStepOutput]) -> tuple[CargoOutputRoots, ...]:
    """Keep output pairs that the recorded step can prove it built."""
    mappings: list[CargoOutputRoots] = []
    for row in rows:
        target, build, folder = row.target_directory, row.build_directory, row.target_folder
        if isinstance(folder, AbsoluteRecordedRoot) and folder != target:
            continue
        fixed_recorder = (isinstance(row.newest_start, KnownStepStart)
                          and row.newest_start.instant > TARGET_OVERRIDE_RECORDER_CUT)
        if row.target_folder_override is TargetFolderOverride.PRESENT and not fixed_recorder:
            continue
        workspace = row.worktree if isinstance(row.worktree, AbsoluteRecordedRoot) else row.cwd
        if (isinstance(workspace, AbsoluteRecordedRoot) and isinstance(target, AbsoluteRecordedRoot)
                and isinstance(build, AbsoluteRecordedRoot)):
            mappings.append(CargoOutputRoots(workspace.path, target.path, build.path))
    return tuple(mappings)


def saved_cargo_output_roots() -> CargoOutputIndex:
    try:
        rows = cast(object, json.loads(cargo_output_cache_path().read_text()))
        if isinstance(rows, list):
            return CargoOutputIndex(_cargo_mapping_rows(cast(list[object], rows)))
        if isinstance(rows, dict):
            fields = cast(dict[str, object], rows)
            return CargoOutputIndex(_cargo_mapping_rows(fields.get("workspaces")),
                                    history=_cargo_mapping_rows(fields.get("history")))
    except (OSError, ValueError):
        pass
    return CargoOutputIndex()


def save_cargo_output_roots(index: CargoOutputIndex) -> None:
    path = cargo_output_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = saved_cargo_output_roots()
        merged = {item.workspace_root: item for item in saved.workspaces}
        history = set(saved.history) | set(index.history)
        current: set[Path] = set()
        for item in index.workspaces:
            if item.workspace_root in current:
                history.add(item)
                continue
            current.add(item.workspace_root)
            old = merged.get(item.workspace_root)
            if old is not None and old != item:
                history.add(old)
            merged[item.workspace_root] = item
        def rows(mappings: Sequence[CargoOutputRoots]) -> list[dict[str, str]]:
            return [{"workspace_root": str(item.workspace_root), "target_directory": str(item.target_directory),
                     "build_directory": str(item.build_directory)} for item in mappings]
        write_text_atomic(path, json.dumps({
            "workspaces": rows([item for _, item in sorted(merged.items())]),
            "history": rows(sorted(history, key=lambda item: (str(item.workspace_root), str(item.build_directory),
                                                               str(item.target_directory)))),
        }) + "\n")


def discover_cargo_outputs(roots: Sequence[str], *,
                           lint_settings: conf.ConfigRead) -> tuple[list[str], CargoOutputIndex]:
    """Map recorded step pairs first, then saved pairs. Protect every other target."""
    from scripts.buildlog import index as build_index

    known_trees: dict[str, list[str]] = {}
    discovered = target_dirs(roots, lint_settings=lint_settings, known_trees=known_trees)
    recorded = recorded_output_roots(build_index.index_path(), store.host_name())
    cached = saved_cargo_output_roots()
    mappings: tuple[CargoOutputRoots, ...] = ()
    if isinstance(recorded, RecordedStepOutputs):
        mappings = _recorded_mapping_rows(recorded.rows)
    else:
        print(f"lint sweep: recorded output roots unavailable: {recorded.reason}", file=sys.stderr)
    resolve = cache(Path.resolve)
    combined = tuple(dict.fromkeys(CargoOutputRoots(*(resolve(path) for path in (
        item.workspace_root, item.target_directory, item.build_directory)))
        for item in (*mappings, *cached.workspaces, *cached.history)))
    mapped_paths = {path for item in combined for path in (item.target_directory, item.build_directory)}
    mapped = {str(path) for path in mapped_paths if path.is_dir()}
    added = sorted(mapped - set(discovered))
    if added:
        discovered = sorted(set(discovered) | set(target_dirs(added, lint_settings=lint_settings, known_trees=known_trees)))
    protected = tuple(NoRecordedEvidence(path) for path in discovered if path not in mapped)
    index = CargoOutputIndex(combined, no_recorded_evidence=protected, known_trees=known_trees)
    return sorted(set(discovered) | mapped), index


@dataclass(frozen=True, slots=True)
class OutputVersion:
    """File identity, size and content modification time at candidate choice."""

    device: int
    inode: int
    size: int
    modified_ns: int


def output_version(stat: os.stat_result) -> OutputVersion:
    return OutputVersion(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


@dataclass
class Group:
    """One removable piece of build output and the inode links it holds."""

    kind: GroupKind
    build_tree: str
    entries: list[str] = field(default_factory=list)
    inodes: list[InodeKey] = field(default_factory=list)
    last_used: float = 0.0
    compiled: float = 0.0
    # Metadata read during the unlocked walk, checked before deleting this group.
    observed: dict[str, OutputVersion] = field(default_factory=dict)


@dataclass
class WholeTargetTake:
    """Observed contents of a target with no build units, leaving its locks and nested targets."""

    target: str
    entries: list[str] = field(default_factory=list)
    inodes: list[InodeKey] = field(default_factory=list)
    observed: dict[str, OutputVersion] = field(default_factory=dict)
    retained: set[str] = field(default_factory=set)


FloorTake = Group | WholeTargetTake


# Sort key for build units; the unit with the smallest key is removed first.
RemovalOrder = Callable[[Group], tuple[float, ...]]


class DiscoveredTargetRoots(list[str]):
    """Roots to scan, with all target roots from this pass's discovery."""

    def __init__(self, roots: Sequence[str], discovered: Sequence[str]) -> None:
        super().__init__(roots)
        self.discovered: tuple[str, ...] = tuple(discovered)


@dataclass
class ScanDirectoryObservations:
    """Directory entries and their cached stats, shared only during one scan."""

    directories: dict[str, list[os.DirEntry[str]]] = field(default_factory=dict)

    def entries(self, directory: str) -> list[os.DirEntry[str]]:
        if directory not in self.directories:
            self.directories[directory] = listing(directory)
        return self.directories[directory]

    def consume(self, directory: str) -> list[os.DirEntry[str]]:
        if directory in self.directories:
            return self.directories.pop(directory)
        return listing(directory)

    def child_times(self, directory: str) -> tuple[float, float]:
        used, modified = 0.0, 0.0
        for entry in self.entries(directory):
            try:
                child = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            used = max(used, child.st_atime, child.st_mtime)
            modified = max(modified, child.st_mtime)
        return used, modified


@dataclass
class Scan:
    discovered_targets: tuple[str, ...] = ()
    blocks: dict[InodeKey, int] = field(default_factory=dict)
    links: dict[InodeKey, int] = field(default_factory=dict)
    groups: list[Group] = field(default_factory=list)
    orphans: list[Group] = field(default_factory=list)
    target_bytes: dict[str, int] = field(default_factory=dict)
    target_links: dict[str, dict[InodeKey, int]] = field(default_factory=dict)
    whole_targets: list[WholeTargetTake] = field(default_factory=list)
    observations: ScanDirectoryObservations = field(default_factory=ScanDirectoryObservations)
    changed_targets: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class UnknownHeldRoots:
    pass


@dataclass(frozen=True)
class KnownHeldRoots:
    roots: frozenset[str]


UNKNOWN_HELD_ROOTS = UnknownHeldRoots()


@dataclass(frozen=True)
class KnownFloorLockHolder:
    pid: int
    started_at: float


@dataclass(frozen=True)
class UnreadableFloorLockHolder:
    pid: int


@dataclass(frozen=True)
class NoFloorLockHolder:
    """No lock holder was observed."""


@dataclass(frozen=True)
class DeliveredFloorLockAlert:
    pid: int
    started_at: float


@dataclass(frozen=True)
class NoFloorLockAlert:
    """No floor lock hold has been reported."""


@dataclass(frozen=True)
class ObservedUnreadableFloorHold:
    """An unreadable hold awaits a successful alert."""

    pid: int
    first_observed_at: float


@dataclass(frozen=True)
class ReportedUnreadableFloorHold:
    """Natedev received the alert for this unreadable hold."""

    pid: int
    first_observed_at: float


FloorLockAlertState = (DeliveredFloorLockAlert | NoFloorLockAlert
                       | ObservedUnreadableFloorHold | ReportedUnreadableFloorHold)


@dataclass(frozen=True)
class LiveSweepProcess:
    """The holder is a live sweep."""


@dataclass(frozen=True)
class OtherFloorProcess:
    command: str


@dataclass(frozen=True)
class UnreadableFloorProcess:
    """The holder command cannot be read."""


@dataclass(frozen=True)
class NoLockAlertNeeded:
    """This observation needs no delivery."""


@dataclass(frozen=True)
class FloorLockAlertCandidate:
    pid: int
    started_at: float
    command: str
    receipt: DeliveredFloorLockAlert | ReportedUnreadableFloorHold


@dataclass(frozen=True)
class LockAlertTransition:
    lock_alert: FloorLockAlertState
    alert: NoLockAlertNeeded | FloorLockAlertCandidate


class CrossingReceipt(Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"


@dataclass(frozen=True)
class AbovePhoneFloor:
    """No low-space crossing is open."""


@dataclass(frozen=True)
class BelowRearmLine:
    """No interval above the rearm line has started."""


@dataclass(frozen=True)
class AboveRearmLine:
    since: float


@dataclass(frozen=True)
class OpenPhoneCrossing:
    identity: str
    natedev: CrossingReceipt = CrossingReceipt.PENDING
    phone: CrossingReceipt = CrossingReceipt.PENDING
    rearm: BelowRearmLine | AboveRearmLine = BelowRearmLine()


class ReturnPushPolicy(Enum):
    ALLOWED = "allowed"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True)
class RecoveringPhoneCrossing:
    """Retire the fall key before the permitted return delivery."""

    identity: str
    receipt: CrossingReceipt = CrossingReceipt.PENDING
    retired: bool = False
    rearm: BelowRearmLine | AboveRearmLine = BelowRearmLine()
    return_policy: ReturnPushPolicy = ReturnPushPolicy.ALLOWED


PhoneCrossing = AbovePhoneFloor | OpenPhoneCrossing | RecoveringPhoneCrossing


@dataclass(frozen=True)
class NoCrossingSend:
    """This delivery does not report a low-space crossing."""


NO_CROSSING_SEND = NoCrossingSend()
UNSAVED_CROSSING_KEY = "natedev:unsaved-crossing"


@dataclass(frozen=True)
class CrossingSendKey:
    key: str
    repeat_minutes: float


@dataclass(frozen=True)
class NoFailure:
    """This scope has no failure awaiting delivery or recovery."""


@dataclass(frozen=True)
class FloorFailureEvent:
    status: int
    detail: str


@dataclass(frozen=True)
class FailurePending:
    event: FloorFailureEvent


@dataclass(frozen=True)
class FailureReported:
    """Natedev received this failure episode, which still needs recovery."""


@dataclass(frozen=True)
class FloorRecord:
    measured_at: float
    free_bytes: int
    build_cache_bytes: int
    alert_history: NoDeliveredAlert | DeliveredAlert
    push_history: NoDeliveredAlert | DeliveredAlert
    held_roots: UnknownHeldRoots | KnownHeldRoots = UNKNOWN_HELD_ROOTS
    lock_alert: FloorLockAlertState = NoFloorLockAlert()
    filesystem: str = ""
    measured_roots: tuple[str, ...] = ()
    phone_crossing: PhoneCrossing = AbovePhoneFloor()
    failure: NoFailure | FailurePending | FailureReported = NoFailure()


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


class FolderChoiceKind(Enum):
    SCRATCH = "scratch"
    TRASH = "trash"
    PROTECTED = "protected"
    HELD_OUTPUT = "held_output"
    OTHER = "other"


@dataclass(frozen=True)
class FolderChoice:
    label: str
    bytes: int
    kind: FolderChoiceKind


@dataclass(frozen=True)
class CurrentMacFolders:
    """Folder choices measured on the Mac during this crossing delivery."""

    folders: tuple[FolderChoice, ...]


@dataclass(frozen=True)
class AvailableFolderChoices:
    choices: tuple[FolderChoice, ...]


@dataclass(frozen=True)
class InsufficientFolderChoices:
    choices: tuple[FolderChoice, ...]


@dataclass(frozen=True)
class HeldOnlyFolderChoices:
    """Every measured folder holds output a build is using."""

    choices: tuple[FolderChoice, ...]


@dataclass(frozen=True)
class UnavailableFolderChoices:
    """No current folder choice is available."""


UNAVAILABLE_FOLDER_CHOICES = UnavailableFolderChoices()


FolderChoices = AvailableFolderChoices | InsufficientFolderChoices | HeldOnlyFolderChoices | UnavailableFolderChoices


class FloorAlertChannels(Enum):
    NATEDEV = "natedev"
    NATEDEV_AND_PHONE = "natedev_and_phone"


class FloorMachine(Enum):
    """The machine whose disk crossed the alert line."""

    NATEDEV = "natedev"
    MAC = "Mac"


class FloorChannelOutcome(Enum):
    NOT_ATTEMPTED = "not_attempted"
    DELIVERED = "delivered"
    QUEUED = "queued"
    FAILED = "failed"

    @property
    def accepted(self) -> bool:
        return self in (FloorChannelOutcome.DELIVERED, FloorChannelOutcome.QUEUED)


@dataclass(frozen=True)
class FloorAlertDelivery:
    natedev: FloorChannelOutcome
    phone: FloorChannelOutcome


@dataclass(frozen=True)
class UnitHash:
    value: str


@dataclass(frozen=True)
class NoUnitHash:
    pass


def unit_hash(name: str) -> UnitHash | NoUnitHash:
    """The unit hash, including a linker's hidden temporary output."""
    source = name[1:] if name.startswith(".") and name.rpartition(".")[2].isdigit() else name
    stem = source.split(".", 1)[0]
    _, separator, suffix = stem.rpartition("-")
    if not separator or len(suffix) != HASH_LENGTH or not set(suffix) <= HEX_DIGITS:
        return NoUnitHash()
    return UnitHash(suffix)


def copied_name(name: str) -> str:
    """Name of the output without Cargo's unit hash."""
    digest = unit_hash(name)
    source = name.removeprefix(".") if isinstance(digest, UnitHash) else name
    stem, dot, rest = source.partition(".")
    if isinstance(digest, UnitHash):
        stem = stem[: -HASH_LENGTH - 1]
    return (stem + dot + rest).replace("-", "_")


def cargo_roots() -> list[str]:
    """Read both output roots through the shared Cargo reader."""
    result = read_cargo_output_roots(Path.cwd())
    if isinstance(result, CargoOutputUnavailable):
        return []
    index = CargoOutputIndex((result,))
    _ = OUTPUT_ROOTS.set(index)
    return sorted(index.output_roots())


def build_trees(root: str, *, lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> list[str]:
    """Directories under root that hold a .fingerprint/ dir."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    return _build_trees(root, lint_settings.whole("sweep_max_tree_depth"), lint_settings=lint_settings)


def _build_trees(root: str, depth: int, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> list[str]:
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    trees: list[str] = []
    frontier = [root]
    for _ in range(depth + 1):
        next_frontier: list[str] = []
        for directory in frontier:
            if directory != root and is_cargo_target(directory, lint_settings=lint_settings):
                continue
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


def has_cargo_tag(directory: str) -> bool:
    """Check whether the cache tag identifies Cargo output."""
    try:
        with open(os.path.join(directory, CACHE_TAG), "rb") as handle:
            return CARGO_TAG_LINE in handle.read(512).decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False


def is_cargo_target(directory: str, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> bool:
    """Check whether Cargo built output in this directory."""
    return (os.path.isfile(os.path.join(directory, RUSTC_INFO))
            or (os.path.isfile(os.path.join(directory, CACHE_TAG)) and has_cargo_tag(directory)
                and bool(build_trees(directory, lint_settings=lint_settings))))


@dataclass(frozen=True)
class EnclosingCargoTarget:
    folder: str


@dataclass(frozen=True)
class NoEnclosingCargoTarget:
    pass


def enclosing_cargo_target(path: str, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> EnclosingCargoTarget | NoEnclosingCargoTarget:
    """Find the nearest Cargo target that contains the resolved path."""
    directory = Path(path).resolve()
    for ancestor in (directory, *directory.parents):
        if is_cargo_target(str(ancestor), lint_settings=lint_settings):
            return EnclosingCargoTarget(str(ancestor))
    return NoEnclosingCargoTarget()


@dataclass(frozen=True)
class TargetLocksAcquired:
    held: list[int]


@dataclass(frozen=True)
class TargetLockBusy:
    path: str


def target_lock_paths(trees: list[str]) -> list[str]:
    """Cargo's locks and the style lock held between Cargo commands."""
    paths = [os.path.join(tree, name) for tree in trees for name in LOCK_NAMES]
    style_targets = {parent for tree in trees for parent in (Path(tree), *Path(tree).parents)
                     if parent.name == scratch.STYLE_TARGET_NAME}
    paths.extend(str(target / scratch.STYLE_LOCK_NAME) for target in sorted(style_targets))
    return paths


def output_lock_trees(roots: Sequence[str], trees: Sequence[str], paths: Sequence[str] = ()) -> list[str]:
    """Include artifact profile locks even when only intermediates have fingerprints."""
    result = set(trees)
    for mapping in OUTPUT_ROOTS.get().workspaces:
        pair = (mapping.target_directory, mapping.build_directory)
        if not set(map(str, pair)) & set(roots):
            continue
        for tree in trees:
            for source in pair:
                if Path(tree).is_relative_to(source):
                    result.update(str(destination / Path(tree).relative_to(source)) for destination in pair)
    for root in roots:
        result.update(entry.path for entry in listing(root) if entry.is_dir(follow_symlinks=False)
                      and any(os.path.exists(os.path.join(entry.path, name)) for name in LOCK_NAMES))
    result.update(os.path.dirname(path) for path in paths if any(
        os.path.exists(os.path.join(os.path.dirname(path), name)) for name in LOCK_NAMES))
    return sorted(result)


def lock_trees(trees: list[str]) -> TargetLocksAcquired | TargetLockBusy:
    """Take every build-folder lock without waiting; on a held one, release and name it."""
    held: list[int] = []
    try:
        for path in target_lock_paths(trees):
            try:
                fd = os.open(path, os.O_RDONLY)
            except FileNotFoundError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                release(held)
                return TargetLockBusy(path)
            except BaseException:
                os.close(fd)
                raise
            held.append(fd)
    except BaseException:
        release(held)
        raise
    return TargetLocksAcquired(held)


def release(held: list[int]) -> None:
    for fd in held:
        os.close(fd)


def file_key(path: str) -> str:
    try:
        stat = os.stat(path)
        return f"{os.major(stat.st_dev):02x}:{os.minor(stat.st_dev):02x}:{stat.st_ino}"
    except OSError:
        return ""


def held_cargo_lock_files(*, lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> set[str]:
    """Observe Linux flocks without taking cargo locks during candidate choice."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    held: set[str] = set()
    for _ in range(lint_settings.whole("sweep_proc_locks_reads")):
        try:
            with open(PROC_LOCKS, encoding="ascii", errors="replace") as handle:
                for line in handle:
                    fields = line.split()
                    if len(fields) > 5 and fields[1] == "FLOCK":
                        held.add(fields[5])
        except OSError:
            break
    return held


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


@dataclass(frozen=True)
class TargetOwner:
    target: str


@dataclass(frozen=True)
class OutsideTargets:
    """No scanned target owns this path."""


class TargetOwnerIndex(Sequence[str]):
    """One pass's resolved targets and memo of the deepest owner of each path."""

    def __init__(self, targets: Sequence[str]) -> None:
        self.targets: tuple[str, ...] = tuple(targets)
        self.checked_targets: tuple[str, ...] = self.targets
        self.rechecking: bool = False
        self.by_path: dict[str, TargetOwner] = {
            os.path.realpath(target): TargetOwner(target) for target in targets
        }
        self.prefixes: tuple[str, ...] = tuple(path.rstrip(os.sep) + os.sep for path in self.by_path)
        self.path_owners: dict[str, TargetOwner | OutsideTargets] = dict(self.by_path)
        self.source_owners: dict[str, TargetOwner | OutsideTargets] = {}

    @overload
    def __getitem__(self, index: int) -> str: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[str, ...]: ...

    @override
    def __getitem__(self, index: int | slice) -> str | tuple[str, ...]:
        return self.targets[index]

    @override
    def __len__(self) -> int:
        return len(self.targets)

    def owner(self, path: str) -> TargetOwner | OutsideTargets:
        """Resolve a path from a build tree or another source once per pass."""
        owner = self.source_owners.get(path)
        if owner is None:
            owner = self.process_owner(os.path.realpath(path))
            self.source_owners[path] = owner
        return owner

    def process_owner(self, path: str) -> TargetOwner | OutsideTargets:
        """Use a kernel-resolved /proc path directly; look up each distinct path once."""
        owner = self.path_owners.get(path)
        if owner is not None:
            return owner
        if not path.startswith(self.prefixes):
            owner = OutsideTargets()
            self.path_owners[path] = owner
            return owner
        parent = path
        while True:
            owner = self.by_path.get(parent)
            if owner is not None:
                self.path_owners[path] = owner
                return owner
            parent = os.path.dirname(parent)


def mapped_files(maps: bytes) -> Iterator[str]:
    """Read each named file from a process's maps once, including paths with spaces."""
    for line in maps.splitlines():
        fields = line.split(None, 5)
        if len(fields) == 6 and fields[5].startswith(b"/"):
            yield fields[5].decode(errors="replace")


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
    owners = targets if isinstance(targets, TargetOwnerIndex) else TargetOwnerIndex(targets)
    found: dict[str, TargetUse] = {}
    checked = owners.checked_targets
    restricted = owners.rechecking
    prefixes = tuple(path.rstrip(os.sep) + os.sep for path, owner in owners.by_path.items()
                     if owner.target in checked) if restricted else ()
    exact_roots = tuple(prefix[:-1] for prefix in prefixes)
    encoded_prefixes = tuple(os.fsencode(prefix) for prefix in prefixes)

    try:
        names = [name for name in os.listdir(proc) if name.isdigit()]
    except OSError:
        return found
    for name in names:
        if len(found) == len(checked):
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
        for path, how in ((exe, "runs"), (cwd, "works in")):
            if path.endswith(DELETED_SUFFIX) and how == "runs":
                continue
            if restricted and path not in exact_roots and not path.startswith(prefixes):
                continue
            owner = owners.process_owner(path)
            if isinstance(owner, TargetOwner) and owner.target in checked:
                _ = found.setdefault(owner.target, TargetUse(pid, f"{how} {path}"))
        matching_maps = not restricted or any(prefix in maps for prefix in encoded_prefixes)
        for path in mapped_files(maps) if matching_maps else ():
            if path.endswith(DELETED_SUFFIX):
                continue
            owner = owners.process_owner(path)
            if isinstance(owner, TargetOwner) and owner.target in checked:
                _ = found.setdefault(owner.target, TargetUse(pid, f"maps {path}"))
        if NEXTEST_ARG in argv[1:2] or (argv and os.path.basename(argv[0]) == f"cargo-{NEXTEST_ARG}"):
            for target in nextest_targets(argv, cwd, os.path.join(base, "environ"), owners):
                if target in checked:
                    _ = found.setdefault(target, TargetUse(pid, f"runs cargo-nextest in {cwd}"))
    return {target: found[target] for target in owners if target in found}


def process_use(target: str, owners: TargetOwnerIndex, proc: str = PROC_DIR) -> dict[str, TargetUse]:
    """Recheck one target against live processes, sharing this pass's deepest owners."""
    check = copy(owners)
    check.checked_targets = (target,)
    check.rechecking = True
    return process_uses(check) if proc == PROC_DIR else process_uses(check, proc)


def lock_holder(lock: str, locks: str = PROC_LOCKS, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> int | None:
    """Read the lock holder with the configured number of attempts."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    return file_lock.lock_holder(lock, locks, reads=lint_settings.whole("sweep_proc_locks_reads"))


def floor_lock_holder(path: str, pid: int, command: bytes, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> KnownFloorLockHolder | UnreadableFloorLockHolder | NoFloorLockHolder:
    """Match the durable hold identity to the pid that currently owns the flock."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    try:
        raw = cast(object, json.loads(Path(path).read_text()))
        if isinstance(raw, dict):
            data = cast(dict[str, object], raw)
            started = data.get("started_at")
            if data.get("pid") == pid and _finite_number(started) and started >= 0:
                if command.rstrip(b"\0") and lock_holder(path, lint_settings=lint_settings) == pid:
                    return KnownFloorLockHolder(pid, float(started))
    except (OSError, ValueError):
        pass
    return UnreadableFloorLockHolder(pid)


def record_floor_lock(path: str, guard: int, started_at: float) -> None:
    """Save this hold's identity while the same inode stays locked."""
    with open(path, "r+", encoding="utf-8") as handle:
        if os.fstat(handle.fileno()).st_ino != os.fstat(guard).st_ino:
            raise OSError("floor lock file changed")
        _ = handle.write(json.dumps({"pid": os.getpid(), "started_at": started_at}) + "\n")
        _ = handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())


def _runs_sweep(command: bytes) -> bool:
    argv = command.rstrip(b"\0").split(b"\0")
    program = os.path.basename(argv[0])
    if program == b"sweep.py":
        return True
    if re.fullmatch(rb"python(?:\d+(?:\.\d+)*)?", program):
        for arg in argv[1:]:
            if not arg.startswith(b"-"):
                return os.path.basename(arg) == b"sweep.py"
    return False


def _command_text(command: bytes) -> str:
    """Decode a process command for a report."""
    return command.replace(b"\0", b" ").decode(errors="replace").strip()


def next_lock_alert(previous: FloorLockAlertState,
                    holder: KnownFloorLockHolder | UnreadableFloorLockHolder | NoFloorLockHolder,
                    process: LiveSweepProcess | OtherFloorProcess | UnreadableFloorProcess,
                    now: float) -> LockAlertTransition:
    """Compute the observation and delivery candidate without reading or writing state."""
    if (isinstance(holder, NoFloorLockHolder) or isinstance(process, LiveSweepProcess)) and isinstance(previous, DeliveredFloorLockAlert):
        return LockAlertTransition(previous, NoLockAlertNeeded())
    if isinstance(holder, NoFloorLockHolder) or isinstance(process, LiveSweepProcess):
        return LockAlertTransition(NoFloorLockAlert(), NoLockAlertNeeded())
    if isinstance(holder, KnownFloorLockHolder):
        receipt = DeliveredFloorLockAlert(holder.pid, holder.started_at)
        if previous == receipt:
            return LockAlertTransition(previous, NoLockAlertNeeded())
        return LockAlertTransition(NoFloorLockAlert(),
                                   FloorLockAlertCandidate(holder.pid, holder.started_at,
                                                           process.command if isinstance(process, OtherFloorProcess)
                                                           else "command is unreadable", receipt))
    if isinstance(previous, (ObservedUnreadableFloorHold, ReportedUnreadableFloorHold)) and previous.pid == holder.pid:
        observed = previous
    else:
        observed = ObservedUnreadableFloorHold(holder.pid, now)
    command = process.command if isinstance(process, OtherFloorProcess) else "command is unreadable"
    alert = (NoLockAlertNeeded() if isinstance(observed, ReportedUnreadableFloorHold)
             else FloorLockAlertCandidate(holder.pid, observed.first_observed_at, command,
                                          ReportedUnreadableFloorHold(holder.pid, observed.first_observed_at)))
    return LockAlertTransition(observed, alert)


def alert_floor_lock(path: str, free: int, floor: int, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> None:
    """Report an overdue hold once, then save its observation or delivery receipt."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    record_path = floor_record_path()
    with locked_floor_record(record_path) as previous:
        now = time.time()
        current = floor_record_or_new(previous, free)
        process: LiveSweepProcess | OtherFloorProcess | UnreadableFloorProcess = UnreadableFloorProcess()
        holder: KnownFloorLockHolder | UnreadableFloorLockHolder | NoFloorLockHolder = NoFloorLockHolder()
        pid = lock_holder(path, lint_settings=lint_settings)
        if pid is not None:
            try:
                command = (Path(PROC_DIR) / str(pid) / "cmdline").read_bytes()
            except OSError:
                command = b""
            if command:
                process = LiveSweepProcess() if _runs_sweep(command) else OtherFloorProcess(_command_text(command))
            holder = floor_lock_holder(path, pid, command, lint_settings=lint_settings)
        transition = next_lock_alert(current.lock_alert, holder, process, now)
        state = transition.lock_alert
        candidate = transition.alert
        seconds = int(lint_settings.number(FLOOR_LOCK_ALERT_KEY) * 60)
        if isinstance(candidate, FloorLockAlertCandidate) and now - candidate.started_at >= seconds:
            message = (f"natedev has {gib(free)} free, under the {gib(floor)} floor. "
                       f"The disk floor sweep cannot run: pid {candidate.pid} ({candidate.command}) "
                       f"has held floor.lock since {when(candidate.started_at)}.")
            delivery = send_floor_alert(message, lint_settings=lint_settings)
            if delivery.accepted:
                state = candidate.receipt
        if state != current.lock_alert:
            write_floor_record(record_path, replace(current, lock_alert=state))


def holder_sessions(pids: Sequence[int]) -> dict[int, str]:
    """The live Claude session each pid runs under: the nearest ancestor a session record names."""
    if not pids:
        return {}
    from scripts.message import process_table
    from scripts.message.sessions import live_sessions
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


def floor_holders(busy: Mapping[str, TargetUse | LockHeld | BuildHoldTarget | NoRecordedEvidence], sizes: dict[str, int],
                  locks: str = PROC_LOCKS, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> list[FloorHolder]:
    """Each busy target, largest first, with the pid that holds it and that pid's session when known."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    uses: list[tuple[str, int | None, str]] = []
    for root, use in busy.items():
        if isinstance(use, (BuildHoldTarget, NoRecordedEvidence)):
            continue
        if isinstance(use, LockHeld):
            uses.append((root, lock_holder(use.lock, locks, lint_settings=lint_settings), f"holds the cargo lock {use.lock}"))
        else:
            uses.append((root, use.pid, use.how))
    sessions = holder_sessions([pid for _, pid, _ in uses if pid is not None])
    holders = [FloorHolder(root, sizes.get(root, 0), pid, how, sessions.get(pid) if pid is not None else None)
               for root, pid, how in uses]
    return sorted(holders, key=lambda holder: (-holder.bytes, holder.path))


def cargo_short_hash(data: bytes) -> str:
    """Cargo's StableSipHasher128, reduced and written as little-endian hex.

    Sources: Cargo src/cargo/util/{hasher,hex}.rs at 58bab51c and
    https://github.com/rust-lang/rustc-stable-hash/blob/main/src/sip128.rs
    """
    mask = (1 << 64) - 1
    v0, v1, v2, v3 = 0x736f6d6570736575, 0x646f72616e646f83, 0x6c7967656e657261, 0x7465646279746573

    def rotate(value: int, bits: int) -> int:
        return ((value << bits) | (value >> (64 - bits))) & mask

    def rounds(count: int) -> None:
        nonlocal v0, v1, v2, v3
        for _ in range(count):
            v0 = (v0 + v1) & mask
            v2 = (v2 + v3) & mask
            v1 = rotate(v1, 13) ^ v0
            v3 = rotate(v3, 16) ^ v2
            v0 = rotate(v0, 32)
            v2 = (v2 + v1) & mask
            v0 = (v0 + v3) & mask
            v1 = rotate(v1, 17) ^ v2
            v3 = rotate(v3, 21) ^ v0
            v2 = rotate(v2, 32)

    end = len(data) - len(data) % 8
    for offset in range(0, end, 8):
        word = int.from_bytes(data[offset:offset + 8], "little")
        v3 ^= word
        rounds(1)
        v0 ^= word
    tail = int.from_bytes(data[end:], "little") | ((len(data) & 0xff) << 56)
    v3 ^= tail
    rounds(1)
    v0 ^= tail
    v2 ^= 0xee
    rounds(3)
    low = v0 ^ v1 ^ v2 ^ v3
    v1 ^= 0xdd
    rounds(3)
    high = v0 ^ v1 ^ v2 ^ v3
    return ((low * 3 + high) & mask).to_bytes(8, "little").hex()


def cargo_workspace_path_hash(manifest: Path) -> str:
    """Cargo hashes the canonical workspace manifest, then splits first two/rest.

    https://rust.googlesource.com/cargo/+/58bab51ca444e109a21ae9e0f17c27e38173a660/src/cargo/util/context/mod.rs
    Path's component bytes and boundary bits follow Rust's Unix Hash for Path:
    https://github.com/rust-lang/rust/blob/1.91.0/library/std/src/path.rs
    """
    if os.name != "posix" or sys.maxsize != (1 << 63) - 1:
        raise ValueError("Cargo workspace path hashing is unavailable on this platform")
    try:
        path = manifest.resolve(strict=True)
    except OSError:
        path = manifest
    if not path.is_absolute():
        raise ValueError("Cargo workspace manifest path is not absolute")
    data = bytearray()
    boundaries = 0
    for component in os.fsencode(path).split(b"/"):
        if component:
            data.extend(component)
            boundaries = (boundaries + len(component)) & ((1 << 64) - 1)
            boundaries = (boundaries >> 2) | ((boundaries & 3) << 62)
    data.extend(boundaries.to_bytes(8, "little"))
    hashed = cargo_short_hash(bytes(data))
    return os.path.join(hashed[:2], hashed[2:])


def cargo_build_folder(setting: CargoFolderSetting, argv: list[str], cwd: str, cargo_home: str) -> str:
    """Expand build.build-dir before resolving it against its configuration root.

    https://doc.rust-lang.org/cargo/reference/config.html#buildbuild-dir
    Unreadable workspace state and unknown templates must keep all caches busy.
    """
    value = setting.value
    replacements: dict[str, str] = {}
    if "{workspace-root}" in value or "{workspace-path-hash}" in value:
        manifest = cargo_workspace_manifest(argv, cwd, cargo_home)
        replacements["workspace-root"] = str(manifest.parent)
        if "{workspace-path-hash}" in value:
            replacements["workspace-path-hash"] = cargo_workspace_path_hash(manifest)
    if cargo_home:
        replacements["cargo-cache-home"] = os.path.realpath(os.path.join(cwd, cargo_home))
    for name in ("workspace-root", "cargo-cache-home", "workspace-path-hash"):
        value = value.replace("{" + name + "}", replacements.get(name, "{" + name + "}"))
    if "{" in value or "}" in value:
        raise ValueError("Cargo build folder template cannot be resolved")
    return os.path.realpath(setting.base / value)


def nextest_targets(argv: list[str], cwd: str, environ: str, targets: Sequence[str]) -> list[str]:
    """Resolve Cargo's artifact and intermediate folders through one busy check."""
    try:
        with open(environ, "rb") as handle:
            entries = handle.read().decode(errors="replace").split("\0")
    except OSError:
        entries = []
    environment = dict(entry.split("=", 1) for entry in entries if "=" in entry)
    settings = cargo_folder_settings(argv, cwd, environment)
    cargo_home = cargo_home_folder(cwd, environment)
    try:
        chosen = {cargo_build_folder(setting, argv, cwd, cargo_home) if key == "build-dir"
                  else os.path.realpath(setting.base / setting.value) for key, setting in settings.items()}
    except (OSError, ValueError):
        return list(targets)
    owners = targets if isinstance(targets, TargetOwnerIndex) else TargetOwnerIndex(targets)
    return [owner.target for path, owner in owners.by_path.items() if path in chosen
            or ("target-dir" not in settings
                and (cwd + os.sep).startswith(os.path.dirname(path) + os.sep))]


def newest_times(directory: os.DirEntry[str], observations: ScanDirectoryObservations) -> tuple[float, float]:
    """Use the directory mtime and immediate child times. Exclude the directory atime."""
    try:
        modified = directory.stat(follow_symlinks=False).st_mtime
    except OSError:
        return 0.0, 0.0
    used, children_modified = observations.child_times(directory.path)
    return max(modified, used), max(modified, children_modified)


def listing(directory: str) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as entries:
            return list(entries)
    except OSError:
        return []


def copied_up(directory: str, observations: ScanDirectoryObservations) -> list[os.DirEntry[str]]:
    """Regular files in directory without a unit hash: the copies and their dep-info."""
    return [
        entry
        for entry in observations.entries(directory)
        if not entry.name.startswith(".")
        and isinstance(unit_hash(entry.name), NoUnitHash)
        and entry.is_file(follow_symlinks=False)
    ]


def dep_info_output(path: str, directory: str, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> str | None:
    """Name of the copy in directory that a dep-info file says it describes."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    return _dep_info_output(path, directory, lint_settings.whole("sweep_dep_info_head_bytes"))


def _dep_info_output(path: str, directory: str, head_bytes: int) -> str | None:
    try:
        with open(path, "rb") as handle:
            head = handle.read(head_bytes)
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
    head_bytes: int,
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
            and _dep_info_output(dep_infos[stem].path, directory, head_bytes) is not None
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
            name = _dep_info_output(entry.path, directory, head_bytes)
            if name is None or os.path.lexists(os.path.join(directory, name)):
                continue
            group = orphans
        attach(entry.path, group, roots)


def group_roots(trees: list[str], scan: Scan, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> dict[str, Group]:
    """Map each removable entry's path to its group, with last-use times filled in."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    roots: dict[str, Group] = {}
    head_bytes = lint_settings.whole("sweep_dep_info_head_bytes")
    shared_units: dict[tuple[str, str, str], Group] = {}
    for tree in trees:
        tree_path = Path(tree)
        mappings = OUTPUT_ROOTS.get().mappings_for(tree_path)
        directories = {source: os.path.normpath(os.path.join(tree, copied)) for source, copied in COPY_DIRS.items()}
        paired_directories: dict[str, list[str]] = {source: [directory] for source, directory in directories.items()}
        for mapping in mappings:
            if mapping.build_directory != mapping.target_directory and tree_path.is_relative_to(mapping.build_directory):
                relative = tree_path.relative_to(mapping.build_directory)
                for source, copied in COPY_DIRS.items():
                    destination = str(mapping.target_directory / relative / copied)
                    if destination not in paired_directories[source]:
                        paired_directories[source].append(destination)
        copies = {source: [entry for directory in paths for entry in copied_up(directory, scan.observations)]
                  for source, paths in paired_directories.items()}
        # The hashed files each copy could have come from, by copied_name.
        candidates: dict[str, dict[str, list[tuple[os.DirEntry[str], Group]]]] = {
            source: {copied_name(entry.name): [] for entry in entries if not entry.name.endswith(DEP_INFO_SUFFIX)}
            for source, entries in copies.items()
        }
        units: dict[str, Group] = {}
        workspace, profile = tree, ""
        for mapping in mappings:
            matched = next((root for root in (mapping.target_directory, mapping.build_directory)
                            if tree_path.is_relative_to(root)), None)
            if matched is not None:
                workspace, profile = str(mapping.workspace_root), str(tree_path.relative_to(matched))
                break
        hashed_entries: dict[str, os.DirEntry[str]] = {}
        for hashed in HASHED_DIRS:
            for entry in scan.observations.entries(os.path.join(tree, hashed)):
                digest = unit_hash(entry.name)
                if isinstance(digest, NoUnitHash):
                    continue
                group = shared_units.get((workspace, profile, digest.value))
                if group is None:
                    group = Group(kind="unit", build_tree=tree)
                    shared_units[(workspace, profile, digest.value)] = group
                    scan.groups.append(group)
                units[digest.value] = group
                attach(entry.path, group, roots)
                hashed_entries[entry.path] = entry
                if hashed == FINGERPRINT_DIR and entry.is_dir(follow_symlinks=False):
                    used, compiled = newest_times(entry, scan.observations)
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
                mtimes: list[float] = []
                for path in group.entries:
                    try:
                        entry = hashed_entries.get(path)
                        state = entry.stat(follow_symlinks=False) if entry is not None else os.lstat(path)
                        mtimes.append(state.st_mtime)
                    except OSError:
                        continue
                group.last_used = group.compiled = max(mtimes, default=0.0)
        orphans = Group(kind="orphan", build_tree=tree)
        for source, paths in paired_directories.items():
            for directory in paths:
                claim_copies(directory, [entry for entry in copies[source] if os.path.dirname(entry.path) == directory],
                             candidates[source], roots, orphans, head_bytes)
        if orphans.entries:
            scan.orphans.append(orphans)
        for entry in scan.observations.entries(os.path.join(tree, INCREMENTAL_DIR)):
            _, modified = newest_times(entry, scan.observations)
            group = Group(kind="incremental", entries=[entry.path], last_used=modified,
                          compiled=modified, build_tree=tree)
            scan.groups.append(group)
            roots[entry.path] = group
    return roots


def walk(directory: str, owner: FloorTake | None, roots: dict[str, Group], scan: Scan, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> None:
    discovered = scan.discovered_targets or tuple(target_dirs([directory], lint_settings=lint_settings))
    nested_targets = set(discovered) - {os.path.realpath(directory)}
    target_links = scan.target_links.setdefault(directory, {})

    def visit(directory: str, owner: FloorTake | None) -> None:
        listed = scan.observations.consume(directory)
        for entry in listed:
            if entry.path in nested_targets:
                if isinstance(owner, WholeTargetTake):
                    owner.retained.add(entry.path)
                continue
            if isinstance(owner, WholeTargetTake) and (
                    entry.name == ".claude-busy" or entry.name in LOCK_NAMES
                    or entry.path == os.path.join(owner.target, "doc", ".lock")
                    or entry.name == scratch.STYLE_LOCK_NAME):
                owner.retained.add(entry.path)
                continue
            group = roots.get(entry.path, owner)
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if group is not None:
                group.observed[entry.path] = output_version(stat)
            if entry.is_dir(follow_symlinks=False):
                visit(entry.path, group)
                continue
            key = (stat.st_dev, stat.st_ino)
            scan.blocks[key] = stat.st_blocks * 512
            scan.links[key] = stat.st_nlink
            target_links[key] = target_links.get(key, 0) + 1
            if group is not None:
                group.inodes.append(key)

    visit(directory, owner)


@dataclass(frozen=True)
class ReclamationEstimate:
    """Blocks a dry run expects to free from the scan's link counts."""

    bytes: int


@dataclass(frozen=True)
class ConfirmedDeletion:
    """Blocks freed, failed attempts, and successful unlinks or directory removals."""

    bytes: int
    failures: int
    removed: int = 0
    device_bytes: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True)
class UnconfirmedReclaim:
    """Old names are gone, but their blocks may still belong to a renamed or linked inode."""

    removed: int
    paths: tuple[str, ...]


@dataclass(frozen=True)
class PendingUnlink:
    path: str
    device: int
    inode: int
    blocks: int
    links: int
    directory: bool
    modified_ns: int = 0
    size: int = 0


@overload
def removed_entry_account(entry: PendingUnlink, confirmed: Literal[True]) -> ConfirmedDeletion: ...


@overload
def removed_entry_account(entry: PendingUnlink, confirmed: Literal[False]) -> UnconfirmedReclaim: ...


def removed_entry_account(entry: PendingUnlink, confirmed: bool) -> ConfirmedDeletion | UnconfirmedReclaim:
    """Only a returned unlink of the last link proves reclaimed blocks."""
    if not confirmed:
        return UnconfirmedReclaim(1, (entry.path,))
    freed = entry.blocks if not entry.directory and entry.links == 1 else 0
    return ConfirmedDeletion(freed, 0, 1, {entry.device: freed})


@dataclass
class TrimTake:
    """A target take whose deletion account must reach the build log before it is forgotten."""

    target: str
    cause: Literal["budget take", "floor take"]
    remaining_groups: int
    at: float = 0.0
    bytes_freed: int = 0
    removed: int = 0
    pending: list[PendingUnlink] = field(default_factory=list)
    group_paths: list[str] = field(default_factory=list)
    stage: Literal["deleting", "publishing"] = "deleting"
    log_offset: int = 0
    unconfirmed_paths: list[str] = field(default_factory=list)
    output_roots: tuple[str, ...] = ()

    def save(self) -> None:
        write_text_atomic(trim_intent_path(self.target), json.dumps(asdict(self)) + "\n")

    def prepare(self, group: FloorTake) -> None:
        self.pending = []
        self.group_paths = list(group.entries)
        for path in group.observed:
            try:
                observed = os.lstat(path)
            except OSError:
                continue
            self.pending.append(PendingUnlink(path, observed.st_dev, observed.st_ino,
                                              observed.st_blocks * 512, observed.st_nlink,
                                              stat.S_ISDIR(observed.st_mode), observed.st_mtime_ns, observed.st_size))
        self.at = time.time()
        self.save()

    def confirm(self, account: ConfirmedDeletion | UnconfirmedReclaim) -> None:
        if isinstance(account, ConfirmedDeletion):
            self.bytes_freed += account.bytes
            if account.removed:
                self.at = time.time()
        else:
            self.unconfirmed_paths.extend(account.paths)
        self.removed += account.removed
        if self.group_paths and all(
                not os.path.lexists(path) or path in self.unconfirmed_paths for path in self.group_paths):
            self.remaining_groups = max(0, self.remaining_groups - 1)
        unfinished_pair = len(self.output_roots) > 1 and any(
            os.path.lexists(path) and path not in self.unconfirmed_paths for path in self.group_paths)
        if not unfinished_pair:
            self.pending = []
            self.group_paths = []
        self.save()

    def recover(self) -> ConfirmedDeletion | UnconfirmedReclaim:
        """A missing or replaced name proves removal, never reclaimed blocks."""
        paths: list[str] = []
        for entry in self.pending:
            try:
                current = os.lstat(entry.path)
                if (current.st_dev, current.st_ino) == (entry.device, entry.inode):
                    continue
            except FileNotFoundError:
                pass
            account = removed_entry_account(entry, False)
            paths.extend(path for path in account.paths if path not in self.unconfirmed_paths)
        result = UnconfirmedReclaim(len(paths), tuple(paths)) if paths else ConfirmedDeletion(0, 0)
        if len(self.output_roots) > 1 and self.stage == "deleting" and self.pending:
            remaining: list[str] = []
            for path in list(self.group_paths):
                observed = {entry.path: OutputVersion(entry.device, entry.inode, entry.size, entry.modified_ns)
                            for entry in self.pending if Path(entry.path).is_relative_to(path)}
                retry = Group("unit", self.target, entries=[path], observed=observed)
                if observed and group_unchanged(retry):
                    remaining.append(path)
                else:
                    self.group_paths.remove(path)
                    self.remaining_groups = max(1, self.remaining_groups)
            if remaining:
                retry = Group("unit", self.target, entries=remaining)
                account = remove([retry])
                self.bytes_freed += account.bytes
                self.removed += account.removed
                if account.removed:
                    self.at = time.time()
        self.confirm(result)
        return result

    def publish(self) -> bool:
        """An append seen after a stop is the same row, not another trim."""
        path = trim_intent_path(self.target)
        if len(self.output_roots) > 1 and self.pending:
            return False
        if not self.removed:
            path.unlink(missing_ok=True)
            return True
        if self.stage == "deleting":
            log_path = store.host_file(store.host_name(), self.at)
            self.log_offset = store.read_last_whole_row_end(log_path)
            self.stage = "publishing"
            self.save()
        if not trim_row_recorded(self):
            extent = "whole" if self.remaining_groups == 0 else "partial"
            if not record_sweep_removal(self.target, self.cause, self.bytes_freed, extent, self.at):
                return False
        path.unlink(missing_ok=True)
        return True


TRIM_TAKES: ContextVar[dict[str, TrimTake]] = ContextVar("sweep_trim_takes", default={})


def trim_intent_path(target: str) -> Path:
    identity = hashlib.sha256(os.path.abspath(target).encode()).hexdigest()
    return floor_record_path().parent / "trims" / (identity + ".json")


def read_trim_intent(path: Path) -> TrimTake:
    decoded = cast(object, json.loads(path.read_text()))
    if not isinstance(decoded, dict):
        raise ValueError(f"invalid trim intent: {path}")
    raw = cast(dict[str, object], decoded)
    target, cause, stage = raw["target"], raw["cause"], raw["stage"]
    counts = [raw[name] for name in ("remaining_groups", "bytes_freed", "removed", "log_offset")]
    pending, group_paths, at = raw["pending"], raw["group_paths"], raw["at"]
    unconfirmed = raw.get("unconfirmed_paths", [])
    output_roots = raw.get("output_roots", [])
    if (not isinstance(target, str) or not os.path.isabs(target) or trim_intent_path(target) != path
            or cause not in ("budget take", "floor take") or stage not in ("deleting", "publishing")
            or not all(isinstance(value, int) and value >= 0 for value in counts) or not _finite_number(at)
            or not isinstance(pending, list) or not isinstance(group_paths, list)
            or not isinstance(unconfirmed, list) or not isinstance(output_roots, list)
            or not all(isinstance(root, str) and os.path.isabs(root) for root in cast(list[object], output_roots))
            or not all(isinstance(value, str) for value in cast(list[object], unconfirmed))
            or not all(isinstance(value, str) for value in cast(list[object], group_paths))):
        raise ValueError(f"invalid trim intent: {path}")
    try:
        _ = store.utc_iso(float(at))
    except (ValueError, OverflowError, OSError) as error:
        raise ValueError(f"invalid trim intent time: {path}: {error}") from error
    entries: list[PendingUnlink] = []
    for entry in cast(list[object], pending):
        if not isinstance(entry, dict):
            raise ValueError(f"invalid unlink in {path}")
        fields = cast(dict[str, object], entry)
        name, directory = fields["path"], fields["directory"]
        numbers = [fields[key] for key in ("device", "inode", "blocks", "links")]
        if (not isinstance(name, str) or not isinstance(directory, bool)
                or not all(isinstance(value, int) and value >= 0 for value in numbers)):
            raise ValueError(f"invalid unlink in {path}")
        entries.append(PendingUnlink(name, cast(int, numbers[0]), cast(int, numbers[1]),
                                     cast(int, numbers[2]), cast(int, numbers[3]), directory,
                                     cast(int, fields.get("modified_ns", 0)), cast(int, fields.get("size", 0))))
    return TrimTake(target, cause, cast(int, counts[0]),
                    float(at), cast(int, counts[1]), cast(int, counts[2]), entries,
                    cast(list[str], group_paths), stage, cast(int, counts[3]), cast(list[str], unconfirmed),
                    tuple(cast(list[str], output_roots)))


@dataclass(frozen=True)
class DamagedTrimIntentSetAside:
    path: Path


def load_trim_for_recovery(path: Path) -> TrimTake | DamagedTrimIntentSetAside:
    """Keep a bad intent for inspection without stopping the next take."""
    try:
        return read_trim_intent(path)
    except (ValueError, KeyError, TypeError) as error:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination = path.with_name(path.name + ".damaged-" + stamp)
        number = 2
        while destination.exists():
            destination = path.with_name(path.name + ".damaged-" + stamp + f"-{number}")
            number += 1
        _ = path.rename(destination)
        print(f"lint sweep: could not recover {path}: {error}; set aside as {destination}", file=sys.stderr)
        return DamagedTrimIntentSetAside(destination)


def build_log_rows(paths: Iterable[Path], start: int = 0) -> Iterator[dict[str, object]]:
    """Read rows of each build-log file through store.locked_rows for rebuild evidence."""
    for path in paths:
        yield from store.locked_rows(path, start)
        # A month that disappears after enumeration must fail the rebuild report.
        if not path.exists():
            raise FileNotFoundError(path)


def trim_row_recorded(take: TrimTake) -> bool:
    path = store.host_file(store.host_name(), take.at)
    extent = "whole" if take.remaining_groups == 0 else "partial"
    return any(row.get("kind") == "sweep_trim" and row.get("folder") == take.target
               and row.get("cause") == take.cause and row.get("at") == store.utc_iso(take.at)
               and row.get("bytes_freed") == take.bytes_freed and row.get("extent") == extent
               for row in store.locked_rows(path, take.log_offset))


@contextmanager
def recoverable_trim(target: str, cause: Literal["budget take", "floor take"], scan: Scan,
                     dry_run: bool, owners: TargetOwnerIndex) -> Generator[None]:
    """One write-ahead deletion and publication path for budget and floor takes."""
    if dry_run:
        yield
        return
    target = os.path.abspath(target)
    path = trim_intent_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        groups = [group for group in scan.groups if owners.owner(group.build_tree) == TargetOwner(target)]
        whole_targets = [whole for whole in scan.whole_targets
                         if cause == "floor take" and whole.target == target]
        take = TrimTake(target, cause, len(groups) + len(whole_targets),
                        output_roots=OUTPUT_ROOTS.get().roots_for(target))
        recovered = True
        if path.exists():
            unfinished = load_trim_for_recovery(path)
            if not isinstance(unfinished, DamagedTrimIntentSetAside):
                _ = unfinished.recover()
                recovered = unfinished.publish()
                if not recovered:
                    take = unfinished
        active = dict(TRIM_TAKES.get())
        active.update({group.build_tree: take for group in groups})
        active.update({whole.target: take for whole in whole_targets})
        token = TRIM_TAKES.set(active)
        try:
            yield
            # A failed append keeps its completed intent for the next invocation.
            if recovered and path.exists() and not take.publish():
                return
        finally:
            TRIM_TAKES.reset(token)


def stamp_newer(root: str, moment: float) -> bool:
    """Return whether the use stamp is newer than moment. A missing stamp returns False."""
    try:
        return os.stat(os.path.join(root, USE_STAMP)).st_mtime > moment
    except FileNotFoundError:
        return False


def recover_pending_trims(locked_targets: Sequence[str] = (), *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> None:
    """Publish unfinished takes even when the rerun has no output or shortfall left."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    directory = floor_record_path().parent / "trims"
    tree_depth = lint_settings.whole("sweep_max_tree_depth")
    for path in sorted(directory.glob("*.json")):
        with path.with_suffix(".lock").open("a") as guard:
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            try:
                take = load_trim_for_recovery(path)
                if isinstance(take, DamagedTrimIntentSetAside):
                    continue
                with ExitStack() as guards:
                    roots = take.output_roots or (take.target,)
                    if len(roots) > 1:
                        owners = TargetOwnerIndex(roots)
                        if build_hold_targets(roots, {}, lint_settings=lint_settings) or process_uses(owners):
                            continue
                        acquired = True
                        for root in sorted(roots):
                            if root not in locked_targets and not guards.enter_context(scratch.removal_lock(Path(root))):
                                acquired = False
                                break
                        if not acquired:
                            continue
                        if any(stamp_newer(root, take.at) for root in roots):
                            continue
                    trees = [tree for root in roots for tree in _build_trees(root, tree_depth, lint_settings=lint_settings)]
                    trees = output_lock_trees(roots, trees, [entry.path for entry in take.pending])
                    locks = (TargetLocksAcquired([]) if all(root in locked_targets for root in roots)
                             else lock_trees(sorted(set(trees))))
                    if isinstance(locks, TargetLockBusy):
                        continue
                    try:
                        _ = take.recover()
                        if not take.publish():
                            continue
                    finally:
                        release(locks.held)
            except (OSError, ValueError, KeyError) as error:
                print(f"lint sweep: could not recover {path}: {error}", file=sys.stderr)


def freed_by(group: FloorTake, scan: Scan, remaining: dict[InodeKey, int]) -> ReclamationEstimate:
    """Estimate blocks once a group's links go, counting them off remaining.

    A file's blocks come back only when its last link goes, so a file with a
    link outside the group frees nothing until that link goes too.
    """
    freed = 0
    for key in group.inodes:
        remaining[key] -= 1
        if remaining[key] == 0:
            freed += scan.blocks[key]
    return ReclamationEstimate(freed)


def unit_age(group: Group, now: float) -> tuple[float, float]:
    """Sorts the build unit unused for the most whole days first, then the oldest compile."""
    return (-int((now - group.last_used) // DAY_SECONDS), group.compiled)


def remove_entry(path: str) -> ConfirmedDeletion:
    """Anchor deletion to the parent directory; never traverse a replacement symlink."""
    parent, name = os.path.split(os.path.normpath(path))
    try:
        parent_fd = os.open(parent or ".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            return remove_entry_at(name, parent_fd, path)
        finally:
            os.close(parent_fd)
    except FileNotFoundError:
        return ConfirmedDeletion(0, 0)
    except OSError as error:
        print(f"lint sweep: could not remove {path}: {error}", file=sys.stderr)
        return ConfirmedDeletion(0, 1)


def remove_entry_at(name: str, parent_fd: int, display_path: str) -> ConfirmedDeletion:
    """Check opened directory identity and account for each unlink relative to its parent."""
    freed = failures = removed = 0
    device_bytes: dict[int, int] = {}
    try:
        before = os.lstat(name, dir_fd=parent_fd)
        if stat.S_ISDIR(before.st_mode):
            directory_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                if not os.path.samestat(before, os.fstat(directory_fd)):
                    raise OSError("directory changed before open")
                with os.scandir(directory_fd) as entries:
                    children = [entry.name for entry in entries]
                for child in children:
                    account = remove_entry_at(child, directory_fd, os.path.join(display_path, child))
                    freed += account.bytes
                    failures += account.failures
                    removed += account.removed
                    for device, size in account.device_bytes.items():
                        device_bytes[device] = device_bytes.get(device, 0) + size
                if not os.path.samestat(before, os.lstat(name, dir_fd=parent_fd)):
                    raise OSError("directory changed before removal")
                os.rmdir(name, dir_fd=parent_fd)
                account = removed_entry_account(PendingUnlink(
                    display_path, before.st_dev, before.st_ino, before.st_blocks * 512, before.st_nlink, True), True)
                removed += account.removed
            finally:
                os.close(directory_fd)
        else:
            os.unlink(name, dir_fd=parent_fd)
            account = removed_entry_account(PendingUnlink(
                display_path, before.st_dev, before.st_ino, before.st_blocks * 512, before.st_nlink, False), True)
            removed += account.removed
            freed = account.bytes
            device_bytes.update(account.device_bytes)
    except FileNotFoundError:
        pass
    except OSError as error:
        failures += 1
        print(f"lint sweep: could not remove {display_path}: {error}", file=sys.stderr)
    return ConfirmedDeletion(freed, failures, removed, device_bytes)


def remove(groups: Sequence[FloorTake], *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> ConfirmedDeletion:
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    failures = 0
    freed = removed = 0
    device_bytes: dict[int, int] = {}
    for group in groups:
        identity = group.target if isinstance(group, WholeTargetTake) else group.build_tree
        take = TRIM_TAKES.get().get(identity)
        known_targets = ([group.target] if isinstance(group, WholeTargetTake) else
                         list(take.output_roots) if take is not None and take.output_roots else
                         [take.target] if take is not None else
                         list(OUTPUT_ROOTS.get().roots_for(physical_root(group.build_tree)))
                         if physical_root(group.build_tree) in OUTPUT_ROOTS.get().output_roots() else [])
        if not record_before_delete([identity], known_targets=known_targets, lint_settings=lint_settings):
            failures += 1
            continue
        markers = {os.path.join(group.target, name) for name in (RUSTC_INFO, CACHE_TAG)} if isinstance(
            group, WholeTargetTake) else set[str]()
        paths = [path for path in group.entries if path not in markers]
        paths.extend(path for path in group.entries if path in markers)
        group_failures = 0
        for path in paths:
            if path in markers and group_failures:
                break
            account = remove_entry(path)
            freed += account.bytes
            failures += account.failures
            group_failures += account.failures
            removed += account.removed
            for device, size in account.device_bytes.items():
                device_bytes[device] = device_bytes.get(device, 0) + size
    return ConfirmedDeletion(freed, failures, removed, device_bytes)


def reclaim(group: FloorTake, scan: Scan, remaining: dict[InodeKey, int],
            dry_run: bool, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> ReclamationEstimate | ConfirmedDeletion:
    """Keep scan estimates separate from the live unlink account."""
    if dry_run:
        projected = replace(group, inodes=selected_inodes(group))
        return freed_by(projected, scan, remaining)
    identity = group.target if isinstance(group, WholeTargetTake) else group.build_tree
    take = TRIM_TAKES.get().get(identity) if isinstance(group, WholeTargetTake) or group.kind != "orphan" else None
    if take is not None:
        if take.stage == "publishing" or len(take.output_roots) > 1 and take.pending:
            return ConfirmedDeletion(0, 1)
        take.prepare(group)
    account = remove([group], lint_settings=lint_settings)
    if take is not None:
        take.confirm(account)
    return account


def directory_blocks(directory: str, seen: set[InodeKey] | None = None,
                     excluded: Collection[str] = ()) -> int:
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
                if entry.path not in excluded:
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


def prune_doc_index(roots: list[str], budget: int, dry_run: bool, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> int:
    """Remove a doc tree whose cross-crate index has grown past the budget."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
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
                if not record_before_delete([root], known_targets=[root], lint_settings=lint_settings):
                    failures += 1
                    continue
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


def scan_roots(roots: list[str], trees: list[str], *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Scan:
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    discovered = roots.discovered if isinstance(roots, DiscoveredTargetRoots) else tuple(target_dirs(roots, lint_settings=lint_settings))
    scan = Scan(discovered_targets=discovered)
    owners = group_roots(trees, scan, lint_settings=lint_settings)
    for root in roots:
        if scan.groups:
            walk(root, None, owners, scan, lint_settings=lint_settings)
        else:
            whole = WholeTargetTake(root)
            whole.retained.add(os.path.join(root, "doc", ".lock"))
            walk(root, whole, owners, scan, lint_settings=lint_settings)
            scan.whole_targets.append(whole)
    return scan


def scan_floor_roots(roots: list[str], trees: dict[str, list[str]], *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> Scan:
    """Scan independent targets with bounded IO concurrency and no cargo locks."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    discovered = tuple(roots.discovered) if isinstance(roots, DiscoveredTargetRoots) else tuple(roots)
    output_index = OUTPUT_ROOTS.get()
    def scan_target(cluster: tuple[str, ...]) -> Scan:
        _ = OUTPUT_ROOTS.set(output_index)
        scan = scan_roots(DiscoveredTargetRoots(cluster, discovered),
                          [tree for root in cluster for tree in trees[root]], lint_settings=lint_settings)
        for whole in scan.whole_targets:
            orphan_paths = [path for orphan in scan.orphans for path in orphan.entries
                            if Path(path).is_relative_to(whole.target)]
            protected = {str(parent) for path in (*whole.retained, *orphan_paths)
                         for parent in Path(path).parents if parent.is_relative_to(whole.target)}
            removable = set(whole.observed) - protected
            whole.entries = [path for path in whole.observed if path in removable
                             and str(Path(path).parent) not in removable]
            whole.observed = {path: version for path, version in whole.observed.items() if path not in protected}
        for root in cluster:
            scan.target_bytes[root] = sum(scan.blocks[key] for key in scan.target_links.get(root, {}))
        return scan

    paired_roots = output_index.roots_for
    clusters: list[tuple[str, ...]] = []
    assigned: set[str] = set()
    for root in roots:
        if root not in assigned:
            cluster = tuple(path for path in paired_roots(root) if path in roots)
            clusters.append(cluster)
            assigned.update(cluster)
    if not clusters:
        return Scan(discovered_targets=discovered)
    if len(clusters) == 1:
        return scan_target(clusters[0])
    combined = Scan(discovered_targets=discovered)
    with ThreadPoolExecutor(max_workers=lint_settings.whole("sweep_scan_workers")) as workers:
        for scan in workers.map(scan_target, clusters):
            combined.blocks.update(scan.blocks)
            combined.links.update(scan.links)
            combined.groups.extend(scan.groups)
            combined.orphans.extend(scan.orphans)
            combined.whole_targets.extend(scan.whole_targets)
            combined.target_bytes.update(scan.target_bytes)
            combined.target_links.update(scan.target_links)
    return combined


def sweep(roots: list[str], trees: list[str], budget: int, source: str, dry_run: bool,
          recent_minutes: float, *, lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> int:
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    if not dry_run:
        recover_pending_trims(roots, lint_settings=lint_settings)
    scan = scan_roots(roots, trees, lint_settings=lint_settings)
    total = sum(scan.blocks.values())
    label = ", ".join(roots)
    state = "within" if total <= budget else "over"
    print(f"lint sweep: {label} is {gib(total)}, {state} the {gib(budget)} budget ({source})")
    now = time.time()
    owners = TargetOwnerIndex(roots)
    with ExitStack() as takes:
        for root in roots:
            takes.enter_context(recoverable_trim(root, "budget take", scan, dry_run, owners))
        left, failures = shrink(
            scan, total, budget, dry_run,
            lambda group: (float(group.last_used > now - recent_minutes * 60), *unit_age(group, now)),
         lint_settings=lint_settings)[:2]
    if left > budget:
        print(
            f"lint sweep: {gib(left)} remains over budget in output this sweep never removes"
            + " (test-run folders, files it cannot match to a build unit, doc/ under its own budget)"
        )
    return 1 if failures else 0


def shrink(
    scan: Scan, total: int, budget: int, dry_run: bool, removal_order: RemovalOrder
, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> tuple[int, int, list[tuple[Group, int]]]:
    """Remove the orphans, then groups in removal_order until total fits budget,
    and say what went; return the size left, failures, and groups taken."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    verb, result = ("would remove", "would leave") if dry_run else ("removed", "left")
    remaining = dict(scan.links)
    failures = 0
    taken: list[tuple[Group, int]] = []
    if scan.orphans:
        orphaned = 0
        for group in scan.orphans:
            account = reclaim(group, scan, remaining, dry_run, lint_settings=lint_settings)
            orphaned += account.bytes
            failed = account.failures if isinstance(account, ConfirmedDeletion) else 0
            failures += failed
            if not failed or (isinstance(account, ConfirmedDeletion) and account.removed):
                taken.append((group, account.bytes))
        total -= orphaned
        print(
            f"lint sweep: {verb} {sum(len(group.entries) for group in scan.orphans)} orphaned files ({gib(orphaned)}),"
            + f" copied-up output whose build unit is gone; {result} {gib(total)}"
        )
    if total <= budget:
        return total, failures, taken
    chosen: list[Group] = []
    left = total
    for group in sorted(scan.groups, key=removal_order):
        if left <= budget:
            break
        chosen.append(group)
        account = reclaim(group, scan, remaining, dry_run, lint_settings=lint_settings)
        left -= account.bytes
        failed = account.failures if isinstance(account, ConfirmedDeletion) else 0
        failures += failed
        if not failed or (isinstance(account, ConfirmedDeletion) and account.removed):
            taken.append((group, account.bytes))
    if chosen:
        units = sum(1 for group in chosen if group.kind == "unit")
        uses = [group.last_used for group in chosen]
        print(
            f"lint sweep: {verb} {units} build units and {len(chosen) - units} incremental dirs"
            + f" ({gib(total - left)}), last used {when(min(uses))}"
            + f" to {when(max(uses))}; {result} {gib(left)}"
        )
    return left, failures, taken


@dataclass
class TargetRootSearch:
    """One directory branch, with its targets and Cargo build-tree exclusions."""

    frontier: list[tuple[str, int]]
    owned_trees: set[str] = field(default_factory=set)
    targets: set[str] = field(default_factory=set)
    checkouts: list[str] = field(default_factory=list)
    known_trees: dict[str, list[str]] = field(default_factory=dict)


DISCOVERY_BRANCH_LEVELS = 3


def target_dirs(roots: Sequence[str], *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
    checkouts: list[str] | UseConfiguredValue = UseConfiguredValue.CURRENT,
    known_trees: dict[str, list[str]] | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> list[str]:
    """Every cargo target directory under roots."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    found: set[str] = set()
    owned_trees: set[str] = set()
    search_depth = lint_settings.whole("sweep_floor_search_depth")
    tree_depth = lint_settings.whole("sweep_max_tree_depth")
    skipped = frozenset(lint_settings.words("sweep_floor_skip"))
    marker_names = frozenset(("Cargo.toml", RUSTC_INFO, CACHE_TAG))

    def visit(search: TargetRootSearch, directory: str, depth: int) -> None:
        if directory in search.owned_trees:
            return
        children: list[os.DirEntry[str]] = []
        markers: set[str] = set()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    name = entry.name
                    if entry.is_dir(follow_symlinks=False):
                        if name == GIT_DIR:
                            markers.add(GIT_DIR)
                        if name not in skipped:
                            children.append(entry)
                    elif name in marker_names and entry.is_file():
                        markers.add(name)
        except OSError:
            # Marker reads can still succeed when the directory cannot be listed.
            markers.update(name for name in marker_names if os.path.isfile(os.path.join(directory, name)))
            git_directory = os.path.join(directory, GIT_DIR)
            if os.path.isdir(git_directory) and not os.path.islink(git_directory):
                markers.add(GIT_DIR)
        if "Cargo.toml" in markers:
            search.checkouts.append(directory)
        if RUSTC_INFO in markers or (CACHE_TAG in markers and has_cargo_tag(directory)):
            trees = _build_trees(directory, tree_depth, lint_settings=lint_settings)
            if RUSTC_INFO in markers or trees:
                target = os.path.realpath(directory)
                search.targets.add(target)
                search.known_trees[target] = trees
                # Exclude Cargo build trees when searching for nested targets.
                depth = 0
                search.owned_trees.update(trees)
                if directory in search.owned_trees:
                    return
        if depth < search_depth:
            # Style keeps its cache under .git. Do not search Git objects.
            retained = os.path.join(directory, GIT_DIR, scratch.STYLE_TARGET_NAME)
            if (GIT_DIR in markers
                    and not os.path.islink(retained) and is_cargo_target(retained, lint_settings=lint_settings)):
                search.targets.add(os.path.realpath(retained))
            search.frontier.extend((entry.path, depth + 1) for entry in children if entry.path not in search.owned_trees)

    def search_branch(search: TargetRootSearch) -> TargetRootSearch:
        while search.frontier:
            directory, depth = search.frontier.pop()
            visit(search, directory, depth)
        return search

    def collect(search: TargetRootSearch) -> None:
        found.update(search.targets)
        owned_trees.update(search.owned_trees)
        if isinstance(known_trees, dict):
            known_trees.update(search.known_trees)
        if isinstance(checkouts, list):
            checkouts.extend(search.checkouts)

    # Branches share no directory traversal state. Preserve the depth from each configured root.
    with ThreadPoolExecutor(max_workers=lint_settings.whole("sweep_discovery_workers")) as workers:
        for root in roots:
            search = TargetRootSearch([(os.path.expanduser(root), 0)], set(owned_trees))
            for _ in range(DISCOVERY_BRANCH_LEVELS):
                frontier, search.frontier = search.frontier, []
                for directory, depth in frontier:
                    visit(search, directory, depth)
            collect(search)
            branches = (TargetRootSearch([branch], set(search.owned_trees)) for branch in search.frontier)
            for result in workers.map(search_branch, branches):
                collect(result)
    return sorted(found)


def physical_root(path: str) -> str:
    matches = [root for root in OUTPUT_ROOTS.get().output_roots() if Path(path).is_relative_to(root)]
    return max(matches, key=len) if matches else path


def selected_inodes(group: FloorTake) -> list[InodeKey]:
    filesystem = FLOOR_FILESYSTEM.get()
    return [key for key in group.inodes if not isinstance(filesystem, FilesystemFloor) or key[0] == filesystem.device]


def group_unchanged(group: FloorTake) -> bool:
    """Validate recorded paths, without another directory scan under the lock."""
    for path, observed in group.observed.items():
        try:
            stat = os.lstat(path)
        except OSError:
            return False
        if output_version(stat) != observed:
            return False
    return True


def shrink_floor(
    scan: Scan, total: int, budget: int, dry_run: bool,
    tree_targets: dict[str, str], trees: dict[str, list[str]],
    busy: dict[str, TargetUse | LockHeld | BuildHoldTarget | NoRecordedEvidence], removal_order: RemovalOrder,
    planned_at: float, owners: TargetOwnerIndex,
*,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
    policy: MacFloor | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> tuple[int, int, list[tuple[FloorTake, int]]]:
    """Plan unlocked; lock just one target for its deletions, then release it."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    orphan_batches: dict[str, list[FloorTake]] = {}
    target_batches: dict[str, list[FloorTake]] = {}
    for groups, target_groups in ((scan.orphans, orphan_batches), (scan.groups, target_batches)):
        for group in sorted(groups, key=removal_order):
            if not selected_inodes(group):
                continue
            root = tree_targets[group.build_tree]
            target_groups.setdefault(root, []).append(group)
    for whole in scan.whole_targets:
        if selected_inodes(whole):
            target_batches.setdefault(whole.target, []).append(whole)
    batches = list(orphan_batches.items()) + sorted(
        target_batches.items(),
        key=lambda batch: (max(target_last_use(root, [
            group for group in batch[1] if isinstance(group, Group)
        ], scan.discovered_targets) for root in OUTPUT_ROOTS.get().roots_for(batch[0])), batch[0]),
    )
    remaining = dict(scan.links)
    taken: list[tuple[FloorTake, int]] = []
    attempted: list[FloorTake] = []
    failures = 0
    orphaned_bytes = 0
    left = total
    for root, candidates in batches:
        if left <= budget:
            break
        paired_roots = OUTPUT_ROOTS.get().roots_for(root)
        if any(paired in busy for paired in paired_roots):
            continue
        current_holds = (build_hold_targets(paired_roots, trees, lint_settings=lint_settings)
                         if not isinstance(policy, MacFloor) else {})
        current_uses: dict[str, TargetUse | LockHeld] = {}
        if isinstance(policy, MacFloor):
            current_uses.update(policy.uses(paired_roots))
        else:
            for paired in paired_roots:
                current_uses.update(process_use(paired, owners))
        current_holds = {path: use for path, use in current_holds.items() if path in paired_roots}
        current_uses = {path: use for path, use in current_uses.items() if path in paired_roots}
        if current_holds or current_uses:
            busy.update(current_holds)
            busy.update(current_uses)
            continue
        with ExitStack() as guards:
            lock = scratch.judgment_lock if dry_run else scratch.removal_lock
            blocked_pair = False
            for paired in paired_roots:
                if not guards.enter_context(lock(Path(paired))):
                    busy[paired] = LockHeld(os.path.join(paired, ".claude-busy"))
                    blocked_pair = True
                    break
            if blocked_pair:
                continue
            pair_trees = output_lock_trees(paired_roots,
                [tree for paired in paired_roots for tree in trees.get(paired, [])],
                [path for candidate in candidates for path in candidate.entries])
            locks = lock_trees(pair_trees)
            if isinstance(locks, TargetLockBusy):
                busy[root] = LockHeld(locks.path)
                continue
            try:
                changed = False
                for paired in paired_roots:
                    root_groups = [group for group in scan.groups if tree_targets[group.build_tree] == paired]
                    if target_last_use(paired, root_groups, scan.discovered_targets) > planned_at:
                        changed = True
                        break
                if changed:
                    continue
                doc_lock = os.path.join(root, "doc", ".lock")
                whole_batch = any(isinstance(candidate, WholeTargetTake) for candidate in candidates)
                if whole_batch and os.path.isdir(os.path.dirname(doc_lock)) and (
                        not dry_run or os.path.exists(doc_lock)):
                    held = try_lock(doc_lock)
                    if held is None:
                        busy[root] = LockHeld(doc_lock)
                        continue
                    locks.held.append(held)
                if whole_batch:
                    checked = set(target_lock_paths(pair_trees)) | {doc_lock}
                    extra = {path for candidate in candidates if isinstance(candidate, WholeTargetTake)
                             for path in candidate.retained if os.path.basename(path) in (*LOCK_NAMES, scratch.STYLE_LOCK_NAME)
                             and path not in checked}
                    blocked = False
                    for path in sorted(extra):
                        held = try_lock(path)
                        if held is None:
                            busy[root] = LockHeld(path)
                            blocked = True
                            break
                        locks.held.append(held)
                    if blocked:
                        continue
                with recoverable_trim(root, "floor take", scan, dry_run, owners):
                    for group in candidates:
                        if left <= budget:
                            break
                        if not group_unchanged(group):
                            continue
                        if isinstance(policy, MacFloor) and not dry_run:
                            current_claims = policy.claims_before_removal(paired_roots)
                            if current_claims:
                                busy.update(current_claims)
                                break
                        attempted.append(group)
                        account = reclaim(group, scan, remaining, dry_run, lint_settings=lint_settings)
                        if isinstance(account, ConfirmedDeletion) and account.removed:
                            scan.changed_targets.add(root)
                            scan.changed_targets.update(OUTPUT_ROOTS.get().roots_for(root))
                        filesystem = FLOOR_FILESYSTEM.get()
                        credited = (account.device_bytes.get(filesystem.device, 0)
                                    if isinstance(account, ConfirmedDeletion) and isinstance(filesystem, FilesystemFloor)
                                    else account.bytes)
                        left -= credited
                        failed = account.failures if isinstance(account, ConfirmedDeletion) else 0
                        failures += failed
                        if not failed:
                            taken.append((group, credited))
                        if isinstance(group, Group) and group.kind == "orphan":
                            orphaned_bytes += account.bytes
            finally:
                release(locks.held)
    verb, result = ("would remove", "would leave") if dry_run else ("removed", "left")
    orphans = [group for group in attempted if isinstance(group, Group) and group.kind == "orphan"]
    if orphans:
        print(f"lint sweep: {verb} {sum(len(group.entries) for group in orphans)} orphaned files ({gib(orphaned_bytes)}),"
              + f" copied-up output whose build unit is gone; {result} {gib(left)}")
    groups = [group for group in attempted if isinstance(group, Group) and group.kind != "orphan"]
    if groups:
        units = sum(group.kind == "unit" for group in groups)
        uses = [group.last_used for group in groups]
        freed = total - left - orphaned_bytes
        print(f"lint sweep: {verb} {units} build units and {len(groups) - units} incremental dirs"
              + f" ({gib(freed)}), last used {when(min(uses))} to {when(max(uses))}; {result} {gib(left)}")
    return left, failures, taken


def target_last_use(root: str, groups: list[Group], discovered: Sequence[str] = ()) -> float:
    """Newest workspace step or build-unit use in a target."""
    try:
        stamp_used = os.stat(os.path.join(root, USE_STAMP)).st_mtime
    except OSError:
        stamp_used = 0.0
    if not groups and not stamp_used:
        newest = 0.0
        for entry in listing(root):
            if (entry.name == ".claude-busy" or entry.name in LOCK_NAMES
                    or any(target == entry.path or target.startswith(entry.path + os.sep)
                           for target in discovered if target != root)):
                continue
            try:
                newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
            except OSError:
                continue
        return newest
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
    from scripts.lint import tmp_sweep

    filesystem = FLOOR_FILESYSTEM.get()
    stem = filesystem.record_stem if isinstance(filesystem, FilesystemFloor) else "floor"
    return tmp_sweep.state_dir() / f"{stem}.json"


def _finite_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def target_deletes_path() -> Path:
    from scripts.lint import tmp_sweep

    return tmp_sweep.state_dir() / tmp_sweep.TARGET_DELETES_NAME


def read_target_deletes(path: Path, now: float) -> dict[str, float]:
    try:
        raw = cast(object, json.loads(path.read_text()))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {root: float(at) for root, at in cast(dict[str, object], raw).items()
            if _finite_number(at) and now - DAY_SECONDS <= at <= now and os.path.isdir(root)}


def record_target_deletes(paths: Sequence[str], *, known_targets: Sequence[str] = (),
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> None:
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    roots = {os.path.realpath(root) for root in known_targets}
    for path in paths:
        if os.path.realpath(path) in roots:
            continue
        target = enclosing_cargo_target(path, lint_settings=lint_settings)
        if isinstance(target, EnclosingCargoTarget):
            roots.add(target.folder)
    if not roots:
        return
    path = target_deletes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        now = time.time()
        deleted = read_target_deletes(path, now)
        deleted.update(dict.fromkeys(roots, now))
        write_text_atomic(path, json.dumps(deleted) + "\n")


def record_before_delete(paths: Sequence[str], *, known_targets: Sequence[str] = (),
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> bool:
    """Refuse a deletion whose warm-promise invalidation cannot be saved."""
    try:
        record_target_deletes(paths, known_targets=known_targets, lint_settings=lint_settings)
    except OSError as error:
        print(f"lint sweep: could not record deletion of {', '.join(paths)}: {error}", file=sys.stderr)
        return False
    return True


def floor_record_or_new(previous: NoFloorRecord | FloorRecord, free: int) -> FloorRecord:
    """Use the saved record, or start a record with this free-space reading."""
    return previous if isinstance(previous, FloorRecord) else FloorRecord(
        time.time(), free, 0, NoDeliveredAlert(), NoDeliveredAlert())


def _valid_pid(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


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
    roots = data.get("held_roots")
    held_roots: UnknownHeldRoots | KnownHeldRoots = UNKNOWN_HELD_ROOTS
    if isinstance(roots, list) and all(isinstance(root, str) for root in cast(list[object], roots)):
        held_roots = KnownHeldRoots(frozenset(cast(list[str], roots)))
    lock_alert: FloorLockAlertState = NoFloorLockAlert()
    saved_alert = data.get("lock_alert")
    if isinstance(saved_alert, dict):
        saved = cast(dict[str, object], saved_alert)
        pid, started = saved.get("pid"), saved.get("started_at")
        if _valid_pid(pid):
            if _finite_number(started):
                lock_alert = DeliveredFloorLockAlert(pid, float(started))
            observed = saved.get("first_observed_at")
            delivered = saved.get("alert_delivered")
            if _finite_number(observed) and observed >= 0 and isinstance(delivered, bool):
                lock_alert = (ReportedUnreadableFloorHold(pid, float(observed)) if delivered
                              else ObservedUnreadableFloorHold(pid, float(observed)))
    filesystem = data.get("filesystem", "")
    measured_roots = data.get("measured_roots", [])
    crossing: PhoneCrossing = AbovePhoneFloor()
    saved_crossing = data.get("phone_crossing")
    if saved_crossing in ("under_pending", "under_pushed"):
        # Old receipts prove session delivery only. The phone still needs its first send.
        crossing = OpenPhoneCrossing(f"legacy-{float(measured):.9f}",
                                    CrossingReceipt.ACCEPTED if saved_crossing == "under_pushed"
                                    else CrossingReceipt.PENDING)
    elif isinstance(saved_crossing, dict):
        saved_channels = cast(dict[str, object], saved_crossing)
        identity = saved_channels.get("identity")
        if isinstance(identity, str) and identity:
            since = saved_channels.get("rearm_since")
            rearm = AboveRearmLine(float(since)) if _finite_number(since) else BelowRearmLine()
            try:
                if "recovery" in saved_channels:
                    crossing = RecoveringPhoneCrossing(identity, CrossingReceipt(saved_channels.get("recovery")),
                                                       saved_channels.get("retired") is True, rearm,
                                                       ReturnPushPolicy(saved_channels.get("return_policy", "allowed")))
                else:
                    crossing = OpenPhoneCrossing(identity, CrossingReceipt(saved_channels.get("natedev")),
                                                CrossingReceipt(saved_channels.get("phone")), rearm)
            except ValueError:
                crossing = OpenPhoneCrossing(identity)
    failure: NoFailure | FailurePending | FailureReported = NoFailure()
    saved_failure = data.get("failure")
    if saved_failure == "reported":
        failure = FailureReported()
    elif isinstance(saved_failure, dict):
        pending = cast(dict[str, object], saved_failure)
        status, detail = pending.get("status"), pending.get("detail")
        if isinstance(status, int) and not isinstance(status, bool) and isinstance(detail, str):
            failure = FailurePending(FloorFailureEvent(status, detail))
    return FloorRecord(float(measured), int(free), int(caches), history, push_history, held_roots, lock_alert,
                       filesystem if isinstance(filesystem, str) else "",
                       tuple(cast(list[str], measured_roots)) if isinstance(measured_roots, list)
                       and all(isinstance(root, str) for root in cast(list[object], measured_roots)) else (), crossing, failure)


@contextmanager
def locked_floor_record(path: Path) -> Generator[NoFloorRecord | FloorRecord]:
    """Serialize floor measurements and alert receipts on one stable lock inode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.stem + "-alert.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield read_floor_record(path)


def write_floor_record(path: Path, record: FloorRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    alert_at = record.alert_history.at if isinstance(record.alert_history, DeliveredAlert) else None
    push_at = record.push_history.at if isinstance(record.push_history, DeliveredAlert) else None
    data: dict[str, object] = {
        "measured_at": record.measured_at,
        "free_bytes": record.free_bytes,
        "build_cache_bytes": record.build_cache_bytes,
        "last_alert_at": alert_at,
        "last_push_at": push_at,
    }
    crossing = record.phone_crossing
    if isinstance(crossing, AbovePhoneFloor):
        data["phone_crossing"] = "above"
    else:
        saved_crossing: dict[str, object] = {"identity": crossing.identity}
        if isinstance(crossing.rearm, AboveRearmLine):
            saved_crossing["rearm_since"] = crossing.rearm.since
        if isinstance(crossing, OpenPhoneCrossing):
            saved_crossing.update(natedev=crossing.natedev.value, phone=crossing.phone.value)
        else:
            saved_crossing.update(recovery=crossing.receipt.value, retired=crossing.retired,
                                  return_policy=crossing.return_policy.value)
        data["phone_crossing"] = saved_crossing
    if isinstance(record.failure, FailurePending):
        data["failure"] = {"status": record.failure.event.status, "detail": record.failure.event.detail}
    elif isinstance(record.failure, FailureReported):
        data["failure"] = "reported"
    else:
        data["failure"] = "none"
    if record.filesystem:
        data["filesystem"] = record.filesystem
        data["measured_roots"] = list(record.measured_roots)
    if isinstance(record.lock_alert, DeliveredFloorLockAlert):
        data["lock_alert"] = asdict(record.lock_alert)
    elif isinstance(record.lock_alert, (ObservedUnreadableFloorHold, ReportedUnreadableFloorHold)):
        data["lock_alert"] = {"pid": record.lock_alert.pid,
                              "first_observed_at": record.lock_alert.first_observed_at,
                              "alert_delivered": isinstance(record.lock_alert, ReportedUnreadableFloorHold)}
    if isinstance(record.held_roots, KnownHeldRoots):
        data["held_roots"] = sorted(record.held_roots.roots)
    _ = partial.write_text(json.dumps(data) + "\n")
    _ = partial.replace(path)


def read_disk_measurement() -> DiskMeasurement:
    path = store.root() / store.DISK_NAME
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


def home_relative(path: str) -> str:
    """Replace the home folder prefix with a tilde."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def alert_path(path: str, limit: int) -> str:
    path = home_relative(path)
    if len(path) <= limit:
        return path
    leaf = path.rsplit("/", 1)[-1]
    prefix = path[:max(0, limit - len(leaf) - 2)]
    return f"{prefix}…/{leaf}"


def disk_measurement_lines(measurement: DiskMeasurement, free_fall: int, cache_growth: int,
                           now: float, explain_fall: bool, path_limit: int,
                           previous: NoFloorRecord | FloorRecord = NO_FLOOR_RECORD, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> list[str]:
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    if isinstance(measurement, UnavailableDiskMeasurement):
        return [f"buildlog-disk measurement unavailable ({measurement.reason})."]
    measurement_paths = lint_settings.whole("sweep_measurement_paths")
    age = now - measurement.measured_at.timestamp()
    measured_before_fall = (explain_fall and isinstance(previous, FloorRecord)
                            and measurement.measured_at.timestamp() <= previous.measured_at)
    stale = f"; stale by {int(age // 60)} minutes" if age > lint_settings.number("sweep_snapshot_max_age_seconds") else ""
    if isinstance(measurement.comparison, NoEarlierDiskMeasurement):
        lines = [f"buildlog-disk measured {pacific_time(measurement.measured_at)}{stale}."]
        lines.append("No earlier buildlog-disk measurement to compare; largest directories outside build caches:")
        ranked = sorted(measurement.directories, key=lambda item: item.bytes, reverse=True)[:measurement_paths]
    else:
        lines = [f"buildlog-disk measured {pacific_time(measurement.comparison.measured_at)} to {pacific_time(measurement.measured_at)}{stale}."]
        lines.append("Directories outside build caches that grew most:")
        ranked = sorted((item for item in measurement.directories if item.growth_bytes > 0), key=lambda item: item.growth_bytes, reverse=True)[:measurement_paths]
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


def fit_alert_text(render: Callable[[int], str], *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> str:
    """Try configured path widths and keep the final complete message."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    widths = tuple(map(int, lint_settings.words("sweep_message_path_widths")))
    limit = lint_settings.whole("sweep_message_limit")
    for index, width in enumerate(widths):
        message = render(width)
        if len(message) <= limit or index == len(widths) - 1:
            return message
    raise conf.Unusable("lint: sweep_message_path_widths needs at least one path width")


def floor_alert_text(now: float, removed: int, previous: NoFloorRecord | FloorRecord,
                     free_fall: int, cache_growth: int, reasons: list[str],
                     measurement: DiskMeasurement, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> str:
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    comparison = (f"Since the previous sweep at {pacific_time(previous.measured_at)}, free space "
                  + f"{movement(-free_fall, 'rose', 'fell')} while build caches {movement(cache_growth, 'grew', 'shrank')}."
                  if isinstance(previous, FloorRecord) else "No earlier floor sweep to compare.")
    def render(path_limit: int) -> str:
        return "\n".join([
            f"Disk floor sweep at {pacific_time(now)} removed {gib(removed)} of build caches.",
            comparison,
            f"Alert threshold: {'; '.join(reasons)}.",
            *disk_measurement_lines(measurement, free_fall, cache_growth, now,
                                    isinstance(previous, FloorRecord), path_limit, previous, lint_settings=lint_settings),
        ])
    return fit_alert_text(render, lint_settings=lint_settings)


def holder_text(holder: FloorHolder, path_limit: int) -> str:
    """Describe one busy target, its size, and its process."""
    who = f"pid {holder.pid}" if holder.pid is not None else "a process"
    return f"{alert_path(holder.path, path_limit)} ({gib(holder.bytes)}): {who} {holder.how}"


def floor_out_of_reach_text(now: float, removed: int, free: int, floor: int,
                            holders: Sequence[FloorHolder], told: frozenset[str], ci_bytes: int,
                            measurement: DiskMeasurement, failures: int, *,
    protected: Sequence[tuple[str, int]] = (),
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> str:
    """The natedev report when no idle build output is left: what holds the space, and who was told."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    def holder_line(holder: FloorHolder, path_limit: int) -> str:
        if holder.path in told:
            outcome = f"session {holder.session} was told"
        elif holder.session is not None:
            outcome = f"the message to session {holder.session} failed"
        else:
            outcome = "no session found"
        return f"- {holder_text(holder, path_limit)}; {outcome}"

    def render(path_limit: int) -> str:
        return "\n".join([
            f"Build cache cleanup removed {gib(removed)}, but natedev still has only {gib(free)} free. Builds need {gib(floor)} free. No idle build cache is left to remove.",
            *(["These target dirs are in use; the sweep removes their output when that use ends:"] if holders else []),
            *(holder_line(holder, path_limit) for holder in holders),
            f"Not swept: CI's targets ({gib(ci_bytes)}).",
            *([f"Could not remove {failures} {'path' if failures == 1 else 'paths'} this sweep; the next sweep tries again."] if failures else []),
            *protected_folder_lines(protected, path_limit, lint_settings=lint_settings),
            "No phone alert was attempted for this event.",
            *disk_measurement_lines(measurement, 0, 0, now, False, path_limit, lint_settings=lint_settings),
        ])
    return fit_alert_text(render, lint_settings=lint_settings)


def holder_message_text(holder: FloorHolder, free: int, floor: int, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> str:
    """The message to the session whose process holds a target the floor needs."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    path_limit = int(lint_settings.words("sweep_message_path_widths")[0])
    return "\n".join([
        f"natedev has {gib(free)} free, and builds need {gib(floor)} free. The disk floor sweep removed all idle build output.",
        f"{holder_text(holder, path_limit)}. This process is in your session, so the sweep cannot remove that output.",
        "When the process ends, the next sweep removes the output. If the process is stale, stop it.",
    ])


def leaf_directories(measurement: AvailableDiskMeasurement) -> list[OutsideCacheDirectory]:
    """Exclude parent folders when a measured child is available."""
    names = [item.path for item in measurement.directories]
    return [item for item in measurement.directories
            if not any(name.startswith(item.path.rstrip(os.sep) + os.sep) for name in names)]


def measure_protected_folders(roots: frozenset[str], excluded: frozenset[str]) -> tuple[tuple[str, int], ...]:
    """Measure protected folders once, excluding nested targets and shared inodes."""
    seen: set[InodeKey] = set()
    return tuple((path, directory_blocks(path, seen, excluded)) for path in sorted(roots)
                 if os.path.isdir(path) and on_floor_filesystem(path))


def protected_folder_lines(protected: Sequence[tuple[str, int]], path_limit: int, *,
                           lint_settings: conf.ConfigRead) -> list[str]:
    """Render the largest measured folders without disk access."""
    if not protected:
        return []
    largest = sorted(protected, key=lambda item: (-item[1], item[0]))[:lint_settings.whole("sweep_measurement_paths")]
    return ["These folders have no recorded evidence. The sweep protects them:",
            *(f"{alert_path(path, path_limit)}: holds {gib(size)}." for path, size in largest)]


def folder_choice(path: str, size: int) -> FolderChoice:
    """Describe measured folders without assuming their contents are build output."""
    folder = Path(path)
    name = " ".join(folder.name.split())
    label = home_relative(str(folder))
    if name.casefold() in ("trash", ".trash"):
        return FolderChoice(label, size, FolderChoiceKind.TRASH)
    if any(part.casefold() == "scratch" for part in folder.parts):
        return FolderChoice(label, size, FolderChoiceKind.SCRATCH)
    return FolderChoice(label, size, FolderChoiceKind.OTHER)


def folder_choices(measurement: DiskMeasurement | CurrentMacFolders, protected: Sequence[tuple[str, int]], shortfall: int, *,
                   now: float, lint_settings: conf.ConfigRead, held_targets: Sequence[str] = ()) -> FolderChoices:
    """Classify measured folders and exclude held output from recommendations."""
    choices = [FolderChoice(home_relative(path), size, FolderChoiceKind.PROTECTED)
               for path, size in protected if size > 0]
    if isinstance(measurement, CurrentMacFolders):
        protected_labels = {item.label for item in choices}
        choices.extend(item for item in measurement.folders if item.bytes > 0 and item.label not in protected_labels)
    elif isinstance(measurement, AvailableDiskMeasurement):
        age = now - measurement.measured_at.timestamp()
        if 0 <= age <= lint_settings.number("sweep_snapshot_max_age_seconds"):
            protected_paths = {item[0] for item in protected}
            choices.extend(folder_choice(item.path, item.bytes) for item in leaf_directories(measurement)
                           if item.bytes > 0 and item.path not in protected_paths)
    held_paths = tuple(Path(path).expanduser().resolve() for path in held_targets)
    for index, item in enumerate(choices):
        path = Path(item.label).expanduser().resolve()
        if any(path.is_relative_to(held) or held.is_relative_to(path) for held in held_paths):
            choices[index] = replace(item, kind=FolderChoiceKind.HELD_OUTPUT)
    choices.sort(key=lambda item: (-item.bytes, item.label))
    if not choices:
        return UNAVAILABLE_FOLDER_CHOICES
    eligible = [item for item in choices if item.kind is not FolderChoiceKind.HELD_OUTPUT]
    if not eligible:
        return HeldOnlyFolderChoices(tuple(choices))
    return (AvailableFolderChoices(tuple(choices)) if eligible[0].bytes >= shortfall
            else InsufficientFolderChoices(tuple(choices)))


@cache
def prepare_push_texts() -> None:
    """Add the notification module path once per process."""


def folder_recommendation(choices: FolderChoices, shortfall: int) -> str:
    """Describe the largest measured folder without held output for both alert channels."""
    if isinstance(choices, UnavailableFolderChoices):
        return "Folder sizes are unknown."
    if isinstance(choices, HeldOnlyFolderChoices):
        return ("Every measured folder holds output a build is using. "
                "The sweep frees it when the build ends.")
    largest = next(item for item in choices.choices if item.kind is not FolderChoiceKind.HELD_OUTPUT)
    if isinstance(choices, InsufficientFolderChoices):
        return (f"The largest folder is {largest.label} at {gib(largest.bytes)}. "
                f"Deleting it frees only part of the {gib(shortfall)} shortfall.")
    if largest.kind is FolderChoiceKind.TRASH:
        action = f"Delete {largest.label}"
        effect = "this frees"
    else:
        action = f"Review {largest.label} first"
        effect = "deleting it frees"
    return f"{action} (recommended): {effect} {gib(largest.bytes)}, which covers the {gib(shortfall)} shortfall."


def folder_choice_lines(choices: FolderChoices, shortfall: int, *, folder_count: int) -> list[str]:
    """List enough measured folders with full paths, within the folder count."""
    if isinstance(choices, UnavailableFolderChoices):
        lines = [folder_recommendation(choices, shortfall), "",
                 "Measure first (recommended): natedev lists folder sizes, then you choose what to delete.", "",
                 "Delete nothing: the disk stays low and builds can fail."]
    else:
        shown: list[FolderChoice] = []
        covered = 0
        for item in choices.choices:
            if len(shown) >= folder_count or covered >= shortfall:
                break
            if item.bytes <= 0:
                continue
            shown.append(item)
            if item.kind is not FolderChoiceKind.HELD_OUTPUT:
                covered += item.bytes
        if shown and not isinstance(choices, HeldOnlyFolderChoices):
            largest = next(item for item in choices.choices if item.kind is not FolderChoiceKind.HELD_OUTPUT)
            if largest not in shown:
                shown[-1] = largest
        effects = {
            FolderChoiceKind.TRASH: "Deleting it removes discarded files.",
            FolderChoiceKind.SCRATCH: "Deleting it removes temporary work; check for work you still need.",
            FolderChoiceKind.OTHER: "Deleting it removes its contents; their use is unknown.",
            FolderChoiceKind.PROTECTED: ("It is protected: no build record shows what made it. "
                                         "Cleanup never deletes it. Deleting it removes its files."),
            FolderChoiceKind.HELD_OUTPUT: "A build is using output inside it; deleting it can break that build.",
        }
        lines = [f"{item.label}: holds {gib(item.bytes)}. {effects[item.kind]}" for item in shown]
        lines.append(folder_recommendation(choices, shortfall))
        lines.append("Delete nothing: builds can fail when the disk is full.")
    lines.append("natedev checks your choice before it deletes anything.")
    return lines


def disk_floor_push(free: int, floor: int, choices: FolderChoices, *,
                    machine: FloorMachine = FloorMachine.NATEDEV) -> RenderedPush:
    """Render free space, the alert line, and one folder recommendation."""
    if TYPE_CHECKING:
        from scripts.notify.push_texts import render
    else:
        prepare_push_texts()
        from scripts.notify.push_texts import render
    folder_action = ("In the natedev Claude session, reply measure or delete nothing."
                     if isinstance(choices, UnavailableFolderChoices)
                     else "In the natedev Claude session, reply delete nothing."
                     if isinstance(choices, HeldOnlyFolderChoices)
                     else f"In the natedev Claude session, say which {machine.value} folders to delete.")
    recommendation = folder_recommendation(choices, max(0, floor - free))
    if isinstance(choices, UnavailableFolderChoices):
        recommendation += (" Measure (recommended): natedev lists folder sizes, then you choose. "
                           "Delete nothing: free space stays low.")
    elif isinstance(choices, InsufficientFolderChoices):
        recommendation = recommendation.removesuffix(".") + "; the natedev session lists every folder."
    return render("sweep.disk_floor", machine=machine.value, free=gib(free), floor=gib(floor), folder_action=folder_action,
                  recommendation=recommendation)


@dataclass(frozen=True)
class NoPhoneAlert:
    """This cleanup report goes only to the machine's session."""


NO_PHONE_ALERT = NoPhoneAlert()


def fit_disk_floor_push(free: int, floor: int, choices: FolderChoices, *,
                        lint_settings: conf.ConfigRead,
                        machine: FloorMachine = FloorMachine.NATEDEV) -> RenderedPush | NoPhoneAlert:
    """Use the unavailable text when the recommendation reaches the limit."""
    limit = lint_settings.whole("sweep_push_text_limit")
    for candidate in (choices, UNAVAILABLE_FOLDER_CHOICES):
        rendered = disk_floor_push(free, floor, candidate, machine=machine)
        if len(rendered.message) < limit:
            return rendered
    print("lint sweep: phone text exceeds sweep_push_text_limit", file=sys.stderr)
    return NO_PHONE_ALERT


def crossing_send_argv(repeat: CrossingSendKey | NoCrossingSend) -> list[str]:
    """Give each channel its delivery key and repeat window."""
    return (["--key", repeat.key, "--repeat-minutes", f"{repeat.repeat_minutes:g}"]
            if isinstance(repeat, CrossingSendKey) else [])


def run_send(command: list[str], label: str, *, input: str | None, timeout: float) -> FloorChannelOutcome:
    """Run either sender and report delivery, queuing, or failure."""
    try:
        result = subprocess.run(command, input=input, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"lint sweep: {label} failed: {error}", file=sys.stderr)
        return FloorChannelOutcome.FAILED
    outcome = {send_result.EXIT["sent"]: FloorChannelOutcome.DELIVERED,
               send_result.EXIT["queued"]: FloorChannelOutcome.QUEUED}.get(result.returncode, FloorChannelOutcome.FAILED)
    if outcome.accepted:
        print(f"lint sweep: {label} {outcome.value}")
    else:
        print(f"lint sweep: {label} failed ({result.returncode}): {failure_detail(result)}", file=sys.stderr)
    return outcome


def send_holder_message(session: str, message: str, *,
    repeat: CrossingSendKey | NoCrossingSend = NO_CROSSING_SEND,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> FloorChannelOutcome:
    """Send a session message and distinguish delivery, queuing, and failure."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    command = session_send_argv(session, lint_settings=lint_settings) + crossing_send_argv(repeat)
    return run_send(command, f"message to {session}", input=message,
                    timeout=send_budget.seconds(lint_settings.number("sweep_sender_timeout_seconds")))


def disk_floor_channels() -> FloorAlertChannels:
    """Keep phone delivery disabled until the user records a current approval."""
    if TYPE_CHECKING:
        from scripts.notify.push_texts import approval_date
    else:
        prepare_push_texts()
        from scripts.notify.push_texts import approval_date
    return (FloorAlertChannels.NATEDEV_AND_PHONE if approval_date("sweep.disk_floor")
            else FloorAlertChannels.NATEDEV)


def send_phone_push(phone: RenderedPush, *,
    repeat: CrossingSendKey | NoCrossingSend = NO_CROSSING_SEND,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> FloorChannelOutcome:
    """Send the phone channel independently of the natedev receipt."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    if TYPE_CHECKING:
        from scripts.notify.push_texts import user_send_argv
    else:
        prepare_push_texts()
        from scripts.notify.push_texts import user_send_argv
        from scripts.notify import review
    command = user_send_argv(phone)
    command[1] = str(session_send.SEND_SCRIPT)
    command.extend(crossing_send_argv(repeat))
    return run_send(command, "phone alert", input=None,
                    timeout=send_budget.user_seconds(review.user_send_seconds()))


def send_floor_alert(message: str, *,
    repeat: CrossingSendKey | NoCrossingSend = NO_CROSSING_SEND,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> FloorChannelOutcome:
    """Send the natedev message and return its channel outcome."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    filesystem = FLOOR_FILESYSTEM.get()
    if isinstance(filesystem, FilesystemFloor) and not filesystem.root_device:
        message = f"Filesystem {filesystem.key} at {filesystem.probe_path}:\n{message}"
    return send_holder_message("natedev", message, repeat=repeat, lint_settings=lint_settings)


class FallPushRetirement(Enum):
    NO_KEPT_PUSH = "no_kept_push"
    DROPPED_KEPT_PUSH = "dropped_kept_push"
    FAILED = "failed"


def retire_floor_fall(identity: str, *, machine: FloorMachine,
                      lint_settings: conf.ConfigRead) -> FallPushRetirement:
    """Retire the fall key on natedev, where silence keeps both machines' pushes."""
    from scripts.notify import silence

    command = session_send.send_argv("retire", "--to", "user", "--from", "disk_floor", "--key", f"phone:{identity}")
    if machine is FloorMachine.MAC:
        remote = ('"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/message/send.py" '
                  + shlex.join(command[2:]))
        command = [*silence.SSH, remote]
    try:
        result = subprocess.run(command, text=True, capture_output=True,
                                timeout=send_budget.seconds(lint_settings.number("sweep_sender_timeout_seconds")))
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"lint sweep: cannot retire the fall push: {error}", file=sys.stderr)
        return FallPushRetirement.FAILED
    dropped = re.search(r"(?m)^dropped ([0-9]+) kept push(?:es)?$", result.stdout)
    if result.returncode or dropped is None:
        print(f"lint sweep: cannot retire the fall push: {result.stderr or result.stdout}", file=sys.stderr)
    if dropped is None:
        return FallPushRetirement.FAILED
    return (FallPushRetirement.DROPPED_KEPT_PUSH if int(dropped.group(1))
            else FallPushRetirement.NO_KEPT_PUSH)


from scripts.message import sessions


@dataclass(frozen=True)
class BuildHoldTarget:
    """A target reserved for a named build hold holder, including between steps."""

    holder: str


def build_hold_targets(targets: Sequence[str], known_trees: dict[str, list[str]], *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> dict[str, BuildHoldTarget]:
    """Protect session workspaces and recently written build trees without reading build output."""
    from scripts.build_hold import build_hold
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    state = build_hold.read_holders(build_hold.holder_directory())
    if isinstance(state, build_hold.NoHolders):
        return {}
    records = sessions.live_sessions()
    own_deletes = read_target_deletes(target_deletes_path(), time.time())
    protected: dict[str, BuildHoldTarget] = {}
    lookback = lint_settings.number("sweep_holder_lookback_seconds")
    delete_grace = lint_settings.number("sweep_delete_stamp_grace_seconds")
    tree_depth = lint_settings.whole("sweep_max_tree_depth")
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
        # Allow the configured interval for clock and filesystem differences.
        changed_since = holder.since.timestamp() - lookback
        for root in targets:
            if root in protected:
                continue
            index = OUTPUT_ROOTS.get()
            mapped_hold = any(
                any(folder == mapping.workspace_root or folder.is_relative_to(mapping.workspace_root)
                    for folder in folders)
                for position in index.output_owners.get(Path(root), ())
                for mapping in (index.workspaces[position],))
            if mapped_hold or any(Path(root).resolve().is_relative_to(folder) for folder in folders):
                protected[root] = BuildHoldTarget(holder.name)
                continue
            if root not in known_trees:
                known_trees[root] = _build_trees(root, tree_depth, lint_settings=lint_settings)
            own_delete = own_deletes.get(str(Path(root).resolve()), float("-inf"))
            paths = [os.path.join(root, USE_STAMP)]
            for tree in known_trees[root]:
                paths.extend([tree, *(os.path.join(tree, name) for name in (*HASHED_DIRS, INCREMENTAL_DIR))])
            for path in paths:
                try:
                    changed = os.stat(path).st_mtime
                except OSError:
                    continue
                # A delete changes its parent folder's mtime; allow the configured timestamp tolerance.
                if changed >= changed_since and changed > own_delete + delete_grace:
                    protected[root] = BuildHoldTarget(holder.name)
                    break
    return protected


def floor_roots(*, lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> tuple[str, ...]:
    """Include the standing pool wherever copies configuration places it."""
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    return (*lint_settings.words("sweep_floor_roots"), str(copies.gates_root()))


def floor_scan_targets(scan: Scan, idle: Sequence[str], target_uses: dict[str, float],
                       tree_targets: dict[str, str]) -> list[record.IdleFloorTarget]:
    """Estimate each target independently of the shortfall and all other takes."""
    _ = tree_targets
    candidates: dict[str, list[FloorTake]] = {root: [] for root in idle}
    for root in idle:
        owned = dict(scan.target_links[root])
        for group in (*scan.orphans, *scan.groups, *scan.whole_targets):
            inodes: list[InodeKey] = []
            for key in group.inodes:
                if owned.get(key, 0):
                    inodes.append(key)
                    owned[key] -= 1
            if inodes:
                candidates[root].append(replace(group, inodes=inodes))
    rows: list[record.IdleFloorTarget] = []
    for root in sorted(idle, key=lambda root: (target_uses[root], root)):
        counts: dict[InodeKey, int] = {}
        for group in candidates[root]:
            for key in group.inodes:
                counts[key] = counts.get(key, 0) + 1
        target_links = scan.target_links[root]
        outside_target = any(target_links[key] < scan.links[key] for key in counts)
        outside_candidates = any(count < target_links[key] for key, count in counts.items())
        remaining = {key: scan.links[key] for key in counts}
        takeable = sum(freed_by(group, scan, remaining).bytes for group in candidates[root])
        row: record.IdleFloorTarget = {
            "folder": root, "last_use": target_uses[root], "bytes": scan.target_bytes[root],
        }
        if takeable != row["bytes"]:
            row["takeable_bytes"] = takeable
            row["reason"] = ("links outside the target" if outside_target else
                             "links outside the take candidates" if outside_candidates else
                             "output outside take candidates")
        rows.append(row)
    return rows


@dataclass(frozen=True)
class MacFloor:
    """Only marked output inside the three approved Mac roots can be removed."""

    roots: tuple[str, ...]
    claims: floor_claims.ClaimSnapshot
    mirror: Path
    busy_mirrors: frozenset[Path] = frozenset()
    leased_mirrors: frozenset[Path] = frozenset()

    def allows(self, target: str) -> bool:
        path = Path(target)
        real = path.resolve()
        if path.is_symlink() or not any(real.is_relative_to(Path(root).expanduser().resolve()) for root in self.roots):
            return False
        if (path / USE_STAMP).is_file():
            return True
        for parent in (path, *path.parents):
            if not any(parent.resolve().is_relative_to(Path(root).expanduser().resolve()) for root in self.roots):
                break
            if (parent / floor_claims.MIRROR_MARK).is_file():
                return True
            if isinstance(copy_record.read_record(parent / copy_record.RECORD), copy_record.Registered):
                return True
        return False

    def uses(self, targets: Sequence[str]) -> dict[str, TargetUse | LockHeld]:
        from scripts.lint import tmp_sweep

        result: dict[str, TargetUse | LockHeld] = {}
        paths = tmp_sweep.lsof_paths(tuple(str(Path(root).expanduser().resolve()) for root in self.roots))
        for root in targets:
            if (not self.allows(root) or self.claims.holds(root, self.mirror)
                    or any(Path(root).resolve().is_relative_to(mirror.resolve()) for mirror in self.busy_mirrors)
                    or isinstance(paths, tmp_sweep.ProcessUsesUnavailable)):
                result[root] = LockHeld("Mac claim or process use is busy or unreadable")
            else:
                for path, pid, how in paths.paths:
                    if Path(path).is_relative_to(root):
                        result[root] = TargetUse(pid, how)
                        break
        return result

    def claims_before_removal(self, targets: Sequence[str]) -> dict[str, LockHeld]:
        """Read claims again for output outside the acquired mirror leases."""
        uncovered = [target for target in targets if not any(
            Path(target).resolve().is_relative_to(mirror.resolve()) for mirror in self.leased_mirrors)]
        if not uncovered:
            return {}
        claims = floor_claims.read_claims(conf.read("mac_test").number("probe_timeout_s"))
        return {target: LockHeld("Mac claim is busy or unreadable") for target in uncovered
                if claims.holds(target, self.mirror)}


class NatedevFloor(Enum):
    """Use the existing Linux process and build hold checks."""

    CURRENT = "natedev"


def mac_folder_reclaimable_bytes(directory: str) -> int:
    """Count file blocks once only when every hard link is inside this folder."""
    total = 0
    links: dict[InodeKey, int] = {}
    shared: dict[InodeKey, os.stat_result] = {}
    frontier = [directory]
    while frontier:
        try:
            with os.scandir(frontier.pop()) as entries:
                listed = list(entries)
        except OSError:
            continue
        for entry in listed:
            try:
                observed = entry.stat(follow_symlinks=False)
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_directory:
                frontier.append(entry.path)
                continue
            if observed.st_nlink == 1:
                total += observed.st_blocks * 512
                continue
            key = (observed.st_dev, observed.st_ino)
            links[key] = links.get(key, 0) + 1
            shared[key] = observed
    return total + sum(observed.st_blocks * 512 for key, observed in shared.items() if links[key] == observed.st_nlink)


def mac_folder_measurement(policy: MacFloor) -> CurrentMacFolders:
    """Measure each root's direct folder children without following directory symlinks."""
    folders: set[str] = set()
    for root in policy.roots:
        try:
            with os.scandir(Path(root).expanduser().resolve()) as entries:
                folders.update(entry.path for entry in entries if entry.is_dir(follow_symlinks=False))
        except OSError as error:
            print(f"lint sweep: cannot measure folders under {root}: {error}", file=sys.stderr)
    return CurrentMacFolders(tuple(folder_choice(path, mac_folder_reclaimable_bytes(path)) for path in sorted(folders)))


class FloorFailureScope(Enum):
    FILESYSTEM = "filesystem"
    DISCOVERY = "discovery"

    @property
    def record_name(self) -> str:
        return floor_record_path().name if self is FloorFailureScope.FILESYSTEM else "floor-discovery.json"

    @property
    def label(self) -> str:
        return "disk floor sweep"


@dataclass(frozen=True)
class WorkspaceFailureScope:
    roots: tuple[str, ...]

    @property
    def record_name(self) -> str:
        identity = hashlib.sha256(json.dumps(self.roots).encode()).hexdigest()
        return f"workspace-{identity}.json"

    @property
    def label(self) -> str:
        return f"workspace sweep for {', '.join(self.roots)}"


@dataclass(frozen=True)
class TemporarySweepFailureScope:
    """The temporary sweep has an independent failure episode."""

    @property
    def record_name(self) -> str:
        return "temporary-sweep.json"

    @property
    def label(self) -> str:
        return "temporary sweep"


SweepFailureScope = FloorFailureScope | WorkspaceFailureScope | TemporarySweepFailureScope


@dataclass(frozen=True)
class FloorOutcome:
    assessment: tmp_sweep.SweepAssessment
    removed: int = 0
    before: int = 0
    after: int = 0
    busy: int = 0
    protected: tuple[tuple[str, int], ...] = ()
    held_targets: tuple[str, ...] = ()

    @property
    def status(self) -> int:
        return self.assessment.status


def mac_floor_settings(
    settings: conf.ConfigRead, *, mirror_root: MacMirrorRoot | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> tuple[int, tuple[str, ...]]:
    """Refuse a missing live floor or a floor below the admission minimum."""
    if isinstance(mirror_root, UseConfiguredValue):
        mirror_root = MacMirrorRoot.read(settings)
    if "sweep_floor_gib" not in settings.values():
        raise conf.Unusable("mac_test.conf: sweep_floor_gib is missing")
    floor = settings.number("sweep_floor_gib")
    if floor < settings.number("free_floor_gib"):
        raise conf.Unusable("mac_test.conf: sweep_floor_gib must be at least free_floor_gib")
    roots = settings.words("sweep_floor_roots")
    approved = (Path("/tmp").resolve(), Path.home() / "rust", mirror_root.local_path())
    if len(roots) != 3 or {Path(root).expanduser().resolve() for root in roots} != {root.resolve() for root in approved}:
        raise conf.Unusable(f"mac_test.conf: sweep_floor_roots must name /tmp, ~/rust and ~/{mirror_root.relative}")
    return int(floor * GIB), roots


def hold_floor(floor: int, dry_run: bool, roots: Sequence[str] = (), lock: str = "", *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
    policy: MacFloor | NatedevFloor = NatedevFloor.CURRENT,
) -> int:
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    if isinstance(policy, NatedevFloor):
        return _hold_floor(floor, dry_run, lint_settings=lint_settings, policy=policy, roots=roots, lock=lock).status
    output = io.StringIO()
    with redirect_stdout(output):
        result = _hold_floor(floor, dry_run, lint_settings=lint_settings, policy=policy, roots=roots, lock=lock)
    if dry_run:
        for line in output.getvalue().splitlines():
            if "the floor would take" in line:
                print(line)
    print(f"lint sweep: removed_bytes={result.removed} free_before_bytes={result.before} "
          + f"free_after_bytes={result.after} busy_targets_skipped={result.busy} dry_run={str(dry_run).lower()}")
    return result.status


def output_device(path: str) -> int:
    return os.stat(path).st_dev


def filesystem_floors(floor: int, targets: Sequence[str], *, lint_settings: conf.ConfigRead) -> list[FilesystemFloor]:
    origin = lint_settings.origin("sweep_free_floor_percent", float)
    if not isinstance(origin, conf.UsableLiveValue) or not math.isfinite(origin.value) or not 0 <= origin.value <= 100:
        raise conf.Unusable("lint.conf: sweep_free_floor_percent must be a live number from 0 to 100")
    home = os.path.expanduser("~")
    root_device = output_device(home)
    devices: dict[int, list[str]] = {root_device: []}
    probes = {root_device: home}
    for path in (*targets, *lint_settings.words("sweep_ci_targets")):
        path = os.path.realpath(os.path.expanduser(path))
        if not os.path.isdir(path):
            continue
        try:
            device = output_device(path)
        except FileNotFoundError:
            continue
        _ = devices.setdefault(device, [])
        _ = probes.setdefault(device, path)
        if path in targets:
            devices[device].append(path)
    floors: list[FilesystemFloor] = []
    for device, device_targets in devices.items():
        probe = probes[device]
        if device == root_device:
            threshold = floor
        else:
            usage = os.statvfs(probe)
            threshold = int(usage.f_blocks * usage.f_frsize * origin.value / 100)
        floors.append(FilesystemFloor(device, probe, threshold, tuple(device_targets), device == root_device,
                                      len(devices) == 1))
    return floors


def _hold_floor(floor: int, dry_run: bool, *,
    lint_settings: conf.ConfigRead, policy: MacFloor | NatedevFloor,
    roots: Sequence[str] = (), lock: str = "",
) -> FloorOutcome:
    from scripts.lint import tmp_sweep

    if isinstance(policy, MacFloor):
        return _hold_floor_pass(floor, dry_run, lint_settings=lint_settings, policy=policy, roots=roots, lock=lock)
    planned_at = time.time()
    token = OUTPUT_ROOTS.set(CargoOutputIndex())
    try:
        targets, mappings = discover_cargo_outputs(roots or floor_roots(lint_settings=lint_settings),
                                                   lint_settings=lint_settings)
        _ = OUTPUT_ROOTS.set(mappings)
        ci_roots = [Path(path).expanduser().resolve() for path in lint_settings.words("sweep_ci_targets")]
        excluded_roots = set(ci_roots)
        for mapping in mappings.workspaces:
            pair = (mapping.target_directory, mapping.build_directory)
            if any(root.is_relative_to(ci) for root in pair for ci in ci_roots):
                excluded_roots.update(pair)
                for root in pair:
                    excluded_roots.update(Path(paired) for paired in mappings.roots_for(str(root)))
        targets = [root for root in targets if not any(Path(root).is_relative_to(excluded)
                                                      for excluded in excluded_roots)]
        filesystems = filesystem_floors(floor, targets, lint_settings=lint_settings)
        if not dry_run:
            notify_floor_problem(tmp_sweep.CompletedSweepAssessment(0), lint_settings=lint_settings,
                                 scope=FloorFailureScope.DISCOVERY)
        results: list[FloorOutcome] = []
        for filesystem in filesystems:
            scope = FLOOR_FILESYSTEM.set(filesystem)
            try:
                outcome = _hold_floor_pass(filesystem.floor_bytes, dry_run, lint_settings=lint_settings,
                                          policy=policy, planned_at=planned_at, roots=roots, lock=lock)
                results.append(outcome)
            except OSError as error:
                print(f"lint sweep: filesystem {filesystem.probe_path}: {error}", file=sys.stderr)
                if not dry_run:
                    notify_floor_problem(tmp_sweep.CompletedSweepAssessment(1),
                                         lint_settings=lint_settings, detail=str(error))
                results.append(FloorOutcome(tmp_sweep.CompletedSweepAssessment(1)))
            finally:
                FLOOR_FILESYSTEM.reset(scope)
        if not dry_run and mappings.workspaces:
            save_cargo_output_roots(mappings)
        return FloorOutcome(tmp_sweep.CompletedSweepAssessment(max(result.status for result in results)),
                            sum(result.removed for result in results),
                            sum(result.before for result in results), sum(result.after for result in results),
                            sum(result.busy for result in results))
    except (conf.Unusable, OSError) as error:
        print(f"lint sweep: {error}", file=sys.stderr)
        if not dry_run:
            notify_floor_problem(tmp_sweep.CompletedSweepAssessment(2), lint_settings=lint_settings,
                                 detail=str(error), scope=FloorFailureScope.DISCOVERY)
        return FloorOutcome(tmp_sweep.CompletedSweepAssessment(2))
    finally:
        OUTPUT_ROOTS.reset(token)


def notify_floor_problem(outcome: FloorOutcome | tmp_sweep.SweepAssessment, *, lint_settings: conf.ConfigRead, detail: str = "",
                         scope: SweepFailureScope = FloorFailureScope.FILESYSTEM,
                         policy: MacFloor | NatedevFloor = NatedevFloor.CURRENT) -> None:
    """Finalize delivery and state before an acquired floor lock closes."""
    from scripts.lint import tmp_sweep

    machine = FloorMachine.MAC if isinstance(policy, MacFloor) else FloorMachine.NATEDEV
    assessment = outcome.assessment if isinstance(outcome, FloorOutcome) else outcome
    if isinstance(assessment, tmp_sweep.SkippedSweepAssessment):
        return
    after = outcome.after if isinstance(outcome, FloorOutcome) else 0
    protected = outcome.protected if isinstance(outcome, FloorOutcome) else ()
    held_targets = outcome.held_targets if isinstance(outcome, FloorOutcome) else ()
    removed = outcome.removed if isinstance(outcome, FloorOutcome) else 0
    filesystem = FLOOR_FILESYSTEM.get()
    root_scope = scope is FloorFailureScope.FILESYSTEM and (
        not isinstance(filesystem, FilesystemFloor) or filesystem.root_device)
    path = floor_record_path().with_name(scope.record_name)
    if not root_scope and not assessment.status and not path.exists():
        return
    phone_floor = int(lint_settings.number("sweep_phone_floor_gib") * GIB)
    phone_rearm = phone_floor + int(lint_settings.number("sweep_phone_rearm_gib") * GIB)
    rearm_seconds = lint_settings.number("sweep_phone_rearm_minutes") * 60

    def recover(current: FloorRecord, free: int, save: Callable[[FloorRecord], bool]) -> FloorRecord:
        crossing = current.phone_crossing
        if isinstance(crossing, AbovePhoneFloor) or (
                isinstance(crossing, RecoveringPhoneCrossing) and crossing.receipt is CrossingReceipt.ACCEPTED):
            return current
        if free <= phone_rearm:
            return replace(current, phone_crossing=replace(crossing, rearm=BelowRearmLine()))
        now = time.time()
        interval = crossing.rearm if isinstance(crossing.rearm, AboveRearmLine) else AboveRearmLine(now)
        crossing = replace(crossing, rearm=interval)
        current = replace(current, phone_crossing=crossing)
        if now - interval.since < rearm_seconds:
            return current
        if isinstance(crossing, OpenPhoneCrossing):
            policy = (ReturnPushPolicy.ALLOWED if crossing.phone is CrossingReceipt.ACCEPTED
                      else ReturnPushPolicy.SUPPRESSED)
            crossing = RecoveringPhoneCrossing(crossing.identity, rearm=interval, return_policy=policy)
            current = replace(current, phone_crossing=crossing)
        if not save(current):
            return current
        if not crossing.retired:
            return_policy = crossing.return_policy
            # Save uncertainty before retirement can remove a kept fall push.
            crossing = replace(crossing, return_policy=ReturnPushPolicy.SUPPRESSED)
            current = replace(current, phone_crossing=crossing)
            if not save(current):
                return current
            retired = retire_floor_fall(crossing.identity, machine=machine, lint_settings=lint_settings)
            if retired is FallPushRetirement.FAILED:
                return current
            if (retired is FallPushRetirement.DROPPED_KEPT_PUSH
                    or return_policy is ReturnPushPolicy.SUPPRESSED):
                return replace(current, phone_crossing=AbovePhoneFloor())
            crossing = replace(crossing, retired=True, return_policy=return_policy)
            current = replace(current, phone_crossing=crossing)
            if not save(current):
                return current
        if crossing.receipt is CrossingReceipt.PENDING and disk_floor_channels() is FloorAlertChannels.NATEDEV_AND_PHONE:
            if TYPE_CHECKING:
                from scripts.notify.push_texts import render
            else:
                prepare_push_texts()
                from scripts.notify.push_texts import render
            phone = render("sweep.disk_floor_recovered", machine=machine.value,
                           free=gib(free), rearm=gib(phone_rearm), floor=gib(phone_floor))
            delivery = send_phone_push(phone,
                repeat=CrossingSendKey(f"phone-recovered:{crossing.identity}",
                                       lint_settings.number("sweep_crossing_repeat_minutes")), lint_settings=lint_settings)
            if delivery.accepted:
                current = replace(current, phone_crossing=replace(crossing, receipt=CrossingReceipt.ACCEPTED))
            _ = save(current)
        return current

    def finalize(previous: NoFloorRecord | FloorRecord, save: Callable[[FloorRecord], bool]) -> FloorRecord:
        current = floor_record_or_new(previous, after)
        free = after
        if root_scope:
            try:
                # Measure inside the record lock. Older callers cannot reset a newer crossing.
                free = free_bytes(filesystem.probe_path if isinstance(filesystem, FilesystemFloor)
                                  else os.path.expanduser("~"))
                current = recover(current, free, save)
                crossing = current.phone_crossing
                if free < phone_floor and isinstance(crossing, (AbovePhoneFloor, RecoveringPhoneCrossing)):
                    current = replace(current, phone_crossing=OpenPhoneCrossing(uuid.uuid4().hex))
            except OSError as error:
                print(f"lint sweep: cannot measure the phone floor: {error}", file=sys.stderr)
        failure = current.failure
        if assessment.status:
            if isinstance(failure, NoFailure):
                failure = FailurePending(FloorFailureEvent(assessment.status, detail))
        else:
            failure = NoFailure()
        current = replace(current, failure=failure)
        crossing = current.phone_crossing if root_scope else AbovePhoneFloor()
        natedev_pending = (free < phone_floor and isinstance(crossing, OpenPhoneCrossing)
                           and crossing.natedev is CrossingReceipt.PENDING)
        phone_due = (free < phone_floor and isinstance(crossing, OpenPhoneCrossing)
                     and crossing.phone is CrossingReceipt.PENDING and not assessment.status)
        phone_pending = phone_due and disk_floor_channels() is FloorAlertChannels.NATEDEV_AND_PHONE
        crossing_pending = natedev_pending or phone_pending
        pending_failure = isinstance(failure, FailurePending)
        if not crossing_pending and not pending_failure:
            return current
        shortfall = max(0, phone_floor - free)
        choices: FolderChoices = UNAVAILABLE_FOLDER_CHOICES
        if crossing_pending:
            measurement = mac_folder_measurement(policy) if isinstance(policy, MacFloor) else read_disk_measurement()
            choices = folder_choices(measurement, protected, shortfall,
                                     now=time.time(), lint_settings=lint_settings, held_targets=held_targets)

        def render(folder_count: int) -> str:
            lines: list[str] = []
            if crossing_pending:
                lines.extend([
                    f"Free disk space fell below the {gib(phone_floor)} alert line. {gib(free)} remains."
                    + (" This is the Mac disk." if machine is FloorMachine.MAC else ""),
                    f"Cleanup removed {gib(removed)}." if removed else "The cleanup run freed nothing.",
                    *folder_choice_lines(choices, shortfall, folder_count=folder_count),
                ])
            if isinstance(failure, FailurePending):
                lines.append(f"The {scope.label} failed with status {failure.event.status}. {failure.event.detail}")
            return "\n".join(lines)
        folder_count = int(lint_settings.number("sweep_alert_folder_count"))
        message = render(folder_count)
        while folder_count > 0 and len(message) > lint_settings.whole("sweep_message_limit"):
            folder_count -= 1
            message = render(folder_count)
        if len(message) > lint_settings.whole("sweep_message_limit"):
            choices = UNAVAILABLE_FOLDER_CHOICES
            message = render(0)

        def save_delivery(delivery: FloorAlertDelivery) -> None:
            nonlocal current
            receipt = current.phone_crossing
            if isinstance(receipt, OpenPhoneCrossing):
                receipt = replace(receipt,
                                  natedev=(CrossingReceipt.ACCEPTED if natedev_pending and delivery.natedev.accepted
                                           else receipt.natedev),
                                  phone=CrossingReceipt.ACCEPTED if delivery.phone.accepted else receipt.phone)
                current = replace(current, phone_crossing=receipt)
            if delivery.natedev.accepted and isinstance(current.failure, FailurePending):
                current = replace(current, failure=FailureReported())
            _ = save(current)

        # Crossings and sweep failures go only to natedev.
        # A current text approval permits the crossing's separate phone delivery.
        identity_saved = save(current) if isinstance(crossing, OpenPhoneCrossing) else False
        if natedev_pending or pending_failure:
            repeat: CrossingSendKey | NoCrossingSend = NO_CROSSING_SEND
            if not pending_failure and isinstance(crossing, OpenPhoneCrossing):
                repeat = (CrossingSendKey(f"natedev:{crossing.identity}", lint_settings.number("sweep_crossing_repeat_minutes"))
                          if identity_saved else CrossingSendKey(UNSAVED_CROSSING_KEY,
                                                                 lint_settings.number("sweep_alert_interval_seconds") / 60))
            delivery = send_floor_alert(message, repeat=repeat, lint_settings=lint_settings)
            save_delivery(FloorAlertDelivery(delivery, FloorChannelOutcome.NOT_ATTEMPTED))
        if phone_pending and identity_saved and isinstance(crossing, OpenPhoneCrossing):
            phone = fit_disk_floor_push(free, phone_floor, choices, lint_settings=lint_settings, machine=machine)
            if not isinstance(phone, NoPhoneAlert):
                result = send_phone_push(phone,
                                        repeat=CrossingSendKey(f"phone:{crossing.identity}",
                                                               lint_settings.number("sweep_crossing_repeat_minutes")),
                                        lint_settings=lint_settings)
                save_delivery(FloorAlertDelivery(FloorChannelOutcome.NOT_ATTEMPTED, result))
        return current

    record_lock = ExitStack()
    last_saved: NoFloorRecord | FloorRecord = NoFloorRecord()
    def save_record(current: FloorRecord) -> bool:
        nonlocal last_saved
        if current == last_saved:
            return True
        try:
            write_floor_record(path, current)
            last_saved = current
            return True
        except OSError as error:
            print(f"lint sweep: cannot save the notification state: {error}", file=sys.stderr)
            return False

    def no_record_access(current: FloorRecord) -> bool:
        del current
        return False

    try:
        try:
            previous = record_lock.enter_context(locked_floor_record(path))
        except OSError as error:
            print(f"lint sweep: cannot access the notification record: {error}", file=sys.stderr)
            _ = finalize(NoFloorRecord(), no_record_access)
            return
        last_saved = previous
        current = finalize(previous, save_record)
        if (isinstance(previous, NoFloorRecord) and isinstance(current.failure, NoFailure)
                and isinstance(current.phone_crossing, AbovePhoneFloor)):
            return
        _ = save_record(current)
    finally:
        try:
            record_lock.close()
        except OSError as error:
            print(f"lint sweep: cannot close the notification record: {error}", file=sys.stderr)


def _hold_floor_pass(floor: int, dry_run: bool, *,
    lint_settings: conf.ConfigRead, policy: MacFloor | NatedevFloor,
    roots: Sequence[str] = (), lock: str = "",
    planned_at: float | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> FloorOutcome:
    """Below the floor, take the shortfall from the least used idle targets."""
    from scripts.lint import tmp_sweep
    from scripts.buildlog import record

    guard: int | None = None
    outcome = FloorOutcome(tmp_sweep.SkippedSweepAssessment())
    protected: tuple[tuple[str, int], ...] = ()
    held_targets: set[str] = set()
    busy: dict[str, TargetUse | LockHeld | BuildHoldTarget | NoRecordedEvidence] = {}
    detail = ""
    mirror_leases = ExitStack()
    try:
        if isinstance(planned_at, UseConfiguredValue):
            planned_at = time.time()
        roots = policy.roots if isinstance(policy, MacFloor) else roots or floor_roots(lint_settings=lint_settings)
        filesystem = FLOOR_FILESYSTEM.get()
        lock = lock or str(floor_record_path().with_suffix(".lock"))
        if not dry_run and isinstance(policy, NatedevFloor):
            recover_pending_trims(lint_settings=lint_settings)
        home = filesystem.probe_path if isinstance(filesystem, FilesystemFloor) else os.path.expanduser("~")
        free = free_bytes(home)
        if free >= floor:
            if dry_run:
                print(f"lint sweep: {home}: {gib(free)} free, at or above the {gib(floor)} floor; nothing to remove")
            outcome = FloorOutcome(tmp_sweep.CompletedSweepAssessment(0), before=free, after=free)
            return outcome
        if not dry_run:
            lock_path = str(lock)
            os.makedirs(os.path.dirname(lock_path), exist_ok=True)
            guard = try_lock(lock_path)
            if guard is None:
                print(f"lint sweep: {gib(free)} free, under the {gib(floor)} floor; another sweep is holding it")
                if isinstance(policy, NatedevFloor):
                    try:
                        alert_floor_lock(lock_path, free, floor, lint_settings=lint_settings)
                    except OSError as error:
                        print(f"lint sweep: cannot report the floor lock: {error}", file=sys.stderr)
                outcome = FloorOutcome(tmp_sweep.SkippedSweepAssessment(), before=free, after=free)
                return outcome
        if guard is not None:
            record_floor_lock(str(lock), guard, time.time())
        record_path = floor_record_path()
        try:
            if guard is not None:
                with locked_floor_record(record_path) as previous:
                    current = floor_record_or_new(previous, free)
                    transition = next_lock_alert(current.lock_alert, NoFloorLockHolder(), LiveSweepProcess(), time.time())
                    if transition.lock_alert != current.lock_alert:
                        write_floor_record(record_path, replace(current, lock_alert=transition.lock_alert))
        except OSError as error:
            print(f"lint sweep: cannot clear the lock observation: {error}", file=sys.stderr)
        idle: list[str] = []
        tree_targets: dict[str, str] = {}
        paired_roots = OUTPUT_ROOTS.get().roots_for
        known_trees = {} if isinstance(policy, MacFloor) else dict(OUTPUT_ROOTS.get().known_trees)
        targets = (sorted({paired for root in filesystem.targets for paired in paired_roots(root)})
                   if isinstance(filesystem, FilesystemFloor) else
                   target_dirs(roots, lint_settings=lint_settings, known_trees=known_trees))
        all_discovered = sorted(set(targets) | {str(Path(path).expanduser().resolve())
                                 for path in lint_settings.words("sweep_ci_targets") if os.path.isdir(os.path.expanduser(path))})
        excluded_targets = frozenset(all_discovered)
        if isinstance(filesystem, FilesystemFloor) and not filesystem.root_device:
            print(f"lint sweep: filesystem {filesystem.key} at {filesystem.probe_path}")
        if isinstance(policy, MacFloor):
            targets = [target for target in targets if policy.allows(target)]
        owners = TargetOwnerIndex(targets)
        hold_targets = (build_hold_targets(targets, known_trees, lint_settings=lint_settings)
                        if isinstance(policy, NatedevFloor) else {})
        uses: dict[str, TargetUse | LockHeld] = dict(policy.uses(targets) if isinstance(policy, MacFloor) else process_uses(owners))
        if isinstance(policy, NatedevFloor):
            busy_mirrors = mirror_leases.enter_context(copies.target_mirror_leases(targets))
            for root in targets:
                if any(Path(root).resolve().is_relative_to(mirror) for mirror in busy_mirrors):
                    uses[root] = LockHeld("remote mirror lease is busy")
        for root in tuple(hold_targets):
            for paired in paired_roots(root):
                if paired in targets:
                    hold_targets[paired] = hold_targets[root]
        for root in tuple(uses):
            for paired in paired_roots(root):
                if paired in targets:
                    uses[paired] = uses[root]
        locked_files = held_cargo_lock_files(lint_settings=lint_settings)
        tree_depth = lint_settings.whole("sweep_max_tree_depth")
        no_evidence = {item.path for item in OUTPUT_ROOTS.get().no_recorded_evidence}
        for root in targets:
            with scratch.judgment_lock(Path(root)) as idle_copy:
                if root in no_evidence and not idle_copy:
                    held_targets.add(root)
            if root in no_evidence:
                if root in uses or root in hold_targets:
                    held_targets.add(root)
                elif locked_files and root not in held_targets:
                    root_trees = (known_trees[root] if root in known_trees else
                                  _build_trees(root, tree_depth, lint_settings=lint_settings))
                    if any(file_key(path) in locked_files for path in target_lock_paths(root_trees)):
                        held_targets.add(root)
                busy[root] = NoRecordedEvidence(root)
                print(f"lint sweep: protected {root}: no recorded evidence")
                continue
            if root not in known_trees and (root in hold_targets or root in uses):
                known_trees[root] = []
            if not idle_copy:
                busy[root] = LockHeld(os.path.join(root, ".claude-busy"))
                _ = known_trees.setdefault(root, [])
                print(f"lint sweep: busy .claude-busy lock; {root} left alone")
                continue
            if root in hold_targets:
                reason = hold_targets[root]
                print(f"lint sweep: {reason.holder} holds natedev builds; {root} left alone")
                busy[root] = reason
                continue
            if root in uses:
                use = uses[root]
                if isinstance(use, TargetUse):
                    print(f"lint sweep: pid {use.pid} {use.how}; {root} left alone")
                else:
                    print(f"lint sweep: {use.lock}; {root} left alone")
                busy[root] = use
                continue
            root_trees = known_trees[root] if root in known_trees else _build_trees(root, tree_depth, lint_settings=lint_settings)
            known_trees[root] = root_trees
            blocked = next((path for path in target_lock_paths(root_trees)
                            if file_key(path) in locked_files), "")
            if blocked:
                busy[root] = LockHeld(blocked)
                continue
            idle.append(root)
            tree_targets.update({tree: root for tree in root_trees if owners.owner(tree) == TargetOwner(root)})
        for root in tuple(idle):
            pair = paired_roots(root)
            blocked_pair = next((paired for paired in pair if paired in busy), "")
            if blocked_pair:
                busy[root] = busy[blocked_pair]
                idle.remove(root)
        scan = scan_floor_roots(DiscoveredTargetRoots(idle, all_discovered), known_trees, lint_settings=lint_settings)
        target_groups: dict[str, list[Group]] = {root: [] for root in idle}
        grouped_roots = {root: paired_roots(root) for root in set(tree_targets.values())}
        for group in scan.groups:
            for root in grouped_roots[tree_targets[group.build_tree]]:
                if root in target_groups:
                    target_groups[root].append(group)
        target_uses = {root: target_last_use(root, groups, targets) for root, groups in target_groups.items()}
        total = sum(size for key, size in scan.blocks.items()
                    if not isinstance(filesystem, FilesystemFloor) or key[0] == filesystem.device)
        unchanged_caches = 0
        busy_bytes: dict[str, int] = {}
        ci_bytes = 0
        before_caches = total
        if not dry_run:
            # Build holds and Mac claims prevent size walks of their output.
            busy_bytes = {path: 0 for path in busy if on_floor_filesystem(path)}
            measured = measure_protected_folders(frozenset(
                path for path, use in busy.items()
                if not isinstance(policy, MacFloor) and not isinstance(use, BuildHoldTarget)),
                excluded_targets)
            busy_bytes.update(measured)
            protected = tuple((path, size) for path, size in measured if isinstance(busy[path], NoRecordedEvidence))
            held_bytes = sum(busy_bytes.values())
            ci_seen: set[InodeKey] = set()
            ci_bytes = sum(directory_blocks(os.path.expanduser(path), ci_seen)
                           for path in lint_settings.words("sweep_ci_targets")
                           if on_floor_filesystem(os.path.expanduser(path)))
            unchanged_caches = held_bytes + ci_bytes
            before_caches = total + unchanged_caches
        # The growth check compares this pair with the previous record. The scan takes minutes, so
        # free is read again after it: a target another process deletes during the scan is then out
        # of both figures, not out of before_caches alone. On 2026-10-25 the read from before the
        # scan reported a 122.6 GiB fall while 123 GiB was freed.
        scanned_free = free_bytes(home)
        protection_reason = ("because they are protected" if any(isinstance(use, NoRecordedEvidence) for use in busy.values())
                             else "while a build or test run uses them")
        print(
            f"lint sweep: {gib(free)} free, under the {gib(floor)} floor; choosing from {len(idle)}"
            + f" target dirs ({gib(total)}), {len(busy)} left alone {protection_reason}"
        )
        budget = total - max(0, floor - scanned_free)
        left, failures = total, 0
        chosen: list[tuple[FloorTake, int]] = []
        if scanned_free < floor:
            if not dry_run:
                record.floor_scan(scanned_free, floor, floor_scan_targets(
                    scan, [root for root in idle if on_floor_filesystem(root)], target_uses, tree_targets), time.time(),
                    filesystem=f"{filesystem.key}:{filesystem.probe_path}" if isinstance(filesystem, FilesystemFloor) else "")
            left, failures, chosen = shrink_floor(
                scan, total, budget, dry_run, tree_targets, known_trees, busy,
                lambda group: (
                    target_uses[tree_targets[group.build_tree]],
                    *unit_age(group, planned_at),
                ),
                planned_at, owners,
             lint_settings=lint_settings, policy=policy if isinstance(policy, MacFloor) else UseConfiguredValue.CURRENT)
        else:
            print(f"lint sweep: {gib(scanned_free)} free after the scan, not under the {gib(floor)} floor;"
                  + " nothing taken")
        taken: dict[str, int] = {}
        for group, freed in chosen:
            root = group.target if isinstance(group, WholeTargetTake) else tree_targets[group.build_tree]
            taken[root] = taken.get(root, 0) + freed
        for root, size in taken.items():
            if size:
                verb = "would take" if dry_run else "took"
                print(f"lint sweep: the floor {verb} {gib(size)} from {root},"
                      + f" last used {when(target_uses[root])}")
        after_free = free_bytes(home)
        print(f"lint sweep: {gib(after_free)} free")
        if not dry_run and isinstance(policy, NatedevFloor):
            now = time.time()
            unchanged = {key for path in idle if path not in scan.changed_targets and on_floor_filesystem(path)
                         for key in scan.target_links.get(path, {})}
            after_caches = sum(scan.blocks[key] for key in unchanged) + unchanged_caches
            after_caches += sum(directory_blocks(path, unchanged, excluded_targets)
                                for path in idle if path in scan.changed_targets and on_floor_filesystem(path))
            # This pass did not remove these blocks.
            externally_removed_keys = {key for root in idle if root not in taken and not os.path.lexists(root)
                                      for key in scan.target_links.get(root, {})
                                      if key not in unchanged
                                      and (not isinstance(filesystem, FilesystemFloor) or key[0] == filesystem.device)}
            removed = max(0, before_caches - after_caches - sum(scan.blocks[key] for key in externally_removed_keys))
            try:
                with locked_floor_record(record_path) as previous:
                    base = floor_record_or_new(previous, after_free)
                    history = base.alert_history
                    push_history = base.push_history
                    alert_interval = lint_settings.number("sweep_alert_interval_seconds")
                    push_due = isinstance(push_history, NoDeliveredAlert) or now - push_history.at >= alert_interval
                    if left > budget and after_free < floor and push_due:
                        holders = floor_holders(busy, busy_bytes, lint_settings=lint_settings)
                        holders.extend(FloorHolder(root, 0, None, f"{use.holder} holds natedev builds", None)
                                       for root, use in busy.items() if isinstance(use, BuildHoldTarget))
                        told = frozenset(holder.path for holder in holders if holder.session is not None
                                         and send_holder_message(holder.session, holder_message_text(holder, after_free, floor, lint_settings=lint_settings), lint_settings=lint_settings).accepted)
                        measurement = (read_disk_measurement() if not isinstance(filesystem, FilesystemFloor)
                                       or filesystem.root_device else UnavailableDiskMeasurement("missing"))
                        message = floor_out_of_reach_text(now, removed, after_free, floor, holders, told,
                                                          ci_bytes, measurement, failures, protected=protected,
                                                          lint_settings=lint_settings)
                        delivered = send_floor_alert(message, lint_settings=lint_settings)
                        if delivered.accepted or told:
                            push_history = DeliveredAlert(time.time())
                    elif removed > 0 and (isinstance(history, NoDeliveredAlert) or now - history.at >= alert_interval):
                        free_fall = previous.free_bytes - scanned_free if isinstance(previous, FloorRecord) else 0
                        cache_growth = before_caches - previous.build_cache_bytes if isinstance(previous, FloorRecord) else 0
                        recent = isinstance(previous, FloorRecord) and 0 <= now - previous.measured_at <= lint_settings.number("sweep_growth_window_seconds")
                        same_held_roots = (isinstance(previous, FloorRecord)
                                           and isinstance(previous.held_roots, KnownHeldRoots)
                                           and previous.held_roots.roots == frozenset(hold_targets))
                        unexplained_fall = lint_settings.whole("sweep_unexplained_fall_bytes")
                        large_removal = lint_settings.whole("sweep_large_removal_bytes")
                        same_filesystem = True
                        if isinstance(filesystem, FilesystemFloor):
                            same_filesystem = isinstance(previous, FloorRecord) and (
                                previous.filesystem == filesystem.key and previous.measured_roots == tuple(sorted(idle))
                                or filesystem.root_device and filesystem.legacy_history_safe and previous.filesystem == "")
                        growth_fired = recent and same_held_roots and same_filesystem and free_fall - cache_growth > unexplained_fall
                        removal_fired = removed > large_removal
                        if growth_fired or removal_fired:
                            reasons: list[str] = []
                            if growth_fired:
                                reasons.append(f"free space fell more than {unexplained_fall / GIB:g} GiB beyond build-cache growth")
                            if removal_fired:
                                reasons.append(f"removed more than {large_removal / GIB:g} GiB in one sweep")
                            message = floor_alert_text(now, removed, previous, free_fall, cache_growth,
                                                       reasons, read_disk_measurement() if not isinstance(filesystem, FilesystemFloor)
                                                       or filesystem.root_device else UnavailableDiskMeasurement("missing"),
                                                       lint_settings=lint_settings)
                            if send_floor_alert(message, lint_settings=lint_settings).accepted:
                                history = DeliveredAlert(time.time())
                    write_floor_record(record_path, replace(
                        base, measured_at=now, free_bytes=after_free, build_cache_bytes=after_caches,
                        alert_history=history, push_history=push_history,
                        held_roots=KnownHeldRoots(frozenset(hold_targets)),
                        filesystem=filesystem.key if isinstance(filesystem, FilesystemFloor) else "",
                        measured_roots=tuple(sorted(idle))))
            except OSError as error:
                print(f"lint sweep: cannot save floor accounting: {error}", file=sys.stderr)
        outcome = FloorOutcome(tmp_sweep.CompletedSweepAssessment(0 if isinstance(policy, MacFloor) else (1 if failures else 0)),
                               0 if dry_run else max(0, total - left), free, after_free, len(busy), protected=protected)
        return outcome
    except (conf.Unusable, OSError) as error:
        if isinstance(policy, MacFloor):
            raise
        detail = str(error)
        outcome = FloorOutcome(tmp_sweep.CompletedSweepAssessment(1))
        print(f"lint sweep: {error}", file=sys.stderr)
        return outcome
    finally:
        try:
            if not dry_run:
                held_targets.update(root for root, use in busy.items() if not isinstance(use, NoRecordedEvidence))
                for root in tuple(held_targets):
                    held_targets.update(OUTPUT_ROOTS.get().roots_for(root))
                notify_floor_problem(replace(outcome, protected=protected, held_targets=tuple(sorted(held_targets))),
                                     lint_settings=lint_settings, detail=detail,
                                     policy=policy)
        finally:
            try:
                if guard is not None:
                    os.close(guard)
            finally:
                mirror_leases.close()


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


def budget_bytes(environ: dict[str, str], config: str | conf.ConfigRead, repo: str | None) -> tuple[int | None, str]:
    """The sweep budget, None when its value is malformed, and where it came from."""
    raw = environ.get(BUDGET_ENV, "")
    if raw:
        return size_bytes(raw, GIB), f"{BUDGET_ENV} in the environment"
    settings_read = config if isinstance(config, conf.ConfigRead) else conf.read(Path(config))
    config = str(settings_read.source)
    values = settings_read.values()
    keys = [f"{BUDGET_KEY}.{repo}", BUDGET_KEY] if repo else [BUDGET_KEY]
    for key in keys:
        if values.get(key):
            return size_bytes(values[key], GIB), f"{key} in {config}"
    return int(settings_read.number(BUDGET_KEY) * GIB), f"{BUDGET_KEY} in {config}"


def doc_index_bytes(*, lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> int | None:
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    raw = os.environ.get(DOC_INDEX_ENV, "")
    if not raw:
        return int(lint_settings.number("sweep_doc_index_mib") * MIB)
    return size_bytes(raw, MIB)


RebuildCause = Literal["floor take", "budget take", "tmp sweep", "none"]
REBUILD_CAUSES: tuple[RebuildCause, ...] = ("floor take", "budget take", "tmp sweep", "none")
CLONE_KINDS = ("gate and merge copies", "phase clones", "mktemp folders", "scratchpad clones",
               "scratch.py folders", "other /tmp", "~/rust worktrees", "other")


@dataclass(frozen=True)
class SweepRemoval:
    at: float
    path: str
    cause: RebuildCause


@dataclass(frozen=True)
class RebuildStep:
    folder: str
    workspace: str
    started_at: float
    ended_at: float
    crates_compiled: int
    compile_seconds: float


@dataclass(frozen=True)
class ColdRebuild:
    step: RebuildStep
    cause: RebuildCause


@dataclass(frozen=True)
class RebuildEvidence:
    steps: list[RebuildStep]
    trims: list[SweepRemoval]


def sweep_removals_path() -> Path:
    return floor_record_path().with_name("removals.jsonl")


def parse_sweep_removals(rows: Iterable[dict[str, object]]) -> list[SweepRemoval]:
    events: list[SweepRemoval] = []
    for row in rows:
        at, path, cause = row.get("at"), row.get("path"), row.get("cause")
        if (_finite_number(at) and isinstance(path, str) and os.path.isabs(path)
                and cause in ("budget take", "floor take")):
            events.append(SweepRemoval(float(at), path, cast(RebuildCause, cause)))
    return events


def record_sweep_removal(target: str, cause: Literal["budget take", "floor take"],
                         bytes_freed: int, extent: Literal["whole", "partial"], at: float) -> bool:
    """One build-log row per target take, including when stdout is discarded."""
    from scripts.buildlog import record

    try:
        record.sweep_trim(os.path.abspath(target), bytes_freed, extent, cause, at)
    except OSError as error:
        print(f"lint sweep: could not record removal of {target}: {error}", file=sys.stderr)
        return False
    return True


def read_sweep_removals() -> list[SweepRemoval]:
    try:
        return parse_sweep_removals(store.locked_rows(sweep_removals_path()))
    except OSError as error:
        print(f"lint sweep: could not read removals: {error}", file=sys.stderr)
        return []


def merge_sweep_removals(*sources: Sequence[SweepRemoval]) -> list[SweepRemoval]:
    events: dict[tuple[str, RebuildCause, int], SweepRemoval] = {}
    for source in sources:
        for event in source:
            key = (event.path, event.cause, int(event.at))
            _ = events.setdefault(key, event)
    return sorted(events.values(), key=lambda event: event.at)


def parse_sweep_journal(lines: Iterable[str]) -> list[SweepRemoval]:
    """Read journalctl JSON lines; budget headers belong to their own output stream."""
    events: list[SweepRemoval] = []
    budgets: dict[str, list[str]] = {}
    for line in lines:
        raw = cast(object, json.loads(line))
        if not isinstance(raw, dict):
            continue
        row = cast(dict[str, object], raw)
        message = row.get("MESSAGE")
        stamp = row.get("__REALTIME_TIMESTAMP")
        if not isinstance(message, str) or not isinstance(stamp, (str, int, float)):
            continue
        at = float(stamp) / 1_000_000
        stream = str(row.get("_STREAM_ID") or (str(row.get("_BOOT_ID", "")) + ":" + str(row.get("_PID", ""))))
        floor = re.search(r"lint sweep: the floor took [\d.]+ GiB from (/.+), last used ", message)
        temporary = re.search(r"tmp sweep: deleted (/.+) \([\d.]+ GiB\), last changed ", message)
        budget = re.search(r"lint sweep: (/.+) is [\d.]+ GiB, (?:over|within) the .* budget", message)
        if floor:
            events.append(SweepRemoval(at, floor[1], "floor take"))
        elif temporary:
            events.append(SweepRemoval(at, temporary[1], "tmp sweep"))
        elif budget:
            budgets[stream] = budget[1].split(", ")
        elif "floor; choosing from" in message:
            _ = budgets.pop(stream, None)
        elif re.search(r"lint sweep: removed \d+ build units and \d+ incremental dirs", message):
            events.extend(SweepRemoval(at, path, "budget take") for path in budgets.get(stream, []))
    return sorted(events, key=lambda event: event.at)


def read_sweep_journal(since: float, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> list[SweepRemoval]:
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    result = subprocess.run(
        ["journalctl", "--since", f"@{since:.6f}", "--output=json", "--no-pager",
         "--grep=lint sweep:|tmp sweep: deleted"],
        capture_output=True, text=True, check=True, timeout=lint_settings.number("sweep_history_timeout_seconds"),
    )
    return parse_sweep_journal(result.stdout.splitlines())


def read_rebuild_evidence(root: Path) -> RebuildEvidence:
    """Read steps and trims together, without creating or refreshing the build-log index."""
    paths = sorted((root / "natedev").glob("????-??.jsonl"))
    if not paths:
        raise ValueError(f"no natedev build log under {root}")
    steps: list[RebuildStep] = []
    trims: list[SweepRemoval] = []
    seen: set[str] = set()
    for row in build_log_rows(paths):
        kind = row.get("kind")
        if (row.get("host") != "natedev" or kind not in ("step", "sweep_trim")
                or (kind == "step" and row.get("step") == "sweep")):
            continue
        identity = row.get("id")
        if isinstance(identity, str):
            if identity in seen:
                continue
            seen.add(identity)
        if kind == "sweep_trim":
            at, folder, cause = row.get("at"), row.get("folder"), row.get("cause")
            if (isinstance(at, str) and isinstance(folder, str) and os.path.isabs(folder)
                    and cause in ("budget take", "floor take")):
                trims.append(SweepRemoval(datetime.fromisoformat(at).timestamp(), os.path.normpath(folder),
                                          cast(RebuildCause, cause)))
            continue
        folder = row.get("cwd") or row.get("worktree")
        start = row.get("started_at")
        if not isinstance(folder, str) or not isinstance(start, str):
            continue
        started = datetime.fromisoformat(start).timestamp()
        end = row.get("ended_at")
        ended = datetime.fromisoformat(end).timestamp() if isinstance(end, str) else started
        crates = row.get("crates_compiled")
        seconds = row.get("finished_s")
        if not _finite_number(seconds):
            seconds = row.get("duration_s")
        workspace = row.get("worktree")
        workspace = workspace if isinstance(workspace, str) and workspace else folder
        steps.append(RebuildStep(os.path.normpath(folder), os.path.normpath(workspace), started, ended,
                                 int(crates) if _finite_number(crates) else 0,
                                 max(0.0, float(seconds)) if _finite_number(seconds) else 0.0))
    return RebuildEvidence(sorted(steps, key=lambda step: step.started_at), sorted(trims, key=lambda trim: trim.at))


def read_rebuild_steps(root: Path) -> list[RebuildStep]:
    return read_rebuild_evidence(root).steps


def clone_kind(folder: str) -> str:
    path = Path(folder)
    parts = path.parts
    # A scratch HOME sits under /tmp, so its ~/rust worktrees are named before the /tmp groups.
    if path.is_relative_to(Path.home() / "rust"):
        return "~/rust worktrees"
    if path.is_relative_to("/tmp") or path.is_relative_to("/private/tmp"):
        # Older copies include gate57e, mergeclone and p34-merge-<sha>.
        if any(re.search(r"(?:^|[-_])(gate|merge)", part) for part in parts):
            return "gate and merge copies"
        if path.is_relative_to("/tmp/claude/scratch"):
            return "scratch.py folders"
        if "phases" in parts or any(re.search(r"(?:^|[-_])phase(?:\d|[-_])", part) for part in parts):
            return "phase clones"
        if "scratchpad" in parts:
            return "scratchpad clones"
        if any(part.startswith(("tmp.", "tmp_", ".tmp")) or re.fullmatch(r"tmp[a-z0-9_]{8}", part)
               for part in parts):
            return "mktemp folders"
        return "other /tmp"
    return "other"


def rebuild_causes(steps: Sequence[RebuildStep], events: Sequence[SweepRemoval],
                   since: float, until: float, *,
    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT,
) -> list[ColdRebuild]:
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    return _rebuild_causes(steps, events, since, until, lint_settings.whole("sweep_cold_crates"))


def _rebuild_causes(steps: Sequence[RebuildStep], events: Sequence[SweepRemoval],
                    since: float, until: float, cold_crates: int) -> list[ColdRebuild]:
    by_folder: dict[str, list[SweepRemoval]] = {}
    saved = saved_cargo_output_roots()
    mappings = (*saved.workspaces, *saved.history)
    for event in events:
        # Floor and budget lines name a target; tmp sweep lines name the removed folder.
        folder = event.path if event.cause == "tmp sweep" else str(Path(event.path).parent)
        by_folder.setdefault(folder, []).append(event)
        for mapping in mappings:
            if event.cause != "tmp sweep" and any(Path(event.path).is_relative_to(root)
                    for root in (mapping.target_directory, mapping.build_directory)):
                by_folder.setdefault(str(mapping.workspace_root), []).append(event)
    previous: dict[str, RebuildStep] = {}
    cold: list[ColdRebuild] = []
    for step in sorted(steps, key=lambda item: item.started_at):
        prior = previous.get(step.workspace)
        if since <= step.started_at < until and step.crates_compiled >= cold_crates:
            cause: RebuildCause = "none"
            if prior is not None:
                candidates = [event for folder in {Path(step.folder), *Path(step.folder).parents,
                                                   Path(step.workspace), *Path(step.workspace).parents}
                              for event in by_folder.get(str(folder), [])
                              if event.cause == "tmp sweep" and prior.ended_at <= event.at <= step.started_at]
                candidates.extend(event for folder in {step.folder, step.workspace}
                                  for event in by_folder.get(folder, [])
                                  if event.cause != "tmp sweep" and prior.ended_at <= event.at <= step.started_at)
                if candidates:
                    cause = max(candidates, key=lambda event: event.at).cause
            cold.append(ColdRebuild(step, cause))
        previous[step.workspace] = step
    return cold


def rebuilds_main(argv: list[str]) -> int:
    lint_settings = conf.read("lint")
    if len(argv) != 2 or argv[0] != "--days" or not argv[1].isdigit() or int(argv[1]) <= 0:
        print("usage: sweep.py rebuilds --days N (N must be a positive integer)", file=sys.stderr)
        return 2
    days = int(argv[1])
    cold_crates = lint_settings.whole("sweep_cold_crates")
    until = time.time()
    since = until - days * DAY_SECONDS
    root = store.root()
    try:
        evidence = read_rebuild_evidence(root)
        steps = evidence.steps
        # Include a removal before the window if it followed the cold step's previous step.
        previous: dict[str, RebuildStep] = {}
        journal_since = since
        for step in steps:
            prior = previous.get(step.workspace)
            if since <= step.started_at < until and step.crates_compiled >= cold_crates and prior is not None:
                journal_since = min(journal_since, prior.ended_at)
            previous[step.workspace] = step
        events = merge_sweep_removals(read_sweep_journal(journal_since, lint_settings=lint_settings), evidence.trims, read_sweep_removals())
        cold = _rebuild_causes(steps, events, since, until, cold_crates)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"lint sweep: cannot read rebuild evidence: {error}", file=sys.stderr)
        return 2
    totals: dict[tuple[str, RebuildCause], float] = {}
    for rebuild in cold:
        key = (clone_kind(rebuild.step.workspace), rebuild.cause)
        totals[key] = totals.get(key, 0.0) + rebuild.step.compile_seconds / 3600
    print(f"Cold rebuild compile-hours over {days} days ({len(cold)} steps with crates_compiled >= {cold_crates})")
    print("Clone kind | " + " | ".join(REBUILD_CAUSES) + " | total")
    print("--- | " + " | ".join("---:" for _ in range(5)))
    for kind in CLONE_KINDS:
        hours = [totals.get((kind, cause), 0.0) for cause in REBUILD_CAUSES]
        print(kind + " | " + " | ".join(f"{value:.2f}" for value in (*hours, sum(hours))))
    hours = [sum(value for (_, recorded), value in totals.items() if recorded == cause)
             for cause in REBUILD_CAUSES]
    print("Total | " + " | ".join(f"{value:.2f}" for value in (*hours, sum(hours))))
    print("Per day | " + " | ".join(f"{value / days:.2f}" for value in (*hours, sum(hours))))
    print("Hours use cargo finished_s, or step duration_s when absent. The latest matching removal")
    print("between the previous step's end and the cold step's start sets the cause; none means no matching evidence.")
    return 0


def main(argv: list[str]) -> int:
    from scripts.lint import tmp_sweep

    if argv and argv[0] == "rebuilds":
        return rebuilds_main(argv[1:])
    dry_run = False
    floor_only = False
    host = ""
    target_dir: str | None = None
    args = iter(argv)
    for arg in args:
        if arg == "--dry-run":
            dry_run = True
        elif arg == FLOOR_ONLY_FLAG:
            floor_only = True
        elif arg == "--host":
            host = next(args, "")
            if host not in ("mac", "natedev"):
                print("lint sweep: --host must be mac or natedev", file=sys.stderr)
                return 2
        elif arg == TARGET_DIR_FLAG:
            target_dir = next(args, None)
            if target_dir is None:
                print(f"lint sweep: {TARGET_DIR_FLAG} needs a directory", file=sys.stderr)
                return 2
        else:
            print(f"lint sweep: unknown argument {arg}", file=sys.stderr)
            return 2
    if host == "mac":
        if not floor_only:
            print("lint sweep: --host mac requires --floor-only", file=sys.stderr)
            return 2
        try:
            with conf.diagnostics(report=False):
                mac_settings = conf.read("mac_test")
                mirror_root = MacMirrorRoot.read(mac_settings)
                mac_floor, mac_roots = mac_floor_settings(mac_settings, mirror_root=mirror_root)
                lint_settings = conf.read("lint")
                timeout = mac_settings.number("probe_timeout_s")
            with copies.mirror_sweep_leases(mirror_root.local_path()) as leases:
                claims = floor_claims.read_claims(timeout)
                return hold_floor(mac_floor, dry_run, lint_settings=lint_settings,
                                  policy=MacFloor(mac_roots, claims, mirror_root.local_path(), leases.busy, leases.held))
        except conf.Unusable as error:
            print(f"lint sweep: {error}", file=sys.stderr)
            return 2
    config = str(conf.path("lint"))
    try:
        settings_read = conf.read("lint")
        values = settings_read.values()
        recent_seconds = int(settings_read.number(RECENT_KEY) * 60)
        floor, floor_key = floor_bytes(values, socket.gethostname())
    except conf.Unusable as error:
        print(f"lint sweep: {error}", file=sys.stderr)
        return 2
    recent_minutes = recent_seconds / 60
    if floor is None and floor_key is not None:
        print(f"lint sweep: {floor_key} in {config} must be a non-negative number of GiB", file=sys.stderr)
        return 2
    status = 0 if floor_only else sweep_workspace(target_dir, settings_read, dry_run, recent_minutes,
                                                 lint_settings=settings_read)
    if floor_only:
        status = report_assessment(lambda: tmp_sweep.run_if_due(dry_run, lint_settings=settings_read),
                                   TemporarySweepFailureScope(), dry_run, settings_read) or status
    if floor is not None:
        status = hold_floor(floor, dry_run, lint_settings=settings_read) or status
    return status


def report_assessment(run: Callable[[], tmp_sweep.SweepAssessment], scope: SweepFailureScope,
                      dry_run: bool, lint_settings: conf.ConfigRead) -> int:
    """Assess one scope and report its failure episode."""
    from scripts.lint import tmp_sweep

    detail = ""
    try:
        assessment = run()
    except (conf.Unusable, OSError) as error:
        detail = str(error)
        print(f"lint sweep: {scope.label}: {error}", file=sys.stderr)
        assessment = tmp_sweep.CompletedSweepAssessment(1)
    if not dry_run:
        notify_floor_problem(assessment, lint_settings=lint_settings, detail=detail, scope=scope)
    return assessment.status


def sweep_workspace(target_dir: str | None, budget_config: conf.ConfigRead, dry_run: bool,
                    recent_minutes: float, *,
                    lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> int:
    if isinstance(lint_settings, UseConfiguredValue):
        lint_settings = conf.read("lint")
    token = OUTPUT_ROOTS.set(CargoOutputIndex())
    try:
        roots = (cargo_roots() if target_dir is None else
                 [os.path.realpath(target_dir)] if os.path.isdir(target_dir) else [])
        def assess() -> tmp_sweep.SweepAssessment:
            if not dry_run and target_dir is None:
                if OUTPUT_ROOTS.get().workspaces:
                    save_cargo_output_roots(OUTPUT_ROOTS.get())
                for root in roots:
                    try:
                        Path(root, USE_STAMP).touch()
                    except OSError:
                        pass
            return _sweep_workspace(roots, budget_config, dry_run, recent_minutes, lint_settings=lint_settings)
        return report_assessment(assess, WorkspaceFailureScope(tuple(sorted(roots))), dry_run, lint_settings)
    finally:
        OUTPUT_ROOTS.reset(token)


def _sweep_workspace(roots: list[str], budget_config: conf.ConfigRead, dry_run: bool,
                    recent_minutes: float, *,
                    lint_settings: conf.ConfigRead) -> tmp_sweep.SweepAssessment:
    """Sweep the resolved output roots to their budget."""
    from scripts.lint import tmp_sweep

    if not roots:
        print("lint sweep: no target directory to sweep")
        return tmp_sweep.SkippedSweepAssessment()
    budget, source = budget_bytes(dict(os.environ), budget_config, repo_name(os.getcwd()))
    if budget is None:
        print(f"lint sweep: {source} must be a non-negative number of GiB", file=sys.stderr)
        return tmp_sweep.CompletedSweepAssessment(2)
    doc_budget = doc_index_bytes(lint_settings=lint_settings)
    if doc_budget is None:
        print(f"lint sweep: {DOC_INDEX_ENV} must be a non-negative number of MiB", file=sys.stderr)
        return tmp_sweep.CompletedSweepAssessment(2)
    uses = process_uses(roots)
    if uses:
        root, use = next(iter(uses.items()))
        print(f"lint sweep: pid {use.pid} {use.how}; {root} skipped")
        return tmp_sweep.SkippedSweepAssessment()
    tree_depth = lint_settings.whole("sweep_max_tree_depth")
    trees = [tree for root in roots for tree in _build_trees(root, tree_depth, lint_settings=lint_settings)]
    locks = lock_trees(output_lock_trees(roots, trees))
    if isinstance(locks, TargetLockBusy):
        print(f"lint sweep: a cargo build holds {locks.path}; skipped")
        return tmp_sweep.SkippedSweepAssessment()
    try:
        doc_status = prune_doc_index(roots, doc_budget, dry_run, lint_settings=lint_settings)
        return tmp_sweep.CompletedSweepAssessment(
            sweep(roots, trees, budget, source, dry_run, recent_minutes, lint_settings=lint_settings) or doc_status)
    finally:
        release(locks.held)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
