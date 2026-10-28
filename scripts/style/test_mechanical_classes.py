"""Exercise the class list loader, the lint removal proof and the doc's class list."""

from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import final, override

import mechanical_classes
import style_footprint
from approved_renames import BindingRename

DOC = Path(__file__).resolve().parents[2] / "docs/style_production.md"
ENTRY = 'name = "{name}"\nproof = "{proof}"\nrules = [{rules}]\nsummary = "{summary}"\n'
HEAD = "mod other;\n\npub fn read() -> usize { 1 }\n\n"
MANIFEST = '[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n'
TESTS = "#[cfg(test)]\n#[allow(clippy::panic, clippy::unwrap_used)]\nmod tests {\n    fn example() {}\n}\n"


def entry(name: str = "pure-move", proof: str = "item_move", rules: str = '"module-structure"',
          summary: str = "A move.") -> str:
    return "[[class]]\n" + ENTRY.format(name=name, proof=proof, rules=rules, summary=summary)


@final
class LoaderTests(unittest.TestCase):
    path: Path = Path()
    catalog: frozenset[str] = frozenset({"module-structure", "test-module-allow-boilerplate"})

    @override
    def setUp(self) -> None:
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "classes.toml"

    def load(self, text: str) -> tuple[mechanical_classes.MechanicalClass, ...]:
        _ = self.path.write_text(text)
        return mechanical_classes.load(self.path, self.catalog)

    def refuses(self, text: str, reason: str) -> None:
        with self.assertRaises(mechanical_classes.ClassListError) as raised:
            _ = self.load(text)
        self.assertIn(reason, str(raised.exception))

    def test_a_valid_list_gives_each_class_in_order(self) -> None:
        lint = entry("stale-allow-lint", "lint_removal", '"test-module-allow-boilerplate"', "A removal.")
        self.assertEqual(self.load(entry() + lint), (
            mechanical_classes.MechanicalClass("pure-move", "item_move", ("module-structure",), "A move."),
            mechanical_classes.MechanicalClass("stale-allow-lint", "lint_removal",
                                               ("test-module-allow-boilerplate",), "A removal."),
        ))

    def test_an_unknown_proof_kind_is_refused(self) -> None:
        self.refuses(entry(proof="item_copy"), "class pure-move names the unknown proof item_copy")

    def test_a_rule_not_in_the_catalog_is_refused(self) -> None:
        self.refuses(entry(rules='"module-structure", "no-such-rule"'),
                     "class pure-move names a rule not in the catalog: no-such-rule")

    def test_a_malformed_entry_is_refused(self) -> None:
        for text, reason in (
            ("[[class]]\nname = \"pure-move\"\nproof = \"item_move\"\nrules = [\"module-structure\"]\n",
             "class 1 must have exactly the fields name, proof, rules and summary"),
            (entry() + 'tier = "D"\n', "class 1 must have exactly the fields"),
            (entry(summary=""), "class 1 needs a name, a proof and a summary as text"),
            (entry().replace('name = "pure-move"', "name = 3"), "class 1 needs a name, a proof and a summary as text"),
            (entry(rules=""), "class pure-move needs a list of rules"),
            (entry().replace('rules = ["module-structure"]', 'rules = "module-structure"'),
             "class pure-move needs a list of rules"),
            (entry(rules='"module-structure", 4'), "class pure-move has a rule that is not text"),
            (entry() + entry(), "class pure-move is listed twice"),
            ("class = 1\n", "the list must hold only [[class]] entries"),
            ("", "the list must hold only [[class]] entries"),
            (entry() + "[other]\n", "the list must hold only [[class]] entries"),
            ("class = [1]\n", "class 1 is not a table"),
            ("[[class]\n", "classes.toml: "),
        ):
            with self.subTest(text=text):
                self.refuses(text, reason)

    def test_the_shipped_list_loads_against_the_global_catalog(self) -> None:
        mechanical_classes.classes.cache_clear()
        self.addCleanup(mechanical_classes.classes.cache_clear)
        listed = mechanical_classes.classes()
        self.assertEqual([item.name for item in listed], ["pure-move", "stale-allow-lint", "binding-rename"])
        self.assertEqual(listed[1].rules, ("test-module-allow-boilerplate",))
        self.assertEqual(listed[2].rules, ("prefer-type-named-fields-and-bindings",))
        self.assertIs(mechanical_classes.classes(), listed)

    def test_the_doc_lists_each_class_of_the_toml_by_name(self) -> None:
        names = [item.name for item in mechanical_classes.classes()]
        text = DOC.read_text()
        rule = "A finding of a class in `scripts/style/mechanical_classes.toml` is applied when its proof passes."
        self.assertIn(rule, text)
        part = text[text.index(rule):].split("\n\n")
        listed = [match.group(1) for match in re.finditer(r"(?m)^- `([^`]+)` \(proof `", part[1])]
        self.assertEqual(listed, names)
        self.assertEqual(text.count(rule), 1)


@final
class MechanicalProofTests(unittest.TestCase):
    repo: Path = Path()
    base: str = ""

    @override
    def setUp(self) -> None:
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="mechanical-classes-")))
        _ = self.git("init", "-q", "--initial-branch=main")
        _ = self.git("config", "user.name", "Class tests")
        _ = self.git("config", "user.email", "classes@example.invalid")
        _ = self.git("config", "commit.gpgsign", "false")
        # A detached auto maintenance after a commit can still write objects while the folder is removed.
        _ = self.git("config", "maintenance.auto", "false")
        _ = self.git("config", "gc.auto", "0")
        self.write("Cargo.toml", MANIFEST)
        self.write("src/lib.rs", "mod other;\n\npub fn read() -> usize { 1 }\n\n" + TESTS)
        self.write("src/other.rs", "pub(crate) fn more() -> usize { 2 }\n")
        self.base = self.commit()

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *arguments],
                              check=True, capture_output=True, text=True).stdout.strip()

    def write(self, path: str, text: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        _ = target.write_text(text)

    def commit(self) -> str:
        _ = self.git("add", "--all")
        _ = self.git("commit", "--allow-empty", "-qm", "Fixture change")
        return self.git("rev-parse", "HEAD")

    def judged(self) -> tuple[frozenset[str], style_footprint.Mechanical | style_footprint.Review]:
        changes = style_footprint.changes_between(self.repo, self.base, self.commit())
        before = style_footprint.RevisionTree(self.repo, self.base)
        finding = style_footprint.Finding(style_footprint.WholeDiff(), changes)
        return style_footprint.lint_removal_files(changes, before), style_footprint.judge(finding, before)

    def test_binding_class_requires_approved_pairs_and_accounts_for_every_edit_in_each_file(self) -> None:
        source = "fn f() {\n    let old = 1;\n    consume(old);\n}\n"
        self.write("src/lib.rs", source)
        base = self.commit()
        tree = style_footprint.RevisionTree(self.repo, base)
        rename = BindingRename(2, source.splitlines()[1], "old", "fresh")
        renames = {"src/lib.rs": (rename,)}
        names = frozenset({"prefer-type-named-fields-and-bindings"})
        cases: tuple[tuple[str, frozenset[str]], ...] = (
            (source, frozenset()), (source.replace("old", "fresh"), frozenset({"src/lib.rs"})),
            (source.replace("let old", "let fresh"), frozenset()),
            (source.replace("old", "fresh").replace("1", "2"), frozenset()))
        for edited, expected in cases:
            self.write("src/lib.rs", edited)
            changes = style_footprint.changes_between(self.repo, base, self.commit())
            with self.subTest(edited=edited):
                self.assertEqual(mechanical_classes.explained_files(changes, tree, names, renames), expected)
                self.assertEqual(mechanical_classes.explained_files(changes, tree, names), frozenset())
                self.assertEqual(mechanical_classes.explained_files(changes, tree, frozenset({"module-structure"}), renames),
                                 frozenset())

    def test_binding_class_explains_only_paths_with_proven_pairs(self) -> None:
        source = "fn f() {\n    let old = 1;\n    consume(old);\n}\n"
        for path in ("src/lib.rs", "src/other.rs"):
            self.write(path, source)
        base = self.commit()
        for path in ("src/lib.rs", "src/other.rs"):
            self.write(path, source.replace("old", "fresh"))
        changes = style_footprint.changes_between(self.repo, base, self.commit())
        rename = BindingRename(2, source.splitlines()[1], "old", "fresh")
        self.assertEqual(mechanical_classes.PROOFS["binding_rename"](
            changes, style_footprint.RevisionTree(self.repo, base), {"src/lib.rs": (rename,)}), frozenset({"src/lib.rs"}))

    def test_a_stale_lint_name_removal_is_applied(self) -> None:
        removed = TESTS.replace("clippy::panic, ", "")
        self.write("src/lib.rs", "mod other;\n\npub fn read() -> usize { 1 }\n\n" + removed)
        self.assertEqual(self.judged(), (frozenset({"src/lib.rs"}), style_footprint.Mechanical()))

    def test_a_removal_that_also_adds_an_allow_or_changes_code_is_held(self) -> None:
        removed = "mod other;\n\npub fn read() -> usize { 1 }\n\n" + TESTS.replace("clippy::panic, ", "")
        for path, text in (("src/lib.rs", removed.replace("clippy::unwrap_used", "clippy::unwrap_used, dead_code")),
                           ("src/lib.rs", removed.replace("#[cfg(test)]", "#[allow(dead_code)]\n#[cfg(test)]")),
                           ("src/lib.rs", removed.replace("{ 1 }", "{ 3 }")),
                           ("src/other.rs", "pub(crate) fn more() -> usize { 4 }\n")):
            with self.subTest(path=path, text=text):
                _ = self.git("reset", "-q", "--hard", self.base)
                self.write("src/lib.rs", removed)
                self.write(path, text)
                files, verdict = self.judged()
                self.assertEqual(files, frozenset())
                self.assertIsInstance(verdict, style_footprint.Review)

    def test_a_lint_name_the_generator_does_not_report_stale_is_held(self) -> None:
        for before, after in (
            (TESTS.replace("fn example() {}", "fn example() { panic!(); }"), "clippy::panic, "),
            (TESTS.replace("#[cfg(test)]\n", ""), "clippy::panic, "),
            (TESTS.replace("allow(", "expect("), "clippy::panic, "),
            (TESTS.replace("mod tests", "mod checks"), "clippy::panic, "),
        ):
            with self.subTest(before=before):
                self.write("src/lib.rs", "mod other;\n\npub fn read() -> usize { 1 }\n\n" + before)
                self.base = self.commit()
                self.write("src/lib.rs", "mod other;\n\npub fn read() -> usize { 1 }\n\n" + before.replace(after, ""))
                files, verdict = self.judged()
                self.assertEqual(files, frozenset() if "mod checks" not in before else frozenset({"src/lib.rs"}))
                self.assertEqual(verdict, style_footprint.Mechanical())

    def test_an_attribute_edit_must_remove_a_lint_name_and_keep_the_rest(self) -> None:
        reason = TESTS.replace("clippy::unwrap_used)", 'clippy::unwrap_used, reason = "tests")')
        three = TESTS.replace("clippy::unwrap_used)", "clippy::unwrap_used, clippy::expect_used)")
        for before, after in (
            (reason, reason.replace(', reason = "tests"', "")),
            (reason, reason.replace("clippy::panic, ", "").replace('"tests"', '"checks"')),
            (reason, reason.replace("clippy::panic, ", "").replace(', reason = "tests"', "")),
            (TESTS, TESTS.replace("clippy::panic, clippy::unwrap_used", "clippy::unwrap_used, clippy::panic")),
            (three, three.replace("clippy::panic, clippy::unwrap_used, clippy::expect_used",
                                  "clippy::expect_used, clippy::unwrap_used")),
        ):
            with self.subTest(after=after):
                self.write("src/lib.rs", "mod other;\n\npub fn read() -> usize { 1 }\n\n" + before)
                self.base = self.commit()
                self.write("src/lib.rs", "mod other;\n\npub fn read() -> usize { 1 }\n\n" + after)
                self.assertEqual(self.judged()[0], frozenset())
        self.write("src/lib.rs", "mod other;\n\npub fn read() -> usize { 1 }\n\n" + reason)
        self.base = self.commit()
        self.write("src/lib.rs", HEAD + reason.replace("clippy::panic, ", ""))
        self.assertEqual(self.judged()[0], frozenset({"src/lib.rs"}))
        whole = HEAD + TESTS.replace("#[allow(clippy::panic, clippy::unwrap_used)]\n", "")
        self.write("src/lib.rs", "mod other;\n\npub fn read() -> usize { 1 }\n\n" + TESTS)
        self.base = self.commit()
        self.write("src/lib.rs", whole)
        self.assertEqual(self.judged()[0], frozenset({"src/lib.rs"}))

    def test_a_removal_beside_another_mechanical_edit_of_the_file_is_held(self) -> None:
        body = "fn example() {\n        let count = 1;\n        let _ = count;\n    }"
        tests = TESTS.replace("fn example() {}", body)
        self.write("src/lib.rs", "mod other;\n\npub fn read() -> usize { 1 }\n\n" + tests)
        self.base = self.commit()
        removed = "mod other;\n\npub fn read() -> usize { 1 }\n\n" + tests.replace("clippy::panic, ", "")
        self.write("src/lib.rs", removed.replace("count", "total"))
        self.assertEqual(self.judged(), (frozenset(), style_footprint.Mechanical()))

    def removal(self, files: dict[str, str], path: str, removed: str) -> frozenset[str]:
        """The files the proof explains when the text removed goes from path, with files in the before tree."""
        _ = self.git("rm", "-rqf", "--ignore-unmatch", "src", "tests")
        self.write("Cargo.toml", MANIFEST)
        for name, text in files.items():
            self.write(name, text)
        self.base = self.commit()
        self.write(path, files[path].replace(removed, "", 1))
        return self.judged()[0]

    def test_an_allow_whose_scope_reaches_another_file_is_held(self) -> None:
        allow = "#[allow(clippy::panic)]\n"
        for lib, other in (
            ("#[cfg(test)]\n" + allow + "mod tests;\n", ("src/tests.rs", "fn f() { panic!(); }\n")),
            ("#[cfg(test)]\n" + allow + "mod tests {\n    mod helpers;\n}\n",
             ("src/tests/helpers.rs", "fn f() { panic!(); }\n")),
            ("#[cfg(test)]\n" + allow + 'mod tests {\n    include!("helpers.rs");\n}\n',
             ("src/helpers.rs", "fn f() { panic!(); }\n")),
        ):
            with self.subTest(lib=lib):
                files = {"src/lib.rs": lib, other[0]: other[1]}
                self.assertEqual(self.removal(files, "src/lib.rs", allow), frozenset())
        inner = {"src/lib.rs": "#[cfg(test)]\nmod tests;\n", "src/tests.rs": "#![allow(clippy::panic)]\nfn f() {}\n"}
        self.assertEqual(self.removal(inner, "src/tests.rs", "#![allow(clippy::panic)]\n"), frozenset())

    def test_an_include_word_or_a_macro_item_in_the_file_holds_the_removal(self) -> None:
        allow = "    #[allow(clippy::unwrap_used)]\n"
        tests = "#[cfg(test)]\nmod tests {\n" + allow + "    fn f() {\n        {call}\n    }\n}\n"
        for head, call, explained in (
            ("use std::include as load;\n", 'load!("helpers.rs");', False),
            ("use std::include;\n", "", False),
            ("macro_rules! open {\n    ($x:expr) => { $x.unwrap() };\n}\n", "open!(Some(1));", False),
            ("", 'let _ = include_str!("helpers.rs");', True),
        ):
            with self.subTest(head=head, call=call):
                files = {"src/lib.rs": head + tests.replace("{call}", call),
                         "src/helpers.rs": "fn g() { Some(1).unwrap(); }\n"}
                self.assertEqual(self.removal(files, "src/lib.rs", allow),
                                 frozenset({"src/lib.rs"}) if explained else frozenset())

    def test_an_allow_on_an_item_with_unwrap_in_its_signature_is_held(self) -> None:
        allow = "#[allow(clippy::unwrap_used)]\n"
        for item in (
            "fn f() -> [u8; {\n    fn length(value: Option<usize>) -> usize {\n        value.unwrap()\n    }\n"
            + "    1\n}] {\n    [0]\n}\n",
            "#[cfg(test)] " + allow.strip() + " fn f() -> [u8; { fn length(value: Option<usize>) -> usize "
            + "{ value.unwrap() } 1 }] { [0] }\n",
            "struct S<const N: usize = { Some(3).unwrap() }> {\n    a: [u8; N],\n}\n",
        ):
            lib = item if item.startswith("#") else "#[cfg(test)]\n" + allow + item
            for code, explained in ((lib, False), (lib.replace(".unwrap()", ""), True)):
                with self.subTest(code=code):
                    self.assertEqual(self.removal({"src/lib.rs": code}, "src/lib.rs", allow.strip()),
                                     frozenset({"src/lib.rs"}) if explained else frozenset())

    def test_an_include_word_or_a_macro_item_in_another_file_of_the_crate_holds_the_removal(self) -> None:
        allow = "#[allow(clippy::unwrap_used)]\n"
        gated = "#[cfg(test)]\nmod tests;\n"
        opener = "macro_rules! open {\n    ($x:expr) => { $x.unwrap() };\n}\n"
        for lib, other, explained in (
            (opener + gated, "", False),
            (gated + "mod other;\n", "use std::include as load;\n", False),
            (gated + "mod other;\n", "// macro_rules! and include! in a comment\n", True),
        ):
            with self.subTest(lib=lib, other=other):
                files = {"src/lib.rs": lib, "src/tests.rs": allow + "fn f() {\n    open!(Some(1));\n}\n"}
                if other:
                    files["src/other.rs"] = other
                self.assertEqual(self.removal(files, "src/tests.rs", allow),
                                 frozenset({"src/tests.rs"}) if explained else frozenset())

    def test_only_a_package_tests_file_or_a_cfg_test_module_file_is_a_test_file(self) -> None:
        allow = "#[allow(clippy::panic)]\n"
        lib = "pub fn read() -> usize { 1 }\n"
        gated = "#[cfg(test)]\nmod tests;\n"
        for files, path, explained in (
            ({"src/lib.rs": lib, "tests/check.rs": allow + "fn f() {}\n"}, "tests/check.rs", True),
            ({"src/lib.rs": gated, "src/tests.rs": allow + "fn f() {}\n"}, "src/tests.rs", True),
            ({"src/lib.rs": gated, "src/tests.rs": "mod helpers;\n", "src/tests/helpers.rs": allow + "fn f() {}\n"},
             "src/tests/helpers.rs", True),
            ({"src/lib.rs": "#[cfg(test)]\nmod tests {\n    mod helpers;\n}\n",
              "src/tests/helpers.rs": allow + "fn f() {}\n"}, "src/tests/helpers.rs", True),
            ({"src/lib.rs": lib, "tests/helper/Cargo.toml": '[package]\nname = "helper"\nversion = "0.1.0"\n',
              "tests/helper/src/lib.rs": allow + "pub fn f() {}\n"}, "tests/helper/src/lib.rs", False),
            ({"src/lib.rs": "mod checks;\n", "src/checks.rs": "mod tests;\n",
              "src/checks/tests.rs": allow + "fn f() {}\n"}, "src/checks/tests.rs", False),
            ({"src/lib.rs": gated + '#[path = "tests.rs"]\nmod again;\n', "src/tests.rs": allow + "fn f() {}\n"},
             "src/tests.rs", False),
            ({"src/lib.rs": "#[cfg_attr(any(), cfg(test))]\nmod tests;\n", "src/tests.rs": allow + "fn f() {}\n"},
             "src/tests.rs", False),
            ({"src/lib.rs": gated + 'include!("other.rs");\n', "src/tests.rs": allow + "fn f() {}\n"},
             "src/tests.rs", False),
            ({"src/lib.rs": gated + 'use std::include as load;\nload!("other.rs");\n',
              "src/tests.rs": allow + "fn f() {}\n"}, "src/tests.rs", False),
            ({"src/lib.rs": gated + "use std::include;\n", "src/tests.rs": allow + "fn f() {}\n"},
             "src/tests.rs", False),
            ({"src/lib.rs": gated + "macro_rules! declare {\n    ($name:ident) => { mod $name; };\n}\ndeclare!(x);\n",
              "src/tests.rs": allow + "fn f() {}\n"}, "src/tests.rs", False),
            ({"src/lib.rs": gated + "macro declare($name:ident) { mod $name; }\n",
              "src/tests.rs": allow + "fn f() {}\n"}, "src/tests.rs", False),
            ({"Cargo.toml": MANIFEST + 'build = "src/tests.rs"\n', "src/lib.rs": gated,
              "src/tests.rs": allow + "fn f() {}\n"}, "src/tests.rs", False),
            ({"Cargo.toml": MANIFEST + '\n[[bin]]\nname = "tool"\npath = "src/tests.rs"\n', "src/lib.rs": gated,
              "src/tests.rs": allow + "fn f() {}\n"}, "src/tests.rs", False),
            ({"src/lib.rs": gated + allow + "fn f() {}\n", "src/tests.rs": "fn g() {}\n"}, "src/lib.rs", False),
            ({"src/lib.rs": lib, "src/tests.rs": allow + "fn f() {}\n"}, "src/tests.rs", False),
        ):
            with self.subTest(files=files):
                self.assertEqual(self.removal(files, path, allow), frozenset({path}) if explained else frozenset())

    def test_a_cfg_test_scope_is_only_the_exact_attribute(self) -> None:
        allow = "#[allow(clippy::panic)]\n"
        for attribute in ("#[cfg_attr(any(), cfg(test))]", "#[cfg(not(test))]", "#[cfg(any(test, unix))]",
                          "#[cfg(all(test, unix))]", "#[cfg_attr(any(), doc(#[cfg(test)]))]", "#[cfg(test, unix)]"):
            with self.subTest(attribute=attribute):
                lib = attribute + "\n" + allow + "mod checks {\n    fn f() {}\n}\n"
                self.assertEqual(self.removal({"src/lib.rs": lib}, "src/lib.rs", allow), frozenset())
        lib = "#[r#cfg(r#test)]\n" + allow + "mod checks {\n    fn f() {}\n}\n"
        self.assertEqual(self.removal({"src/lib.rs": lib}, "src/lib.rs", allow), frozenset({"src/lib.rs"}))

    def test_an_allow_inside_a_cfg_test_module_body_is_applied(self) -> None:
        allow = "    #[allow(clippy::panic)]\n"
        for lib in ("#[cfg(test)]\nmod tests {\n" + allow + "    fn f() {}\n}\n",
                    "#[cfg(test)]\n#[doc(hidden)]\npub(crate) mod tests {\n    mod inner {\n" + allow
                    + "    fn f() {}\n    }\n}\n"):
            with self.subTest(lib=lib):
                self.assertEqual(self.removal({"src/lib.rs": lib}, "src/lib.rs", allow), frozenset({"src/lib.rs"}))

    def test_an_allow_in_a_macro_token_tree_is_held(self) -> None:
        allow = "    #[allow(clippy::panic)]\n"
        for files, path in (
            ({"src/lib.rs": "make! {\n    #[cfg(test)]\n    mod tests {\n" + allow + "    fn f() {}\n    }\n}\n"},
             "src/lib.rs"),
            ({"src/lib.rs": "pub fn read() {}\n", "tests/check.rs": "make! {\n" + allow + "    fn f() {}\n}\n"},
             "tests/check.rs"),
            ({"src/lib.rs": "pub fn read() {}\n",
              "tests/check.rs": "macro_rules! make {\n    () => {\n" + allow + "    fn f() {}\n    };\n}\n"},
             "tests/check.rs"),
        ):
            with self.subTest(files=files):
                self.assertEqual(self.removal(files, path, allow), frozenset())


if __name__ == "__main__":
    _ = unittest.main()
