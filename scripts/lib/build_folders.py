#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Name temporary build folders and preserve their caller's lifetime."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import fcntl
import json
import os
import errno
import re
import secrets
import shutil
import signal
import stat
from pathlib import Path
import sys
import tempfile
import time
from typing import TYPE_CHECKING, Literal, cast, override

from scripts.lib.boot_identity import boot_identity

if TYPE_CHECKING:
    from scripts.lib.conf import ConfigRead

# Python's mkdtemp allocator uses this suffix.
MKDTEMP_SUFFIX_PATTERN = r"[a-z0-9_]{8}"
# Read old shell mktemp folders until 2026-12-03.
LEGACY_MKTEMP_SUFFIX_PATTERN = r"[A-Za-z0-9]{6}"


class FolderPrefixes(Mapping[str, str]):
    """Read legacy caller prefixes from the current configuration."""

    @override
    def __getitem__(self, key: str) -> str:
        return _settings().text(f"prefixes.{key}")

    @override
    def __iter__(self) -> Iterator[str]:
        return iter(key.removeprefix("prefixes.") for key in _settings().values() if key.startswith("prefixes."))

    @override
    def __len__(self) -> int:
        return len([key for key in _settings().values() if key.startswith("prefixes.")])


FOLDER_PREFIXES: Mapping[str, str] = FolderPrefixes()


@dataclass(frozen=True)
class InheritedTemporaryParent:
    """Use the caller's temporary parent."""


@dataclass(frozen=True)
class ExplicitBuildParent:
    path: Path


@dataclass(frozen=True)
class SystemTemporaryParent:
    """Use the short system temporary parent."""


@dataclass(frozen=True)
class UnitRunParent:
    """Use the current unit root authority."""


BuildFolderParent = InheritedTemporaryParent | ExplicitBuildParent | SystemTemporaryParent | UnitRunParent


@dataclass(frozen=True)
class UniqueBuildFolder:
    prefix: str
    parent: BuildFolderParent = InheritedTemporaryParent()


@dataclass(frozen=True)
class StableOwnerFolder:
    name: str
    parent: BuildFolderParent
    mode: int = 0o777


BuildFolderRequest = UniqueBuildFolder | StableOwnerFolder


def inherited_temporary_parent() -> Path:
    """Read the caller's current temporary parent."""
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def tmpfs_temporary_parent() -> Path:
    """Keep nested work under the caller's tmpfs folder."""
    root = Path(_settings().text("roots.gate"))
    inherited = inherited_temporary_parent().resolve()
    return inherited if inherited.is_relative_to(root) else root


def _parent(parent: BuildFolderParent) -> Path:
    if isinstance(parent, InheritedTemporaryParent):
        return inherited_temporary_parent()
    if isinstance(parent, SystemTemporaryParent):
        return Path(_settings().text("roots.system"))
    if isinstance(parent, UnitRunParent):
        from scripts.lib import unit_runs

        return unit_runs.run_roots()[0]
    return parent.path


def _check_name(name: str) -> None:
    if name in (".", "..") or "/" in name or "\x00" in name:
        raise ValueError("a build folder name must be one path component")


BuildFolderKind = Literal["temporary", "gate", "standing", "editable", "mirror", "target", "shell-cache",
                          "scratch", "legacy-unique", "legacy-stable"]


@dataclass(frozen=True, kw_only=True)
class BuildFolderSpec:
    kind: BuildFolderKind
    parent: Path
    suffix_length: int
    repository: str = ""
    owner: str = ""
    purpose: str = ""
    slot: int = 0
    suffix: str = ""
    showrunner: str = ""
    unit: str = ""
    work: str = ""


def _settings() -> ConfigRead:
    from scripts.lib import conf

    return conf.read_or_repository("build_folders")


def repository_key_for(name: str, identity: str) -> str:
    """Append the configured identity hash to a readable repository name."""
    from hashlib import sha256

    return f"{name}-{sha256(os.fsencode(identity)).hexdigest()[:_settings().whole('names.repository_key_length')]}"


def repository_key(repository: Path) -> str:
    """Distinguish repositories with equal basenames using their canonical paths."""
    canonical = repository.resolve()
    return repository_key_for(canonical.name, str(canonical))


def _new_suffix(length: int, settings: ConfigRead) -> str:
    if length <= 0:
        raise ValueError("a unique build folder needs a suffix")
    characters = settings.text("names.suffix_characters")
    if not characters or any(not (char.isascii() and (char.isalnum() or char == "_")) for char in characters):
        raise ValueError("suffix characters must be ASCII letters, digits, or underscores")
    return "".join(secrets.choice(characters) for _ in range(length))


def build_folder_path(spec: BuildFolderSpec) -> Path:
    """Apply the configured name rule without creating a folder."""
    settings = _settings()
    for component in (spec.repository, spec.owner, spec.purpose, spec.suffix, spec.showrunner, spec.unit, spec.work):
        _check_name(component)
    if spec.suffix_length < 0:
        raise ValueError("a suffix length cannot be negative")
    suffix = spec.suffix
    named = bool(spec.showrunner or spec.unit or spec.work)
    if named and not (spec.showrunner and spec.unit and spec.purpose):
        raise ValueError("a named build folder needs showrunner, unit, and purpose")
    if not suffix and ((named and not spec.work) or
                       (not named and spec.kind in ("temporary", "gate", "scratch", "legacy-unique"))):
        suffix = _new_suffix(spec.suffix_length, settings)
    pattern = settings.text("patterns.named" if named else f"patterns.{spec.kind}")
    for field, value in (("repository", spec.repository), ("owner", spec.owner), ("purpose", spec.purpose)):
        if "{" + field + "}" in pattern and not value and spec.kind != "legacy-unique":
            raise ValueError(f"{spec.kind} needs {field}")
    relative = pattern.format(
        repository=spec.repository, owner=spec.owner, purpose=spec.purpose, suffix=suffix, slot=spec.slot,
        showrunner=spec.showrunner, unit=spec.unit, work=spec.work or suffix)
    if not relative or Path(relative).is_absolute() or any(part in (".", "..") for part in relative.split("/")):
        raise ValueError("a build folder pattern must stay below its parent")
    path = spec.parent / relative
    if not path.is_relative_to(spec.parent) or path == spec.parent:
        raise ValueError("a build folder must stay below its parent")
    return path


@contextmanager
def _folder_parent(path: Path, root: Path, *, create: bool) -> Generator[int]:
    """Open each parent below the root without following directory links."""
    canonical = root.resolve()
    if create:
        canonical.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(canonical, flags)
    try:
        for component in path.parent.relative_to(root).parts:
            if create:
                try:
                    os.mkdir(component, dir_fd=directory)
                except FileExistsError:
                    pass
            child = os.open(component, flags, dir_fd=directory)
            previous = directory
            directory = child
            os.close(previous)
        yield directory
    finally:
        os.close(directory)


_locks: dict[int, Path] = {}

def _open_lock(path: Path, directory: int, *, create: bool) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT
    descriptor = os.open(path.name + ".lock", flags, 0o600, dir_fd=directory)
    try:
        inode = os.fstat(descriptor)
        if not stat.S_ISREG(inode.st_mode) or inode.st_nlink != 1:
            raise ValueError("a build folder lock must be a regular file with one link")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _take_lock(descriptor: int, mode: int) -> bool:
    settings = _settings()
    deadline = time.monotonic() + settings.number("locks.probe_wait_seconds")
    retry = settings.number("locks.probe_retry_seconds")
    while True:
        try:
            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(retry, remaining))


def _lock_current(path: Path, directory: int, descriptor: int) -> bool:
    try:
        return os.path.samestat(os.fstat(descriptor), os.stat(path.name + ".lock", dir_fd=directory,
                                                            follow_symlinks=False))
    except FileNotFoundError:
        return False


def build_folder_state(path: Path) -> Literal["held", "free"]:
    """Check the lock without using the lease record or session state."""
    try:
        with _folder_parent(path, path.parent, create=False) as directory:
            while True:
                descriptor = _open_lock(path, directory, create=False)
                try:
                    if not _take_lock(descriptor, fcntl.LOCK_EX):
                        if _lock_current(path, directory, descriptor):
                            return "held"
                        continue
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    if _lock_current(path, directory, descriptor):
                        return "free"
                finally:
                    os.close(descriptor)
    except FileNotFoundError:
        return "free"


def build_folder_lock_fd(path: Path) -> int:
    """Return the held descriptor for subprocess pass_fds."""
    return next(descriptor for descriptor, candidate in reversed(_locks.items()) if candidate == path)


def _release_build_folder(path: Path, *, descriptor: int | None = None) -> None:
    if descriptor is None:
        try:
            descriptor = build_folder_lock_fd(path)
        except StopIteration:
            return
    if _locks.get(descriptor) != path:
        return
    try:
        _ = _locks.pop(descriptor, None)
    finally:
        # Closing preserves a child's inherited hold on the same lock.
        os.close(descriptor)


def _write_lease(path: Path, directory: int, spec: BuildFolderSpec) -> None:
    record = {"showrunner": spec.showrunner, "unit": spec.unit or spec.owner,
              "work": spec.work, "purpose": spec.purpose, "pid": os.getpid(),
              "start_time": datetime.now(timezone.utc).isoformat()}
    name = path.name + ".lease.json"
    try:
        existing = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(existing.st_mode):
            raise OSError(errno.ELOOP, "a build folder lease cannot be a symbolic link", name)
        if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
            raise ValueError("a build folder lease must be a regular file with one link")
    text = json.dumps(record) + "\n"
    temporary = name + "." + secrets.token_hex(8) + ".tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
    try:
        with os.fdopen(descriptor, "w") as output:
            _ = output.write(text)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class _TemporaryFolderRequest:
    request: UniqueBuildFolder | BuildFolderSpec
    cleanup: ExitStack


def _request_spec(request: BuildFolderRequest | BuildFolderSpec | _TemporaryFolderRequest) -> BuildFolderSpec:
    if isinstance(request, _TemporaryFolderRequest):
        return _request_spec(request.request)
    if isinstance(request, BuildFolderSpec):
        return request
    else:
        stable = isinstance(request, StableOwnerFolder)
        name = request.name if isinstance(request, StableOwnerFolder) else request.prefix
        _check_name(name)
        if stable and not name:
            raise ValueError("a stable build folder needs an owner name")
        spec = BuildFolderSpec(kind="legacy-stable" if stable else "legacy-unique", parent=_parent(request.parent),
                               purpose=name, suffix_length=_settings().whole("names.suffix_length"))
        return spec


def _stable_folder(spec: BuildFolderSpec) -> bool:
    return spec.kind in ("standing", "editable", "mirror", "target", "shell-cache", "legacy-stable")


def create_build_folder(request: BuildFolderRequest | BuildFolderSpec | _TemporaryFolderRequest) -> Path:
    """Allocate a unique folder or retain an owner's folder."""
    original = request.request if isinstance(request, _TemporaryFolderRequest) else request
    spec = _request_spec(original)
    stable = _stable_folder(spec)
    descriptor = -1
    try:
        with ExitStack() as allocation_scope:
            parents = request.cleanup if isinstance(request, _TemporaryFolderRequest) else allocation_scope
            directories: dict[Path, int] = {}
            while True:
                allocation = (replace(spec, work=_new_suffix(spec.suffix_length, _settings()))
                              if (spec.showrunner or spec.unit) and not spec.work else spec)
                path = build_folder_path(allocation)
                directory = directories.get(path.parent)
                if directory is None:
                    directory = parents.enter_context(_folder_parent(path, spec.parent,
                        create=stable or not isinstance(original, UniqueBuildFolder)))
                    directories[path.parent] = directory
                descriptor = _open_lock(path, directory, create=True)
                made_inode: os.stat_result | None = None
                new_folder = False
                try:
                    if not _take_lock(descriptor, fcntl.LOCK_SH if stable else fcntl.LOCK_EX):
                        if not stable and not (spec.suffix or spec.work):
                            continue
                        raise BlockingIOError(errno.EWOULDBLOCK, "build folder is held", str(path))
                    if not _lock_current(path, directory, descriptor):
                        continue
                    try:
                        existing = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        existing = None
                    if existing is not None:
                        if not stable:
                            if spec.suffix or spec.work:
                                raise FileExistsError(errno.EEXIST, "build folder already exists", str(path))
                            continue
                        if not stat.S_ISDIR(existing.st_mode):
                            raise FileExistsError(errno.EEXIST, "build folder is not a directory", str(path))
                    new_folder = existing is None
                    _write_lease(path, directory, allocation)
                    if existing is None:
                        mode = request.mode if isinstance(request, StableOwnerFolder) else 0o777 if stable else 0o700
                        try:
                            os.mkdir(path.name, mode=mode, dir_fd=directory)
                            made_inode = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                        except FileExistsError:
                            if not stable:
                                raise
                            if not stat.S_ISDIR(os.stat(path.name, dir_fd=directory, follow_symlinks=False).st_mode):
                                raise
                    allocated = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                    os.set_inheritable(descriptor, True)
                    _locks[descriptor] = path
                    if isinstance(request, _TemporaryFolderRequest):
                        _ = request.cleanup.callback(_finish_temporary, path, directory, allocated,
                                                     descriptor=descriptor, temporary=not stable)
                    return path
                except BaseException:
                    _ = _locks.pop(descriptor, None)
                    if (made_inode is not None or (new_folder and not stable)) and _take_lock(descriptor, fcntl.LOCK_EX):
                        _remove_failed_allocation(path, directory, made_inode)
                    raise
                finally:
                    if descriptor not in _locks:
                        closing = descriptor
                        descriptor = -1
                        os.close(closing)
    except BaseException:
        if descriptor >= 0:
            _release_build_folder(_locks[descriptor], descriptor=descriptor)
        raise


def _remove_failed_allocation(path: Path, directory: int, allocated: os.stat_result | None) -> None:
    if allocated is not None:
        _remove_folder(path, directory, allocated)
    if not _folder_exists(path, directory):
        _unlink_metadata(path, directory)


def _remove_folder(path: Path, directory: int, allocated: os.stat_result) -> None:
    def repair(function: object, name: str, error: BaseException) -> None:
        if not isinstance(error, PermissionError):
            raise error
        os.chmod(Path(name).parent, 0o700, dir_fd=directory)
        if not stat.S_ISLNK(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode):
            os.chmod(name, 0o700, dir_fd=directory, follow_symlinks=False)
        if function is os.open:
            shutil.rmtree(name, dir_fd=directory, onexc=repair)
        else:
            _ = cast(Callable[..., object], function)(name, dir_fd=directory)

    try:
        current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    if os.path.samestat(current, allocated):
        shutil.rmtree(path.name, dir_fd=directory, onexc=repair)


def _folder_exists(path: Path, directory: int) -> bool:
    try:
        _ = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _unlink_metadata(path: Path, directory: int) -> None:
    temporary = re.compile(re.escape(path.name) + r"\.lease\.json\.[0-9a-f]{16}\.tmp")
    for name in os.listdir(directory):
        if temporary.fullmatch(name):
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
    for suffix in (".lease.json", ".lock"):
        try:
            os.unlink(path.name + suffix, dir_fd=directory)
        except FileNotFoundError:
            pass


@contextmanager
def collector_lease(path: Path) -> Generator[bool]:
    """Hold a managed folder's lease lock while a collector judges and removes it.

    Yield False when an owner holds the lease. A folder without a lock file has no lease.
    """
    with ExitStack() as stack:
        try:
            directory = stack.enter_context(_folder_parent(path, path.parent, create=False))
            descriptor = _open_lock(path, directory, create=False)
        except FileNotFoundError:
            free = True
        except (OSError, ValueError):
            free = False
        else:
            _ = stack.callback(os.close, descriptor)
            free = _take_lock(descriptor, fcntl.LOCK_EX) and _lock_current(path, directory, descriptor)
        yield free


def remove_build_folder(path: Path, cleanup: Callable[[], object], *, temporary: bool = True,
                        dir_fd: int | None = None) -> bool:
    """Run cleanup under the free folder's lock, then remove temporary metadata."""
    with ExitStack() as parents:
        directory = dir_fd if dir_fd is not None else parents.enter_context(
            _folder_parent(path, path.parent, create=False))
        descriptor = _open_lock(path, directory, create=True)
        try:
            if not _take_lock(descriptor, fcntl.LOCK_EX):
                return False
            if not _lock_current(path, directory, descriptor):
                return False
            if cleanup() is False:
                return False
            if _folder_exists(path, directory):
                return False
            if temporary:
                _unlink_metadata(path, directory)
            return True
        finally:
            os.close(descriptor)


def _finish_temporary(path: Path, directory: int, allocated: os.stat_result, *,
                      descriptor: int, temporary: bool) -> None:
    if _locks.get(descriptor) != path:
        return
    _release_build_folder(path, descriptor=descriptor)
    _ = remove_build_folder(path, lambda: _remove_folder(path, directory, allocated),
                            temporary=temporary, dir_fd=directory)


def sweep_temporary_build_folders(parent: Path) -> None:
    """Remove free allocations in a parent reserved for temporary maker folders."""
    try:
        with _folder_parent(parent / "entry", parent, create=False) as directory:
            names: set[str] = set()
            for entry in os.listdir(directory):
                match = re.fullmatch(r"(.+)\.(?:lock|lease\.json(?:\.[0-9a-f]{16}\.tmp)?)", entry)
                if match is not None:
                    names.add(match[1])
            for name in sorted(names):
                path = parent / name

                def cleanup() -> None:
                    try:
                        allocated = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        return
                    _remove_folder(path, directory, allocated)

                _ = remove_build_folder(path, cleanup, dir_fd=directory)
    except FileNotFoundError:
        pass


@contextmanager
def temporary_build_folder(request: UniqueBuildFolder | BuildFolderSpec) -> Generator[Path]:
    """Remove private work after the caller ends, including interrupted callers."""
    with ExitStack() as cleanup:
        yield create_build_folder(_TemporaryFolderRequest(request, cleanup))


OWNER_MARKER = ".build-folder-owner.json"


def run_build(owner: str, name: str, command: Sequence[str], *, showrunner: str = "", unit: str = "",
              work: str = "") -> int:
    """Keep the folder until the command and its descendants stop."""
    import json
    import subprocess
    from scripts.lib.descendants import adopt_descendants

    if not command:
        raise ValueError("run needs a command after --")
    if sys.platform != "linux":
        raise ValueError("run requires Linux descendant adoption")
    _ = adopt_descendants()
    settings = _settings()
    root = Path(os.path.expandvars(settings.text("roots.temporary").replace("${TMPDIR}", str(inherited_temporary_parent())))).expanduser().resolve()
    spec = BuildFolderSpec(kind="temporary", parent=root, repository=repository_key(Path.cwd()),
                           owner=owner, purpose=name, suffix_length=settings.whole("names.suffix_length"),
                           showrunner=showrunner, unit=unit, work=work)

    with temporary_build_folder(spec) as path:
        pid = os.getpid()
        start_time = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        marker = {"owner": owner, "pid": pid, "start_time": start_time,
                  "boot_id": boot_identity()}
        _ = (path / OWNER_MARKER).write_text(json.dumps(marker) + "\n")
        descriptor = build_folder_lock_fd(path)
        child = subprocess.Popen(command, cwd=path,
                                 env={**os.environ, "BUILD_FOLDER": str(path), "BUILD_FOLDER_LOCK_FD": str(descriptor)},
                                 pass_fds=(descriptor,), process_group=os.getpgrp())
        try:
            status = child.wait()
        finally:
            try:
                child.kill()
            except ProcessLookupError:
                pass
            _ = child.wait()
            while True:
                children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
                for descendant in children:
                    try:
                        os.kill(int(descendant), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    _ = os.waitpid(-1, 0)
                except ChildProcessError:
                    break
        return status if status >= 0 else 128 - status


def main(argv: Sequence[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    unique = commands.add_parser("unique")
    prefix = unique.add_mutually_exclusive_group(required=True)
    _ = prefix.add_argument("--prefix")
    _ = prefix.add_argument("--kind", choices=tuple(FOLDER_PREFIXES))
    _ = unique.add_argument("--owner", default="")
    unique_parent = unique.add_mutually_exclusive_group()
    _ = unique_parent.add_argument("--parent", type=Path)
    _ = unique_parent.add_argument("--system-temp", action="store_true")
    stable = commands.add_parser("stable")
    stable_destination = stable.add_mutually_exclusive_group()
    _ = stable_destination.add_argument("--path", type=Path)
    _ = stable_destination.add_argument("--run-temp", type=Path)
    _ = stable.add_argument("--name", default="")
    stable_parent = stable.add_mutually_exclusive_group()
    _ = stable_parent.add_argument("--parent", type=Path)
    _ = stable_parent.add_argument("--unit-root", action="store_true")
    remove = commands.add_parser("remove")
    _ = remove.add_argument("path", type=Path)
    run_temp = commands.add_parser("run-temp-path")
    _ = run_temp.add_argument("run", type=Path)
    run = commands.add_parser("run")
    _ = run.add_argument("--owner", default="")
    _ = run.add_argument("--showrunner", default="")
    _ = run.add_argument("--unit", default="")
    _ = run.add_argument("--work", default="")
    _ = run.add_argument("--name", required=True)
    _ = run.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    request: BuildFolderRequest
    command = cast(str, args.command)
    try:
        if command == "run":
            arguments = cast(list[str], args.argv)
            owner = cast(str, args.owner) or cast(str, args.unit)
            if not owner:
                raise ValueError("run needs --owner or --unit")
            return run_build(owner, cast(str, args.name), arguments[1:] if arguments[:1] == ["--"] else arguments,
                             showrunner=cast(str, args.showrunner), unit=cast(str, args.unit), work=cast(str, args.work))
        if command == "remove":
            path = cast(Path, args.path)
            _release_build_folder(path)
            def cleanup() -> None:
                try:
                    shutil.rmtree(path)
                except FileNotFoundError:
                    pass
            return 0 if remove_build_folder(path, cleanup) else 1
        if command == "run-temp-path":
            from scripts.lib import unit_runs

            print(unit_runs.run_temp_path(cast(Path, args.run)))
            return 0
        if command == "stable":
            path = cast(Path | None, args.path)
            run = cast(Path | None, args.run_temp)
            if run is not None:
                from scripts.lib import unit_runs

                path = unit_runs.run_temp_path(run)
            if path is not None:
                request = StableOwnerFolder(path.name, ExplicitBuildParent(path.parent))
            else:
                parent: BuildFolderParent = UnitRunParent() if cast(bool, args.unit_root) else ExplicitBuildParent(cast(Path, args.parent))
                if not cast(bool, args.unit_root) and cast(Path | None, args.parent) is None:
                    raise ValueError("a stable folder needs --path, --run-temp, or a parent and name")
                request = StableOwnerFolder(cast(str, args.name), parent)
        else:
            unique_choice: BuildFolderParent = InheritedTemporaryParent()
            explicit_parent = cast(Path | None, args.parent)
            if cast(bool, args.system_temp):
                unique_choice = SystemTemporaryParent()
            elif explicit_parent is not None:
                unique_choice = ExplicitBuildParent(explicit_parent)
            kind = cast(str | None, args.kind)
            name = FOLDER_PREFIXES[kind].format(owner=cast(str, args.owner)) if kind else cast(str, args.prefix)
            request = UniqueBuildFolder(name, unique_choice)
        print(create_build_folder(request))
    except (OSError, ValueError) as error:
        print(f"build folders: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
