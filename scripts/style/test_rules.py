"""Rule metadata is validated before global and local rules enter the registry."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, final, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rules
import style_rules
from candidate_generators import CandidatesSpecError, read_candidates_spec


@final
class RuleTests(unittest.TestCase):
    root: Path = Path()
    global_dir: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.global_dir = self.root / "nate_style" / "rust"
        self.global_dir.mkdir(parents=True)
        _ = self.enterContext(mock.patch.object(style_rules, "NATE_STYLE_DIR", self.global_dir.parent))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"STYLE_LEDGER_STATE_DIR": str(self.root / "state")})))

    def write_rule(self, name: str, metadata: str) -> Path:
        path = self.global_dir / f"{name}.md"
        _ = path.write_text(f"---\n{metadata}---\n# {name}\n")
        return path

    def member_repo(self) -> Path:
        repo = self.root / "repo"
        member = repo / "crates" / "member"
        member.mkdir(parents=True, exist_ok=True)
        _ = (repo / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/member"]\n')
        _ = (member / "Cargo.toml").write_text('[package]\nname = "public-member"\nversion = "0.1.0"\n')
        return repo

    def test_loads_each_tier_and_preserves_scheduling_metadata(self) -> None:
        _ = self.write_rule("tool", "tier: A\nmechanism: clippy\n")
        _ = self.write_rule("listed", "tier: B\nfamily: names\npre_filter: 'Some<'\nmode: propose\n")
        _ = self.write_rule("read", "tier: C\nfamily: values\ninput_scope: file\n")
        _ = self.write_rule("structure", "tier: D\nfamily: modules\ninput_scope: manifest\n")
        _ = self.write_rule("process", "tier: E\n")

        loaded = {rule.name: rule for rule in rules.load_rules(None)}

        self.assertEqual({name: rule.tier for name, rule in loaded.items()}, {
            "tool": "A", "listed": "B", "read": "C", "structure": "D", "process": "E",
        })
        self.assertEqual(loaded["listed"].pre_filter, "Some<")
        self.assertTrue(loaded["listed"].mode_propose)
        self.assertEqual(loaded["listed"].family, "names")
        self.assertEqual(loaded["listed"].input_scope, "crate")
        self.assertEqual(loaded["read"].input_scope, "file")
        self.assertEqual(loaded["structure"].input_scope, "manifest")
        self.assertFalse(loaded["process"].mode_propose)
        self.assertIsNone(loaded["process"].family)
        self.assertIsNone(loaded["read"].candidates)
        self.assertIsNone(loaded["read"].pre_filter)
        self.assertEqual({rule.origin for rule in loaded.values()}, {"global"})

    def test_partial_candidates_retain_uncovered_surface_and_generator_constraints(self) -> None:
        path = self.write_rule(
            "owned-booleans",
            "tier: B\nfamily: types\ncandidates:\n  kind: struct_fields\n"
            + "  surface: 'partial'\n  uncovered: 'arguments, returns, accumulators'\n"
            + "  structs: [Settings]\n  field_type_pattern: 'bool'\n",
        )

        rule, = rules.load_rules(None)
        spec = rule.candidates
        if spec is None:
            self.fail("The listed rule lost its candidate declaration")
        self.assertEqual((spec.surface, spec.uncovered), ("partial", "arguments, returns, accumulators"))
        self.assertEqual((spec.kind, spec.structs, spec.field_type_pattern), ("struct_fields", ("Settings",), "bool"))
        self.assertEqual(spec.origin, str(path))

    def test_listed_rule_requires_a_top_level_pre_filter(self) -> None:
        metadata = "tier: B\nfamily: names\nother:\n  pre_filter: never_matches\n"
        path = self.write_rule("listed", metadata)

        with self.assertRaises(rules.RuleValidationError) as raised:
            _ = rules.load_rules(None)
        self.assertIn(str(path), str(raised.exception))
        self.assertIn("requires candidates or pre_filter", str(raised.exception))

        _ = self.write_rule("listed", metadata + "pre_filter: 'Some<'\n")
        rule, = rules.load_rules(None)
        self.assertEqual(rule.pre_filter, "Some<")

    def test_candidate_metadata_errors_name_the_rule(self) -> None:
        path = self.write_rule(
            "invalid", "tier: B\nfamily: names\ncandidates:\n  kind: regex\n  pattern: item\n"
        )
        spec = read_candidates_spec(path)
        if spec is None:
            self.fail("The candidates block was not read")
        self.assertEqual(spec.surface, "unclassified")
        with self.assertRaises(rules.RuleValidationError) as missing_surface:
            _ = rules.load_rules(None)
        self.assertIn(str(path), str(missing_surface.exception))
        self.assertIn("candidates.surface must be whole or partial", str(missing_surface.exception))

        declarations = (
            ("kind: regex\n  surface: uncertain", "surface"),
            ("kind: regex\n  surface: ''", "surface"),
            ("kind: regex\n  surface: unclassified", "surface"),
            ("kind: regex\n  surface: partial", "uncovered"),
            ("kind: regex\n  surface: partial\n  uncovered: ''", "uncovered"),
            ("surface: whole", "kind"),
            ("", "kind"),
        )
        for declaration, reason in declarations:
            with self.subTest(declaration=declaration):
                path = self.write_rule("invalid", f"tier: B\nfamily: names\ncandidates:\n  {declaration}\n")
                with self.assertRaises(CandidatesSpecError) as raised:
                    _ = read_candidates_spec(path)
                self.assertIn(str(path), str(raised.exception))
                self.assertIn(reason, str(raised.exception))
                with self.assertRaises(rules.RuleValidationError) as raised_rule:
                    _ = rules.load_rules(None)
                self.assertIn(str(path), str(raised_rule.exception))

    def test_top_level_surface_does_not_classify_candidate_block(self) -> None:
        path = self.write_rule(
            "invalid", "tier: B\nfamily: names\ncandidates:\n  kind: regex\n  pattern: item\nsurface: whole\n"
        )
        spec = read_candidates_spec(path)
        if spec is None:
            self.fail("The candidates block was not read")
        self.assertEqual(spec.surface, "unclassified")

        with self.assertRaises(rules.RuleValidationError) as raised:
            _ = rules.load_rules(None)
        self.assertIn(str(path), str(raised.exception))
        self.assertIn("candidates.surface must be whole or partial", str(raised.exception))

    def test_legacy_global_rules_load_and_list_as_unclassified(self) -> None:
        _ = self.write_rule("listed", "mechanism: llm\ncandidates:\n  kind: regex\n  pattern: item\n")
        _ = self.write_rule("read", "mechanism: llm\nmode: propose\npre_filter: 'Some<'\n")
        _ = self.write_rule("tool", "mechanism: clippy\nmode: auto\n")
        bare = self.global_dir / "bare.md"
        _ = bare.write_text("# A rule without frontmatter\n")

        loaded = {rule.name: rule for rule in rules.load_rules(None)}

        self.assertEqual(set(loaded), {"bare", "listed", "read", "tool"})
        self.assertEqual({rule.tier for rule in loaded.values()}, {"unclassified"})
        self.assertEqual({rule.family for rule in loaded.values()}, {None})
        self.assertEqual({rule.input_scope for rule in loaded.values()}, {"crate"})
        self.assertEqual({rule.origin for rule in loaded.values()}, {"global"})
        spec = loaded["listed"].candidates
        if spec is None:
            self.fail("The legacy listed rule lost its candidate declaration")
        self.assertEqual((spec.kind, spec.pattern, spec.surface), ("regex", "item", "unclassified"))
        self.assertEqual(loaded["read"].pre_filter, "Some<")
        self.assertTrue(loaded["read"].mode_propose)
        self.assertFalse(loaded["tool"].mode_propose)

        environment = {**os.environ, "STYLE_HISTORY_NATE_STYLE_DIR": str(self.global_dir.parent)}
        command = [sys.executable, str(Path(rules.__file__)), "list"]
        text = subprocess.run(command, env=environment, capture_output=True, text=True, check=True)
        self.assertEqual(text.stdout.splitlines(), [
            "rule_id\tname\ttier\tfamily\tinput_scope\tsurface",
            "global:rust/bare.md\tbare\tunclassified\t-\tcrate\t-",
            "global:rust/listed.md\tlisted\tunclassified\t-\tcrate\tunclassified",
            "global:rust/read.md\tread\tunclassified\t-\tcrate\t-",
            "global:rust/tool.md\ttool\tunclassified\t-\tcrate\t-",
        ])
        output = subprocess.run([*command, "--json"], env=environment, capture_output=True, text=True, check=True)
        rows = cast(list[rules.RuleListing], json.loads(output.stdout))
        self.assertEqual(rows, [
            {"rule_id": f"global:rust/{name}.md", "name": name, "tier": "unclassified", "family": None, "input_scope": "crate",
             "surface": "unclassified" if name == "listed" else None}
            for name in ("bare", "listed", "read", "tool")
        ])

    def test_incomplete_global_classification_still_requires_tier(self) -> None:
        declarations = (
            "family: names\n",
            "input_scope: crate\n",
            "input_scope: invalid\n",
        )
        for declaration in declarations:
            with self.subTest(declaration=declaration):
                path = self.write_rule("incomplete", "mechanism: llm\n" + declaration)
                with self.assertRaises(rules.RuleValidationError) as raised:
                    _ = rules.load_rules(None)
                self.assertIn(str(path), str(raised.exception))
                self.assertIn("tier", str(raised.exception))

    def test_candidate_coverage_does_not_classify_a_global_rule(self) -> None:
        declarations = (
            ("  surface: whole\n", "whole", ""),
            ("  surface: partial\n  uncovered: 'other sites'\n", "partial", "other sites"),
            ("  uncovered: 'other sites'\n", "unclassified", "other sites"),
        )
        for declaration, expected_surface, expected_uncovered in declarations:
            with self.subTest(declaration=declaration):
                _ = self.write_rule(
                    "listed", "mechanism: llm\ncandidates:\n  kind: regex\n  pattern: item\n" + declaration
                )

                rule, = rules.load_rules(None)

                self.assertEqual(rule.tier, "unclassified")
                self.assertIsNone(rule.family)
                spec = rule.candidates
                if spec is None:
                    self.fail("The unclassified rule lost its candidate declaration")
                self.assertEqual((spec.surface, spec.uncovered), (expected_surface, expected_uncovered))

    def test_nested_candidate_values_do_not_replace_top_level_rule_metadata(self) -> None:
        _ = self.write_rule(
            "listed", "tier: B\nfamily: names\ninput_scope: crate\n"
            + "candidates:\n  kind: regex\n  surface: whole\n  pattern: item\n"
            + "  family: types\n  input_scope: file\n",
        )
        rule, = rules.load_rules(None)
        self.assertEqual((rule.family, rule.input_scope), ("names", "crate"))

    def test_rejects_invalid_tier_family_mechanism_scope_and_listing(self) -> None:
        declarations = (
            ("family: names\n", "tier"),
            ("tier: F\n", "tier"),
            ("tier: ''\n", "tier"),
            ("tier: A\nmechanism: llm\n", "mechanism"),
            ("tier: A\n", "mechanism"),
            ("tier: C\nfamily: names\nmechanism: rustfmt\n", "mechanism"),
            ("tier: B\nfamily: names\nmechanism: mend\n", "mechanism"),
            ("tier: A\nmechanism: clippy\nfamily: names\n", "forbidden"),
            ("tier: E\nfamily: names\n", "forbidden"),
            ("tier: B\npre_filter: item\n", "family"),
            ("tier: C\n", "family"),
            ("tier: D\n", "family"),
            ("tier: C\nfamily: other\n", "family"),
            ("tier: C\nfamily: names\ninput_scope: directory\n", "input_scope"),
            ("tier: C\nfamily: names\ninput_scope: ''\n", "input_scope"),
            ("tier: B\nfamily: names\n", "requires candidates or pre_filter"),
            ("tier: B\nfamily: names\npre_filter: ''\n", "requires candidates or pre_filter"),
        )
        for declaration, reason in declarations:
            with self.subTest(declaration=declaration):
                path = self.write_rule("invalid", declaration)
                with self.assertRaises(rules.RuleValidationError) as raised:
                    _ = rules.load_rules(None)
                self.assertIn(str(path), str(raised.exception))
                self.assertIn(reason, str(raised.exception))

    def test_tool_mechanisms_are_all_tier_a(self) -> None:
        for mechanism in ("clippy", "mend", "rustfmt"):
            _ = self.write_rule(mechanism, f"tier: A\nmechanism: '{mechanism}'\n")
        loaded = rules.load_rules(None)
        self.assertEqual([rule.name for rule in loaded], ["clippy", "mend", "rustfmt"])
        self.assertEqual([rule.tier for rule in loaded], ["A", "A", "A"])

    def test_global_tier_d_rule_rejects_the_types_family(self) -> None:
        path = self.write_rule("structure", "tier: D\nfamily: types\n")

        with self.assertRaises(rules.RuleValidationError) as raised:
            _ = rules.load_rules(None)

        self.assertEqual(str(raised.exception), f"{path}: tier D requires the modules family")

    def test_approval_loads_always_with_the_rule_family_and_refuses_any_other_value(self) -> None:
        _ = self.write_rule("asked", "tier: C\nfamily: names\n")
        _ = self.write_rule("always", "tier: C\nfamily: types\napproval: always\n")
        self.assertEqual({rule.name: rule.approval for rule in rules.load_rules(None)},
                         {"asked": rules.UserApproves(), "always": rules.AlwaysApproved("types")})
        for name, metadata, reason in (("typo", "tier: C\nfamily: names\napproval: alway\n", "approval must be always"),
                                       ("blank", "tier: C\nfamily: names\napproval:\n", "approval must be a nonempty scalar"),
                                       ("unclassified", "approval: always\n", "approval always requires a family")):
            with self.subTest(name=name):
                path = self.write_rule(name, metadata)
                with self.assertRaises(rules.RuleValidationError) as raised:
                    _ = rules.load_rules(None)
                self.assertEqual(str(raised.exception), f"{path}: {reason}")
                path.unlink()

    def test_local_tier_d_rule_rejects_the_types_family(self) -> None:
        repo = self.member_repo()
        local_dir = repo / "docs" / "style"
        local_dir.mkdir(parents=True)
        path = local_dir / "structure.md"
        _ = path.write_text("---\ntier: D\nfamily: types\n---\n# Structure rule\n")

        with self.assertRaises(rules.RuleValidationError) as raised:
            _ = rules.load_rules(repo)

        self.assertEqual(str(raised.exception), f"{path}: tier D requires the modules family")

    def test_modules_family_loads_for_tiers_b_c_and_d_from_both_origins(self) -> None:
        repo = self.member_repo()
        local_dir = repo / "docs" / "style"
        local_dir.mkdir(parents=True)
        for tier in ("B", "C", "D"):
            metadata = f"tier: {tier}\nfamily: modules\n"
            if tier == "B":
                metadata += "pre_filter: item\n"
            _ = self.write_rule(f"modules-{tier}", metadata)
            _ = (local_dir / f"modules-{tier}.md").write_text(f"---\n{metadata}---\n# Modules rule\n")

        loaded = rules.load_rules(repo)

        self.assertEqual(
            {(rule.origin, rule.tier, rule.family) for rule in loaded},
            {(origin, tier, "modules") for origin in ("global", "local") for tier in ("B", "C", "D")},
        )
        self.assertEqual(len(loaded), 6)

    def test_discovers_root_and_member_rules_without_filtering_tool_or_bevy_rules(self) -> None:
        global_path = self.write_rule("same-name", "tier: A\nmechanism: mend\ntags:\n  - bevy\nmode: auto\n")
        repo = self.member_repo()
        root_dir = repo / "docs" / "style"
        member_dir = repo / "docs" / "member" / "style"
        nested_dir = repo / "docs" / "nested" / "other" / "style"
        for directory in (root_dir, member_dir, nested_dir):
            directory.mkdir(parents=True)
        root_path = root_dir / "same-name.md"
        member_path = member_dir / "member-rule.md"
        _ = root_path.write_text("---\ntier: C\nfamily: values\n---\n# Root rule\n")
        _ = member_path.write_text("---\ntier: D\nfamily: modules\n---\n# Member rule\n")
        _ = (nested_dir / "ignored.md").write_text("No metadata: below the lookup depth\n")

        loaded = rules.load_rules(repo)

        self.assertEqual([rule.path for rule in loaded], [global_path, root_path, member_path])
        self.assertEqual([rule.origin for rule in loaded], ["global", "local", "local"])
        self.assertEqual([rule.family for rule in loaded], [None, "values", "modules"])
        self.assertEqual([rule.rule_id for rule in loaded], [
            "global:rust/same-name.md", "local:docs/style/same-name.md", "local:docs/member/style/member-rule.md",
        ])
        self.assertEqual([rule.reach for rule in loaded], [
            rules.RepoReach(), rules.RepoReach(), rules.MemberReach("member", Path("crates/member")),
        ])

    def test_member_reach_resolves_package_name_as_well_as_directory_name(self) -> None:
        repo = self.member_repo()
        local_dir = repo / "docs" / "public-member" / "style"
        local_dir.mkdir(parents=True)
        _ = (local_dir / "local.md").write_text("---\ntier: C\nfamily: types\n---\n# Local rule\n")

        rule, = rules.load_rules(repo)

        self.assertEqual(rule.rule_id, "local:docs/public-member/style/local.md")
        self.assertEqual(rule.reach, rules.MemberReach("public-member", Path("crates/member")))

    def test_root_member_reach_resolves_checkout_directory_and_package_names(self) -> None:
        repo = self.root / "sample"
        repo.mkdir()
        _ = (repo / "Cargo.toml").write_text('[package]\nname = "root-package"\nversion = "0.1.0"\n')
        for name in ("sample", "root-package"):
            local_dir = repo / "docs" / name / "style"
            local_dir.mkdir(parents=True)
            _ = (local_dir / "local.md").write_text("---\ntier: C\nfamily: types\n---\n# Local rule\n")

        loaded = rules.load_rules(repo)

        self.assertEqual([rule.rule_id for rule in loaded], [
            "local:docs/root-package/style/local.md", "local:docs/sample/style/local.md",
        ])
        self.assertEqual([rule.reach for rule in loaded], [
            rules.MemberReach("root-package", Path(".")), rules.MemberReach("sample", Path(".")),
        ])

    def test_member_rule_without_a_matching_workspace_member_names_the_error(self) -> None:
        repo = self.member_repo()
        local_dir = repo / "docs" / "absent" / "style"
        local_dir.mkdir(parents=True)
        path = local_dir / "local.md"
        _ = path.write_text("---\ntier: C\nfamily: types\n---\n# Local rule\n")

        with self.assertRaises(rules.RuleValidationError) as raised:
            _ = rules.load_rules(repo)

        self.assertIn(str(path), str(raised.exception))
        self.assertIn("member-local rule names no workspace member absent", str(raised.exception))

    def test_member_reach_follows_recursive_in_tree_path_dependencies(self) -> None:
        repo = self.member_repo()
        manifest = repo / "crates/member/Cargo.toml"
        _ = manifest.write_text(manifest.read_text() + '\n[dependencies]\nhelper = { path = "../helper" }\n')
        for name, dependency in (("helper", '\n[target.\'cfg(unix)\'.build-dependencies]\nleaf = { path = "../leaf" }\n'), ("leaf", "")):
            directory = repo / "crates" / name
            directory.mkdir()
            _ = (directory / "Cargo.toml").write_text(f'[package]\nname = "{name}"\nversion = "0.1.0"\n' + dependency)
        local_dir = repo / "docs/leaf/style"
        local_dir.mkdir(parents=True)
        _ = (local_dir / "local.md").write_text("---\ntier: C\nfamily: types\n---\n# Leaf\n")

        rule, = rules.load_rules(repo)

        self.assertEqual(rule.reach, rules.MemberReach("leaf", Path("crates/leaf")))

    def test_implicit_member_reach_honors_workspace_exclusions(self) -> None:
        repo = self.member_repo()
        _ = (repo / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/member"]\nexclude = ["crates/excluded"]\n'
            + '[workspace.dependencies]\nhelper = { path = "crates/excluded" }\n'
        )
        manifest = repo / "crates/member/Cargo.toml"
        _ = manifest.write_text(manifest.read_text() + '\n[dev-dependencies]\nhelper.workspace = true\n')
        directory = repo / "crates/excluded"
        directory.mkdir()
        _ = (directory / "Cargo.toml").write_text('[package]\nname = "excluded"\nversion = "0.1.0"\n')
        local_dir = repo / "docs/excluded/style"
        local_dir.mkdir(parents=True)
        path = local_dir / "local.md"
        _ = path.write_text("---\ntier: C\nfamily: types\n---\n# Excluded\n")

        with self.assertRaises(rules.RuleValidationError) as raised:
            _ = rules.load_rules(repo)

        self.assertIn(str(path), str(raised.exception))
        self.assertIn("member-local rule names no workspace member excluded", str(raised.exception))

    def test_local_tier_e_rule_loads_without_a_family(self) -> None:
        repo = self.member_repo()
        local_dir = repo / "docs" / "style"
        local_dir.mkdir(parents=True)
        path = local_dir / "process.md"
        _ = path.write_text("---\ntier: E\n---\n# Process rule\n")

        rule, = rules.load_rules(repo)

        self.assertEqual((rule.path, rule.origin, rule.tier), (path, "local", "E"))
        self.assertIsNone(rule.family)

    def test_local_clippy_rule_loads_as_tier_a_without_a_family(self) -> None:
        repo = self.member_repo()
        local_dir = repo / "docs" / "member" / "style"
        local_dir.mkdir(parents=True)
        path = local_dir / "tool.md"
        _ = path.write_text("---\ntier: A\nmechanism: clippy\n---\n# Tool rule\n")

        rule, = rules.load_rules(repo)

        self.assertEqual((rule.path, rule.origin, rule.tier), (path, "local", "A"))
        self.assertEqual(rule.reach, rules.MemberReach("member", Path("crates/member")))
        self.assertIsNone(rule.family)

    def test_local_tier_c_rule_requires_a_family(self) -> None:
        repo = self.member_repo()
        local_dir = repo / "docs" / "style"
        local_dir.mkdir(parents=True)
        path = local_dir / "read.md"
        _ = path.write_text("---\ntier: C\n---\n# Read rule\n")

        with self.assertRaises(rules.RuleValidationError) as raised:
            _ = rules.load_rules(repo)

        self.assertEqual(str(raised.exception), f"{path}: family is required for tier B, C and D")

    def test_local_tier_e_rule_forbids_a_family(self) -> None:
        repo = self.member_repo()
        local_dir = repo / "docs" / "style"
        local_dir.mkdir(parents=True)
        path = local_dir / "process.md"
        _ = path.write_text("---\ntier: E\nfamily: names\n---\n# Process rule\n")

        with self.assertRaises(rules.RuleValidationError) as raised:
            _ = rules.load_rules(repo)

        self.assertIn(str(path), str(raised.exception))
        self.assertIn("forbidden", str(raised.exception))

    def test_local_rules_must_declare_a_tier(self) -> None:
        repo = self.member_repo()
        local_dir = repo / "docs" / "member" / "style"
        local_dir.mkdir(parents=True)
        path = local_dir / "local.md"
        declarations = (
            ("family: names", "tier"),
            ("mechanism: llm\ncandidates:\n  kind: regex\n  pattern: item", "tier"),
        )
        for declaration, reason in declarations:
            with self.subTest(declaration=declaration):
                _ = path.write_text(f"---\n{declaration}\n---\n# Local rule\n")
                with self.assertRaises(rules.RuleValidationError) as raised:
                    _ = rules.load_rules(repo)
                self.assertIn(str(path), str(raised.exception))
                self.assertIn(reason, str(raised.exception))

    def test_local_candidate_rules_require_a_declared_surface(self) -> None:
        repo = self.member_repo()
        local_dir = repo / "docs" / "member" / "style"
        local_dir.mkdir(parents=True)
        path = local_dir / "local.md"
        _ = path.write_text(
            "---\ntier: B\nfamily: names\ncandidates:\n  kind: regex\n  pattern: item\n---\n# Local rule\n"
        )

        with self.assertRaises(rules.RuleValidationError) as raised:
            _ = rules.load_rules(repo)
        self.assertIn(str(path), str(raised.exception))
        self.assertIn("candidates.surface must be whole or partial", str(raised.exception))

    def test_blob_tracks_uncommitted_file_contents(self) -> None:
        path = self.write_rule("read", "tier: C\nfamily: types\n")
        first, = rules.load_rules(None)
        content = path.read_bytes()
        expected = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
        self.assertEqual(first.blob, expected)
        _ = path.write_text(path.read_text() + "Changed rule text\n")
        second, = rules.load_rules(None)
        self.assertNotEqual(first.blob, second.blob)

    def test_focused_eval_keeps_its_json_protocol_with_new_metadata(self) -> None:
        path = self.write_rule(
            "listed", "tier: B\nfamily: names\ninput_scope: crate\nmechanism: llm\nmode: propose\n"
            + "tags:\n  - non-negotiable\ncandidates:\n  kind: regex\n  surface: whole\n  pattern: item\n",
        )
        loader = self.root / "list-files.zsh"
        _ = loader.write_text('printf \'%s\\n\' "$STYLE_RULE_TEST_FILE"\n')
        environment = {
            **os.environ,
            "STYLE_HISTORY_NATE_STYLE_DIR": str(self.global_dir.parent),
            "STYLE_HISTORY_LOAD_STYLE_SCRIPT": str(loader),
            "STYLE_RULE_TEST_FILE": str(path),
        }
        output = subprocess.run(
            [sys.executable, str(Path(style_rules.__file__)), "focused-eval",
             "--project-root", str(self.root), "--guideline", "listed"],
            env=environment, capture_output=True, text=True, check=True,
        )
        self.assertEqual(output.stdout, json.dumps({
            "unit_id": "rust/listed.md",
            "display_name": "listed",
            "guideline_ids": ["rust/listed.md"],
            "see_also_guideline_ids": [],
        }, sort_keys=True) + "\n")

    def test_cli_lists_the_same_metadata_as_text_and_json_and_reports_invalid_rules(self) -> None:
        _ = self.write_rule(
            "listed", "tier: B\nfamily: names\ncandidates:\n  kind: regex\n  pattern: item\n  surface: whole\n"
        )
        environment = {**os.environ, "STYLE_HISTORY_NATE_STYLE_DIR": str(self.global_dir.parent)}
        command = [sys.executable, str(Path(rules.__file__)), "list"]
        text = subprocess.run(command, env=environment, capture_output=True, text=True, check=True)
        self.assertEqual(text.stdout.splitlines(), [
            "rule_id\tname\ttier\tfamily\tinput_scope\tsurface", "global:rust/listed.md\tlisted\tB\tnames\tcrate\twhole",
        ])
        output = subprocess.run([*command, "--json"], env=environment, capture_output=True, text=True, check=True)
        rows = cast(list[rules.RuleListing], json.loads(output.stdout))
        self.assertEqual(rows, [{
            "rule_id": "global:rust/listed.md", "name": "listed", "tier": "B", "family": "names", "input_scope": "crate", "surface": "whole",
        }])
        invalid = self.write_rule("invalid", "tier: C\n")
        error = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
        self.assertEqual(error.returncode, 2)
        self.assertIn(str(invalid), error.stderr)
        self.assertNotIn("Traceback", error.stderr)


if __name__ == "__main__":
    _ = unittest.main()
