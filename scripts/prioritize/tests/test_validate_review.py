from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import cast, final
from unittest import mock


SCRIPTS_DIR = Path(__file__).parents[2]
PRIORITIZE_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(PRIORITIZE_DIR))
from prioritize import renumber, review_hash, review_manifest, validate_review


GOALS = """# prioritization goals

## Current goals

1. `1 - Ship Hana`
2. `2 - Find Collaborators`
3. `3 - Seek Investors`
"""

RUBRIC: dict[str, str] = {
    "strategic_goal": "1 - Ship Hana",
    "alignment": "⭐⭐⭐⭐",
    "impact": "⭐⭐⭐",
    "urgency": "⭐⭐",
    "leverage": "⭐⭐⭐",
    "confidence": "⭐⭐",
    "effort": "⭐⭐⭐",
}


def issue_text(
    *,
    rubric: dict[str, str] | None = None,
    body: str = "# Example issue\n\nEvidence.\n",
) -> str:
    values = RUBRIC if rubric is None else rubric
    lines = [
        "---",
        'project: "[[hana]]"',
        'category: "[[issue structure#feature|feature]]"',
        "status: open",
    ]
    lines.extend(f'{key}: "{value}"' for key, value in values.items())
    lines.extend(("---", body.rstrip("\n")))
    return "\n".join(lines) + "\n"


def write_jsonl(path: Path, rows: Sequence[object]) -> None:
    content = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    )
    _ = path.write_text(content, encoding="utf-8")


def finding(
    record: review_manifest.ManifestRecord,
    *,
    verdict: str = "unchanged",
    proposed: dict[str, str] | None = None,
    evidence: Sequence[dict[str, str]] | None = None,
) -> dict[str, object]:
    selected_evidence = (
        [{"path": record["path"], "detail": "Issue note."}]
        if evidence is None
        else list(evidence)
    )
    return {
        "path": record["path"],
        "review_hash": record["review_hash"],
        "goals_hash": record["goals_hash"],
        "current": dict(record["current"]),
        "verdict": verdict,
        "proposed": {} if proposed is None else proposed,
        "evidence": selected_evidence,
        "reason": "  Evidence supports this judgment.  ",
    }


@final
class VaultFixture:
    temporary: tempfile.TemporaryDirectory[str]
    root: Path
    vault: Path
    issues: Path
    goals: Path
    session: Path
    scope: review_manifest.Scope

    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.vault = self.root / "vault"
        self.issues = self.vault / "issues"
        self.issues.mkdir(parents=True)
        self.goals = self.vault / "prioritization goals.md"
        _ = self.goals.write_text(GOALS, encoding="utf-8")
        self.session = self.root / "session"
        self.session.mkdir()
        self.scope = review_manifest.Scope(self.vault, self.issues, self.goals)

    def add_issue(self, name: str, content: str | None = None) -> Path:
        path = self.issues / name
        _ = path.write_text(
            issue_text() if content is None else content,
            encoding="utf-8",
        )
        return path

    def add_note(self, name: str, content: str = "# Supporting note\n") -> Path:
        path = self.vault / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content, encoding="utf-8")
        return path

    def manifest(
        self,
        records: Sequence[review_manifest.ManifestRecord] | None = None,
    ) -> tuple[Path, tuple[review_manifest.ManifestRecord, ...]]:
        inventory = review_manifest.build_inventory(self.scope)
        selected = inventory if records is None else tuple(records)
        path = self.session / "manifest.jsonl"
        write_jsonl(path, selected)
        return path, tuple(selected)

    def findings(self, rows: Sequence[object], name: str = "findings.jsonl") -> Path:
        path = self.session / name
        write_jsonl(path, rows)
        return path

    def close(self) -> None:
        self.temporary.cleanup()


@final
class ValidateReviewTests(unittest.TestCase):
    _fixture: VaultFixture | None = None

    @property
    def fixture(self) -> VaultFixture:
        if self._fixture is None:
            raise AssertionError("fixture is not initialized")
        return self._fixture

    def setUp(self) -> None:  # pyright: ignore[reportImplicitOverride]
        self._fixture = VaultFixture()

    def tearDown(self) -> None:  # pyright: ignore[reportImplicitOverride]
        fixture = self._fixture
        if fixture is not None:
            fixture.close()
        self._fixture = None

    def test_reviewer_normalizes_complete_ordered_findings_and_hashes_evidence(
        self,
    ) -> None:
        alpha = self.fixture.add_issue(
            "alpha.md",
            issue_text(
                body="# Alpha issue\n\nSupporting context: [[notes/support]].\n"
            ),
        )
        _ = self.fixture.add_issue("bravo.md", issue_text(body="# Bravo\n"))
        support = self.fixture.add_note("notes/support.md", "# Stable support\n")
        manifest_path, inventory = self.fixture.manifest()
        rows = [
            finding(
                inventory[0],
                evidence=(
                    {"path": str(alpha.resolve()), "detail": "  Issue body.  "},
                    {"path": str(self.fixture.goals.resolve()), "detail": "Goal list."},
                    {"path": str(support.resolve()), "detail": "Supporting note."},
                ),
            ),
            finding(
                inventory[1],
                verdict="proposed",
                proposed={**RUBRIC, "impact": "⭐⭐⭐⭐"},
            ),
        ]
        findings_path = self.fixture.findings(rows)

        normalized = validate_review.validate(
            "reviewer", manifest_path, findings_path, self.fixture.scope
        )

        self.assertEqual(
            [row["path"] for row in normalized],
            [record["path"] for record in inventory],
        )
        self.assertEqual(normalized[0]["reason"], "Evidence supports this judgment.")
        self.assertEqual(normalized[0]["evidence"][0]["detail"], "Issue body.")
        alpha_source = renumber._read_source(  # pyright: ignore[reportPrivateUsage]
            alpha.resolve()
        )
        self.assertEqual(
            normalized[0]["evidence_hashes"][str(alpha.resolve())],
            review_hash.evidence_hash_for_source(alpha_source),
        )
        self.assertEqual(
            normalized[0]["evidence_hashes"][str(self.fixture.goals.resolve())],
            hashlib.sha256(GOALS.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            normalized[0]["evidence_hashes"][str(support.resolve())],
            hashlib.sha256(b"# Stable support\n").hexdigest(),
        )

        output = self.fixture.session / "normalized.jsonl"
        written = validate_review.write_output(output, normalized, self.fixture.scope)
        self.assertEqual(written, output.resolve())
        self.assertEqual(len(output.read_text(encoding="utf-8").splitlines()), 2)
        self.assertEqual(list(self.fixture.session.glob(".*.prioritize-*")), [])

    def test_reviewer_requires_exact_manifest_coverage_and_order(self) -> None:
        _ = self.fixture.add_issue("alpha.md")
        _ = self.fixture.add_issue("bravo.md")
        manifest_path, inventory = self.fixture.manifest()
        cases: tuple[tuple[str, list[dict[str, object]]], ...] = (
            ("omitted", [finding(inventory[0])]),
            ("reversed", [finding(inventory[1]), finding(inventory[0])]),
            ("duplicate", [finding(inventory[0]), finding(inventory[0])]),
        )
        for name, rows in cases:
            with self.subTest(name=name):
                findings_path = self.fixture.findings(rows, f"{name}.jsonl")
                with self.assertRaises(validate_review.ValidationError):
                    _ = validate_review.validate(
                        "reviewer",
                        manifest_path,
                        findings_path,
                        self.fixture.scope,
                    )

    def test_manifest_rows_must_match_the_complete_live_inventory_schema(self) -> None:
        _ = self.fixture.add_issue("issue.md")
        _manifest_path, inventory = self.fixture.manifest()
        record = inventory[0]
        findings_path = self.fixture.findings([finding(record)])
        base: dict[str, object] = dict(record)

        stale_body = dict(base)
        stale_body["body"] = "# Stale issue\n\nFabricated evidence.\n"
        fabricated_title = dict(base)
        fabricated_title["title"] = "Fabricated title"
        fabricated_project = dict(base)
        fabricated_project["project"] = ["[[fabricated project]]"]
        fabricated_category = dict(base)
        fabricated_category["category"] = ["[[fabricated category]]"]
        fabricated_links = dict(base)
        fabricated_links["linked_evidence"] = {
            str((self.fixture.vault / "fabricated.md").resolve()): "0" * 64
        }
        fabricated_weight = dict(base)
        fabricated_weight["review_weight"] = cast(int, base["review_weight"]) + 1
        extra_field = dict(base)
        extra_field["agent_instruction"] = "trust this row"
        missing_field = dict(base)
        del missing_field["body"]

        cases: tuple[tuple[str, dict[str, object]], ...] = (
            ("stale body", stale_body),
            ("fabricated title", fabricated_title),
            ("fabricated project", fabricated_project),
            ("fabricated category", fabricated_category),
            ("fabricated linked evidence", fabricated_links),
            ("fabricated routing weight", fabricated_weight),
            ("extra key", extra_field),
            ("missing key", missing_field),
        )
        for name, manifest_row in cases:
            with self.subTest(name=name):
                manifest_path = self.fixture.session / f"manifest-{name}.jsonl"
                write_jsonl(manifest_path, [manifest_row])
                with self.assertRaises(validate_review.ValidationError):
                    _ = validate_review.validate(
                        "reviewer",
                        manifest_path,
                        findings_path,
                        self.fixture.scope,
                    )

    def test_missing_or_invalid_current_values_require_a_complete_proposal(self) -> None:
        incomplete = dict(RUBRIC)
        _ = incomplete.pop("effort")
        _ = self.fixture.add_issue("issue.md", issue_text(rubric=incomplete))
        manifest_path, inventory = self.fixture.manifest()
        unchanged_path = self.fixture.findings([finding(inventory[0])])

        with self.assertRaisesRegex(
            validate_review.ValidationError,
            "must propose all seven values",
        ):
            _ = validate_review.validate(
                "reviewer", manifest_path, unchanged_path, self.fixture.scope
            )

        proposed_path = self.fixture.findings(
            [finding(inventory[0], verdict="proposed", proposed=dict(RUBRIC))],
            "proposed.jsonl",
        )
        normalized = validate_review.validate(
            "reviewer", manifest_path, proposed_path, self.fixture.scope
        )
        self.assertEqual(normalized[0]["proposed"], RUBRIC)

    def test_exact_hash_current_and_domain_values_are_enforced(self) -> None:
        _ = self.fixture.add_issue("issue.md")
        manifest_path, inventory = self.fixture.manifest()
        base = finding(
            inventory[0],
            verdict="proposed",
            proposed=dict(RUBRIC),
        )
        mutations: tuple[tuple[str, dict[str, object]], ...] = (
            ("review hash", {**base, "review_hash": "0" * 64}),
            ("goals hash", {**base, "goals_hash": "0" * 64}),
            (
                "current",
                {
                    **base,
                    "current": {**cast(dict[str, object], base["current"]), "effort": "⭐"},
                },
            ),
            (
                "goal",
                {
                    **base,
                    "proposed": {**RUBRIC, "strategic_goal": "1 - Invented"},
                },
            ),
            (
                "domain",
                {**base, "proposed": {**RUBRIC, "effort": "⭐⭐⭐⭐⭐⭐"}},
            ),
        )
        for name, row in mutations:
            with self.subTest(name=name):
                findings_path = self.fixture.findings([row], f"{name}.jsonl")
                with self.assertRaises(validate_review.ValidationError):
                    _ = validate_review.validate(
                        "reviewer",
                        manifest_path,
                        findings_path,
                        self.fixture.scope,
                    )

    def test_calibrator_accepts_only_unique_proposed_manifest_subsets(self) -> None:
        _ = self.fixture.add_issue("alpha.md")
        _ = self.fixture.add_issue("bravo.md")
        manifest_path, inventory = self.fixture.manifest()
        reversed_rows = [
            finding(inventory[1], verdict="proposed", proposed=dict(RUBRIC)),
            finding(inventory[0], verdict="proposed", proposed=dict(RUBRIC)),
        ]
        findings_path = self.fixture.findings(reversed_rows)

        normalized = validate_review.validate(
            "calibrator", manifest_path, findings_path, self.fixture.scope
        )

        self.assertEqual(
            [row["path"] for row in normalized],
            [record["path"] for record in inventory],
        )

        cross_inventory = self.fixture.findings(
            [
                finding(
                    inventory[0],
                    verdict="proposed",
                    proposed=dict(RUBRIC),
                    evidence=(
                        {
                            "path": inventory[1]["path"],
                            "detail": "Comparable inventory issue.",
                        },
                    ),
                )
            ],
            "cross-inventory.jsonl",
        )
        cross_normalized = validate_review.validate(
            "calibrator", manifest_path, cross_inventory, self.fixture.scope
        )
        self.assertEqual(
            set(cross_normalized[0]["evidence_hashes"]),
            {inventory[1]["path"]},
        )

        arbitrary = self.fixture.add_note("notes/arbitrary.md")
        arbitrary_findings = self.fixture.findings(
            [
                finding(
                    inventory[0],
                    verdict="proposed",
                    proposed=dict(RUBRIC),
                    evidence=(
                        {"path": str(arbitrary), "detail": "Not inventory evidence."},
                    ),
                )
            ],
            "calibrator-arbitrary.jsonl",
        )
        with self.assertRaisesRegex(
            validate_review.ValidationError, "calibrator allowlist"
        ):
            _ = validate_review.validate(
                "calibrator",
                manifest_path,
                arbitrary_findings,
                self.fixture.scope,
            )

        unchanged = self.fixture.findings([finding(inventory[0])], "unchanged.jsonl")
        with self.assertRaises(validate_review.ValidationError):
            _ = validate_review.validate(
                "calibrator", manifest_path, unchanged, self.fixture.scope
            )

        duplicate = self.fixture.findings(
            [reversed_rows[0], reversed_rows[0]], "duplicate.jsonl"
        )
        with self.assertRaises(validate_review.ValidationError):
            _ = validate_review.validate(
                "calibrator", manifest_path, duplicate, self.fixture.scope
            )

        shard_path = self.fixture.session / "shard.jsonl"
        write_jsonl(shard_path, [inventory[0]])
        outside = self.fixture.findings([reversed_rows[0]], "outside.jsonl")
        with self.assertRaisesRegex(
            validate_review.ValidationError, "complete live inventory"
        ):
            _ = validate_review.validate(
                "calibrator", shard_path, outside, self.fixture.scope
            )

        complete = self.fixture.findings(
            [{"status": "complete", "amendments": 0}], "complete.jsonl"
        )
        self.assertEqual(
            validate_review.validate(
                "calibrator", manifest_path, complete, self.fixture.scope
            ),
            (),
        )

        empty = self.fixture.findings([], "empty.jsonl")
        with self.assertRaisesRegex(
            validate_review.ValidationError, "completion object"
        ):
            _ = validate_review.validate(
                "calibrator", manifest_path, empty, self.fixture.scope
            )

    def test_evidence_must_be_unique_absolute_regular_vault_notes(self) -> None:
        _ = self.fixture.add_issue("issue.md")
        outside = self.fixture.root / "outside.md"
        _ = outside.write_text("# Outside\n", encoding="utf-8")
        uppercase = self.fixture.add_note("notes/uppercase.MD")
        arbitrary = self.fixture.add_note("notes/arbitrary.md")
        manifest_path, inventory = self.fixture.manifest()
        issue_path = inventory[0]["path"]
        cases: tuple[tuple[str, Sequence[dict[str, str]]], ...] = (
            ("empty", ()),
            ("relative", ({"path": "notes/context.md", "detail": "Context."},)),
            ("outside", ({"path": str(outside), "detail": "Context."},)),
            ("uppercase suffix", ({"path": str(uppercase), "detail": "Context."},)),
            (
                "unlinked vault note",
                ({"path": str(arbitrary), "detail": "Not explicitly linked."},),
            ),
            (
                "missing",
                ({"path": str(self.fixture.vault / "missing.md"), "detail": "Context."},),
            ),
            (
                "duplicate",
                (
                    {"path": issue_path, "detail": "One."},
                    {"path": issue_path, "detail": "Two."},
                ),
            ),
            ("empty detail", ({"path": issue_path, "detail": "  "},)),
        )
        for name, evidence in cases:
            with self.subTest(name=name):
                findings_path = self.fixture.findings(
                    [finding(inventory[0], evidence=evidence)],
                    f"evidence-{name}.jsonl",
                )
                with self.assertRaises(validate_review.ValidationError):
                    _ = validate_review.validate(
                        "reviewer",
                        manifest_path,
                        findings_path,
                        self.fixture.scope,
                    )

        symlink = self.fixture.vault / "linked.md"
        symlink.symlink_to(Path(issue_path))
        symlink_findings = self.fixture.findings(
            [
                finding(
                    inventory[0],
                    evidence=({"path": str(symlink), "detail": "Linked."},),
                )
            ],
            "evidence-symlink.jsonl",
        )
        with self.assertRaises(validate_review.ValidationError):
            _ = validate_review.validate(
                "reviewer", manifest_path, symlink_findings, self.fixture.scope
            )

    def test_manifest_source_changes_and_markdown_wrappers_are_rejected(self) -> None:
        issue = self.fixture.add_issue("issue.md")
        manifest_path, inventory = self.fixture.manifest()
        findings_path = self.fixture.findings([finding(inventory[0])])
        _ = issue.write_text(
            issue_text(body="# Changed issue\n"),
            encoding="utf-8",
        )
        with self.assertRaises(validate_review.ValidationError):
            _ = validate_review.validate(
                "reviewer", manifest_path, findings_path, self.fixture.scope
            )

        _ = issue.write_text(issue_text(), encoding="utf-8")
        manifest_path, inventory = self.fixture.manifest()
        wrapped = self.fixture.session / "wrapped.jsonl"
        payload = json.dumps(finding(inventory[0]), separators=(",", ":"))
        _ = wrapped.write_text(
            f"```json\n{payload}\n```\n",
            encoding="utf-8",
        )
        with self.assertRaises(validate_review.ValidationError):
            _ = validate_review.validate(
                "reviewer", manifest_path, wrapped, self.fixture.scope
            )

    def test_linked_evidence_change_after_manifest_discovery_is_rejected(self) -> None:
        support = self.fixture.add_note("notes/support.md", "# Original support\n")
        issue = self.fixture.add_issue(
            "issue.md",
            issue_text(body="# Linked issue\n\nSee [[notes/support]].\n"),
        )
        manifest_path, inventory = self.fixture.manifest()
        findings_path = self.fixture.findings(
            [
                finding(
                    inventory[0],
                    evidence=(
                        {"path": str(issue), "detail": "Issue note."},
                        {"path": str(support), "detail": "Supporting note."},
                    ),
                )
            ]
        )
        _ = support.write_text("# Changed support\n", encoding="utf-8")

        with self.assertRaisesRegex(
            validate_review.ValidationError, "linked_evidence changed"
        ):
            _ = validate_review.validate(
                "reviewer",
                manifest_path,
                findings_path,
                self.fixture.scope,
            )

    def test_derived_only_changes_keep_manifest_current_but_evidence_does_not(
        self,
    ) -> None:
        original = issue_text().replace(
            "---\n# Example issue",
            "backlog_score: 10\nbacklog_rank: 20\n---\n# Example issue",
        )
        issue = self.fixture.add_issue("issue.md", original)
        manifest_path, inventory = self.fixture.manifest()
        findings_path = self.fixture.findings([finding(inventory[0])])

        changed_derived = original.replace(
            "backlog_score: 10", "backlog_score: 999"
        ).replace("backlog_rank: 20", "backlog_rank: 1")
        _ = issue.write_text(changed_derived, encoding="utf-8")

        normalized = validate_review.validate(
            "reviewer", manifest_path, findings_path, self.fixture.scope
        )
        self.assertEqual(len(normalized), 1)

        changed_evidence = changed_derived.replace("Evidence.", "Changed evidence.")
        _ = issue.write_text(changed_evidence, encoding="utf-8")
        with self.assertRaises(validate_review.ValidationError):
            _ = validate_review.validate(
                "reviewer", manifest_path, findings_path, self.fixture.scope
            )

    def test_cli_mode_writes_normalized_jsonl_outside_vault(self) -> None:
        _ = self.fixture.add_issue("issue.md")
        manifest_path, inventory = self.fixture.manifest()
        findings_path = self.fixture.findings([finding(inventory[0])])
        output_path = self.fixture.session / "cli-output.jsonl"
        stdout = io.StringIO()

        with mock.patch.object(
            validate_review, "PRODUCTION_SCOPE", self.fixture.scope
        ):
            with contextlib.redirect_stdout(stdout):
                result = validate_review.main(
                    [
                        "reviewer",
                        "--manifest",
                        str(manifest_path),
                        "--findings",
                        str(findings_path),
                        "--output",
                        str(output_path),
                    ]
                )

        self.assertEqual(result, 0)
        self.assertTrue(output_path.is_file())
        self.assertIn("validated 1 reviewer findings", stdout.getvalue())

        with self.assertRaises(validate_review.ValidationError):
            _ = validate_review.write_output(
                self.fixture.vault / "forbidden.jsonl",
                (),
                self.fixture.scope,
            )


if __name__ == "__main__":
    _ = unittest.main()
