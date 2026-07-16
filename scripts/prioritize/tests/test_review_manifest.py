from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, final
from unittest import mock


SCRIPTS_DIR = Path(__file__).parents[2]
PRIORITIZE_DIR = Path(__file__).parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(PRIORITIZE_DIR))
from prioritize import review_manifest


GOALS = """# prioritization goals

## Current goals

1. `1 - Ship Hana`
2. `2 - Find Collaborators`
3. `3 - Seek Investors`
"""

RUBRIC = {
    "backlog_goal": "1 - Ship Hana",
    "backlog_alignment": "⭐⭐⭐⭐",
    "backlog_impact": "⭐⭐⭐",
    "backlog_urgency": "⭐⭐",
    "backlog_effort": "⭐⭐⭐",
}


def issue_text(
    *,
    status: str = "open",
    body: str = "# Example issue\n\nEvidence.\n",
    generated: tuple[int, int] | None = None,
    extra: str = "",
) -> str:
    lines = [
        "---",
        'project: "[[hana]]"',
        "category:",
        '  - "[[issue structure#feature|feature]]"',
        f"status: {status}",
    ]
    lines.extend(f'{key}: "{value}"' for key, value in RUBRIC.items())
    if extra:
        lines.extend(extra.rstrip("\n").splitlines())
    if generated is not None:
        lines.extend(
            (
                f"backlog_score: {generated[0]}",
                f"backlog_rank: {generated[1]}",
            )
        )
    lines.extend(("---", body.rstrip("\n")))
    return "\n".join(lines) + "\n"


def read_jsonl(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed: object = json.loads(line)  # pyright: ignore[reportAny]
        if not isinstance(parsed, dict):
            raise AssertionError(f"expected an object in {path}")
        records.append(cast(dict[str, object], parsed))
    return records


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
        self.root = Path(self.temporary.name)
        self.vault = self.root / "vault"
        self.issues = self.vault / "issues"
        self.issues.mkdir(parents=True)
        self.goals = self.vault / "prioritization goals.md"
        _ = self.goals.write_text(GOALS, encoding="utf-8")
        self.session = self.root / "session"
        self.scope = review_manifest.Scope(self.vault, self.issues, self.goals)

    def add(self, name: str, content: str) -> Path:
        path = self.issues / name
        _ = path.write_text(content, encoding="utf-8")
        return path

    def close(self) -> None:
        self.temporary.cleanup()


@final
class ReviewManifestTests(unittest.TestCase):
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

    def test_inventory_contains_every_open_issue_and_routing_metadata(self) -> None:
        alpha = self.fixture.add("alpha.md", issue_text())
        _ = self.fixture.add("closed.md", issue_text(status="closed"))

        records = review_manifest.build_inventory(self.fixture.scope)

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["path"], str(alpha.resolve()))
        self.assertEqual(record["project"], ["[[hana]]"])
        self.assertEqual(
            record["category"], ["[[issue structure#feature|feature]]"]
        )
        self.assertEqual(record["linked_evidence"], {})
        self.assertEqual(record["goals"], [
            "1 - Ship Hana",
            "2 - Find Collaborators",
            "3 - Seek Investors",
        ])
        self.assertEqual(
            record["goals_hash"],
            hashlib.sha256(GOALS.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(record["current"], RUBRIC)
        self.assertEqual(record["title"], "Example issue")
        self.assertIn("Evidence.", record["body"])
        self.assertGreater(record["review_weight"], record["note_bytes"])

    def test_inventory_normalizes_goal_wikilinks_to_displayed_text(self) -> None:
        linked_goals = GOALS.replace(
            "1 - Ship Hana", "1 - Ship [[hana|Hana]]"
        )
        _ = self.fixture.goals.write_text(linked_goals, encoding="utf-8")
        _ = self.fixture.add("issue.md", issue_text())

        record = review_manifest.build_inventory(self.fixture.scope)[0]

        self.assertEqual(record["goals"][0], "1 - Ship Hana")
        self.assertEqual(record["current"]["backlog_goal"], "1 - Ship Hana")

    def test_inventory_resolves_only_safe_explicit_markdown_links(self) -> None:
        notes = self.fixture.vault / "notes"
        notes.mkdir()
        wiki = notes / "wiki support.md"
        embed = notes / "embed support.md"
        markdown = notes / "markdown support.md"
        _ = wiki.write_text("# Wiki\n", encoding="utf-8")
        _ = embed.write_text("# Embed\n", encoding="utf-8")
        _ = markdown.write_text("# Markdown\n", encoding="utf-8")
        outside = self.fixture.root / "outside.md"
        _ = outside.write_text("# Outside\n", encoding="utf-8")
        (notes / "symlink.md").symlink_to(outside)
        body = """# Linked issue

[[wiki support#Evidence|wiki alias]]
![[notes/embed support^block]]
[markdown](../notes/markdown%20support.md#Evidence)
[[missing note]]
[outside](../../outside.md)
[[notes/symlink]]
"""
        _ = self.fixture.add("issue.md", issue_text(body=body))

        record = review_manifest.build_inventory(self.fixture.scope)[0]

        self.assertEqual(
            record["linked_evidence"],
            {
                str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (wiki, embed, markdown)
            },
        )

    def test_record_hash_excludes_only_top_level_derived_fields(self) -> None:
        base = issue_text(generated=(10, 20))
        path = self.fixture.add("issue.md", base)
        first = review_manifest.build_inventory(self.fixture.scope)[0]

        changed_derived = base.replace("backlog_score: 10", "backlog_score: 999").replace(
            "backlog_rank: 20", "backlog_rank: 1"
        )
        _ = path.write_text(changed_derived, encoding="utf-8")
        second = review_manifest.build_inventory(self.fixture.scope)[0]
        self.assertEqual(first["review_hash"], second["review_hash"])
        self.assertEqual(first["note_bytes"], second["note_bytes"])
        self.assertEqual(first["review_weight"], second["review_weight"])

        changed_evidence = base.replace("Evidence.", "Different evidence.")
        _ = path.write_text(changed_evidence, encoding="utf-8")
        third = review_manifest.build_inventory(self.fixture.scope)[0]
        self.assertNotEqual(first["review_hash"], third["review_hash"])
        self.assertNotEqual(first["note_bytes"], third["note_bytes"])
        self.assertNotEqual(first["review_weight"], third["review_weight"])

        body_field = base.replace("Evidence.", "backlog_rank: 7")
        _ = path.write_text(body_field, encoding="utf-8")
        fourth = review_manifest.build_inventory(self.fixture.scope)[0]
        self.assertNotEqual(first["review_hash"], fourth["review_hash"])

    def test_yaml_string_domain_styles_are_recorded(self) -> None:
        plain = issue_text().replace(
            'backlog_impact: "⭐⭐⭐"', "backlog_impact: ⭐⭐⭐"
        )
        single_quoted = issue_text().replace(
            'backlog_impact: "⭐⭐⭐"', "backlog_impact: '⭐⭐⭐'"
        )
        _ = self.fixture.add("plain.md", plain)
        _ = self.fixture.add("single-quoted.md", single_quoted)

        records = {
            Path(record["path"]).name: record
            for record in review_manifest.build_inventory(self.fixture.scope)
        }

        self.assertEqual(records["plain.md"]["current"]["backlog_impact"], "⭐⭐⭐")
        self.assertEqual(
            records["single-quoted.md"]["current"]["backlog_impact"], "⭐⭐⭐"
        )

    def test_malformed_duplicate_and_list_domains_are_unassessed(self) -> None:
        cases = {
            "malformed.md": issue_text().replace(
                'backlog_impact: "⭐⭐⭐"', 'backlog_impact: "⭐⭐⭐'
            ),
            "duplicate.md": issue_text(extra='backlog_impact: "⭐⭐⭐⭐"'),
            "list.md": issue_text().replace(
                'backlog_impact: "⭐⭐⭐"',
                'backlog_impact:\n  - "⭐⭐⭐"',
            ),
            "indentless.md": issue_text().replace(
                'backlog_impact: "⭐⭐⭐"',
                'backlog_impact:\n- "⭐⭐⭐"',
            ),
        }
        for name, content in cases.items():
            _ = self.fixture.add(name, content)

        records = {
            Path(record["path"]).name: record
            for record in review_manifest.build_inventory(self.fixture.scope)
        }

        for name in cases:
            with self.subTest(name=name):
                self.assertIsNone(records[name]["current"]["backlog_impact"])

    def test_shards_are_deterministic_balanced_and_exact(self) -> None:
        for index, size in enumerate((10, 20, 40, 80, 160, 320, 640), start=1):
            _ = self.fixture.add(
                f"issue-{index}.md",
                issue_text(body=f"# Issue {index}\n\n" + ("x" * size)),
            )
        inventory = review_manifest.build_inventory(self.fixture.scope)

        first = review_manifest.shard_inventory(inventory, 3)
        second = review_manifest.shard_inventory(inventory, 3)

        self.assertEqual(first, second)
        review_manifest.verify_shard_union(inventory, first)
        paths = [record["path"] for shard in first for record in shard]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(set(paths), {record["path"] for record in inventory})
        loads = [sum(record["review_weight"] for record in shard) for shard in first]
        self.assertLessEqual(max(loads) - min(loads), max(
            record["review_weight"] for record in inventory
        ))

    def test_cli_writes_inventory_and_default_four_shards_atomically(self) -> None:
        _ = self.fixture.add("bravo.md", issue_text(body="# Bravo\n"))
        _ = self.fixture.add("alpha.md", issue_text(body="# Alpha\n"))
        output = io.StringIO()

        with mock.patch.object(
            review_manifest, "PRODUCTION_SCOPE", self.fixture.scope
        ):
            with contextlib.redirect_stdout(output):
                result = review_manifest.main(
                    ["--session-dir", str(self.fixture.session)]
                )

        self.assertEqual(result, 0)
        expected = [self.fixture.session / "inventory.jsonl"] + [
            self.fixture.session / f"shard_{index}.jsonl" for index in range(1, 3)
        ]
        self.assertTrue(all(path.is_file() for path in expected))
        inventory = read_jsonl(expected[0])
        shards = [read_jsonl(path) for path in expected[1:]]
        inventory_paths = [cast(str, record["path"]) for record in inventory]
        shard_paths = [
            cast(str, record["path"])
            for shard in shards
            for record in shard
        ]
        self.assertEqual(shard_paths, list(dict.fromkeys(shard_paths)))
        self.assertEqual(set(shard_paths), set(inventory_paths))
        self.assertIn("2 open issues across 2 shards", output.getvalue())
        self.assertEqual(list(self.fixture.session.glob(".*.prioritize-*")), [])

    def test_cli_emits_no_empty_shards_for_an_empty_inventory(self) -> None:
        with mock.patch.object(
            review_manifest, "PRODUCTION_SCOPE", self.fixture.scope
        ):
            result = review_manifest.main(
                ["--session-dir", str(self.fixture.session)]
            )

        self.assertEqual(result, 0)
        self.assertTrue((self.fixture.session / "inventory.jsonl").is_file())
        self.assertEqual(list(self.fixture.session.glob("shard_*.jsonl")), [])

    def test_session_output_inside_vault_is_rejected(self) -> None:
        _ = self.fixture.add("issue.md", issue_text())
        inventory = review_manifest.build_inventory(self.fixture.scope)
        shards = review_manifest.shard_inventory(inventory, 1)

        with self.assertRaises(review_manifest.ManifestError):
            _ = review_manifest.write_outputs(
                self.fixture.vault / "session",
                inventory,
                shards,
                self.fixture.scope,
            )

    def test_duplicate_shard_record_is_rejected(self) -> None:
        _ = self.fixture.add("issue.md", issue_text())
        inventory = review_manifest.build_inventory(self.fixture.scope)

        with self.assertRaises(review_manifest.ManifestError):
            review_manifest.verify_shard_union(
                inventory, ((inventory[0],), (inventory[0],))
            )

    def test_inventory_rejects_membership_change_during_discovery(self) -> None:
        issue = self.fixture.add("issue.md", issue_text())
        initial_paths = (issue,)
        changed_paths = (issue, self.fixture.issues / "new.md")

        with mock.patch.object(
            review_manifest,
            "_issue_paths",
            side_effect=(initial_paths, changed_paths),
        ):
            with self.assertRaisesRegex(
                review_manifest.ManifestError, "issue files changed"
            ):
                _ = review_manifest.build_inventory(self.fixture.scope)


if __name__ == "__main__":
    _ = unittest.main()
