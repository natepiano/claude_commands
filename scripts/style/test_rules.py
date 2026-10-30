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

    def test_manifest_read_failure_remains_the_original_file_error(self) -> None:
        repo = self.member_repo()
        path = repo / "docs/member/style/rule.md"
        path.parent.mkdir(parents=True)
        _ = path.write_text("---\ntier: C\nfamily: names\n---\n# Rule\n")
        failure = PermissionError("manifest unreadable")
        original = Path.read_text

        def read(source: Path) -> str:
            if source.name == "Cargo.toml":
                raise failure
            return original(source)

        with mock.patch.object(Path, "read_text", read):
            with self.assertRaises(PermissionError) as refused:
                _ = rules.load_rules(repo)
        self.assertIs(refused.exception, failure)

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
        self.assertIn("unsupported frontmatter block", str(raised.exception))

        _ = self.write_rule("listed", "tier: B\nfamily: names\npre_filter: 'Some<'\n")
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


class JudgedRuleTextTests(unittest.TestCase):
    def assert_snapshot_comparison(self, before: bytes, after: bytes, *, unchanged: bool) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            path = repo / "rule.md"
            old = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                                 input=before, check=True, capture_output=True).stdout.decode().strip()
            _ = path.write_bytes(after)
            current = rules._load_rule(path, "local", repo, rules.RepoReach())  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(rules.judged_text_unchanged(old, current), unchanged)
            self.assertEqual(rules.judged_text_unchanged(current.blob, current, compared_blob=old), unchanged)

    def test_unicode_line_separator_cannot_hide_scope_change(self) -> None:
        before = "---\ntier: C\nfamily: names\ntags: [one] # comment\u2028input_scope: file\n---\n# Body\n".encode()
        self.assert_snapshot_comparison(before, before.replace(b"file", b"crate"), unchanged=False)

    def test_other_unicode_line_frames_cannot_hide_scope_change(self) -> None:
        for separator in ("\u2029", "\u0085", "\u001c", "\u001d", "\u001e", "\v", "\f"):
            with self.subTest(separator=repr(separator)):
                before = f"---\ntier: C\nfamily: names\ntags: [one] # comment{separator}input_scope: file\n---\n# Body\n".encode()
                self.assert_snapshot_comparison(before, before.replace(b"file", b"crate"), unchanged=False)

    def test_framing_mismatch_is_stale_even_for_equal_blobs_and_unchanged_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            path = repo / "rule.md"
            for content in (
                "---\ntier: C\nfamily: names\ntags: [one] # comment\u2028input_scope: file\n---\n# Body\n",
                "---\ntier: C\nfamily: names\n---\n# Body\u2028Unchanged\n",
            ):
                with self.subTest(content=content):
                    original = content.encode()
                    self.assert_snapshot_comparison(original, original, unchanged=False)
                    old = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                                         input=original, check=True, capture_output=True).stdout.decode().strip()
                    _ = path.write_bytes(b"---\ntier: C\nfamily: names\n---\n# Today\n")
                    current = rules._load_rule(path, "local", repo, rules.RepoReach())  # pyright: ignore[reportPrivateUsage]
                    self.assertFalse(rules.judged_text_unchanged(old, current, compared_blob=old))

    def test_tags_header_cannot_take_ownership_of_retained_list_items(self) -> None:
        for gap in (b"", b"# comment\n", b"\n  # comment\n\n"):
            for items in (b"- '[[related]]'\n", b"- one\n" + gap + b"- '[[related]]'\n"):
                with self.subTest(gap=gap, items=items):
                    before = b"---\ntier: C\nfamily: names\nsee_also:\n" + gap + items + b"---\n# Body\n"
                    after = before.replace(b"see_also:\n", b"see_also:\ntags:\n")
                    self.assert_snapshot_comparison(before, after, unchanged=False)
                    self.assert_snapshot_comparison(after, before, unchanged=False)

    def test_admin_edits_preserve_crlf_trailing_newlines_and_block_trivia(self) -> None:
        for newline in (b"\n", b"\r\n", b"\r"):
            for trailing in (b"", newline):
                for before, after in (
                    (b"tags: [one] # old\n", b"tags: [two] # new\n"),
                    (b"date_modified: '[[2026-01-01]]' # old\n", b"date_modified: '[[2026-02-02]]' # new\n"),
                    (b"tags:\n# comment\n\n- one\n", b"tags:\n# comment\n\n- two\n"),
                ):
                    with self.subTest(newline=newline, trailing=trailing, before=before):
                        original = (b"---\ntier: C\nfamily: names\n" + before + b"---\n# Body").replace(b"\n", newline) + trailing
                        edited = original.replace(before.replace(b"\n", newline), after.replace(b"\n", newline))
                        self.assert_snapshot_comparison(original, edited, unchanged=True)

    def test_noncanonical_admin_and_other_frontmatter_changes_are_stale(self) -> None:
        changes = (
            (b"tags: one:\n", b"tags: [one]\n"),
            (b"tags: [one{two}]\n", b"tags: [one]\n"),
            (b"tags: [one}two]\n", b"tags: [one]\n"),
            (b"other: one\n", b"other: 'one'\n"),
            (b"other: one\n", b"other: one # comment\n"),
            (b"other: one\nnext: two\n", b"next: two\nother: one\n"),
            (b"input_scope: crate\n", b"input_scope: file\n"),
            (b"see_also: '[[one]]'\n", b"see_also: '[[two]]'\n"),
            (b"mode: propose\n", b"mode: check\n"),
        )
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            path = repo / "rule.md"
            for before, after in changes:
                with self.subTest(before=before, after=after):
                    original = b"---\ntier: C\nfamily: names\n" + before + b"---\n# Body\n"
                    old = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                                         input=original, check=True, capture_output=True).stdout.decode().strip()
                    _ = path.write_bytes(original.replace(before, after))
                    current = rules._load_rule(path, "local", repo, rules.RepoReach())  # pyright: ignore[reportPrivateUsage]
                    self.assertFalse(rules.judged_text_unchanged(old, current))
                    self.assertFalse(rules.judged_text_unchanged(current.blob, current, compared_blob=old))

    def test_canonical_admin_edits_preserve_identical_remaining_lines(self) -> None:
        changes = (
            (b"tags: one:\n", b"tags: one:\napproval: always\n"),
            (b"tags: [one{two}]\n", b"tags: [one{two}]\napproval: always # execution gate\n"),
            (b"tags:\n- one\n", b"tags:\n- two\n- three\n"),
            (b"tags:\n  - one\n", b"tags:\n  - two # item\n"),
            (b"tags: [one]\n", b"tags: [two, three] # comment\n"),
            (b"date_created: '[[2026-01-01]]'\n", b'date_created: "[[2026-02-02]]" # date\n'),
            (b'date_modified: "[[2026-01-01]]"\n', b"date_modified: '[[2026-02-02]]'\n"),
        )
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            path = repo / "rule.md"
            for before, after in changes:
                with self.subTest(before=before, after=after):
                    original = b"---\ntier: C\nfamily: names\n" + before + b"---\n# Body\n"
                    old = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                                         input=original, check=True, capture_output=True).stdout.decode().strip()
                    _ = path.write_bytes(original.replace(before, after))
                    current = rules._load_rule(path, "local", repo, rules.RepoReach())  # pyright: ignore[reportPrivateUsage]
                    self.assertTrue(rules.judged_text_unchanged(old, current))

    def test_scope_changes_preserve_body_bytes_but_invalidate_judgment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            path = repo / "rule.md"
            _ = path.write_bytes(b"---\ntier: C\nfamily: names\ninput_scope: crate\n---\n# Rule\nKeep bytes.\n")
            old = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", str(path)],
                                 check=True, capture_output=True, text=True).stdout.strip()
            before = rules._load_rule(path, "local", repo, rules.RepoReach())  # pyright: ignore[reportPrivateUsage]
            _ = path.write_bytes(path.read_bytes().replace(b"input_scope: crate", b"input_scope: file\napproval: always"))
            current = rules._load_rule(path, "local", repo, rules.RepoReach())  # pyright: ignore[reportPrivateUsage]
            self.assertNotEqual(current.blob, old)
            self.assertEqual(current.body_identity, before.body_identity)
            self.assertFalse(rules.judged_text_unchanged(old, current))
            self.assertTrue(rules.judged_text_unchanged(current.blob, current))

    def test_closed_grammar_validates_both_operands_even_when_equal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            path = repo / "rule.md"
            valid = b"---\ntier: C\nfamily: names\n---\n# Body\n"
            _ = path.write_bytes(valid)
            current = rules._load_rule(path, "local", repo, rules.RepoReach())  # pyright: ignore[reportPrivateUsage]
            good = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=valid,
                                  check=True, capture_output=True).stdout.decode().strip()
            for extra in (b"other: - orphan", b"other: ? orphan", b"tags: [", b"tags: 'unclosed",
                          b"tags: [one]\ntags: [two]", b"tags:\n\t- one", b"approval: never",
                          b"input_scope: invalid", b"candidates:\n  surface: whole"):
                with self.subTest(extra=extra):
                    content = valid.replace(b"---\n# Body", extra + b"\n---\n# Body")
                    bad = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=content,
                                         check=True, capture_output=True).stdout.decode().strip()
                    self.assertFalse(rules.judged_text_unchanged(bad, current))
                    self.assertFalse(rules.judged_text_unchanged(good, current, compared_blob=bad))
                    self.assertFalse(rules.judged_text_unchanged(bad, current, compared_blob=bad))
                    _ = path.write_bytes(content)
                    with self.assertRaises(rules.RuleValidationError):
                        _ = rules._load_rule(path, "local", repo, rules.RepoReach())  # pyright: ignore[reportPrivateUsage]
            self.assertFalse(rules.judged_text_unchanged("0" * 40, current, compared_blob="0" * 40))

    def test_only_administrative_metadata_preserves_judgments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            path = repo / "rule.md"
            valid = "---\ntier: C\nfamily: names\n---\n# Body\n"
            _ = path.write_text(valid)
            old = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", str(path)],
                                 check=True, capture_output=True, text=True).stdout.strip()
            changes = (("tags: [one] # comment", True), ("tags:\n- one", True),
                       ("approval: always", True),
                       ("date_created: '[[2026-01-01]]'\ndate_modified: '[[2026-01-02]]'", True),
                       ("input_scope: file", False), ('see_also: "[[binding]]"', False),
                       ("mode: propose", False), ("unknown: value", False),
                       ("pre_filter: value", False), ("candidates:\n  kind: regex\n  surface: whole", False))
            for extra, expected in changes:
                with self.subTest(extra=extra):
                    _ = path.write_text(valid.replace("---\n# Body", extra + "\n---\n# Body"))
                    current = rules._load_rule(path, "local", repo, rules.RepoReach())  # pyright: ignore[reportPrivateUsage]
                    self.assertEqual(rules.judged_text_unchanged(old, current), expected)

    def test_historical_equivalence_is_independent_of_todays_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            path = repo / "rule.md"
            pins: list[str] = []
            for extra in ("", "tags: [one]\n", "other: changed\n"):
                content = f"---\ntier: C\nfamily: names\n{extra}---\n# Old\n".encode()
                pins.append(subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=content,
                                           check=True, capture_output=True).stdout.decode().strip())
            _ = path.write_text("---\ntier: C\nfamily: names\n---\n# New\n")
            current = rules._load_rule(path, "local", repo, rules.RepoReach())  # pyright: ignore[reportPrivateUsage]
            self.assertTrue(rules.judged_text_unchanged(pins[0], current, compared_blob=pins[1]))
            self.assertFalse(rules.judged_text_unchanged(pins[0], current, compared_blob=pins[2]))

    def test_frontmatter_and_body_bytes_both_control_equivalence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            path = repo / "rule.md"
            original = b"---\ntier: C\nfamily: names\npre_filter: value\nother: value\n---\n# Body\n"
            old = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=original,
                                 check=True, capture_output=True).stdout.decode().strip()
            for content, expected in (
                (b"---\nother: 'value' # comment\nfamily: names\ntier: C\n---\n# Body\n", False),
                (original.replace(b"other: value", b"other: [value]"), False),
                (original.replace(b"# Body\n", b"# Body\r\n"), False),
                (original.replace(b"# Body", b"# Changed"), False),
                (original.replace(b"family: names", b"family: values"), False),
                (original.replace(b"tier: C", b"tier: B"), False),
                (original.replace(b"other: value", b"other: changed"), False),
            ):
                with self.subTest(content=content):
                    _ = path.write_bytes(content)
                    current = rules._load_rule(path, "local", repo, rules.RepoReach())  # pyright: ignore[reportPrivateUsage]
                    self.assertEqual(rules.judged_text_unchanged(old, current), expected)

    def test_body_edit_missing_blob_and_unterminated_frontmatter_are_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            path = repo / "rule.md"
            _ = path.write_bytes(b"---\ntier: C\nfamily: names\n---\n# Body\n")
            current = rules._load_rule(path, "local", repo, rules.RepoReach())  # pyright: ignore[reportPrivateUsage]
            for content in (b"---\ntier: C\n---\n# Changed\n", b"---\n# Body\n",
                            b"---\n---\n# Body\r\n", b"---\n---\n\xff"):
                blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=content,
                                      check=True, capture_output=True).stdout.decode().strip()
                self.assertFalse(rules.judged_text_unchanged(blob, current), repr(content))
            self.assertFalse(rules.judged_text_unchanged("0" * 40, current))

    def test_malformed_historical_frontmatter_is_stale_under_the_rule_loaders_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            path = repo / "rule.md"
            valid = b"---\ntier: C\nfamily: names\n---\n# Body\n"
            _ = path.write_bytes(valid)
            current = rules._load_rule(path, "global", repo, rules.RepoReach())  # pyright: ignore[reportPrivateUsage]
            for metadata in (b"tags: [", b"tags: 'unterminated", b"tier: unknown", b"tier: C\nfamily: wrong",
                             b"tier: C\nfamily: names\n- orphan", b"tier: C\nfamily: names\nother: [",
                             b"tier: C\nfamily: names\ncandidates:\n  kind: [",
                             b"candidates:\n  surface: whole"):
                with self.subTest(metadata=metadata):
                    content = b"---\n" + metadata + b"\n---\n# Body\n"
                    blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"], input=content,
                                          check=True, capture_output=True).stdout.decode().strip()
                    self.assertFalse(rules.judged_text_unchanged(blob, current))
                    _ = path.write_bytes(content)
                    with self.assertRaises(rules.RuleValidationError):
                        _ = rules._load_rule(path, "global", repo, rules.RepoReach())  # pyright: ignore[reportPrivateUsage]
                    _ = path.write_bytes(valid)

    def test_inline_list_comment_loads_and_keeps_judgment_current(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            path = repo / "rule.md"
            _ = path.write_text("---\ntier: C\nfamily: names\n---\n# Body\n")
            current = rules._load_rule(path, "local", repo, rules.RepoReach())  # pyright: ignore[reportPrivateUsage]
            _ = path.write_text("---\ntier: C\nfamily: names\ntags: [one] # comment\n---\n# Body\n")
            old = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", str(path)],
                                 check=True, capture_output=True, text=True).stdout.strip()
            self.assertTrue(rules.judged_text_unchanged(old, current))
            edited = rules._load_rule(path, "local", repo, rules.RepoReach())  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(edited.body_identity, current.body_identity)

    def test_index_activity_keeps_one_cat_file_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            path = repo / "rule.md"
            _ = path.write_text("---\ntier: C\nfamily: names\n---\n# Body\n")
            old = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", str(path)],
                                 check=True, capture_output=True, text=True).stdout.strip()
            _ = path.write_text("---\ntier: C\nfamily: names\napproval: always\n---\n# Body\n")
            current = rules._load_rule(path, "local", repo, rules.RepoReach())  # pyright: ignore[reportPrivateUsage]
            with mock.patch("rules.subprocess.Popen", wraps=subprocess.Popen) as popen:
                self.assertTrue(rules.judged_text_unchanged(old, current))
                _ = subprocess.run(["git", "-C", str(repo), "read-tree", "--empty"], check=True)
                self.assertTrue(rules.judged_text_unchanged(old, current))
            batch_calls = [call for call in popen.call_args_list if "cat-file" in call.args[0]]
            self.assertEqual(len(batch_calls), 1)

    def test_replaced_rule_repository_cannot_reuse_the_previous_repositories_blob_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "rules"
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            path = repo / "rule.md"
            _ = path.write_bytes(b"---\ntier: C\nfamily: names\n---\n# Body\n")
            old = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", str(path)],
                                 check=True, capture_output=True, text=True).stdout.strip()
            valid = b"---\ntier: C\nfamily: names\napproval: always\n---\n# Body\n"
            _ = path.write_bytes(valid)
            current = rules._load_rule(path, "local", repo, rules.RepoReach())  # pyright: ignore[reportPrivateUsage]
            self.assertTrue(rules.judged_text_unchanged(old, current))
            _ = repo.rename(root / "previous-repository")
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            _ = path.write_bytes(valid)
            current = rules._load_rule(path, "local", repo, rules.RepoReach())  # pyright: ignore[reportPrivateUsage]
            absent = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", old], capture_output=True)
            self.assertNotEqual(absent.returncode, 0)
            self.assertFalse(rules.judged_text_unchanged(old, current))

    def test_many_judged_blobs_share_one_cat_file_process_and_cached_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True)
            path = repo / "rule.md"
            _ = path.write_bytes(b"---\ntier: C\nfamily: names\n---\n# Body\n")
            current = rules._load_rule(path, "local", repo, rules.RepoReach())  # pyright: ignore[reportPrivateUsage]
            blobs = [subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                                    input=f"---\ntier: C\nfamily: names\ndate_modified: '[[2026-01-{index + 1:02}]]'\n---\n# Body\n".encode(),
                                    check=True, capture_output=True).stdout.decode().strip() for index in range(20)]
            with mock.patch("rules.subprocess.Popen", wraps=subprocess.Popen) as popen:
                for blob in blobs * 3:
                    self.assertTrue(rules.judged_text_unchanged(blob, current))
            self.assertEqual(popen.call_count, 1)
            self.assertIn("--batch", cast(list[str], popen.call_args.args[0]))


if __name__ == "__main__":
    _ = unittest.main()
