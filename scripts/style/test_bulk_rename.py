"""Bulk local renames on a temporary Cargo project with private Rust tool stubs.

Real LSP and compiler tests use BULK_RENAME_REAL_TOOLS=1; the merge gate never sets it.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import traceback
import unittest
from collections.abc import Callable
from contextlib import closing, nullcontext
from dataclasses import replace
from pathlib import Path
from typing import cast, override
from unittest import mock

import bulk_rename as bulk
import berth_board
import rules
import style_ledger
import style_rules
from scripts.direct.test_verify_untested_examples import rust_tool_path, workspace_metadata
from approved_renames import AppliedInCode, BindingRename, NoBindingScope, binding_rename_check, binding_scope


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

class ScriptedServer:
    def __init__(self, edit: Callable[[str, int, str], tuple[bulk.SourceEdit, ...]]) -> None:
        self.edit: Callable[[str, int, str], tuple[bulk.SourceEdit, ...]] = edit
        self.calls: list[tuple[int, str]] = []
        self.changes: list[str] = []
        self.opened: str = ""
        self.documents: dict[Path, str] = {}
        self.sources: bulk.RunSourceIndex = bulk.RunSourceIndex(self)
        self.derive_origins: dict[str, bulk.DefinitionTarget] = {}

    def open(self, path: Path, text: str) -> None:
        self.sources.update(path, text)
        if path in self.documents:
            self.change(path, text)
        else:
            self.opened = text
            self.documents[path] = text

    def change(self, path: Path, text: str) -> None:
        self.sources.update(path, text)
        self.documents[path] = text
        self.changes.append(text)

    def rename(self, path: Path, text: str, offset: int, new: str) -> bulk.WorkspaceRename:
        self.calls.append((offset, new))
        return bulk.WorkspaceRename((bulk.DocumentEdits(path, text, self.edit(text, offset, new)),))

    def definitions(self, path: Path, text: str, offsets: tuple[int, ...]) -> tuple[bulk.DefinitionResolution, ...]:
        spans = bulk.BindingText(text).spans
        answers: list[bulk.DefinitionResolution] = []
        for offset in offsets:
            token = next(span.token.value for span in spans if span.start == offset)
            if token in self.derive_origins:
                answers.append(self.derive_origins[token])
                continue
            declaration = next(span for span in spans if span.token.value == token)
            begin, end = bulk.lsp_position(text, declaration.start), bulk.lsp_position(text, declaration.end)
            answers.append(bulk.DefinitionTarget(path.as_uri(), (begin["line"], begin["character"]),
                                                (end["line"], end["character"])))
        return tuple(answers)


def word_edits(source: str, old: str, new: str, *, macros: bool = True) -> tuple[bulk.SourceEdit, ...]:
    parsed = bulk.BindingText(source)
    return tuple(bulk.SourceEdit(span.start, span.end, new) for index, span in enumerate(parsed.spans)
                 if span.token.value == old and (macros or not parsed.contains(parsed.macros, index)))


class CargoProjectTests(unittest.TestCase):
    repo: Path = Path()
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="bulk-rename-test-")))
        self.set_up_tools()
        self.repo = self.root / "project"
        (self.repo / "src").mkdir(parents=True)
        _ = (self.repo / "Cargo.toml").write_text('[package]\nname="bulk_sample"\nversion="0.1.0"\nedition="2021"\n')
        _ = (self.repo / ".gitignore").write_text("/target\n/Cargo.lock\n")
        _ = (self.repo / "src/lib.rs").write_text("pub fn run() {\n    let old = 1;\n    let _ = old + 1;\n}\n")
        _ = self.git("init", "-q", "--initial-branch=main")
        _ = self.git("config", "user.name", "Bulk test")
        _ = self.git("config", "user.email", "bulk@example.invalid")
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "fixture")

    def set_up_tools(self) -> None:
        tools = rust_tool_path(self.root / "tools")
        analyzer = Path(tools) / "rust-analyzer"
        _ = analyzer.write_text('#!/bin/sh\necho "Unexpected rust-analyzer call" >&2\nexit 99\n')
        analyzer.chmod(0o755)
        self.enterContext(mock.patch.dict(os.environ, {
            "PATH": tools,
            "CARGO_HOME": str(self.root / "cargo-home"),
            "RUSTUP_HOME": str(self.root / "rustup"),
        }))

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, check=True).stdout.strip()

    def finding(self, source: str, old: str = "old", new: str = "number", line: int = 2,
                identity: str = "finding") -> bulk.RenameFinding:
        return bulk.RenameFinding(identity, "src/lib.rs", BindingRename(line, source.splitlines()[line - 1], old, new))

    def rename(self, source: str, findings: tuple[bulk.RenameFinding, ...], server: ScriptedServer) -> bulk.FileRenames:
        return bulk.rename_file(server, self.repo / "src/lib.rs", "src/lib.rs", source, findings, bulk.repository_docs(self.repo))

    def derive_server(self, edit: Callable[[str, int, str], tuple[bulk.SourceEdit, ...]]) -> ScriptedServer:
        server = ScriptedServer(edit)
        for package, names in (("serde_derive", ("Serialize", "Deserialize")), ("clap_derive", ("Parser", "Args"))):
            folder = self.root / package
            folder.mkdir(exist_ok=True)
            _ = (folder / "Cargo.toml").write_text('[package]\nname="' + package + '"\nversion="1.0.0"\n')
            source = folder / "lib.rs"
            _ = source.write_text("fn derive() {}\n")
            for name in names:
                server.derive_origins[name] = bulk.DefinitionTarget(source.as_uri(), (0, 3), (0, 9))
        return server

    def ledger(self) -> style_ledger.Ledger:
        rule_folder = self.root / "style/rust"
        rule_folder.mkdir(parents=True, exist_ok=True)
        _ = (rule_folder / "prefer-type-named-fields-and-bindings.md").write_text(
            "---\ntier: C\nfamily: names\ninput_scope: file\n---\n# Names\n")
        _ = self.enterContext(mock.patch.object(style_rules, "NATE_STYLE_DIR", rule_folder.parent))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"STYLE_HISTORY_NATE_STYLE_DIR": str(rule_folder.parent),
                                                                      "STYLE_LEDGER_STATE_DIR": str(self.root / "state")})))
        return self.enterContext(style_ledger.open_ledger(self.repo))

    def ticket(self, ledger: style_ledger.Ledger) -> tuple[str, str]:
        definition = next(rule for rule in rules.load_rules(self.repo) if rule.name == "prefer-type-named-fields-and-bindings")
        source = (self.repo / "src/lib.rs").read_text()
        issue = self.root / "decision.md"
        _ = issue.write_text("Approved names")
        ids: list[str] = []
        for line, old, new in ((2, "old", "number"), (3, "missing", "replacement")):
            held = ledger.hold(definition.rule_id, "src/lib.rs", line, source.splitlines()[line - 1],
                               "rename", old, new, definition.blob, "judging-1")
            ledger.file_held(held.finding_id, issue)
            ids.append(held.finding_id)
        ledger.ticket_add("names-1", "names", "proposal", "names", ".", ["src/lib.rs"], [definition.rule_id],
                          proposal=str(issue), finding_ids=tuple(ids))
        ledger.ticket_assign("names-1", "main")
        return ids[0], ids[1]


class RenameTests(CargoProjectTests):
    def test_parameter_own_docs_are_coordinated_without_editing_external_or_neighbor_docs(self) -> None:
        source = ("/// Neighbor uses `old`.\nfn neighbor() {}\n"
                  "/// Add `old`; older is ordinary prose.\nfn run(old: usize) -> usize { old + 1 }\n")
        _ = (self.repo / "README.md").write_text("Call run with `old`.\n")
        result = self.rename(source, (self.finding(source, line=4),), ScriptedServer(
            lambda text, offset, new: word_edits(text, "old", new)))
        self.assertEqual(result.refusals, ())
        self.assertEqual(result.edited, source[:source.index("/// Add")] + source[source.index("/// Add"):].replace("`old`", "`number`").replace("run(old:", "run(number:").replace("{ old +", "{ number +"))
        self.assertEqual((self.repo / "README.md").read_text(), "Call run with `old`.\n")

    def test_parameter_ambiguous_doc_word_refuses_without_an_overlay(self) -> None:
        for doc in ("/// The old behavior is retained.", "/** The old behavior is retained. */"):
            with self.subTest(doc=doc):
                source = doc + "\nfn run(old: usize) -> usize { old + 1 }\n"
                server = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))
                result = self.rename(source, (self.finding(source),), server)
                self.assertEqual(result.edited, source)
                self.assertIn("parameter doc word is ambiguous", result.refusals[0].reason)
                self.assertEqual(server.calls, [])

    def test_parameter_refactor_cannot_change_a_neighbors_doc_comment(self) -> None:
        source = '/// Neighbor mentions `old`.\nfn neighbor() {}\n/// Use `old`.\nfn run(old: usize) -> usize { old }\n'
        def edits(text: str, _offset: int, new: str) -> tuple[bulk.SourceEdit, ...]:
            start = text.index('old')
            return (*word_edits(text, 'old', new), bulk.SourceEdit(start, start + 3, new))
        server = ScriptedServer(edits)
        result = self.rename(source, (self.finding(source, line=4),), server)
        self.assertEqual(result.edited, source)
        self.assertIn('outside its proved comment mentions', result.refusals[0].reason)
        self.assertEqual(server.documents[self.repo / 'src/lib.rs'], source)

    def test_clap_field_keeps_its_argument_id_and_generated_value_name(self) -> None:
        for kind, expected in (('bool', '#[arg(id = "old", long = "old")]'),
                               ('usize', '#[arg(id = "old", long = "old", value_name = "OLD")]')):
            with self.subTest(kind=kind):
                source = '#[derive(clap::Parser)]\nstruct R {\n #[arg(long)] old: ' + kind + ',\n #[arg(long, requires = "old")] dependent: bool,\n}\n'
                server = self.derive_server(lambda text, offset, new: word_edits(text, 'old', new))
                result = self.rename(source, (self.finding(source, line=3),), server)
                self.assertEqual(result.refusals, ())
                self.assertIn(expected + ' number: ' + kind, result.edited)
                self.assertIn('requires = "old"', result.edited)

    def test_clap_unproved_id_positional_and_help_forms_refuse_before_rename(self) -> None:
        for attribute, kind in (('#[arg(id = "custom", long)]', 'bool'),
                                ('', 'usize'), ('#[arg(long, value_name = "VALUE")]', 'usize'),
                                ('#[arg(long, help = "Use old")]', 'bool'),
                                ('/// Use `old`.\n #[arg(long)]', 'bool'),
                                ('#[arg(long)]', 'String')):
            with self.subTest(attribute=attribute, kind=kind):
                source = '#[derive(clap::Parser)]\nstruct R {\n ' + attribute + ' old: ' + kind + ',\n}\n'
                line = next(i for i, text in enumerate(source.splitlines(), 1) if ' old:' in text)
                server = self.derive_server(lambda text, offset, new: word_edits(text, 'old', new))
                result = self.rename(source, (self.finding(source, line=line),), server)
                self.assertEqual(result.kept, ())
                self.assertIn('derive field CLI', result.refusals[0].reason)
                self.assertEqual(server.calls, [])
                self.assertEqual(server.documents[self.repo / 'src/lib.rs'], source)

    def test_parameter_intra_doc_links_are_not_parameter_mentions(self) -> None:
        for mention in ('[old]', '[`old`]', '[the function](old)', '[call]: old', '[call]: `old`'):
            with self.subTest(mention=mention):
                source = 'fn old() {}\n/// Call ' + mention + '\nfn run(old: usize) -> usize { old }\n'
                server = ScriptedServer(lambda text, offset, new: tuple(
                    edit for edit in word_edits(text, 'old', new) if edit.start >= offset))
                result = self.rename(source, (self.finding(source, line=3),), server)
                self.assertEqual(result.kept, ())
                self.assertIn('parameter doc', result.refusals[0].reason)
                self.assertEqual(server.calls, [])
                self.assertEqual(server.documents[self.repo / 'src/lib.rs'], source)

    def test_outer_parameter_preserves_nested_item_docs_and_refuses_analyzer_doc_edits(self) -> None:
        source = '/// Outer `old`.\nfn run(old: usize) -> usize {\n /// Inner `old`.\n fn inner(old: usize) -> usize { old }\n old + inner(1)\n}\n'
        nested = source.index('Inner `old`') + len('Inner `')
        def edits(text: str, _offset: int, new: str) -> tuple[bulk.SourceEdit, ...]:
            return tuple(edit for edit in word_edits(text, 'old', new)
                         if edit.start < text.index('fn inner') or edit.start > text.index(' old + inner'))
        for malicious in (False, True):
            with self.subTest(malicious=malicious):
                server = ScriptedServer(lambda text, offset, new: (
                    *edits(text, offset, new), *((bulk.SourceEdit(nested, nested + 3, new),) if malicious else ())))
                result = self.rename(source, (self.finding(source, line=2),), server)
                if malicious:
                    self.assertEqual(result.kept, ())
                    self.assertIn('outside its proved comment mentions', result.refusals[0].reason)
                    self.assertEqual(server.documents[self.repo / 'src/lib.rs'], source)
                else:
                    self.assertEqual(result.refusals, ())
                    self.assertIn('/// Outer `number`.', result.edited)
                    self.assertIn('/// Inner `old`.\n fn inner(old: usize) -> usize { old }', result.edited)
                    self.assertIn(' number + inner(1)', result.edited)

    def test_derive_fields_freeze_wire_and_implicit_long_names(self) -> None:
        for derive, attribute, expected in (
                ("serde::Serialize, serde::Deserialize, Clone", "", '#[serde(rename = "old")]'),
                ("clap::Parser", "#[arg(long)]", '#[arg(id = "old", long = "old", value_name = "OLD")]'),
                ("serde::Serialize, clap::Args", "#[arg(long)]", '#[serde(rename = "old")] #[arg(id = "old", long = "old", value_name = "OLD")]')):
            with self.subTest(derive=derive):
                source = '#[derive(' + derive + ')]\nstruct R {\n ' + attribute + ' old: usize,\n}\n'
                result = self.rename(source, (self.finding(source, line=3),), self.derive_server(
                    lambda text, offset, new: word_edits(text, "old", new)))
                self.assertEqual(result.refusals, ())
                self.assertIn(expected + ' number: usize', result.edited)
                self.assertTrue(bulk.file_edits_preserved(result, result.edited.replace(' usize', '  usize')))
                self.assertFalse(bulk.file_edits_preserved(result, result.edited.replace('"old"', '"number"')))

    def test_cli_snake_case_flag_keeps_its_original_kebab_case(self) -> None:
        source = '#[derive(clap::Parser)]\nstruct R {\n #[arg(long)] old_name: usize,\n}\n'
        result = self.rename(source, (self.finding(source, 'old_name', 'number', 3),), self.derive_server(
            lambda text, offset, new: word_edits(text, 'old_name', new)))
        self.assertEqual(result.refusals, ())
        self.assertIn('#[arg(id = "old_name", long = "old-name", value_name = "OLD_NAME")] number:', result.edited)

    def test_derive_field_unproved_names_and_metadata_are_refused(self) -> None:
        for derive, outer, field in (
                ('serde::Serialize', '#[serde(rename_all = "camelCase")]', ''),
                ('serde::Serialize', '', '#[serde(rename = "wire")]'),
                ('serde::Serialize', '', '#[serde(flatten)]'),
                ('serde::Serialize, Unknown', '', ''),
                ('clap::Parser', '', '#[arg(long = "flag")]'),
                ('clap::Parser', '#[command(rename_all = "camelCase")]', '#[arg(long)]'),
                ('clap::Parser', '', '#[arg(short, long)]'),
                ('serde::Serialize', '', '#[cfg_attr(unix, serde(rename = "wire"))]')):
            with self.subTest(derive=derive, outer=outer, field=field):
                source = '#[derive(' + derive + ')] ' + outer + '\nstruct R {\n ' + field + ' old: usize,\n}\n'
                server = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))
                result = self.rename(source, (self.finding(source, line=3),), server)
                self.assertEqual(result.edited, source)
                self.assertRegex(result.refusals[0].reason, 'derive field.*(unproved|already|flatten|requires)')
                self.assertEqual(server.calls, [])

    def test_familiar_derive_spelling_requires_its_provider_definition(self) -> None:
        source = '#[derive(serde::Serialize)]\nstruct R { old: usize }\n'
        for provider in ('missing', 'same_crate', 'other_crate'):
            with self.subTest(provider=provider):
                server = self.derive_server(lambda text, offset, new: word_edits(text, 'old', new))
                if provider == 'missing':
                    server.derive_origins['Serialize'] = bulk.DefinitionTarget((self.repo / 'src/lib.rs').as_uri(), (0, 16), (0, 25))
                elif provider == 'same_crate':
                    _ = (self.repo / 'Cargo.toml').write_text('[package]\nname="serde_derive"\nversion="1.0.0"\n')
                    server.derive_origins['Serialize'] = bulk.DefinitionTarget((self.repo / 'src/lib.rs').as_uri(), (0, 16), (0, 25))
                else:
                    _ = (self.root / 'serde_derive/Cargo.toml').write_text('[package]\nname="other_derive"\nversion="1.0.0"\n')
                result = self.rename(source, (self.finding(source),), server)
                self.assertEqual(result.edited, source)
                self.assertIn('derive field origin unproved', result.refusals[0].reason)
                self.assertEqual(server.calls, [])

    def test_refactor_cannot_overwrite_the_preserved_field_attribute(self) -> None:
        source = '#[derive(clap::Parser)]\nstruct R { #[arg(long)] old: usize }\n'
        def edits(text: str, _offset: int, new: str) -> tuple[bulk.SourceEdit, ...]:
            start = text.index('#[arg')
            return (*word_edits(text, 'old', new), bulk.SourceEdit(start, text.index(']', start) + 1, '#[arg(long = "changed")]'))
        server = self.derive_server(edits)
        result = self.rename(source, (self.finding(source),), server)
        self.assertEqual(result.edited, source)
        self.assertIn('overlaps the preserved external name', result.refusals[0].reason)
        self.assertEqual(server.documents[self.repo / 'src/lib.rs'], source)

    def test_f009_several_globs_and_proof_share_one_coverage_walk(self) -> None:
        source = ''.join(f"mod provider{i} {{}} use provider{i}::*;\n" for i in range(5))
        source += "fn f() { let old = 1; consume(old); }\n"
        server = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))
        with mock.patch.object(bulk, "crate_source_coverage", wraps=bulk.crate_source_coverage) as walk:
            result = self.rename(source, (self.finding(source, line=6),), server)
        self.assertEqual(result.refusals, ())
        self.assertEqual(walk.call_count, 1)

    def test_f007_index_reads_once_and_tracks_accepted_and_rolled_back_edits(self) -> None:
        root, child = self.repo / "src/lib.rs", self.repo / "src/child.rs"
        source = "mod child; fn f() { let old = 1; consume(old); }\n"
        other = "fn f() { let other = 2; consume(other); }\n"
        _ = root.write_text(source)
        _ = child.write_text(other)
        server = ScriptedServer(lambda text, offset, new: word_edits(text, "old" if new == "number" else "other", new))
        read_text = Path.read_text
        reads: list[Path] = []
        def read(path: Path, *_args: object, **_kwargs: object) -> str:
            reads.append(path)
            return read_text(path)  # All indexed sources use the same UTF-8 text boundary.
        with mock.patch.object(Path, "read_text", read), mock.patch.object(
                bulk, "crate_source_coverage", wraps=bulk.crate_source_coverage) as walk:
            self.assertEqual(bulk.crate_name_documents(server, root, "number"), ())
            edit = {"changes": {child.as_uri(): [{"range": {"start": {"line": 0, "character": 0},
                                                             "end": {"line": 0, "character": 0}}, "newText": "// note\n"}]}}
            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("WorkspaceEdit reread indexed source")):
                workspace = bulk.workspace_edits(edit, root, source, 1,
                                                  read_source=lambda path: server.sources.read(path).source)
            self.assertEqual(workspace.documents[0].source, other)
            accepted = self.rename(source, (self.finding(source, line=1),), server)
            self.assertEqual(accepted.refusals, ())
            self.assertEqual({doc.path for doc in bulk.crate_name_documents(server, root, "number")}, {root})
            definitions = server.definitions
            def refuse(path: Path, text: str, offsets: tuple[int, ...]) -> tuple[bulk.DefinitionResolution, ...]:
                if "count" in text:
                    return tuple(bulk.DefinitionUnavailable("test rollback") for _ in offsets)
                return definitions(path, text, offsets)
            with mock.patch.object(server, "definitions", side_effect=refuse):
                rolled_back = bulk.rename_file(server, child, "src/child.rs", other,
                                               (self.finding(other, "other", "count", 1),))
            self.assertIn("test rollback", rolled_back.refusals[0].reason)
            self.assertEqual(server.documents[child], other)
            self.assertEqual(bulk.crate_name_documents(server, root, "count"), ())
            self.assertIn("number", bulk.crate_name_documents(server, root, "number")[0].source)
        self.assertEqual((reads.count(root), reads.count(child)), (1, 1))
        self.assertEqual(walk.call_count, 1)

    def test_direct_call_and_multiple_bindings_use_the_refactor(self) -> None:
        source = "fn f() { let first = 1; let second = consume(first); }\n"
        findings = (self.finding(source, "first", "first_number", 1, "first"),
                    self.finding(source, "second", "second_number", 1, "second"))
        server = ScriptedServer(lambda text, offset, new: word_edits(text, "first" if new == "first_number" else "second", new))
        result = self.rename(source, findings, server)
        self.assertEqual([new for _, new in server.calls], ["second_number", "first_number"])
        self.assertEqual(len(result.kept), 2)
        self.assertEqual(result.refusals, ())
        self.assertIn("consume(first_number)", result.edited)

    def test_comments_and_parameter_docs_include_prose(self) -> None:
        for source, line in (("fn run() {\n let old = 1;\n // old is useful.\n let _ = old + 1;\n}\n", 2),
                             ("/// Use `old` twice: `old`.\nfn run(old: usize) -> usize { old + 1 }\n", 2)):
            result = self.rename(source, (self.finding(source, line=line),), ScriptedServer(
                lambda text, offset, new: word_edits(text, "old", new)))
            self.assertEqual(result.refusals, ())
            self.assertEqual(result.edited, source.replace("old", "number"))

    def test_parameter_comment_edits_rebase_later_declaration(self) -> None:
        source = "/// Add `first` and `second`.\nfn f(first: usize, second: usize) -> usize { first + second }\n"
        findings = (self.finding(source, "first", "first_number", 2, "first"),
                    self.finding(source, "second", "second_number", 2, "second"))
        server = ScriptedServer(lambda text, offset, new: word_edits(text, "first" if new == "first_number" else "second", new))
        result = self.rename(source, findings, server)
        self.assertEqual(result.refusals, ())
        self.assertEqual(result.edited, source.replace("first", "first_number").replace("second", "second_number"))

    def test_capture_and_external_text_guards_precede_edits(self) -> None:
        for source, line, cause in (
                ("fn f() {\n let old = 1;\n let number = old + 1;\n}\n", 2, "capture 'number'"),
                ("#[derive(serde::Serialize)]\nstruct R {\n #[serde(rename = \"wire\")] old: usize,\n}\n", 3, "derive field"),
                ("#[derive(Wire)]\nstruct R {\n old: usize,\n}\n", 3, "custom derive may expose names: Wire"),
                ("pub struct R {\n pub old: usize,\n}\n", 2, "public item of a published crate")):
            server = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))
            result = self.rename(source, (self.finding(source, line=line),), server)
            self.assertEqual(result.edited, source)
            self.assertEqual(server.calls, [])
            self.assertIn(cause, result.refusals[0].reason)

    def test_unknown_macro_can_serialize_an_identifier(self) -> None:
        source = "fn f() {\n let old = 1;\n custom!(old);\n}\n"
        result = self.rename(source, (self.finding(source),), ScriptedServer(
            lambda text, offset, new: word_edits(text, "old", new)))
        self.assertEqual(result.edited, source)
        self.assertIn("macro-generated text: custom", result.refusals[0].reason)

    def test_glob_clash_in_file_refuses_before_rename(self) -> None:
        source = "use provider::*;\nfn f() { let old = 1; let number = 2; }\n"
        server = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))
        result = self.rename(source, (self.finding(source),), server)
        self.assertEqual(server.calls, [])
        self.assertIn("glob import clash", result.refusals[0].reason)

    def test_overlapping_findings_refuse_the_second_edit(self) -> None:
        source = "struct R { old: usize }\nfn f() { let _ = R { old: 1 }; }\n"
        findings = (self.finding(source, line=1), self.finding(source, line=1, identity="duplicate"))
        result = self.rename(source, findings, ScriptedServer(lambda text, offset, new: word_edits(text, "old", new)))
        self.assertEqual(len(result.kept), 1)
        self.assertEqual(len(result.refusals), 1)

    def test_refactor_output_is_the_post_format_authority(self) -> None:
        source = (self.repo / "src/lib.rs").read_text()
        result = self.rename(source, (self.finding(source),), ScriptedServer(
            lambda text, offset, new: word_edits(text, "old", new)))
        self.assertTrue(bulk.file_edits_preserved(result, result.edited.replace("    ", "  ")))
        self.assertFalse(bulk.file_edits_preserved(result, result.edited.replace("+ 1", "+ 2")))

    def test_resolution_failure_restores_every_changed_document(self) -> None:
        source = "struct R { old: usize } mod other;\n"
        other = self.repo / "src/other.rs"
        _ = other.write_text("fn use_field(r: R) { let _ = r.old; }\n")
        server = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))
        workspace = bulk.WorkspaceRename(tuple(bulk.DocumentEdits(path, text, word_edits(text, "old", "number"))
                                               for path, text in ((self.repo / "src/lib.rs", source), (other, other.read_text()))))
        for answer in (bulk.DefinitionUnavailable("ambiguous definition"), bulk.DefinitionUnavailable("definition request timed out")):
            def unavailable(_path: Path, _text: str, offsets: tuple[int, ...]) -> tuple[bulk.DefinitionResolution, ...]:
                return tuple(answer for _ in offsets)
            with self.subTest(answer=answer), mock.patch.object(server, "rename", return_value=workspace), mock.patch.object(
                    server, "definitions", side_effect=unavailable):
                result = self.rename(source, (self.finding(source, line=1),), server)
                self.assertIn(answer.reason, result.refusals[0].reason)
                self.assertEqual(result.edited, source)
                self.assertEqual(server.documents[self.repo / "src/lib.rs"], source)
                self.assertEqual(server.documents[other], other.read_text())

    def test_prepare_or_rename_refusal_returns_its_exact_reason(self) -> None:
        source = (self.repo / "src/lib.rs").read_text()
        def refuse(_text: str, _offset: int, _new: str) -> tuple[bulk.SourceEdit, ...]:
            raise bulk.BulkRenameError("prepareRename refused")
        result = self.rename(source, (self.finding(source),), ScriptedServer(refuse))
        self.assertEqual(result.kept, ())
        self.assertEqual(result.refusals[0].reason, "prepareRename refused")


class SourceCoverageTests(CargoProjectTests):
    def server(self) -> ScriptedServer:
        return ScriptedServer(lambda source, offset, new: ())

    def test_f005_loader_only_module_refuses_before_name_filter(self) -> None:
        _ = (self.repo / "src/lib.rs").write_text("mod child; fn old() {}")
        child = self.repo / "src/child.rs"
        for loader in (
                       '#[cfg_attr(unix, path = "hidden.rs")] mod hidden;',
                       '#[cfg_attr(unix, cfg_attr(any(), path = "hidden.rs"))] mod hidden;',
                       'include /* comment */ ! ("generated.txt");'):
            with self.subTest(loader=loader):
                _ = child.write_text(loader)
                with self.assertRaisesRegex(bulk.BulkRenameError, "unsupported source loader"):
                    _ = bulk.crate_name_documents(self.server(), self.repo / "src/lib.rs", "number")

    def test_f005_configured_target_is_root(self) -> None:
        _ = (self.repo / "Cargo.toml").write_text(
            '[package]\nname="bulk_sample"\nversion="0.1.0"\n[lib]\npath="custom/root.rs"\n')
        (self.repo / "custom").mkdir()
        root = self.repo / "custom/root.rs"
        _ = root.write_text("mod child; fn number() {}")
        child = self.repo / "custom/child.rs"
        _ = child.write_text("fn f() { let number = 1; }")
        _ = (self.repo / "src/lib.rs").write_text('include!("not-loaded.txt"); fn number() {}')
        documents = bulk.crate_name_documents(self.server(), root, "number")
        self.assertEqual({document.path for document in documents}, {root, child})

    def test_f005_ordinary_modules_pass_without_scanning_unloaded_files(self) -> None:
        root = self.repo / "src/lib.rs"
        _ = root.write_text("mod outer { mod child; } fn number() {}")
        (self.repo / "src/outer/child").mkdir(parents=True)
        child = self.repo / "src/outer/child.rs"
        _ = child.write_text("mod leaf; fn f() { let number = 1; }")
        leaf = self.repo / "src/outer/child/leaf.rs"
        _ = leaf.write_text("fn f() { let number = 2; }")
        _ = (self.repo / "src/unloaded.rs").write_text('include!("ignored.txt"); fn number() {}')
        documents = bulk.crate_name_documents(self.server(), root, "number")
        self.assertEqual({document.path for document in documents}, {root, child, leaf})

    def test_f005_target_directory_and_symlink_are_followed(self) -> None:
        root = self.repo / "src/lib.rs"
        _ = root.write_text("mod target; mod linked;")
        (self.repo / "src/target").mkdir()
        child = self.repo / "src/target/mod.rs"
        _ = child.write_text("fn number() {}")
        external = self.root / "module"
        external.mkdir()
        linked = external / "mod.rs"
        _ = linked.write_text("fn number() {}")
        (self.repo / "src/linked").symlink_to(external, target_is_directory=True)
        documents = bulk.crate_name_documents(self.server(), root, "number")
        self.assertEqual({document.path for document in documents}, {child, linked})

    def test_f005_missing_or_ambiguous_module_refuses(self) -> None:
        root = self.repo / "src/lib.rs"
        _ = root.write_text("mod absent;")
        with self.assertRaisesRegex(bulk.BulkRenameError, "module 'absent'.*missing source"):
            _ = bulk.crate_name_documents(self.server(), root, "number")
        _ = (self.repo / "src/absent.rs").write_text("")
        (self.repo / "src/absent").mkdir()
        _ = (self.repo / "src/absent/mod.rs").write_text("")
        with self.assertRaisesRegex(bulk.BulkRenameError, "module 'absent'.*ambiguous source"):
            _ = bulk.crate_name_documents(self.server(), root, "number")

    def test_f005_unreached_edit_refuses(self) -> None:
        path = self.repo / "src/unloaded.rs"
        _ = path.write_text("fn number() {}")
        with self.assertRaisesRegex(bulk.BulkRenameError, "outside reached modules"):
            _ = bulk.crate_name_documents(self.server(), path, "number")

    def write_modules(self, files: dict[str, str]) -> set[Path]:
        paths: set[Path] = set()
        for relative, text in files.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text(text)
            paths.add(path.resolve())
        return paths

    def test_f005_path_loading_directories_and_decoys(self) -> None:
        _ = (self.repo / "Cargo.toml").write_text(
            '[package]\nname="bulk_sample"\nversion="0.1.0"\n[lib]\npath="custom/root.rs"\n')
        expected = self.write_modules({
            "custom/root.rs": 'mod flat; mod directory; #[cfg(test)] #[path="impl/backend.txt"] mod backend;',
            "custom/flat.rs": '#[path="sibling.txt"] mod sibling; mod ordinary; mod outer { mod inner { #[path="leaf.txt"] mod leaf; } }',
            "custom/sibling.txt": 'mod child;',
            "custom/child.rs": '',
            "custom/flat/ordinary.rs": '',
            "custom/flat/outer/inner/leaf.txt": '',
            "custom/directory/mod.rs": '#[path="../shared.txt"] mod shared;',
            "custom/shared.txt": '',
            "custom/impl/backend.txt": 'mod leaf; mod inside { #[path=r##"extra.txt"##] mod extra; }',
            "custom/impl/leaf.rs": '',
            "custom/impl/inside/extra.txt": '',
        })
        _ = self.write_modules({
            "custom/flat/sibling.txt": 'include!("wrong");',
            "custom/sibling/child.rs": 'include!("wrong");',
            "custom/impl/backend/leaf.rs": 'include!("wrong");',
            "custom/outer/inner/leaf.txt": 'include!("wrong");',
            "src/lib.rs": 'include!("not-a-target");',
        })
        documents = bulk.crate_source_documents(self.server(), self.repo / "custom/root.rs")
        self.assertEqual({document.path for document in documents}, expected)

    def test_f005_path_symlink_uses_lexical_parent_for_children(self) -> None:
        expected = self.write_modules({
            "src/lib.rs": '# [ /* comment */ path = "linked/selected.txt" ] mod selected;',
            "src/linked/leaf.rs": '',
            "elsewhere/selected.txt": 'mod leaf;',
        })
        _ = self.write_modules({"elsewhere/leaf.rs": 'include!("wrong");'})
        (self.repo / "src/linked/selected.txt").symlink_to(self.repo / "elsewhere/selected.txt")
        documents = bulk.crate_source_documents(self.server(), self.repo / "src/lib.rs")
        self.assertEqual({document.path for document in documents}, expected)

    def test_f005_repeated_files_only_refuse_when_evidence_is_needed(self) -> None:
        root = self.repo / "src/lib.rs"
        shared = self.repo / "src/shared.txt"
        _ = self.write_modules({
            "src/lib.rs": '#[path="shared.txt"] mod first; #[path="shared.txt"] mod second; fn number() {}',
            "src/shared.txt": 'fn unrelated() {}',
            "tests/check.rs": '#[path="../src/shared.txt"] mod support;',
        })
        server = self.server()
        self.assertEqual({document.path for document in bulk.crate_name_documents(server, root, "number")}, {root})
        coverage = bulk.crate_source_coverage(server, root)
        self.assertEqual(sum(instance.canonical_path == shared for instance in coverage.instances), 3)
        self.assertEqual(len(coverage.documents), 3)
        for name, changes in (("unrelated", ()), ("number", (shared,))):
            with self.subTest(name=name), self.assertRaisesRegex(bulk.BulkRenameError, "module instance coverage unavailable.*3 instances"):
                _ = bulk.crate_name_documents(server, root, name, changes)

    def test_f005_same_root_in_two_targets_retains_membership(self) -> None:
        root = self.repo / "src/lib.rs"
        _ = (self.repo / "Cargo.toml").write_text(
            '[package]\nname="bulk_sample"\nversion="0.1.0"\n[[bin]]\nname="shared"\npath="src/lib.rs"\n')
        with self.assertRaisesRegex(bulk.BulkRenameError, "module instance coverage unavailable.*2 instances"):
            _ = bulk.crate_name_documents(self.server(), root, "number")

    def test_f005_path_cycle_is_distinct_from_repeated_instances(self) -> None:
        _ = self.write_modules({"src/lib.rs": '#[path="loop.txt"] mod child;',
                                "src/loop.txt": '#[path="lib.rs"] mod back; '})
        with self.assertRaisesRegex(bulk.BulkRenameError, "source coverage unavailable: cyclic module"):
            _ = bulk.crate_name_documents(self.server(), self.repo / "src/lib.rs", "number")

    def test_f005_unsupported_path_forms_have_exact_package_wide_causes(self) -> None:
        root = self.repo / "src/lib.rs"
        _ = root.write_text('mod child; fn old() {}')
        child = self.repo / "src/child.rs"
        cases = (
            ('#[cfg_attr(unix, path="x.rs")] mod x;', "conditional path attribute"),
            ('#[cfg_attr(unix, cfg_attr(any(), path="x.rs"))] mod x;', "conditional path attribute"),
            ('#[path=concat!("x.rs")] mod x;', "nonliteral path attribute"),
            ('#[path="x.rs"] #[path="y.rs"] mod x;', "duplicate path attributes"),
            ('#[path="x.rs"] mod x {}', "path attribute requires an out-of-line module"),
            ('#![path="x.rs"] mod x;', "inner path attribute"),
            (r'#[path="x\x2ers"] mod x;', "escaped path string"),
        )
        for text, reason in cases:
            with self.subTest(text=text):
                _ = child.write_text(text)
                with self.assertRaisesRegex(bulk.BulkRenameError, "unsupported source loader: " + reason):
                    _ = bulk.crate_name_documents(self.server(), root, "number")

    def test_f005_missing_explicit_module_refuses_before_filter(self) -> None:
        _ = (self.repo / "src/lib.rs").write_text('#[path="missing.txt"] mod child; fn old() {}')
        with self.assertRaisesRegex(bulk.BulkRenameError, "source coverage unavailable: cannot read module.*missing.txt"):
            _ = bulk.crate_name_documents(self.server(), self.repo / "src/lib.rs", "number")

    def test_f005_macro_body_is_not_a_direct_path_module(self) -> None:
        source = 'macro_rules! example { () => { #[path="missing.rs"] mod child; include!("missing"); } } fn number() {}'
        root = self.repo / "src/lib.rs"
        _ = root.write_text(source)
        self.assertEqual({document.path for document in bulk.crate_name_documents(self.server(), root, "number")}, {root})

    def test_f005_glob_provider_uses_selected_path_and_overlay(self) -> None:
        root = self.repo / "src/lib.rs"
        source = '#[path="chosen.txt"] mod provider; use provider::*; fn old() {}'
        _ = self.write_modules({"src/lib.rs": source, "src/chosen.txt": "pub fn number() {}",
                                "src/provider.rs": "pub fn decoy() {}"})
        server = self.server()
        module = bulk.glob_modules(bulk.BindingText(source))[0]
        self.assertTrue(bulk.module_exports(server, root, source, module, "number", set()))
        server.open(self.repo / "src/chosen.txt", 'pub fn replacement() {}')
        self.assertFalse(bulk.module_exports(server, root, source, module, "number", set()))
        self.assertTrue(bulk.module_exports(server, root, source, module, "replacement", set()))

    def test_f008_twice_loaded_unrelated_glob_provider_needs_no_instance_query(self) -> None:
        root = self.repo / "src/lib.rs"
        source = '#[path="shared.rs"] mod first; #[path="shared.rs"] mod second; use first::*; fn old() {}'
        _ = self.write_modules({"src/lib.rs": source, "src/shared.rs": "pub fn unrelated() {}"})
        server = self.server()
        module = bulk.glob_modules(bulk.BindingText(source))[0]
        try:
            self.assertFalse(bulk.module_exports(server, root, source, module, "number", set()))
            self.assertTrue(bulk.module_exports(server, root, source, module, "unrelated", set()))
        except bulk.BulkRenameError as error:
            self.fail(str(error))


class ProtocolTests(CargoProjectTests):
    def test_f006_non_shorthand_multi_target_stays_unresolved(self) -> None:
        source = "fn f() {} fn g() {}"
        root = self.repo / "src/lib.rs"
        server = ScriptedServer(lambda text, offset, new: ())
        server.open(root, source)
        rows = [{"uri": root.as_uri(), "range": {"start": bulk.lsp_position(source, point),
                                                   "end": bulk.lsp_position(source, point + 1)}}
                for point in (source.index("f()"), source.index("g()"))]
        result = bulk.definition_result(rows, server.sources, source, source.index("f()"))
        self.assertIsInstance(result, bulk.DefinitionUnavailable)
        self.assertIn("ambiguous definition", cast(bulk.DefinitionUnavailable, result).reason)
        source = ("macro_rules! fields { () => { struct R { number: usize } } }\n"
                  + "fn f() { let number = 1; let _ = R { number }; }")
        server.open(root, source)
        rows = [{"uri": root.as_uri(), "range": {"start": bulk.lsp_position(source, point),
                                                   "end": bulk.lsp_position(source, point + len("number"))}}
                for point in (source.index("number"), source.index("number", source.index("fn f")))]
        result = bulk.definition_result(rows, server.sources, source, source.rindex("number"))
        self.assertIsInstance(result, bulk.DefinitionUnavailable)
        self.assertIn("ambiguous definition", cast(bulk.DefinitionUnavailable, result).reason)

    def test_stdio_handshake_enables_build_scripts_and_proc_macros_and_prepares_each_rename(self) -> None:
        folder = self.root / "bin"
        folder.mkdir()
        executable = folder / "rust-analyzer"
        log = self.root / "lsp.jsonl"
        _ = executable.write_text(f"#!{sys.executable}\n" + r'''
import json, os, sys
prepared = 0
def send(message):
    body = json.dumps(dict(jsonrpc="2.0", **message)).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()
while True:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        if line == b"\r\n":
            break
        key, value = line.decode().split(":", 1)
        headers[key.lower()] = value.strip()
    message = json.loads(sys.stdin.buffer.read(int(headers["content-length"])))
    with open(os.environ["BULK_LSP_LOG"], "a") as log:
        log.write(json.dumps(message) + "\n")
    method = message.get("method")
    if method == "initialize":
        send({"id": message["id"], "result": {"capabilities": {}}})
        send({"id": 900, "method": "workspace/configuration", "params": {"items": [{"section": "rust-analyzer"}]}})
        send({"method": "experimental/serverStatus", "params": {"quiescent": True}})
    elif method == "textDocument/prepareRename":
        prepared += 1
        send({"id": message["id"], "result": None if prepared == 1 else {"defaultBehavior": True}})
    elif method == "textDocument/rename":
        params = message["params"]
        start = params["position"]
        end = dict(start, character=start["character"] + 3)
        edit = {"range": {"start": start, "end": end}, "newText": params["newName"]}
        send({"id": message["id"], "result": {"changes": {params["textDocument"]["uri"]: [edit]}}})
''')
        executable.chmod(0o755)
        source = (self.repo / "src/lib.rs").read_text()
        path = self.repo / "src/lib.rs"
        with mock.patch.dict(os.environ, {"PATH": str(folder) + os.pathsep + os.environ["PATH"], "BULK_LSP_LOG": str(log)}):
            with bulk.RustAnalyzer(self.repo, timeout=TEST_HANG_GUARD_SECS) as analyzer:
                analyzer.open(path, source)
                analyzer.change(path, source)
                with self.assertRaisesRegex(bulk.BulkRenameError, "prepareRename"):
                    _ = analyzer.rename(path, source, source.index("old"), "number")
                edits = analyzer.rename(path, source, source.index("old"), "number")
                self.assertIn("let number", bulk.apply_edits(source, edits.documents[0].edits))
        messages = [bulk.object_fields(cast(object, json.loads(line))) for line in log.read_text().splitlines()]
        methods = [message.get("method") for message in messages]
        self.assertEqual(methods.count("textDocument/prepareRename"), 2)
        self.assertEqual(methods.count("textDocument/rename"), 1)
        initialize = next(message for message in messages if message.get("method") == "initialize")
        settings = bulk.object_fields(bulk.object_fields(initialize["params"])["initializationOptions"])
        self.assertEqual(settings["cargo"], {"buildScripts": {"enable": True}})
        self.assertEqual(settings["procMacro"], {"enable": True})
        configured = next(message for message in messages if message.get("id") == 900)
        self.assertEqual(configured["result"], [settings])

    def test_utf16_positions_round_trip_and_reject_split_surrogates(self) -> None:
        text = "// 😀\nlet old = 1; 😀 old\n"
        for offset in (0, 3, 4, 5, text.rindex("old"), len(text)):
            self.assertEqual(bulk.source_offset(text, bulk.lsp_position(text, offset)), offset)
        with self.assertRaises(bulk.BulkRenameError):
            _ = bulk.source_offset(text, {"line": 0, "character": 4})

    def test_workspace_edit_supports_both_lsp_shapes_and_refuses_other_files(self) -> None:
        path = self.repo / "src/lib.rs"
        text = "let old = 1;"
        edit = {"range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 7}}, "newText": "number"}
        for payload in ({"changes": {path.as_uri(): [edit]}},
                        {"documentChanges": [{"textDocument": {"uri": path.as_uri(), "version": 3}, "edits": [edit]}]}):
            self.assertEqual(bulk.apply_edits(text, bulk.workspace_edits(payload, path, text, 3).documents[0].edits), "let number = 1;")
        with self.assertRaisesRegex(bulk.BulkRenameError, "regular Rust file"):
            _ = bulk.workspace_edits({"changes": {path.as_uri(): [edit], (self.repo / "other.rs").as_uri(): [edit]}}, path, text, 3)
        with self.assertRaisesRegex(bulk.BulkRenameError, "stale"):
            _ = bulk.workspace_edits({"documentChanges": [{"textDocument": {"uri": path.as_uri(), "version": 2}, "edits": [edit]}]}, path, text, 3)
        with self.assertRaises(bulk.BulkRenameError):
            _ = bulk.workspace_edits({"documentChanges": [{"kind": "rename", "oldUri": path.as_uri(), "newUri": "file:///other"}]}, path, text, 3)

    def test_overlapping_edits_are_refused(self) -> None:
        with self.assertRaises(bulk.BulkRenameError):
            _ = bulk.apply_edits("abcdef", (bulk.SourceEdit(1, 4, "x"), bulk.SourceEdit(3, 5, "y")))


class ValidationTests(CargoProjectTests):
    def file(self) -> bulk.FileRenames:
        source = (self.repo / "src/lib.rs").read_text()
        finding = self.finding(source)
        kept = bulk.KeptRename(finding, "let", 0)
        return bulk.FileRenames("src/lib.rs", source, source.replace("old", "number"), (kept,), ())

    def test_fmt_then_check_lint_test_once_for_each_affected_package(self) -> None:
        workspace = bulk.BatchWorkspace(self.repo, (self.file(),), ("bulk_sample", "dependent"))
        with mock.patch.object(bulk, "command") as command:
            self.assertEqual(bulk.validated_files(("src/lib.rs",), workspace.validate), ("src/lib.rs",))
        self.assertEqual([call.args[1] for call in command.call_args_list], [
            ["cargo", "+nightly", "fmt", "--package", "bulk_sample"],
            ["cargo", "+nightly", "fmt", "--package", "dependent"],
            *[[str(Path.home() / ".claude/scripts/direct/verify.sh"), verb, package]
              for package in ("bulk_sample", "dependent") for verb in ("check", "lint", "test")],
        ])

    def test_file_split_removes_bad_file_and_checks_interactions(self) -> None:
        calls: list[tuple[str, ...]] = []
        def validate(paths: tuple[str, ...]) -> bool:
            calls.append(paths)
            return "bad" not in paths and not {"left", "right"} <= set(paths)
        good = bulk.validated_files(("left", "bad", "right", "safe"), validate)
        self.assertEqual(good, ("left", "safe"))
        self.assertIn(("left", "right"), calls)
        self.assertEqual(bulk.validated_files(("a", "b"), lambda paths: False), ())

    def test_failed_validation_restores_originals_and_does_not_keep_tool_changes(self) -> None:
        workspace = bulk.BatchWorkspace(self.repo, (self.file(),), ("bulk_sample",))
        original = (self.repo / "Cargo.toml").read_bytes()
        def command(repo: Path, _arguments: list[str]) -> None:
            _ = (repo / "Cargo.toml").write_text("unwanted fix")
        with mock.patch.object(bulk, "command", side_effect=command):
            self.assertFalse(workspace.validate(("src/lib.rs",)))
        workspace.keep()
        self.assertEqual((self.repo / "Cargo.toml").read_bytes(), original)
        self.assertEqual((self.repo / "src/lib.rs").read_text(), self.file().original)

    def test_post_fmt_proof_rejects_lint_auto_fixes(self) -> None:
        file = self.file()
        workspace = bulk.BatchWorkspace(self.repo, (file,), ("bulk_sample",))
        def command(repo: Path, _arguments: list[str]) -> None:
            _ = (repo / "src/lib.rs").write_text(file.edited.replace("+ 1", "+ 2"))
        with mock.patch.object(bulk, "command", side_effect=command):
            self.assertFalse(workspace.validate((file.path,)))
        workspace.keep()
        self.assertEqual((self.repo / file.path).read_text(), file.original)

    def test_stratified_sample_has_60_unique_findings_and_each_site_kind(self) -> None:
        base = self.file().kept[0]
        findings = tuple(replace(base, finding=replace(base.finding, finding_id=str(index)),
                                 kind="let" if index < 100 else "closure") for index in range(120))
        sample = bulk.sample_findings(findings)
        self.assertEqual(len(sample), 60)
        self.assertEqual(len({item.finding.finding_id for item in sample}), 60)
        self.assertEqual({item.kind for item in sample}, {"let", "closure"})
        self.assertEqual(len(bulk.sample_findings(findings[:3])), 3)

    def test_busy_path_or_unreadable_board_refuses_writes(self) -> None:
        reservation = berth_board.Reservation("other", "x", self.root / "other", "branch", ("src/lib.rs",), "active", "blocking")
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((reservation,))):
            with self.assertRaisesRegex(bulk.BulkRenameError, "holds"):
                bulk.check_available(self.repo, "src/lib.rs")
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.BoardUnavailable("offline")):
            with self.assertRaisesRegex(bulk.BulkRenameError, "offline"):
                bulk.check_available(self.repo, "src/lib.rs")
        own = replace(reservation, holder_worktree_root=self.repo)
        with mock.patch.object(berth_board, "read_board", return_value=berth_board.Board((own,))):
            bulk.check_available(self.repo, "src/lib.rs")

    def test_workspace_dependents_are_verified_transitively(self) -> None:
        with (self.repo / "Cargo.toml").open("a") as manifest:
            _ = manifest.write('\n[workspace]\nmembers=["dependent", "last"]\n')
        for name, dependency, path in (("dependent", "bulk_sample", ".."), ("last", "dependent", "../dependent")):
            folder = self.repo / name
            (folder / "src").mkdir(parents=True)
            _ = (folder / "src/lib.rs").write_text("pub fn f() {}\n")
            _ = (folder / "Cargo.toml").write_text(
                f'[package]\nname="{name}"\nversion="0.1.0"\nedition="2021"\n'
                + f'[dependencies]\n{dependency}={{path="{path}"}}\n')
        metadata = self.root / "metadata.json"
        _ = metadata.write_text(json.dumps(workspace_metadata(self.repo)))
        cargo = self.root / "tools/cargo"
        _ = cargo.write_text(
            '#!/bin/sh\n'
            + 'if [ "$*" = "metadata --no-deps --format-version 1" ]; then\n'
            + '    cat "$TEST_METADATA_FILE"; exit 0\n'
            + 'fi\necho "Unexpected cargo call: $*" >&2; exit 99\n'
        )
        self.enterContext(mock.patch.dict(os.environ, {"TEST_METADATA_FILE": str(metadata)}))
        self.assertEqual(bulk.verification_packages(self.repo, "."), ("bulk_sample", "dependent", "last"))
        self.assertEqual(bulk.verification_packages(self.repo, "last"), ("last",))


def stop_process(command: Callable[[], object]) -> None:
    """A fault point exits the child; cleanup handlers cannot mask process loss."""
    child = os.fork()
    if child == 0:
        try:
            _ = command()
        except BaseException:
            traceback.print_exc()
        finally:
            os._exit(74)
    _, status = os.waitpid(child, 0)
    if os.waitstatus_to_exitcode(status) != 73:
        raise AssertionError(f"fault point was not reached: {status}")


def stop_after_edit(path: Path, command: Callable[[], object]) -> None:
    """Lose the child process immediately after a named write, without exception cleanup."""
    def execute() -> None:
        write_text, write_bytes = Path.write_text, Path.write_bytes

        def text(target: Path, content: str) -> int:
            result = write_text(target, content)
            if target == path:
                os._exit(73)
            return result

        def data(target: Path, content: bytes) -> int:
            result = write_bytes(target, content)
            if target == path:
                os._exit(73)
            return result

        with mock.patch.object(Path, "write_text", text), mock.patch.object(Path, "write_bytes", data):
            _ = command()
    stop_process(execute)


class LedgerTests(CargoProjectTests):
    def test_review_keeps_refusals_after_process_loss_on_ledger_release(self) -> None:
        ledger = self.ledger()
        kept, refused = self.ticket(ledger)
        server = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))
        release = style_ledger.Ledger.bulk_release

        def stopped(instance: style_ledger.Ledger, ticket: str,
                    reasons: dict[str, str]) -> tuple[style_ledger.BulkRefusal, ...]:
            _ = release(instance, ticket, reasons)
            os._exit(73)

        with (mock.patch.object(bulk, "RustAnalyzer", return_value=nullcontext(server)),
              mock.patch.object(shutil, "which", return_value="rust-analyzer"),
              mock.patch.object(bulk, "verification_packages", return_value=("bulk_sample",)),
              mock.patch.object(bulk, "check_available"), mock.patch.object(bulk, "command")):
            with mock.patch.object(style_ledger.Ledger, "bulk_release", stopped):
                stop_process(lambda: bulk.run_batch(self.repo, "names-1"))
            self.assertEqual([row.finding_id for row in ledger.bulk_refusals("names-1")], [refused])
            with mock.patch("builtins.print") as output:
                review = bulk.run_batch(self.repo, "names-1")
        payload = bulk.object_fields(cast(object, json.loads(review.read_text())))
        self.assertEqual(payload["kept"], [kept])
        self.assertEqual(payload["refusals"], [dict(finding_id=refused, reason=ledger.bulk_refusals("names-1")[0].reason)])
        output.assert_called_once_with(f"bulk rename: 1 kept, 1 refused, 0 macro edits; review {review}")

    def test_batch_reruns_after_process_loss_on_first_edit(self) -> None:
        ledger = self.ledger()
        kept, _ = self.ticket(ledger)
        server = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))
        with (mock.patch.object(bulk, "RustAnalyzer", return_value=nullcontext(server)),
              mock.patch.object(shutil, "which", return_value="rust-analyzer"),
              mock.patch.object(bulk, "verification_packages", return_value=("bulk_sample",)),
              mock.patch.object(bulk, "check_available"), mock.patch.object(bulk, "command")):
            stop_after_edit(self.repo / "src/lib.rs", lambda: bulk.run_batch(self.repo, "names-1"))
            self.assertIn("let number", (self.repo / "src/lib.rs").read_text())
            review = bulk.run_batch(self.repo, "names-1")
        payload = bulk.object_fields(cast(object, json.loads(review.read_text())))
        self.assertEqual(payload["kept"], [kept])
        self.assertIn("let number", (self.repo / "src/lib.rs").read_text())

    def test_migration_and_per_finding_release_are_atomic_idempotent_and_leave_decisions_filed(self) -> None:
        ledger = self.ledger()
        kept, refused = self.ticket(ledger)
        _ = ledger.bulk_release("names-1", {refused: "not located"})
        self.assertEqual([finding.finding_id for finding in ledger.bulk_findings("names-1")], [kept])
        finding, = ledger.named_held((refused,))
        self.assertIsInstance(finding.state, style_ledger.Filed)
        self.assertIsInstance(finding.build_ticket, style_ledger.NoBuildTicket)
        rows = ledger.bulk_refusals("names-1")
        self.assertEqual((rows[0].finding_id, rows[0].ticket, rows[0].reason), (refused, "names-1", "not located"))
        self.assertTrue(rows[0].at)
        ticket_before = ledger.bulk_ticket("names-1")
        self.assertEqual(ledger.bulk_release("names-1", {refused: "different"}), rows)
        self.assertEqual(ledger.bulk_ticket("names-1"), ticket_before)
        with self.assertRaises(style_ledger.LedgerError):
            _ = ledger.bulk_release("names-1", {kept: "would release", "unknown": "not owned"})
        self.assertEqual([finding.finding_id for finding in ledger.bulk_findings("names-1")], [kept])
        self.assertEqual(ledger.bulk_refusals("names-1"), rows)
        _ = ledger.bulk_release("names-1", {kept: "all refused"})
        self.assertEqual(ledger.bulk_findings("names-1"), ())
        with closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0],
                             max(style_ledger.MIGRATIONS))

    def test_release_requires_assigned_proposal_and_nonempty_reason(self) -> None:
        ledger = self.ledger()
        kept, _ = self.ticket(ledger)
        with self.assertRaises(style_ledger.LedgerError):
            _ = ledger.bulk_release("names-1", {kept: " "})
        ledger.ticket_drop("names-1")
        with self.assertRaises(style_ledger.LedgerError):
            _ = ledger.bulk_release("names-1", {kept: "stale"})
        self.assertEqual(ledger.bulk_refusals("names-1"), ())

    def test_batch_publishes_only_proven_verified_edits_and_writes_the_review_sample(self) -> None:
        ledger = self.ledger()
        kept, refused = self.ticket(ledger)
        server = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))
        with (mock.patch.object(bulk, "RustAnalyzer", return_value=nullcontext(server)),
              mock.patch.object(shutil, "which", return_value="rust-analyzer"),
              mock.patch.object(bulk, "verification_packages", return_value=("bulk_sample",)),
              mock.patch.object(bulk, "check_available"), mock.patch.object(bulk, "command")):
            review = bulk.run_batch(self.repo, "names-1")
        payload = cast(dict[str, object], json.loads(review.read_text()))
        self.assertEqual(payload["kept"], [kept])
        self.assertEqual(payload["macro_edits"], 0)
        self.assertEqual(len(cast(list[object], payload["sample"])), 1)
        self.assertIn("let number", (self.repo / "src/lib.rs").read_text())
        self.assertEqual([row.finding_id for row in ledger.bulk_refusals("names-1")], [refused])
        self.assertIsInstance(ledger.named_held((kept,))[0].state, style_ledger.Filed)

    def test_failed_crate_releases_its_renames_and_restores_the_file(self) -> None:
        ledger = self.ledger()
        kept, refused = self.ticket(ledger)
        source = (self.repo / "src/lib.rs").read_text()
        server = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))
        def verify(repo: Path, _arguments: list[str]) -> None:
            if "let number" in (repo / "src/lib.rs").read_text():
                raise bulk.BulkRenameError("crate failed")
        with (mock.patch.object(bulk, "RustAnalyzer", return_value=nullcontext(server)),
              mock.patch.object(shutil, "which", return_value="rust-analyzer"),
              mock.patch.object(bulk, "verification_packages", return_value=("bulk_sample",)),
              mock.patch.object(bulk, "check_available"), mock.patch.object(bulk, "command", side_effect=verify)):
            review = bulk.run_batch(self.repo, "names-1")
        payload = cast(dict[str, object], json.loads(review.read_text()))
        self.assertEqual(payload["kept"], [])
        self.assertEqual(payload["sample"], [])
        self.assertEqual((self.repo / "src/lib.rs").read_text(), source)
        self.assertEqual({row.finding_id for row in ledger.bulk_refusals("names-1")}, {kept, refused})
        self.assertEqual(ledger.bulk_findings("names-1"), ())

    def test_existing_database_gets_the_refusal_migration(self) -> None:
        ledger = self.ledger()
        with closing(sqlite3.connect(ledger.database)) as connection:
            _ = connection.execute("DROP TABLE bulk_refusals")
            _ = connection.execute("DROP TABLE refill_results")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN built_at")
            _ = connection.execute("UPDATE schema_version SET version=23")
            connection.commit()
        with style_ledger.Ledger(self.repo, ledger.database) as upgraded:
            self.assertEqual(upgraded.bulk_refusals("names-1"), ())
        # The refusal migration also accepts an existing refusal table.
        with closing(sqlite3.connect(ledger.database)) as connection:
            _ = connection.execute("DROP TABLE refill_results")
            _ = connection.execute("ALTER TABLE held_findings DROP COLUMN built_at")
            _ = connection.execute("UPDATE schema_version SET version=23")
            connection.commit()
        with style_ledger.Ledger(self.repo, ledger.database) as replayed:
            self.assertEqual(replayed.bulk_refusals("names-1"), ())

    def test_dirty_batch_exits_before_starting_analyzer(self) -> None:
        _ = (self.repo / "src/lib.rs").write_text("dirty\n")
        with mock.patch.object(bulk, "RustAnalyzer") as analyzer:
            with self.assertRaisesRegex(bulk.BulkRenameError, "clean"):
                _ = bulk.run_batch(self.repo, "names-1")
            analyzer.assert_not_called()


@unittest.skipUnless(
    os.environ.get("BULK_RENAME_REAL_TOOLS") == "1" and shutil.which("rust-analyzer"),
    "real LSP and compiler tests run only with BULK_RENAME_REAL_TOOLS=1 and rust-analyzer on PATH",
)
class RustAnalyzerTests(CargoProjectTests):
    @override
    def set_up_tools(self) -> None:
        """Keep the caller's PATH, CARGO_HOME and RUSTUP_HOME."""

    def test_real_f006_shorthand_preserves_both_targets_and_detects_either_capture(self) -> None:
        source = ("struct Pair { old: usize }\nstruct R { number: usize }\n"
                  + "fn f() { let number = 1; let _ = R { number }; let _ = Pair { old: number }; }\n")
        root = self.repo / "src/lib.rs"
        _ = root.write_text(source)
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            definitions = analyzer.definitions
            pairs: list[bulk.FieldShorthandTargets] = []
            changed_role = ""
            def answers(path: Path, text: str, offsets: tuple[int, ...]) -> tuple[bulk.DefinitionResolution, ...]:
                result = list(definitions(path, text, offsets))
                for i, answer in enumerate(result):
                    if isinstance(answer, bulk.FieldShorthandTargets):
                        pairs.append(answer)
                        if changed_role and "struct Pair { number" in text:
                            result[i] = (replace(answer, field=replace(answer.field, end=(99, 0))) if changed_role == "field"
                                         else replace(answer, binding=replace(answer.binding, end=(99, 0))))
                return tuple(result)
            with mock.patch.object(analyzer, "definitions", side_effect=answers):
                result = bulk.rename_file(analyzer, root, "src/lib.rs", source, (self.finding(source, line=1),))
                self.assertEqual(result.refusals, ())
                self.assertIn("Pair { number }", result.edited)
                self.assertEqual(len(pairs), 3)
                self.assertEqual(pairs[0], pairs[1])
                self.assertEqual(pairs[1].binding, pairs[2].binding)
                self.assertNotEqual(pairs[1].field, pairs[2].field)
                for changed_role in ("field", "binding"):
                    analyzer.open(root, source)
                    captured = bulk.rename_file(analyzer, root, "src/lib.rs", source, (self.finding(source, line=1),))
                    self.assertEqual(captured.kept, ())
                    self.assertIn("capture 'number'", captured.refusals[0].reason)
                    self.assertEqual(analyzer.documents[root], source)

    def test_real_f008_twice_loaded_unrelated_glob_allows_rename(self) -> None:
        source = ('#[path="shared.rs"] mod first; #[path="shared.rs"] mod second;\n'
                  + 'use first::*; fn f() { let old = 1; consume(old); }\nfn consume(_: usize) {}\n')
        _ = (self.repo / "src/lib.rs").write_text(source)
        _ = (self.repo / "src/shared.rs").write_text("pub fn unrelated() {}\n")
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source,
                                      (self.finding(source),))
        self.assertEqual(result.refusals, ())
        self.assertIn("consume(number)", result.edited)

    def test_real_lsp_renames_all_site_kinds_shorthand_captures_and_shadowing_then_cargo_checks(self) -> None:
        source = '''struct Record { value: usize }
pub fn run(parameter: usize) -> usize {
    let value = parameter + 1;
    let Record { value: field } = Record { value };
    let closure = |element: usize| element + 1;
    let mut total = closure(1);
    for item in [1, 2] { total += item; }
    if let Some(conditional) = Some(1) { total += conditional; }
    match Some(2) { Some(arm) => total += arm, None => () }
    println!("{value} {{value}}");
    let value = value + 1;
    total + value + field
}
'''
        _ = (self.repo / "src/lib.rs").write_text(source)
        names = ((2, "parameter", "input_number"), (3, "value", "outer_number"), (4, "field", "field_number"),
                 (5, "closure", "increment"), (5, "element", "element_number"), (6, "total", "sum"),
                 (7, "item", "item_number"), (8, "conditional", "conditional_number"), (9, "arm", "arm_number"),
                 (11, "value", "inner_number"))
        findings = tuple(self.finding(source, old, new, line, old + str(line)) for line, old, new in names)
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source, findings)
        self.assertEqual(result.refusals, ())
        self.assertEqual(len(result.kept), len(findings))
        self.assertEqual({item.kind for item in result.kept}, {"fn parameter", "let", "closure", "for", "condition let", "match arm"})
        self.assertIn("Record { value: outer_number }", result.edited)
        self.assertIn("let inner_number = outer_number + 1", result.edited)
        self.assertIn('"{outer_number} {{value}}"', result.edited)
        proof = binding_rename_check("src/lib.rs", source, result.edited, tuple(finding.rename for finding in findings))
        self.assertTrue(proof.explained)
        self.assertTrue(all(isinstance(check, AppliedInCode) for check in proof.checks.values()))
        _ = (self.repo / "src/lib.rs").write_text(result.edited)
        result_check = subprocess.run(["cargo", "check", "--offline"], cwd=self.repo, capture_output=True, text=True, check=False)
        self.assertEqual(result_check.returncode, 0, result_check.stderr)

    def test_real_lsp_parameter_comments_rebase_the_next_prepare_rename(self) -> None:
        source = "/// Add `first` and `second`.\npub fn run(first: usize, second: usize) -> usize { first + second }\n"
        _ = (self.repo / "src/lib.rs").write_text(source)
        findings = (self.finding(source, "first", "first_number", 2, "first"),
                    self.finding(source, "second", "second_number", 2, "second"))
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source, findings)
        self.assertEqual(result.refusals, ())
        self.assertEqual(result.edited, source.replace("first", "first_number").replace("second", "second_number"))

    def test_real_resolution_preserves_items_other_functions_types_and_raw_names(self) -> None:
        cases = (
            ("type old = usize;\nfn f() {\n let old = 1;\n let _ = old + 1;\n}\n", 3),
            ("fn other() { let number = 2; let _ = number + 1; }\nfn f() {\n let old = 1;\n let _ = old + 1;\n}\n", 3),
            ("type old = usize;\nfn f() {\n let old = 1;\n let typed: old = old + 1;\n}\n", 3),
            ("fn other() { let r#number = 2; let _ = r#number + 1; }\nfn f() {\n let old = 1;\n let _ = old + 1;\n}\n", 3),
        )
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            for source, line in cases:
                with self.subTest(source=source):
                    finding = self.finding(source, line=line)
                    self.assertIsInstance(binding_scope(source, finding.rename), NoBindingScope)
                    result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source, (finding,))
                    self.assertEqual(result.refusals, ())
                    self.assertEqual(len(result.kept), 1)
                    self.assertTrue(bulk.file_edits_preserved(result, result.edited.replace(" let", "    let")))
                    self.assertFalse(bulk.file_edits_preserved(result, result.edited.replace("+ 1", "+ 2")))
                    self.assertFalse(binding_rename_check("src/lib.rs", source, result.edited, (finding.rename,)).explained)
                    if "typed:" in source:
                        self.assertIn("typed: old = number", result.edited)

    def test_real_resolution_refuses_capture_and_names_the_token(self) -> None:
        source = "fn f() {\n let number = 2;\n let old = 1;\n let _ = old + number;\n}\n"
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source,
                                      (self.finding(source, line=3),))
        self.assertEqual(result.edited, source)
        self.assertEqual(result.kept, ())
        self.assertIn("capture 'number' at line 4", result.refusals[0].reason)

    def test_real_resolution_batches_two_findings_in_one_file(self) -> None:
        source = ("type old = usize; type other = usize;\nfn f() {\n let old = 1;\n let _ = old + 1;\n}\n"
                  "fn g() {\n let other = 2;\n let _ = other + 1;\n}\n")
        findings = (self.finding(source, line=3), self.finding(source, "other", "second", 7, "second"))
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer, mock.patch.object(
                analyzer, "definitions", wraps=analyzer.definitions) as definitions:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source, findings)
            self.assertEqual(definitions.call_count, 2)
        self.assertEqual(result.refusals, ())
        self.assertEqual(len(result.kept), 2)

    def test_real_resolution_handles_names_introduced_by_an_earlier_rename(self) -> None:
        source = ("#![allow(dead_code)]\nfn f() {\n let old = 1;\n let _ = old + 1;\n}\n"
                  "fn g() {\n let old = 2;\n let _ = old + 1;\n}\nfn negation() -> bool { !(true) }\n")
        findings = (self.finding(source, line=3), self.finding(source, line=7, identity="second"))
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer, mock.patch.object(
                analyzer, "definitions", wraps=analyzer.definitions) as definitions:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source, findings)
            self.assertEqual(definitions.call_count, 3)
        self.assertEqual(result.refusals, ())
        self.assertEqual(len(result.kept), 2)
        self.assertEqual(result.edited, source.replace("old", "number"))

    def test_real_refactor_field_updates_literal_pattern_and_access(self) -> None:
        source = "struct Record { old: usize }\nfn f() -> usize { let r = Record { old: 1 }; let Record { old } = r; r.old + old }\n"
        _ = (self.repo / "src/lib.rs").write_text(source)
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source,
                                      (self.finding(source, line=1),))
        self.assertEqual(result.refusals, ())
        self.assertIn("struct Record { number: usize }", result.edited)
        self.assertIn("Record { number: 1 }", result.edited)
        self.assertIn("Record { number: old }", result.edited)
        self.assertIn("r.number + old", result.edited)

    def test_real_refactor_call_and_two_bindings(self) -> None:
        source = "fn consume(_: usize, _: usize) {}\nfn f() { let (old, untouched) = (1, 2); consume(old, untouched); }\n"
        _ = (self.repo / "src/lib.rs").write_text(source)
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source,
                                      (self.finding(source, line=2),))
        self.assertEqual(result.refusals, ())
        self.assertEqual(result.edited, source.replace("old", "number"))

    def test_real_refactor_glob_clash_and_safe_glob(self) -> None:
        source = "mod provider { pub fn number() {} }\nuse provider::*;\nfn f() { let old = 1; let _ = old; }\n"
        _ = (self.repo / "src/lib.rs").write_text(source)
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            with mock.patch.object(analyzer, "rename", wraps=analyzer.rename) as rename:
                result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source,
                                          (self.finding(source, line=3),))
                self.assertEqual(rename.call_count, 0)
            self.assertIn("glob import clash", result.refusals[0].reason)
            safe = source.replace("pub fn number", "pub fn unrelated")
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", safe,
                                      (self.finding(safe, line=3),))
            self.assertEqual(result.refusals, ())
            self.assertIn("let number = 1", result.edited)

    def test_real_refactor_external_glob_export_clash(self) -> None:
        source = "mod provider;\nuse provider::*;\nfn f() { let old = 1; let _ = old; }\n"
        _ = (self.repo / "src/lib.rs").write_text(source)
        _ = (self.repo / "src/provider.rs").write_text("pub fn number() {}\n")
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source,
                                      (self.finding(source, line=3),))
        self.assertEqual(result.kept, ())
        self.assertIn("is exported by provider", result.refusals[0].reason)

    def test_real_refactor_hidden_clash_is_reverted_by_workspace_check(self) -> None:
        import bulk_run
        source = ('#![allow(dead_code, unused_imports, unexpected_cfgs)]\nmod provider { pub fn unused() {} }\n'
                  'use provider::*;\ninclude!("generated.rs");\nfn f() { let old = 1; let _ = old; }\n')
        _ = (self.repo / "src/lib.rs").write_text(source)
        _ = (self.repo / "src/generated.rs").write_text("const NUMBER: usize = 7;\n")
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source,
                                      (self.finding(source, new="NUMBER", line=5),))
        self.assertEqual(result.edited, source)
        self.assertIn("unsupported source loader: include!", result.refusals[0].reason)
        # Includes refuse even when their contents would be inactive.
        _ = (self.repo / "src/generated.rs").write_text("#[cfg(end_gate)] const NUMBER: usize = 7;\n")
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source,
                                      (self.finding(source, new="NUMBER", line=5),))
            self.assertEqual(analyzer.documents[self.repo / "src/lib.rs"], source)
        self.assertEqual(result.edited, source)
        self.assertIn("unsupported source loader: include!", result.refusals[0].reason)
        # Compiler-only configuration can still fail for reasons beyond name resolution.
        _ = (self.repo / "src/generated.rs").write_text("")
        source = source.replace('include!("generated.rs");', "").replace(
            "fn f()", "#[cfg_attr(end_gate, deny(non_snake_case))] fn f()")
        _ = (self.repo / "src/lib.rs").write_text(source)
        _ = self.git("add", ".")
        _ = self.git("commit", "-qm", "configured hidden constant fixture")
        base = self.git("rev-parse", "HEAD")
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source,
                                      (self.finding(source, new="NUMBER", line=5),))
        self.assertEqual(result.refusals, ())
        _ = (self.repo / "src/lib.rs").write_text(result.edited)
        workspace = bulk_run.WorkspaceEdits(self.repo, base, (result,))
        with mock.patch.dict(os.environ, {"RUSTFLAGS": "--cfg end_gate"}):
            kept, reverted = bulk_run.validate(workspace, bulk_run.run_command, {"check_clippy": 0.0}, [])
        self.assertEqual(kept, set())
        self.assertEqual(reverted, {"finding": "workspace diagnostics"})
        self.assertEqual((self.repo / "src/lib.rs").read_text(), source)

    def test_real_clap_binary_keeps_requires_id_flag_help_and_usage(self) -> None:
        source = ('use clap::{Parser, CommandFactory};\n#[derive(Parser)]\n'
                  'struct Options {\n #[arg(long)] old: bool,\n'
                  ' #[arg(long, requires = "old")] dependent: bool,\n}\n'
                  'fn main() {\n'
                  ' let matches = Options::command().try_get_matches_from(["tool", "--old", "--dependent"]).unwrap();\n'
                  ' assert_eq!(matches.get_one::<bool>("old"), Some(&true));\n'
                  ' let parsed = Options::try_parse_from(["tool", "--old", "--dependent"]).unwrap();\n'
                  ' assert!(parsed.old && parsed.dependent);\n'
                  ' assert!(Options::try_parse_from(["tool", "--dependent"]).is_err());\n'
                  ' let mut command = Options::command();\n'
                  ' println!("{}", command.render_help());\n'
                  ' println!("{}", command.render_long_help());\n'
                  ' println!("{}", command.render_usage());\n}\n')
        manifest = self.repo / 'Cargo.toml'
        _ = manifest.write_text(manifest.read_text() + '\n[dependencies]\nclap={version="=4.6.7", features=["derive"]}\n')
        path = self.repo / 'src/main.rs'
        _ = path.write_text(source)
        before = subprocess.run(['cargo', 'run', '--offline', '--quiet'], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(before.returncode, 0, before.stdout + before.stderr)
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            finding = replace(self.finding(source, line=4), path='src/main.rs')
            result = bulk.rename_file(analyzer, path, 'src/main.rs', source, (finding,))
        self.assertEqual(result.refusals, ())
        self.assertIn('#[arg(id = "old", long = "old")] number: bool', result.edited)
        _ = path.write_text(result.edited)
        after = subprocess.run(['cargo', 'run', '--offline', '--quiet'], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
        self.assertEqual(after.stdout, before.stdout)

    def test_real_refactor_serde_field_preserves_its_wire_name(self) -> None:
        source = ('#[derive(serde::Serialize, serde::Deserialize, clap::Parser)]\n'
                  'struct Options { #[arg(long)] old_name: usize }\n'
                  '#[test] fn stable_names() { use clap::{Parser, CommandFactory};\n'
                  ' let matches = Options::command().try_get_matches_from(["tool", "--old-name", "7"]).unwrap();\n'
                  ' assert_eq!(matches.get_one::<usize>("old_name"), Some(&7));\n'
                  ' let help = Options::command().render_help().to_string();\n'
                  ' assert!(help.contains("--old-name <OLD_NAME>"));\n'
                  ' let parsed = Options::try_parse_from(["tool", "--old-name", "7"]).unwrap();\n'
                  ' assert_eq!(serde_json::to_string(&parsed).unwrap(), "{\\\"old_name\\\":7}");\n'
                  ' let decoded: Options = serde_json::from_str("{\\\"old_name\\\":8}").unwrap();\n'
                  ' assert_eq!(decoded.old_name, 8); }\n')
        manifest = self.repo / 'Cargo.toml'
        _ = manifest.write_text(manifest.read_text() + '\n[dependencies]\nserde={version="=1.0.229", features=["derive"]}\nclap={version="=4.6.7", features=["derive"]}\nserde_json="=1.0.154"\n')
        _ = (self.repo / "src/lib.rs").write_text(source)
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source,
                                      (self.finding(source, old='old_name', line=2),))
        self.assertEqual(result.refusals, ())
        self.assertIn('#[serde(rename = "old_name")] #[arg(id = "old_name", long = "old-name", value_name = "OLD_NAME")] number: usize', result.edited)
        _ = (self.repo / 'src/lib.rs').write_text(result.edited)
        checked = subprocess.run(['cargo', 'test', '--offline', '--quiet'], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_real_refactor_applies_other_documents_in_the_same_crate(self) -> None:
        source = "mod record;\nfn f(r: record::Record) -> usize { r.old }\n"
        declaration = "pub(crate) struct Record { pub(crate) old: usize }\n"
        _ = (self.repo / "src/lib.rs").write_text(source)
        path = self.repo / "src/record.rs"
        _ = path.write_text(declaration)
        finding = bulk.RenameFinding("field", "src/record.rs", BindingRename(1, declaration.strip(), "old", "number"))
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            result = bulk.rename_file(analyzer, path, "src/record.rs", declaration, (finding,))
        self.assertEqual(result.refusals, ())
        self.assertEqual(len(result.related), 1)
        self.assertIn("r.number", result.related[0].edited)
        self.assertEqual(result.dependencies, frozenset({"src/record.rs", "src/lib.rs"}))
        combined = bulk.combine_file_renames((result,))
        self.assertEqual(len(combined), 2)
        self.assertTrue(all(file.dependencies == result.dependencies for file in combined))

    def test_real_f002_item_capture_refuses_and_restores_the_overlay(self) -> None:
        source = "fn old() -> usize { 1 }\nfn f() -> usize { fn number() -> usize { 2 } old() }\n"
        _ = (self.repo / "src/lib.rs").write_text(source)
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source,
                                      (self.finding(source, line=1),))
            self.assertEqual(analyzer.documents[self.repo / "src/lib.rs"], source)
        self.assertEqual(result.edited, source)
        self.assertEqual(result.kept, ())
        self.assertIn("capture 'number' at line 2", result.refusals[0].reason)

    def test_real_f005_unedited_glob_reference_refuses_and_restores(self) -> None:
        source = "mod child;\nfn old(value: usize) { println!(\"{}\", value + 10); }\n"
        child = self.repo / "src/child.rs"
        child_source = "use crate::*;\npub fn run() { drop(1usize); }\n"
        _ = (self.repo / "src/lib.rs").write_text(source)
        _ = child.write_text(child_source)
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source,
                                      (self.finding(source, new="drop"),))
            self.assertEqual(result.kept, ())
            self.assertIn("glob capture in an unedited file", result.refusals[0].reason)
            self.assertIn("'drop' at line 2", result.refusals[0].reason)
            self.assertIn("child.rs", result.refusals[0].reason)
            self.assertEqual(analyzer.documents[self.repo / "src/lib.rs"], source)
            self.assertEqual(analyzer.documents[child], child_source)
        self.assertEqual(result.edited, source)
        self.assertEqual(child.read_text(), child_source)

    def test_real_f005_unedited_unrelated_reference_is_checked_and_preserved(self) -> None:
        source = "mod child;\nmod quiet;\nfn old() {}\n"
        child = self.repo / "src/child.rs"
        child_source = "pub fn run() { let number = 1usize; std::mem::drop(number); }\n"
        quiet = self.repo / "src/quiet.rs"
        _ = (self.repo / "src/lib.rs").write_text(source)
        _ = child.write_text(child_source)
        _ = quiet.write_text("fn unrelated() {}\n")
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer, mock.patch.object(
                analyzer, "definitions", wraps=analyzer.definitions) as definitions:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source,
                                      (self.finding(source, line=3),))
            self.assertEqual(result.refusals, ())
            self.assertEqual(len(result.kept), 1)
            checked = [call.args[0] for call in definitions.call_args_list]
            self.assertEqual(checked.count(child), 2)
            self.assertNotIn(quiet, checked)
            self.assertEqual(analyzer.documents[child], child_source)
        self.assertIn("fn number()", result.edited)
        self.assertEqual(result.related, ())
        self.assertEqual(child.read_text(), child_source)

    def test_real_f005_nested_manifest_module_glob_capture_refuses(self) -> None:
        source = "mod child;\nfn old(value: usize) { println!(\"{}\", value + 10); }\n"
        child_dir = self.repo / "src/child"
        child_dir.mkdir()
        _ = (child_dir / "Cargo.toml").write_text('[package]\nname="unrelated"\nversion="0.1.0"\n')
        child = child_dir / "mod.rs"
        _ = child.write_text("use crate::*;\npub fn run() { drop(1usize); }\n")
        _ = (self.repo / "src/lib.rs").write_text(source)
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source,
                                      (self.finding(source, new="drop"),))
            self.assertEqual(result.kept, ())
            self.assertIn("glob capture in an unedited file", result.refusals[0].reason)
            self.assertEqual(analyzer.documents[self.repo / "src/lib.rs"], source)

    def test_real_f005_path_into_nested_manifest_refuses(self) -> None:
        source = '#[path = "hidden/child.rs"] mod child;\nfn old(value: usize) {}\n'
        child_dir = self.repo / "src/hidden"
        child_dir.mkdir()
        _ = (child_dir / "Cargo.toml").write_text('[package]\nname="unrelated"\nversion="0.1.0"\n')
        _ = (child_dir / "child.rs").write_text("use crate::*; pub fn run() { drop(1usize); }")
        _ = (self.repo / "src/lib.rs").write_text(source)
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source,
                                      (self.finding(source, new="drop"),))
            self.assertEqual(result.kept, ())
            self.assertIn("glob capture in an unedited file", result.refusals[0].reason)
            self.assertEqual(analyzer.documents[self.repo / "src/lib.rs"], source)

    def test_real_f005_harmless_path_module_preserves_unrelated_reference(self) -> None:
        source = '#[path="hidden/child.txt"] mod child;\nfn old() {}\n'
        child = self.repo / "src/hidden/child.txt"
        child.parent.mkdir()
        child_source = 'pub fn run() { let number = 1; std::mem::drop(number); }'
        _ = child.write_text(child_source)
        _ = (self.repo / "src/lib.rs").write_text(source)
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer, mock.patch.object(
                analyzer, "definitions", wraps=analyzer.definitions) as definitions:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source,
                                      (self.finding(source),))
            self.assertEqual(result.refusals, ())
            self.assertEqual(len(result.kept), 1)
            self.assertEqual([call.args[0] for call in definitions.call_args_list].count(child), 2)
            self.assertEqual(analyzer.documents[child], child_source)
        self.assertIn("fn number()", result.edited)

    def test_real_f003_local_format_macro_is_not_standard(self) -> None:
        source = ('macro_rules! format { ($value:ident) => {{ let _ = $value; stringify!($value) }} }\n'
                  "fn f() -> &'static str { let old = 1; format!(old) }\n")
        _ = (self.repo / "src/lib.rs").write_text(source)
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source,
                                      (self.finding(source, line=2),))
        self.assertEqual(result.edited, source)
        self.assertEqual(result.kept, ())
        self.assertIn("macro origin is not standard: format at line 2", result.refusals[0].reason)

    def test_real_f004_standard_derive_with_restricted_visibility_allows_field_rename(self) -> None:
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer:
            for visibility in ("", "pub(crate) ", "pub(super) ", "pub(in crate) "):
                with self.subTest(visibility=visibility):
                    source = ('mod inner {\n#[derive(Clone)]\n' + visibility + 'struct Record { old: usize }\n'
                              + 'fn f(r: Record) -> usize { r.old }\n}\n')
                    result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source,
                                              (self.finding(source, line=3),))
                    self.assertEqual(result.refusals, ())
                    self.assertEqual(len(result.kept), 1)
                    self.assertIn("struct Record { number: usize }", result.edited)
                    self.assertIn("r.number", result.edited)

    def test_real_f001_duplicate_function_parameter_refuses_before_rename(self) -> None:
        source = "fn f(old: usize, number: usize) {}\n"
        _ = (self.repo / "src/lib.rs").write_text(source)
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer, mock.patch.object(
                analyzer, "rename", wraps=analyzer.rename) as rename:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source,
                                      (self.finding(source, line=1),))
            rename.assert_not_called()
        self.assertEqual(result.edited, source)
        self.assertIn("'number' is already a binding in the same pattern or parameter list at line 1", result.refusals[0].reason)

    def test_real_f001_duplicate_closure_parameter_refuses_before_rename(self) -> None:
        source = "fn f() { let closure = |old: usize, number: usize| {}; }\n"
        _ = (self.repo / "src/lib.rs").write_text(source)
        with bulk.RustAnalyzer(self.repo, timeout=120) as analyzer, mock.patch.object(
                analyzer, "rename", wraps=analyzer.rename) as rename:
            result = bulk.rename_file(analyzer, self.repo / "src/lib.rs", "src/lib.rs", source,
                                      (self.finding(source, line=1),))
            rename.assert_not_called()
        self.assertEqual(result.edited, source)
        self.assertIn("'number' is already a binding in the same pattern or parameter list at line 1", result.refusals[0].reason)


class ToolEnvironmentTests(unittest.TestCase):
    def test_only_real_tool_setup_keeps_the_callers_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bulk-tool-paths-") as directory:
            root = Path(directory)
            caller_tools = root / "caller-tools"
            caller_tools.mkdir()
            for name in ("cargo", "rust-analyzer"):
                tool = caller_tools / name
                _ = tool.write_text("#!/bin/sh\nexit 99\n")
                tool.chmod(0o755)
            caller_environment = {
                "PATH": str(caller_tools),
                "CARGO_HOME": str(root / "caller-cargo"),
                "RUSTUP_HOME": str(root / "caller-rustup"),
            }
            for fixture in (CargoProjectTests(), RustAnalyzerTests(
                "test_real_lsp_parameter_comments_rebase_the_next_prepare_rename"
            )):
                with self.subTest(fixture=type(fixture).__name__), mock.patch.dict(
                    os.environ, caller_environment
                ), mock.patch.object(fixture, "git", return_value=""):
                    try:
                        fixture.setUp()
                        stubs = fixture.root / "tools"
                        if isinstance(fixture, RustAnalyzerTests):
                            expected_tools = caller_tools
                            self.assertEqual(os.environ["CARGO_HOME"], caller_environment["CARGO_HOME"])
                            self.assertEqual(os.environ["RUSTUP_HOME"], caller_environment["RUSTUP_HOME"])
                            self.assertFalse(stubs.exists())
                        else:
                            expected_tools = stubs
                            self.assertEqual(os.environ["CARGO_HOME"], str(fixture.root / "cargo-home"))
                            self.assertEqual(os.environ["RUSTUP_HOME"], str(fixture.root / "rustup"))
                        self.assertEqual(os.environ["PATH"], str(expected_tools))
                        for name in ("cargo", "rust-analyzer"):
                            self.assertEqual(shutil.which(name), str(expected_tools / name))
                    finally:
                        _ = fixture.doCleanups()


if __name__ == "__main__":
    _ = unittest.main()
