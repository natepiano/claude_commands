"""Compare maintained coverage with the original judgment calculation."""

from __future__ import annotations

import sqlite3
import unittest
from collections.abc import Callable, Iterator
from contextlib import closing
from typing import cast, final
from unittest import mock

from scripts.style import rules, style_ledger
from scripts.style.ledger_test_support import LedgerTestCase


@final
class JudgmentCellsTests(LedgerTestCase):
    def coverage(self, ledger: style_ledger.Ledger) -> tuple[tuple[style_ledger.Cell, str], ...]:
        read = cast(Callable[[], Iterator[tuple[style_ledger.Cell, str]]], getattr(ledger, "_coverage"))
        return tuple(read())

    def assert_full_recomputation(self) -> None:
        with self.ledger() as maintained:
            actual = self.coverage(maintained)
        with self.ledger() as full, mock.patch.object(full, "_maintain_judgments", False):
            self.assertEqual(actual, self.coverage(full))
        with self.ledger() as reopened:
            self.assertEqual(actual, self.coverage(reopened))

    def test_cell_states_match_after_source_and_membership_changes(self) -> None:
        read = self.rule("read")
        modules = self.rule("modules", "tier: C\nfamily: modules\ninput_scope: crate\n")
        _ = self.rule("structure", "tier: D\nfamily: modules\ninput_scope: file\n")
        _ = self.rule("manifest", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        _ = self.rule("partial", "tier: B\nfamily: names\ninput_scope: file\ncandidates:\n"
                      + "  kind: regex\n  pattern: value\n  surface: partial\n  uncovered: other sites\n")
        _ = self.rule("lint", "tier: A\nmechanism: clippy\n")
        self.set_tip(self.initial_tip)
        self.assert_full_recomputation()
        for rule in (read, modules):
            with self.subTest(change="judgment", rule=rule):
                self.record(rule, self.sources[0], self.initial_tip)
                self.assert_full_recomputation()
        original = (self.rule_dir / "read.md").read_text()
        for label, target, text in (
            ("path add", "crates/alpha/src/added.rs", "pub fn value() {}\n"),
            ("generator site", self.sources[1], "pub fn replacement() {}\n"),
            ("ignore", "rustfmt.toml", 'ignore = ["crates/beta"]\n'),
            ("manifest", "crates/alpha/Cargo.toml",
             (self.repo / "crates/alpha/Cargo.toml").read_text() + "\n[features]\nextra = []\n"),
            ("comment", self.sources[0], "// A comment.\npub fn value() -> u32 { 1 }\n"),
        ):
            with self.subTest(change=label):
                self.write(target, text)
                self.set_tip(self.commit(label))
                self.assert_full_recomputation()
        with self.subTest(change="path remove"):
            (self.repo / "crates/alpha/src/added.rs").unlink()
            self.set_tip(self.commit("Remove a path"))
            self.assert_full_recomputation()
        for label, text in (("rule change", original + "\nChanged instructions.\n"), ("rule revert", original)):
            with self.subTest(change=label):
                _ = (self.rule_dir / "read.md").write_text(text)
                with self.ledger() as ledger:
                    _ = ledger.withdraw_superseded({item.rule_id: item for item in rules.load_rules(self.repo)})
                self.assert_full_recomputation()
                self.assertEqual(self.states(read)[self.sources[0]], "stale")
        with self.subTest(change="lint judgment"):
            self.lint(self.git("rev-parse", "HEAD"), "clippy", True, {})
            self.assert_full_recomputation()

    def test_unchanged_reopen_reads_no_historical_judgment_source(self) -> None:
        rule = self.rule("modules", "tier: C\nfamily: modules\ninput_scope: crate\n")
        self.write("rustfmt.toml", "ignore = []\n")
        base = self.commit("Add historical configuration")
        self.set_tip(base)
        self.record(rule, self.sources[0], base)
        self.write(self.sources[0], "// A comment.\npub fn value() -> u32 { 1 }\n")
        self.write("Cargo.toml", (self.repo / "Cargo.toml").read_text() + "\n# The members stay unchanged.\n")
        self.write("rustfmt.toml", 'ignore = ["absent"]\n')
        tip = self.commit("Change a comment")
        self.set_tip(tip)
        with self.ledger() as cold:
            expected = self.coverage(cold)
        builder = cast(Callable[[object, str], style_ledger.RepositoryTip], vars(style_ledger)["_tip"])
        code = cast(Callable[[bytes], object], vars(style_ledger)["_rust_code"])
        with self.ledger() as warm, mock.patch.object(style_ledger, "_tip", wraps=builder) as tips:
            judgments = cast(Callable[..., object], getattr(warm, "_judgments"))
            with mock.patch.object(warm, "_judgments", wraps=judgments) as histories:
                with mock.patch.object(style_ledger, "_rust_code", wraps=code) as sources:
                    self.assertEqual(self.coverage(warm), expected)
                    histories.assert_not_called()
                    sources.assert_not_called()
            self.assertTrue(tips.call_args_list)
            self.assertEqual({call.args[1] for call in tips.call_args_list}, {tip})
        for kind, path in (("manifest", "Cargo.toml"), ("configuration", "rustfmt.toml"),
                           ("source", self.sources[0]), ("tree", "crates/alpha")):
            with self.subTest(missing=kind):
                object_id = self.git("rev-parse", f"{base}:{path}")
                object_path = self.repo / ".git/objects" / object_id[:2] / object_id[2:]
                hidden = object_path.with_name(object_path.name + ".hidden")
                _ = object_path.rename(hidden)
                try:
                    for maintained in (False, True):
                        with self.subTest(maintained=maintained), self.ledger() as ledger:
                            with mock.patch.object(ledger, "_maintain_judgments", maintained):
                                with self.assertRaises(style_ledger.GitReadFailed):
                                    _ = self.coverage(ledger)
                finally:
                    _ = hidden.rename(object_path)

    def test_row_versions_preserve_updates_deletions_and_key_changes(self) -> None:
        rule = self.rule("read")
        self.set_tip(self.initial_tip)
        self.record(rule, self.sources[0], self.initial_tip)
        self.assert_full_recomputation()
        with self.ledger() as ledger, closing(sqlite3.connect(ledger.database)) as connection:
            row = cast(tuple[int], connection.execute("SELECT id FROM judgments LIMIT 1").fetchone())
            for change, statement, arguments in (
                ("correction", "UPDATE judgments SET verdict='exception' WHERE id=?", (row[0],)),
                ("cell move", "UPDATE judgments SET path=? WHERE id=?", (self.sources[1], row[0])),
                ("key move", "UPDATE judgments SET id=id+100 WHERE id=?", (row[0],)),
                ("delete", "DELETE FROM judgments WHERE id=?", (row[0] + 100,)),
            ):
                with self.subTest(change=change):
                    _ = connection.execute(statement, arguments)
                    connection.commit()
                    self.assert_full_recomputation()
            versions = cast(list[tuple[int, int]], connection.execute(
                "SELECT version,present FROM refill_rows WHERE source_table='judgments' ORDER BY version").fetchall())
            self.assertEqual(len(versions), 2)
            self.assertTrue(all(not present for _, present in versions))
            self.assertTrue(all(version > 0 for version, _ in versions))
        for name in ("café.rs", '引数"\\.rs'):
            with self.subTest(path=name):
                path = "crates/alpha/src/" + name
                self.write(path, "pub fn value() -> u32 { 1 }\n")
                tip = self.commit("Add a Unicode path")
                self.set_tip(tip)
                self.assertEqual(self.states(rule)[path], "open")
                self.record(rule, path, tip)
                self.assertEqual(self.states(rule)[path], "current")
                self.assert_full_recomputation()


if __name__ == "__main__":
    _ = unittest.main()
