#!/usr/bin/env python3
"""Specification tests for frozen backfill ledgers, using only temporary git repos."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
import fcntl
from io import StringIO
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import NamedTuple, cast, final, override
import unittest
from unittest.mock import patch

from feature_log import state_dir
import inventory
from inventory import (
    AfterRef,
    BackfillSourceRecord,
    ChangelogSource,
    DocumentSource,
    EntireHistory,
    HistorySource,
    ProjectSources,
    ProposalCounts,
    SourceTable,
    TagSource,
)


PHASE_PATH = "docs/plans/widgets.md"
ASBUILT_PATH = "docs/widgets/as-built/current.md"
MIXED_PATH = "docs/widgets/as-built/production.md"
CHANGELOG_PATH = "crates/widget/CHANGELOG.md"

PHASE_TEXT = """# Widget production

## Work orders

### Phase 1 — Controls
The plan for controls.

#### As-built
Controls now respond to keyboard input.
- Focus follows the selected control.

#### Verification
The control tests pass.

### Phase 2 — Colors
The plan for colors.

#### As-built
The selected color is visible beside the control.

## Future work
An unrelated production section.
"""

ASBUILT_TEXT = """# Current widget surface

Keyboard controls and their colors are visible in the widget panel.
"""

MIXED_TEXT = """# Mixed production document

### Phase 3 — Navigation

#### As-built
Navigation opens the selected widget.
"""

CHANGELOG_TEXT = """# Changelog

## [Unreleased]
- Preview the selected widget.
  - This nested detail is not a separate bullet.

## [1.2.0] - 2026-10-01

### Added
- Navigate between widgets.
- Display each widget's color.

## Maintenance
- This is outside a version section.

## [1.1.0]
- Select a widget from the list.
"""


class CliResult(NamedTuple):
    status: int
    stdout: str
    stderr: str


@final
class TempRepository:
    """A test-owned checkout; every git command names its temporary path."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.mkdir(parents=True)
        _ = self.git("init", "-b", "main")

    def git(self, *arguments: str, date: str = "2026-10-01") -> str:
        environment = os.environ.copy()
        timestamp = date + "T12:00:00+00:00"
        environment.update(
            GIT_AUTHOR_DATE=timestamp,
            GIT_COMMITTER_DATE=timestamp,
        )
        result = subprocess.run(
            ["git", "-C", str(self.path), *arguments],
            check=True,
            text=True,
            capture_output=True,
            env=environment,
        )
        return result.stdout.strip()

    def write(self, path: str, contents: str) -> None:
        destination = self.path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        _ = destination.write_text(contents, encoding="utf-8")

    def commit(self, subject: str, *, date: str = "2026-10-01") -> str:
        _ = self.git("add", "--all")
        _ = self.git("commit", "--allow-empty", "-m", subject, date=date)
        return self.git("rev-parse", "HEAD")

    def merge(self, branch: str, subject: str, *, date: str) -> str:
        _ = self.git("merge", "--no-ff", branch, "-m", subject, date=date)
        return self.git("rev-parse", "HEAD")


class InventoryTests(unittest.TestCase):
    root: Path = Path()
    vault: Path = Path()
    state: Path = Path()
    repositories: dict[str, TempRepository] = {}
    base_sha: str = ""
    docs_sha: str = ""
    topic_sha: str = ""
    main_sha: str = ""
    merge_sha: str = ""
    source_table: SourceTable = {}

    @property
    def repo(self) -> TempRepository:
        return self.repositories["primary"]

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.vault = self.root / "vault"
        state_home = self.root / "state-home"
        self.state = state_home / "claude/feature-log"
        git_config = self.root / "empty-gitconfig"
        _ = git_config.write_text("", encoding="utf-8")
        git_template = self.root / "empty-git-template"
        git_template.mkdir()
        _environment = cast(
            dict[str, str],
            self.enterContext(
                patch.dict(
                    os.environ,
                    {
                        "FEATURE_LOG_VAULT": str(self.vault),
                        "XDG_STATE_HOME": str(state_home),
                        "GIT_CONFIG_GLOBAL": str(git_config),
                        "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_TEMPLATE_DIR": str(git_template),
                        "GIT_AUTHOR_NAME": "Inventory Test",
                        "GIT_AUTHOR_EMAIL": "inventory@example.invalid",
                        "GIT_COMMITTER_NAME": "Inventory Test",
                        "GIT_COMMITTER_EMAIL": "inventory@example.invalid",
                        "GIT_AUTHOR_DATE": "2026-10-01T12:00:00+00:00",
                        "GIT_COMMITTER_DATE": "2026-10-01T12:00:00+00:00",
                    },
                )
            ),
        )
        self.repositories = {"primary": TempRepository(self.root / "repo")}
        self.repo.write(PHASE_PATH, PHASE_TEXT)
        self.repo.write(ASBUILT_PATH, ASBUILT_TEXT)
        self.repo.write(MIXED_PATH, MIXED_TEXT)
        self.repo.write(CHANGELOG_PATH, CHANGELOG_TEXT)
        self.repo.write("docs/guides/internal.md", "# Internal guide\nNo as-built block.\n")
        self.repo.write("docs/widgets/as-built/raw.txt", "# Not a markdown record\n")
        self.base_sha = self.repo.commit("Initial recorded sources")
        _ = self.repo.git("update-ref", "refs/remotes/origin/main", self.base_sha)
        self.repo.write("docs/guides/internal.md", "# Internal guide\nEdited guide.\n")
        self.docs_sha = self.repo.commit("Revise internal guide", date="2026-10-02")
        _ = self.repo.git("switch", "-c", "topic")
        self.repo.write("widget.txt", "Keyboard navigation is available.\n")
        self.topic_sha = self.repo.commit(
            "Add keyboard navigation\n\nKeyboard users can move between widgets.",
            date="2026-10-03",
        )
        _ = self.repo.git("switch", "main")
        self.repo.write("main.txt", "Widget colors are visible.\n")
        self.main_sha = self.repo.commit("Display widget colors", date="2026-10-04")
        self.merge_sha = self.repo.merge(
            "topic", "Accept keyboard navigation", date="2026-10-05"
        )
        _ = self.repo.git("tag", "v1.0.0", self.base_sha)
        _ = self.repo.git(
            "tag", "-a", "v1.2.0", "-m", "Released widget navigation", date="2026-10-06"
        )
        _ = self.repo.git("switch", "-c", "unmerged", self.base_sha)
        self.repo.write("unmerged.txt", "This work has not been accepted.\n")
        _ = self.repo.commit("Unaccepted work", date="2026-10-07")
        _ = self.repo.git("tag", "unmerged-tag")
        _ = self.repo.git("switch", "main")
        self.source_table = self.temp_sources()

    def temp_sources(self) -> SourceTable:
        repo = str(self.repo.path)
        counts: ProposalCounts = {
            "structured": {"phase": 200, "changelog": 755, "tag": 30},
            "commits": {"commit": 2156, "merge": 76},
        }
        return {
            "hana": ProjectSources(
                (
                    DocumentSource(repo, "main"),
                    HistorySource(repo, "main", EntireHistory(), ("merge",)),
                ),
                {},
            ),
            "natedev": ProjectSources(
                (
                    TagSource(repo, "main", "commits"),
                    HistorySource(repo, "main", EntireHistory(), ("commit", "merge")),
                ),
                {},
            ),
            "cargo-liner": ProjectSources(
                (
                    DocumentSource(repo, "main"),
                    ChangelogSource(repo, "main", ("crates/*/CHANGELOG.md",)),
                    TagSource(repo, "main", "structured"),
                    HistorySource(repo, "main", EntireHistory(), ("commit", "merge")),
                ),
                counts,
            ),
            "bevy_brp": ProjectSources(
                (
                    DocumentSource(repo, "main"),
                    ChangelogSource(repo, "main", ("mcp/CHANGELOG.md", "mcp_macros/CHANGELOG.md", "extras/CHANGELOG.md")),
                    TagSource(repo, "main", "structured"),
                    HistorySource(repo, "main", EntireHistory(), ("commit", "merge")),
                ),
                {},
            ),
            "obsidian_knife": ProjectSources(
                (HistorySource(repo, "main", EntireHistory(), ("commit", "merge")),),
                {},
            ),
            "nateroids": ProjectSources(
                (
                    DocumentSource(repo, "main"),
                    HistorySource(repo, "main", EntireHistory(), ("commit", "merge")),
                ),
                {},
            ),
        }

    def cli(self, arguments: list[str]) -> CliResult:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                status = inventory.main(arguments, sources=self.source_table)
            except SystemExit as error:
                if not isinstance(error.code, int):
                    raise
                status = error.code
        return CliResult(status, stdout.getvalue(), stderr.getvalue())

    def ledger_path(self, project: str = "cargo-liner") -> Path:
        return self.state / "backfill" / f"{project}.jsonl"

    def records(self, project: str = "cargo-liner") -> list[BackfillSourceRecord]:
        return [
            cast(BackfillSourceRecord, json.loads(line))
            for line in self.ledger_path(project).read_text(encoding="utf-8").splitlines()
        ]

    def collect(self, project: str = "cargo-liner") -> CliResult:
        result = self.cli(["collect", "--project", project])
        self.assertEqual(result.status, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result

    def find_record(self, kind: str, title: str) -> BackfillSourceRecord:
        matches = [
            record for record in self.records()
            if record["kind"] == kind and record["title"] == title
        ]
        self.assertEqual(len(matches), 1, (kind, title, matches))
        return matches[0]

    def record_after(self, identifier: str) -> BackfillSourceRecord:
        return next(record for record in self.records() if record["id"] == identifier)

    def list_ids(self, result: CliResult) -> list[str]:
        self.assertEqual(result.status, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return [line.split(" | ", 1)[0] for line in result.stdout.splitlines() if line]

    def note(self, title: str, *, project: str = "cargo-liner") -> None:
        destination = self.vault / "feature-log" / project / f"{title}.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        _ = destination.write_text(f"# {title}\n", encoding="utf-8")

    def assert_refusal(self, arguments: list[str], *details: str) -> None:
        original = self.ledger_path().read_bytes()
        result = self.cli(arguments)
        self.assertEqual(result.status, 2, result)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertTrue(result.stderr.startswith("inventory: "), result.stderr)
        for detail in details:
            self.assertIn(detail, result.stderr)
        self.assertEqual(self.ledger_path().read_bytes(), original)

    def test_collect_records_every_kind_with_exact_source_identity(self) -> None:
        _ = self.collect()
        records = self.records()
        self.assertEqual(len(records), 15)
        self.assertEqual(len({record["id"] for record in records}), len(records))
        for record in records:
            with self.subTest(identifier=record["id"]):
                self.assertEqual(record["project"], "cargo-liner")
                self.assertEqual(record["repo"], str(self.repo.path))
                self.assertEqual(record["ref"], self.merge_sha)
                self.assertRegex(record["ref"], r"^[0-9a-f]{40}$")
                self.assertEqual(record["claim"], {"state": "unclaimed"})
                self.assertEqual(record["outcome"], {"state": "undisposed"})
        expected_phases = (
            ("Phase 1 — Controls", PHASE_PATH, PHASE_TEXT),
            ("Phase 2 — Colors", PHASE_PATH, PHASE_TEXT),
            ("Phase 3 — Navigation", MIXED_PATH, MIXED_TEXT),
        )
        for heading, path, contents in expected_phases:
            record = self.find_record("phase", heading)
            self.assertEqual(record["id"], f"cargo-liner:phase:{path}#{heading}")
            heading_line = next(
                index for index, line in enumerate(contents.splitlines())
                if line == f"### {heading}"
            )
            line = next(
                index + 1 for index, source_line in enumerate(contents.splitlines())
                if index > heading_line and source_line == "#### As-built"
            )
            self.assertEqual(record["source"], f"{path}:{line}")
            self.assertEqual(record["date"], "2026-10-01")
            self.assertEqual(record["pool"], "structured")
        asbuilt = self.find_record("asbuilt", "Current widget surface")
        self.assertEqual(asbuilt["id"], f"cargo-liner:asbuilt:{ASBUILT_PATH}")
        self.assertEqual(asbuilt["source"], f"{ASBUILT_PATH}:1")
        self.assertEqual(asbuilt["date"], "2026-10-01")
        self.assertFalse(any(record["kind"] == "asbuilt" and MIXED_PATH in record["source"] for record in records))
        expected_bullets = (
            ("Unreleased", 1, "Preview the selected widget."),
            ("1.2.0", 1, "Navigate between widgets."),
            ("1.2.0", 2, "Display each widget's color."),
            ("1.1.0", 1, "Select a widget from the list."),
        )
        for version, ordinal, bullet in expected_bullets:
            record = self.find_record("changelog", bullet)
            self.assertEqual(record["id"], f"cargo-liner:changelog:{CHANGELOG_PATH}@{version}#{ordinal}")
            line = next(
                index + 1 for index, source_line in enumerate(CHANGELOG_TEXT.splitlines())
                if source_line == "- " + bullet
            )
            self.assertEqual(record["source"], f"{CHANGELOG_PATH}:{line}")
            self.assertEqual(record["date"], "2026-10-01")
            self.assertEqual(record["pool"], "structured")
        for tag, sha, date in (("v1.0.0", self.base_sha, "2026-10-01"), ("v1.2.0", self.merge_sha, "2026-10-06")):
            record = self.find_record("tag", tag)
            self.assertEqual(record["id"], f"cargo-liner:tag:{tag}")
            self.assertEqual(record["source"], sha)
            self.assertEqual(record["date"], date)
            self.assertEqual(record["pool"], "structured")
        self.assertFalse(any(record["title"] == "unmerged-tag" for record in records))
        merge = self.find_record("merge", "Accept keyboard navigation")
        self.assertEqual(merge["id"], "cargo-liner:merge:" + self.merge_sha)
        self.assertEqual(merge["source"], self.merge_sha)
        self.assertEqual(merge["date"], "2026-10-05")
        expected_commits = {self.base_sha, self.docs_sha, self.topic_sha, self.main_sha}
        self.assertEqual({record["source"] for record in records if record["kind"] == "commit"}, expected_commits)
        for record in records:
            if record["kind"] in ("merge", "commit"):
                self.assertEqual(record["pool"], "commits")
                self.assertEqual(record["id"], f"cargo-liner:{record['kind']}:{record['source']}")

    def test_collect_reads_committed_files_and_last_file_commit_dates(self) -> None:
        self.repo.write(PHASE_PATH, PHASE_TEXT.replace("keyboard input", "arrow keys"))
        file_sha = self.repo.commit("Update accepted controls", date="2026-10-08")
        self.repo.write(PHASE_PATH, "# Dirty text that must not become a record\n")
        self.repo.write("docs/widgets/as-built/untracked.md", "# Untracked result\n")
        _ = self.collect()
        first = self.find_record("phase", "Phase 1 — Controls")
        self.assertEqual(first["ref"], file_sha)
        self.assertEqual(first["date"], "2026-10-08")
        self.assertEqual(self.find_record("asbuilt", "Current widget surface")["date"], "2026-10-01")
        self.assertFalse(any("Dirty" in record["title"] or "Untracked" in record["title"] for record in self.records()))

    def test_collect_preserves_non_ascii_document_and_changelog_paths(self) -> None:
        phase_path = "docs/plans/café.md"
        asbuilt_path = "docs/as-built/résumé.md"
        changelog_path = "crates/naïve/CHANGELOG.md"
        self.repo.write(phase_path, "### Phase 9 — café\n\n#### As-built\nCafé controls are visible.\n")
        self.repo.write(asbuilt_path, "# Résumé panel\nThe résumé is visible.\n")
        self.repo.write(changelog_path, "# Changelog\n\n## [Unreleased]\n- Show café controls.\n- Show the résumé panel.\n")
        _ = self.repo.commit("Record international widget surfaces", date="2026-10-08")
        _ = self.collect()
        expected = (
            ("phase", "Phase 9 — café", f"{phase_path}#Phase 9 — café", f"{phase_path}:3"),
            ("asbuilt", "Résumé panel", asbuilt_path, f"{asbuilt_path}:1"),
            ("changelog", "Show café controls.", f"{changelog_path}@Unreleased#1", f"{changelog_path}:4"),
            ("changelog", "Show the résumé panel.", f"{changelog_path}@Unreleased#2", f"{changelog_path}:5"),
        )
        for kind, title, key, source in expected:
            with self.subTest(kind=kind, title=title):
                record = self.find_record(kind, title)
                self.assertEqual(record["id"], f"cargo-liner:{kind}:{key}")
                self.assertEqual(record["source"], source)

    def test_collect_selects_changelogs_and_asbuilt_files_by_parent_components(self) -> None:
        nested_changelog = "crates/widget/vendor/CHANGELOG.md"
        nested_document = "docs/as-built/sub/notes.md"
        kept_document = "docs/guide/as-built/kept.md"
        self.repo.write(nested_changelog, "# Changelog\n\n## [Unreleased]\n- Vendor detail.\n")
        self.repo.write(nested_document, "# Nested implementation notes\nInternal details.\n")
        self.repo.write(kept_document, "# Accepted guide surface\nThe guide is visible.\n")
        _ = self.repo.commit("Record documents at different directory depths", date="2026-10-08")
        _ = self.collect()
        paths = {record["source"].rsplit(":", 1)[0] for record in self.records()}
        with self.subTest(path=nested_changelog):
            self.assertNotIn(nested_changelog, paths)
        with self.subTest(path=nested_document):
            self.assertNotIn(nested_document, paths)
        kept = self.find_record("asbuilt", "Accepted guide surface")
        self.assertEqual(kept["id"], f"cargo-liner:asbuilt:{kept_document}")
        self.assertEqual(kept["source"], f"{kept_document}:1")

    def test_collect_reads_sources_without_holding_the_state_lock(self) -> None:
        real_collect = cast(
            Callable[[str, ProjectSources], list[BackfillSourceRecord]],
            getattr(inventory, "_collect"),
        )
        read_while_unlocked = False

        def collect_without_lock(project: str, configuration: ProjectSources) -> list[BackfillSourceRecord]:
            nonlocal read_while_unlocked
            lock_path = state_dir() / "lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    read_while_unlocked = True
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            return real_collect(project, configuration)

        with patch("inventory._collect", side_effect=collect_without_lock):
            _ = self.collect()
        self.assertTrue(read_while_unlocked)
        self.assertEqual(len(self.records()), 15)

    def test_collect_preserves_a_ledger_frozen_while_sources_are_read(self) -> None:
        concurrent_record: BackfillSourceRecord = {
            "id": f"cargo-liner:commit:{self.base_sha}",
            "project": "cargo-liner",
            "repo": str(self.repo.path),
            "ref": self.base_sha,
            "kind": "commit",
            "pool": "commits",
            "title": "Initial recorded sources",
            "date": "2026-10-01",
            "source": self.base_sha,
            "claim": {"state": "unclaimed"},
            "outcome": {"state": "internal", "reason": "Already reviewed by the first collector"},
        }
        concurrent_bytes = (json.dumps(concurrent_record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        real_collect = cast(
            Callable[[str, ProjectSources], list[BackfillSourceRecord]],
            getattr(inventory, "_collect"),
        )

        def collect_after_competing_freeze(project: str, configuration: ProjectSources) -> list[BackfillSourceRecord]:
            path = self.ledger_path(project)
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_bytes(concurrent_bytes)
            return real_collect(project, configuration)

        with patch("inventory._collect", side_effect=collect_after_competing_freeze):
            result = self.collect()
        self.assertEqual(self.ledger_path().read_bytes(), concurrent_bytes)
        lines = result.stdout.splitlines()
        self.assertIn("cargo-liner: 1 frozen records", lines)
        self.assertTrue(any(line.startswith("structured: 0 | proposal: ") for line in lines))
        self.assertTrue(any(line.startswith("commits: 1 | proposal: ") for line in lines))
        self.assertIn("  commit: 1 | proposal: 2156", lines)
        self.assertIn("  merge: 0 | proposal: 76", lines)

    def test_collect_twice_preserves_ledger_bytes_and_frozen_ref(self) -> None:
        _ = self.collect()
        frozen = self.ledger_path().read_bytes()
        self.repo.write(PHASE_PATH, PHASE_TEXT + "\n### Phase 4 — Later\n#### As-built\nLater work.\n")
        _ = self.repo.commit("Accept later work", date="2026-10-08")
        _ = self.repo.git("tag", "v2.0.0")
        _ = self.collect()
        self.assertEqual(self.ledger_path().read_bytes(), frozen)
        self.assertEqual({record["ref"] for record in self.records()}, {self.merge_sha})

    def test_existing_ledger_keeps_review_states_without_reading_the_repository(self) -> None:
        _ = self.collect()
        identifiers = self.list_ids(self.cli(["next", "--project", "cargo-liner", "--pool", "structured", "--claim", "alpha", "--limit", "1"]))
        marked = self.cli(["mark", identifiers[0], "--worker", "alpha", "--as", "internal", "--reason", "Reviewed implementation work"])
        self.assertEqual(marked.status, 0, marked.stderr)
        original = self.ledger_path().read_bytes()
        _ = self.repo.path.rename(self.root / "unavailable-repo")
        _ = self.collect()
        self.assertEqual(self.ledger_path().read_bytes(), original)

    def test_collect_reports_measured_pools_and_proposal_counts(self) -> None:
        result = self.collect()
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "cargo-liner: 15 frozen records")
        self.assertEqual(lines[1:10], [
            "structured: 10 | proposal: phase=200, changelog=755, tag=30",
            "  phase: 3 | proposal: 200",
            "  asbuilt: 1 | proposal: not measured",
            "  changelog: 4 | proposal: 755",
            "  tag: 2 | proposal: 30",
            "  merge: 0 | proposal: not measured",
            "  commit: 0 | proposal: not measured",
            "  documents: 4 | proposal: not measured",
            "  files: 3 | proposal: not measured",
        ])
        self.assertEqual(lines[10:17], [
            "commits: 5 | proposal: commit=2156, merge=76",
            "  phase: 0 | proposal: not measured",
            "  asbuilt: 0 | proposal: not measured",
            "  changelog: 0 | proposal: not measured",
            "  tag: 0 | proposal: not measured",
            "  merge: 1 | proposal: 76",
            "  commit: 4 | proposal: 2156",
        ])

    def test_excluded_origin_main_range_omits_its_reachable_commits(self) -> None:
        self.source_table["cargo-liner"] = ProjectSources(
            (
                HistorySource(
                    str(self.repo.path), "main", AfterRef("origin/main"), ("commit", "merge")
                ),
            ),
            {},
        )
        _ = self.collect()
        self.assertEqual(
            {record["source"] for record in self.records()},
            {self.docs_sha, self.topic_sha, self.main_sha, self.merge_sha},
        )
        self.assertNotIn(self.base_sha, {record["source"] for record in self.records()})
        self.assertEqual({record["pool"] for record in self.records()}, {"commits"})

    def test_merges_follow_only_the_first_parent(self) -> None:
        _ = self.repo.git("switch", "-c", "nested", self.docs_sha)
        self.repo.write("nested.txt", "Nested work.\n")
        _ = self.repo.commit("Nested work", date="2026-10-06")
        _ = self.repo.git("switch", "-c", "nested-side", self.docs_sha)
        self.repo.write("nested-side.txt", "Another nested result.\n")
        _ = self.repo.commit("Nested side work", date="2026-10-07")
        _ = self.repo.git("switch", "nested")
        nested_merge = self.repo.merge("nested-side", "Merge inside topic", date="2026-10-08")
        _ = self.repo.git("switch", "main")
        accepted_merge = self.repo.merge("nested", "Accept nested topic", date="2026-10-09")
        _ = self.collect()
        merges = {record["source"] for record in self.records() if record["kind"] == "merge"}
        self.assertEqual(merges, {self.merge_sha, accepted_merge})
        self.assertNotIn(nested_merge, merges)

    def test_hana_sources_deduplicate_merges_by_sha(self) -> None:
        _ = self.repo.git("switch", "-c", "init/catalyst")
        self.repo.write("catalyst.txt", "Catalyst accepts another result.\n")
        _ = self.repo.commit("Prepare catalyst result", date="2026-10-07")
        _ = self.repo.git("switch", "-c", "catalyst-topic")
        self.repo.write("catalyst-topic.txt", "Another accepted result.\n")
        _ = self.repo.commit("Add catalyst result", date="2026-10-08")
        _ = self.repo.git("switch", "init/catalyst")
        catalyst_merge = self.repo.merge("catalyst-topic", "Accept catalyst result", date="2026-10-09")
        repo = str(self.repo.path)
        self.source_table["hana"] = ProjectSources(
            (
                DocumentSource(repo, "init/catalyst"),
                HistorySource(repo, "main", EntireHistory(), ("merge",)),
                HistorySource(repo, "init/catalyst", AfterRef("origin/main"), ("merge",)),
            ),
            {},
        )
        _ = self.collect("hana")
        records = self.records("hana")
        merges = [record for record in records if record["kind"] == "merge"]
        self.assertEqual({record["source"] for record in merges}, {self.merge_sha, catalyst_merge})
        self.assertEqual(len(merges), 2)
        self.assertEqual({record["kind"] for record in records}, {"phase", "asbuilt", "merge"})
        for record in records:
            if record["source"] == self.merge_sha:
                self.assertIn(record["ref"], (self.merge_sha, catalyst_merge))
            else:
                self.assertEqual(record["ref"], catalyst_merge)

    def test_project_source_pools_do_not_leak_other_kinds(self) -> None:
        expected = {
            "natedev": ({"commit", "merge", "tag"}, {"commits"}),
            "obsidian_knife": ({"commit", "merge"}, {"commits"}),
            "nateroids": ({"phase", "asbuilt", "commit", "merge"}, {"structured", "commits"}),
        }
        for project, (kinds, pools) in expected.items():
            with self.subTest(project=project):
                _ = self.collect(project)
                records = self.records(project)
                self.assertEqual({record["kind"] for record in records}, kinds)
                self.assertEqual({record["pool"] for record in records}, pools)

    def test_bevy_changelog_sources_use_only_the_named_paths(self) -> None:
        for directory in ("mcp", "mcp_macros", "extras"):
            self.repo.write(f"{directory}/CHANGELOG.md", f"# Changelog\n\n## [1.0.0]\n- A result for {directory}.\n")
        self.repo.write("unrelated/CHANGELOG.md", "# Changelog\n## [1.0.0]\n- Unrelated result.\n")
        _ = self.repo.commit("Record component changelogs", date="2026-10-08")
        _ = self.collect("bevy_brp")
        changelogs = [record for record in self.records("bevy_brp") if record["kind"] == "changelog"]
        self.assertEqual(len(changelogs), 3)
        self.assertEqual({record["source"].rsplit(":", 1)[0] for record in changelogs}, {"mcp/CHANGELOG.md", "mcp_macros/CHANGELOG.md", "extras/CHANGELOG.md"})

    def test_next_is_oldest_first_and_prints_the_specified_fields(self) -> None:
        _ = self.collect()
        frozen = self.ledger_path().read_bytes()
        result = self.cli(["next", "--project", "cargo-liner", "--pool", "commits", "--limit", "0"])
        identifiers = self.list_ids(result)
        records = {record["id"]: record for record in self.records()}
        dates = [records[identifier]["date"] for identifier in identifiers]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(len(identifiers), 5)
        for identifier, line in zip(identifiers, result.stdout.splitlines()):
            record = records[identifier]
            self.assertEqual(line, f"{identifier} | {record['date']} | {record['kind']} | {record['title']} | {record['source']}")
        self.assertEqual(self.ledger_path().read_bytes(), frozen)

    def test_next_default_limit_is_25_and_zero_lists_all(self) -> None:
        for number in range(27):
            _ = self.repo.commit(f"Additional result {number}", date="2026-10-08")
        _ = self.collect()
        arguments = ["next", "--project", "cargo-liner", "--pool", "commits", "--kind", "commit"]
        self.assertEqual(len(self.list_ids(self.cli(arguments))), 25)
        self.assertEqual(len(self.list_ids(self.cli(arguments + ["--limit", "0"]))), 31)

    def test_next_claim_hides_another_worker_and_keeps_same_worker(self) -> None:
        _ = self.collect()
        arguments = ["next", "--project", "cargo-liner", "--pool", "commits", "--kind", "commit"]
        listed = self.list_ids(self.cli(arguments + ["--claim", "alpha", "--limit", "1"]))
        self.assertEqual(listed, ["cargo-liner:commit:" + self.base_sha])
        self.assertEqual(self.record_after(listed[0])["claim"], {"state": "claimed", "worker": "alpha"})
        other = self.list_ids(self.cli(arguments + ["--claim", "beta", "--limit", "0"]))
        self.assertEqual(len(other), 3)
        self.assertNotIn(listed[0], other)
        same = self.list_ids(self.cli(arguments + ["--claim", "alpha", "--limit", "0"]))
        self.assertEqual(same, listed)
        self.assertEqual(self.list_ids(self.cli(arguments + ["--limit", "0"])), [])

    def test_an_interrupted_claim_can_be_resumed_released_and_reassigned_on_rerun(self) -> None:
        _ = self.collect()
        arguments = ["next", "--project", "cargo-liner", "--pool", "commits", "--kind", "commit",
                     "--claim", "alpha", "--limit", "1"]
        replace = os.replace

        def stop_after_save(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
            replace(source, destination)
            raise KeyboardInterrupt("stopped after saving claims")

        with patch.object(os, "replace", side_effect=stop_after_save):
            with self.assertRaises(KeyboardInterrupt):
                _ = self.cli(arguments)
        claimed = self.list_ids(self.cli(arguments))
        self.assertEqual(claimed, ["cargo-liner:commit:" + self.base_sha])
        self.assertEqual(self.record_after(claimed[0])["claim"], {"state": "claimed", "worker": "alpha"})
        release = ["release", "--project", "cargo-liner", "--worker", "alpha"]
        with patch.object(os, "replace", side_effect=stop_after_save):
            with self.assertRaises(KeyboardInterrupt):
                _ = self.cli(release)
        self.assertEqual(self.cli(release), CliResult(0, "", ""))
        self.assertEqual(self.record_after(claimed[0])["claim"], {"state": "unclaimed"})
        reassigned = self.list_ids(self.cli([*arguments[:-3], "beta", "--limit", "1"]))
        self.assertEqual(reassigned, claimed)
        self.assertEqual(self.record_after(claimed[0])["claim"], {"state": "claimed", "worker": "beta"})

    def test_release_only_clears_the_named_workers_undisposed_claims(self) -> None:
        _ = self.collect()
        arguments = ["next", "--project", "cargo-liner", "--pool", "commits", "--kind", "commit"]
        claimed = self.list_ids(self.cli([*arguments, "--claim", "alpha", "--limit", "2"]))
        other, = self.list_ids(self.cli([*arguments, "--claim", "beta", "--limit", "1"]))
        self.assertEqual(self.cli(["mark", claimed[0], "--worker", "alpha", "--as", "internal",
                                   "--reason", "Implementation work"]).status, 0)
        result = self.cli(["release", "--project", "cargo-liner", "--worker", "alpha"])
        self.assertEqual(result, CliResult(0, claimed[1] + "\n", ""))
        self.assertEqual(self.record_after(claimed[0])["claim"], {"state": "claimed", "worker": "alpha"})
        self.assertEqual(self.record_after(claimed[1])["claim"], {"state": "unclaimed"})
        self.assertEqual(self.record_after(other)["claim"], {"state": "claimed", "worker": "beta"})
        before = self.ledger_path().read_bytes()
        self.assert_refusal(["release", "--project", "cargo-liner", "--worker", " "], "nonempty worker")
        self.assertEqual(self.ledger_path().read_bytes(), before)

    def test_next_path_prefixes_are_a_union_and_sha_sources_match_none(self) -> None:
        _ = self.collect()
        arguments = ["next", "--project", "cargo-liner", "--pool", "structured", "--limit", "0"]
        listed = self.list_ids(self.cli(arguments + ["--path", "docs/plans/", "--path", "crates/"]))
        expected = {record["id"] for record in self.records() if record["kind"] in ("phase", "changelog") and record["source"].startswith(("docs/plans/", "crates/"))}
        self.assertEqual(set(listed), expected)
        self.assertEqual(len(listed), 6)
        self.assertEqual(self.list_ids(self.cli(["next", "--project", "cargo-liner", "--pool", "commits", "--path", self.base_sha[:4], "--limit", "0"])), [])
        self.assertEqual(self.list_ids(self.cli(arguments + ["--path", "missing/"])), [])

    def test_next_excludes_disposed_records(self) -> None:
        _ = self.collect()
        identifier = self.find_record("commit", "Initial recorded sources")["id"]
        result = self.cli(["mark", identifier, "--as", "internal", "--reason", "Initial scaffolding"])
        self.assertEqual(result.status, 0, result.stderr)
        arguments = ["next", "--project", "cargo-liner", "--pool", "commits", "--limit", "0"]
        identifiers = self.list_ids(self.cli(arguments))
        self.assertNotIn(identifier, identifiers)
        self.assertEqual(len(identifiers), 4)

    def test_show_phase_reads_only_its_frozen_asbuilt_block(self) -> None:
        _ = self.collect()
        record = self.find_record("phase", "Phase 1 — Controls")
        self.repo.write(PHASE_PATH, "# Dirty replacement\n")
        result = self.cli(["show", record["id"]])
        self.assertEqual(result.status, 0, result.stderr)
        self.assertIn(record["id"], result.stdout)
        self.assertIn("#### As-built\nControls now respond to keyboard input.\n- Focus follows the selected control.", result.stdout)
        for unwanted in ("Dirty replacement", "The control tests pass.", "The selected color is visible", "An unrelated production section."):
            self.assertNotIn(unwanted, result.stdout)
        self.assertEqual(result.stderr, "")

    def test_show_asbuilt_reads_the_frozen_file(self) -> None:
        _ = self.collect()
        record = self.find_record("asbuilt", "Current widget surface")
        self.repo.write(ASBUILT_PATH, "# Dirty replacement\n")
        result = self.cli(["show", record["id"]])
        self.assertEqual(result.status, 0, result.stderr)
        self.assertIn(record["id"], result.stdout)
        self.assertIn(ASBUILT_TEXT.strip(), result.stdout)
        self.assertNotIn("Dirty replacement", result.stdout)

    def test_show_changelog_includes_the_matching_version_and_bullet(self) -> None:
        _ = self.collect()
        for version, title in (("1.2.0", "Navigate between widgets."), ("Unreleased", "Preview the selected widget.")):
            with self.subTest(version=version):
                record = self.find_record("changelog", title)
                result = self.cli(["show", record["id"]])
                self.assertEqual(result.status, 0, result.stderr)
                self.assertIn(record["id"], result.stdout)
                self.assertIn(f"## [{version}]", result.stdout)
                self.assertIn("- " + title, result.stdout)
                self.assertNotIn("- Select a widget from the list.", result.stdout)

    def test_show_history_includes_commit_message_and_stat(self) -> None:
        _ = self.collect()
        for kind, title, sha, changed_file in (
            ("commit", "Add keyboard navigation", self.topic_sha, "widget.txt"),
            ("merge", "Accept keyboard navigation", self.merge_sha, "widget.txt"),
            ("tag", "v1.0.0", self.base_sha, PHASE_PATH),
        ):
            with self.subTest(kind=kind):
                record = self.find_record(kind, title)
                result = self.cli(["show", record["id"]])
                self.assertEqual(result.status, 0, result.stderr)
                self.assertIn(sha, result.stdout)
                if kind != "merge":
                    self.assertIn(changed_file, result.stdout)
                    self.assertIn("|", result.stdout)
                if kind == "commit":
                    self.assertIn("Keyboard users can move between widgets.", result.stdout)
                elif kind == "merge":
                    self.assertIn("Accept keyboard navigation", result.stdout)
                else:
                    self.assertIn("Initial recorded sources", result.stdout)

    def test_mark_refuses_missing_note_reason_or_foreign_claim(self) -> None:
        _ = self.collect()
        identifier = self.find_record("phase", "Phase 1 — Controls")["id"]
        self.assert_refusal(["mark", identifier, "--as", "represented"], "note")
        self.assert_refusal(["mark", identifier, "--as", "represented", "--note", "Missing result"], "Missing result")
        self.assert_refusal(["mark", identifier, "--as", "internal"], "reason")
        self.note("Another project's result", project="hana")
        self.assert_refusal(["mark", identifier, "--as", "combined", "--note", "Another project's result"], "Another project's result")
        _ = self.list_ids(self.cli(["next", "--project", "cargo-liner", "--pool", "structured", "--kind", "phase", "--claim", "alpha", "--limit", "0"]))
        self.assert_refusal(["mark", identifier, "--worker", "beta", "--as", "internal", "--reason", "Internal implementation"], "alpha")
        self.assert_refusal(["mark", identifier, "--as", "internal", "--reason", "Internal implementation"], "alpha")

    def test_mark_requires_the_argument_matching_its_outcome(self) -> None:
        _ = self.collect()
        identifier = self.find_record("phase", "Phase 1 — Controls")["id"]
        self.note("Keyboard navigation")
        for disposition in ("represented", "combined"):
            with self.subTest(disposition=disposition):
                self.assert_refusal(["mark", identifier, "--as", disposition, "--reason", "Wrong outcome shape"], "note")
        for disposition in ("internal", "not-current", "unresolved"):
            with self.subTest(disposition=disposition):
                self.assert_refusal(["mark", identifier, "--as", disposition, "--note", "Keyboard navigation"], "reason")
        self.assert_refusal(["mark", identifier, "--as", "represented", "--note", "Keyboard navigation", "--reason", "Both fields"])

    def test_read_migrates_an_old_claim_once_and_only_the_worker_flag_can_complete_it(self) -> None:
        _ = self.collect()
        rows = self.records()
        selected = rows[0]["id"]
        old_rows = [cast(dict[str, object], cast(object, row)) for row in rows]
        old_rows[0]["claim"] = {"state": "claimed", "seat": "alpha"}
        path = self.ledger_path()
        _ = path.write_text("".join(json.dumps(row) + "\n" for row in old_rows))
        self.assertEqual(self.cli(["next", "--project", "cargo-liner", "--pool", "structured", "--limit", "0"]).status, 0)
        self.assertEqual(self.record_after(selected)["claim"], {"state": "claimed", "worker": "alpha"})
        self.assertNotIn('"seat"', path.read_text())
        modified = path.stat().st_mtime_ns
        self.assertEqual(self.cli(["next", "--project", "cargo-liner", "--pool", "structured", "--limit", "0"]).status, 0)
        self.assertEqual(path.stat().st_mtime_ns, modified)
        self.assertEqual(self.cli(["mark", selected, "--seat", "alpha", "--as", "internal", "--reason", "Internal work"]).status, 2)
        self.assertEqual(self.cli(["mark", selected, "--worker", "alpha", "--as", "internal", "--reason", "Internal work"]).status, 0)

    def test_mark_claimed_record_accepts_the_owning_worker(self) -> None:
        _ = self.collect()
        identifiers = self.list_ids(self.cli(["next", "--project", "cargo-liner", "--pool", "structured", "--kind", "asbuilt", "--claim", "alpha", "--limit", "0"]))
        self.assertEqual(len(identifiers), 1)
        self.note("Keyboard navigation")
        result = self.cli(["mark", identifiers[0], "--worker", "alpha", "--as", "represented", "--note", "Keyboard navigation"])
        self.assertEqual(result.status, 0, result.stderr)
        record = self.record_after(identifiers[0])
        self.assertEqual(record["outcome"], {"state": "represented", "note": "Keyboard navigation"})
        self.assertEqual(record["claim"], {"state": "claimed", "worker": "alpha"})

    def test_mark_reads_ids_from_a_file_with_and_without_positional_ids(self) -> None:
        _ = self.collect()
        phase_id = self.find_record("phase", "Phase 1 — Controls")["id"]
        identifiers = [
            phase_id,
            *(record["id"] for record in self.records() if record["id"] != phase_id),
        ]
        ids_file = self.root / "record-ids.txt"
        _ = ids_file.write_text("\n".join(identifiers[:2]) + "\n", encoding="utf-8")
        result = self.cli(["mark", "--ids-from", str(ids_file), "--as", "internal", "--reason", "Implementation details"])
        self.assertEqual(result.status, 0, result.stderr)
        _ = ids_file.write_text(identifiers[2] + "\n", encoding="utf-8")
        result = self.cli(["mark", identifiers[3], "--ids-from", str(ids_file), "--as", "not-current", "--reason", "Replaced by current behavior"])
        self.assertEqual(result.status, 0, result.stderr)
        for identifier in identifiers[:2]:
            self.assertEqual(self.record_after(identifier)["outcome"], {"state": "internal", "reason": "Implementation details"})
        for identifier in identifiers[2:4]:
            self.assertEqual(self.record_after(identifier)["outcome"], {"state": "not-current", "reason": "Replaced by current behavior"})
        self.assertEqual(self.record_after(identifiers[4])["outcome"], {"state": "undisposed"})

    def test_remark_replaces_tagged_outcome_without_stale_fields(self) -> None:
        _ = self.collect()
        identifier = self.find_record("phase", "Phase 1 — Controls")["id"]
        self.note("Keyboard navigation")
        for disposition in ("represented", "combined"):
            result = self.cli(["mark", identifier, "--as", disposition, "--note", "Keyboard navigation"])
            self.assertEqual(result.status, 0, result.stderr)
            self.assertEqual(self.record_after(identifier)["outcome"], {"state": disposition, "note": "Keyboard navigation"})
        for disposition in ("internal", "not-current", "unresolved"):
            result = self.cli(["mark", identifier, "--as", disposition, "--reason", "Reviewed source evidence"])
            self.assertEqual(result.status, 0, result.stderr)
            self.assertEqual(self.record_after(identifier)["outcome"], {"state": disposition, "reason": "Reviewed source evidence"})
        result = self.cli(["mark", identifier, "--as", "represented", "--note", "Keyboard navigation"])
        self.assertEqual(result.status, 0, result.stderr)
        self.assertEqual(self.record_after(identifier)["outcome"], {"state": "represented", "note": "Keyboard navigation"})
        for record in self.records():
            self.assertFalse("note" in record["outcome"] and "reason" in record["outcome"])

    def test_status_is_incomplete_until_every_record_has_a_disposition(self) -> None:
        _ = self.collect()
        initial = self.cli(["status", "--project", "cargo-liner"])
        self.assertEqual(initial.status, 1, initial)
        self.assertIn("undisposed", initial.stdout)
        self.assertRegex(initial.stdout, r"\bundisposed\b[^0-9\n]*15\b")
        self.assertEqual(initial.stderr, "")
        identifiers = [record["id"] for record in self.records()]
        result = self.cli(["mark", *identifiers, "--as", "internal", "--reason", "Source reviewed as implementation work"])
        self.assertEqual(result.status, 0, result.stderr)
        final = self.cli(["status", "--project", "cargo-liner"])
        self.assertEqual(final.status, 0, final)
        self.assertIn("internal", final.stdout)
        self.assertRegex(final.stdout, r"\binternal\b[^0-9\n]*15\b")
        self.assertRegex(final.stdout, r"\bundisposed\b[^0-9\n]*0\b")
        self.assertEqual(final.stderr, "")

    def test_status_pool_filter_and_named_notes(self) -> None:
        _ = self.collect()
        structured = [record["id"] for record in self.records() if record["pool"] == "structured"]
        self.note("Keyboard navigation")
        self.note("Widget colors")
        first = self.cli(["mark", structured[0], "--as", "represented", "--note", "Keyboard navigation"])
        self.assertEqual(first.status, 0, first.stderr)
        second = self.cli(["mark", structured[1], "--as", "combined", "--note", "Widget colors"])
        self.assertEqual(second.status, 0, second.stderr)
        rest = self.cli(["mark", *structured[2:], "--as", "unresolved", "--reason", "More evidence needed"])
        self.assertEqual(rest.status, 0, rest.stderr)
        result = self.cli(["status", "--project", "cargo-liner", "--pool", "structured"])
        self.assertEqual(result.status, 0, result)
        self.assertEqual(result.stdout.splitlines()[0], "cargo-liner structured: 10 records")
        for label in ("represented", "combined", "unresolved", "undisposed", "Keyboard navigation", "Widget colors"):
            self.assertIn(label, result.stdout)
        for disposition, count in (("represented", 1), ("combined", 1), ("unresolved", 8), ("undisposed", 0)):
            self.assertRegex(result.stdout, rf"\b{re.escape(disposition)}\b[^0-9\n]*{count}\b")
        commits = self.cli(["status", "--project", "cargo-liner", "--pool", "commits"])
        self.assertEqual(commits.status, 1, commits)
        self.assertEqual(commits.stderr, "")
        self.assertEqual(commits.stdout.splitlines(), [
            "cargo-liner commits: 5 records",
            "undisposed: 5",
            "represented: 0",
            "combined: 0",
            "internal: 0",
            "not-current: 0",
            "unresolved: 0",
            "notes:",
        ])
        all_pools = self.cli(["status", "--project", "cargo-liner"])
        self.assertEqual(all_pools.status, 1, all_pools)
        self.assertEqual(all_pools.stderr, "")
        self.assertEqual(all_pools.stdout.splitlines(), [
            "cargo-liner: 15 records",
            "undisposed: 5",
            "represented: 1",
            "combined: 1",
            "internal: 0",
            "not-current: 0",
            "unresolved: 8",
            "notes:",
            "  Keyboard navigation",
            "  Widget colors",
        ])

    def test_inventory_commands_leave_the_repository_unchanged(self) -> None:
        self.repo.write("dirty.txt", "An untracked local file.\n")
        before = {path.relative_to(self.repo.path).as_posix(): path.read_bytes() for path in self.repo.path.rglob("*") if path.is_file()}
        _ = self.collect()
        identifiers = self.list_ids(self.cli(["next", "--project", "cargo-liner", "--pool", "structured", "--claim", "alpha", "--limit", "0"]))
        shown = self.cli(["show", identifiers[0]])
        self.assertEqual(shown.status, 0, shown.stderr)
        marked = self.cli(["mark", *identifiers, "--worker", "alpha", "--as", "internal", "--reason", "Reviewed implementation work"])
        self.assertEqual(marked.status, 0, marked.stderr)
        self.assertEqual(self.cli(["status", "--project", "cargo-liner"]).status, 1)
        after = {path.relative_to(self.repo.path).as_posix(): path.read_bytes() for path in self.repo.path.rglob("*") if path.is_file()}
        self.assertEqual(after, before)


if __name__ == "__main__":
    _ = unittest.main()
