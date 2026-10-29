"""Per-file constant extraction on temporary files; no repository or ledger writes."""
from __future__ import annotations

import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import override
from unittest import mock

import bulk_constants as bulk
import style_footprint as rust


@dataclass(frozen=True)
class Finding:
    finding_id: str
    path: str
    line: int
    text: str
    after: str


class ConstantTests(unittest.TestCase):
    path: Path = Path()
    @override
    def setUp(self) -> None:
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'lib.rs'

    def finding(self, source: str, *, line: int = 2, name: str = 'LIMIT', value: str = '42',
                place: str = 'at module level in this file', identity: str = 'finding') -> Finding:
        return Finding(identity, 'src/lib.rs', line, source.splitlines()[line - 1],
                       f'Add `const {name}: i32 = {value};` {place}; replace `{value}` with `{name}`.')

    def apply(self, source: str, *findings: Finding) -> bulk.FileConstants:
        _ = self.path.write_text(source)
        result = bulk.apply_constants(self.path, 'src/lib.rs', source, findings)
        self.assertEqual(self.path.read_text(), source, 'the applier must never write files')
        return result

    def refused(self, source: str, finding: Finding, reason: str) -> None:
        result = self.apply(source, finding)
        self.assertEqual(result.edited, source)
        self.assertEqual(result.kept, ())
        self.assertEqual(result.refusals, (bulk.ConstantRefusal(finding.finding_id, reason),))

    def test_applies_module_constant_and_replaces_only_the_judged_literal(self) -> None:
        source = 'fn run() -> i32 {\n    42\n}\nfn other() -> i32 { 42 }\n'
        finding = self.finding(source)
        result = self.apply(source, finding)
        self.assertEqual(result.refusals, ())
        self.assertEqual(len(result.kept), 1)
        self.assertEqual(result.path, 'src/lib.rs')
        self.assertEqual(result.original, source)
        self.assertEqual(result.edited, 'fn run() -> i32 {\n    LIMIT\n}\nfn other() -> i32 { 42 }\n\nconst LIMIT: i32 = 42;\n')
        self.assertIsInstance(bulk.constant_check('src/lib.rs', source, result.edited, (finding,)), bulk.ConstantsProved)

    def test_reuses_equal_existing_constant(self) -> None:
        source = 'const LIMIT: i32 = 42;\nfn run() -> i32 {\n    42\n}\n'
        result = self.apply(source, self.finding(source, line=3))
        self.assertEqual(result.refusals, ())
        self.assertEqual(result.edited, source.replace('    42', '    LIMIT'))
        self.assertEqual(result.edited.count('const LIMIT:'), 1)

    def test_refuses_taken_name_with_different_value_or_type(self) -> None:
        for declaration in ('const LIMIT: i32 = 43;', 'const LIMIT: u8 = 42;', 'use other::LIMIT;',
                            'const r#LIMIT: i32 = 43;'):
            with self.subTest(declaration=declaration):
                source = declaration + '\nfn run() -> i32 {\n    42\n}\n'
                self.refused(source, self.finding(source, line=3), 'constant name taken')

    def test_refuses_changed_judged_line_including_indentation(self) -> None:
        source = 'fn run() -> i32 {\n    42\n}\n'
        finding = self.finding(source)
        for changed in (source.replace('42', '43'), source.replace('    42', '  42'), source.replace('42', '42 // changed')):
            with self.subTest(changed=changed):
                self.refused(changed, finding, 'changed since judged')

    def test_refuses_missing_exact_name_type_value_or_place_and_extra_instructions(self) -> None:
        source = 'fn run() -> i32 {\n    42\n}\n'
        for after in ('Name this literal.', 'Add LIMIT at module level in this file.',
                      'Add `const LIMIT = 42;` at module level in this file.',
                      'Add `const LIMIT: i32 = 42;`.',
                      'Add `const LIMIT: i32 = 42;` in constants.rs.',
                      'Add `const LIMIT: i32 = 21 + 21;` at module level in this file.',
                      'Add `const LIMIT: i32 = 42;` at module level in this file; also change sorting.'):
            with self.subTest(after=after):
                self.refused(source, replace(self.finding(source), after=after), 'no exact constant')

    def test_refuses_literal_patterns(self) -> None:
        for line in ('    let 42 = value else { return; };', '    match value { 42 => (), _ => () }',
                     '    if let 42 = value {}', '    while let 42 = value {}'):
            with self.subTest(line=line):
                source = 'fn run() {\n' + line + '\n}\n'
                self.refused(source, self.finding(source), 'literal is not an ordinary expression')

    def test_refuses_macro_arguments(self) -> None:
        source = 'fn run() {\n    println!("{}", 42);\n}\n'
        self.refused(source, self.finding(source), 'literal is not an ordinary expression')

    def test_refuses_const_generics_attributes_and_string_contents(self) -> None:
        for line in ('    let result = make::<42>();', '    #[size = 42] let value = 1;',
                     '    let value = [0; 42];'):
            with self.subTest(line=line):
                source = 'fn run() {\n' + line + '\n}\n'
                self.refused(source, self.finding(source), 'literal is not an ordinary expression')
        source = 'fn run() {\n    let value = "42";\n}\n'
        self.refused(source, self.finding(source), 'literal is not uniquely located')

    def test_two_findings_share_one_new_constant(self) -> None:
        source = 'fn run() {\n    let first = 42;\n    let second = 42;\n}\n'
        result = self.apply(source, self.finding(source), self.finding(source, line=3, identity='second'))
        self.assertEqual(result.refusals, ())
        self.assertEqual(len(result.kept), 2)
        self.assertEqual(result.edited.count('const LIMIT: i32 = 42;'), 1)
        self.assertIn('let first = LIMIT;', result.edited)
        self.assertIn('let second = LIMIT;', result.edited)

    def test_places_constant_inside_the_named_function(self) -> None:
        source = 'fn run() -> i32 {\n    42\n}\n'
        finding = self.finding(source, place='in fn `run`')
        result = self.apply(source, finding)
        self.assertEqual(result.refusals, ())
        self.assertEqual(result.edited, 'fn run() -> i32 {\nconst LIMIT: i32 = 42;\n\n    LIMIT\n}\n')
        self.refused(source, self.finding(source, place='in fn `other`'), 'no exact constant')

    def test_reuses_an_equal_function_local_constant(self) -> None:
        source = 'fn run() -> i32 {\n    const LIMIT: i32 = 42;\n    42\n}\n'
        result = self.apply(source, self.finding(source, line=3, place='in fn `run`'))
        self.assertEqual(result.refusals, ())
        self.assertEqual(result.edited.count('const LIMIT:'), 1)
        self.assertIn('\n    LIMIT\n', result.edited)

    def test_collision_uses_whole_identifiers_and_refuses_shadowed_names(self) -> None:
        source = 'const LIMIT_COUNT: i32 = 1;\nfn run() -> i32 {\n    42\n}\n'
        result = self.apply(source, self.finding(source, line=3))
        self.assertEqual(result.refusals, ())
        self.assertIn('const LIMIT_COUNT: i32 = 1;', result.edited)
        for declaration in ('let LIMIT = 1;', 'const LIMIT: i32 = 42;'):
            source = 'fn other() { ' + declaration + ' }\nfn run() -> i32 {\n    42\n}\n'
            self.refused(source, self.finding(source, line=3), 'constant name taken')

    def test_refuses_ambiguous_and_duplicate_sites(self) -> None:
        source = 'fn run() {\n    let value = (42, 42);\n}\n'
        self.refused(source, self.finding(source), 'literal is not uniquely located')
        source = 'fn run() -> i32 {\n    42\n}\n'
        first = self.finding(source)
        result = self.apply(source, first, replace(first, finding_id='duplicate'))
        self.assertEqual(len(result.kept), 1)
        self.assertEqual(result.refusals, (bulk.ConstantRefusal('duplicate', 'literal is not uniquely located'),))

    def test_tuple_field_indices_are_not_literal_expressions(self) -> None:
        source = 'fn run() {\n    let value = tuple.42;\n}\n'
        self.refused(source, self.finding(source), 'literal is not an ordinary expression')

    def test_whole_file_proof_refuses_extra_token_changes(self) -> None:
        source = 'fn run() -> i32 {\n    42\n}\n'
        finding = self.finding(source)
        result = self.apply(source, finding)
        for edited in (result.edited.replace('fn run', 'fn changed'),
                       result.edited.replace('i32 = 42', 'i32 = 43'),
                       result.edited + 'fn surprise() {}\n',
                       result.edited + '// surprise comment\n',
                       result.edited.replace('    LIMIT', '    42')):
            with self.subTest(edited=edited):
                proof = bulk.constant_check('src/lib.rs', source, edited, (finding,))
                self.assertIsInstance(proof, bulk.ConstantsNotProved)

    def test_failed_file_proof_refuses_every_finding_and_discards_all_edits(self) -> None:
        source = 'fn run() {\n    let first = 42;\n    let second = 42;\n}\n'
        original_render = bulk.render_constants
        def extra(text: str, plan: bulk.ConstantPlan) -> str:
            return original_render(text, plan) + 'fn extra() {}'

        with mock.patch.object(bulk, 'render_constants', side_effect=extra):
            result = self.apply(source, self.finding(source), self.finding(source, line=3, identity='second'))
        self.assertEqual(result.edited, source)
        self.assertEqual(result.kept, ())
        self.assertEqual(len(result.refusals), 2)
        self.assertTrue(all(refusal.reason == 'file proof failed: unexplained token change' for refusal in result.refusals))

    def test_whole_string_literal_can_be_lifted_without_editing_its_contents(self) -> None:
        source = 'fn run() -> &\'static str {\n    "hello"\n}\n'
        finding = replace(self.finding(source), after='Add `const GREETING: &str = "hello";` at module level in this file.')
        result = self.apply(source, finding)
        self.assertEqual(result.refusals, ())
        self.assertIn('\n    GREETING\n', result.edited)
        self.assertIn('const GREETING: &str = "hello";', result.edited)

    def test_refuses_globs_and_macro_generated_names_even_outside_the_site(self) -> None:
        for prefix in ('use other::*;\n', 'macro_rules! make { () => { const LIMIT: i32 = 1; }; }\n',
                       '#[r#macro_use] mod provider;\n'):
            with self.subTest(prefix=prefix):
                source = prefix + 'fn run() -> i32 {\n    42\n}\n'
                self.refused(source, self.finding(source, line=3), 'scope may introduce names')

    def test_refuses_two_planned_meanings_for_the_same_name(self) -> None:
        source = 'fn run() {\n    let first = 42;\n    let second = 43;\n}\n'
        result = self.apply(source, self.finding(source), self.finding(source, line=3, value='43', identity='second'))
        self.assertEqual(len(result.kept), 1)
        self.assertEqual(result.refusals, (bulk.ConstantRefusal('second', 'constant name taken'),))


class CrateConstantTests(unittest.TestCase):
    def snapshot(self, **files: str) -> bulk.CrateSources:
        return bulk.CrateSources('src/lib.rs', files)

    def finding(self, source: str, path: str = 'src/worker.rs', *, name: str = 'LIMIT',
                value: str = '42', identity: str = 'first', destination: str = 'constants.rs') -> Finding:
        line = next(i for i, text in enumerate(source.splitlines(), 1) if text.strip() == value)
        return Finding(identity, path, line, source.splitlines()[line - 1],
                       f'Add `const {name}: i32 = {value};` in {destination}; import it at the top.')

    def apply(self, snapshot: bulk.CrateSources, *findings: Finding) -> bulk.CrateConstants:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path, text in snapshot.files.items():
                destination = root / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                _ = destination.write_text(text)
            result = bulk.apply_crate_constants(snapshot, findings)
            self.assertEqual({str(path.relative_to(root)): path.read_text() for path in root.rglob('*.rs')}, dict(snapshot.files))
            return result

    def test_creates_crate_constants_module_and_top_import_preserving_docs(self) -> None:
        source = '/// This documents run.\nfn run() -> i32 {\n    42\n}\n'
        snapshot = bulk.CrateSources('src/lib.rs', {'src/lib.rs': '//! Crate docs.\nmod worker;\n', 'src/worker.rs': source})
        finding = self.finding(source)
        result = self.apply(snapshot, finding)
        self.assertEqual(result.refusals, ())
        self.assertEqual(len(result.kept), 1)
        edits = {edit.path: edit for edit in result.edits}
        self.assertEqual(set(edits), {'src/lib.rs', 'src/worker.rs', 'src/constants.rs'})
        self.assertEqual(edits['src/lib.rs'].edited, '//! Crate docs.\nmod constants;\n\nmod worker;\n')
        self.assertEqual(edits['src/worker.rs'].edited, 'use crate::constants::LIMIT;\n\n/// This documents run.\nfn run() -> i32 {\n    LIMIT\n}\n')
        self.assertEqual(edits['src/constants.rs'].edited, '// extracted values\npub(super) const LIMIT: i32 = 42;\n\n')
        self.assertTrue(edits['src/constants.rs'].created)
        self.assertFalse(edits['src/lib.rs'].created)
        self.assertIsInstance(bulk.crate_constant_check(snapshot, result.edits, (finding,)), bulk.ConstantsProved)

    def test_reuses_equal_crate_constant_without_modifying_its_file(self) -> None:
        source = 'fn run() -> i32 {\n    42\n}\n'
        snapshot = bulk.CrateSources('src/lib.rs', {'src/lib.rs': 'mod constants;\nmod worker;\n',
             'src/constants.rs': '// limits\npub(super) const LIMIT: i32 = 42;\n', 'src/worker.rs': source})
        result = self.apply(snapshot, self.finding(source))
        self.assertEqual(result.refusals, ())
        self.assertEqual([edit.path for edit in result.edits], ['src/worker.rs'])
        self.assertIn('use crate::constants::LIMIT;', result.edits[0].edited)

    def test_crate_collision_checks_type_value_visibility_and_whole_names(self) -> None:
        source = 'fn run() -> i32 {\n    42\n}\n'
        for declaration in ('pub(super) const LIMIT: i32 = 43;', 'pub(super) const LIMIT: u8 = 42;',
                            'const LIMIT: i32 = 42;', 'pub(super) const r#LIMIT: i32 = 43;'):
            with self.subTest(declaration=declaration):
                snapshot = bulk.CrateSources('src/lib.rs', {'src/lib.rs': 'mod constants;\nmod worker;\n',
                    'src/constants.rs': '// limits\n' + declaration + '\n', 'src/worker.rs': source})
                result = self.apply(snapshot, self.finding(source))
                self.assertEqual(result.edits, ())
                self.assertEqual(result.refusals, (bulk.ConstantRefusal('first', 'constant name taken'),))
        source = 'const LIMIT_COUNT: i32 = 9;\nfn run() -> i32 {\n    42\n}\n'
        snapshot = bulk.CrateSources('src/lib.rs', {'src/lib.rs': 'mod worker;\n', 'src/worker.rs': source})
        self.assertEqual(len(self.apply(snapshot, self.finding(source)).kept), 1)
        for prefix in ('const LIMIT: i32 = 9;', 'const r#LIMIT: i32 = 9;', 'use other::LIMIT;'):
            changed = prefix + '\n' + source
            result = self.apply(replace(snapshot, files={**snapshot.files, 'src/worker.rs': changed}), self.finding(changed))
            self.assertEqual(result.refusals, (bulk.ConstantRefusal('first', 'constant name taken'),))

    def test_crate_mode_keeps_judged_line_byte_identity(self) -> None:
        source = 'fn run() -> i32 {\n    42\n}\n'
        finding = self.finding(source)
        for changed in (source.replace('    42', '  42'), source.replace('42', '43'), source.replace('42', '42 // changed')):
            snapshot = bulk.CrateSources('src/lib.rs', {'src/lib.rs': 'mod worker;\n', 'src/worker.rs': changed})
            result = self.apply(snapshot, finding)
            self.assertEqual(result.edits, ())
            self.assertEqual(result.refusals, (bulk.ConstantRefusal('first', 'changed since judged'),))

    def test_shared_crate_constant_and_import_added_once(self) -> None:
        source = 'fn run() {\n    42\n}\nfn other() {\n    42\n}\n'
        first = self.finding(source)
        second = replace(first, finding_id='second', line=5)
        snapshot = bulk.CrateSources('src/lib.rs', {'src/lib.rs': 'mod worker;\nmod other;\n',
            'src/worker.rs': source, 'src/other.rs': source})
        result = self.apply(snapshot, first, second, replace(first, finding_id='third', path='src/other.rs'))
        self.assertEqual(result.refusals, ())
        self.assertEqual(len(result.kept), 3)
        edits = {edit.path: edit.edited for edit in result.edits}
        self.assertEqual(edits['src/constants.rs'].count('const LIMIT:'), 1)
        self.assertEqual(edits['src/worker.rs'].count('use crate::constants::LIMIT;'), 1)
        self.assertEqual(edits['src/lib.rs'].count('mod constants;'), 1)

    def test_adds_constants_in_alphabetical_sections_and_name_order(self) -> None:
        source = 'fn run() -> i32 {\n    42\n}\n'
        for original, expected in (
            ('// apples\npub(super) const APPLE: i32 = 1;\n\n// zebras\npub(super) const ZEBRA: i32 = 1;\n',
             '// apples\npub(super) const APPLE: i32 = 1;\n\n// extracted values\npub(super) const LIMIT: i32 = 42;\n\n// zebras\npub(super) const ZEBRA: i32 = 1;\n'),
            ('// extracted values\npub(super) const ALPHA: i32 = 1;\npub(super) const ZEBRA: i32 = 1;\n',
             '// extracted values\npub(super) const ALPHA: i32 = 1;\npub(super) const LIMIT: i32 = 42;\npub(super) const ZEBRA: i32 = 1;\n'),
            ('// extracted values\n', '// extracted values\npub(super) const LIMIT: i32 = 42;\n')):
            with self.subTest(original=original):
                snapshot = bulk.CrateSources('src/lib.rs', {'src/lib.rs': 'mod constants;\nmod worker;\n',
                    'src/constants.rs': original, 'src/worker.rs': source})
                result = self.apply(snapshot, self.finding(source))
                self.assertEqual(result.refusals, ())
                self.assertEqual(len(result.kept), 1)
                self.assertEqual(next(edit.edited for edit in result.edits if edit.path == 'src/constants.rs'), expected)

    def test_creates_nested_constants_module_without_promoting_a_flat_file(self) -> None:
        source = 'fn run() -> i32 {\n    42\n}\n'
        snapshot = bulk.CrateSources('src/lib.rs', {'src/lib.rs': 'mod feature;\n',
            'src/feature/mod.rs': 'mod worker;\n', 'src/feature/worker.rs': source})
        result = self.apply(snapshot, self.finding(source, 'src/feature/worker.rs', destination='feature/constants.rs'))
        self.assertEqual(result.refusals, ())
        edits = {edit.path: edit for edit in result.edits}
        self.assertNotIn('src/lib.rs', edits)
        self.assertIn('src/feature/constants.rs', edits)
        self.assertIn('mod constants;', edits['src/feature/mod.rs'].edited)
        self.assertIn('use crate::feature::constants::LIMIT;', edits['src/feature/worker.rs'].edited)
        self.assertEqual(set(snapshot.files) | set(edits), {'src/lib.rs', 'src/feature/mod.rs', 'src/feature/worker.rs', 'src/feature/constants.rs'})

    def test_named_crate_string_request_has_unambiguous_type(self) -> None:
        source = 'fn run() -> &str {\n    "hello"\n}\n'
        finding = Finding('first', 'src/worker.rs', 2, '    "hello"', 'Name GREETING in constants.rs and import it.')
        snapshot = bulk.CrateSources('src/lib.rs', {'src/lib.rs': 'mod worker;\n', 'src/worker.rs': source})
        result = self.apply(snapshot, finding)
        self.assertEqual(result.refusals, ())
        self.assertEqual(len(result.kept), 1)
        self.assertIn('pub(super) const GREETING: &str = "hello";', next(edit.edited for edit in result.edits if edit.created))
        self.assertIn('    GREETING', next(edit.edited for edit in result.edits if edit.path == finding.path))

    def test_crate_graph_ignores_unrelated_cfg_items_but_refuses_attributed_route(self) -> None:
        source = 'fn run() -> i32 {\n    42\n}\n'
        snapshot = bulk.CrateSources('src/lib.rs', {'src/lib.rs': '#[cfg(test)] mod tests;\nmod opaque;\nmod worker;\n', 'src/worker.rs': source,
             'src/opaque.rs': 'macro_rules! generated { () => {} }\n'})
        result = self.apply(snapshot, self.finding(source))
        self.assertEqual(result.refusals, ())
        self.assertEqual(len(result.kept), 1)
        snapshot = replace(snapshot, files={**snapshot.files, 'src/lib.rs': '#[cfg(test)] mod worker;\n'})
        result = self.apply(snapshot, self.finding(source))
        self.assertEqual(result.edits, ())
        self.assertEqual(result.refusals, (bulk.ConstantRefusal('first', 'finding is not in an ordinary crate module'),))

    def test_named_ledger_prose_with_preservation_notes(self) -> None:
        source = 'fn run() -> &\'static str {\n    "bisect_refs"\n}\n'
        snapshot = bulk.CrateSources('src/lib.rs', {'src/lib.rs': 'mod git;\n', 'src/git/mod.rs': 'mod checkout;\n', 'src/git/checkout.rs': source})
        finding = Finding('first', 'src/git/checkout.rs', 2, '    "bisect_refs"',
                          'Extract GIT_BISECT_REFS_OPERATION in owning git/constants.rs and import bare; preserve label.')
        result = self.apply(snapshot, finding)
        self.assertEqual(result.refusals, ())
        self.assertEqual(len(result.kept), 1)
        self.assertIn('pub(super) const GIT_BISECT_REFS_OPERATION: &str = "bisect_refs";',
                      next(edit.edited for edit in result.edits if edit.created))

    def test_crate_mode_refuses_computed_expected_values_and_incomplete_requests(self) -> None:
        source = 'fn run() -> i32 {\n    42\n}\n'
        snapshot = bulk.CrateSources('src/lib.rs', {'src/lib.rs': 'mod worker;\n', 'src/worker.rs': source})
        first = self.finding(source)
        for after, reason in (
            ('Derive the expected value from production geometry helpers: rows(42).', 'computed expected values require a worker'),
            ('Name the limit in constants.rs and import it.', 'no exact constant'),
            ('Name LIMIT in constants.rs and import it.', 'no exact constant type'),
            ('Add `const LIMIT: i32 = 21 + 21;` in constants.rs.', 'no exact constant'),
            ('Add `const LIMIT: i32 = 42;` in constants.rs; also change the algorithm.', 'no exact constant')):
            result = self.apply(snapshot, replace(first, after=after))
            self.assertEqual(result.edits, ())
            self.assertEqual(result.refusals, (bulk.ConstantRefusal('first', reason),))

    def test_crate_mode_refuses_patterns_macros_and_uncertain_module_graphs(self) -> None:
        for source in ('fn run() {\n    match value { 42 => (), _ => () }\n}\n',
                       'fn run() {\n    println!("{}", 42);\n}\n',
                       'fn run() {\n    let value = make::<42>();\n}\n'):
            finding = Finding('first', 'src/worker.rs', 2, source.splitlines()[1],
                              'Add `const LIMIT: i32 = 42;` in constants.rs.')
            snapshot = bulk.CrateSources('src/lib.rs', {'src/lib.rs': 'mod worker;\n', 'src/worker.rs': source})
            result = self.apply(snapshot, finding)
            self.assertEqual(result.edits, ())
            self.assertEqual(len(result.refusals), 1)
        source = 'fn run() -> i32 {\n    42\n}\n'
        for root in ('#[path = "worker.rs"] mod worker;\n', 'mod missing;\nmod worker;\n',
                     'mod worker { }\n', 'use other::*;\nmod worker;\n'):
            snapshot = bulk.CrateSources('src/lib.rs', {'src/lib.rs': root, 'src/worker.rs': source})
            result = self.apply(snapshot, self.finding(source))
            self.assertEqual(result.edits, ())
            self.assertEqual(len(result.refusals), 1)

    def test_crate_mode_refuses_taken_module_name_or_ambiguous_destination(self) -> None:
        source = 'fn run() -> i32 {\n    42\n}\n'
        snapshot = bulk.CrateSources('src/lib.rs', {'src/lib.rs': 'struct constants;\nmod worker;\n', 'src/worker.rs': source})
        result = self.apply(snapshot, self.finding(source))
        self.assertEqual(result.edits, ())
        self.assertEqual(result.refusals, (bulk.ConstantRefusal('first', 'constants module name taken'),))
        snapshot = replace(snapshot, files={**snapshot.files, 'src/lib.rs': 'mod worker;\n'})
        result = self.apply(snapshot, self.finding(source, destination='other/constants.rs'))
        self.assertEqual(result.edits, ())
        self.assertEqual(result.refusals, (bulk.ConstantRefusal('first', 'constant destination is ambiguous'),))

    def test_crate_proof_rejects_unexplained_changes_in_every_touched_file(self) -> None:
        source = 'fn run() -> i32 {\n    42\n}\n'
        snapshot = bulk.CrateSources('src/lib.rs', {'src/lib.rs': 'mod worker;\n', 'src/worker.rs': source})
        finding = self.finding(source)
        result = self.apply(snapshot, finding)
        self.assertEqual(len(result.kept), 1)
        for edit in result.edits:
            for changed in (replace(edit, edited=edit.edited + 'fn surprise() {}'),
                            replace(edit, edited=edit.edited + '// surprise comment\n'),
                            replace(edit, original=edit.original + ' '), replace(edit, created=not edit.created)):
                with self.subTest(path=edit.path, changed=changed):
                    edits = tuple(changed if other.path == edit.path else other for other in result.edits)
                    self.assertIsInstance(bulk.crate_constant_check(snapshot, edits, (finding,)), bulk.ConstantsNotProved)
        for edits in (result.edits[:-1], (*result.edits, result.edits[0])):
            self.assertIsInstance(bulk.crate_constant_check(snapshot, edits, (finding,)), bulk.ConstantsNotProved)

    def test_failed_crate_proof_discards_all_files_and_kept_findings(self) -> None:
        source = 'fn run() -> i32 {\n    42\n}\n'
        snapshot = bulk.CrateSources('src/lib.rs', {'src/lib.rs': 'mod worker;\n', 'src/worker.rs': source})
        original_render = bulk.render_crate_constants
        def corrupt(plan: bulk.CratePlan) -> tuple[bulk.CrateConstantEdit, ...]:
            edits = original_render(plan)
            return (replace(edits[0], edited=edits[0].edited + 'fn surprise() {}'), *edits[1:])
        with mock.patch.object(bulk, 'render_crate_constants', side_effect=corrupt):
            result = self.apply(snapshot, self.finding(source))
        self.assertEqual(result.edits, ())
        self.assertEqual(result.kept, ())
        self.assertEqual(result.refusals, (bulk.ConstantRefusal('first', 'crate proof failed: unexplained token change'),))


class RepairTests(unittest.TestCase):
    def extraction(self, statement: str, kind: str = 'usize', value: str = '42') -> tuple[bulk.CrateSources, Finding]:
        source = 'fn run() {\n    ' + statement + '\n}\n'
        finding = Finding('repair', 'src/worker.rs', 2, source.splitlines()[1],
                          f'Add `const LIMIT: {kind} = {value};` in constants.rs.')
        return bulk.CrateSources('src/lib.rs', {'src/lib.rs': 'mod worker;\n', 'src/worker.rs': source}), finding

    def test_refuses_silent_inference_change_in_both_modes(self) -> None:
        snapshot, finding = self.extraction('let value = 42;')
        source = snapshot.files[finding.path].replace('\n}', '\n    std::mem::size_of_val(&value);\n}')
        snapshot = replace(snapshot, files={**snapshot.files, finding.path: source})
        result = bulk.apply_crate_constants(snapshot, (finding,))
        self.assertEqual(result.edits, ())
        self.assertEqual(result.refusals, (bulk.ConstantRefusal('repair', 'type not proved'),))
        local = replace(finding, after='Add `const LIMIT: usize = 42;` at module level in this file.')
        per_file = bulk.apply_constants(Path('unused'), local.path, source, (local,))
        self.assertEqual(per_file.edited, source)
        self.assertEqual(per_file.refusals, result.refusals)

    def test_keeps_only_the_four_authorized_type_proofs(self) -> None:
        for statement, kind, value, proof in (
            ('let value = 42u16;', 'u16', '42u16', bulk.TypeProof.INTRINSIC),
            ('let value = 1.5f32;', 'f32', '1.5f32', bulk.TypeProof.INTRINSIC),
            ('let value = "hello";', '&str', '"hello"', bulk.TypeProof.INTRINSIC),
            ('let value = r#"hello"#;', "&'static str", 'r#"hello"#', bulk.TypeProof.INTRINSIC),
            ("let value = 'a';", 'char', "'a'", bulk.TypeProof.INTRINSIC),
            ('let value = true;', 'bool', 'true', bulk.TypeProof.INTRINSIC),
            ('let mut value: usize = 42;', 'usize', '42', bulk.TypeProof.ANNOTATION),
            ('let value: f32 = 1.5;', 'f32', '1.5', bulk.TypeProof.ANNOTATION),
            ('let value = 42 as u8;', 'usize', '42', bulk.TypeProof.CAST),
            ('let value = 0xff as u16;', 'u8', '0xff', bulk.TypeProof.CAST),
            ('let value = 42;', 'i32', '42', bulk.TypeProof.DEFAULT),
            ('let value = 0xff32;', 'i32', '0xff32', bulk.TypeProof.DEFAULT),
            ('let value = 1.5;', 'f64', '1.5', bulk.TypeProof.DEFAULT),
        ):
            with self.subTest(statement=statement, kind=kind):
                snapshot, finding = self.extraction(statement, kind, value)
                result = bulk.apply_crate_constants(snapshot, (finding,))
                self.assertEqual(result.refusals, ())
                self.assertEqual(len(result.kept), 1)
                self.assertIs(result.kept[0].type_proof, proof)
                self.assertIsInstance(bulk.crate_constant_check(snapshot, result.edits, (finding,)), bulk.ConstantsProved)
        for statement, kind, value in (
            ('let value = 42u8;', 'u16', '42u8'),
            ('let value = "hello";', 'char', '"hello"'),
            ('let value: u64 = 42;', 'usize', '42'),
            ('let value: usize = 42 + other;', 'usize', '42'),
            ('let value = 256 as u8;', 'usize', '256'),
            ('let value = 256 as u16;', 'u8', '256'),
            ('let value = 42;', 'f64', '42'),
            ('let value = 1.5;', 'f32', '1.5'),
            ('let value = 0xff32;', 'f32', '0xff32'),
        ):
            with self.subTest(statement=statement, kind=kind):
                snapshot, finding = self.extraction(statement, kind, value)
                result = bulk.apply_crate_constants(snapshot, (finding,))
                self.assertEqual(result.edits, ())
                self.assertEqual(result.refusals, (bulk.ConstantRefusal('repair', 'type not proved'),))

    def test_attributed_routes_refuse_every_visibility_prefix(self) -> None:
        snapshot, finding = self.extraction('let value: usize = 42;')
        for visibility in ('', 'pub ', 'pub(crate) ', 'pub(super) ', 'pub(self) ', 'pub(in crate) ', 'pub(in crate::feature) '):
            for attribute in ('#[cfg(any())]', '#[path = "actual.rs"]', '#[other]'):
                with self.subTest(visibility=visibility, attribute=attribute):
                    files = {**snapshot.files, 'src/lib.rs': f'{attribute}\n{visibility}mod worker;\n',
                             'src/actual.rs': snapshot.files[finding.path]}
                    result = bulk.apply_crate_constants(replace(snapshot, files=files), (finding,))
                    self.assertEqual(result.edits, ())
                    self.assertEqual(result.refusals, (bulk.ConstantRefusal('repair', 'finding is not in an ordinary crate module'),))
        for visibility in ('pub(unknown)', 'unsafe', 'pub(in crate + nope)'):
            result = bulk.apply_crate_constants(replace(snapshot, files={**snapshot.files,
                'src/lib.rs': f'{visibility} mod worker;\n'}), (finding,))
            self.assertEqual(result.edits, ())
            self.assertEqual(len(result.refusals), 1)

    def test_unattributed_visibility_routes_remain_ordinary(self) -> None:
        snapshot, finding = self.extraction('let value: usize = 42;')
        for visibility in ('pub', 'pub(crate)', 'pub(super)', 'pub(self)', 'pub(in crate)'):
            result = bulk.apply_crate_constants(replace(snapshot, files={**snapshot.files,
                'src/lib.rs': f'{visibility} mod worker;\n'}), (finding,))
            self.assertEqual(result.refusals, ())
            self.assertEqual(len(result.kept), 1)

    def test_nested_header_comments_keep_wiring_and_imports_as_code(self) -> None:
        snapshot, finding = self.extraction('let value: usize = 42;')
        for header in ('/*! Outer /* nested */ still docs */\n',
                       '/* Outer /** nested */ still comment */\n',
                       '/** Outer /* nested */ still docs */\n'):
            with self.subTest(header=header):
                snapshot_with_docs = replace(snapshot, files={
                    path: header + text for path, text in snapshot.files.items()})
                shifted = replace(finding, line=finding.line + 1)
                result = bulk.apply_crate_constants(snapshot_with_docs, (shifted,))
                self.assertEqual(result.refusals, ())
                self.assertEqual(len(result.kept), 1)
                edited = {edit.path: edit.edited for edit in result.edits}
                self.assertEqual(bulk.Source(edited['src/lib.rs']).words[:3], ('mod', 'constants', ';'))
                self.assertIn('use', bulk.Source(edited[finding.path]).words)
                self.assertIsInstance(bulk.crate_constant_check(snapshot_with_docs, result.edits, (shifted,)), bulk.ConstantsProved)
        malformed = replace(snapshot, files={**snapshot.files, 'src/lib.rs': '/*! unclosed\nmod worker;\n'})
        self.assertEqual(bulk.apply_crate_constants(malformed, (finding,)).edits, ())

    def test_proof_rejects_planned_tokens_hidden_inside_a_comment(self) -> None:
        snapshot, finding = self.extraction('let value: usize = 42;')
        header = '/*! Outer /* nested */ still docs */\n'
        snapshot = replace(snapshot, files={**snapshot.files, 'src/lib.rs': header + 'mod worker;\n'})
        def broken_header_end(text: str) -> int:
            return text.index('*/') + 2 if text.startswith('/*!') else 0
        with mock.patch.object(bulk, '_header_end', side_effect=broken_header_end):
            with mock.patch.object(bulk, 'crate_constant_check', return_value=bulk.ConstantsProved()):
                unchecked = bulk.apply_crate_constants(snapshot, (finding,))
            self.assertEqual(len(unchecked.kept), 1)
            self.assertIsInstance(bulk.crate_constant_check(snapshot, unchecked.edits, (finding,)), bulk.ConstantsNotProved)
            result = bulk.apply_crate_constants(snapshot, (finding,))
        self.assertEqual(result.edits, ())
        self.assertEqual(result.kept, ())

    def test_proof_rejects_a_declaration_inserted_inside_an_existing_comment(self) -> None:
        snapshot, finding = self.extraction('let value: usize = 42;')
        snapshot = replace(snapshot, files={**snapshot.files,
            'src/lib.rs': 'mod constants;\nmod worker;\n',
            'src/constants.rs': '// extracted values\npub(super) const ALPHA: usize = 1 /*;\npub(super) const ZEBRA: usize = 2; */;\n'})
        result = bulk.apply_crate_constants(snapshot, (finding,))
        self.assertEqual(result.edits, ())
        self.assertEqual(result.kept, ())
        self.assertEqual(len(result.refusals), 1)

    def test_crate_indexes_once_and_renders_shared_declarations_once(self) -> None:
        for count in (1, 40):
            source = 'fn run() {\n' + ''.join(f'    let value_{i}: usize = {i};\n' for i in range(count)) + '}\n'
            snapshot = bulk.CrateSources('src/lib.rs', {'src/lib.rs': 'mod worker;\n', 'src/worker.rs': source})
            findings = tuple(Finding(str(i), 'src/worker.rs', i + 2, source.splitlines()[i + 1],
                             f'Add `const LIMIT_{i}: usize = {i};` in constants.rs.') for i in range(count))
            real_render = bulk.ConstantLayout.render
            rendered_sizes: list[int] = []
            def render_once(layout: bulk.ConstantLayout, additions: Mapping[str, str]) -> tuple[str, tuple[rust.Token, ...]]:
                rendered_sizes.append(len(additions))
                return real_render(layout, additions)
            with mock.patch.object(bulk, 'Source', wraps=bulk.Source) as parse, mock.patch.object(
                    bulk.ConstantLayout, 'render', autospec=True, side_effect=render_once):
                result = bulk.apply_crate_constants(snapshot, findings)
            self.assertEqual(result.refusals, ())
            self.assertEqual(len(result.kept), count)
            # The independent proof plans again; each plan parses each source
            # once and renders the full accumulated declaration set once.
            self.assertEqual(parse.call_count, 2 * len(snapshot.files))
            self.assertEqual(rendered_sizes, [count, count])


if __name__ == '__main__':
    _ = unittest.main()
