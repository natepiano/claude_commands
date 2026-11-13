"""Store immutable source facts and replace candidate partitions with their dependencies."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import cast

from scripts.style import candidate_generators as generators
from scripts.style.git_process import TreeEntry
from scripts.style.refill_inputs import RefillInputs, UnstoredFact, UnstoredTree, input_identity

# Increase this version when a scanner or a partition policy changes.
CANDIDATE_READER_VERSION = 2


@dataclass(frozen=True)
class CandidatePartition:
    """An ordered enumeration with all inputs that can change its result."""

    key: str
    dependencies: tuple[tuple[str, str, str], ...]


class RefillFacts:
    """Own source facts, candidate partitions, and candidate dependency edges."""

    def __init__(self, connection: sqlite3.Connection, object_store: str, catalog: str) -> None:
        self.connection: sqlite3.Connection = connection
        self.object_store: str = object_store
        self.catalog: str = catalog
        self.used: dict[str, set[str]] = {}
        self.failed_reads: int = 0
        self.failed_blobs: set[str] = set()

    def fact(self, object_id: str, kind: str, key: str = "") -> str | UnstoredFact:
        row = cast(tuple[str] | None, self.connection.execute(
            "SELECT fact_text FROM refill_objects WHERE object_store=? AND object_id=? "
            + "AND reader_version=? AND fact_kind=? AND fact_key=?",
            (self.object_store, object_id, CANDIDATE_READER_VERSION, kind, key)).fetchone())
        return UnstoredFact() if row is None else row[0]

    def store_fact(self, object_id: str, kind: str, key: str, value: str) -> None:
        _ = self.connection.execute(
            "INSERT OR IGNORE INTO refill_objects "
            + "(object_store,object_id,reader_version,fact_kind,fact_key,line_count,fact_text) VALUES (?,?,?,?,?,0,?)",
            (self.object_store, object_id, CANDIDATE_READER_VERSION, kind, key, value))

    def tree(self, object_id: str) -> dict[str, TreeEntry] | UnstoredTree:
        if isinstance(self.fact(object_id, "tree"), UnstoredFact):
            return UnstoredTree()
        rows = cast(list[tuple[str, str, str, str]], self.connection.execute(
            "SELECT fact_key,entry_mode,entry_kind,entry_object FROM refill_objects "
            + "WHERE object_store=? AND object_id=? AND reader_version=? AND fact_kind='tree_entry' ORDER BY fact_key",
            (self.object_store, object_id, CANDIDATE_READER_VERSION)).fetchall())
        return {name: TreeEntry(mode, kind, value) for name, mode, kind, value in rows}

    def store_tree(self, object_id: str, entries: Mapping[str, TreeEntry]) -> None:
        _ = self.connection.executemany(
            "INSERT OR IGNORE INTO refill_objects "
            + "(object_store,object_id,reader_version,fact_kind,fact_key,line_count,entry_mode,entry_kind,entry_object) "
            + "VALUES (?,?,?,'tree_entry',?,0,?,?,?)",
            ((self.object_store, object_id, CANDIDATE_READER_VERSION, name, entry.mode, entry.kind, entry.object)
             for name, entry in entries.items()))
        self.store_fact(object_id, "tree", "", "")

    def masked(self, blob: str, text: Callable[[], str]) -> generators.MaskedSource:
        stored = self.fact(blob, "masked_source")
        if isinstance(stored, UnstoredFact):
            masked = generators.mask_source(text())
            self.store_fact(blob, "masked_source", "", json.dumps((masked.lines, [asdict(s) for s in masked.strings])))
            return masked
        lines, strings = cast(tuple[list[str], list[dict[str, str | int]]], json.loads(stored))
        return generators.MaskedSource(lines, [generators.StringLiteral(
            cast(int, item["line"]), cast(int, item["col"]), cast(str, item["text"]), cast(str, item["kind"]))
            for item in strings])

    def consumer_key(self, rule: str, partition: str) -> str:
        return json.dumps((self.catalog, rule, partition), separators=(",", ":"))

    def partition(self, rule: str, spec: object, partition: CandidatePartition,
                  calculate: Callable[[], generators.Enumeration]) -> generators.Enumeration:
        self.used.setdefault(rule, set()).add(partition.key)
        identity = input_identity((spec, partition.dependencies, CANDIDATE_READER_VERSION))
        row = cast(tuple[str, int, str] | None, self.connection.execute(
            "SELECT input_identity,generator_version,sites FROM refill_candidates WHERE catalog=? AND rule=? AND partition=?",
            (self.catalog, rule, partition.key)).fetchone())
        failed = any(kind == "blob" and value in self.failed_blobs for kind, _, value in partition.dependencies)
        if not failed and row is not None and row[:2] == (identity, CANDIDATE_READER_VERSION):
            return decode_enumeration(row[2])
        before = self.failed_reads
        result = calculate()
        if failed or self.failed_reads != before:
            return result
        _ = self.connection.execute("INSERT OR REPLACE INTO refill_candidates VALUES (?,?,?,?,?,?)",
                                    (self.catalog, rule, partition.key, identity, CANDIDATE_READER_VERSION,
                                     encode_enumeration(result)))
        key = self.consumer_key(rule, partition.key)
        _ = self.connection.execute("DELETE FROM refill_dependencies WHERE consumer_table='refill_candidates' AND consumer_key=?",
                                    (key,))
        _ = self.connection.executemany("INSERT INTO refill_dependencies VALUES ('refill_candidates',?,?,?,?)",
                                        ((key, kind, source, value) for kind, source, value in partition.dependencies))
        return result

    def finish(self) -> None:
        rows = cast(list[tuple[str, str]], self.connection.execute(
            "SELECT rule,partition FROM refill_candidates WHERE catalog=?", (self.catalog,)).fetchall())
        for rule, partition in rows:
            if partition in self.used.get(rule, set()):
                continue
            _ = self.connection.execute("DELETE FROM refill_candidates WHERE catalog=? AND rule=? AND partition=?",
                                        (self.catalog, rule, partition))
            _ = self.connection.execute(
                "DELETE FROM refill_dependencies WHERE consumer_table='refill_candidates' AND consumer_key=?",
                (self.consumer_key(rule, partition),))

    def file_enumeration(self, spec: generators.CandidatesSpec, inputs: RefillInputs, path: str) -> generators.Enumeration:
        blob = inputs.blobs[path]
        key = input_identity((asdict(spec), path))
        stored = self.fact(blob, "generator", key)
        if isinstance(stored, UnstoredFact):
            before = self.failed_reads
            result = generators.enumerate_candidates(spec, inputs.root, inputs=inputs.view((path,)))
            if self.failed_reads != before:
                return result
            self.store_fact(blob, "generator", key, encode_enumeration(result))
            return result
        return decode_enumeration(stored)

    def enumerate(self, rule: str, spec: generators.CandidatesSpec, inputs: RefillInputs) -> generators.Enumeration:
        if spec.kind == "regex":
            return self.regex(rule, spec, inputs)
        if spec.kind == "single_impl_traits":
            return self.traits(rule, spec, inputs)
        paths = inputs.rust_paths(inputs.root)
        if spec.kind != "submodule_names":
            inputs.prepare_text(paths)
        manifests = tuple((path, blob) for path, blob in sorted(inputs.blobs.items()) if path.split("/")[-1] == "Cargo.toml")
        cross: tuple[tuple[str, str, str], ...] = ()
        if spec.kind in {"workspace_deps", "bevy_kana_usage"}:
            # Membership includes absent and newly added manifests through the path-set identity.
            cross = (("manifest_membership", ".", input_identity(manifests)),
                     *(("manifest", path, blob) for path, blob in manifests))
            view = inputs.view(tuple(inputs.blobs))
            members = generators.member_manifests(inputs.root, view)
            if spec.kind == "workspace_deps":
                paths = tuple(dict.fromkeys(path.relative_to(inputs.root).as_posix() for path in members))
            else:
                manifest_paths = tuple(dict.fromkeys((inputs.root / "Cargo.toml", *members)))
                sources = tuple(path for manifest in manifest_paths for path in inputs.rust_paths(manifest.parent / "src"))
                paths = tuple(dict.fromkeys((*(path.relative_to(inputs.root).as_posix() for path in manifest_paths), *sources)))
                paths = tuple(path for path in paths if path in inputs.blobs)
                libraries = tuple(path for path in inputs.blobs.keys() | inputs.directories
                                  if path.endswith("/src/lib.rs") or path == "src/lib.rs")
                cross += (("library_membership", ".", input_identity(sorted(libraries))),)
        if not paths:
            return self.partition(rule, asdict(spec), CandidatePartition("@empty", cross),
                                  lambda: generators.enumerate_candidates(spec, inputs.root, inputs=inputs.view(())))
        results: list[generators.Candidate] = []
        source = ""
        for index, path in enumerate(paths):
            dependencies = (("blob", path, inputs.blobs[path]), *cross)
            partition = CandidatePartition(path if spec.kind != "workspace_deps" else f"{index}:{path}", dependencies)
            def calculate(path: str = path) -> generators.Enumeration:
                if cross:
                    return generators.enumerate_candidates(spec, inputs.root, inputs=inputs.view((path,)))
                return self.file_enumeration(spec, inputs, path)
            result = self.partition(rule, asdict(spec), partition, calculate)
            results.extend(result.candidates)
            source = result.source
        results.sort(key=lambda candidate: (candidate.file, candidate.line, candidate.text)
                     if spec.kind in {"field_affixes", "test_allow_boilerplate"} else (candidate.file, candidate.line))
        return generators.Enumeration(tuple(results), source)

    def regex(self, rule: str, spec: generators.CandidatesSpec, inputs: RefillInputs) -> generators.Enumeration:
        selection, ignores = inputs.regex_identity(spec)
        dependencies = (("regex_selection", ".", selection), *(("ignore", path, blob) for path, blob in ignores))
        if not inputs.blobs:
            return self.partition(rule, asdict(spec), CandidatePartition("@empty", dependencies),
                                  lambda: inputs.regex(spec, ()))
        identities = {path: input_identity((asdict(spec), (("blob", path, blob), *dependencies), CANDIDATE_READER_VERSION))
                      for path, blob in inputs.blobs.items()}
        rows = dict(cast(list[tuple[str, str]], self.connection.execute(
            "SELECT partition,input_identity FROM refill_candidates WHERE catalog=? AND rule=?",
            (self.catalog, rule)).fetchall()))
        pending = tuple(path for path in sorted(inputs.blobs) if rows.get(path) != identities[path])
        by_path: dict[str, list[generators.Candidate]] = {}
        if pending:
            enumeration = inputs.regex(spec, pending)
            for candidate in enumeration.candidates:
                by_path.setdefault(candidate.file, []).append(candidate)
        result: list[generators.Candidate] = []
        for path in sorted(inputs.blobs):
            partition = CandidatePartition(path, (("blob", path, inputs.blobs[path]), *dependencies))
            current = self.partition(rule, asdict(spec), partition,
                                     lambda path=path: generators.Enumeration(tuple(by_path.get(path, ())), f"regex:{spec.pattern}"))
            result.extend(current.candidates)
        result.sort(key=lambda candidate: (candidate.file, candidate.line))
        return generators.Enumeration(tuple(result), f"regex:{spec.pattern}")

    def trait_declarations(self, blob: str, masked: Callable[[], generators.MaskedSource]) -> tuple[tuple[str, int], ...]:
        if isinstance(self.fact(blob, "trait_declarations"), UnstoredFact):
            declarations = generators.trait_declarations(masked())
            if blob not in self.failed_blobs:
                _ = self.connection.executemany(
                    "INSERT OR IGNORE INTO refill_objects "
                    + "(object_store,object_id,reader_version,fact_kind,fact_key,line_count,declaration_name,source_line) "
                    + "VALUES (?,?,?,'trait_declaration',?,0,?,?)",
                    ((self.object_store, blob, CANDIDATE_READER_VERSION, str(line), name, line)
                     for name, line in declarations))
                self.store_fact(blob, "trait_declarations", "", "")
            return declarations
        return tuple(cast(list[tuple[str, int]], self.connection.execute(
            "SELECT declaration_name,source_line FROM refill_objects WHERE object_store=? AND object_id=? "
            + "AND reader_version=? AND fact_kind='trait_declaration' ORDER BY source_line",
            (self.object_store, blob, CANDIDATE_READER_VERSION)).fetchall()))

    def traits(self, rule: str, spec: generators.CandidatesSpec, inputs: RefillInputs) -> generators.Enumeration:
        paths = inputs.rust_paths(inputs.root)
        inputs.prepare_text(paths)
        declarations: dict[str, list[generators.Candidate]] = {}
        for path in paths:
            blob = inputs.blobs[path]
            declared = self.trait_declarations(
                blob, lambda path=path: generators.read_masked(inputs.root / path, inputs.view((path,))))
            for name, line in declared:
                declarations.setdefault(name, []).append(generators.Candidate(path, line, ""))
        dependencies = (("rust_membership", ".", input_identity(paths)),
                        *(("blob", path, inputs.blobs[path]) for path in paths))
        if not declarations:
            return self.partition(rule, asdict(spec), CandidatePartition("@empty", dependencies),
                                  lambda: generators.Enumeration((), "parse:trait-decls-with-impl-count<=1"))
        result: list[generators.Candidate] = []
        for name, sites in declarations.items():
            def calculate(name: str = name, sites: list[generators.Candidate] = sites) -> generators.Enumeration:
                count = 0
                for path in paths:
                    blob = inputs.blobs[path]
                    stored = self.fact(blob, "trait_impl_count", name)
                    if isinstance(stored, UnstoredFact):
                        masked = generators.read_masked(inputs.root / path, inputs.view((path,)))
                        stored = str(generators.trait_impl_count(name, masked))
                        if blob not in self.failed_blobs:
                            self.store_fact(blob, "trait_impl_count", name, stored)
                    count += int(stored)
                return generators.Enumeration(tuple(
                    generators.Candidate(site.file, site.line, f"trait {name}: {count} impl(s) project-wide")
                    for site in sites if count <= 1), "parse:trait-decls-with-impl-count<=1")
            result.extend(self.partition(rule, asdict(spec), CandidatePartition(name, dependencies), calculate).candidates)
        result.sort(key=lambda candidate: (candidate.file, candidate.line))
        return generators.Enumeration(tuple(result), "parse:trait-decls-with-impl-count<=1")

    def pre_filter(self, rule: str, pattern: str, inputs: RefillInputs,
                   paths: tuple[str, ...]) -> tuple[generators.Candidate, ...]:
        compiled = re.compile(pattern)
        if not paths:
            return self.partition(rule, pattern, CandidatePartition("@empty", (("listed_membership", ".", "empty"),)),
                                  lambda: generators.Enumeration((), "pre_filter:" + pattern)).candidates
        inputs.prepare_text(paths)
        result: list[generators.Candidate] = []
        for path in paths:
            partition = CandidatePartition(path, (("blob", path, inputs.blobs[path]), ("listed_membership", path, "present")))
            def calculate(path: str = path) -> generators.Enumeration:
                text = inputs.view((path,)).text(inputs.root / path)
                return generators.Enumeration(tuple(generators.Candidate(path, line, text)
                                                    for line, text in enumerate(text.splitlines(), 1) if compiled.search(text)),
                                              "pre_filter:" + pattern)
            result.extend(self.partition(rule, pattern, partition, calculate).candidates)
        return tuple(result)


def encode_enumeration(enumeration: generators.Enumeration) -> str:
    return json.dumps((enumeration.source, [(site.file, site.line, site.text) for site in enumeration.candidates]),
                      separators=(",", ":"))


def decode_enumeration(value: str) -> generators.Enumeration:
    source, sites = cast(tuple[str, list[tuple[str, int, str]]], json.loads(value))
    return generators.Enumeration(tuple(generators.Candidate(*site) for site in sites), source)
