"""Read immutable candidate inputs without a repository export."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path, PurePosixPath
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from scripts.style import candidate_generators as generators
from scripts.style.git_process import GitReadFailed, TreeEntry, blob_contents, run_git

@dataclass(frozen=True)
class UnstoredFact:
    """The immutable input has no completed fact for this reader."""


@dataclass(frozen=True)
class UnstoredTree:
    """The tree has no completed entry set for this reader."""


class ImmutableCandidateFacts(Protocol):
    """Supply verified source facts without coupling Git reads to SQLite."""

    failed_reads: int
    failed_blobs: set[str]

    def fact(self, object_id: str, kind: str, key: str = "") -> str | UnstoredFact: ...
    def store_fact(self, object_id: str, kind: str, key: str, value: str) -> None: ...
    def tree(self, object_id: str) -> dict[str, TreeEntry] | UnstoredTree: ...
    def store_tree(self, object_id: str, entries: Mapping[str, TreeEntry]) -> None: ...
    def masked(self, blob: str, text: Callable[[], str]) -> generators.MaskedSource: ...


def input_identity(value: object) -> str:
    """Name exact inputs independently of a mutable revision role."""
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


class RegexConfigState(Enum):
    """Distinguish absent, missing, and readable ripgrep configuration files."""

    UNSET = "unset"
    MISSING = "missing"
    PRESENT = "present"


@dataclass(frozen=True)
class RegexConfiguration:
    """Describe the current ripgrep configuration and its ignore files."""

    path: str
    state: RegexConfigState
    content: bytes
    ignore_files: tuple[str, ...]


def regex_configuration() -> RegexConfiguration:
    """Read the external ripgrep configuration at the current observation."""
    path = os.environ.get("RIPGREP_CONFIG_PATH", "")
    if not path:
        return RegexConfiguration(path, RegexConfigState.UNSET, b"", ())
    try:
        content = Path(path).read_bytes()
    except FileNotFoundError:
        return RegexConfiguration(path, RegexConfigState.MISSING, b"", ())
    arguments = tuple(line for line in content.decode().splitlines() if line and not line.startswith("#"))
    ignores: list[str] = []
    for index, argument in enumerate(arguments):
        if argument == "--ignore-file" and index + 1 < len(arguments):
            ignores.append(arguments[index + 1])
        elif argument.startswith("--ignore-file="):
            ignores.append(argument.removeprefix("--ignore-file="))
    return RegexConfiguration(path, RegexConfigState.PRESENT, content, tuple(ignores))


def regex_tool_identity() -> str:
    """Hash ripgrep and its current external configuration."""
    executable = shutil.which("rg") or "rg"
    tool = Path(executable).read_bytes()
    config = regex_configuration()
    external = tuple((path, hashlib.sha256(Path(path).read_bytes()).hexdigest())
                     for path in config.ignore_files if Path(path).is_absolute())
    return input_identity((executable, hashlib.sha256(tool).hexdigest(), config.path, config.state.value,
                           hashlib.sha256(config.content).hexdigest(), external))


def source_text(content: bytes) -> str:
    """Match the decoding and newline conversion of Path.read_text."""
    return content.decode().replace("\r\n", "\n").replace("\r", "\n")


class RefillInputs:
    """Share a verified Git tree and one current source blob among candidate partitions."""

    root: Path = Path("/export")

    def __init__(self, repo: Path, tree: str, facts: ImmutableCandidateFacts) -> None:
        self.repo: Path = repo
        self.tree: str = tree
        self.facts: ImmutableCandidateFacts = facts
        self.blobs: dict[str, str] = {}
        self.entries: dict[str, TreeEntry] = {}
        self.trees: set[str] = set()
        self.manifests: dict[str, dict[str, object]] = {}
        self._walk(tree, "")
        self._verify()
        self.directories: frozenset[str] = frozenset(
            parent.as_posix() for path in self.blobs for parent in PurePosixPath(path).parents
        )

    def _walk(self, tree: str, prefix: str) -> None:
        pending: list[tuple[str, str]] = [(tree, prefix)]
        while pending:
            current = pending
            pending = []
            nodes: dict[str, dict[str, TreeEntry]] = {}
            unread: dict[str, str] = {}
            for object_id, _ in current:
                stored = self.facts.tree(object_id)
                if isinstance(stored, UnstoredTree):
                    unread[object_id] = object_id
                else:
                    nodes[object_id] = stored
            if unread:
                nodes.update(self.read_trees(tuple(unread)))
                for object_id in unread:
                    self.facts.store_tree(object_id, nodes[object_id])
            for object_id, directory in current:
                self.trees.add(object_id)
                for name, entry in nodes[object_id].items():
                    path = directory + name
                    if entry.kind == "tree":
                        pending.append((entry.object, path + "/"))
                    else:
                        self.entries[path] = entry
                        if entry.kind == "blob":
                            self.blobs[path] = entry.object

    def read_trees(self, objects: tuple[str, ...]) -> dict[str, dict[str, TreeEntry]]:
        """Stream changed tree objects through the existing Git batch transport."""
        command = ("git", "-C", str(self.repo), "cat-file", "--batch")
        result: dict[str, dict[str, TreeEntry]] = {}
        with tempfile.TemporaryFile() as output:
            _ = run_git(command, input="".join(object_id + "\n" for object_id in objects).encode(), output=output)
            _ = output.seek(0)
            for object_id in objects:
                header = output.readline().split()
                if len(header) != 3 or header[:2] != [object_id.encode(), b"tree"] or not header[2].isdigit():
                    raise GitReadFailed(command, 0, "invalid candidate tree response")
                content = output.read(int(header[2]))
                if len(content) != int(header[2]) or output.read(1) != b"\n":
                    raise GitReadFailed(command, 0, "incomplete candidate tree response")
                entries: dict[str, TreeEntry] = {}
                offset = 0
                while offset < len(content):
                    space = content.index(b" ", offset)
                    end = content.index(b"\0", space)
                    mode = content[offset:space].decode().zfill(6)
                    name = content[space + 1:end].decode(errors="replace")
                    size = len(object_id) // 2
                    value = content[end + 1:end + 1 + size].hex()
                    kind = "tree" if mode == "040000" else "commit" if mode == "160000" else "blob"
                    entries[name] = TreeEntry(mode, kind, value)
                    offset = end + 1 + size
                result[object_id] = entries
        return result

    def _verify(self) -> None:
        required = {**dict.fromkeys(self.trees, "tree"), **dict.fromkeys(self.blobs.values(), "blob")}
        command = ("git", "-C", str(self.repo), "cat-file", "--batch-check=%(objectname) %(objecttype)")
        result = run_git(command, input="".join(name + "\n" for name in required).encode())
        replies = result.stdout.decode(errors="replace").splitlines()
        expected = [name + " " + kind for name, kind in required.items()]
        if replies != expected:
            raise GitReadFailed(command, 0, "candidate input object is unavailable")

    def view(self, paths: tuple[str, ...]) -> IndexedCandidateInputs:
        return IndexedCandidateInputs(self, frozenset(paths))

    def rust_paths(self, root: Path) -> tuple[str, ...]:
        prefix = root.relative_to(self.root).as_posix()
        prefix = "" if prefix == "." else prefix + "/"
        return tuple(path for path in sorted(self.blobs)
                     if path.startswith(prefix) and path.endswith(".rs")
                     and "target" not in PurePosixPath(path[len(prefix):]).parts
                     and not any(part.startswith(".") for part in PurePosixPath(path[len(prefix):]).parts))

    def prepare_text(self, paths: tuple[str, ...]) -> None:
        unique = {self.blobs[path]: path for path in paths}
        unread = tuple(path for blob, path in unique.items()
                       if isinstance(self.facts.fact(blob, "source_text"), UnstoredFact))
        for path, content in self.sources(unread):
            try:
                text = source_text(content)
            except UnicodeDecodeError:
                self.facts.failed_blobs.add(self.blobs[path])
                continue
            self.facts.store_fact(self.blobs[path], "source_text", "", text)

    def read(self, path: str) -> bytes:
        blob = self.blobs.get(path, "")
        if not blob:
            raise FileNotFoundError(path)
        return next(blob_contents(self.repo, {path: blob}))[1]

    def sources(self, paths: tuple[str, ...]) -> Iterator[tuple[str, bytes]]:
        if paths:
            yield from blob_contents(self.repo, {path: self.blobs[path] for path in paths})

    def regex_identity(self, spec: generators.CandidatesSpec) -> tuple[str, tuple[tuple[str, str], ...]]:
        configured = regex_configuration().ignore_files
        configured_paths = {Path(path).as_posix() for path in configured if not Path(path).is_absolute()}
        ignores = tuple((path, blob) for path, blob in sorted(self.blobs.items())
                        if PurePosixPath(path).name in {".ignore", ".gitignore", ".rgignore"} or path in configured_paths)
        # Explicit file arguments can change ripgrep's filename prefix when membership changes.
        explicit = tuple((path, path in self.blobs, path in self.directories) for path in spec.paths)
        return input_identity((ignores, regex_tool_identity(), explicit)), ignores

    def regex(self, spec: generators.CandidatesSpec, paths: tuple[str, ...]) -> generators.Enumeration:
        """Give ripgrep its path topology, ignore files, and changed file contents."""
        with tempfile.TemporaryDirectory(prefix="style-regex-") as temporary:
            root = Path(temporary)
            for path in self.blobs:
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
            _, ignores = self.regex_identity(spec)
            for path, content in self.sources(tuple(path for path, _ in ignores)):
                _ = (root / path).write_bytes(content)
            args = ["rg", "--files", "--null", "--glob", "!target/**"]
            if spec.globs:
                for glob in spec.globs:
                    args += ["--glob", glob]
            else:
                args += ["--type", "rust"]
            args += list(spec.paths) if spec.paths else ["."]
            selected = subprocess.run(args, cwd=root, capture_output=True, check=False)
            if selected.returncode not in (0, 1):
                raise SystemExit(f"candidates regex for {spec.origin} failed (rg exit {selected.returncode}): "
                                 + selected.stderr.decode(errors="replace").strip())
            names = {name.removeprefix("./") for name in selected.stdout.decode().split("\0") if name}
            for path, content in self.sources(tuple(path for path in paths if path in names)):
                _ = (root / path).write_bytes(content)
            return generators.enumerate_candidates(spec, root)


class IndexedCandidateInputs:
    """Limit enumeration to one partition while retaining its complete dependency domain."""

    def __init__(self, source: RefillInputs, paths: frozenset[str]) -> None:
        self.source: RefillInputs = source
        self.paths: frozenset[str] = paths

    def relative(self, path: Path) -> str:
        return path.relative_to(self.source.root).as_posix()

    def includes(self, path: Path) -> bool:
        return self.relative(path) in self.paths

    def rust_files(self, root: Path) -> list[Path]:
        prefix = self.relative(root)
        prefix = "" if prefix == "." else prefix + "/"
        return [self.source.root / path for path in sorted(self.paths)
                if path.startswith(prefix) and path.endswith(".rs")
                and "target" not in PurePosixPath(path[len(prefix):]).parts
                and not any(part.startswith(".") for part in PurePosixPath(path[len(prefix):]).parts)]

    def text(self, path: Path) -> str:
        relative = self.relative(path)
        blob = self.source.blobs.get(relative, "")
        stored = self.source.facts.fact(blob, "source_text")
        if isinstance(stored, UnstoredFact):
            try:
                stored = source_text(self.source.read(relative))
            except (OSError, UnicodeDecodeError):
                self.source.facts.failed_reads += 1
                self.source.facts.failed_blobs.add(blob)
                raise
            self.source.facts.store_fact(blob, "source_text", "", stored)
        return stored

    def masked(self, path: Path) -> generators.MaskedSource:
        relative = self.relative(path)
        return self.source.facts.masked(self.source.blobs[relative], lambda: self.text(path))

    def manifest(self, path: Path) -> dict[str, object]:
        relative = self.relative(path)
        blob = self.source.blobs.get(relative, "")
        if not blob:
            raise FileNotFoundError(relative)
        if blob not in self.source.manifests:
            self.source.manifests[blob] = tomllib.loads(self.source.read(relative).decode("utf-8"))
        return self.source.manifests[blob]

    def exists(self, path: Path) -> bool:
        relative = self.relative(path)
        return relative in self.source.blobs or relative in self.source.directories

    def glob(self, root: Path, pattern: str) -> list[Path]:
        prefix = self.relative(root)
        prefix = "" if prefix == "." else prefix + "/"
        return [self.source.root / path for path in sorted(self.source.directories | self.source.blobs.keys())
                if path.startswith(prefix) and PurePosixPath(path[len(prefix):]).full_match(pattern)]

    def regex(self, spec: generators.CandidatesSpec, root: Path) -> list[generators.Candidate]:
        _ = root
        result = self.source.regex(spec, tuple(sorted(self.paths)))
        return list(result.candidates)
