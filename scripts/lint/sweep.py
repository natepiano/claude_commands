#!/usr/bin/env python3
"""Keep a cargo project's target directory under a size budget.

cargo-port runs `lint sweep` last in every per-project lint run, and
`invoke.sh` hands the call here. When the target directory is over budget,
this removes the least recently used build output until it fits:

    build unit        every entry under a build tree's .fingerprint/, build/,
                      deps/ and examples/ that carries one unit's 16-hex-digit
                      hash, removed together
    incremental dir   one direct child of a build tree's incremental/

The budget is LINT_SWEEP_BUDGET_GIB (default 96), summed over every file
under the target and build directories, so output the budget sweep never
removes (test-run folders, binaries cargo copied up out of deps/) still
counts. doc/ has its own rule, below. --dry-run reports what would go and
removes nothing.

Why 96. A budget below the working set evicts output the next lint run needs,
and because this runs after every lint run, that rebuild repeats on every
save. Replaying hana's cycle into an empty target (2026-09-15: scoped and
workspace clippy, doc and mend, the nextest test build, the external-client
fixture, the app build) came to 56.9 GiB, of which the test build alone was
33.5 GiB; the clerestory-tests suite adds 9.4 GiB. A rerun rebuilt nothing,
but sweeping that target to 48 GiB made the next cycle rebuild nearly every
workspace unit. 96 covers that working set with room for feature and profile
variants.

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
build graph, so the newest atime in .fingerprint/<unit>/ is its last use.
relatime refreshes an atime at most once a day, so eviction orders by whole
days since last use, then by compile time (newest mtime) inside a day. An
incremental dir's mtime moves on every compile of its crate. Anything that
reads every fingerprint JSON resets every atime and erases that order;
cargo-sweep --installed does exactly that, and running it before --time kept
this sweep's predecessor from removing anything at all.

Concurrency. cargo 1.98.1 holds flock() on .cargo-lock, .cargo-build-lock
and .cargo-artifact-lock for the whole build (checked 2026-09-15 by probing
each with LOCK_EX|LOCK_NB while a build script slept). This takes all of them
without waiting, in every build tree, before scanning, and skips the sweep if
any is held: a delete under a running build leaves cargo failing to write
into fingerprint dirs that vanished. Holding them makes a build that starts
mid-sweep wait until the sweep finishes.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, TypedDict, cast

GIB = 1 << 30
MIB = 1 << 20
DAY_SECONDS = 86_400
DEFAULT_BUDGET_GIB = 96.0
BUDGET_ENV = "LINT_SWEEP_BUDGET_GIB"
DEFAULT_DOC_INDEX_MIB = 250.0
DOC_INDEX_ENV = "LINT_SWEEP_DOC_INDEX_MIB"
DOC_DIR = "doc"
DOC_LOCK_NAME = ".lock"
# The cross-crate stores every rustdoc run reads and rewrites at its end.
MERGE_DIRS = ("search.index", "trait.impl", "type.impl")
LOCK_NAMES = (".cargo-lock", ".cargo-build-lock", ".cargo-artifact-lock")
HASHED_DIRS = (".fingerprint", "build", "deps", "examples")
FINGERPRINT_DIR = ".fingerprint"
INCREMENTAL_DIR = "incremental"
# target/debug, target/<triple>/debug, target/<custom>/<triple>/debug.
MAX_TREE_DEPTH = 3
HASH_LENGTH = 16
HEX_DIGITS = frozenset("0123456789abcdef")

InodeKey = tuple[int, int]
GroupKind = Literal["unit", "incremental"]


class CargoMetadata(TypedDict, total=False):
    target_directory: str
    build_directory: str


@dataclass
class Group:
    """One removable piece of build output and the inode links it holds."""

    kind: GroupKind
    entries: list[str] = field(default_factory=list)
    inodes: list[InodeKey] = field(default_factory=list)
    last_used: float = 0.0
    compiled: float = 0.0


@dataclass
class Scan:
    blocks: dict[InodeKey, int] = field(default_factory=dict)
    links: dict[InodeKey, int] = field(default_factory=dict)
    groups: list[Group] = field(default_factory=list)


def unit_hash(name: str) -> str | None:
    """The unit hash cargo appends to a per-unit file or directory name."""
    stem = name.split(".", 1)[0]
    _, separator, suffix = stem.rpartition("-")
    if not separator or len(suffix) != HASH_LENGTH or not set(suffix) <= HEX_DIGITS:
        return None
    return suffix


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


def newest_times(directory: str) -> tuple[float, float]:
    """Newest atime-or-mtime and newest mtime over a directory and its direct children."""
    stat = os.lstat(directory)
    used, modified = max(stat.st_atime, stat.st_mtime), stat.st_mtime
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                child = entry.stat(follow_symlinks=False)
                used = max(used, child.st_atime, child.st_mtime)
                modified = max(modified, child.st_mtime)
    except OSError:
        pass
    return used, modified


def group_roots(trees: list[str], scan: Scan) -> dict[str, Group]:
    """Map each removable entry's path to its group, with last-use times filled in."""
    roots: dict[str, Group] = {}
    for tree in trees:
        units: dict[str, Group] = {}
        for hashed in HASHED_DIRS:
            try:
                with os.scandir(os.path.join(tree, hashed)) as entries:
                    listed = list(entries)
            except OSError:
                continue
            for entry in listed:
                digest = unit_hash(entry.name)
                if digest is None:
                    continue
                group = units.get(digest)
                if group is None:
                    group = Group(kind="unit")
                    units[digest] = group
                    scan.groups.append(group)
                group.entries.append(entry.path)
                roots[entry.path] = group
                if hashed == FINGERPRINT_DIR and entry.is_dir(follow_symlinks=False):
                    used, compiled = newest_times(entry.path)
                    group.last_used = max(group.last_used, used)
                    group.compiled = max(group.compiled, compiled)
        for group in units.values():
            if group.last_used == 0.0:
                # No fingerprint dir survives for this hash, so nothing reads
                # these entries any more; their own mtimes are all there is.
                mtimes = [os.lstat(path).st_mtime for path in group.entries]
                group.last_used = group.compiled = max(mtimes)
        try:
            with os.scandir(os.path.join(tree, INCREMENTAL_DIR)) as entries:
                listed = list(entries)
        except OSError:
            continue
        for entry in listed:
            _, modified = newest_times(entry.path)
            group = Group(kind="incremental", entries=[entry.path], last_used=modified, compiled=modified)
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


def choose(scan: Scan, total: int, budget: int) -> tuple[list[Group], int]:
    """Least recently used groups to remove, and the size left once they go.

    A file's blocks come back only when its last link goes, so a binary cargo
    hard-linked out of deps/ frees nothing until that copy goes too.
    """
    now = time.time()
    order = sorted(
        scan.groups,
        key=lambda group: (-int((now - group.last_used) // DAY_SECONDS), group.compiled),
    )
    remaining = dict(scan.links)
    chosen: list[Group] = []
    for group in order:
        if total <= budget:
            break
        chosen.append(group)
        for key in group.inodes:
            remaining[key] -= 1
            if remaining[key] == 0:
                total -= scan.blocks[key]
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


def directory_blocks(directory: str) -> int:
    """Disk blocks held under a directory, counting a hard-linked file once."""
    seen: set[InodeKey] = set()
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


def lock_doc(doc: str) -> int | None:
    """Take rustdoc's own lock without waiting; None when a doc run holds it."""
    path = os.path.join(doc, DOC_LOCK_NAME)
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
        held = lock_doc(doc)
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
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def sweep(roots: list[str], trees: list[str], budget: int, dry_run: bool) -> int:
    scan = Scan()
    owners = group_roots(trees, scan)
    for root in roots:
        walk(root, None, owners, scan)
    total = sum(scan.blocks.values())
    label = ", ".join(roots)
    if total <= budget:
        print(f"lint sweep: {label} is {gib(total)}, within the {gib(budget)} budget")
        return 0
    print(f"lint sweep: {label} is {gib(total)}, over the {gib(budget)} budget")
    chosen, left = choose(scan, total, budget)
    failures = 0 if dry_run else remove(chosen)
    if chosen:
        units = sum(1 for group in chosen if group.kind == "unit")
        verb, result = ("would remove", "would leave") if dry_run else ("removed", "left")
        print(
            f"lint sweep: {verb} {units} build units and {len(chosen) - units} incremental dirs"
            + f" ({gib(total - left)}), last used {when(chosen[0].last_used)}"
            + f" to {when(chosen[-1].last_used)}; {result} {gib(left)}"
        )
    if left > budget:
        print(
            f"lint sweep: {gib(left)} remains over budget in output this sweep never removes"
            + " (test-run folders, binaries copied out of deps/, doc/ under its own budget)"
        )
    return 1 if failures else 0


def budget_bytes() -> int | None:
    raw = os.environ.get(BUDGET_ENV, "")
    if not raw:
        return int(DEFAULT_BUDGET_GIB * GIB)
    try:
        value = float(raw)
    except ValueError:
        return None
    return int(value * GIB) if value >= 0 else None


def doc_index_bytes() -> int | None:
    raw = os.environ.get(DOC_INDEX_ENV, "")
    if not raw:
        return int(DEFAULT_DOC_INDEX_MIB * MIB)
    try:
        value = float(raw)
    except ValueError:
        return None
    return int(value * MIB) if value >= 0 else None


def main(argv: list[str]) -> int:
    dry_run = False
    for arg in argv:
        if arg == "--dry-run":
            dry_run = True
        else:
            print(f"lint sweep: unknown argument {arg}", file=sys.stderr)
            return 2
    budget = budget_bytes()
    if budget is None:
        print(f"lint sweep: {BUDGET_ENV} must be a non-negative number of GiB", file=sys.stderr)
        return 2
    doc_budget = doc_index_bytes()
    if doc_budget is None:
        print(f"lint sweep: {DOC_INDEX_ENV} must be a non-negative number of MiB", file=sys.stderr)
        return 2
    roots = cargo_roots()
    if not roots:
        print("lint sweep: no target directory to sweep")
        return 0
    trees = [tree for root in roots for tree in build_trees(root)]
    held, blocked = lock_trees(trees)
    if blocked is not None:
        print(f"lint sweep: a cargo build holds {blocked}; skipped")
        return 0
    try:
        doc_status = prune_doc_index(roots, doc_budget, dry_run)
        return sweep(roots, trees, budget, dry_run) or doc_status
    finally:
        release(held)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
