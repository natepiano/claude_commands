#!/usr/bin/env python3
"""Apply approved local renames with rust-analyzer, the shared proof and crate verification.

Nothing is committed. Refused findings return to ordinary tickets; kept findings become built at the normal merge.
The review file samples the verified findings that the batch will build.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import random
import shutil
import subprocess
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Protocol, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import berth_board
import style_footprint
import style_ledger
from approved_renames import (AppliedInCode, BindingComments, BindingRename, BindingScope, NoBindingScope,
                              backtick_mentions, binding_comments, binding_rename, binding_rename_check, binding_scope,
                              identifier_mentions)


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
class LocatedRename:
    finding: RenameFinding
    scope: BindingScope
    offset: int


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


@dataclass(frozen=True)
class SourceEdit:
    start: int
    end: int
    text: str


class RenameServer(Protocol):
    def open(self, path: Path, text: str) -> None: ...
    def change(self, path: Path, text: str) -> None: ...
    def rename(self, path: Path, text: str, offset: int, new: str) -> tuple[SourceEdit, ...]: ...


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


def workspace_edits(value: object, path: Path, text: str, version: int) -> tuple[SourceEdit, ...]:
    """Accept text edits for this document only; never silently discard a resource or another document edit."""
    fields = object_fields(value)
    edits: list[SourceEdit] = []
    documents: list[tuple[str, list[object]]] = []
    if "changes" in fields:
        for uri, rows in object_fields(fields["changes"]).items():
            documents.append((uri, object_list(rows)))
    if "documentChanges" in fields:
        for change in object_list(fields["documentChanges"]):
            row = object_fields(change)
            document = object_fields(row.get("textDocument"))
            if document.get("version") not in (None, version):
                raise BulkRenameError("rust-analyzer edited a stale document version")
            documents.append((str(document.get("uri")), object_list(row.get("edits"))))
    for uri, rows in documents:
        if uri != path.as_uri():
            raise BulkRenameError("rust-analyzer requested edits in another file")
        for item in rows:
            row = object_fields(item)
            bounds = object_fields(row.get("range"))
            replacement = row.get("newText")
            if not isinstance(replacement, str):
                raise BulkRenameError("LSP edit has no replacement text")
            edits.append(SourceEdit(source_offset(text, bounds.get("start")),
                                    source_offset(text, bounds.get("end")), replacement))
    if not edits:
        raise BulkRenameError("rust-analyzer returned no edits")
    return tuple(edits)


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
        self.versions[path] = 1
        self.notify("textDocument/didOpen", {"textDocument": {
            "uri": path.as_uri(), "languageId": "rust", "version": 1, "text": text}})

    def change(self, path: Path, text: str) -> None:
        self.versions[path] += 1
        self.notify("textDocument/didChange", {"textDocument": {"uri": path.as_uri(), "version": self.versions[path]},
                                               "contentChanges": [{"text": text}]})

    def rename(self, path: Path, text: str, offset: int, new: str) -> tuple[SourceEdit, ...]:
        params: dict[str, object] = {"textDocument": {"uri": path.as_uri()}, "position": lsp_position(text, offset)}
        if self.request("textDocument/prepareRename", params) is None:
            raise BulkRenameError("rust-analyzer refused prepareRename")
        value = self.request("textDocument/rename", {**params, "newName": new})
        return workspace_edits(value, path, text, self.versions[path])


class BindingText:
    """Token locations for bulk exclusions and macro repairs; binding visibility comes from the shared proof."""

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

    def call_argument(self, scope: BindingScope, old: str) -> bool:
        for index, word in enumerate(self.words):
            if (word != old or index == scope.declaration or not self.contains(scope.ranges, index)
                    or self.contains(self.macros, index) or self.words[index + 1:index + 2] not in {(",",), (")",)}):
                continue
            first = index
            while first and self.words[first - 1] in {"&", "mut", "*"}:
                first -= 1
            if first == 0 or self.words[first - 1] not in {"(", ","}:
                continue
            parent = self.parents.get(index, -1)
            if parent < 1 or self.words[parent] != "(":
                continue
            lead = self.words[parent - 1]
            # A grouping/tuple paren has no callable expression before it. Function parameter lists are before body scopes.
            if (style_footprint.IDENTIFIER.fullmatch(lead) and lead not in style_footprint.RUST_KEYWORDS
                    or lead in {")", "]", ">", ">>"}):
                return True
        return False

    def name_word(self, index: int) -> bool:
        previous = self.words[index - 1] if index else ""
        following = self.words[index + 1] if index + 1 < len(self.words) else ""
        parent = self.parents.get(index, -1)
        return (previous not in {".", "::", "'"} and following not in {"::", "!"}
                and not (following == ":" and previous in {"{", ","} and parent >= 0 and self.words[parent] == "{"))

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
            if "derive" in attributes and attributes & {"Serialize", "Deserialize", "Parser", "Args", "Subcommand", "Config"}:
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

    def macro_repairs(self, scope: BindingScope, rename: BindingRename,
                      semantic: tuple[SourceEdit, ...]) -> tuple[SourceEdit, ...]:
        repairs: list[SourceEdit] = []
        for index, span in enumerate(self.spans):
            if not self.contains(scope.ranges, index) or not self.contains(self.macros, index):
                continue
            word = span.token.value
            if word == rename.old:
                if any(edit.start < span.end and span.start < edit.end for edit in semantic):
                    continue
                previous = self.words[index - 1] if index else ""
                following = self.words[index + 1] if index + 1 < len(self.words) else ""
                if previous in {".", "::", "'"} or following in {"::", "!", ":"}:
                    continue
                parent = self.parents.get(index, -1)
                shorthand = parent >= 0 and self.words[parent] == "{" and style_footprint.shorthand_field(self.words, index)
                repairs.append(SourceEdit(span.start, span.end, f"{word}: {rename.new}" if shorthand else rename.new))
            elif index in self.formats:
                # Only main captures are rewritten. The proof checks the macro's format argument and all remaining uses.
                cursor = 0
                while cursor < len(word):
                    if word[cursor:cursor + 2] in {"{{", "}}"}:
                        cursor += 2
                        continue
                    if word[cursor] == "{":
                        start = cursor + 1
                        end = start + len(rename.old)
                        overlaps = any(edit.start < span.start + end and span.start + start < edit.end for edit in semantic)
                        if word[start:end] == rename.old and word[end:end + 1] in {"}", ":"} and not overlaps:
                            repairs.append(SourceEdit(span.start + start, span.start + end, rename.new))
                        close = word.find("}", start)
                        cursor = len(word) if close < 0 else close + 1
                    else:
                        cursor += 1
        return tuple(repairs)


def repository_docs(repo: Path) -> tuple[str, ...]:
    """Read repository Markdown once per batch; bulk edits never write these files."""
    root = repo.resolve()
    return tuple(path.read_text() for path in sorted(root.rglob("*.md"))
                 if ".git" not in path.relative_to(root).parts and path.is_file() and path.resolve().is_relative_to(root))


def docs_name_binding(docs: tuple[str, ...], comments: BindingComments, old: str) -> bool:
    return any(identifier_mentions(document, function) and backtick_mentions(document, old)
               for document in docs for function in comments.functions)


def rename_file(server: RenameServer, path: Path, relative: str, source: str,
                findings: tuple[RenameFinding, ...], docs: tuple[str, ...] = ()) -> FileRenames:
    try:
        original = BindingText(source)
    except BulkRenameError as error:
        return FileRenames(relative, source, source, (), tuple(Refusal(f.finding_id, str(error)) for f in findings))
    located: list[LocatedRename] = []
    refused: list[Refusal] = []
    for finding in findings:
        rename = finding.rename
        scope = binding_scope(source, rename)
        comments = binding_comments(source, rename)
        reason = ""
        if isinstance(scope, NoBindingScope):
            reason = "serde/clap/confique derive field" if original.derive_field(rename) else scope.reason
        elif isinstance(comments, NoBindingScope):
            reason = comments.reason
        elif docs_name_binding(docs, comments, rename.old):
            reason = "docs name the binding"
        elif len(original.spans) != len(style_footprint.tokens(source)):
            reason = "source token offsets are unavailable"
        elif original.call_argument(scope, rename.old):
            reason = "binding is passed straight to a call"
        else:
            located.append(LocatedRename(finding, scope, original.spans[scope.declaration].start))
        if reason:
            refused.append(Refusal(finding.finding_id, reason))
    server.open(path, source)
    current = source
    kept: list[KeptRename] = []
    offsets = {location.finding.finding_id: location.offset for location in located}
    for location in sorted(located, key=lambda location: location.offset, reverse=True):
        finding = location.finding
        offset = offsets[finding.finding_id]
        position = lsp_position(current, offset)
        line = position["line"] + 1
        rename = replace(finding.rename, line=line, text=current.splitlines()[line - 1])
        scope = binding_scope(current, rename)
        if isinstance(scope, NoBindingScope):
            refused.append(Refusal(finding.finding_id, scope.reason))
            continue
        comments = binding_comments(current, rename)
        if isinstance(comments, NoBindingScope):
            refused.append(Refusal(finding.finding_id, comments.reason))
            continue
        try:
            semantic = server.rename(path, current, offset, rename.new)
            repairs = BindingText(current).macro_repairs(scope, rename, semantic)
            edits = (*semantic, *repairs, *(SourceEdit(edit.start, edit.end, edit.replacement) for edit in comments.edits))
            edited = apply_edits(current, edits)
            proof = binding_rename_check(relative, current, edited, (rename,))
            if not proof.explained or not isinstance(proof.checks.get(rename), AppliedInCode):
                raise BulkRenameError(proof.refusals.get(rename, "edit is not a complete proven rename"))
        except BulkRenameError as error:
            refused.append(Refusal(finding.finding_id, str(error)))
            continue
        server.change(path, edited)
        current = edited
        for identity, previous in offsets.items():
            offsets[identity] = previous + sum(len(edit.text) - (edit.end - edit.start) for edit in edits if edit.end <= previous)
        kept.append(KeptRename(finding, scope.kind, len(repairs)))
    proof = binding_rename_check(relative, source, current, tuple(item.finding.rename for item in kept))
    if not proof.explained or any(not isinstance(check, AppliedInCode) for check in proof.checks.values()):
        refused.extend(Refusal(item.finding.finding_id, "whole-file proof failed") for item in kept)
        kept.clear()
        current = source
        server.change(path, source)
    return FileRenames(relative, source, current, tuple(kept), tuple(refused))


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
                proof = binding_rename_check(path, file.original, (self.repo / path).read_text(),
                                             tuple(item.finding.rename for item in file.kept))
                if not proof.explained or any(not isinstance(check, AppliedInCode) for check in proof.checks.values()):
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
        refused: list[Refusal] = []
        for finding in held:
            rename = binding_rename(finding.kind, finding.before, finding.after, finding.line, finding.text)
            path = repo / finding.path
            if not isinstance(rename, BindingRename):
                refused.append(Refusal(finding.finding_id, "finding is not a local binding name pair"))
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
                    file = rename_file(server, repo / path, path, (repo / path).read_text(), tuple(findings), docs)
                    files.append(file)
                    refused.extend(file.refusals)
                except BulkRenameError as error:
                    refused.extend(Refusal(finding.finding_id, str(error)) for finding in findings)
        candidates = tuple(file for file in files if file.kept)
        workspace = BatchWorkspace(repo, candidates, packages)
        try:
            # Check again immediately before publishing the in-memory edits.
            for file in candidates:
                check_available(repo, file.path)
            good = validated_files(tuple(file.path for file in candidates), workspace.validate)
            workspace.keep()
            for file in candidates:
                if file.path not in good:
                    refused.extend(Refusal(item.finding.finding_id, "crate verification failed for this file") for item in file.kept)
            kept = tuple(item for file in candidates if file.path in good for item in file.kept)
            _ = ledger.bulk_release(ticket_id, {refusal.finding_id: refusal.reason for refusal in refused})
            folder = ledger.database.parent / "bulk_review"
            folder.mkdir(parents=True, exist_ok=True)
            review = folder / f"{ticket_id}.json"
            sample = sample_findings(kept)
            _ = review.write_text(json.dumps({
                "ticket": ticket_id, "base": ticket["base"] or ledger.ticket_base(ticket_id),
                "status": "awaiting independent review; built at normal ticket merge",
                "on_error": "return the entire batch to review",
                "kept": [item.finding.finding_id for item in kept],
                "macro_edits": sum(item.macro_edits for item in kept),
                "refusals": [asdict(refusal) for refusal in refused],
                "sample": [asdict(item) for item in sample],
                "files": {path: {"before": workspace.files[path].original, "after": (repo / path).read_text()} for path in good},
            }, indent=2) + "\n")
        except BaseException:
            workspace.restore()
            raise
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
