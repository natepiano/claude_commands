"""Measure each output filesystem once for the build report's disk table."""

from __future__ import annotations

import json
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from scripts.lib import conf
from scripts.lib.cargo_output import CargoOutputUnavailable, read_cargo_output_roots
from scripts.lib.user_zone import user_zone

from scripts.buildlog import store
from scripts.buildlog import filesystem


class DiskRow(TypedDict):
    label: str
    bytes: int


class OutsideBuildCacheDirectory(TypedDict):
    path: str
    bytes: int
    growth_bytes: int
    largest_child_path: str | None
    largest_child_growth_bytes: int | None
    child_bytes: NotRequired[dict[str, int]]


class OutsideBuildCacheTotal(TypedDict):
    label: str
    bytes: int
    growth_bytes: int


class FilesystemCapacity(TypedDict):
    path: str
    device: str
    device_name: str
    used: int
    free: int


class DiskSnapshot(TypedDict):
    measured_at: str
    capacity_measured_at: NotRequired[str]
    host: str
    rows: list[DiskRow]
    used: int
    free: int
    floor: int | None
    output_unavailable: NotRequired[list[str]]
    filesystems: NotRequired[list[FilesystemCapacity]]
    previous_measured_at: NotRequired[str | None]
    outside_build_caches: NotRequired[list[OutsideBuildCacheDirectory]]
    outside_build_cache_totals: NotRequired[list[OutsideBuildCacheTotal]]


class MeasuredDiskSnapshot(TypedDict):
    measured_at: str
    capacity_measured_at: str
    host: str
    rows: list[DiskRow]
    used: int
    free: int
    floor: int | None
    output_unavailable: NotRequired[list[str]]
    filesystems: NotRequired[list[FilesystemCapacity]]
    previous_measured_at: str | None
    outside_build_caches: list[OutsideBuildCacheDirectory]
    outside_build_cache_totals: list[OutsideBuildCacheTotal]


MIN_OUTSIDE_DIRECTORY_BYTES = 1 << 30


FOLDERS: list[tuple[str, str]] = [
    ("~/rust", "~/rust"),
    ("/tmp", "/tmp"),
    ("CI runner 1", "/var/lib/hana-ci/hana-linux-1"),
    ("CI runner 2", "/var/lib/hana-ci/hana-linux-2"),
]


@dataclass(frozen=True)
class EarlierOutsideMeasurement:
    measured_at: str
    directory_bytes: dict[str, int]
    child_directory_bytes: dict[str, int]
    folder_bytes: dict[str, int]


@dataclass(frozen=True)
class NoEarlierOutsideMeasurement:
    pass


type PreviousOutsideMeasurement = EarlierOutsideMeasurement | NoEarlierOutsideMeasurement
NO_EARLIER_OUTSIDE_MEASUREMENT = NoEarlierOutsideMeasurement()


class InvalidFloor(ValueError):
    """A configured disk floor that cannot be measured in GiB."""


def read_floor() -> int | None:
    from scripts.lint import sweep

    config = str(conf.path("lint"))
    floor, key = sweep.floor_bytes(conf.read(Path(config)).values(), socket.gethostname())
    if floor is None and key is not None:
        raise InvalidFloor(f"{key} in {config} must be a non-negative number of GiB")
    return floor


def outside_directory_bytes(folders: list[tuple[str, str]]) -> tuple[dict[str, int], dict[str, int]]:
    """Allocated bytes under folder roots and shallow directories, excluding cargo targets."""
    from scripts.lint import sweep

    sizes: dict[str, int] = {}
    depths: dict[str, int] = {}
    seen: set[sweep.InodeKey] = set()
    for _, root in folders:
        frontier: list[tuple[str, int, tuple[str, ...]]] = [(os.path.expanduser(root), 0, ())]
        while frontier:
            directory, depth, parents = frontier.pop()
            if sweep.is_cargo_target(directory):
                continue
            counted = (*parents, directory) if depth in (0, 1, 2, 3) else parents
            if depth in (0, 1, 2, 3):
                _ = sizes.setdefault(directory, 0)
                depths[directory] = depth
            try:
                with os.scandir(directory) as entries:
                    listed = sorted(entries, key=lambda entry: entry.name)
            except OSError:
                continue
            for entry in reversed(listed):
                try:
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    frontier.append((entry.path, depth + 1, counted))
                    continue
                key = (stat.st_dev, stat.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                for parent in counted:
                    sizes[parent] += stat.st_blocks * 512
    return sizes, depths


def previous_outside_measurement(snapshot: DiskSnapshot | None) -> PreviousOutsideMeasurement:
    if snapshot is None:
        return NO_EARLIER_OUTSIDE_MEASUREMENT
    rows = snapshot.get("outside_build_caches")
    totals = snapshot.get("outside_build_cache_totals")
    if not isinstance(rows, list) or not isinstance(totals, list):
        return NO_EARLIER_OUTSIDE_MEASUREMENT
    child_sizes = {path: size for row in rows for path, size in row.get("child_bytes", {}).items()}
    return EarlierOutsideMeasurement(
        snapshot["measured_at"], {row["path"]: row["bytes"] for row in rows}, child_sizes,
        {row["label"]: row["bytes"] for row in totals},
    )


def outside_directories(
    folders: list[tuple[str, str]], previous: PreviousOutsideMeasurement,
) -> tuple[str | None, list[OutsideBuildCacheDirectory], list[OutsideBuildCacheTotal]]:
    earlier = previous.measured_at if isinstance(previous, EarlierOutsideMeasurement) else None
    old_sizes = previous.directory_bytes if isinstance(previous, EarlierOutsideMeasurement) else {}
    old_child_sizes = previous.child_directory_bytes if isinstance(previous, EarlierOutsideMeasurement) else {}
    old_folder_sizes = previous.folder_bytes if isinstance(previous, EarlierOutsideMeasurement) else {}
    sizes, depths = outside_directory_bytes(folders)
    totals: list[OutsideBuildCacheTotal] = [
        {
            "label": label,
            "bytes": sizes.get(os.path.expanduser(path), 0),
            "growth_bytes": sizes.get(os.path.expanduser(path), 0) - old_folder_sizes.get(label, 0),
        }
        for label, path in folders
    ]
    children_by_parent: dict[str, dict[str, int]] = {}
    for path, size in sizes.items():
        children_by_parent.setdefault(str(Path(path).parent), {})[path] = size
    rows: list[OutsideBuildCacheDirectory] = [
        {
            "path": path,
            "bytes": size,
            "growth_bytes": size - old_sizes.get(path, 0),
            "largest_child_path": None,
            "largest_child_growth_bytes": None,
            "child_bytes": children_by_parent.get(path, {}),
        }
        for path, size in sorted(sizes.items())
        if depths[path] in (1, 2) and size >= MIN_OUTSIDE_DIRECTORY_BYTES
    ]
    for row in rows:
        children = row.get("child_bytes", {})
        if children:
            largest = max(children, key=lambda path: (children[path] - old_child_sizes.get(path, 0), path))
            row["largest_child_path"] = largest
            row["largest_child_growth_bytes"] = children[largest] - old_child_sizes.get(largest, 0)
    return earlier, rows, totals


def measure(
    folders: list[tuple[str, str]], usage: Callable[[], filesystem.FilesystemUsage], floor: int | None,
    previous: PreviousOutsideMeasurement = NO_EARLIER_OUTSIDE_MEASUREMENT,
) -> MeasuredDiskSnapshot:
    from scripts.lint import sweep

    seen: set[sweep.InodeKey] = set()
    rows: list[DiskRow] = [
        {"label": label, "bytes": sweep.directory_blocks(os.path.expanduser(path), seen)}
        for label, path in folders
    ]
    previous_measured_at, outside_build_caches, outside_build_cache_totals = outside_directories(folders, previous)
    current_usage = usage()
    measured_at = store.utc_iso(time.time())
    return {
        "measured_at": measured_at,
        "capacity_measured_at": measured_at,
        "host": store.host_name(),
        "rows": rows,
        "used": current_usage.used,
        "free": current_usage.free,
        "floor": floor,
        "previous_measured_at": previous_measured_at,
        "outside_build_caches": outside_build_caches,
        "outside_build_cache_totals": outside_build_cache_totals,
    }


def capacity_rows(paths: list[Path], measured: tuple[tuple[int, filesystem.FilesystemUsage], ...] = ()) -> list[FilesystemCapacity]:
    rows: list[FilesystemCapacity] = []
    seen: set[str] = set()
    for path in paths:
        existing = path.expanduser().resolve()
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        try:
            usage = dict(measured).get(existing.stat().st_dev)
            if usage is None:
                usage = filesystem.filesystem_usage(str(existing))
        except OSError:
            continue
        identity = usage.device if usage.device != "unavailable" else str(existing.stat().st_dev)
        if identity in seen:
            continue
        seen.add(identity)
        rows.append({"path": str(existing), "device": identity, "device_name": usage.device_name,
                     "used": usage.used, "free": usage.free})
    return rows


def mapped_output_paths(folders: list[tuple[str, str]]) -> tuple[list[Path], list[str]]:
    """Read current mappings and retain saved output roots for older workspaces."""
    from scripts.lint import sweep

    saved = sweep.saved_cargo_output_roots()
    mappings = list((*saved.workspaces, *saved.history))
    checkouts: list[str] = []
    _ = sweep.target_dirs([path for _, path in folders], checkouts=checkouts)
    checkouts.extend(str(item.workspace_root) for item in saved.workspaces
                     if (item.workspace_root / "Cargo.toml").is_file())
    unavailable: list[str] = []
    for checkout in sorted(set(checkouts)):
        roots = read_cargo_output_roots(Path(checkout))
        if isinstance(roots, CargoOutputUnavailable):
            unavailable.append(f"{checkout}: {roots.reason}")
        else:
            mappings.append(roots)
    return sorted({path for roots in mappings
                   for path in (roots.target_directory, roots.build_directory)}), unavailable


def snapshot() -> MeasuredDiskSnapshot:
    previous_snapshot = read_snapshot()
    floor = read_floor()
    settings = conf.read("buildlog")
    walk_minutes = settings.whole("DISK_WALK_MINUTES")
    walk_hour = settings.whole("DISK_WALK_HOUR")
    if (previous_snapshot is not None and "filesystems" in previous_snapshot
            and "outside_build_caches" in previous_snapshot and "outside_build_cache_totals" in previous_snapshot
            and "previous_measured_at" in previous_snapshot):
        now = datetime.now(user_zone())
        request = store.read_disk_walk_request()
        requested = (not isinstance(request, store.DiskWalkRequestState)
                     and datetime.fromisoformat(request["requested_at"]) > datetime.fromisoformat(previous_snapshot["measured_at"]))
        if not requested and (now.timestamp() - datetime.fromisoformat(previous_snapshot["measured_at"]).timestamp() < walk_minutes * 60
                              or now.hour != walk_hour):
            usage = filesystem.filesystem_usage("/")
            capacities: list[FilesystemCapacity] = []
            for row in previous_snapshot["filesystems"]:
                try:
                    current = filesystem.filesystem_usage(row["path"])
                except OSError:
                    capacities.append(row)
                else:
                    capacities.append({**row, "used": current.used, "free": current.free})
            return {
                **previous_snapshot, "used": usage.used, "free": usage.free, "floor": floor,
                "filesystems": capacities, "capacity_measured_at": store.utc_iso(now.timestamp()),
                "outside_build_caches": previous_snapshot["outside_build_caches"],
                "outside_build_cache_totals": previous_snapshot["outside_build_cache_totals"],
                "previous_measured_at": previous_snapshot["previous_measured_at"],
            }
    previous = previous_outside_measurement(previous_snapshot)
    folders = list(FOLDERS)
    outputs, unavailable = mapped_output_paths(folders)
    for output in outputs:
        if not any(output.is_relative_to(Path(path).expanduser().resolve()) for _, path in folders):
            folders.append((str(output), str(output)))
    cache = os.environ.get("SCCACHE_DIR")
    if cache and not any(Path(cache).expanduser().is_relative_to(Path(path).expanduser()) for _, path in folders):
        folders.append(("sccache", cache))
    measured: list[tuple[int, filesystem.FilesystemUsage]] = []

    def root_usage() -> filesystem.FilesystemUsage:
        usage = filesystem.filesystem_usage("/")
        measured.append((Path("/").stat().st_dev, usage))
        return usage

    result = measure(folders, root_usage, floor, previous)
    result["filesystems"] = capacity_rows([Path("/"), *(Path(path) for _, path in folders), *outputs], tuple(measured))
    result["output_unavailable"] = unavailable
    return result


def write_snapshot(snapshot: DiskSnapshot | MeasuredDiskSnapshot) -> None:
    path = store.root() / store.DISK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    _ = partial.write_text(json.dumps(snapshot) + "\n")
    _ = partial.replace(path)


def read_snapshot() -> DiskSnapshot | None:
    try:
        return cast(DiskSnapshot, json.loads((store.root() / store.DISK_NAME).read_text()))
    except (OSError, ValueError):
        return None
