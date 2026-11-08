#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Name temporary build folders and preserve their caller's lifetime."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import os
import secrets
import shutil
import signal
import stat
from pathlib import Path
import sys
import tempfile
import threading
from typing import TYPE_CHECKING, Literal, cast, override

from scripts.lib.boot_identity import boot_identity

if TYPE_CHECKING:
    from scripts.lib.conf import ConfigRead

# Python's mkdtemp allocator uses this suffix.
MKDTEMP_SUFFIX_PATTERN = r"[a-z0-9_]{8}"
# Read old shell mktemp folders until 2026-12-03.
LEGACY_MKTEMP_SUFFIX_PATTERN = r"[A-Za-z0-9]{6}"
_CANCELLATION_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


@contextmanager
def defer_signals() -> Generator[None]:
    """Record cancellation and replay its first signal after handlers are restored."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    pending: list[int] = []

    def record(number: int, frame: object) -> None:
        del frame
        pending.append(number)

    previous = {number: signal.signal(number, record) for number in _CANCELLATION_SIGNALS}
    try:
        yield
    finally:
        for number, handler in previous.items():
            _ = signal.signal(number, handler)
        if pending:
            signal.raise_signal(pending[0])


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


def build_folder_path(spec: BuildFolderSpec) -> Path:
    """Apply the configured name rule without creating a folder."""
    settings = _settings()
    for component in (spec.repository, spec.owner, spec.purpose, spec.suffix):
        _check_name(component)
    if spec.suffix_length < 0:
        raise ValueError("a suffix length cannot be negative")
    suffix = spec.suffix
    if not suffix and spec.kind in ("temporary", "gate", "scratch", "legacy-unique"):
        if not spec.suffix_length:
            raise ValueError("a unique build folder needs a suffix")
        characters = settings.text("names.suffix_characters")
        if not characters or any(not (char.isascii() and (char.isalnum() or char == "_")) for char in characters):
            raise ValueError("suffix characters must be ASCII letters, digits, or underscores")
        suffix = "".join(secrets.choice(characters) for _ in range(spec.suffix_length))
    pattern = settings.text(f"patterns.{spec.kind}")
    for field, value in (("repository", spec.repository), ("owner", spec.owner), ("purpose", spec.purpose)):
        if "{" + field + "}" in pattern and not value and spec.kind != "legacy-unique":
            raise ValueError(f"{spec.kind} needs {field}")
    relative = pattern.format(
        repository=spec.repository, owner=spec.owner, purpose=spec.purpose, suffix=suffix, slot=spec.slot)
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
            os.close(directory)
            directory = child
        yield directory
    finally:
        os.close(directory)


def create_build_folder(request: BuildFolderRequest | BuildFolderSpec) -> Path:
    """Allocate a unique folder or retain an owner's folder."""
    stable = isinstance(request, StableOwnerFolder)
    if isinstance(request, BuildFolderSpec):
        spec = request
        stable = spec.kind in ("standing", "editable", "mirror", "target", "shell-cache", "legacy-stable")
    else:
        name = request.name if isinstance(request, StableOwnerFolder) else request.prefix
        _check_name(name)
        if stable and not name:
            raise ValueError("a stable build folder needs an owner name")
        spec = BuildFolderSpec(kind="legacy-stable" if stable else "legacy-unique", parent=_parent(request.parent),
                               purpose=name, suffix_length=_settings().whole("names.suffix_length"))
    while True:
        path = build_folder_path(spec)
        with _folder_parent(path, spec.parent, create=stable or isinstance(request, BuildFolderSpec)) as directory:
            mode = request.mode if isinstance(request, StableOwnerFolder) else 0o777 if stable else 0o700
            try:
                os.mkdir(path.name, mode=mode, dir_fd=directory)
            except FileExistsError:
                if stable:
                    if not stat.S_ISDIR(os.stat(path.name, dir_fd=directory, follow_symlinks=False).st_mode):
                        raise
                elif spec.suffix:
                    raise
                else:
                    continue
            return path


def _remove_folder(path: Path, directory: int, allocated: os.stat_result) -> None:
    with defer_signals():
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


@contextmanager
def temporary_build_folder(request: UniqueBuildFolder | BuildFolderSpec) -> Generator[Path]:
    """Remove private work after the caller ends, including interrupted callers."""
    with ExitStack() as cleanup:
        with defer_signals():
            path = create_build_folder(request)
            root = request.parent if isinstance(request, BuildFolderSpec) else _parent(request.parent)
            directory = cleanup.enter_context(_folder_parent(path, root, create=False))
            allocated = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            _ = cleanup.callback(_remove_folder, path, directory, allocated)
        yield path


OWNER_MARKER = ".build-folder-owner.json"


def run_build(owner: str, name: str, command: Sequence[str]) -> int:
    """Keep the folder until the command and its descendants stop."""
    import json
    import subprocess
    from scripts.lib.descendants import adopt_descendants
    from scripts.lib.process_group import send_process_group

    if not command:
        raise ValueError("run needs a command after --")
    if sys.platform != "linux":
        raise ValueError("run requires Linux descendant adoption")
    _ = adopt_descendants()
    settings = _settings()
    root = Path(os.path.expandvars(settings.text("roots.temporary").replace("${TMPDIR}", str(inherited_temporary_parent())))).expanduser().resolve()
    spec = BuildFolderSpec(kind="temporary", parent=root, repository=repository_key(Path.cwd()),
                           owner=owner, purpose=name, suffix_length=settings.whole("names.suffix_length"))

    class CommandSignal(BaseException):
        def __init__(self, number: int) -> None:
            super().__init__(number)
            self.number: int = number

    def interrupted(number: int, frame: object) -> None:
        del frame
        raise CommandSignal(number)

    watched = _CANCELLATION_SIGNALS
    previous = {number: signal.signal(number, interrupted) for number in watched}
    try:
        with temporary_build_folder(spec) as path:
            pid = os.getpid()
            start_time = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            marker = {"owner": owner, "pid": pid, "start_time": start_time,
                      "boot_id": boot_identity()}
            _ = (path / OWNER_MARKER).write_text(json.dumps(marker) + "\n")
            blocked = signal.pthread_sigmask(signal.SIG_BLOCK, watched)

            def child_signals() -> None:
                _ = signal.pthread_sigmask(signal.SIG_SETMASK, blocked)

            try:
                child = subprocess.Popen(command, cwd=path, env={**os.environ, "BUILD_FOLDER": str(path)},
                                         start_new_session=True, preexec_fn=child_signals)
            except BaseException:
                _ = signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
                raise
            try:
                _ = signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
                status = child.wait()
            except CommandSignal as error:
                status = -error.number
            finally:
                for number in watched:
                    _ = signal.signal(number, signal.SIG_IGN)
                try:
                    send_process_group(child.pid, signal.SIGKILL)
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
    except CommandSignal as error:
        return 128 + error.number
    finally:
        for number, handler in previous.items():
            _ = signal.signal(number, handler)


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
    run_temp = commands.add_parser("run-temp-path")
    _ = run_temp.add_argument("run", type=Path)
    run = commands.add_parser("run")
    _ = run.add_argument("--owner", required=True)
    _ = run.add_argument("--name", required=True)
    _ = run.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    request: BuildFolderRequest
    command = cast(str, args.command)
    try:
        if command == "run":
            arguments = cast(list[str], args.argv)
            return run_build(cast(str, args.owner), cast(str, args.name),
                             arguments[1:] if arguments[:1] == ["--"] else arguments)
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
