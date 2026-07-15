from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import final
from unittest import mock


SCRIPTS_DIR = Path(__file__).parents[2]
PRIORITIZE_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(PRIORITIZE_DIR))
from prioritize import apply_ratings, renumber, review_hash


GOALS = """# Prioritization goals

## Current goals

1. `1 - Ship Hana`
2. `2 - Find Collaborators`
3. `3 - Seek Investors`
"""

VALUES = {
    "strategic_goal": "1 - Ship Hana",
    "alignment": "⭐⭐⭐⭐",
    "impact": "⭐⭐⭐",
    "urgency": "⭐⭐",
    "effort": "⭐⭐⭐",
}


def issue_text(
    *,
    status: str = "open",
    values: dict[str, str] | None = None,
    generated: tuple[int, int] | None = None,
    body: str = "# Example\n\nEvidence.\n",
) -> str:
    lines = [
        "---",
        'project: "[[hana]]"',
        "category:",
        '  - "[[issue structure#feature|feature]]"',
        'priority: "2"',
        f"status: {status}",
    ]
    if values is not None:
        lines.extend(
            f'{key}: "{value}"' if key == "strategic_goal" else f"{key}: {value}"
            for key, value in values.items()
        )
    if generated is not None:
        lines.extend(
            [
                f"backlog_score: {generated[0]}",
                f"backlog_rank: {generated[1]}",
            ]
        )
    lines.extend(["---", body.rstrip("\n")])
    return "\n".join(lines) + "\n"


@final
class Fixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.issues = self.vault / "issues"
        self.issues.mkdir(parents=True)
        self.goals = self.vault / "prioritization goals.md"
        self.goals.write_text(GOALS, encoding="utf-8")
        self.scope = renumber.Scope(self.vault, self.issues, self.goals)
        self.state = self.root / "state"
        self.runtime = apply_ratings.Runtime(
            scope=self.scope,
            writer_lock_path=self.state / "writer.lock",
        )

    def add(self, name: str, content: str) -> Path:
        path = self.issues / name
        path.write_text(content, encoding="utf-8")
        return path

    def manifest(
        self,
        path: Path,
        proposed: dict[str, str] | None = None,
        *,
        expected_hash: str | None = None,
        evidence_paths: tuple[Path, ...] = (),
    ) -> Path:
        source = renumber._read_source(path)
        evidence_hashes: dict[str, str] = {}
        selected_evidence = (path,) + tuple(
            evidence_path for evidence_path in evidence_paths if evidence_path != path
        )
        for evidence_path in selected_evidence:
            evidence_source = renumber._read_source(evidence_path)
            if evidence_path.parent == self.issues:
                evidence_digest = review_hash.evidence_hash_for_source(
                    evidence_source
                )
            else:
                evidence_digest = evidence_source.digest
            evidence_hashes[str(evidence_path)] = evidence_digest
        record = {
            "path": str(path),
            "review_hash": expected_hash
            or review_hash.review_hash_for_source(source),
            "goals_hash": renumber._read_source(self.goals).digest,
            "evidence_hashes": evidence_hashes,
            "proposed": proposed or VALUES,
        }
        manifest = self.root / "approved.jsonl"
        manifest.write_text(json.dumps(record) + "\n", encoding="utf-8")
        return manifest

    def close(self) -> None:
        self.temporary.cleanup()


@final
class ApplyRatingsTests(unittest.TestCase):
    _fixture: Fixture | None = None

    @property
    def fixture(self) -> Fixture:
        if self._fixture is None:
            raise AssertionError("fixture is not initialized")
        return self._fixture

    def setUp(self) -> None:
        self._fixture = Fixture()

    def tearDown(self) -> None:
        fixture = self._fixture
        if fixture is not None:
            fixture.close()
        self._fixture = None

    def test_apply_inserts_approved_fields_preserves_note_and_reranks(self) -> None:
        path = self.fixture.add("issue.md", issue_text(values=None))
        original_body = "# Example\n\nEvidence.\n"
        manifest = self.fixture.manifest(path)

        plan = apply_ratings.apply_manifest(manifest, self.fixture.runtime)

        self.assertEqual(len(plan.changes), 1)
        content = path.read_text(encoding="utf-8")
        self.assertTrue(content.endswith(original_body))
        self.assertIn('project: "[[hana]]"', content)
        for field, value in VALUES.items():
            expected = (
                f'{field}: "{value}"'
                if field == "strategic_goal"
                else f"{field}: {value}"
            )
            self.assertIn(expected, content)
        self.assertRegex(content, r"backlog_score: \d+\n")
        self.assertIn("backlog_rank: 1\n", content)
        self.assertEqual(renumber.build_plan(self.fixture.scope).changes, ())

    def test_generated_changes_do_not_invalidate_approval_hash(self) -> None:
        path = self.fixture.add("issue.md", issue_text(values=VALUES, generated=(1, 9)))
        manifest = self.fixture.manifest(path)
        content = path.read_text(encoding="utf-8").replace(
            "backlog_score: 1\nbacklog_rank: 9",
            "backlog_score: 99\nbacklog_rank: 2",
        )
        path.write_text(content, encoding="utf-8")

        plan = apply_ratings.prepare_plan(manifest, self.fixture.scope)

        self.assertEqual(plan.changes, ())

    def test_apply_canonicalizes_duplicate_and_list_judgment_fields(self) -> None:
        content = issue_text(values=VALUES).replace(
            "impact: ⭐⭐⭐",
            'impact:\n  - "⭐⭐⭐"',
        ).replace(
            "urgency: ⭐⭐",
            'urgency:\n- "⭐⭐"',
        ).replace(
            "---\n# Example",
            'effort: "⭐"\n---\n# Example',
        )
        path = self.fixture.add("issue.md", content)
        manifest = self.fixture.manifest(path)

        apply_ratings.apply_manifest(manifest, self.fixture.runtime)

        updated = path.read_text(encoding="utf-8")
        self.assertEqual(updated.count("impact:"), 1)
        self.assertEqual(updated.count("urgency:"), 1)
        self.assertEqual(updated.count("effort:"), 1)
        self.assertIn("impact: ⭐⭐⭐", updated)
        self.assertIn("urgency: ⭐⭐", updated)
        self.assertIn("effort: ⭐⭐⭐", updated)
        self.assertNotIn('  - "⭐⭐⭐"', updated)
        self.assertNotIn('- "⭐⭐"', updated)
        self.assertTrue(updated.endswith("# Example\n\nEvidence.\n"))

    def test_source_evidence_change_rejects_batch(self) -> None:
        path = self.fixture.add("issue.md", issue_text(values=None))
        manifest = self.fixture.manifest(path)
        path.write_text(
            path.read_text(encoding="utf-8").replace("Evidence.", "Changed."),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(apply_ratings.ApprovalError, "evidence changed"):
            apply_ratings.prepare_plan(manifest, self.fixture.scope)

    def test_invalid_domain_and_out_of_scope_path_are_rejected(self) -> None:
        path = self.fixture.add("issue.md", issue_text(values=None))
        invalid = dict(VALUES)
        invalid["impact"] = "⭐⭐⭐⭐⭐⭐"
        manifest = self.fixture.manifest(path, invalid)
        with self.assertRaisesRegex(apply_ratings.ApprovalError, "invalid impact"):
            apply_ratings.prepare_plan(manifest, self.fixture.scope)

        record = {
            "path": str(self.fixture.vault / "outside.md"),
            "review_hash": "0" * 64,
            "goals_hash": "0" * 64,
            "evidence_hashes": {str(self.fixture.vault / "outside.md"): "0" * 64},
            "proposed": VALUES,
        }
        manifest.write_text(json.dumps(record) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(apply_ratings.ApprovalError, "outside fixed scope"):
            apply_ratings.prepare_plan(manifest, self.fixture.scope)

    def test_closed_issue_is_rejected_even_with_matching_hash(self) -> None:
        path = self.fixture.add("closed.md", issue_text(status="closed", values=None))
        manifest = self.fixture.manifest(path)

        with self.assertRaisesRegex(apply_ratings.ApprovalError, "currently open"):
            apply_ratings.prepare_plan(manifest, self.fixture.scope)

    def test_goal_note_change_rejects_batch(self) -> None:
        path = self.fixture.add("issue.md", issue_text(values=None))
        manifest = self.fixture.manifest(path)
        self.fixture.goals.write_text(
            GOALS + "\nChanged definition.\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(apply_ratings.ApprovalError, "goals changed"):
            apply_ratings.prepare_plan(manifest, self.fixture.scope)

    def test_linked_evidence_change_rejects_batch(self) -> None:
        path = self.fixture.add("issue.md", issue_text(values=None))
        evidence = self.fixture.vault / "support.md"
        evidence.write_text("# Support\n\nEvidence.\n", encoding="utf-8")
        manifest = self.fixture.manifest(path, evidence_paths=(evidence,))
        evidence.write_text("# Support\n\nChanged.\n", encoding="utf-8")

        with self.assertRaisesRegex(apply_ratings.ApprovalError, "evidence changed"):
            apply_ratings.prepare_plan(manifest, self.fixture.scope)

    def test_linked_issue_judgment_change_does_not_change_evidence_hash(self) -> None:
        path = self.fixture.add("target.md", issue_text(values=None))
        evidence = self.fixture.add(
            "evidence.md", issue_text(values=VALUES, generated=(10, 2))
        )
        manifest = self.fixture.manifest(path, evidence_paths=(evidence,))
        updated = evidence.read_text(encoding="utf-8").replace(
            "impact: ⭐⭐⭐", "impact: ⭐⭐⭐⭐"
        ).replace("backlog_score: 10", "backlog_score: 99")
        evidence.write_text(updated, encoding="utf-8")

        plan = apply_ratings.prepare_plan(manifest, self.fixture.scope)

        self.assertEqual(len(plan.approvals), 1)

    def test_failure_during_rerank_rolls_back_approved_fields(self) -> None:
        path = self.fixture.add("issue.md", issue_text(values=None))
        original = path.read_bytes()
        manifest = self.fixture.manifest(path)

        with mock.patch.object(
            apply_ratings.renumber,
            "apply_plan",
            side_effect=apply_ratings.renumber.ApplyError("injected"),
        ):
            with self.assertRaisesRegex(apply_ratings.ApprovalError, "injected"):
                apply_ratings.apply_manifest(manifest, self.fixture.runtime)

        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(
            apply_ratings.writer_lock.lock_is_held(
                self.fixture.runtime.writer_lock_path
            )
        )

    def test_membership_change_during_apply_baseline_is_rejected(self) -> None:
        path = self.fixture.add("issue.md", issue_text(values=None))
        original = path.read_bytes()
        manifest = self.fixture.manifest(path)
        initial_paths = (path,)
        changed_paths = (path, self.fixture.issues / "new.md")

        with mock.patch.object(
            apply_ratings.renumber,
            "_discover_issue_paths",
            side_effect=(initial_paths, changed_paths),
        ):
            with self.assertRaisesRegex(
                apply_ratings.ApprovalError, "baseline was captured"
            ):
                apply_ratings.apply_manifest(manifest, self.fixture.runtime)

        self.assertEqual(path.read_bytes(), original)

    def test_same_values_still_repairs_derived_state(self) -> None:
        path = self.fixture.add("issue.md", issue_text(values=VALUES))
        manifest = self.fixture.manifest(path)

        plan = apply_ratings.apply_manifest(manifest, self.fixture.runtime)

        self.assertEqual(plan.changes, ())
        self.assertIn("backlog_rank: 1", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
