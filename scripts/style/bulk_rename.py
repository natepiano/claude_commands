#!/usr/bin/env python3
"""Apply approved local renames with rust-analyzer, the shared proof and crate verification.

Nothing is committed. Refused findings return to ordinary tickets; kept findings become built at the normal merge.
The review file samples the verified findings that the batch will build.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import random
import shutil
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from urllib.parse import unquote, urlparse
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from functools import cache
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import BinaryIO, Protocol, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import berth_board
import style_footprint
import style_ledger
from approved_renames import (BindingComments, BindingRename, BindingScope, NoBindingScope,
                              RenameDeclaration, rename_declaration, refactor_rename, refactor_comments,
                              PatternBindingCollision, pattern_binding_check,
                              backtick_mentions, identifier_mentions)


class BulkRenameError(Exception):
    """The batch cannot safely proceed."""


def object_fields(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BulkRenameError("expected a JSON object")
    return cast(dict[str, object], value)


def object_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise BulkRenameError("expected a JSON list")
    return cast(list[object], value)


@dataclass(frozen=True)
class Refusal:
    finding_id: str
    reason: str


@dataclass(frozen=True)
class RenameFinding:
    finding_id: str
    path: str
    rename: BindingRename


@dataclass(frozen=True)
class KeptRename:
    finding: RenameFinding
    kind: str
    macro_edits: int


@dataclass(frozen=True)
class FileRenames:
    path: str
    original: str
    edited: str
    kept: tuple[KeptRename, ...]
    refusals: tuple[Refusal, ...]
    related: tuple[RenamedDocument, ...] = ()
    dependencies: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SourceEdit:
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class DefinitionTarget:
    """One unambiguous definition's URI and UTF-16 selection range."""

    uri: str
    start: tuple[int, int]
    end: tuple[int, int]


@dataclass(frozen=True)
class DefinitionUnavailable:
    reason: str


@dataclass(frozen=True)
class FieldShorthandTargets:
    """The two meanings of one record shorthand token, with neither discarded."""

    field: DefinitionTarget
    binding: DefinitionTarget


ResolvedReference = DefinitionTarget | FieldShorthandTargets
DefinitionResolution = ResolvedReference | DefinitionUnavailable


@dataclass(frozen=True)
class DocumentEdits:
    """One document's complete rust-analyzer edit against its current text."""

    path: Path
    source: str
    edits: tuple[SourceEdit, ...]


@dataclass(frozen=True)
class WorkspaceRename:
    documents: tuple[DocumentEdits, ...]


@dataclass(frozen=True)
class RenamedDocument:
    path: str
    original: str
    edited: str


class RenameServer(Protocol):
    @property
    def sources(self) -> RunSourceIndex: ...

    @property
    def documents(self) -> Mapping[Path, str]: ...

    def open(self, path: Path, text: str) -> None: ...
    def change(self, path: Path, text: str) -> None: ...
    def rename(self, path: Path, text: str, offset: int, new: str) -> WorkspaceRename: ...
    def definitions(self, path: Path, text: str, offsets: tuple[int, ...]) -> tuple[DefinitionResolution, ...]: ...


def definition_target(value: object) -> DefinitionTarget | DefinitionUnavailable:
    row = object_fields(value)
    uri = row.get("targetUri", row.get("uri"))
    bounds = object_fields(row.get("targetSelectionRange", row.get("range")))
    positions: list[tuple[int, int]] = []
    for side in ("start", "end"):
        position = object_fields(bounds.get(side))
        line, character = position.get("line"), position.get("character")
        if not isinstance(line, int) or not isinstance(character, int) or line < 0 or character < 0:
            raise BulkRenameError("invalid definition position")
        positions.append((line, character))
    if not isinstance(uri, str) or not uri.startswith("file:"):
        return DefinitionUnavailable("macro-made or non-file definition")
    if positions[0] > positions[1]:
        return DefinitionUnavailable("reversed definition range")
    return DefinitionTarget(uri, positions[0], positions[1])


def definition_result(value: object, sources: RunSourceIndex, source: str, offset: int) -> DefinitionResolution:
    if value is None or value == []:
        return DefinitionUnavailable("missing definition")
    rows = cast(list[object], value) if isinstance(value, list) else [value]
    if len(rows) == 1:
        return definition_target(rows[0])
    parsed = sources.tokens(source)
    site = next((i for i, span in enumerate(parsed.spans) if span.start == offset), -1)
    if (len(rows) != 2 or site < 0 or not style_footprint.shorthand_field(parsed.words, site)
            or parsed.contains(parsed.macros, site)):
        return DefinitionUnavailable("ambiguous definition: not field shorthand")
    targets = tuple(definition_target(row) for row in rows)
    if any(isinstance(target, DefinitionUnavailable) for target in targets):
        return DefinitionUnavailable("ambiguous definition: shorthand target unavailable")
    return field_shorthand_targets(tuple(target for target in targets if isinstance(target, DefinitionTarget)),
                                   sources, frozenset({parsed.words[site]}))


def field_shorthand_targets(targets: tuple[DefinitionTarget, ...], sources: RunSourceIndex,
                            names: frozenset[str]) -> FieldShorthandTargets | DefinitionUnavailable:
    """Classify exactly one field and one binding, retaining both complete selection ranges."""
    if len(targets) != 2:
        return DefinitionUnavailable("ambiguous definition: not one field and one binding")
    roles: dict[str, DefinitionTarget] = {}
    for target in targets:
        declaration = sources.read(Path(unquote(urlparse(target.uri).path)))
        point = source_offset(declaration.source, {"line": target.start[0], "character": target.start[1]})
        token = next((i for i, span in enumerate(declaration.spans) if span.start == point), -1)
        if declaration.contains(declaration.macros, token):
            return DefinitionUnavailable("ambiguous definition: macro-made shorthand target")
        if (token < 0 or declaration.words[token] not in names
                or lsp_position(declaration.source, declaration.spans[token].end)
                != {"line": target.end[0], "character": target.end[1]}):
            return DefinitionUnavailable("ambiguous definition: shorthand target is not a declaration")
        # Reuse the declaration parser with one token per line to select this exact token.
        scope = rename_declaration("\n".join(declaration.words),
                                   BindingRename(token + 1, declaration.words[token], declaration.words[token], "unused_name"))
        if not isinstance(scope, RenameDeclaration):
            return DefinitionUnavailable("ambiguous definition: shorthand target role unavailable")
        role = "field" if scope.scope.kind == "field" else "binding"
        if scope.scope.kind in {"item", "variant"} or role in roles:
            return DefinitionUnavailable("ambiguous definition: not one field and one binding")
        roles[role] = target
    if set(roles) != {"field", "binding"}:
        return DefinitionUnavailable("ambiguous definition: not one field and one binding")
    return FieldShorthandTargets(roles["field"], roles["binding"])


def lsp_position(text: str, offset: int) -> dict[str, int]:
    prefix = text[:offset]
    return {"line": prefix.count("\n"), "character": len(prefix.rsplit("\n", 1)[-1].encode("utf-16-le")) // 2}


def source_offset(text: str, position: object) -> int:
    fields = object_fields(position)
    line, character = fields.get("line"), fields.get("character")
    if not isinstance(line, int) or not isinstance(character, int) or line < 0 or character < 0:
        raise BulkRenameError("invalid LSP position")
    lines = text.split("\n")
    if line >= len(lines):
        raise BulkRenameError("LSP line is outside the source")
    units = 0
    for index, char in enumerate(lines[line]):
        if units == character:
            return sum(len(part) + 1 for part in lines[:line]) + index
        units += len(char.encode("utf-16-le")) // 2
    if units == character:
        return sum(len(part) + 1 for part in lines[:line]) + len(lines[line])
    raise BulkRenameError("LSP character is outside the source or splits a surrogate")


EMPTY_DOCUMENTS: Mapping[Path, str] = MappingProxyType({})
EMPTY_VERSIONS: Mapping[Path, int] = MappingProxyType({})


def workspace_edits(value: object, path: Path, text: str, version: int,
                    opened: Mapping[Path, str] = EMPTY_DOCUMENTS, versions: Mapping[Path, int] = EMPTY_VERSIONS,
                    read_source: Callable[[Path], str] = Path.read_text) -> WorkspaceRename:
    """Read the entire WorkspaceEdit; reject resource operations and stale or unsafe documents."""
    fields = object_fields(value)
    documents: dict[Path, list[object]] = {}
    def add(uri: str, rows: object, expected: object = None) -> None:
        parsed = urlparse(uri)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise BulkRenameError("WorkspaceEdit contains a non-file URI")
        target = Path(unquote(parsed.path))
        if target.resolve() != target or not target.is_file() or target.suffix != ".rs":
            raise BulkRenameError("WorkspaceEdit target is not a regular Rust file: " + str(target))
        if expected is not None and expected != (version if target == path else versions.get(target)):
            raise BulkRenameError("rust-analyzer edited a stale document version")
        documents.setdefault(target, []).extend(object_list(rows))
    for uri, rows in object_fields(fields.get("changes", {})).items():
        add(uri, rows)
    for change in object_list(fields.get("documentChanges", [])):
        row = object_fields(change)
        if "kind" in row:
            raise BulkRenameError("WorkspaceEdit contains a resource operation")
        document = object_fields(row.get("textDocument"))
        add(str(document.get("uri")), row.get("edits"), document.get("version"))
    result: list[DocumentEdits] = []
    for target, rows in documents.items():
        source = text if target == path else opened[target] if target in opened else read_source(target)
        edits: list[SourceEdit] = []
        for item in rows:
            row = object_fields(item)
            bounds = object_fields(row.get("range"))
            replacement = row.get("newText")
            if not isinstance(replacement, str):
                raise BulkRenameError("LSP edit has no replacement text")
            edits.append(SourceEdit(source_offset(source, bounds.get("start")),
                                    source_offset(source, bounds.get("end")), replacement))
        if edits:
            _ = apply_edits(source, edits)
            result.append(DocumentEdits(target, source, tuple(edits)))
    if not result:
        raise BulkRenameError("rust-analyzer returned no edits")
    return WorkspaceRename(tuple(result))


def apply_edits(text: str, edits: Sequence[SourceEdit]) -> str:
    end = len(text) + 1
    for edit in sorted(edits, key=lambda edit: (edit.start, edit.end), reverse=True):
        if not 0 <= edit.start <= edit.end <= len(text) or edit.end > end or edit.start == end:
            raise BulkRenameError("overlapping or invalid edits")
        text = text[:edit.start] + edit.text + text[edit.end:]
        end = edit.start
    return text


class RustAnalyzer:
    """A synchronous LSP client; a reader thread keeps pipe reads subject to request deadlines."""

    def __init__(self, repo: Path, timeout: float = 180) -> None:
        self.repo: Path = repo
        self.timeout: float = timeout
        self.process: subprocess.Popen[bytes] = subprocess.Popen(
            ["rust-analyzer"], cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.inbox: queue.Queue[dict[str, object] | Exception] = queue.Queue()
        self.sequence: int = 0
        self.versions: dict[Path, int] = {}
        self.documents: dict[Path, str] = {}
        self.sources: RunSourceIndex = RunSourceIndex(self)
        self.ready: bool = False
        self.settings: dict[str, object] = {
            "cargo": {"buildScripts": {"enable": True}}, "procMacro": {"enable": True}, "checkOnSave": False,
        }
        self.reader: threading.Thread = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        try:
            stream = cast(BinaryIO | None, self.process.stdout)
            if stream is None:
                raise BulkRenameError("rust-analyzer has no stdout")
            while True:
                headers: dict[str, str] = {}
                while True:
                    line = stream.readline()
                    if not line:
                        raise BulkRenameError("rust-analyzer closed its output")
                    if line in (b"\r\n", b"\n"):
                        break
                    key, value = line.decode().split(":", 1)
                    headers[key.lower()] = value.strip()
                size = int(headers["content-length"])
                body = stream.read(size)
                if len(body) != size:
                    raise BulkRenameError("incomplete LSP message")
                self.inbox.put(object_fields(cast(object, json.loads(body))))
        except Exception as error:
            self.inbox.put(error)

    def _send(self, message: dict[str, object]) -> None:
        stream = self.process.stdin
        if stream is None:
            raise BulkRenameError("rust-analyzer has no stdin")
        body = json.dumps({"jsonrpc": "2.0", **message}).encode()
        try:
            _ = stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            stream.flush()
        except OSError as error:
            raise BulkRenameError(str(error)) from error

    def notify(self, method: str, params: dict[str, object]) -> None:
        self._send({"method": method, "params": params})

    def _message(self, deadline: float) -> dict[str, object]:
        try:
            message = self.inbox.get(timeout=max(0.001, deadline - time.monotonic()))
        except queue.Empty as error:
            raise BulkRenameError("rust-analyzer timed out") from error
        if isinstance(message, Exception):
            raise BulkRenameError(str(message)) from message
        method = message.get("method")
        if "id" in message and method:
            result: object = None
            if method == "workspace/configuration":
                items = object_list(object_fields(message.get("params")).get("items"))
                configs: list[object] = []
                for item in items:
                    section = str(object_fields(item).get("section", "rust-analyzer"))
                    config: object = self.settings
                    for part in section.removeprefix("rust-analyzer").strip(".").split("."):
                        if part:
                            config = object_fields(config).get(part)
                    configs.append(config)
                result = configs
            self._send({"id": message["id"], "result": result})
        if method == "experimental/serverStatus":
            self.ready = object_fields(message.get("params")).get("quiescent") is True
        return message

    def request(self, method: str, params: dict[str, object]) -> object:
        self.sequence += 1
        identity = self.sequence
        self._send({"id": identity, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            message = self._message(deadline)
            if message.get("id") == identity and "method" not in message:
                if "error" in message:
                    raise BulkRenameError(str(message["error"]))
                return message.get("result")
        raise BulkRenameError(f"rust-analyzer timed out on {method}")

    def __enter__(self) -> RustAnalyzer:
        try:
            _ = self.request("initialize", {
                "processId": os.getpid(), "rootUri": self.repo.as_uri(),
                "capabilities": {"workspace": {"workspaceEdit": {"documentChanges": True}},
                                 "experimental": {"serverStatusNotification": True},
                                 "textDocument": {"rename": {"prepareSupport": True}}},
                "initializationOptions": self.settings,
            })
            self.notify("initialized", {})
            deadline = time.monotonic() + self.timeout
            while not self.ready and time.monotonic() < deadline:
                _ = self._message(deadline)
            if not self.ready:
                raise BulkRenameError("rust-analyzer did not finish loading the project")
            return self
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                _ = self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                _ = self.process.wait()
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None:
                stream.close()
        self.reader.join(timeout=1)

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None) -> None:
        self.close()

    def open(self, path: Path, text: str) -> None:
        self.sources.update(path, text)
        if path in self.versions:
            self.change(path, text)
            return
        self.documents[path] = text
        self.versions[path] = 1
        self.notify("textDocument/didOpen", {"textDocument": {
            "uri": path.as_uri(), "languageId": "rust", "version": 1, "text": text}})

    def change(self, path: Path, text: str) -> None:
        self.sources.update(path, text)
        self.documents[path] = text
        self.versions[path] += 1
        self.notify("textDocument/didChange", {"textDocument": {"uri": path.as_uri(), "version": self.versions[path]},
                                               "contentChanges": [{"text": text}]})

    def rename(self, path: Path, text: str, offset: int, new: str) -> WorkspaceRename:
        params: dict[str, object] = {"textDocument": {"uri": path.as_uri()}, "position": lsp_position(text, offset)}
        if self.request("textDocument/prepareRename", params) is None:
            raise BulkRenameError("rust-analyzer refused prepareRename")
        value = self.request("textDocument/rename", {**params, "newName": new})
        return workspace_edits(value, path, text, self.versions[path], self.documents, self.versions,
                               lambda target: self.sources.read(target).source)

    def definitions(self, path: Path, text: str, offsets: tuple[int, ...]) -> tuple[DefinitionResolution, ...]:
        """Pipeline one file's definition requests; one deadline bounds the entire round."""
        pending: dict[int, int] = {}
        answers: list[DefinitionResolution] = [DefinitionUnavailable("definition request timed out")] * len(offsets)
        for index, offset in enumerate(offsets):
            self.sequence += 1
            pending[self.sequence] = index
            self._send({"id": self.sequence, "method": "textDocument/definition", "params": {
                "textDocument": {"uri": path.as_uri()}, "position": lsp_position(text, offset)}})
        deadline = time.monotonic() + self.timeout
        while pending and time.monotonic() < deadline:
            try:
                message = self._message(deadline)
            except BulkRenameError as error:
                for index in pending.values():
                    answers[index] = DefinitionUnavailable(str(error))
                break
            identity = message.get("id")
            if not isinstance(identity, int) or identity not in pending or "method" in message:
                continue
            index = pending.pop(identity)
            try:
                answers[index] = (DefinitionUnavailable("server error: " + str(message["error"])) if "error" in message
                                  else definition_result(message.get("result"), self.sources, text, offsets[index]))
            except (BulkRenameError, OSError, UnicodeError, ValueError) as error:
                answers[index] = DefinitionUnavailable(str(error))
        return tuple(answers)


class BindingText:
    """Token locations for external-name and macro-output guards."""

    def __init__(self, source: str) -> None:
        self.source: str = source
        try:
            self.spans: tuple[style_footprint.TokenSpan, ...] = style_footprint.token_spans(source)
        except style_footprint.UnreadableTokenSpans as error:
            raise BulkRenameError(str(error)) from error
        self.words: tuple[str, ...] = tuple(span.token.value for span in self.spans)
        self.ends: dict[int, int] = style_footprint.delimiter_ends(self.words)
        self.macros: tuple[tuple[int, int], ...] = style_footprint.macro_spans(self.words, self.ends)
        self.formats: frozenset[int] = self.format_literals()
        self.parents: dict[int, int] = {}
        stack: list[int] = []
        for index in range(len(self.words)):
            if stack and self.ends[stack[-1]] == index:
                _ = stack.pop()
            if stack:
                self.parents[index] = stack[-1]
            if index in self.ends:
                stack.append(index)

    @staticmethod
    def contains(ranges: tuple[tuple[int, int], ...], index: int) -> bool:
        return any(start <= index < end for start, end in ranges)

    def derive_field(self, rename: BindingRename) -> bool:
        for index, word in enumerate(self.words):
            if word not in {"struct", "enum"}:
                continue
            body = next((i for i in range(index + 1, len(self.words)) if self.words[i] in {"{", "(", ";"}), -1)
            if body not in self.ends:
                continue
            finish = self.ends[body]
            start_line = self.source[:self.spans[body].start].count("\n") + 1
            end_line = self.source[:self.spans[finish].end].count("\n") + 1
            if not start_line <= rename.line <= end_line:
                continue
            start = index - 1
            while start >= 0 and self.words[start] not in {";", "}"}:
                start -= 1
            attributes = set(self.words[start + 1:index])
            if "derive" in attributes and attributes & {"Serialize", "Deserialize", "Parser", "Args", "Subcommand", "ValueEnum", "Config"}:
                return True
        return False

    def format_literals(self) -> frozenset[int]:
        """Literal format arguments, excluding ordinary strings in the same standard macro."""
        positions: set[int] = set()
        plain = {"println", "eprintln", "print", "eprint", "format", "format_args", "panic", "unreachable",
                 "todo", "unimplemented"}
        for bang, finish in self.macros:
            name = self.words[bang - 1]
            argument = (0 if name in plain else 1 if name in {"write", "writeln", "assert", "debug_assert"}
                        else 2 if name in {"assert_eq", "assert_ne", "debug_assert_eq", "debug_assert_ne"} else -1)
            if argument < 0:
                continue
            cursor = bang + 2
            for _ in range(argument):
                while cursor < finish - 1 and self.words[cursor] != ",":
                    cursor = self.ends.get(cursor, cursor) + 1
                cursor += 1
            if cursor < finish - 1 and (self.words[cursor].startswith('"') or self.words[cursor].startswith('r')):
                if self.spans[cursor].token.kind == "literal":
                    positions.add(cursor)
        return frozenset(positions)


def repository_docs(repo: Path) -> tuple[str, ...]:
    """Read repository Markdown once per batch; bulk edits never write these files."""
    root = repo.resolve()
    return tuple(path.read_text() for path in sorted(root.rglob("*.md"))
                 if ".git" not in path.relative_to(root).parts and path.is_file() and path.resolve().is_relative_to(root))


def docs_name_binding(docs: tuple[str, ...], comments: BindingComments, old: str) -> bool:
    return any(identifier_mentions(document, function) and backtick_mentions(document, old)
               for document in docs for function in comments.functions)


def mapped_offset(offset: int, edits: Sequence[SourceEdit]) -> int:
    """Map a token boundary through non-overlapping edits; interiors have no proven correspondence."""
    if any(edit.start < offset < edit.end for edit in edits):
        raise BulkRenameError("definition boundary lies inside an edit")
    return offset + sum(len(edit.text) - (edit.end - edit.start) for edit in edits if edit.end <= offset)


def crate_directory(path: Path) -> Path:
    for parent in path.parents:
        manifest = parent / "Cargo.toml"
        if manifest.is_file() and "package" in tomllib.loads(manifest.read_text()):
            return parent
    raise BulkRenameError("finding has no Cargo package")


def published_crate(crate: Path) -> bool:
    package = object_fields(tomllib.loads((crate / "Cargo.toml").read_text()).get("package"))
    return package.get("publish") not in (False, [])


def public_declaration(parsed: BindingText, scope: BindingScope) -> bool:
    if scope.kind not in {"field", "variant", "item"}:
        return False
    index = scope.declaration
    start = index - 1
    while start >= 0 and parsed.words[start] not in {";", "{", "}", ","}:
        start -= 1
    prefix = parsed.words[start + 1:index]
    # Restricted pub is crate-private; plain pub is externally observable.
    if any(word == "pub" and prefix[i + 1:i + 2] != ("(",) for i, word in enumerate(prefix)):
        return True
    if scope.kind == "variant":
        owner = scope.ranges[0][0]
        return owner > 0 and parsed.words[owner - 1] == "pub"
    # Trait members and enum fields inherit the public owner's visibility.
    for i, word in enumerate(parsed.words[:index]):
        if word not in {"trait", "enum"} or not i or parsed.words[i - 1] != "pub":
            continue
        body = next((j for j in range(i + 1, index) if parsed.words[j] == "{"), -1)
        if body < index < parsed.ends.get(body, -1):
            return True
    return False


def glob_modules(parsed: BindingText, *, exported_only: bool = False) -> tuple[int, ...]:
    """The module token before each glob leaf, including nested use trees."""
    modules: list[int] = []
    for start, word in enumerate(parsed.words):
        if word != "use" or exported_only and parsed.words[max(0, start - 1):start] != ("pub",):
            continue
        end = next((i for i in range(start + 1, len(parsed.words)) if parsed.words[i] == ";"), len(parsed.words))
        for i in range(start + 1, end):
            if parsed.words[i] != "*":
                continue
            previous = i - 1
            if parsed.words[previous] == "::":
                previous -= 1
            elif parsed.words[previous] in {"{", ","}:
                parent = parsed.parents.get(i, -1)
                previous = parent - 2
            if previous <= start or parsed.spans[previous].token.kind not in {"identifier", "keyword"}:
                raise BulkRenameError("glob import module cannot be located")
            modules.append(previous)
    return tuple(modules)


def module_exports(server: RenameServer, path: Path, source: str, module: int, new: str,
                   visited: set[tuple[str, tuple[int, int]]]) -> bool:
    parsed = server.sources.tokens(source)
    answers = server.definitions(path, source, (parsed.spans[module].start,))
    if len(answers) != 1 or not isinstance(answers[0], DefinitionTarget):
        cause = answers[0].reason if answers and isinstance(answers[0], DefinitionUnavailable) else "ambiguous module"
        raise BulkRenameError("glob import exports unavailable: " + cause)
    target = answers[0]
    key = (target.uri, target.start)
    if key in visited:
        return False
    visited.add(key)
    target_path = Path(unquote(urlparse(target.uri).path))
    target_path = target_path.resolve()
    tokens = server.sources.read(target_path)
    module_source = tokens.source
    offset = source_offset(module_source, {"line": target.start[0], "character": target.start[1]})
    selection = next((i for i, span in enumerate(tokens.spans) if span.start <= offset < span.end), -1)
    coverage = server.sources.coverage(path)
    covered = {document.path: document.source for document in coverage.documents}
    if selection > 0 and tokens.words[selection - 1] == "mod":
        selected = {(entry.path, entry.begin, entry.end): entry for entry in coverage.modules
                    if entry.instance.canonical_path == target_path and entry.declaration_offset == offset}
        if len(selected) != 1:
            raise BulkRenameError("glob import module instance coverage unavailable")
        entry = next(iter(selected.values()))
        target_path = entry.path
        module_source = covered[target_path]
        # Keep definition offsets when examining an inline module's body.
        module_source = ''.join(char if entry.begin <= i < entry.end or char == "\n" else ' '
                                for i, char in enumerate(module_source))
        tokens = server.sources.tokens(module_source)
    elif target_path in covered:
        module_source = covered[target_path]
        tokens = server.sources.tokens(module_source)
    else:
        # External module files can be inspected directly, but cannot use guessed child paths.
        if module_path_attributes(target_path, tokens):
            raise BulkRenameError("glob import module context unavailable for external path loader")
    for i, word in enumerate(tokens.words):
        if word.removeprefix("r#") != new.removeprefix("r#"):
            continue
        # A public declaration/re-export or enum variant is an exported collision.
        line = module_source.count("\n", 0, tokens.spans[i].start) + 1
        declaration = rename_declaration(module_source, BindingRename(line, module_source.splitlines()[line - 1], word, "unused_name"))
        if isinstance(declaration, RenameDeclaration) and public_declaration(tokens, declaration.scope):
            return True
        start = i - 1
        while start >= 0 and tokens.words[start] not in {";", "}"}:
            start -= 1
        if "pub" in tokens.words[start + 1:i] and ("use" in tokens.words[start + 1:i]
                or isinstance(declaration, RenameDeclaration) and declaration.scope.kind == "item"):
            return True
    for child in glob_modules(tokens, exported_only=True):
        # Resolving a re-export needs its module instance. Plain absence needs only source text.
        coverage.require_single_instance({target_path})
        if module_exports(server, target_path, module_source, child, new, visited):
            return True
    if "macro_rules" in tokens.words or any(word == "include" for word in tokens.words):
        raise BulkRenameError("glob import exports are macro-generated")
    return False


def rename_guard(server: RenameServer, path: Path, source: str, declaration: RenameDeclaration,
                 rename: BindingRename) -> None:
    parsed = server.sources.tokens(source)
    collision = pattern_binding_check(source, declaration, rename)
    if isinstance(collision, PatternBindingCollision):
        raise BulkRenameError(collision.reason)
    if parsed.derive_field(rename):
        raise BulkRenameError("serde/clap/confique derive field or variant")
    if declaration.scope.kind in {"field", "variant", "item"}:
        owners = style_footprint.attribute_owners(parsed.words)
        for start, owner in owners.items():
            applies = (owner == declaration.scope.ranges[0][0]
                       or owner <= declaration.scope.declaration
                       and not set(parsed.words[owner:declaration.scope.declaration]) & {";", ",", "{", "}"})
            attribute_end = dict(style_footprint.attribute_spans(parsed.words))[start]
            attributes = parsed.words[start:attribute_end]
            if applies and set(attributes) & {"serde", "clap", "confique", "arg", "command"}:
                raise BulkRenameError("serialized text, CLI flag or config key attribute")
            if applies and "derive" in attributes:
                derive = start + attributes.index("derive")
                opening = derive + 1
                if parsed.words[opening] != "(" or opening not in parsed.ends:
                    raise BulkRenameError("derive attribute arguments unavailable")
                names = {word for word in parsed.words[opening + 1:parsed.ends[opening]]
                         if style_footprint.IDENTIFIER.fullmatch(word)}
                unknown = names - style_footprint.STD_DERIVES
                if unknown:
                    raise BulkRenameError("custom derive may expose names: " + ", ".join(sorted(unknown)))
        if published_crate(crate_directory(path)) and public_declaration(parsed, declaration.scope):
            raise BulkRenameError("rename touches a public item of a published crate")
    modules = glob_modules(parsed)
    if modules:
        if any(word.removeprefix("r#") == rename.new.removeprefix("r#") for word in parsed.words):
            raise BulkRenameError("glob import clash: new name " + repr(rename.new) + " occurs in the file")
        for module in modules:
            if module_exports(server, path, source, module, rename.new, set()):
                raise BulkRenameError("glob import clash: " + repr(rename.new) + " is exported by " + parsed.words[module])
    # Reject obvious binding collisions before requesting the refactor; verify its resolutions below.
    if declaration.scope.kind not in {"field", "variant", "item"}:
        for i, word in enumerate(parsed.words):
            if word.removeprefix("r#") != rename.new.removeprefix("r#"):
                continue
            if parsed.contains(declaration.scope.ranges, i) and parsed.words[max(0, i - 1):i] not in {(".",), ("::",)}:
                line = source.count("\n", 0, parsed.spans[i].start) + 1
                raise BulkRenameError(f"rename could capture {rename.new!r} at line {line}")


@cache
def standard_library(crate: Path) -> Path:
    """The active toolchain's source root, never a path inferred from a macro's spelling."""
    try:
        result = subprocess.run(["rustc", "--print", "sysroot"], cwd=crate, capture_output=True,
                                text=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        raise BulkRenameError("macro origin unavailable: active Rust sysroot could not be read") from error
    return (Path(result.stdout.strip()) / "lib/rustlib/src/rust/library").resolve()


def text_name_guard(server: RenameServer, document: DocumentEdits, rename: BindingRename) -> None:
    parsed = BindingText(document.source)
    safe = {"println", "eprintln", "print", "eprint", "format", "format_args", "write", "writeln",
            "assert", "assert_eq", "assert_ne", "debug_assert", "debug_assert_eq", "debug_assert_ne",
            "panic", "todo", "unreachable", "unimplemented", "vec", "matches"}
    for bang, finish in parsed.macros:
        if bang == 0 or parsed.words[bang - 1] == "#":
            continue
        start, end = parsed.spans[bang].start, parsed.spans[finish - 1].end
        if not any(edit.start < end and start < edit.end for edit in document.edits):
            continue
        name = parsed.words[bang - 1]
        line = document.source.count("\n", 0, start) + 1
        if name not in safe:
            raise BulkRenameError(f"name reaches macro-generated text: {name} at line {line}")
        answers = server.definitions(document.path, document.source, (parsed.spans[bang - 1].start,))
        if len(answers) != 1 or not isinstance(answers[0], DefinitionTarget):
            raise BulkRenameError(f"macro origin unproved: {name} at line {line}: missing or ambiguous definition")
        origin = Path(unquote(urlparse(answers[0].uri).path)).resolve()
        library = standard_library(crate_directory(document.path))
        if not any(origin.is_relative_to(library / package / "src") for package in ("std", "core", "alloc")):
            raise BulkRenameError(f"macro origin is not standard: {name} at line {line}")
    for index, span in enumerate(parsed.spans):
        if span.token.kind != "literal" or not identifier_mentions(span.token.value, rename.old):
            continue
        if index not in parsed.formats and any(edit.start < span.end and span.start < edit.end for edit in document.edits):
            raise BulkRenameError("rename changes serialized or externally visible literal text")


def public_edit_guard(document: DocumentEdits, rename: BindingRename) -> None:
    if not published_crate(crate_directory(document.path)):
        return
    parsed = BindingText(document.source)
    for i, span in enumerate(parsed.spans):
        if span.token.kind != "identifier" or not any(edit.start <= span.start < edit.end for edit in document.edits):
            continue
        line = document.source.count("\n", 0, span.start) + 1
        scope = BindingScope("item", i, ((i, i + 1),))
        if not public_declaration(parsed, scope):
            continue
        candidate = rename_declaration(document.source, BindingRename(
            line, document.source.splitlines()[line - 1], span.token.value, rename.new))
        scope = candidate.scope if isinstance(candidate, RenameDeclaration) else scope
        if public_declaration(parsed, scope):
            raise BulkRenameError("WorkspaceEdit touches a public item of a published crate: " + str(document.path))


@dataclass(frozen=True)
class ReferenceMustResolve:
    """A post-rename offset and the exact definition that must survive the rename."""

    offset: int
    target: ResolvedReference


def source_name_index(parsed: BindingText) -> dict[str, tuple[int, ...]]:
    """Index all identifiers once, including explicit format captures but excluding comments."""
    offsets: dict[str, list[int]] = defaultdict(list)
    for i, span in enumerate(parsed.spans):
        if span.token.kind == "identifier":
            offsets[span.token.value.removeprefix("r#")].append(span.start)
        elif i in parsed.formats:
            names = {match.group().removeprefix("r#") for match in style_footprint.IDENTIFIER.finditer(span.token.value)}
            for name in names:
                offsets[name].extend(span.start + start for start, _ in identifier_mentions(span.token.value, name))
    return {name: tuple(points) for name, points in offsets.items()}


def target_after_edits(target: DefinitionTarget, replacements: Sequence[tuple[DocumentEdits, str]]) -> DefinitionTarget:
    for document, edited in replacements:
        if document.path.as_uri() != target.uri:
            continue
        positions: list[tuple[int, int]] = []
        for line, character in (target.start, target.end):
            before = source_offset(document.source, {"line": line, "character": character})
            after = lsp_position(edited, mapped_offset(before, document.edits))
            positions.append((after["line"], after["character"]))
        return DefinitionTarget(target.uri, positions[0], positions[1])
    return target


def reference_after_edits(target: ResolvedReference, replacements: Sequence[tuple[DocumentEdits, str]]) -> ResolvedReference:
    if isinstance(target, FieldShorthandTargets):
        return FieldShorthandTargets(target_after_edits(target.field, replacements),
                                    target_after_edits(target.binding, replacements))
    return target_after_edits(target, replacements)


@dataclass(frozen=True)
class ModuleSource:
    """Current text of a source file reached from a Cargo target root."""

    path: Path
    source: str


@dataclass(frozen=True)
class CargoSourceTarget:
    kind: str
    name: str
    path: Path


@dataclass(frozen=True)
class OwnedModuleDirectory:
    """Roots, mod.rs and explicit paths look up children in this lexical directory."""

    directory: Path

    def children(self) -> Path:
        return self.directory


@dataclass(frozen=True)
class FlatModuleDirectory:
    """Ordinary file modules defer their name until ordinary or inline child lookup."""

    directory: Path
    deferred_name: str

    def children(self) -> Path:
        return self.directory / self.deferred_name


ModuleLoadingDirectory = OwnedModuleDirectory | FlatModuleDirectory


@dataclass(frozen=True)
class ModuleInstance:
    target: CargoSourceTarget
    ancestry: tuple[str, ...]
    lexical_path: Path
    canonical_path: Path


@dataclass(frozen=True)
class ModuleDeclarationSource:
    """The source body selected by one module declaration in one target instance."""

    instance: ModuleInstance
    declaration_offset: int
    path: Path
    begin: int
    end: int


@dataclass(frozen=True)
class CrateSourceCoverage:
    documents: tuple[ModuleSource, ...]
    instances: tuple[ModuleInstance, ...]
    modules: tuple[ModuleDeclarationSource, ...]

    def require_single_instance(self, paths: set[Path]) -> None:
        for path in sorted(paths):
            count = sum(instance.canonical_path == path for instance in self.instances)
            if count > 1:
                raise BulkRenameError(f"module instance coverage unavailable: {path} has {count} instances; "
                                      + "definition evidence does not cover every instance")


@dataclass(frozen=True)
class ModuleLoadingSite:
    """A stable loading edge and its current source boundaries, independent of other tokens."""

    spelling: tuple[str, ...]
    declaration: int
    begin: int
    end: int


@dataclass(frozen=True)
class SourceCoverageUnavailable:
    reason: str


class RunSourceIndex:
    """One session's source graph and tokens; definition answers are deliberately never cached."""

    def __init__(self, server: RenameServer) -> None:
        self.server: RenameServer = server
        self.files: dict[Path, BindingText] = {}
        self.parsed: dict[str, BindingText] = {}
        self.names: dict[str, dict[str, tuple[int, ...]]] = {}
        self.packages: dict[Path, CrateSourceCoverage | SourceCoverageUnavailable] = {}
        self.views: dict[Path, tuple[int, CrateSourceCoverage]] = {}
        self.layouts: dict[tuple[Path, str], tuple[ModuleLoadingSite, ...]] = {}
        self.revision: int = 0

    def tokens(self, source: str) -> BindingText:
        if source not in self.parsed:
            self.parsed[source] = BindingText(source)
        return self.parsed[source]

    def update(self, path: Path, source: str) -> None:
        canonical = path.resolve()
        if canonical not in self.files or self.files[canonical].source != source:
            self.files[canonical] = self.tokens(source)
            self.revision += 1

    def read(self, path: Path) -> BindingText:
        canonical = path.resolve()
        if canonical in self.server.documents:
            self.update(canonical, self.server.documents[canonical])
        elif canonical not in self.files:
            self.update(canonical, canonical.read_text())
        return self.files[canonical]

    def named(self, source: str, name: str) -> tuple[int, ...]:
        if source not in self.names:
            self.names[source] = source_name_index(self.tokens(source))
        return self.names[source].get(name.removeprefix("r#"), ())

    def module_layout(self, path: Path, parsed: BindingText) -> tuple[ModuleLoadingSite, ...]:
        key = (path, parsed.source)
        if key in self.layouts:
            return self.layouts[key]
        explicit = module_path_attributes(path, parsed)
        sites: list[ModuleLoadingSite] = []
        for i, word in enumerate(parsed.words):
            if word != "mod" or parsed.contains(parsed.macros, i) or i + 2 >= len(parsed.words):
                continue
            body = i + 2
            sites.append(ModuleLoadingSite((*parsed.words[i:i + 3], explicit.get(i, "")), parsed.spans[i + 1].start,
                                            parsed.spans[body].end, parsed.spans[parsed.ends.get(body, body)].start))
        self.layouts[key] = tuple(sites)
        return self.layouts[key]

    def coverage(self, path: Path) -> CrateSourceCoverage:
        crate = crate_directory(path)
        if crate not in self.packages:
            try:
                self.packages[crate] = crate_source_coverage(self.server, path)
            except (BulkRenameError, OSError, UnicodeError, ValueError) as error:
                self.packages[crate] = SourceCoverageUnavailable(str(error))
        # Opening, accepted edits and rollback all come through the same source update boundary.
        base = self.packages[crate]
        if isinstance(base, SourceCoverageUnavailable):
            raise BulkRenameError(base.reason)
        for document in base.documents:
            _ = self.read(document.path)
        if crate in self.views and self.views[crate][0] == self.revision:
            return self.views[crate][1]
        boundaries: dict[Path, dict[int, int]] = {}
        documents: list[ModuleSource] = []
        for document in base.documents:
            current = self.files[document.path]
            documents.append(ModuleSource(document.path, current.source))
            if current.source == document.source:
                continue
            before = self.module_layout(document.path, self.tokens(document.source))
            after = self.module_layout(document.path, current)
            if tuple(site.spelling for site in before) != tuple(site.spelling for site in after):
                raise BulkRenameError("source coverage unavailable: module loading changed during rename run: " + str(document.path))
            # Only module anchors need rebasing. Token edits elsewhere cannot change the graph.
            points = {old: new for a, b in zip(before, after, strict=True)
                      for old, new in ((a.declaration, b.declaration), (a.begin, b.begin), (a.end, b.end))}
            points.update({0: 0, len(document.source): len(current.source)})
            boundaries[document.path] = points
        def boundary(path: Path, offset: int) -> int:
            return boundaries[path][offset] if path in boundaries else offset
        modules = tuple(replace(entry,
                                declaration_offset=boundary(entry.instance.canonical_path, entry.declaration_offset),
                                begin=boundary(entry.path, entry.begin), end=boundary(entry.path, entry.end)) for entry in base.modules)
        coverage = CrateSourceCoverage(tuple(documents), base.instances, modules)
        self.views[crate] = (self.revision, coverage)
        return coverage


def cargo_target_roots(crate: Path) -> tuple[CargoSourceTarget, ...]:
    """Read configured targets and Cargo's conventional target locations, without building."""
    manifest = object_fields(tomllib.loads((crate / "Cargo.toml").read_text()))
    package = object_fields(manifest.get("package"))
    roots: list[CargoSourceTarget] = []
    library = object_fields(manifest.get("lib", {}))
    if "lib" in manifest or package.get("autolib", True) and (crate / "src/lib.rs").exists():
        roots.append(CargoSourceTarget("lib", str(package.get("name")), crate / str(library.get("path", "src/lib.rs"))))
    for kind, folder, auto in (("bin", "src/bin", "autobins"), ("example", "examples", "autoexamples"),
                               ("test", "tests", "autotests"), ("bench", "benches", "autobenches")):
        discovered: dict[str, Path] = {}
        directory = crate / folder
        if directory.exists():
            for entry in sorted(directory.iterdir()):
                if entry.suffix == ".rs" and entry.is_file():
                    discovered[entry.stem] = entry
                elif entry.is_dir() and (entry / "main.rs").exists():
                    discovered[entry.name] = entry / "main.rs"
        if kind == "bin" and (crate / "src/main.rs").exists():
            discovered[str(package.get("name"))] = crate / "src/main.rs"
        configured = object_list(manifest.get(kind, []))
        for value in configured:
            target = object_fields(value)
            name = str(target.get("name", package.get("name")))
            if "path" in target:
                roots.append(CargoSourceTarget(kind, name, crate / str(target["path"])))
            elif name in discovered:
                roots.append(CargoSourceTarget(kind, name, discovered[name]))
            else:
                raise BulkRenameError(f"source coverage unavailable: Cargo {kind} target {name!r} has no source path")
            _ = discovered.pop(name, None)
        if package.get(auto, True):
            roots.extend(CargoSourceTarget(kind, name, path) for name, path in sorted(discovered.items()))
    build = package.get("build", "build.rs")
    if isinstance(build, str) and ("build" in package or (crate / build).exists()):
        roots.append(CargoSourceTarget("build", "build", crate / build))
    if not roots:
        raise BulkRenameError("source coverage unavailable: no Cargo target roots")
    return tuple(roots)


def module_path_attributes(path: Path, parsed: BindingText) -> dict[int, str]:
    """Associate supported literal outer paths with their out-of-line mod owners."""
    paths: dict[int, str] = {}
    owners = style_footprint.attribute_owners(parsed.words)
    macros = parsed.macros

    def refuse(start: int, cause: str) -> None:
        line = parsed.source.count("\n", 0, parsed.spans[start].start) + 1
        raise BulkRenameError(f"unsupported source loader: {cause} in {path} at line {line}")

    for start, end in style_footprint.attribute_spans(parsed.words):
        if any(begin <= start < finish for begin, finish in macros):
            continue  # Expansion-generated modules remain the phase 102 limitation.
        inner = parsed.words[start + 1] == "!"
        opening = start + (2 if inner else 1)
        attribute = parsed.words[opening + 1:end - 1]
        words = tuple(word.removeprefix("r#") for word in attribute)
        if words[:1] == ("cfg_attr",) and any(words[i:i + 2] == ("path", "=") for i in range(len(words) - 1)):
            refuse(start, "conditional path attribute")
        if words[:1] != ("path",):
            continue
        if inner:
            refuse(start, "inner path attribute")
        owner = owners.get(start, -1)
        if owner < 0 or parsed.words[owner] != "mod" or parsed.words[owner + 2:owner + 3] != (";",):
            refuse(start, "path attribute requires an out-of-line module")
        if owner in paths:
            refuse(start, "duplicate path attributes")
        if len(attribute) != 3 or attribute[1] != "=":
            refuse(start, "nonliteral path attribute")
        literal = attribute[2]
        if literal.startswith('"') and literal.endswith('"'):
            if "\\" in literal:
                refuse(start, "escaped path string")
            filename = literal[1:-1]
        elif literal.startswith('r') and '"' in literal:
            opening_quote = literal.index('"')
            hashes = literal[1:opening_quote]
            if hashes.strip('#') or not literal.endswith('"' + hashes):
                refuse(start, "nonliteral path attribute")
            filename = literal[opening_quote + 1:len(literal) - len(hashes) - 1]
        else:
            refuse(start, "nonliteral path attribute")
            raise AssertionError("refuse always raises")
        paths[owner] = filename
    for i, word in enumerate(parsed.words):
        if (word.removeprefix("r#") == "include" and parsed.words[i + 1:i + 2] == ("!",)
                and not any(begin <= i < finish for begin, finish in macros)):
            refuse(i, "include!")
    return paths


def crate_source_coverage(server: RenameServer, path: Path) -> CrateSourceCoverage:
    """Finish the supported source graph before filtering names; retain every instance."""
    documents: dict[Path, ModuleSource] = {}
    instances: list[ModuleInstance] = []
    declarations: list[ModuleDeclarationSource] = []
    active: set[Path] = set()

    def visit(candidate: Path, loading: ModuleLoadingDirectory, target: CargoSourceTarget,
              ancestry: tuple[str, ...]) -> ModuleSource:
        try:
            canonical = candidate.resolve(strict=True)
            if canonical in active:
                raise BulkRenameError(f"source coverage unavailable: cyclic module {candidate}")
            parsed = server.sources.read(canonical)
            source = parsed.source
        except (OSError, UnicodeError, RuntimeError) as error:
            raise BulkRenameError(f"source coverage unavailable: cannot read module {candidate}: {error}") from error
        explicit_paths = module_path_attributes(candidate, parsed)
        document = ModuleSource(canonical, source)
        documents[canonical] = document
        instance = ModuleInstance(target, ancestry, candidate, canonical)
        instances.append(instance)
        active.add(canonical)
        excluded = dict((*style_footprint.attribute_spans(parsed.words), *parsed.macros))

        def modules(start: int, finish: int, directory: ModuleLoadingDirectory, route: tuple[str, ...]) -> None:
            i = start
            while i < finish:
                if i in excluded:
                    i = excluded[i]
                    continue
                if (parsed.words[i] == "mod" and i + 2 < finish
                        and parsed.spans[i + 1].token.kind == "identifier"):
                    name = parsed.words[i + 1].removeprefix("r#")
                    body = i + 2
                    if parsed.words[body] == ";":
                        child_loading: ModuleLoadingDirectory
                        if i in explicit_paths:
                            selected = directory.directory / explicit_paths[i]
                            child_loading = OwnedModuleDirectory(selected.parent)
                        else:
                            folder = directory.children()
                            choices = (folder / (name + ".rs"), folder / name / "mod.rs")
                            found = [choice for choice in choices if choice.exists()]
                            if len(found) != 1:
                                raise BulkRenameError(f"source coverage unavailable: module {name!r} in {candidate}: "
                                                      + ("missing source" if not found else "ambiguous source"))
                            selected = found[0]
                            child_loading = (OwnedModuleDirectory(selected.parent) if selected.name == "mod.rs"
                                             else FlatModuleDirectory(selected.parent, name))
                        child = visit(selected, child_loading, target, (*route, name))
                        declarations.append(ModuleDeclarationSource(instance, parsed.spans[i + 1].start,
                                                                    child.path, 0, len(child.source)))
                    elif parsed.words[body] == "{" and body in parsed.ends:
                        end = parsed.ends[body]
                        declarations.append(ModuleDeclarationSource(instance, parsed.spans[i + 1].start,
                                                                    canonical, parsed.spans[body].end, parsed.spans[end].start))
                        modules(body + 1, end, OwnedModuleDirectory(directory.children() / name), (*route, name))
                        i = end + 1
                        continue
                i += 1

        modules(0, len(parsed.words), loading, ancestry)
        active.remove(canonical)
        return document

    for root in cargo_target_roots(crate_directory(path)):
        _ = visit(root.path, OwnedModuleDirectory(root.path.parent), root, ())
    return CrateSourceCoverage(tuple(documents.values()), tuple(instances), tuple(declarations))


def crate_source_documents(server: RenameServer, path: Path) -> tuple[ModuleSource, ...]:
    return server.sources.coverage(path).documents


def crate_name_documents(server: RenameServer, path: Path, name: str,
                         changed_paths: tuple[Path, ...] = ()) -> tuple[DocumentEdits, ...]:
    """Check coverage first; single-target definitions cannot prove shared-file instances."""
    coverage = server.sources.coverage(path)
    covered = {document.path for document in coverage.documents}
    edited = {path.resolve(), *(changed.resolve() for changed in changed_paths)}
    outside = edited - covered
    if outside:
        raise BulkRenameError("source coverage unavailable: edit is outside reached modules: " + str(min(outside)))
    inspected = tuple(DocumentEdits(document.path, document.source, ()) for document in coverage.documents
                      if server.sources.named(document.source, name))
    coverage.require_single_instance(edited | {document.path for document in inspected})
    return inspected


@dataclass(frozen=True)
class CollapsedFieldShorthand:
    """An explicit field and binding that the refactor combines into one shorthand token."""

    offset: int
    before_offsets: tuple[int, int]


def collapsed_field_shorthands(sources: RunSourceIndex, document: DocumentEdits, edited: str,
                               rename: BindingRename) -> tuple[CollapsedFieldShorthand, ...]:
    before, after = sources.tokens(document.source), sources.tokens(edited)
    reverse = tuple(SourceEdit(mapped_offset(edit.start, document.edits),
                               mapped_offset(edit.start, document.edits) + len(edit.text),
                               document.source[edit.start:edit.end]) for edit in document.edits)
    collapsed: list[CollapsedFieldShorthand] = []
    for i, span in enumerate(after.spans):
        if (span.token.value != rename.new or not style_footprint.shorthand_field(after.words, i)
                or after.contains(after.macros, i)):
            continue
        # The unchanged delimiters bound this record field even when RA deletes `old:`
        # and retains the following binding token instead of replacing the whole field.
        begin = mapped_offset(after.spans[i - 1].end, reverse)
        end = mapped_offset(after.spans[i + 1].start, reverse)
        field = tuple(token for token in before.spans if begin <= token.start and token.end <= end)
        words = tuple(token.token.value for token in field)
        if words in ((rename.old, ":", rename.new), (rename.new, ":", rename.old)):
            collapsed.append(CollapsedFieldShorthand(span.start, (field[0].start, field[2].start)))
    return tuple(collapsed)


def verify_rename_references(server: RenameServer, path: Path, offset: int, rename: BindingRename,
                             replacements: Sequence[tuple[DocumentEdits, str]]) -> None:
    """Accept only preserved definitions; restore all server overlays on every failed proof."""
    declaration_document, declaration_text = next((document, edited) for document, edited in replacements
                                                  if document.path == path)
    renamed_offset = mapped_offset(offset, declaration_document.edits)
    declaration_span = next(span for span in server.sources.tokens(declaration_text).spans if span.start == renamed_offset)
    begin = lsp_position(declaration_text, renamed_offset)
    end = lsp_position(declaration_text, declaration_span.end)
    renamed_target = DefinitionTarget(path.as_uri(), (begin["line"], begin["character"]),
                                     (end["line"], end["character"]))
    obligations: dict[Path, list[ReferenceMustResolve]] = {}
    changed_paths = {document.path for document, _ in replacements}
    inspected = [*replacements, *((document, document.source)
                                 for document in crate_name_documents(server, path, rename.new, tuple(changed_paths))
                                 if document.path not in changed_paths)]
    for document, edited in inspected:
        checks: list[ReferenceMustResolve] = []
        collapsed = collapsed_field_shorthands(server.sources, document, edited, rename) if document.edits else ()
        combined = {point for shorthand in collapsed for point in shorthand.before_offsets}
        # Existing uses of the destination name must not become captured by the new declaration.
        untouched = tuple(point for point in server.sources.named(document.source, rename.new)
                          if point not in combined and not any(edit.start <= point < edit.end for edit in document.edits))
        requested = (*untouched, *(point for shorthand in collapsed for point in shorthand.before_offsets))
        if requested:
            server.open(document.path, document.source)
        answers = server.definitions(document.path, document.source, requested) if requested else ()
        if len(answers) != len(requested):
            raise BulkRenameError("rename resolution unavailable: incomplete before-definition response")
        for point, answer in zip(requested, answers, strict=True):
            line = document.source.count("\n", 0, point) + 1
            if isinstance(answer, DefinitionUnavailable):
                raise BulkRenameError(f"rename resolution unavailable for {rename.new!r} at line {line} in {document.path.name}: {answer.reason}")
        for point, answer in zip(untouched, answers[:len(untouched)], strict=True):
            if isinstance(answer, DefinitionUnavailable):
                raise AssertionError("unavailable definitions were refused above")
            checks.append(ReferenceMustResolve(mapped_offset(point, document.edits), reference_after_edits(answer, replacements)))
        for i, shorthand in enumerate(collapsed):
            pair = answers[len(untouched) + i * 2:len(untouched) + i * 2 + 2]
            expected = field_shorthand_targets(tuple(target for target in pair if isinstance(target, DefinitionTarget)),
                                               server.sources, frozenset({rename.old, rename.new}))
            if isinstance(expected, DefinitionUnavailable):
                raise BulkRenameError("rename resolution unavailable for collapsed field shorthand: " + expected.reason)
            mapped = reference_after_edits(expected, replacements)
            if not isinstance(mapped, FieldShorthandTargets) or renamed_target not in (mapped.field, mapped.binding):
                raise BulkRenameError("rename resolution unavailable: collapsed field shorthand does not refer to the renamed declaration")
            checks.append(ReferenceMustResolve(shorthand.offset, mapped))
        intervals = tuple((mapped_offset(edit.start, document.edits), len(edit.text)) for edit in document.edits)
        shorthand_offsets = {shorthand.offset for shorthand in collapsed}
        checks.extend(ReferenceMustResolve(point, renamed_target) for point in server.sources.named(edited, rename.new)
                      if point not in shorthand_offsets and any(start <= point < start + size for start, size in intervals))
        obligations[document.path] = checks
    try:
        for document, edited in replacements:
            server.open(document.path, edited)
        for document, edited in inspected:
            checks = obligations[document.path]
            answers = server.definitions(document.path, edited, tuple(check.offset for check in checks))
            if len(answers) != len(checks):
                raise BulkRenameError("rename resolution unavailable: incomplete after-definition response")
            for check, answer in zip(checks, answers, strict=True):
                line = edited.count("\n", 0, check.offset) + 1
                if isinstance(answer, DefinitionUnavailable):
                    raise BulkRenameError(f"rename resolution unavailable for {rename.new!r} at line {line}: {answer.reason}")
                if answer != check.target:
                    cause = "rename would capture" if document.path in changed_paths else "rename would cause glob capture in an unedited file for"
                    raise BulkRenameError(f"{cause} {rename.new!r} at line {line}: definition changed in {document.path.name}")
    except BaseException:
        for document, _ in replacements:
            server.open(document.path, document.source)
        raise


def file_edits_preserved(file: FileRenames, after: str) -> bool:
    """Formatting may change whitespace only, never RA's tokens or the approved comments."""
    try:
        before_spans, after_spans = style_footprint.token_spans(file.edited), style_footprint.token_spans(after)
    except style_footprint.UnreadableTokenSpans:
        return False
    return (tuple(span.token for span in before_spans) == tuple(span.token for span in after_spans)
            and style_footprint.comment_gaps(file.edited, before_spans, lambda text: text)
            == style_footprint.comment_gaps(after, after_spans, lambda text: text))


def rename_file(server: RenameServer, path: Path, relative: str, source: str,
                findings: tuple[RenameFinding, ...], docs: tuple[str, ...] = ()) -> FileRenames:
    refused: list[Refusal] = []
    located: list[tuple[RenameFinding, RenameDeclaration]] = []
    for finding in findings:
        try:
            declaration = rename_declaration(source, finding.rename)
            if isinstance(declaration, NoBindingScope):
                raise BulkRenameError(declaration.reason)
            located.append((finding, declaration))
        except (BulkRenameError, style_footprint.UnreadableTokenSpans) as error:
            refused.append(Refusal(finding.finding_id, str(error)))
    server.open(path, source)
    current = source
    kept: list[KeptRename] = []
    offsets = {finding.finding_id: declaration.offset for finding, declaration in located}
    changed: dict[Path, RenamedDocument] = {}
    occupied: dict[Path, list[tuple[int, int]]] = defaultdict(list)
    root = path
    for _ in Path(relative).parts:
        root = root.parent
    for finding, _ in sorted(located, key=lambda item: item[1].offset, reverse=True):
        offset = offsets[finding.finding_id]
        line = current.count("\n", 0, offset) + 1
        rename = replace(finding.rename, line=line, text=current.splitlines()[line - 1])
        try:
            declaration = rename_declaration(current, rename)
            if isinstance(declaration, NoBindingScope) or declaration.offset != offset:
                raise BulkRenameError("earlier rename changed this finding's declaration")
            rename_guard(server, path, current, declaration, rename)
            comments = refactor_comments(current, declaration, rename)
            if docs_name_binding(docs, comments, rename.old):
                raise BulkRenameError("repository Markdown names the parameter; requires a coordinated documentation edit")
            workspace = server.rename(path, current, offset, rename.new)
            documents: list[DocumentEdits] = []
            for document in workspace.documents:
                if not document.path.is_relative_to(root) or crate_directory(document.path) != crate_directory(path):
                    raise BulkRenameError("WorkspaceEdit leaves the finding's crate: " + str(document.path))
                text_name_guard(server, document, rename)
                public_edit_guard(document, rename)
                edits = list(document.edits)
                if document.path == path:
                    edits.extend(SourceEdit(edit.start, edit.end, edit.replacement) for edit in comments.edits
                                 if not any(other.start < edit.end and edit.start < other.end for other in edits))
                if any(edit.start < end and start < edit.end for edit in edits for start, end in occupied[document.path]):
                    raise BulkRenameError("WorkspaceEdit overlaps an earlier finding's edit")
                documents.append(replace(document, edits=tuple(edits)))
            if not any(document.path == path and any(edit.start <= offset < edit.end for edit in document.edits)
                       for document in documents):
                raise BulkRenameError("WorkspaceEdit does not rename the finding's declaration")
            # Validate every document before changing any server document.
            replacements = [(document, apply_edits(document.source, document.edits)) for document in documents]
            verify_rename_references(server, path, offset, rename, replacements)
        except (BulkRenameError, OSError, UnicodeError, ValueError) as error:
            refused.append(Refusal(finding.finding_id, str(error)))
            continue
        for document, edited in replacements:
            target = document.path
            previous = changed.get(target)
            changed[target] = RenamedDocument(target.relative_to(root).as_posix(),
                                             previous.original if previous else document.source, edited)
            occupied[target] = [(mapped_offset(start, document.edits), mapped_offset(end, document.edits))
                                for start, end in occupied[target]]
            for edit in document.edits:
                start = mapped_offset(edit.start, document.edits)
                occupied[target].append((start, start + len(edit.text)))
            if target == path:
                current = edited
                for identity, previous_offset in offsets.items():
                    try:
                        offsets[identity] = mapped_offset(previous_offset, document.edits)
                    except BulkRenameError:
                        offsets[identity] = -1
        kept.append(KeptRename(finding, declaration.scope.kind, 0))
    return FileRenames(relative, source, current, tuple(kept), tuple(refused),
                       tuple(document for target, document in changed.items() if target != path),
                       frozenset(document.path for document in changed.values()))


def combine_file_renames(files: Sequence[FileRenames]) -> tuple[FileRenames, ...]:
    """Collapse sequential overlays; every document in a WorkspaceEdit reverts together."""
    merged: dict[str, FileRenames] = {}
    for file in files:
        for document in (RenamedDocument(file.path, file.original, file.edited), *file.related):
            previous = merged.get(document.path)
            own = document.path == file.path
            merged[document.path] = FileRenames(document.path, previous.original if previous else document.original,
                                                document.edited,
                                                (*(previous.kept if previous else ()), *(file.kept if own else ())),
                                                (*(previous.refusals if previous else ()), *(file.refusals if own else ())),
                                                dependencies=(previous.dependencies if previous else frozenset[str]()) | file.dependencies)
    return tuple(merged.values())


def command(repo: Path, arguments: Sequence[str]) -> None:
    print("bulk rename: " + " ".join(arguments), flush=True)
    result = subprocess.run(arguments, cwd=repo, check=False)
    if result.returncode:
        raise BulkRenameError(f"{' '.join(arguments)} exited {result.returncode}")


def verification_packages(repo: Path, crate: str) -> tuple[str, ...]:
    result = subprocess.run(["cargo", "metadata", "--no-deps", "--format-version", "1"], cwd=repo,
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise BulkRenameError(result.stderr.strip())
    metadata = object_fields(cast(object, json.loads(result.stdout)))
    members = set(str(item) for item in object_list(metadata.get("workspace_members")))
    packages = [object_fields(item) for item in object_list(metadata.get("packages"))]
    packages = [package for package in packages if package.get("id") in members]
    manifests = {str(package["name"]): Path(str(package["manifest_path"])).parent.resolve() for package in packages}
    selected = {name for name, root in manifests.items() if root == (repo / crate).resolve()}
    if not selected:
        raise BulkRenameError(f"{crate}: no workspace package")
    while True:
        expanded = selected | {str(package["name"]) for package in packages
                               if any(object_fields(dep).get("name") in selected and object_fields(dep).get("path")
                                      for dep in object_list(package.get("dependencies")))}
        if expanded == selected:
            return tuple(sorted(selected))
        selected = expanded


def atomic_json(path: Path, value: object) -> None:
    """Durably publish complete batch state before any source edit."""
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            _ = stream.write(json.dumps(value, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


class UnfinishedFileEdit(TypedDict):
    before: str
    before_sha256: str
    after: str


class UnfinishedBatch(TypedDict):
    batch: str
    operation: str
    base: str
    branch: str
    files: dict[str, UnfinishedFileEdit]
    tickets: list[str]
    findings: list[str]


class BatchIntent:
    """An interrupted batch may restore only its exact recorded edits, then start again."""

    def __init__(self, repo: Path, operation: str) -> None:
        self.repo: Path = repo
        self.operation: str = operation
        self.path: Path = Path(self.git("rev-parse", "--path-format=absolute", "--git-path", "style-batch-intent.json"))
        self.batch_id: str = uuid.uuid4().hex

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", *arguments], cwd=self.repo, capture_output=True, text=True, check=True).stdout.strip()

    def refuse(self, reason: str) -> BulkRenameError:
        archive = shlex.quote(str(self.path) + ".reviewed")
        return BulkRenameError(f"unfinished batch {self.path}: {reason}; preserve and review the worktree and intent, "
                               + f"then clear the intent with mv -- {shlex.quote(str(self.path))} {archive} and rerun {self.operation}")

    def file_edits(self, files: tuple[FileRenames, ...]) -> dict[str, UnfinishedFileEdit]:
        edits: dict[str, UnfinishedFileEdit] = {}
        for file in files:
            before = (self.repo / file.path).read_bytes()
            edits[file.path] = {"before": before.hex(), "before_sha256": hashlib.sha256(before).hexdigest(),
                                "after": file.edited.encode().hex()}
        return edits

    def begin(self, files: tuple[FileRenames, ...], tickets: Sequence[str] = (),
              findings: Sequence[str] = ()) -> None:
        if self.path.exists():
            raise self.refuse("another batch has not finished")
        self.batch_id = uuid.uuid4().hex
        record: UnfinishedBatch = {"batch": self.batch_id, "operation": self.operation,
                                   "base": self.git("rev-parse", "HEAD"), "branch": self.git("symbolic-ref", "HEAD"),
                                   "files": self.file_edits(files), "tickets": list(tickets), "findings": list(findings)}
        atomic_json(self.path, record)

    def record_edits(self, files: tuple[FileRenames, ...]) -> None:
        """Extend the pre-claim intent without losing its planned ticket ownership."""
        record = object_fields(cast(object, json.loads(self.path.read_text())))
        record["files"] = self.file_edits(files)
        atomic_json(self.path, record)

    def recover(self) -> tuple[str, ...]:
        """Restore before bytes only when every dirty path is an exact recorded before/after state."""
        if not self.path.exists():
            return ()
        try:
            value = object_fields(cast(object, json.loads(self.path.read_text())))
            batch_id = value["batch"]
            if not isinstance(batch_id, str) or not batch_id:
                raise ValueError("invalid batch owner")
            self.batch_id = batch_id
            files = object_fields(value["files"])
            tickets = object_list(value["tickets"])
            if not all(isinstance(ticket, str) for ticket in tickets):
                raise ValueError("invalid tickets")
            if (value["operation"] != self.operation or value["base"] != self.git("rev-parse", "HEAD")
                    or value["branch"] != self.git("symbolic-ref", "HEAD")):
                raise self.refuse("command, branch or HEAD changed")
            if self.git("diff", "--cached", "--name-only"):
                raise self.refuse("index changed")
            changed = set(filter(None, self.git("diff", "HEAD", "--name-only", "-z").split("\0")))
            if changed - files.keys():
                raise self.refuse("unrecorded dirty files: " + ", ".join(sorted(changed - files.keys())))
            originals: dict[Path, bytes] = {}
            for name, raw in files.items():
                if Path(name).is_absolute() or ".." in Path(name).parts:
                    raise ValueError("invalid recorded path")
                target = self.repo / name
                if target.resolve() != target or not target.is_file():
                    raise self.refuse(f"{name}: file was replaced or removed")
                edit = object_fields(raw)
                before, after, digest = edit["before"], edit["after"], edit["before_sha256"]
                if not isinstance(before, str) or not isinstance(after, str):
                    raise ValueError("invalid source bytes")
                data = bytes.fromhex(before)
                if hashlib.sha256(data).hexdigest() != digest or target.read_bytes() not in (data, bytes.fromhex(after)):
                    raise self.refuse(f"{name}: bytes do not match the recorded edit")
                originals[target] = data
            # Check the whole set before restoring any file. Repeated recovery accepts before bytes too.
            for target, data in originals.items():
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    _ = stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.chmod(target.stat().st_mode)
                os.replace(temporary, target)
            return tuple(cast(list[str], tickets))
        except (KeyError, ValueError, OSError) as error:
            raise self.refuse(f"cannot read or restore recorded state: {error}") from error

    def finish(self) -> None:
        self.path.unlink(missing_ok=True)


class BatchWorkspace:
    """The clean tracked files before the batch; restore them between failed file subsets."""

    def __init__(self, repo: Path, files: tuple[FileRenames, ...], packages: tuple[str, ...]) -> None:
        self.repo: Path = repo
        self.files: dict[str, FileRenames] = {file.path: file for file in files}
        self.packages: tuple[str, ...] = packages
        names = subprocess.run(["git", "ls-files", "-z"], cwd=repo, capture_output=True, check=True).stdout.decode().split("\0")
        self.originals: dict[str, bytes] = {name: (repo / name).read_bytes() for name in names if name and not (repo / name).is_symlink()}
        self.last_good: dict[str, bytes] = {}

    def restore(self) -> None:
        for path, data in self.originals.items():
            target = self.repo / path
            if not target.exists() or target.read_bytes() != data:
                target.parent.mkdir(parents=True, exist_ok=True)
                _ = target.write_bytes(data)

    def validate(self, paths: tuple[str, ...]) -> bool:
        self.restore()
        if any(not self.files[path].dependencies <= set(paths) for path in paths):
            return False
        for path in paths:
            _ = (self.repo / path).write_text(self.files[path].edited)
        try:
            for package in self.packages:
                command(self.repo, ["cargo", "+nightly", "fmt", "--package", package])
            verify = str(Path.home() / ".claude/scripts/direct/verify.sh")
            for package in self.packages:
                for verb in ("check", "lint", "test"):
                    command(self.repo, [verify, verb, package])
            selected = set(paths)
            for path, data in self.originals.items():
                if path not in selected and (not (self.repo / path).is_file() or (self.repo / path).read_bytes() != data):
                    raise BulkRenameError(f"verification changed an unrelated file: {path}")
            for path in paths:
                file = self.files[path]
                if not file_edits_preserved(file, (self.repo / path).read_text()):
                    raise BulkRenameError(f"verification changed more than the approved renames in {path}")
            self.last_good = {path: (self.repo / path).read_bytes() for path in paths}
            return True
        except (BulkRenameError, OSError) as error:
            print(f"bulk rename: validation failed: {error}", file=sys.stderr, flush=True)
            return False

    def keep(self) -> None:
        self.restore()
        for path, data in self.last_good.items():
            _ = (self.repo / path).write_bytes(data)


def validated_files(paths: tuple[str, ...], validate: Callable[[tuple[str, ...]], bool]) -> tuple[str, ...]:
    """On failure, split by file with the passing prefix present, so interacting files are checked together."""
    if not paths or validate(paths):
        return paths
    if not validate(()):
        return ()
    kept: tuple[str, ...] = ()

    def split(candidates: tuple[str, ...]) -> None:
        nonlocal kept
        if validate((*kept, *candidates)):
            kept += candidates
        elif len(candidates) > 1:
            middle = len(candidates) // 2
            split(candidates[:middle])
            split(candidates[middle:])

    middle = max(1, len(paths) // 2)
    split(paths[:middle])
    if paths[middle:]:
        split(paths[middle:])
    return kept


def sample_findings(findings: tuple[KeptRename, ...], limit: int = 60) -> tuple[KeptRename, ...]:
    strata: dict[str, list[KeptRename]] = defaultdict(list)
    rng = random.SystemRandom()
    for finding in findings:
        strata[finding.kind].append(finding)
    for group in strata.values():
        rng.shuffle(group)
    sample: list[KeptRename] = []
    while len(sample) < min(limit, len(findings)):
        for kind in sorted(strata):
            if strata[kind] and len(sample) < limit:
                sample.append(strata[kind].pop())
    return tuple(sample)


def check_available(repo: Path, path: str) -> None:
    board = berth_board.read_board(repo)
    if isinstance(board, berth_board.BoardUnavailable):
        raise BulkRenameError(f"cannot check reservation availability: {board.reason}")
    if any(entry.blocks(path) and entry.holder_worktree_root.resolve() != repo for entry in board.reservations()):
        raise BulkRenameError(f"{path}: another worktree holds the file")


def run_batch(repo: Path, ticket_id: str) -> Path:
    repo = repo.resolve()
    intent = BatchIntent(repo, f"bulk_rename --ticket {shlex.quote(ticket_id)} --repo {shlex.quote(str(repo))}")
    _ = intent.recover()
    intent.finish()
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo,
                            capture_output=True, text=True, check=True)
    if status.stdout:
        raise BulkRenameError("bulk rename requires a clean tracked worktree and index")
    if shutil.which("rust-analyzer") is None:
        raise BulkRenameError("rust-analyzer is absent")
    with style_ledger.open_ledger(repo) as ledger:
        ticket = ledger.bulk_ticket(ticket_id)
        branch = subprocess.run(["git", "symbolic-ref", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
        if branch != ticket["branch"]:
            raise BulkRenameError("bulk ticket is assigned to a different branch")
        base = ledger.ticket_base(ticket_id)
        held = ledger.bulk_findings(ticket_id)
        groups: dict[str, list[RenameFinding]] = defaultdict(list)
        docs = repository_docs(repo)
        refused = [Refusal(row.finding_id, row.reason) for row in ledger.bulk_refusals(ticket_id)]
        for finding in held:
            rename = refactor_rename(finding.kind, finding.before, finding.after, finding.line, finding.text)
            path = repo / finding.path
            if not isinstance(rename, BindingRename):
                refused.append(Refusal(finding.finding_id, "finding does not specify one identifier rename"))
            elif path.resolve() != path or not path.is_file() or not finding.path.endswith(".rs"):
                refused.append(Refusal(finding.finding_id, "finding path is not a regular Rust file"))
            else:
                groups[finding.path].append(RenameFinding(finding.finding_id, finding.path, rename))
        packages = verification_packages(repo, ticket["crate"])
        files: list[FileRenames] = []
        with RustAnalyzer(repo) as server:
            for path, findings in groups.items():
                try:
                    check_available(repo, path)
                    previous = subprocess.run(["git", "show", f"{base}:{path}"], cwd=repo,
                                              capture_output=True, check=False)
                    if previous.returncode or previous.stdout != (repo / path).read_bytes():
                        raise BulkRenameError("file changed since the ticket base")
                    file = rename_file(server, repo / path, path, server.sources.read(repo / path).source, tuple(findings), docs)
                    files.append(file)
                    refused.extend(file.refusals)
                except BulkRenameError as error:
                    refused.extend(Refusal(finding.finding_id, str(error)) for finding in findings)
        candidates = tuple(file for file in combine_file_renames(files) if file.edited != file.original)
        workspace = BatchWorkspace(repo, candidates, packages)
        try:
            # Check again immediately before publishing the in-memory edits.
            for file in candidates:
                check_available(repo, file.path)
            intent.begin(candidates)
            good = validated_files(tuple(file.path for file in candidates), workspace.validate)
            workspace.keep()
            for file in candidates:
                if file.path not in good:
                    refused.extend(Refusal(item.finding.finding_id, "crate verification failed for this file") for item in file.kept)
            kept = tuple(item for file in candidates if file.path in good for item in file.kept)
            folder = ledger.database.parent / "bulk_review"
            folder.mkdir(parents=True, exist_ok=True)
            review = folder / f"{ticket_id}.json"
            sample = sample_findings(kept)
            atomic_json(review, {
                "ticket": ticket_id, "base": ticket["base"] or ledger.ticket_base(ticket_id),
                "status": "awaiting independent review; built at normal ticket merge",
                "on_error": "return the entire batch to review",
                "kept": [item.finding.finding_id for item in kept],
                "macro_edits": sum(item.macro_edits for item in kept),
                "refusals": [asdict(refusal) for refusal in refused],
                "sample": [asdict(item) for item in sample],
                "files": {path: {"before": workspace.files[path].original, "after": (repo / path).read_text()} for path in good},
            })
            _ = ledger.bulk_release(ticket_id, {refusal.finding_id: refusal.reason for refusal in refused})
        except BaseException:
            workspace.restore()
            intent.finish()
            raise
        intent.finish()
        print(f"bulk rename: {len(kept)} kept, {len(refused)} refused, {sum(item.macro_edits for item in kept)} macro edits; review {review}")
        return review


class Arguments(argparse.Namespace):
    ticket: str = ""
    repo: Path = Path()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--ticket", required=True)
    _ = parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv, namespace=Arguments())
    try:
        _ = run_batch(args.repo, args.ticket)
        return 0
    except (BulkRenameError, style_ledger.LedgerError, OSError, subprocess.SubprocessError) as error:
        print(f"bulk rename: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
