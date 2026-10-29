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
from approved_renames import AppliedInCode, BindingRename, binding_rename_check


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

class ScriptedServer:
    def __init__(self, edit: Callable[[str, int, str], tuple[bulk.SourceEdit, ...]]) -> None:
        self.edit: Callable[[str, int, str], tuple[bulk.SourceEdit, ...]] = edit
        self.calls: list[tuple[int, str]] = []
        self.changes: list[str] = []
        self.opened: str = ""

    def open(self, path: Path, text: str) -> None:
        _ = path
        self.opened = text

    def change(self, path: Path, text: str) -> None:
        _ = path
        self.changes.append(text)

    def rename(self, path: Path, text: str, offset: int, new: str) -> tuple[bulk.SourceEdit, ...]:
        _ = path
        self.calls.append((offset, new))
        return self.edit(text, offset, new)


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
    def test_comments_are_rewritten_after_lsp_and_exact_names_are_preserved(self) -> None:
        source = ('struct AlertKind; fn alert_kind() {}\nfn f() {\n let alert = 1;\n'
                  ' // `alert`, `alert_count`, alerts, Alert, ALERT, alert.rs, alert-count.\n'
                  ' println!("{alert} {alert:?} {alert:>8} {alert_count}");\n let _ = alert + 1;\n}\n')
        result = self.rename(source, (self.finding(source, "alert", "number", 3),), ScriptedServer(
            lambda text, offset, new: word_edits(text, "alert", new, macros=False)))
        self.assertEqual(result.refusals, ())
        self.assertIn('// `number`, `alert_count`, alerts, Alert, ALERT, alert.rs, alert-count.', result.edited)
        self.assertIn('"{number} {number:?} {number:>8} {alert_count}"', result.edited)
        self.assertIn("struct AlertKind; fn alert_kind() {}", result.edited)

    def test_parameter_docs_and_body_links_are_rewritten(self) -> None:
        source = '/// Use `old` and [old].\n#[inline]\npub fn run(old: usize) -> usize {\n // [value](old)\n old + 1\n}\n'
        result = self.rename(source, (self.finding(source, line=3),), ScriptedServer(
            lambda text, offset, new: word_edits(text, "old", new)))
        self.assertEqual(result.refusals, ())
        self.assertEqual(result.edited, source.replace("old", "number"))

    def test_prose_in_scope_or_parameter_docs_refuses_before_lsp(self) -> None:
        for source, line in (("fn run() {\n let old = 1;\n // old is useful.\n let _ = old + 1;\n}\n", 2),
                             ("/// old is useful.\nfn run(old: usize) -> usize { old + 1 }\n", 2)):
            server = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))
            result = self.rename(source, (self.finding(source, line=line),), server)
            self.assertEqual(result.refusals, (bulk.Refusal("finding", "prose names the binding"),))
            self.assertEqual(result.edited, source)
            self.assertEqual(server.calls, [])

    def test_repository_docs_refuse_parameters_without_editing_markdown(self) -> None:
        source = "pub fn run(old: usize) -> usize { old + 1 }\n"
        for relative in ("README.md", "docs/api.md", "plans/rename.md", "as-builts/run.md"):
            path = self.repo / relative
            path.parent.mkdir(exist_ok=True)
            document = "Call `run` with `old`.\n"
            _ = path.write_text(document)
            server = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))
            result = self.rename(source, (self.finding(source, line=1),), server)
            self.assertEqual(result.refusals, (bulk.Refusal("finding", "docs name the binding"),))
            self.assertEqual(server.calls, [])
            self.assertEqual(path.read_text(), document)
            path.unlink()

    def test_markdown_guard_uses_exact_function_and_parameter_code_names(self) -> None:
        source = "pub fn run(alert: usize) -> usize { alert + 1 }\n"
        path = self.repo / "README.md"
        for document in ("run `alert_count` `alerts` `Alert` `ALERT` `alert.rs` `alert-count` `./alert` `../alert` `https://host/alert`",
                         "`run_all` `alert`", "`run.rs` `alert`", "run alert", "`RUN` `alert`", "run-all `alert`"):
            _ = path.write_text(document)
            result = self.rename(source, (self.finding(source, "alert", "number", 1),), ScriptedServer(
                lambda text, offset, new: word_edits(text, "alert", new)))
            self.assertEqual(result.refusals, (), document)
            self.assertEqual(path.read_text(), document)
        _ = path.write_text("run `alert`")
        result = self.rename(source, (self.finding(source, "alert", "number", 1),), ScriptedServer(
            lambda text, offset, new: word_edits(text, "alert", new)))
        self.assertEqual(result.refusals, (bulk.Refusal("finding", "docs name the binding"),))

    def test_comment_edits_before_parameters_rebase_pending_lsp_positions(self) -> None:
        source = '/// Use `first` and `second`.\nfn run(first: usize, second: usize) -> usize { first + second }\n'
        findings = (self.finding(source, "first", "first_number", 2, "first"),
                    self.finding(source, "second", "second_number", 2, "second"))
        def semantic(text: str, offset: int, new: str) -> tuple[bulk.SourceEdit, ...]:
            old = "first" if new == "first_number" else "second"
            self.assertEqual(text[offset:offset + len(old)], old)
            return word_edits(text, old, new)
        result = self.rename(source, findings, ScriptedServer(semantic))
        self.assertEqual(result.refusals, ())
        self.assertEqual(result.edited, source.replace("first", "first_number").replace("second", "second_number"))

    def test_whole_file_refusals_happen_before_lsp(self) -> None:
        for header, tail, reason in (("const number: usize = 1;", "old + 1", "new name already occurs in file"),
                                     ("use provider::*;", "old + 1", "file contains a glob import"),
                                     ("type old = usize;", "old + 1", "old name also occurs as an item or type")):
            source = f"{header} fn f() {{\n let old = 1;\n let _ = {tail};\n}}\n"
            server = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))
            result = self.rename(source, (self.finding(source),), server)
            self.assertEqual(result.refusals, (bulk.Refusal("finding", reason),))
            self.assertEqual(server.calls, [])

    def test_reverse_shorthand_refuses_the_new_field_label(self) -> None:
        source = "struct R { number: usize }\nfn f() {\n let old = 1;\n let _ = R { number: old };\n}\n"
        server = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))
        result = self.rename(source, (self.finding(source, line=3),), server)
        self.assertEqual(result.refusals, (bulk.Refusal("finding", "new name already occurs in file"),))
        self.assertEqual(server.calls, [])

    def test_renames_are_bottom_up_and_same_line_locations_use_current_text(self) -> None:
        source = "fn f() { let first = 1; let second = first + 2; let _ = second; }\n"
        findings = (self.finding(source, "first", "first_number", 1, "first"),
                    self.finding(source, "second", "second_number", 1, "second"))
        server = ScriptedServer(lambda text, offset, new: word_edits(text, "first" if new == "first_number" else "second", new))
        result = self.rename(source, findings, server)
        self.assertEqual([new for _, new in server.calls], ["second_number", "first_number"])
        self.assertEqual(len(result.kept), 2)
        self.assertEqual(result.refusals, ())
        self.assertIn("let second_number = first_number", result.edited)

    def test_missing_macro_uses_and_format_captures_are_repaired_and_counted(self) -> None:
        source = 'fn f() {\n let old = 1;\n assert_eq!(old, 1, "{old} {{old}}");\n}\n'
        server = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new, macros=False))
        result = self.rename(source, (self.finding(source),), server)
        self.assertEqual(len(result.kept), 1)
        self.assertEqual(result.kept[0].macro_edits, 2)
        self.assertIn('assert_eq!(number, 1, "{number} {{old}}")', result.edited)

    def test_unknown_macro_is_not_a_token_rewrite_escape_hatch(self) -> None:
        source = "fn f() {\n let old = 1;\n custom!(old);\n}\n"
        result = self.rename(source, (self.finding(source),), ScriptedServer(
            lambda text, offset, new: word_edits(text, "old", new, macros=False)))
        self.assertEqual(result.edited, source)
        self.assertEqual(result.kept, ())
        self.assertIn("macro", result.refusals[0].reason)

    def test_macro_repair_leaves_non_format_strings_unchanged(self) -> None:
        source = 'fn f() {\n let old = "{old}";\n assert_eq!(old, "{old}", "{old}");\n}\n'
        result = self.rename(source, (self.finding(source),), ScriptedServer(
            lambda text, offset, new: word_edits(text, "old", new, macros=False)))
        self.assertEqual(result.refusals, ())
        self.assertIn('let number = "{old}"', result.edited)
        self.assertIn('assert_eq!(number, "{old}", "{number}")', result.edited)

    def test_macro_repair_completes_a_partly_renamed_format_literal(self) -> None:
        source = 'fn f() {\n let old = 1;\n println!("{old} {old}");\n}\n'
        first_capture = source.index("{old}") + 1
        server = ScriptedServer(lambda text, offset, new: (
            *word_edits(text, "old", new, macros=False), bulk.SourceEdit(first_capture, first_capture + 3, new)))
        result = self.rename(source, (self.finding(source),), server)
        self.assertEqual(result.refusals, ())
        self.assertEqual(result.kept[0].macro_edits, 1)
        self.assertIn('"{number} {number}"', result.edited)

    def test_missed_non_macro_use_is_refused_and_server_keeps_the_original_document(self) -> None:
        source = (self.repo / "src/lib.rs").read_text()
        server = ScriptedServer(lambda text, offset, new: (bulk.SourceEdit(offset, offset + 3, new),))
        result = self.rename(source, (self.finding(source),), server)
        self.assertEqual(result.edited, source)
        self.assertEqual(server.changes, [])
        self.assertIn("missed", result.refusals[0].reason)

    def test_unrelated_edit_is_undone(self) -> None:
        source = (self.repo / "src/lib.rs").read_text()
        server = ScriptedServer(lambda text, offset, new: (*word_edits(text, "old", new), bulk.SourceEdit(7, 10, "other")))
        result = self.rename(source, (self.finding(source),), server)
        self.assertEqual(result.edited, source)
        self.assertEqual(len(result.refusals), 1)

    def test_semantic_edits_of_field_labels_and_qualified_names_are_refused(self) -> None:
        for expression in ("Record { old: old }", "data.old + old", "Type::old + old"):
            with self.subTest(expression=expression):
                source = f"fn f() {{\n let old = 1;\n let _ = {expression};\n}}\n"
                result = self.rename(source, (self.finding(source),), ScriptedServer(
                    lambda text, offset, new: word_edits(text, "old", new)))
                self.assertEqual(result.kept, ())
                self.assertEqual(result.edited, source)
                self.assertEqual(len(result.refusals), 1)

    def test_prepare_or_rename_refusal_returns_the_finding(self) -> None:
        source = (self.repo / "src/lib.rs").read_text()
        def refuse(_text: str, _offset: int, _new: str) -> tuple[bulk.SourceEdit, ...]:
            raise bulk.BulkRenameError("prepareRename refused")
        result = self.rename(source, (self.finding(source),), ScriptedServer(refuse))
        self.assertEqual(result.kept, ())
        self.assertEqual(result.refusals[0].reason, "prepareRename refused")

    def test_direct_call_arguments_are_excluded_before_lsp(self) -> None:
        for expression in ("consume(old)", "obj.consume(&old)", "consume(1, &mut old)", "consume(*old)", "consume::<usize>(old)"):
            with self.subTest(expression=expression):
                source = f"fn f() {{\n let old = 1;\n {expression};\n}}\n"
                server = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))
                result = self.rename(source, (self.finding(source),), server)
                self.assertEqual(server.calls, [])
                self.assertIn("passed straight", result.refusals[0].reason)

    def test_macro_arguments_and_arithmetic_call_arguments_are_eligible(self) -> None:
        for expression in ("assert_eq!(old, 1)", "consume(old + 1)"):
            with self.subTest(expression=expression):
                source = f"fn f() {{\n let old = 1;\n {expression};\n}}\n"
                result = self.rename(source, (self.finding(source),), ScriptedServer(
                    lambda text, offset, new: word_edits(text, "old", new)))
                self.assertEqual(len(result.kept), 1)

    def test_collision_and_absent_or_tied_locations_are_excluded(self) -> None:
        cases = (
            ("fn f() {\n let old = 1;\n let number = old + 1;\n}\n", 2, "already"),
            ("fn f() {}\n", 1, "exactly one"),
            ("fn f() {\n let old = 1;\n}\nfn g() {\n let old = 1;\n}\n", 3, "tied"),
        )
        for source, line, reason in cases:
            with self.subTest(reason=reason):
                finding = self.finding(source, line=line)
                if reason == "tied":
                    finding = replace(finding, rename=replace(finding.rename, line=3, text=" let old = 1;"))
                    # Equidistant declarations on lines 2 and 4.
                    source = "fn f() {\n let old = 1;\n}\n let old = 1;\n"
                server = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))
                result = self.rename(source, (finding,), server)
                self.assertEqual(server.calls, [])
                self.assertIn(reason, result.refusals[0].reason)

    def test_derive_fields_are_explicitly_refused(self) -> None:
        for derive in ("serde::Serialize", "clap::Parser", "confique::Config"):
            source = f"#[derive({derive})]\nstruct Options {{\n old: usize,\n}}\n"
            result = self.rename(source, (self.finding(source, line=3),), ScriptedServer(lambda text, offset, new: ()))
            self.assertEqual(result.kept, ())
            self.assertIn("derive field", result.refusals[0].reason)

    def test_whole_file_proof_is_required_even_after_each_step_passes(self) -> None:
        source = (self.repo / "src/lib.rs").read_text()
        real_check = binding_rename_check
        calls = 0
        def check(path: str, before: str, after: str, renames: tuple[BindingRename, ...]):
            nonlocal calls
            calls += 1
            proof = real_check(path, before, after, renames)
            return replace(proof, explained=False) if calls == 2 else proof
        server = ScriptedServer(lambda text, offset, new: word_edits(text, "old", new))
        with mock.patch.object(bulk, "binding_rename_check", side_effect=check):
            result = self.rename(source, (self.finding(source),), server)
        self.assertEqual(result.kept, ())
        self.assertEqual(result.edited, source)
        self.assertEqual(server.changes[-1], source)
        self.assertIn("whole-file", result.refusals[0].reason)


class ProtocolTests(CargoProjectTests):
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
                self.assertIn("let number", bulk.apply_edits(source, edits))
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
            self.assertEqual(bulk.apply_edits(text, bulk.workspace_edits(payload, path, text, 3)), "let number = 1;")
        with self.assertRaisesRegex(bulk.BulkRenameError, "another file"):
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


class LedgerTests(CargoProjectTests):
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
