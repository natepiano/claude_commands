"""The change-review trailer is required on checkpoints, and a merge adds one row per visible change.

Every test works in a temporary git repository and a temporary list; none reads or writes hanadocs.
"""
from __future__ import annotations

from scripts.production.staff_test_support import foreign_hostname

import fcntl
import io
import json
import os
import shlex
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime
from dataclasses import replace
from pathlib import Path
from typing import cast, override
from unittest import mock


from scripts.production import change_review
from scripts.lib import copy_record
from scripts.production import (
    showrunners, staff, change_routing, merge_checkpoint, session_staff, production_doc, unit_config, unit_lookup,
)
from scripts.agents import remote
from scripts.production.production_doc import Production
from zoneinfo import ZoneInfo
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, recorded_showrunner, recorded_unit
from scripts.production import showrunner_tree
from scripts.production.change_review import Invisible, Malformed, Visible

# Importable once change_review has put scripts/lib on the path.
from scripts.lib import scratch
from scripts.lib import urgency
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

LIST = """# Change review list

## To review

| # | Change | Merged | On the Mac build | How to check |
| --- | --- | --- | --- | --- |
| C76 | Panels keep shields | 2026-10-14 | not yet | Launch Hana |
| C77 | Fonts redraw | 2026-10-14 | not yet | Change a font |

## Questions: later

| C80 | a closed row elsewhere still counts | 2026-10-01 | yes | - |

## Reviewed

| # | Change | Verdict | Date |
| --- | --- | --- | --- |
| C70 | Older | works | 2026-10-13 |
"""
GRID = Visible("The grid fades more gently", "Run `hana` in a natedev terminal", "The grid lines fade out farther away")
SEE = f"Change-Review: {GRID.change} | {GRID.check} | {GRID.see}"
# A production doc as the vault records it; a write never reads the doc itself.
PRODUCTION = Path.home() / "worktrees/demo-trunk/docs/plans/demo-production.md"
DOC = "demo"
DEMO_ID = fixture_runtime_id("showrunner::demo")


def setUpModule() -> None:
    """The pin on the row `show` printed and the scratch folders a verdict removes are in a temporary
    folder, never ~/.local/state/next, /tmp/claude/scratch or ~/rust/repro."""
    unittest.enterModuleContext(foreign_hostname())
    folder = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(folder.cleanup)
    unittest.enterModuleContext(mock.patch.dict(os.environ, {
        "STAFF_FILE": str(Path(folder.name) / "staff.json"), "NEXT_STATE_DIR": folder.name,
        "UNIT_HISTORY_DIR": f"{folder.name}/history", copy_record.ROOT_ENV: f"{folder.name}/scratch",
        scratch.VISIBLE_ROOT_ENV: f"{folder.name}/repro"}))
    _ = fixture_member("showrunner", name="fixture")
    _ = fixture_member("showrunner", name="demo", runtime_id=DEMO_ID,
                       doc=staff.LiveDoc(remote.local_machine(), PRODUCTION))
    _ = unittest.enterModuleContext(mock.patch.object(showrunners, "timer_docs", return_value={}))


def producer_rows(root: Path, documents: dict[str, Path], lists: dict[Path, str],
                    *, selected_rows: dict[Path, set[str]] | None = None) -> None:
    """Give the selected fixture rows Producer ids and staff records."""
    runners: dict[str, staff.StaffRow] = {}
    for name, doc in documents.items():
        runners[name] = fixture_member("showrunner", name=name, doc=staff.LiveDoc(remote.local_machine(), doc))
        text = doc.read_text() if doc.exists() else f"# {name}\n"
        if "**Showrunner checkout:**" not in text:
            text += f"\n- **Showrunner checkout:** `{root}`\n"
        if "**Change review:**" not in text:
            text += f"- **Change review:** `{next(iter(lists))}`\n"
        _ = doc.write_text(text)
    for path, default in lists.items():
        rows = change_review.pending(path)
        text = path.read_text()
        for row in rows:
            if selected_rows is not None and row["#"] not in selected_rows.get(path, set[str]()):
                continue
            owner = row.get("Production") or default
            if owner not in runners:
                continue
            name = "fixture-" + owner + "-" + path.stem.replace(" ", "-") + "-" + row["#"]
            member = fixture_member("unit", name=name, parent=runners[owner].id,
                                    worktree=staff.PresentWorktree(remote.local_machine(), root / name))
            if "| Producer |" not in text:
                lines = text.splitlines()
                header = next(i for i, line in enumerate(lines) if line.startswith("| # |"))
                lines[header] += " Producer |"
                lines[header + 1] += " --- |"
                for i in range(header + 2, len(lines)):
                    if lines[i].startswith("| C"):
                        lines[i] += " |"
                text = "\n".join(lines) + "\n"
            columns = next(line for line in text.splitlines() if line.startswith("| # |")).strip("|").split("|")
            index = [column.strip() for column in columns].index("Producer")
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if line.startswith(f"| {row['#']} |"):
                    cells = line.strip("|").split("|")
                    cells[index] = " " + str(member.id) + " "
                    lines[i] = "|" + "|".join(cells) + "|"
                    break
            text = "\n".join(lines) + "\n"
        if not text.startswith("---\n"):
            docs = "".join(f"  - {doc}\n" for doc in documents.values())
            text = f"---\nshowrunner: {default}\nproductions:\n{docs}---\n" + text
        _ = path.write_text(text)


class ProducerFixture(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    doc: Path = Path()
    listing: Path = Path()
    unit: staff.StaffRow

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.unit = staff.StaffRow(staff.StaffId(""), "unit", "", staff.StaffRoot(), current_session=staff.StaffId(""))


    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.doc = self.root / "top.md"
        self.listing = self.root / "change-review - top.md"
        self.enterContext(mock.patch.dict(os.environ, {"STAFF_FILE": str(self.root / "staff.json"),
            "CHANGE_REVIEW_DIR": str(self.root), "REVIEW_LINKS_ROOT": str(self.root / "links"),
            "NEXT_STATE_DIR": str(self.root / "next"), "UNIT_HISTORY_DIR": str(self.root / "history")}))
        _ = self.git("init", "-b", "main")
        _ = self.git("config", "user.name", "Producer fixture")
        _ = self.git("config", "user.email", "producer@example.invalid")
        _ = self.git("commit", "--allow-empty", "-m", "Initial")
        origin = self.root / "origin.git"
        _ = self.git("init", "--bare", str(origin))
        _ = self.git("remote", "add", "origin", str(origin))
        _ = self.git("push", "origin", "main")
        self.unit = self.branch_unit("new", "unit", "old-branch", "old-plan.md")
        _ = self.doc.write_text(f"# Top\n- **Repository:** `{self.repo}`\n- **Merge branch:** `main`\n"
            + f"- **Showrunner checkout:** `{self.repo}`\n- **Change review:** `{self.listing}`\n")

    def branch_unit(self, unit: str, folder: str, branch: str, plan: str) -> staff.StaffRow:
        row = recorded_unit("top", unit, self.root / folder, self.doc, plan=plan)
        _ = subprocess.run(["git", "-C", str(self.root / folder), "checkout", "-q", "-B", branch], check=True)
        return row

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *arguments], capture_output=True,
                              text=True, check=True).stdout.strip()

    def record_unit_commit(self, checkpoint: str, unit: str) -> None:
        record = staff.snapshot()
        documents = [self.doc]
        for row in sorted(record.find(kind="unit"), key=lambda row: isinstance(row.worktree, staff.RemovedWorktree)):
            parent = record.parent_of(row)
            if parent is not None and isinstance(parent.doc, (staff.LiveDoc, staff.WrappedDoc)):
                if parent.doc.path not in documents:
                    documents.append(parent.doc.path)
        source = change_review.unit_branches(documents).get(unit)
        path = Path(os.environ["UNIT_HISTORY_DIR"]) / "runs" / f"{checkpoint}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"schema_version": 1, "event_type": "checkpoint",
            "checkpoint_commit": checkpoint, "owner_staff_id": source.producer if source is not None else "",
            "status": "completed", "unit": unit, "working_dir": str(source.worktree) if source is not None else ""}) + "\n")

    def merge(self, name: str = "old-branch", change: str = "A visible change") -> str:
        _ = self.git("checkout", "-b", name)
        _ = self.git("commit", "--allow-empty", "-m",
            f"checkpoint({name}): change\n\nChange-Review: {change} | Read | Proof")
        self.record_unit_commit(self.git("rev-parse", "HEAD"), name)
        _ = self.git("checkout", "main")
        _ = self.git("merge", "--no-ff", name, "-m", f"Merge {name} into main")
        _ = self.git("push", "origin", "main")
        return self.git("rev-parse", "HEAD")

class ProducerWriterTests(ProducerFixture):
    def test_open_cells_follow_staff_changes_without_rewriting_review_history(self) -> None:
        _ = change_review.append_rows(self.listing, [Visible("Unit", "Read", "Proof"),
            Visible("Production", "Read", "Proof"), Visible("Reviewed", "Read", "Proof")],
            date(2026, 11, 4), "", self.doc,
            producers=[self.unit.id, change_review.UnknownProducer(), self.unit.id], record=staff.snapshot())
        _ = change_review.record_verdict(self.listing, "C3", "works", date(2026, 11, 4), self.doc)
        before = self.listing.read_bytes()
        parent = self.unit.parent
        assert isinstance(parent, staff.ReportsTo)
        destination = fixture_member("showrunner", name="destination")
        _ = staff.rename(parent.id, "renamed-production")
        _ = staff.rename(self.unit.id, "renamed")
        _ = staff.move(self.unit.id, staff.ReportsTo(destination.id))
        snapshot = change_review.review_snapshot(self.listing)
        unit, production = snapshot.pending
        self.assertEqual(unit["Production"], self.unit.id)
        self.assertEqual(production["Production"], parent.id)
        self.assertEqual(snapshot.reviewed[0]["Production"], "top")
        router = change_routing.StaffRouting(staff.snapshot(), {})
        self.assertEqual(router.owner(unit, self.listing).tab, "destination")
        self.assertEqual(router.owner(production, self.listing).tab, "renamed-production")
        self.assertIn("Production: destination", change_review.describe(unit))
        self.assertIn("Producer: renamed", change_review.describe(unit))
        self.assertEqual(self.listing.read_bytes(), before)

    def test_migrate_owners_is_not_a_command(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            _ = change_review.main(["migrate-owners"])
        self.assertEqual(error.exception.code, 2)

    def colliding_units(self) -> staff.StaffRow:
        _ = self.branch_unit("new", "unit", "branch-a", "branch-b.md")
        return self.branch_unit("second", "second", "branch-b", "second.md")

    def unrelated_unit(self, name: str = "external-unit", plan: str = "external-plan.md") -> staff.StaffRow:
        doc = self.root / "other.md"
        _ = doc.write_text("# Other\n")
        row = recorded_unit("other", name, self.root / "external", doc, plan=plan)
        _ = subprocess.run(["git", "-C", str(self.root / "external"), "checkout", "-q", "-B", "external-branch"],
                           check=True)
        return row

    def test_an_exact_branch_wins_over_an_earlier_unit_alias(self) -> None:
        _ = self.colliding_units()
        source = change_review.unit_branches([self.doc])["branch-b"]
        self.assertEqual(source.worktree, self.root / "second")
        self.assertEqual(source.name, "second")

    def test_merge_publication_uses_the_exact_branch_producer_before_an_alias(self) -> None:
        second = self.colliding_units()
        _ = self.git("checkout", "-b", "checkpoints")
        for name in ("branch-b", "branch-a"):
            _ = self.git("commit", "--allow-empty", "-m",
                         f"checkpoint({name}): change\n\nChange-Review: {name} | Read | Proof")
            self.record_unit_commit(self.git("rev-parse", "HEAD"), name)
        _ = self.git("checkout", "main")
        _ = self.git("merge", "--no-ff", "checkpoints", "-m", "Merge checkpoints into main")
        _ = self.git("push", "origin", "main")
        merged = self.git("rev-parse", "HEAD")
        production = Production(self.doc, "top", "main", self.repo, self.root / "log", ZoneInfo("UTC"), unit_lookup.doc_owner(self.doc, staff.snapshot()))
        merge_checkpoint.record_publication_changes(
            production, known_producer=change_review.KnownMergeProducer(merged, self.unit.id))
        self.assertEqual(change_review.pending(self.listing)[0]["Producer"], second.id)
        rows = change_review.pending(self.listing)
        self.assertEqual([row["Producer"] for row in rows], [second.id, self.unit.id])
        self.assertEqual([row["Production"] for row in rows], [production.owner.id, production.owner.id])

    def test_publication_writes_an_unrelated_units_producer_and_production(self) -> None:
        other = self.unrelated_unit()
        _ = self.merge("external-branch")
        production = Production(self.doc, "top", "main", self.repo, self.root / "log", ZoneInfo("UTC"),
                                unit_lookup.doc_owner(self.doc, staff.snapshot()))
        merge_checkpoint.record_publication_changes(production)
        row = change_review.pending(self.listing)[0]
        self.assertEqual(row["Producer"], other.id)
        assert isinstance(other.parent, staff.ReportsTo)
        self.assertEqual(row["Production"], other.parent.id)

    def test_publication_uses_the_live_unit_before_a_retired_unit_with_the_same_branch(self) -> None:
        retired = self.unrelated_unit(name="retired-unit")
        live_doc = self.root / "live.md"
        _ = live_doc.write_text("# Live\n")
        live = recorded_unit("live", "live-unit", self.root / "live", live_doc)
        _ = subprocess.run(["git", "-C", str(self.root / "live"), "checkout", "-q", "-B", "external-branch"],
                           check=True)
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.root)}))
        parked = unit_config.parked_path(retired.id)
        parked.parent.mkdir(parents=True)
        _ = parked.write_text((self.root / "external/.unit.toml").read_text()
            + f'\n[retired]\ndate = "2026-11-05"\nsession = "{retired.current_session}"\nbranch = "external-branch"\n')
        _ = staff.set_worktree(retired.id, staff.RemovedWorktree("natedev", self.root / "external"),
                              expected=retired.worktree)
        _ = self.merge("external-branch")
        production = Production(self.doc, "top", "main", self.repo, self.root / "log", ZoneInfo("UTC"),
                                unit_lookup.doc_owner(self.doc, staff.snapshot()))
        merge_checkpoint.record_publication_changes(production)
        row = change_review.pending(self.listing)[0]
        self.assertEqual(row["Producer"], live.id)
        assert isinstance(live.parent, staff.ReportsTo)
        self.assertEqual(row["Production"], live.parent.id)

        with self.subTest(checkpoint="legacy owner at a reused worktree"):
            retired = staff.snapshot().by_id(retired.id)
            assert retired is not None
            _ = staff.set_worktree(retired.id, staff.RemovedWorktree(remote.local_machine(), self.root / "live"),
                                  expected=retired.worktree)
            _ = self.git("checkout", "-b", "legacy-owner")
            _ = self.git("commit", "--allow-empty", "-m",
                         "checkpoint(legacy): change\n\nChange-Review: Legacy owner | Read | Live unit")
            checkpoint = self.git("rev-parse", "HEAD")
            history = self.root / "history/runs/legacy.jsonl"
            _ = history.write_text(json.dumps({"schema_version": 1, "event_type": "checkpoint",
                "checkpoint_commit": checkpoint, "working_dir": str(self.root / "live")}) + "\n")
            _ = self.git("checkout", "main")
            _ = self.git("merge", "--no-ff", "legacy-owner", "-m", "Merge legacy delivery")
            merged = self.git("rev-parse", "HEAD")
            snapshot = staff.snapshot()
            rows = [row for row in snapshot.rows if row.id != retired.id]
            retired = snapshot.by_id(retired.id)
            assert retired is not None
            for members in ((*rows, retired), (retired, *rows)):
                with self.subTest(retired_last=members[-1].id == retired.id):
                    changes = change_review.publication_changes(change_review.merge_commits(self.repo, merged), self.doc,
                                                                 record=staff.StaffSnapshot(tuple(members)))
                    self.assertEqual(changes[0].producer, live.id)

    def test_unit_producer_resolves_the_exact_staff_name_across_productions(self) -> None:
        other = self.unrelated_unit()
        for name, expected in (("new", self.unit.id), ("external-unit", other.id),
                               ("new-unit", change_review.UNKNOWN_PRODUCER), ("external", change_review.UNKNOWN_PRODUCER)):
            with self.subTest(name=name):
                self.assertEqual(change_review.unit_producer(name), expected)

    def test_unit_producer_returns_unknown_for_duplicate_and_absent_names(self) -> None:
        _ = self.unrelated_unit(name="new")
        for name in ("new", "absent"):
            with self.subTest(name=name):
                self.assertIs(change_review.unit_producer(name), change_review.UNKNOWN_PRODUCER)

    def test_publication_keeps_the_scoped_alias_when_another_production_has_it(self) -> None:
        other = self.unrelated_unit(plan="old-plan.md")
        _ = self.git("checkout", "-b", "checkpoints")
        for name in ("external-branch", "old-plan", "old"):
            _ = self.git("commit", "--allow-empty", "-m",
                         f"checkpoint({name}): change\n\nChange-Review: {name} | Read | Proof")
            self.record_unit_commit(self.git("rev-parse", "HEAD"), name)
        _ = self.git("checkout", "main")
        _ = self.git("merge", "--no-ff", "checkpoints", "-m", "Merge checkpoints into main")
        _ = self.git("push", "origin", "main")
        production = Production(self.doc, "top", "main", self.repo, self.root / "log", ZoneInfo("UTC"),
                                unit_lookup.doc_owner(self.doc, staff.snapshot()))
        merge_checkpoint.record_publication_changes(production)
        rows = change_review.pending(self.listing)
        self.assertEqual([row["Producer"] for row in rows], [other.id, self.unit.id, self.unit.id])
        assert isinstance(other.parent, staff.ReportsTo)
        assert isinstance(self.unit.parent, staff.ReportsTo)
        self.assertEqual([row["Production"] for row in rows],
                         [other.parent.id, self.unit.parent.id, self.unit.parent.id])

    def test_a_checkpoint_with_an_unmatched_branch_pattern_keeps_the_row_values_aligned(self) -> None:
        _ = self.git("checkout", "-b", "old-branch")
        for name, change in (("old-branch", "First"), ("a (b)", "Unmatched"), ("old-branch", "Last")):
            _ = self.git("commit", "--allow-empty", "-m",
                         f"checkpoint({name}): change\n\nChange-Review: {change} | Read | Proof")
            self.record_unit_commit(self.git("rev-parse", "HEAD"), name)
        _ = self.git("checkout", "main")
        _ = self.git("merge", "--no-ff", "old-branch", "-m", "Merge all into main")
        _ = self.git("push", "origin", "main")
        production = Production(self.doc, "top", "main", self.repo, self.root / "log", ZoneInfo("UTC"), unit_lookup.doc_owner(self.doc, staff.snapshot()))
        merge_checkpoint.record_publication_changes(production)
        rows = change_review.pending(self.listing)
        self.assertEqual([row["Change"] for row in rows], ["First", "Unmatched", "Last"])
        self.assertEqual([row["Producer"] for row in rows], [self.unit.id, "", self.unit.id])
        assert isinstance(self.unit.parent, staff.ReportsTo)
        self.assertEqual([row["Production"] for row in rows], [self.unit.parent.id] * 3)


    def test_a_verdict_preserves_the_producer_for_a_returning_row(self) -> None:
        _ = change_review.append_rows(self.listing, [change_review.Visible("Return", "Read", "Proof")],
            date(2026, 11, 2), "", self.doc, producers=[self.unit.id], record=staff.snapshot())
        _ = change_review.record_verdict(self.listing, "C1", "not works", date(2026, 11, 2), self.doc)
        row = change_review.review_snapshot(self.listing).reviewed[0]
        self.assertEqual(row["Producer"], self.unit.id)
        self.assertEqual(change_routing.StaffRouting(staff.snapshot(), {}).owner(row, self.root / "change-review - other.md").tab, "top")

    def test_merge_publication_writes_the_units_staff_id(self) -> None:
        production = Production(self.doc, "top", "main", self.repo, self.root / "log", ZoneInfo("UTC"), unit_lookup.doc_owner(self.doc, staff.snapshot()))
        for branch in ("old-branch", "natedev-shutdown"):
            with self.subTest(branch=branch):
                _ = subprocess.run(["git", "-C", str(self.root / "unit"), "branch", "-M", branch], check=True)
                _ = self.merge(branch, change=branch)
                merge_checkpoint.record_publication_changes(production)
                row = change_review.pending(self.listing)[-1]
                self.assertEqual(row["Producer"], self.unit.id)
                self.assertNotIn(self.unit.name, row["Producer"])
                self.assertEqual(row["Production"], production.owner.id)

    def test_unit_merge_with_an_unmatched_slug_writes_the_units_staff_id(self) -> None:
        merged = self.merge("unmatched-checkpoint-slug")
        production = Production(self.doc, "top", "main", self.repo, self.root / "log", ZoneInfo("UTC"),
                                unit_lookup.doc_owner(self.doc, staff.snapshot()))
        unit = merge_checkpoint.unit_from_row(
            self.unit, session_staff.RowResolver(staff.snapshot()))
        assert isinstance(unit, merge_checkpoint.Unit)
        request = merge_checkpoint.MergeRequest(
            production, unit, "1", merged, merge_checkpoint.ShrinkCommit(), merge_checkpoint.ValidateAndPush(),
            production_doc.NoPromotion(), (), frozenset(), frozenset(), (unit,), self.root,
            (), merge_checkpoint.NoReviewTrial(), "", False, "", "", 0, 0, "",
        )
        output = io.StringIO()
        with redirect_stdout(output):
            merge_checkpoint.record_publication_changes(
                request.production, record=request.staff_record,
                known_producer=change_review.KnownMergeProducer(merged, request.unit.staff_id))
        self.assertEqual(change_review.pending(self.listing)[0]["Producer"], self.unit.id)
        self.assertEqual(change_review.pending(self.listing)[0]["Production"], production.owner.id)
        notes = [line for line in output.getvalue().splitlines() if line.startswith("change review: no staff row")]
        self.assertEqual(notes, [])

        with self.subTest(subject="queue(7): queued change"):
            _ = self.git("checkout", "-b", "queue-unit")
            _ = self.git("commit", "--allow-empty", "-m",
                         "queue(7): queued change\n\nChange-Review: Queued change | Read | Queue result")
            _ = self.git("checkout", "main")
            _ = self.git("merge", "--no-ff", "queue-unit", "-m", "Merge queue-unit into main")
            _ = self.git("push", "origin", "main")
            queued_merge = self.git("rev-parse", "HEAD")
            merge_checkpoint.record_publication_changes(
                production, known_producer=change_review.KnownMergeProducer(queued_merge, self.unit.id))
            row = change_review.pending(self.listing)[-1]
            self.assertEqual(row["Change"], "Queued change")
            self.assertEqual(row["Producer"], self.unit.id)
            self.assertEqual(row["Production"], production.owner.id)

        with self.subTest(subject="checkpoint(old-branch): final change"):
            _ = self.git("checkout", "-b", "draft-unit")
            _ = self.git("commit", "--allow-empty", "-m",
                         "fix(unit): draft change\n\nChange-Review: Draft change | Draft check | Draft result")
            _ = self.git("commit", "--allow-empty", "-m",
                         "checkpoint(old-branch): final change\n\nChange-Review: Final change | Read | Final result")
            _ = self.git("commit", "--allow-empty", "-m",
                         "queue(7): later change\n\nChange-Review: Queued later | Read | Later result")
            _ = self.git("checkout", "main")
            _ = self.git("merge", "--no-ff", "draft-unit", "-m", "Merge draft-unit into main")
            _ = self.git("push", "origin", "main")
            final_merge = self.git("rev-parse", "HEAD")
            merge_checkpoint.record_publication_changes(
                production, known_producer=change_review.KnownMergeProducer(final_merge, self.unit.id))
            rows = change_review.pending(self.listing)
            self.assertEqual([row["Change"] for row in rows],
                             ["A visible change", "Queued change", "Final change", "Queued later"])

        for location in ("unfilled", "other-machine", "absent", "remote-parent"):
            with self.subTest(location=location):
                _ = self.git("checkout", "-b", f"producer-{location}")
                _ = self.git("commit", "--allow-empty", "-m",
                             f"queue(8): delivery\n\nChange-Review: {location} | Read | Fixed producer")
                _ = self.git("checkout", "main")
                _ = self.git("merge", "--no-ff", f"producer-{location}", "-m", "Merge delivery")
                _ = self.git("push", "origin", "main")
                merged = self.git("rev-parse", "HEAD")
                member = replace(self.unit, worktree=(staff.WorktreeUnfilled() if location == "unfilled"
                                 else staff.PresentWorktree(remote.other_machine(), self.root / "unit")))
                if location == "remote-parent":
                    parent = fixture_member("showrunner", name="remote-parent",
                        doc=staff.LiveDoc(remote.other_machine(), self.root / "remote-production.md"))
                    member = replace(member, parent=staff.ReportsTo(parent.id))
                rows = tuple(member if row.id == self.unit.id else row for row in staff.snapshot().rows
                             if row.id != self.unit.id or location != "absent")
                merge_checkpoint.record_publication_changes(production, record=staff.StaffSnapshot(rows),
                    known_producer=change_review.KnownMergeProducer(merged, self.unit.id))
                row = change_review.pending(self.listing)[-1]
                self.assertEqual(row["Change"], location)
                self.assertEqual(row["Producer"], self.unit.id)
                if location == "remote-parent":
                    self.assertEqual(row["Production"], production.owner.id)

    def test_an_unregistered_unit_writes_an_empty_producer_and_one_named_note(self) -> None:
        production = Production(self.doc, "top", "main", self.repo, self.root / "log", ZoneInfo("UTC"), unit_lookup.doc_owner(self.doc, staff.snapshot()))
        output = io.StringIO()
        with (mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot(
                  tuple(staff.snapshot().find(kind="showrunner")))), redirect_stdout(output)):
            _ = self.merge()
            merge_checkpoint.record_publication_changes(production)
        notes = [line for line in output.getvalue().splitlines() if line.startswith("change review: no staff row")]
        self.assertEqual(len(notes), 1)
        self.assertIn("old-branch", notes[0])
        self.assertEqual(change_review.pending(self.listing)[0]["Producer"], "")

    def test_add_in_a_unit_worktree_saves_its_id_and_a_showrunner_saves_no_id(self) -> None:
        for directory, expected in ((self.root / "unit", self.unit.id),
                                    (self.root / "unit" / "src", self.unit.id), (self.repo, ""), (self.root, "")):
            with self.subTest(directory=directory), redirect_stdout(io.StringIO()):
                result = change_review.main(["add", "--repo", str(directory), "--production", "top",
                    "--list", str(self.listing), "--change", str(directory), "--check", "Read", "--see", "Proof"])
                self.assertEqual(result, 0)
                self.assertEqual(change_review.pending(self.listing)[-1]["Producer"], expected)


class ReviewKindTest(unittest.TestCase):
    def test_set_kind_is_recorded_once_and_survives_the_verdict_with_its_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "change-review - sample.md"
            _ = change_review.append_rows(path, [GRID], date(2026, 11, 1), "abc12345", PRODUCTION, record=staff.snapshot())
            self.assertEqual(run("set-kind", "--production", DOC, "--list", str(path), "--row", "C1",
                                 "--kind", "Read only")[0], 0)
            row = change_review.review_rows(path)[0]
            self.assertEqual(row["Kind"], "Read only")
            before = path.read_bytes()
            self.assertEqual(run("set-kind", "--production", DOC, "--list", str(path), "--row", "C1",
                                 "--kind", "hidden")[0], 1)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(run("verdict", "--production", DOC, "--list", str(path), "--row", "C1",
                                 "--verdict", "works")[0], 0)
            saved = change_review.review_rows(path)[0]
            self.assertEqual((saved["Kind"], saved[change_review.CHECK], saved["Verdict"]),
                             ("Read only", GRID.how(), "works"))


class ParseTest(unittest.TestCase):
    def test_a_visible_line_a_none_line_and_a_malformed_line(self) -> None:
        self.assertEqual(change_review.entries(f"checkpoint(x): y\n\n{SEE}\nChange-Review: none — scripts only\n"
                                               + "Change-Review: something\n"),
                         [GRID, Invisible("scripts only"),
                          Malformed("something")])

    def test_only_a_checkpoint_that_changes_more_than_markdown_needs_a_line(self) -> None:
        self.assertTrue(change_review.needs_trailer("checkpoint(x): y", ("src/a.rs",)))
        self.assertFalse(change_review.needs_trailer("checkpoint(x): y", ("docs/a.md", "b.md")))
        for subject in ("ci(x): fix", "as-built(x): docs", "merge(x): origin", "production(x): row", "fix the vault",
                        "queue(7): queued change"):
            self.assertFalse(change_review.needs_trailer(subject, ("src/a.rs",)), subject)

    def test_a_missing_or_malformed_line_is_named(self) -> None:
        self.assertIn("no Change-Review line", change_review.problem("checkpoint(x): y", "checkpoint(x): y\n", ("a.py",)))
        self.assertIn(change_review.FORM, change_review.problem("checkpoint(x): y", "checkpoint(x): y\n", ("a.py",)))
        self.assertIn(change_review.FORM, change_review.problem("checkpoint(x): y", "Change-Review: grid\n", ("a.py",)))
        self.assertEqual(change_review.problem("checkpoint(x): y", SEE, ("a.py",)), "")

    def test_a_line_without_exactly_three_full_parts_is_refused(self) -> None:
        for value in ("The grid fades | Look across the floor", "A | B | ", " | B | C", "A | B | C | D"):
            self.assertEqual(change_review.parse(value), Malformed(value.strip()), value)
            problem = change_review.problem("checkpoint(x): y", f"Change-Review: {value}\n", ("a.py",))
            self.assertIn("needs three parts", problem, value)
            self.assertIn(change_review.FORM, problem, value)

    def test_checkpoint_validation_accepts_a_named_live_step(self) -> None:
        line = "Change-Review: New service | Open | New result | after restart"
        self.assertEqual(change_review.problem("checkpoint(x): service", line, ("service.py",)), "")
        item, = change_review.entries(line)
        self.assertIsInstance(item, Visible)
        assert isinstance(item, Visible)
        self.assertEqual(item.readiness, change_review.AfterStep("restart"))

    def test_the_check_cell_is_how_to_try_it_then_what_you_see(self) -> None:
        self.assertEqual(GRID.how(), "Run `hana` in a natedev terminal. You see: The grid lines fade out farther away")
        self.assertEqual(Visible("A", "Type `x`:", "B").how(), "Type `x`: You see: B")


class ListTest(unittest.TestCase):
    path: Path = Path()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "change-review.md"
        _ = self.path.write_text(LIST, encoding="utf-8")

    def test_rows_take_the_next_number_in_the_whole_file_and_end_the_table(self) -> None:
        rows = change_review.append_rows(self.path, [Visible("A | B", "Look", "It"), Visible("C", "D.", "E")],
                                         date(2026, 10, 14), "61b65bbf", PRODUCTION, record=staff.snapshot())
        self.assertEqual(rows, [f"| C81 | A \\| B | 2026-10-14 · 61b65bbf | not yet | Look. You see: It | {DEMO_ID} |  |",
                                f"| C82 | C | 2026-10-14 · 61b65bbf | not yet | D. You see: E | {DEMO_ID} |  |"])
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[lines.index("| C77 | Fonts redraw | 2026-10-14 | not yet | Change a font | | |") + 1], rows[0])

    def test_a_list_without_the_section_is_refused_and_left_alone(self) -> None:
        _ = self.path.write_text("# list\n", encoding="utf-8")
        self.assertRaises(change_review.ChangeReviewError, change_review.append_rows, self.path,
                          [Visible("A", "B", "C")], date(2026, 10, 14), "abc", PRODUCTION, record=staff.snapshot())
        self.assertEqual(self.path.read_text(encoding="utf-8"), "# list\n")

    def test_a_row_with_only_a_separator_raises_a_named_review_error(self) -> None:
        _ = self.path.write_text(LIST.replace("| C76 | Panels keep shields | 2026-10-14 | not yet | Launch Hana |", "|"))
        with self.assertRaises(change_review.ChangeReviewError) as raised:
            _ = change_review.review_snapshot(self.path)
        self.assertEqual(str(raised.exception), f"{self.path}: | has the wrong number of cells")

    def test_one_routed_pass_lists_delivery_records_once_for_three_lists(self) -> None:
        root = self.path.parent
        doc = root / "top.md"
        _ = doc.write_text("# Top")
        listings = [root / f"change-review - {name}.md" for name in ("top", "child", "other")]
        for listing in listings:
            _ = listing.write_text("## To review\n\n| # | Change |\n| --- | --- |\n| C1 | First |\n| C2 | Second |\n")
        links = root / "links"
        record = staff.StaffSnapshot((fixture_member("showrunner", name="top",
            doc=staff.LiveDoc(remote.local_machine(), doc)),))
        scans = mock.Mock(wraps=links.glob)
        folder = mock.Mock(glob=scans)
        with (mock.patch.object(change_review, "lists_directory", return_value=root),
              mock.patch.object(change_routing, "review_root", return_value=folder)):
            routed = change_review.routed_review_entries(listings[0], doc, record=record)
        self.assertEqual(sorted((source.path.name, owner.tab, row["#"]) for source, owner, row in routed),
                         sorted((listing.name, name, row) for listing, name in zip(listings, ("top", "child", "other"))
                                for row in ("C1", "C2")))
        scans.assert_called_once_with(".review-link-delivery-*")

    def test_a_production_doc_names_its_list_or_none(self) -> None:
        doc = self.path.with_name("production.md")
        _ = doc.write_text("# p\n\n- **Change review:** `~/rust/hanadocs/change-review.md`\n", encoding="utf-8")
        self.assertEqual(change_review.list_path(doc), Path.home() / "rust/hanadocs/change-review.md")
        _ = doc.write_text("# p\n", encoding="utf-8")
        self.assertIsNone(change_review.list_path(doc))

    def test_the_list_is_named_for_the_running_top_level_showrunner(self) -> None:
        doc = self.path.with_name("demo-production.md")
        mine = change_review.list_file("natedev")
        _ = doc.write_text(f"- **Change review:** `{mine}` — changes for the user to check\n", encoding="utf-8")
        with mock.patch.object(change_review, "showrunner_of", return_value="natedev"):
            self.assertEqual(change_review.list_path(doc), mine)
        self.assertEqual(mine.name, "change-review - natedev.md")
        stale = Path.home() / "rust/hanadocs/change-review/build-followups.md"
        _ = doc.write_text(f"- **Change review:** `{stale}`\n", encoding="utf-8")
        error = io.StringIO()
        with mock.patch.object(change_review, "showrunner_of", return_value="natedev"), redirect_stderr(error):
            self.assertEqual(change_review.list_path(doc), mine)
        self.assertIn(f"names {stale}", error.getvalue())
        down = change_review.ChangeReviewError("the showrunner of demo is not running")
        with mock.patch.object(change_review, "showrunner_of", side_effect=down):
            self.assertEqual(change_review.list_path(doc), stale)



from scripts.production.staff_test_support import isolated_staff_file


class StaffProductionNameTests(unittest.TestCase):
    def test_child_document_uses_the_staff_parent_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with isolated_staff_file(root / "staff.json", {}):
                parent = fixture_member("showrunner", name="hana")
                doc = root / "tool-based-ui-production.md"
                _ = fixture_member("showrunner", name="child", parent=parent.id,
                                   doc=staff.LiveDoc(remote.local_machine(), doc))
                self.assertEqual(change_review.showrunner_of(doc), "hana")
                self.assertEqual(change_review.production_doc("child"), doc)


    def test_inherited_list_uses_one_staff_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with isolated_staff_file(root / "staff.json", {}):
                listing = root / "change-review - hana.md"
                parent_doc = root / "parent-settings.md"
                _ = parent_doc.write_text(f"- **Change review:** {listing}\n")
                parent = fixture_member("showrunner", name="hana",
                    doc=staff.LiveDoc(remote.local_machine(), parent_doc))
                child_doc = root / "child-settings.md"
                _ = child_doc.write_text("# Child\n")
                _ = fixture_member("showrunner", name="child", parent=parent.id,
                    doc=staff.LiveDoc(remote.local_machine(), child_doc))
                record = staff.snapshot()
                with (mock.patch.object(change_review, "lists_directory", return_value=root),
                      mock.patch.object(staff, "snapshot", side_effect=[record]) as reads):
                    self.assertEqual(change_review.scope_list(child_doc), listing)
                reads.assert_called_once_with()
                child_doc.unlink()
                with (mock.patch.object(change_review, "lists_directory", return_value=root),
                      mock.patch.object(staff, "snapshot", side_effect=AssertionError("use the supplied snapshot"))):
                    self.assertEqual(change_review.scope_list(child_doc, record=record),
                                     root / "change-review - child.md")


class AttachedListTest(unittest.TestCase):
    root: Path = Path()
    doc: Path = Path()
    current: Path = Path()
    earlier: Path = Path()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.doc = self.root / "child-production.md"
        _ = self.doc.write_text("# Child\n")
        self.current = self.root / "change-review - parent.md"
        self.earlier = self.root / "change-review - child.md"
        _ = self.current.write_text("## To review\n\n| # | Change | Production | How to check | Merged |\n" +
            "| --- | --- | --- | --- | --- |\n" +
            "| C9 | Current child | child | Check current | 2026-10-20 |\n" +
            "| C10 | Parent-only legacy row | | Check parent | 2026-10-01 |\n")
        _ = self.earlier.write_text("## To review\n\n| # | Change | How to check | Merged | Urgency |\n" +
            "| --- | --- | --- | --- | --- |\n" +
            "| C9 | Earlier child | Check earlier | 2026-10-02 | |\n" +
            "| C11 | Dependent child | Check dependent | 2026-10-03 | blocks, after C9 |\n")
        _ = self.enterContext(mock.patch.object(change_review, "lists_directory", return_value=self.root))
        _ = self.enterContext(mock.patch.object(showrunner_tree, "root_of", return_value="parent"))

        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        producer_rows(self.root, {"parent": self.root / "parent-production.md", "child": self.doc},
                        {self.current: "parent", self.earlier: "child"})

    def test_descendant_lists_use_the_supplied_staff_names_after_a_rename(self) -> None:
        child = unit_lookup.doc_owner(self.doc, staff.snapshot())
        doc = self.root / "descendant-settings.md"
        _ = doc.write_text("# Descendant\n")
        _ = fixture_member("showrunner", name="grandchild", parent=child.id,
                           doc=staff.LiveDoc(remote.local_machine(), doc))
        earlier = self.root / "change-review - grandchild.md"
        _ = earlier.write_text("# Earlier changes\n")
        record = staff.snapshot()
        _ = staff.rename(child.id, "renamed-child")
        with mock.patch.object(staff, "snapshot", side_effect=AssertionError("use the supplied snapshot")):
            lists = change_review.scope_lists(self.current, self.doc, below=True, record=record)
            scope = change_review.review_scope(self.doc, True, record)
        self.assertEqual([item.path for item in lists], [self.current, self.earlier, earlier])
        self.assertEqual(scope, {"child", "grandchild"})

    def test_columnless_earlier_rows_belong_to_the_attached_production_without_list_edits(self) -> None:
        before = {path: path.read_bytes() for path in (self.current, self.earlier)}
        rows = change_review.scope_rows(self.current, self.doc)
        self.assertEqual([(row["#"], row["Change"]) for row in rows],
                         [("C9", "Current child"), ("C9", "Earlier child"), ("C11", "Dependent child")])
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_merged_prerequisite_names_its_own_list_despite_a_repeated_row_number(self) -> None:
        entries = change_review.scope_entries(self.current, self.doc)
        self.assertEqual({entry.key: entry.after for entry in entries},
                         {"C9": "", "child/C9": "", "child/C11": "child/C9"})
        ordered = [entry.key for entry in urgency.queue_order(entries, "attached-list-prerequisites")]
        self.assertLess(ordered.index("child/C9"), ordered.index("child/C11"))

    def test_broken_current_list_keeps_earlier_rows_and_reports_one_problem(self) -> None:
        _ = self.current.write_text("# Missing table\n")
        errors = io.StringIO()
        with redirect_stderr(errors):
            rows = change_review.scope_rows(self.current, self.doc)
        self.assertEqual([row["Change"] for row in rows], ["Earlier child", "Dependent child"])
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn(str(self.current), errors.getvalue())

    def test_a_collector_retains_unreadable_lists_while_default_readers_skip_them(self) -> None:
        read_text = Path.read_text

        def read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
            if path == self.current:
                raise PermissionError(f"{path}: unreadable")
            return read_text(path, encoding=encoding, errors=errors)

        record = staff.snapshot()
        errors = io.StringIO()
        sources: list[Path] = []
        with mock.patch.object(Path, "read_text", read), redirect_stderr(errors):
            routed = change_review.routed_review_entries(self.current, self.doc, record=record)
            with self.assertRaises(ValueError) as raised:
                _ = change_review.routed_review_entries(self.current, self.doc, record=record, sources=sources)
        self.assertIn(self.earlier, {source.path for source, _, _ in routed})
        self.assertNotIn(self.current, {source.path for source, _, _ in routed})
        self.assertIn(self.doc, sources)
        self.assertIn(self.current, sources)
        self.assertIn(str(self.current), str(raised.exception))
        self.assertIn(str(self.current), errors.getvalue())

    def test_absent_earlier_list_preserves_current_row_keys_and_filter(self) -> None:
        self.earlier.unlink()
        entries = change_review.scope_entries(self.current, self.doc)
        self.assertEqual([(entry.key, entry.item["Change"]) for entry in entries], [("C9", "Current child")])

    def test_show_for_the_calling_production_includes_earlier_rows_and_scoped_count(self) -> None:
        before = {path: path.read_bytes() for path in (self.current, self.earlier)}
        with mock.patch.object(change_review, "production_doc", return_value=self.doc), \
                mock.patch.object(change_review, "production_list", return_value=self.current):
            code, output, error = run("show")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output.splitlines()[0], "child/C9 Earlier child")
        self.assertEqual(output.splitlines()[-1], "2 more to review")
        self.assertNotIn("Parent-only", output)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_current_list_prerequisites_keep_their_local_row_ids_after_attachment(self) -> None:
        _ = self.current.write_text("## To review\n\n| # | Change | Production | How to check | Merged | Urgency |\n" +
            "| --- | --- | --- | --- | --- | --- |\n" +
            "| C9 | Current child | child | Check current | 2026-10-20 | after C12 |\n" +
            "| C12 | Current prerequisite | child | Check prerequisite | 2026-10-21 | |\n")
        producer_rows(self.root, {"parent": self.root / "parent-production.md", "child": self.doc},
                        {self.current: "parent"})
        entries = change_review.scope_entries(self.current, self.doc)
        self.assertEqual({entry.key: entry.after for entry in entries},
                         {"C9": "C12", "C12": "", "child/C9": "", "child/C11": "child/C9"})
        ordered = [entry.key for entry in urgency.queue_order(entries, "attached-current-prerequisites")]
        self.assertLess(ordered.index("C12"), ordered.index("C9"))


class StampTest(unittest.TestCase):
    """Every write sets date_modified and adds its production once, keeping every other key."""

    path: Path = Path()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "change-review - natedev.md"
        _ = self.path.write_text("\n".join([
            "---", 'date_created: "[[2026-10-14]]"', 'date_modified: "[[2026-10-14]]"', "productions:",
            f'  - "{PRODUCTION}"', "showrunner: natedev", "tags:", "  - review", "---", "",
            "# natedev showrunner: changes to review", "", change_review.BODY]), encoding="utf-8")

    def frontmatter(self) -> list[str]:
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return lines[:lines.index("---", 1) + 1]

    def test_every_row_writer_preserves_pipes_through_sorting_and_the_saved_verdict(self) -> None:
        item = Visible("Use A | B", "Print A | B", "A | B appears")
        _ = change_review.append_rows(self.path, [item], date(2026, 11, 2), "abc12345", PRODUCTION, record=staff.snapshot())
        _ = change_review.set_row_kind(self.path, "C1", "See in action", PRODUCTION)
        edited = Visible("Use C | D", r"grep 'C\|D'", "C | D appears")
        _ = change_review.edit_row(self.path, "C1", edited, date(2026, 11, 2), PRODUCTION)
        _ = change_review.mark_row(self.path, "C1", datetime(2026, 11, 2, 12), PRODUCTION)
        _ = change_review.append_rows(self.path, [item], date(2026, 11, 2), "def12345", PRODUCTION, record=staff.snapshot())
        before = change_review.review_snapshot(self.path)
        self.assertEqual(before.pending[0]["Change"], edited.change)
        self.assertEqual(before.pending[0][change_review.CHECK], edited.how())
        _ = change_review.record_verdict(self.path, "C1", "works: C | D", date(2026, 11, 2), PRODUCTION)
        after = change_review.review_snapshot(self.path)
        self.assertEqual(after.reviewed[0]["Verdict"], "works: C | D")
        self.assertEqual(after.reviewed[0][change_review.CHECK], edited.how())
        self.assertEqual(after.pending[0]["Change"], item.change)

    def test_a_malformed_cell_count_is_refused_before_a_snapshot_can_misroute_a_row(self) -> None:
        _ = change_review.append_rows(self.path, [Visible("A", "B", "C")], date(2026, 11, 2), "", PRODUCTION, record=staff.snapshot())
        text = self.path.read_text().replace("| A |", "| A | extra |")
        _ = self.path.write_text(text)
        with self.assertRaisesRegex(change_review.ChangeReviewError, "C1 has the wrong number of cells"):
            _ = change_review.review_snapshot(self.path)

    def test_page_and_save_readers_refuse_the_same_malformed_review_rows(self) -> None:
        _ = change_review.append_rows(self.path, [Visible("A", "B", "C")], date(2026, 11, 2), "", PRODUCTION, record=staff.snapshot())
        for reviewed in (False, True):
            with self.subTest(reviewed=reviewed):
                if reviewed:
                    _ = change_review.record_verdict(self.path, "C1", "works", date(2026, 11, 2), PRODUCTION)
                original = self.path.read_text()
                _ = self.path.write_text(original.replace("| A |", "| A | extra |"))
                for reader in (change_review.review_rows, change_review.review_snapshot):
                    with self.assertRaisesRegex(change_review.ChangeReviewError, "C1 has the wrong number of cells"):
                        _ = reader(self.path)
                if not reviewed:
                    with self.assertRaisesRegex(change_review.ChangeReviewError, "C1 has the wrong number of cells"):
                        _ = change_review.pending(self.path)
                _ = self.path.write_text(original)

    def test_a_write_moves_date_modified_and_keeps_a_listed_production_in_either_form(self) -> None:
        _ = change_review.append_rows(self.path, [Visible("A", "B", "C")], date(2026, 10, 15), "",
                                      Path("~/worktrees/demo-trunk/docs/plans/demo-production.md"), record=staff.snapshot())
        self.assertEqual(self.frontmatter(), [
            "---", 'date_created: "[[2026-10-14]]"', 'date_modified: "[[2026-10-15]]"', "productions:",
            f'  - "{PRODUCTION}"', "showrunner: natedev", "tags:", "  - review", "---"])

    def test_a_new_production_is_added_after_the_listed_ones(self) -> None:
        other = Path.home() / "rust/cargo-liner/docs/child-production.md"
        _ = fixture_member("showrunner", name="child", doc=staff.LiveDoc(remote.local_machine(), other))
        _ = change_review.append_rows(self.path, [Visible("A", "B", "C")], date(2026, 10, 15), "", other, record=staff.snapshot())
        _ = change_review.record_verdict(self.path, "C1", "works", date(2026, 10, 16), other)
        self.assertEqual(self.frontmatter()[2:6], [
            'date_modified: "[[2026-10-16]]"', "productions:", f'  - "{PRODUCTION}"',
            '  - "~/rust/cargo-liner/docs/child-production.md"'])

    def test_a_list_without_frontmatter_gains_it_on_its_next_write(self) -> None:
        _ = self.path.write_text(change_review.BODY, encoding="utf-8")
        _ = change_review.append_rows(self.path, [Visible("A", "B", "C")], date(2026, 10, 15), "", PRODUCTION, record=staff.snapshot())
        self.assertEqual(self.frontmatter(), [
            "---", 'date_created: "[[2026-10-15]]"', 'date_modified: "[[2026-10-15]]"', "productions:",
            '  - "~/worktrees/demo-trunk/docs/plans/demo-production.md"', "showrunner: natedev", "---"])


def run(*arguments: str) -> tuple[int, str, str]:
    """The command line's exit code, standard output and standard error."""
    output, error = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(error):
        code = change_review.main(list(arguments))
    return code, output.getvalue(), error.getvalue()


def origin_commits(root: Path) -> tuple[Path, str, str]:
    """Create two commits on main and push them to a temporary bare origin."""
    repo = root / "source"
    repo.mkdir()

    def git(*arguments: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True,
                              text=True, check=True).stdout.strip()

    _ = git("init", "-q", "-b", "main")
    for key, value in (("user.name", "Review fixture"), ("user.email", "review@example.invalid"),
                       ("commit.gpgsign", "false")):
        _ = git("config", key, value)
    _ = git("commit", "-q", "--allow-empty", "-m", "First change")
    first = git("rev-parse", "HEAD")
    _ = git("commit", "-q", "--allow-empty", "-m", "Second change")
    second = git("rev-parse", "HEAD")
    origin = root / "origin.git"
    _ = git("init", "-q", "--bare", str(origin))
    _ = git("remote", "add", "origin", str(origin))
    _ = git("push", "-q", "origin", "main")
    return repo, first, second


class ReadyListTests(ProducerFixture):
    repo: Path = Path()
    promotion: Path = Path()
    listing: Path

    @override
    def setUp(self) -> None:
        super().setUp()
        seed = self.repo
        source = self.root / "source"
        _ = self.git("clone", "--branch", "main", str(self.root / "origin.git"), str(source))
        self.repo = source
        _ = self.git("config", "user.name", "Ready fixture")
        _ = self.git("config", "user.email", "ready@example.invalid")
        _ = self.doc.write_text(self.doc.read_text().replace(str(seed), str(source)))
        self.promotion = self.root / "promotion"
        _ = self.git("clone", "--branch", "main", str(self.root / "origin.git"), str(self.promotion))
        _ = self.git("checkout", "-b", "integration")
        _ = self.git("push", "origin", "integration")
        _ = self.doc.write_text(self.doc.read_text().replace("`main`", "`integration`"))

    def merge_change(self, lines: str, *, branch: str = "old-branch", day: str = "2099-01-01T12:00:00Z") -> str:
        _ = self.git("checkout", "-b", branch)
        environment = {**os.environ, "GIT_AUTHOR_DATE": day, "GIT_COMMITTER_DATE": day}
        message = f"checkpoint({branch}): visible change\n\n{lines}"
        for arguments in (("commit", "--allow-empty", "-m", message),
                          ("checkout", "integration"), ("merge", "--no-ff", branch, "-m", f"Merge {branch} into integration")):
            result = subprocess.run(["git", "-C", str(self.repo), *arguments], env=environment,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
        _ = self.git("push", "origin", "integration")
        self.record_unit_commit(self.git("rev-parse", "HEAD^2"), branch)
        return self.git("rev-parse", "HEAD")

    def promote(self) -> None:
        for arguments in (("fetch", "origin", "integration"), ("merge", "--ff-only", "origin/integration"),
                          ("push", "origin", "main")):
            result = subprocess.run(["git", "-C", str(self.promotion), *arguments],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)

    def ready(self, *options: str) -> tuple[int, str, str]:
        return run("list-ready", "--repo", str(self.repo), "--production", "top", *options)

    def live(self, merged: str, step: str = "restart") -> tuple[int, str, str]:
        return run("live", "--repo", str(self.repo), "--production", "top", "--merged", merged, "--step", step)

    def test_a_separate_promotion_makes_the_source_clone_add_each_row_once(self) -> None:
        merged = self.merge_change("Change-Review: First | Read | First result\n"
                                   + "Change-Review: Second | Read | Second result")
        _ = self.listing.write_text(LIST)
        before = self.listing.read_bytes()
        self.assertEqual(self.ready()[0], 0)
        self.assertEqual(self.listing.read_bytes(), before)
        self.assertNotEqual(self.git("rev-parse", "origin/main"), merged)
        self.promote()
        self.assertEqual(self.ready()[0], 0)
        rows = change_review.pending(self.listing)[-2:]
        self.assertEqual([row["Change"] for row in rows], ["First", "Second"])
        self.assertEqual([row["Merged"].split(" · ")[1] for row in rows], [merged[:8], merged[:8]])
        self.assertEqual([row["Producer"] for row in rows], [self.unit.id, self.unit.id])
        written = self.listing.read_bytes()
        self.assertEqual(self.ready()[0], 0)
        self.assertEqual(self.listing.read_bytes(), written)

        with self.subTest(merge_branch="local-merge-gate"):
            local = self.branch_unit("local", "local", "local-unit", "local.md")
            _ = self.merge_change("Change-Review: Local | Read | Local result", branch="local-unit")
            checkpoint = self.git("rev-parse", "HEAD^2")
            _ = self.git("commit", "--amend", "-m", f"Merge commit '{checkpoint}' into local-merge-gate")
            merged = self.git("rev-parse", "HEAD")
            _ = self.git("push", "--force", "origin", "integration")
            self.promote()
            self.assertEqual(self.ready()[0], 0)
            rows = change_review.pending(self.listing)[-3:]
            self.assertEqual([row["Change"] for row in rows], ["First", "Second", "Local"])
            self.assertEqual(rows[-1]["Merged"].split(" · ")[1], merged[:8])
            self.assertEqual(rows[-1]["Producer"], local.id)

        with self.subTest(subject="queue(7): queued change", identity="run record"):
            other_doc = self.root / "other.md"
            _ = other_doc.write_text("# Other\n")
            queued = recorded_unit("other", "queued", self.root / "queued", other_doc)
            _ = self.git("checkout", "-b", "queued-change")
            _ = self.git("commit", "--allow-empty", "-m",
                         "queue(7): queued change\n\nChange-Review: Queued | Read | Queue result")
            checkpoint = self.git("rev-parse", "HEAD")
            history = self.root / "history/runs/queued.jsonl"
            history.parent.mkdir(parents=True, exist_ok=True)
            _ = history.write_text(json.dumps({"schema_version": 1, "event_type": "checkpoint",
                "checkpoint_commit": checkpoint, "owner_staff_id": queued.id,
                "working_dir": str(self.root / "queued"), "timestamp_epoch": 1}) + "\n")
            _ = self.git("checkout", "integration")
            _ = self.git("merge", "--no-ff", "queued-change", "-m", "Merge delivery")
            _ = self.git("push", "origin", "integration")
            self.promote()
            self.assertEqual(self.ready()[0], 0)
            row = change_review.pending(self.listing)[-1]
            self.assertEqual(row["Change"], "Queued")
            self.assertEqual(row["Producer"], queued.id)
            assert isinstance(queued.parent, staff.ReportsTo)
            self.assertEqual(row["Production"], queued.parent.id)
            written = self.listing.read_bytes()
            self.assertEqual(self.ready()[0], 0)
            self.assertEqual(self.listing.read_bytes(), written)

        with self.subTest(identity="recorded during publication"):
            _ = self.git("checkout", "-b", "queued-again")
            _ = self.git("commit", "--allow-empty", "-m",
                         "queue(7): another change\n\nChange-Review: Queued again | Read | Queue result")
            _ = self.git("checkout", "integration")
            _ = self.git("merge", "--no-ff", "queued-again", "-m", "Merge old-branch into integration")
            merged = self.git("rev-parse", "HEAD")
            _ = self.git("push", "origin", "integration")
            self.promote()
            change_review.record_merge_producer(change_review.KnownMergeProducer(merged, queued.id))
            before = self.listing.read_bytes()
            _ = change_review.list_ready(self.repo, self.doc, "integration",
                known_producer=change_review.KnownMergeProducer(merged, queued.id))
            self.assertEqual(list((self.root / "history/review_producers").glob("*.json")), [])
            _ = self.listing.write_bytes(before)
            self.assertEqual(self.ready()[0], 0)
            row = change_review.pending(self.listing)[-1]
            self.assertEqual(row["Change"], "Queued again")
            self.assertEqual(row["Producer"], queued.id)
            self.assertEqual(row["Production"], queued.parent.id)

        with self.subTest(history="partial last line"):
            partial = self.root / "history/runs/partial.jsonl"
            _ = partial.write_text('{"schema_version": 1, "event_type": "checkpoint",')
            before = self.listing.read_bytes()
            self.assertEqual(self.ready()[0], 0)
            self.assertEqual(self.listing.read_bytes(), before)

        for owner_record in ("fixed", "legacy-session", "inner-unrecorded"):
            with self.subTest(owner_record=owner_record, checkpoint="repair after the visible checkpoint"):
                branch = f"repair-{owner_record}"
                _ = self.git("checkout", "-b", branch)
                _ = self.git("commit", "--allow-empty", "-m",
                             f"checkpoint(unrelated-slug): visible change\n\nChange-Review: {branch} | Read | Proof")
                if owner_record == "inner-unrecorded":
                    _ = self.git("checkout", "-b", f"side-{branch}")
                    _ = self.git("commit", "--allow-empty", "-m",
                                 f"checkpoint(other): visible change\n\nChange-Review: side-{branch} | Read | Other result")
                    _ = self.git("checkout", branch)
                    _ = self.git("merge", "--no-ff", f"side-{branch}", "-m", "Merge another unit")
                _ = self.git("commit", "--allow-empty", "-m", "fix: repair\n\nChange-Review: none — fixture repair")
                tip = self.git("rev-parse", "HEAD")
                history = self.root / f"history/runs/{branch}.jsonl"
                owner = ({"main_agent": {"session_id": self.unit.current_session}} if owner_record == "legacy-session"
                         else {"owner_staff_id": self.unit.id})
                _ = history.write_text(json.dumps({"schema_version": 1, "event_type": "checkpoint",
                    "checkpoint_commit": tip, **owner}) + "\n")
                _ = self.git("checkout", "integration")
                _ = self.git("merge", "--no-ff", branch, "-m", "Merge delivery")
                _ = self.git("push", "origin", "integration")
                self.promote()
                self.assertEqual(self.ready()[0], 0)
                rows = change_review.pending(self.listing)
                row = (next(row for row in rows if row["Change"] == branch)
                       if owner_record == "inner-unrecorded" else rows[-1])
                self.assertEqual(row["Change"], branch)
                self.assertEqual(row["Producer"], self.unit.id)
                if owner_record == "inner-unrecorded":
                    row = next(row for row in rows if row["Change"] == f"side-{branch}")
                    self.assertEqual(row["Producer"], "")
                    self.assertEqual(row["Production"], unit_lookup.doc_owner(self.doc, staff.snapshot()).id)

    def test_a_second_scan_of_an_unregistered_unit_adds_no_row_or_staff_note(self) -> None:
        _ = self.merge_change("Change-Review: First | Read | First result\n"
                              + "Change-Review: Second | Read | Second result\n"
                              + "Change-Review: Restarted | Open | New service | after restart", branch="unregistered")
        self.promote()
        code, output, error = self.ready()
        self.assertEqual((code, error), (0, ""))
        notes = [line for line in output.splitlines() if line.startswith("change review: no staff row")]
        self.assertEqual(notes, ["change review: no staff row for unit unregistered; Producer stays empty"])
        self.assertEqual([row["Change"] for row in change_review.pending(self.listing)], ["First", "Second"])
        written = self.listing.read_bytes()
        code, output, error = self.ready()
        self.assertEqual((code, error), (0, ""))
        self.assertNotIn("change review: no staff row", output)
        self.assertEqual(self.listing.read_bytes(), written)

    def test_an_edited_change_row_covers_its_merge_on_the_next_scan(self) -> None:
        _ = self.merge_change("Change-Review: Original words | Read | Result")
        self.promote()
        self.assertEqual(self.ready()[0], 0)
        _ = change_review.edit_row(self.listing, "C1", Visible("Edited words", "Read", "Result"),
                                   date(2026, 11, 7), self.doc)
        written = self.listing.read_bytes()
        self.assertEqual(self.ready(), (0, "", ""))
        self.assertEqual([row["Change"] for row in change_review.pending(self.listing)], ["Edited words"])
        self.assertEqual(self.listing.read_bytes(), written)

    def test_text_matching_reserves_its_row_before_an_edited_row_covers_another_change(self) -> None:
        _ = self.merge_change("Change-Review: First | Read | First result\n"
                              + "Change-Review: Second | Read | Second result")
        self.promote()
        self.assertEqual(self.ready()[0], 0)
        _ = change_review.edit_row(self.listing, "C1", Visible("Edited first", "Read", "First result"),
                                   date(2026, 11, 7), self.doc)
        written = self.listing.read_bytes()
        self.assertEqual(self.ready(), (0, "", ""))
        self.assertEqual([row["Change"] for row in change_review.pending(self.listing)], ["Edited first", "Second"])
        self.assertEqual(self.listing.read_bytes(), written)

    def test_one_edited_row_leaves_the_second_change_of_its_merge_uncovered(self) -> None:
        merged = self.merge_change("Change-Review: First | Read | First result\n"
                                   + "Change-Review: Second | Read | Second result")
        self.promote()
        _ = change_review.append_rows(self.listing, [Visible("First", "Read", "First result")],
                                      date(2026, 11, 7), merged[:8], self.doc, record=staff.snapshot())
        _ = change_review.edit_row(self.listing, "C1", Visible("Edited first", "Read", "First result"),
                                   date(2026, 11, 7), self.doc)
        code, output, error = self.ready()
        self.assertEqual((code, error), (0, ""))
        self.assertIn("C2", output)
        self.assertEqual([(row["#"], row["Change"]) for row in change_review.pending(self.listing)],
                         [("C1", "Edited first"), ("C2", "Second")])
        written = self.listing.read_bytes()
        self.assertEqual(self.ready(), (0, "", ""))
        self.assertEqual(self.listing.read_bytes(), written)

    def test_an_edited_ready_row_does_not_cover_an_earlier_held_change_during_live(self) -> None:
        merged = self.merge_change("Change-Review: Restarted | Open | New service | after restart\n"
                                   + "Change-Review: Plain | Read | Ready")
        self.promote()
        self.assertEqual(self.ready()[0], 0)
        self.assertEqual([row["Change"] for row in change_review.pending(self.listing)], ["Plain"])
        _ = change_review.edit_row(self.listing, "C1", Visible("Edited plain", "Read", "Ready"),
                                   date(2026, 11, 7), self.doc)
        code, output, error = self.live(merged)
        self.assertEqual((code, error), (0, ""))
        self.assertIn("C2", output)
        self.assertEqual([(row["#"], row["Change"]) for row in change_review.pending(self.listing)],
                         [("C1", "Edited plain"), ("C2", "Restarted")])
        _ = change_review.edit_row(self.listing, "C2", Visible("Edited restart", "Open", "New service"),
                                   date(2026, 11, 7), self.doc)
        written = self.listing.read_bytes()
        self.assertEqual(self.live(merged), (0, "", ""))
        self.assertEqual(self.ready(), (0, "", ""))
        self.assertEqual(self.listing.read_bytes(), written)

    def test_an_edited_second_step_row_does_not_hide_the_first_step_change(self) -> None:
        merged = self.merge_change("Change-Review: Restarted | Open | New service | after restart\n"
                                   + "Change-Review: Deployed | Open | New build | after deploy")
        self.promote()
        self.assertEqual(self.live(merged, "deploy")[0], 0)
        self.assertEqual([row["Change"] for row in change_review.pending(self.listing)], ["Deployed"])
        _ = change_review.edit_row(self.listing, "C1", Visible("Edited deploy", "Open", "New build"),
                                   date(2026, 11, 7), self.doc)
        code, output, error = self.live(merged, "restart")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("C2", output)
        self.assertEqual([(row["#"], row["Change"]) for row in change_review.pending(self.listing)],
                         [("C1", "Edited deploy"), ("C2", "Restarted")])

    def test_a_row_without_this_merge_hash_does_not_cover_different_words(self) -> None:
        earlier = self.git("rev-parse", "HEAD")
        _ = self.merge_change("Change-Review: New change | Read | New result")
        self.promote()
        for merged in ("", earlier[:8]):
            with self.subTest(merged=merged):
                self.listing.unlink(missing_ok=True)
                _ = change_review.append_rows(self.listing, [Visible("Unrelated words", "Read", "Old result")],
                                              date(2026, 11, 7), merged, self.doc, record=staff.snapshot())
                self.assertEqual(self.ready()[0], 0)
                self.assertEqual([row["Change"] for row in change_review.pending(self.listing)],
                                 ["Unrelated words", "New change"])

    def test_live_adds_only_the_held_line_once_after_main_promotion(self) -> None:
        merged = self.merge_change("Change-Review: Plain | Read | Ready\n"
                                   + "Change-Review: Restarted | Open | New service | after restart")
        self.promote()
        for layout in ("not-imported", "imported", "later-side-checkpoint"):
            with self.subTest(layout=layout):
                if layout == "imported":
                    outside_doc = self.root / "outside.md"
                    _ = outside_doc.write_text(f"# Outside\n- **Repository:** `{self.repo}`\n"
                        + "- **Merge branch:** `outside-integration`\n"
                        + f"- **Change review:** `{self.root / 'change-review - outside.md'}`\n")
                    _ = recorded_showrunner("outside", outside_doc)
                    _ = self.git("checkout", "-b", "outside-integration", "main")
                    _ = self.git("commit", "--allow-empty", "-m", "Independent outside work")
                    _ = self.git("merge", "--no-ff", "integration", "-m", "Import published work")
                    _ = self.git("push", "origin", "outside-integration")
                    _ = self.git("checkout", "integration")
                elif layout == "later-side-checkpoint":
                    _ = self.git("checkout", "-b", "later-side", "old-branch")
                    _ = self.git("commit", "--allow-empty", "-m",
                                 "checkpoint(old-branch): later phase\n\nChange-Review: none — fixture continuation")
                    _ = self.git("checkout", "outside-integration")
                    _ = self.git("merge", "--no-ff", "later-side", "-m", "Merge later phase")
                    _ = self.git("push", "origin", "outside-integration")
                    _ = self.git("checkout", "integration")
                _ = self.listing.write_text(change_review.skeleton(self.listing, self.doc, change_review.today()))
                code, output, error = self.ready()
                self.assertEqual((code, error), (0, ""))
                self.assertEqual([row["Change"] for row in change_review.pending(self.listing)], ["Plain"])
                notices = [line for line in output.splitlines() if "restart" in line]
                self.assertEqual(len(notices), 1)
                self.assertIn(merged[:8], notices[0])
                self.assertIn(f"live --merged {merged[:8]}", notices[0])
                self.assertIn(f"--repo {self.repo}", notices[0])
                self.assertIn("--production top", notices[0])
                self.assertEqual(self.live(merged)[0], 0)
                self.assertEqual([row["Change"] for row in change_review.pending(self.listing)], ["Plain", "Restarted"])
                written = self.listing.read_bytes()
                self.assertEqual(self.live(merged)[0], 0)
                self.assertEqual(self.ready()[0], 0)
                self.assertEqual(self.listing.read_bytes(), written)

    def test_the_live_command_uses_tools_outside_the_production_repository(self) -> None:
        merged = self.merge_change("Change-Review: Restarted | Open | New service | after restart")
        self.promote()
        code, output, error = self.ready()
        self.assertEqual((code, error), (0, ""))
        notice, = output.splitlines()
        command = shlex.split(notice.split("; run ", 1)[1])
        self.assertTrue(Path(command[0]).is_file(), command[0])
        self.assertTrue(Path(command[1]).is_file(), command[1])
        self.assertFalse((self.repo / "scripts").exists())
        self.assertIn(merged[:8], command)

    def test_live_releases_only_the_named_step_once(self) -> None:
        merged = self.merge_change("Change-Review: Restarted | Open | New service | after restart\n"
                                   + "Change-Review: Deployed | Open | New build | after deploy")
        self.promote()
        self.assertEqual(self.ready()[0], 0)
        self.assertEqual(self.live(merged, "restart")[0], 0)
        self.assertEqual([row["Change"] for row in change_review.pending(self.listing)], ["Restarted"])
        written = self.listing.read_bytes()
        self.assertEqual(self.live(merged, "restart"), (0, "", ""))
        self.assertEqual(self.listing.read_bytes(), written)
        self.assertEqual(self.live(merged, "deploy")[0], 0)
        self.assertEqual([row["Change"] for row in change_review.pending(self.listing)], ["Restarted", "Deployed"])
        written = self.listing.read_bytes()
        code, output, error = self.live(merged, "other")
        self.assertEqual(code, 1)
        self.assertEqual(len((output + error).splitlines()), 1)
        self.assertEqual(self.listing.read_bytes(), written)

    def test_live_without_a_merge_refuses_without_scanning(self) -> None:
        _ = self.merge_change("Change-Review: Plain | Read | Ready")
        self.promote()
        _ = self.listing.write_text(LIST)
        before = self.listing.read_bytes()
        code, output, error = run("live", "--repo", str(self.repo), "--production", "top", "--step", "restart")
        self.assertEqual(code, 1)
        self.assertEqual(len((output + error).splitlines()), 1)
        self.assertEqual(self.listing.read_bytes(), before)

    def test_a_scan_omits_the_note_for_an_already_listed_restart(self) -> None:
        merged = self.merge_change("Change-Review: Restarted | Open | New service | after restart")
        self.promote()
        self.assertEqual(self.live(merged)[0], 0)
        self.assertEqual([row["Change"] for row in change_review.pending(self.listing)], ["Restarted"])
        written = self.listing.read_bytes()
        self.assertEqual(self.ready(), (0, "", ""))
        self.assertEqual(self.listing.read_bytes(), written)

    def test_a_child_without_a_list_uses_the_root_list_and_keeps_its_credit(self) -> None:
        child_doc = self.root / "child.md"
        _ = child_doc.write_text(f"# Child\n- **Repository:** `{self.repo}`\n"
            + f"- **Showrunner checkout:** `{self.repo}`\n- **Merge branch:** `child-integration`\n")
        child_unit = recorded_unit("child", "child-unit", self.root / "child-unit", child_doc, plan="child-unit.md")
        _ = recorded_showrunner("child", child_doc, parent="top")
        _ = self.enterContext(mock.patch.object(showrunners, "timer_docs", return_value={"top": self.doc, "child": child_doc}))
        _ = self.merge_change("Change-Review: Child result | Read | Attached", branch="child-unit")
        self.promote()
        self.assertEqual(self.ready()[0], 0)
        row, = change_review.pending(self.listing)
        self.assertEqual(row["Change"], "Child result")
        self.assertEqual(row["Producer"], child_unit.id)
        self.assertIn("  Production: child\n", change_review.describe(row) + "\n")

    def test_a_returned_change_gets_a_new_row_for_a_later_checkpoint_with_the_same_words(self) -> None:
        first = self.merge_change("Change-Review: Same words | Read | First result")
        self.promote()
        self.assertEqual(self.ready()[0], 0)
        _ = change_review.record_verdict(self.listing, "C1", "returned — words", date(2026, 11, 6), self.doc)
        _ = self.branch_unit("repair", "repair", "repair-branch", "repair.md")
        second = self.merge_change("Change-Review: Same words | Read | Fixed result", branch="repair-branch")
        self.promote()
        self.assertEqual(self.ready()[0], 0)
        snapshot = change_review.review_snapshot(self.listing)
        self.assertEqual([(row["#"], row["Change"]) for row in snapshot.pending], [("C2", "Same words")])
        self.assertEqual(snapshot.pending[0]["Merged"].split(" · ")[1], second[:8])
        self.assertEqual([(row["#"], row["Verdict"]) for row in snapshot.reviewed], [("C1", "returned — words")])
        self.assertEqual(snapshot.reviewed[0]["Merged"].split(" · ")[1], first[:8])
        written = self.listing.read_bytes()
        self.assertEqual(self.ready()[0], 0)
        self.assertEqual(self.listing.read_bytes(), written)
        _ = self.git("branch", "first-scan", first)
        _ = self.git("push", "origin", "first-scan")
        self.assertEqual(self.ready("--merge-branch", "first-scan")[0], 0)
        self.assertEqual(self.listing.read_bytes(), written)

    def test_a_reviewed_row_without_a_merged_cell_covers_its_words(self) -> None:
        _ = self.merge_change("Change-Review: Older | Read | Already checked")
        self.promote()
        _ = self.listing.write_text(LIST)
        before = self.listing.read_bytes()
        self.assertEqual(self.ready()[0], 0)
        self.assertEqual(self.listing.read_bytes(), before)

    def test_live_refuses_a_merge_outside_main_without_a_list_write(self) -> None:
        merged = self.merge_change("Change-Review: Restarted | Open | New service | after restart")
        _ = self.listing.write_text(LIST)
        before = self.listing.read_bytes()
        self.assertEqual(self.live(merged), (1, "", f"{merged} is not on origin/main; no row added. "
                         + "A change goes on the list only when the user can check it now. "
                         + "If it just landed, run git fetch and retry.\n"))
        self.assertEqual(self.listing.read_bytes(), before)

    def test_a_reviewed_line_does_not_hide_another_line_from_the_same_merge(self) -> None:
        merged = self.merge_change("Change-Review: Already checked | Read | Seen\n"
                                   + "Change-Review: Still new | Read | New")
        self.promote()
        _ = change_review.append_rows(self.listing, [Visible("Already checked", "Read", "Seen")],
                                      date(2026, 11, 6), merged[:8], self.doc, record=staff.snapshot())
        _ = change_review.record_verdict(self.listing, "C1", "works", date(2026, 11, 6), self.doc)
        self.assertEqual(self.ready()[0], 0)
        snapshot = change_review.review_snapshot(self.listing)
        self.assertEqual([row["Change"] for row in snapshot.pending], ["Still new"])
        self.assertEqual([row["Change"] for row in snapshot.reviewed], ["Already checked"])
        self.assertEqual(len(change_review.review_rows(self.listing)), 2)

    def test_merges_before_the_scan_date_do_not_add_rows(self) -> None:
        _ = self.merge_change("Change-Review: Old | Read | History", day="2026-10-15T12:00:00Z")
        self.promote()
        _ = self.listing.write_text(LIST)
        before = self.listing.read_bytes()
        self.assertEqual(self.ready()[0], 0)
        self.assertEqual(self.listing.read_bytes(), before)

    def test_a_merge_early_on_the_scan_date_adds_its_row(self) -> None:
        _ = self.merge_change("Change-Review: Early | Read | Ready", day="2026-11-06T00:00:01Z")
        self.promote()
        self.assertEqual(self.ready()[0], 0)
        self.assertEqual([row["Change"] for row in change_review.pending(self.listing)], ["Early"])

    def test_a_failed_fetch_leaves_the_list_unchanged(self) -> None:
        _ = self.merge_change("Change-Review: Ready | Read | Result")
        self.promote()
        _ = self.listing.write_text(LIST)
        before = self.listing.read_bytes()
        _ = self.git("remote", "set-url", "origin", str(self.root / "missing-origin"))
        code, output, error = self.ready()
        self.assertEqual(code, 0)
        self.assertEqual(len((output + error).splitlines()), 1)
        self.assertEqual(self.listing.read_bytes(), before)

    def test_two_readiness_calls_share_the_list_lock_without_duplicate_rows(self) -> None:
        _ = self.merge_change("Change-Review: Concurrent | Read | One row")
        self.promote()
        command = [str(Path(change_review.__file__).parents[1] / "lib/py"), str(Path(change_review.__file__)),
                   "list-ready", "--repo", str(self.repo), "--production", "top"]
        descriptor = os.open(self.listing.parent, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            processes = [subprocess.Popen(command, env=os.environ.copy(), stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, text=True) for _ in range(2)]
        finally:
            os.close(descriptor)
        for process in processes:
            output, error = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, 0, output + error)
        self.assertEqual([row["Change"] for row in change_review.pending(self.listing)], ["Concurrent"])

    def test_foreign_production_and_child_handoff_rows_keep_their_lists(self) -> None:
        other_doc = self.root / "other.md"
        other_list = self.root / "change-review - other.md"
        _ = other_doc.write_text(f"# Other\n- **Repository:** `{self.repo}`\n"
            + f"- **Showrunner checkout:** `{self.repo}`\n- **Merge branch:** `other-integration`\n"
            + f"- **Change review:** `{other_list}`\n")
        _ = recorded_unit("other", "foreign", self.root / "foreign", other_doc, plan="foreign.md")
        root_doc = self.root / "root.md"
        _ = root_doc.write_text("# Root\n")
        _ = recorded_showrunner("root", root_doc)
        _ = recorded_showrunner("top", self.doc, parent="root")
        _ = recorded_showrunner("other", other_doc, parent="root")
        shared_list = self.root / "change-review - root.md"
        _ = self.doc.write_text(self.doc.read_text().replace(str(self.listing), str(shared_list)))
        _ = other_doc.write_text(other_doc.read_text().replace(str(other_list), str(shared_list)))
        self.listing = shared_list
        _ = self.git("checkout", "-b", "foreign")
        foreign_changes = (("foreign", "Foreign", ""),
                           ("unmatched-foreign-checkpoint", "Foreign unmatched",
                            "\nChange-Review: Foreign held | Read | Restarted other | after restart"))
        for branch, change, held in foreign_changes:
            _ = self.git("commit", "--allow-empty", "-m",
                         f"checkpoint({branch}): other\n\nChange-Review: {change} | Read | Other{held}")
            self.record_unit_commit(self.git("rev-parse", "HEAD"), branch)
        _ = self.git("checkout", "-b", "other-integration", "integration")
        _ = self.git("merge", "--no-ff", "foreign", "-m", "Merge foreign into other-integration")
        _ = self.git("push", "origin", "other-integration")
        foreign_merge = self.git("rev-parse", "HEAD")
        _ = self.git("checkout", "-b", "fast-forward-import", "integration")
        _ = self.git("merge", "--ff-only", "other-integration")
        _ = self.git("push", "origin", "fast-forward-import", "other-integration:main")
        fast_forward_list = self.root / "change-review - fast-forward.md"
        _ = fast_forward_list.write_text(change_review.skeleton(fast_forward_list, self.doc, change_review.today()))
        before = fast_forward_list.read_bytes()
        selections = (change_review.ReadinessScan(), change_review.CompletedStep(foreign_merge, "restart"))
        for selection in selections:
            with self.subTest(fast_forward=selection):
                if isinstance(selection, change_review.CompletedStep):
                    with self.assertRaisesRegex(change_review.ChangeReviewError, "no change waits"):
                        _ = change_review.list_ready(self.repo, self.doc, "fast-forward-import",
                                                    review_list=fast_forward_list, selection=selection)
                else:
                    self.assertEqual(change_review.list_ready(
                        self.repo, self.doc, "fast-forward-import", review_list=fast_forward_list), [])
                self.assertEqual(fast_forward_list.read_bytes(), before)
        _ = self.git("checkout", "integration")
        _ = self.git("merge", "--no-ff", "other-integration", "-m", "Merge imported checkpoints into integration")
        child_doc = self.root / "child.md"
        _ = child_doc.write_text(f"# Child\n- **Repository:** `{self.repo}`\n"
            + f"- **Showrunner checkout:** `{self.repo}`\n- **Merge branch:** `child-integration`\n- **Kind:** style\n")
        child_unit = recorded_unit("child", "child-unit", self.root / "child-unit", child_doc, plan="child-unit.md")
        child = recorded_showrunner("child", child_doc, parent="top")
        _ = self.git("checkout", "-b", "child-unit")
        _ = self.git("commit", "--allow-empty", "-m", "checkpoint(child-unit): handoff\n\nChange-Review: Child | Read | Attached")
        self.record_unit_commit(self.git("rev-parse", "HEAD"), "child-unit")
        _ = self.git("checkout", "-b", "child-integration", "integration")
        _ = self.git("merge", "--no-ff", "child-unit", "-m", "Merge child unit into child-integration")
        _ = self.git("push", "origin", "child-integration")
        _ = self.git("checkout", "integration")
        _ = self.git("merge", "--no-ff", "child-integration", "-m", "Merge child hand-off into integration")
        merged = self.merge_change("Change-Review: Own | Read | Top result\n"
                                   + "Change-Review: Own held | Read | Restarted result | after restart")
        self.promote()
        _ = self.enterContext(mock.patch.object(showrunners, "timer_docs", return_value={
            "root": root_doc, "top": self.doc, "other": other_doc, "child": child_doc}))
        self.assertEqual(self.ready()[0], 0)
        self.assertEqual([row["Change"] for row in change_review.pending(self.listing)], ["Child", "Own"])
        parent = unit_lookup.doc_owner(self.doc, staff.snapshot())
        self.assertEqual([row["Production"] for row in change_review.pending(self.listing)], [child.id, parent.id])
        isolated_list = self.root / "change-review - top-only.md"
        _ = change_review.list_ready(self.repo, self.doc, "integration", review_list=isolated_list)
        self.assertEqual([row["Change"] for row in change_review.pending(isolated_list)], ["Child", "Own"])
        self.assertEqual(run("list-ready", "--repo", str(self.repo), "--production", "other")[0], 0)
        rows = change_review.pending(self.listing)
        self.assertEqual([row["Change"] for row in rows], ["Child", "Own", *(item[1] for item in foreign_changes)])
        other = unit_lookup.doc_owner(other_doc, staff.snapshot())
        self.assertEqual([row["Production"] for row in rows], [child.id, parent.id, other.id, other.id])
        before = self.listing.read_bytes()
        self.assertEqual(self.ready()[0], 0)
        self.assertEqual(run("list-ready", "--repo", str(self.repo), "--production", "other")[0], 0)
        self.assertEqual(self.listing.read_bytes(), before)
        _ = self.git("checkout", "other-integration")
        _ = self.git("merge", "--no-ff", "origin/main", "-m", "Merge main after publication")
        _ = self.git("push", "origin", "other-integration")
        _ = self.git("checkout", "integration")
        cases = ((self.listing, ["Child", "Own", *(item[1] for item in foreign_changes)], merged[:8]),
                 (isolated_list, ["Child", "Own"], merged))
        for review_list, expected, checkpoint in cases:
            with self.subTest(review_list=review_list):
                before = review_list.read_bytes()
                self.assertEqual(change_review.list_ready(
                    self.repo, self.doc, "integration", review_list=review_list), [])
                self.assertEqual(review_list.read_bytes(), before)
                released = change_review.list_ready(
                    self.repo, self.doc, "integration", review_list=review_list,
                    selection=change_review.CompletedStep(checkpoint, "restart"))
                self.assertEqual(len(released), 1)
                rows = change_review.pending(review_list)
                self.assertEqual([row["Change"] for row in rows], [*expected, "Own held"])
                self.assertEqual(rows[-1]["Producer"], self.unit.id)
                self.assertEqual(rows[-1]["Production"], parent.id)


        with self.subTest(checkpoint="queue commit in a child hand-off"):
            _ = self.git("checkout", "child-unit")
            _ = self.git("commit", "--allow-empty", "-m",
                         "queue(7): child change\n\nChange-Review: Child queued | Read | Attached queue")
            _ = self.git("checkout", "child-integration")
            _ = self.git("merge", "--no-ff", "child-unit", "-m", "Merge delivery")
            inner = self.git("rev-parse", "HEAD")
            change_review.record_merge_producer(change_review.KnownMergeProducer(inner, child_unit.id))
            _ = self.git("checkout", "integration")
            _ = self.git("merge", "--no-ff", "child-integration", "-m", "Merge hand-off")
            _ = self.git("push", "origin", "integration", "child-integration")
            self.promote()
            self.assertEqual(self.ready()[0], 0)
            row = change_review.pending(self.listing)[-1]
            self.assertEqual(row["Change"], "Child queued")
            self.assertEqual(row["Producer"], child_unit.id)
            self.assertEqual(row["Production"], child.id)
            before = self.listing.read_bytes()
            self.assertEqual(self.ready()[0], 0)
            self.assertEqual(self.listing.read_bytes(), before)


class MergedAddTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    path: Path = Path()
    merged: str = ""

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo, self.merged, second = origin_commits(self.root)
        self.path = self.root / "change-review.md"
        _ = self.path.write_text(LIST, encoding="utf-8")
        _ = subprocess.run(["git", "-C", str(self.repo), "switch", "-qc", "side", second],
                           capture_output=True, text=True, check=True)
        _ = subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "--allow-empty", "-m", "Side change"],
                           capture_output=True, text=True, check=True)

    def add(self, *options: str) -> tuple[int, str, str]:
        return run("add", "--repo", str(self.repo), "--production", DOC, "--list", str(self.path),
                   "--change", "Ready change", "--check", "Read", "--see", "The change", *options)

    def test_add_accepts_an_ancestor_of_origin_main(self) -> None:
        self.assertEqual(self.add("--merged", self.merged), (0, "C81\n", ""))
        row = change_review.pending(self.path)[-1]
        self.assertEqual(row["Change"], "Ready change")
        self.assertEqual(row["Merged"], f"{change_review.today().isoformat()} · {self.merged}")

    def assert_merge_refused(self, merged: str) -> None:
        before = self.path.read_bytes()
        self.assertEqual(self.add("--merged", merged), (1, "",
            f"{merged} is not on origin/main; no row added. "
            + "A change goes on the list only when the user can check it now. "
            + "If it just landed, run git fetch and retry.\n"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_add_refuses_a_commit_on_a_side_branch(self) -> None:
        side = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        self.assert_merge_refused(side)

    def test_add_refuses_an_unknown_hash(self) -> None:
        self.assert_merge_refused("f" * 40)

    def test_add_without_merged_does_not_require_a_repository(self) -> None:
        self.repo = self.root / "not-a-repository"
        self.assertEqual(self.add(), (0, "C81\n", ""))
        row = change_review.pending(self.path)[-1]
        self.assertEqual(row["Change"], "Ready change")
        self.assertEqual(row["Merged"], change_review.today().isoformat())


class ActionTest(unittest.TestCase):
    """add, show, verdict and list-path, on a Hana-shaped list and on a list not yet written."""

    path: Path = Path()
    fresh: Path = Path()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "change-review.md"
        _ = self.path.write_text(LIST, encoding="utf-8")
        self.fresh = Path(directory.name) / "change-review" / "change-review - natedev.md"

    def test_the_first_row_creates_a_missing_list_from_the_skeleton(self) -> None:
        rows = change_review.append_rows(self.fresh, [Visible("A", "B", "C")], date(2026, 10, 15), "", PRODUCTION, record=staff.snapshot())
        self.assertEqual(rows, [f"| C1 | A | 2026-10-15 | B. You see: C | {DEMO_ID} |  |"])
        self.assertEqual(self.fresh.read_text(encoding="utf-8").split("\n\n## To review")[0], "\n".join([
            "---", 'date_created: "[[2026-10-15]]"', 'date_modified: "[[2026-10-15]]"', "productions:",
            '  - "~/worktrees/demo-trunk/docs/plans/demo-production.md"', "showrunner: natedev", "---", "",
            "# natedev showrunner: changes to review", "",
            'Ask for "change review" any time to walk it one change at a time. A reviewed row moves to Reviewed '
            + "with your verdict."]))
        self.assertIn(f"| --- | --- | --- | --- | --- | --- |\n| C1 | A | 2026-10-15 | B. You see: C | {DEMO_ID} |  |\n\n## Reviewed", self.fresh.read_text(encoding="utf-8"))

    def test_add_takes_the_next_number_and_fills_the_mac_build_column_only_where_the_table_has_one(self) -> None:
        repo, *commits = origin_commits(self.path.parent)
        merged = commits[-1][:8]
        self.assertEqual(run("add", "--repo", str(repo), "--production", DOC, "--list", str(self.path), "--change", "Tabs | spaces", "--check", "Look", "--see", "It",
                             "--merged", merged), (0, "C81\n", ""))
        day = change_review.today().isoformat()
        self.assertIn(f"| C81 | Tabs \\| spaces | {day} · {merged} | not yet | Look. You see: It | {DEMO_ID} |  |\n\n## Questions",
                      self.path.read_text(encoding="utf-8"))
        self.assertEqual(run("add", "--production", DOC, "--list", str(self.fresh), "--change", "A", "--check", "B", "--see", "C")[1], "C1\n")
        self.assertIn(f"| C1 | A | {day} | B. You see: C |", self.fresh.read_text(encoding="utf-8"))

    def test_add_refuses_a_second_row_for_the_same_merge_and_change_in_a_short_or_long_hash(self) -> None:
        repo, first, second = origin_commits(self.path.parent)
        short, long_hash, other = first[:7], first[:8], second[:8]
        add = ("add", "--repo", str(repo), "--production", DOC, "--list", str(self.path), "--change", "Tabs", "--check", "Look", "--see", "It")
        self.assertEqual(run(*add, "--merged", short)[:2], (0, "C81\n"))
        before = self.path.read_text(encoding="utf-8")
        for merged in (long_hash, short):
            with self.subTest(merged=merged):
                code, output, error = run(*add, "--merged", merged)
                self.assertEqual((code, output), (1, ""))
                self.assertIn(f"C81 already holds that change from merge {merged}", error)
                self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        # A different merge or change receives a new row.
        self.assertEqual(run(*add, "--merged", other)[:2], (0, "C82\n"))
        self.assertEqual(run(*add[:-6], "--change", "Spaces", "--check", "Look", "--see", "It", "--merged", long_hash)[:2],
                         (0, "C83\n"))

    def test_a_style_productions_change_is_credited_to_its_parent(self) -> None:
        parent = self.path.parent / "parent-production.md"
        _ = parent.write_text("- **Merge branch:** main\n", encoding="utf-8")
        style = self.path.parent / "parent-style-production.md"
        _ = style.write_text("- **Kind:** style\n- **Parent production:** /stale/parent-production.md\n", encoding="utf-8")
        owner = fixture_member("showrunner", name="parent", doc=staff.LiveDoc(remote.local_machine(), parent))
        _ = fixture_member("showrunner", name="parent-style", parent=owner.id,
                           doc=staff.LiveDoc(remote.local_machine(), style))
        record = staff.snapshot()
        self.assertEqual(change_review.credited(style, record), parent)
        self.assertEqual(change_review.credited(parent, record), parent)
        rows = change_review.append_rows(self.path, [Visible("A", "B", "C")], date(2026, 10, 27), "216c4e1d", style,
                                         owners=[style], record=staff.snapshot())
        self.assertEqual(next(row for row in change_review.pending(self.path)
                              if row["#"] == rows[0].split()[1])["Production"], owner.id)
        rows = change_review.append_rows(self.path, [Visible("D", "B", "C")], date(2026, 10, 27), "", style, record=staff.snapshot())
        self.assertEqual(next(row for row in change_review.pending(self.path)
                              if row["#"] == rows[0].split()[1])["Production"], owner.id)

    def test_add_holds_each_folder_by_the_new_row(self) -> None:
        held = self.path.parent / "shown-repo"
        held.mkdir()
        with mock.patch.dict(os.environ, {scratch.HOLDS_ENV: str(self.path.parent / "holds.json")}):
            code, output, _ = run("add", "--production", DOC, "--list", str(self.path), "--change", "A", "--check", "B",
                                  "--see", "C", "--hold", str(held))
            self.assertEqual(code, 0)
            row = output.splitlines()[0]
            self.assertEqual(scratch.holds()[os.path.realpath(held)]["task"], f"row:{change_review._owner(self.path)}-{row}")  # pyright: ignore[reportPrivateUsage]

    def test_back_to_back_adds_in_one_shell_each_append_the_next_number(self) -> None:
        changes = [Visible("cargo-handler nests a showrunner | inside its parent", "open `cargo-handler` on natedev",
                           "the showrunner tile sits inside its parent"),
                   Visible("tile titles carry the role: showrunner, unit director", "read the tile titles",
                           "each title names its role"),
                   Visible("cargo-tile no longer panics at startup", "start it in a natedev terminal",
                           "~/.config/cargo-tile/panic.log gains nothing"),
                   Visible("cargo-berth gives each phase its own reservation", "run `cargo-berth board | grep phase`",
                           "one line per phase")]
        repo, *commits = origin_commits(self.path.parent)
        script = str(Path(change_review.__file__))
        python = str(Path(__file__).resolve().parents[1] / "lib/py")
        for path, first in ((self.fresh, 1), (self.path, 81)):
            numbers = [subprocess.run([python, script, "add", "--repo", str(repo), "--production", DOC, "--list", str(path),
                                       "--change", item.change, "--check", item.check, "--see", item.see,
                                       "--merged", commits[-1][:8]],
                                      capture_output=True, text=True, check=True).stdout.strip()
                       for item in changes]
            self.assertEqual(numbers, [f"C{number}" for number in range(first, first + len(changes))])
            rows = [row for row in change_review.pending(path) if row["#"] in numbers]
            self.assertEqual([(row["#"], row["Change"], row[change_review.CHECK]) for row in rows],
                             [(number, item.change, item.how()) for number, item in zip(numbers, changes)])

    def test_add_refuses_an_empty_or_multi_line_part_and_writes_nothing(self) -> None:
        for change, check, see in (("", "Look", "It"), ("A", " ", "It"), ("A\nB", "Look", "It"), ("A", "Look", "")):
            code, _, error = run("add", "--production", DOC, "--list", str(self.path), "--change", change, "--check", check,
                                 "--see", see)
            self.assertEqual(code, 1)
            self.assertIn("needs one line of words", error)
        self.assertEqual(self.path.read_text(encoding="utf-8"), LIST)

    def test_add_without_see_is_refused_and_writes_nothing(self) -> None:
        code, output, error = run("add", "--production", DOC, "--list", str(self.path), "--change", "A", "--check", "Look")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("--see needs one line of words", error)
        self.assertEqual(self.path.read_text(encoding="utf-8"), LIST)

    def test_edit_rewrites_one_row_in_place_and_keeps_its_id_merged_cell_and_place(self) -> None:
        self.assertEqual(run("edit", "--production", DOC, "--list", str(self.path), "--row", "C76",
                             "--change", "Panels | shields stay", "--check", "Run `hana` in a natedev terminal",
                             "--see", "Each panel keeps its shield"),
                         (0, "| C76 | Panels \\| shields stay | 2026-10-14 | not yet | Run `hana` in a natedev terminal. "
                          + "You see: Each panel keeps its shield |\n", ""))
        # The write stamps frontmatter above the list; every line below it is the list with the one row changed.
        self.assertEqual(self.path.read_text(encoding="utf-8").split("\n---\n\n", 1)[1], LIST.replace(
            "| C76 | Panels keep shields | 2026-10-14 | not yet | Launch Hana |",
            "| C76 | Panels \\| shields stay | 2026-10-14 | not yet | Run `hana` in a natedev terminal. "
            + "You see: Each panel keeps its shield |"))

    def test_edit_keeps_check_line_breaks_and_pipes_in_one_table_row(self) -> None:
        check = 'Steps:\r\n- Run A | B\r\n- Run C'
        see = 'Results:\n1. One\n2. Two | three'
        code, output, error = run('edit', '--production', DOC, '--list', str(self.path), '--row', 'C76',
                                  '--change', 'Panels keep shields', '--check', check, '--see', see)
        self.assertEqual((code, error), (0, ''))
        expected = 'Steps:\n- Run A | B\n- Run C. You see: Results:\n1. One\n2. Two | three'
        row = next(row for row in change_review.pending(self.path) if row['#'] == 'C76')
        self.assertEqual(row[change_review.CHECK], expected)
        lines = [line for line in self.path.read_text().splitlines() if line.startswith('| C76 |')]
        self.assertEqual(lines, [output.rstrip('\n')])
        self.assertIn('Steps:<br>- Run A \\| B<br>- Run C.', lines[0])
        self.assertIn('2. Two \\| three', lines[0])

    def test_edit_stores_each_line_separator_in_one_table_row(self) -> None:
        for separator in ('\n', '\r\n', '\r', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029'):
            with self.subTest(separator=repr(separator)):
                _ = self.path.write_text(LIST, encoding='utf-8')
                code, output, error = run('edit', '--production', DOC, '--list', str(self.path), '--row', 'C76',
                                          '--change', 'Panels keep shields', '--check', f'First{separator}- Second',
                                          '--see', 'Both remain')
                self.assertEqual((code, error), (0, ''))
                text = self.path.read_text(encoding='utf-8')
                self.assertIn('| First<br>- Second. You see: Both remain |', text)
                self.assertEqual([line for line in text.splitlines() if line.startswith('| C76 |')],
                                 [output.rstrip('\n')])
                row = next(row for row in change_review.pending(self.path) if row['#'] == 'C76')
                self.assertEqual(row[change_review.CHECK], 'First\n- Second. You see: Both remain')

    def test_saved_verdicts_keep_typed_break_tags_and_line_breaks_unchanged(self) -> None:
        for verdict in ('The <br> tag is visible', r'The <\br> tag is visible',
                        r'The <\\br> tag is visible', 'a <br> b | c', 'x\n<br>y', 'x\\\ny'):
            with self.subTest(verdict=verdict):
                _ = self.path.write_text(LIST, encoding='utf-8')
                _ = change_review.record_verdict(self.path, 'C76', verdict, date(2026, 11, 5), PRODUCTION)
                row = next(row for row in change_review.review_rows(self.path) if row['#'] == 'C76')
                self.assertEqual(row['Verdict'], verdict)
                before = self.path.read_bytes()
                repeated = change_review.record_verdict(self.path, 'C76', verdict, date(2026, 11, 5), PRODUCTION)
                self.assertIsInstance(repeated, change_review.VerdictAlreadyRecorded)
                self.assertEqual(self.path.read_bytes(), before)

    def test_a_multiline_check_stays_with_a_classified_row_after_a_verdict(self) -> None:
        item = Visible('Panels keep shields', '- Run A\n- Run B', 'Both remain')
        _ = change_review.edit_row(self.path, 'C76', item, date(2026, 11, 5), PRODUCTION)
        _ = change_review.set_row_kind(self.path, 'C76', 'See in action', PRODUCTION)
        _ = change_review.record_verdict(self.path, 'C76', 'not works', date(2026, 11, 5), PRODUCTION)
        saved = next(row for row in change_review.review_rows(self.path) if row['#'] == 'C76')
        self.assertEqual(saved[change_review.CHECK], item.how())
        self.assertEqual(saved['Verdict'], 'not works')

    def test_edit_refuses_a_row_not_to_review_and_a_missing_part(self) -> None:
        for row in ("C70", "C99", "76"):
            code, output, error = run("edit", "--production", DOC, "--list", str(self.path), "--row", row,
                                      "--change", "A", "--check", "B", "--see", "C")
            self.assertEqual((code, output), (1, ""), row)
            self.assertEqual(len(error.splitlines()), 1, row)
        self.assertIn("C99 is not on the list to review", run("edit", "--production", DOC, "--list", str(self.path),
                                                              "--row", "C99", "--change", "A", "--check", "B",
                                                              "--see", "C")[2])
        self.assertIn("--see needs one line of words", run("edit", "--production", DOC, "--list", str(self.path),
                                                           "--row", "C76", "--change", "A", "--check", "B")[2])
        self.assertEqual(self.path.read_text(encoding="utf-8"), LIST)

    def test_show_prints_only_the_first_row_to_review_and_leaves_the_list_as_it_is(self) -> None:
        self.assertEqual(run("show", "--list", str(self.path)), (0, (
            "C76 Panels keep shields\n  How to check: Launch Hana\n  Merged: 2026-10-14\n  On the Mac build: not yet\n\n"
            + "1 more to review\n"), ""))
        self.assertEqual(self.path.read_text(encoding="utf-8"), LIST)
        self.assertEqual(run("show", "--list", str(self.fresh)), (0, "nothing to review\n", ""))
        self.assertFalse(self.fresh.parent.exists())

    def test_the_most_urgent_row_shows_first_and_the_shown_row_stays_until_its_verdict(self) -> None:
        def add(*rank: str) -> str:
            code, out, error = run("add", "--production", DOC, "--list", str(self.fresh), "--change", f"change {len(rank)}",
                                   "--check", "B", "--see", "C", *rank)
            self.assertEqual((code, error), (0, ""))
            return out.strip()

        def shown() -> str:
            return run("show", "--list", str(self.fresh))[1].split(" ", 1)[0]

        normal = add()
        self.assertEqual(shown(), normal)
        blocks = add("--urgency", "blocks")
        soon = add("--urgency", "soon", "--due", "2026-10-22")
        # The shown row stays current while a more urgent one waits.
        self.assertEqual(shown(), normal)
        text = self.fresh.read_text(encoding="utf-8")
        self.assertLess(text.index(f"| {normal} |"), text.index(f"| {blocks} |"))
        self.assertLess(text.index(f"| {blocks} |"), text.index(f"| {soon} |"))
        self.assertIn(f"| {soon} | change 4 | ", text)
        self.assertIn("| soon, due 2026-10-22 |", text)
        _ = run("verdict", "--production", DOC, "--list", str(self.fresh), "--row", normal, "--verdict", "works")
        self.assertEqual(shown(), blocks)
        # The user's mark takes the slot after the shown row.
        self.assertEqual(run("mark-important", "--production", DOC, "--list", str(self.fresh), "--row", soon)[0], 0)
        self.assertIn(f"| {soon} | change 4 |", self.fresh.read_text(encoding="utf-8"))
        self.assertEqual(shown(), blocks)
        _ = run("verdict", "--production", DOC, "--list", str(self.fresh), "--row", blocks, "--verdict", "works")
        self.assertEqual(shown(), soon)

    def test_a_row_waits_for_its_prerequisite_and_a_bad_level_is_refused(self) -> None:
        _ = run("add", "--production", DOC, "--list", str(self.path), "--change", "A", "--check", "B", "--see", "C",
                "--urgency", "blocks", "--after", "C77")
        text = self.path.read_text(encoding="utf-8")
        self.assertLess(text.index("| C77 |"), text.index("| C81 |"))
        self.assertLess(text.index("| C81 |"), text.index("| C76 |"))
        self.assertIn("| Urgency | Production | Producer |\n| --- | --- | --- | --- | --- | --- | --- | --- |", text)
        self.assertIn("| C76 | Panels keep shields | 2026-10-14 | not yet | Launch Hana | |", text)
        self.assertEqual(run("add", "--production", DOC, "--list", str(self.path), "--change", "A", "--check", "B",
                             "--see", "C", "--urgency", "soon")[0], 1)
        self.assertEqual(run("add", "--production", DOC, "--list", str(self.path), "--change", "A", "--check", "B",
                             "--see", "C", "--after", "later")[0], 1)

    def test_a_verdict_moves_the_row_to_the_top_of_reviewed(self) -> None:
        self.assertEqual(change_review.record_verdict(self.path, "C76", "Works, thanks", date(2026, 10, 15), PRODUCTION),
                         change_review.VerdictRecorded(change_review.RecordedMerge("2026-10-14")))
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("| C76 | Panels keep shields | 2026-10-14", text.split("## Reviewed")[0])
        self.assertIn("| # | Change | Verdict | Date | Merged |", text)
        self.assertIn("| --- | --- | --- | --- | --- |\n| C76 | Panels keep shields | Works, thanks | 2026-10-15 | 2026-10-14 |\n| C70 |", text)

    def test_edit_and_verdict_replacement_failures_keep_the_list_and_rerun_once(self) -> None:
        for action in ("edit", "verdict"):
            with self.subTest(action=action):
                _ = self.path.write_text(LIST, encoding="utf-8")
                before = self.path.read_bytes()
                options = (("--change", "Updated panels", "--check", "Open the panel", "--see", "Updated shield")
                           if action == "edit" else ("--verdict", "works"))
                command = (action, "--production", DOC, "--list", str(self.path), "--row", "C76", *options)
                real_replace = os.replace

                def interrupted(source: Path, destination: Path) -> None:
                    if Path(destination) == self.path:
                        raise OSError("list replacement stopped")
                    real_replace(source, destination)

                with mock.patch.object(os, "replace", side_effect=interrupted):
                    code, _, error = run(*command)
                self.assertEqual(code, 1)
                self.assertIn("list replacement stopped", error)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(run(*command)[0], 0)
                self.assertEqual(self.path.read_text().count("| C76 |"), 1)
                self.assertEqual([row["#"] for row in change_review.pending(self.path)],
                                 ["C76", "C77"] if action == "edit" else ["C77"])

    def test_verdict_rerun_finishes_scratch_and_replays_merged_result(self) -> None:
        for verdict in ("works", "still missing the shield"):
            with self.subTest(verdict=verdict), mock.patch.dict(os.environ, {
                copy_record.ROOT_ENV: str(self.path.parent / "scratch"),
                scratch.HOLDS_ENV: str(self.path.parent / "holds.json"),
            }):
                _ = self.path.write_text(LIST, encoding="utf-8")
                task = f"row:{self.path.stem}-C76"
                folder = scratch.new(task, "verdict")
                _ = change_review.hold_for_row(self.path, "C76", [str(folder)])
                command = ("verdict", "--production", DOC, "--list", str(self.path), "--row", "C76", "--verdict", verdict)
                with mock.patch.object(change_review, "end_row_scratch", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        _ = run(*command)
                self.assertTrue(folder.is_dir())
                self.assertIn(os.path.realpath(folder), scratch.holds())
                self.assertNotIn("C76", [row["#"] for row in change_review.pending(self.path)])
                before = self.path.read_bytes()
                code, output, error = run(*command)
                self.assertEqual((code, error), (0, ""))
                self.assertIn("C76 already has this verdict (", output)
                self.assertIn("C76 landed\n" if verdict == "works" else
                              "C76 returns to its unit (merged 2026-10-14)\n", output)
                self.assertIn(f"removed {folder}\n", output)
                self.assertFalse(folder.exists())
                self.assertNotIn(os.path.realpath(folder), scratch.holds())
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.path.read_text().split("## Reviewed")[1].count("| C76 |"), 1)

    def test_conflicting_verdict_keeps_reviewed_row_and_finishes_scratch(self) -> None:
        with mock.patch.dict(os.environ, {copy_record.ROOT_ENV: str(self.path.parent / "scratch"),
                                          scratch.HOLDS_ENV: str(self.path.parent / "holds.json")}):
            folder = scratch.new(f"row:{self.path.stem}-C76", "conflict")
            _ = change_review.hold_for_row(self.path, "C76", [str(folder)])
            with mock.patch.object(change_review, "end_row_scratch", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    _ = run("verdict", "--production", DOC, "--list", str(self.path), "--row", "C76", "--verdict", "works")
            before = self.path.read_bytes()
            code, output, error = run("verdict", "--production", DOC, "--list", str(self.path), "--row", "C76", "--verdict", "still broken")
            self.assertEqual(code, 1)
            self.assertIn('C76 has the verdict "works" from ', error)
            self.assertIn("this verdict is not recorded", error)
            self.assertIn(f"removed {folder}", output)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertFalse(folder.exists())
            self.assertNotIn(os.path.realpath(folder), scratch.holds())

    def test_missing_merge_is_explicit_for_new_and_earlier_reviewed_rows(self) -> None:
        _ = self.path.write_text(LIST.replace("Panels keep shields | 2026-10-14", "Panels keep shields |"))
        recorded = change_review.record_verdict(self.path, "C76", "not yet", date(2026, 10, 15), PRODUCTION)
        self.assertEqual(recorded, change_review.VerdictRecorded(change_review.MergeNotRecorded()))
        repeated = change_review.record_verdict(self.path, "C76", "not yet", date(2026, 10, 16), PRODUCTION)
        self.assertIsInstance(repeated, change_review.VerdictAlreadyRecorded)
        if isinstance(repeated, change_review.VerdictAlreadyRecorded):
            self.assertIsInstance(repeated.merged, change_review.MergeNotRecorded)
        code, output, error = run("verdict", "--production", DOC, "--list", str(self.path), "--row", "C76", "--verdict", "not yet")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("C76 returns to its unit (merged not recorded)", output)
        _ = self.path.write_text(LIST)
        earlier = change_review.record_verdict(self.path, "C70", "works", date(2026, 10, 16), PRODUCTION)
        self.assertIsInstance(earlier, change_review.VerdictAlreadyRecorded)
        if isinstance(earlier, change_review.VerdictAlreadyRecorded):
            self.assertIsInstance(earlier.merged, change_review.MergeNotRecorded)
        self.assertEqual(self.path.read_text(), LIST)

    def test_works_or_fine_lands_and_any_other_verdict_returns_to_the_unit(self) -> None:
        self.assertEqual(run("verdict", "--production", DOC, "--list", str(self.path), "--row", "C76", "--verdict", "fine"),
                         (0, "C76 landed\n", ""))
        self.assertEqual(run("verdict", "--production", DOC, "--list", str(self.path), "--row", "C77", "--verdict", "still the old font | x"),
                         (0, "C77 returns to its unit (merged 2026-10-14)\n", ""))
        self.assertIn("| C77 | Fonts redraw | still the old font \\| x |", self.path.read_text(encoding="utf-8"))
        self.assertTrue(change_review.lands("Works: it does"))
        self.assertFalse(change_review.lands("not fine"))
        for verdict in ("okay c17 works", "ok fine", "Works: it does", "OK C17 FINE: still checked"):
            self.assertTrue(change_review.lands(verdict), verdict)
        for verdict in ("still pushes", "it works but still pushes", "no, broken", "doesn't work", "yes works",
                        "worksheets", "finely", "c17 okay works"):
            self.assertFalse(change_review.lands(verdict), verdict)

    def test_verdict_start_sets_the_printed_outcome_and_keeps_the_users_words(self) -> None:
        for verdict, outcome in (("okay c17 works", "C76 landed"),
                                 ("works: checked, no empty cwd", "C76 landed"),
                                 ("not works", "C76 returns to its unit (merged 2026-10-14)")):
            with self.subTest(verdict=verdict):
                _ = self.path.write_text(LIST, encoding="utf-8")
                self.assertEqual(run("verdict", "--production", DOC, "--list", str(self.path), "--row", "C76",
                                     "--verdict", verdict), (0, outcome + "\n", ""))
                text = self.path.read_text(encoding="utf-8")
                self.assertNotIn("| C76 |", text.split("## Reviewed")[0])
                self.assertIn(f"| C76 | Panels keep shields | {verdict} | {change_review.today().isoformat()} "
                              + "| 2026-10-14 |\n| C70 |", text)

    def test_a_verdict_removes_the_rows_scratch_folders_either_way(self) -> None:
        demo = scratch.new("row:natedev-C1", "demo", visible=True)
        copy = scratch.new("row:natedev-C2", "copy")
        kept = scratch.new("row:natedev-C3")
        _ = change_review.append_rows(self.fresh, [Visible(name, "B", "C") for name in "XYZ"], date(2026, 10, 15), "",
                                      PRODUCTION, record=staff.snapshot())
        self.assertEqual(run("verdict", "--production", DOC, "--list", str(self.fresh), "--row", "C1", "--verdict", "works"),
                         (0, f"C1 landed\nremoved {demo}\n", ""))
        self.assertEqual(run("verdict", "--production", DOC, "--list", str(self.fresh), "--row", "C2", "--verdict", "no"),
                         (0, f"C2 returns to its unit (merged 2026-10-15)\nremoved {copy}\n", ""))
        self.assertFalse(demo.exists() or copy.exists())
        self.assertTrue(kept.is_dir())

    def test_a_verdict_for_a_row_not_to_review_is_refused(self) -> None:
        for row in ("C99", "76"):
            code, output, error = run("verdict", "--production", DOC, "--list", str(self.path), "--row", row, "--verdict", "works")
            self.assertEqual((code, output), (1, ""), row)
            self.assertEqual(len(error.splitlines()), 1, row)
        self.assertEqual(self.path.read_text(encoding="utf-8"), LIST)

    def attached_lists(self) -> Path:
        earlier = self.path.with_name("change-review - child.md")
        _ = self.path.write_text(LIST.replace("C76", "C9"), encoding="utf-8")
        _ = earlier.write_text(LIST.replace("C76", "C9").replace("Panels keep shields", "Earlier change"),
                               encoding="utf-8")
        _ = self.enterContext(mock.patch.object(change_review, "lists_directory", return_value=self.path.parent))
        _ = self.enterContext(mock.patch.object(change_review, "production_list", return_value=self.path))
        return earlier

    def test_list_named_verdict_changes_only_that_list_and_ends_its_scratch(self) -> None:
        earlier = self.attached_lists()
        before = self.path.read_bytes()
        scratch_folder = scratch.new("row:child-C9", "earlier")
        current_scratch = scratch.new("row:current-C9", "current")
        self.assertEqual(run("verdict", "--production", DOC, "--row", "child/C9", "--verdict", "works"),
                         (0, f"child/C9 landed\nremoved {scratch_folder}\n", ""))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn("C9", {row["#"] for row in change_review.pending(earlier)})
        self.assertIn("| C9 | Earlier change | works |", earlier.read_text())
        self.assertNotIn("child/C9", earlier.read_text())
        self.assertFalse(scratch_folder.exists())
        self.assertTrue(current_scratch.exists())

    def test_list_named_important_mark_changes_only_that_lists_local_row(self) -> None:
        earlier = self.attached_lists()
        before = self.path.read_bytes()
        code, output, error = run("mark-important", "--production", DOC, "--row", "child/C9")
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(output.startswith("| C9 | Earlier change |"))
        self.assertEqual(self.path.read_bytes(), before)
        entry = next(entry for entry in change_review.row_entries(change_review.pending(earlier)) if entry.key == "C9")
        self.assertEqual(entry.urgency.level, urgency.USER)
        self.assertNotIn("child/C9", earlier.read_text())

    def test_list_named_edit_accepts_a_matching_list_and_keeps_local_row_text(self) -> None:
        earlier = self.attached_lists()
        before = self.path.read_bytes()
        code, output, error = run("edit", "--production", DOC, "--list", str(earlier), "--row", "child/C9",
                                  "--change", "Revised change", "--check", "Run it", "--see", "Revised result")
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(output.startswith("| C9 | Revised change |"))
        self.assertEqual(self.path.read_bytes(), before)
        row = next(row for row in change_review.pending(earlier) if row["#"] == "C9")
        self.assertEqual((row["Change"], row[change_review.CHECK]), ("Revised change", "Run it. You see: Revised result"))
        self.assertNotIn("child/C9", earlier.read_text())

    def test_list_named_row_refuses_a_different_explicit_list_without_writes(self) -> None:
        earlier = self.attached_lists()
        before = {path: path.read_bytes() for path in (self.path, earlier)}
        for action, arguments in (("verdict", ("--verdict", "works")), ("mark-important", ()),
                                  ("edit", ("--change", "Changed", "--check", "Run it", "--see", "A result"))):
            with self.subTest(action=action):
                code, output, error = run(action, "--production", DOC, "--list", str(self.path),
                                          "--row", "child/C9", *arguments)
                self.assertEqual((code, output), (1, ""))
                self.assertEqual(len(error.splitlines()), 1)
                self.assertIn("differs from --list", error)
                self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_a_write_waits_for_the_lock_and_concurrent_writes_keep_every_row(self) -> None:
        held = os.open(self.path.parent, os.O_RDONLY)
        fcntl.flock(held, fcntl.LOCK_EX)
        writers = [threading.Thread(target=change_review.append_rows,
                                    args=(self.path, [Visible(f"Row {index}", "Look", "It")], date(2026, 10, 15), "", PRODUCTION),
                                    kwargs={"record": staff.snapshot()})
                   for index in range(8)]
        blocked = threading.Event()
        real_flock = fcntl.flock

        def observe_lock(descriptor: int, operation: int) -> None:
            if operation == fcntl.LOCK_EX:
                try:
                    real_flock(descriptor, operation | fcntl.LOCK_NB)
                except BlockingIOError:
                    blocked.set()
                else:
                    return
            real_flock(descriptor, operation)

        with mock.patch.object(fcntl, "flock", side_effect=observe_lock):
            try:
                for writer in writers:
                    writer.start()
                self.assertTrue(blocked.wait(TEST_HANG_GUARD_SECS), "writer did not reach the held lock")
                self.assertTrue(writers[0].is_alive())
                self.assertEqual(self.path.read_text(encoding="utf-8"), LIST)
            finally:
                os.close(held)
                for writer in writers:
                    writer.join(TEST_HANG_GUARD_SECS)
        rows = [row for row in self.path.read_text(encoding="utf-8").splitlines() if "| Row " in row]
        self.assertEqual(sorted(row.split(" ")[1] for row in rows), [f"C{number}" for number in range(81, 89)])

    def test_list_path_prints_the_list_a_production_doc_names(self) -> None:
        self.fresh = self.fresh.with_name("change-review - fixture-list.md")
        _ = self.enterContext(mock.patch.object(change_review, "lists_directory", return_value=self.fresh.parent))
        doc = self.path.with_name("production.md")
        _ = doc.write_text(f"# p\n\n- **Change review:** `{self.fresh}`\n", encoding="utf-8")
        _ = fixture_member("showrunner", name="fixture-list", doc=staff.LiveDoc(remote.local_machine(), doc))
        self.assertEqual(run("list-path", "--production", "fixture-list"), (0, f"{self.fresh}\n", ""))
        _ = doc.write_text("# p\n", encoding="utf-8")
        self.assertEqual(run("list-path", "--production", "fixture-list")[:2], (1, ""))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
            self.assertIn("pass --production", run("list-path")[2])


class CommitTest(unittest.TestCase):
    """Each write commits the list alone when it sits in a git work tree, in a scratch repository."""

    repo: Path = Path()
    path: Path = Path()

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *arguments], capture_output=True, text=True,
                              check=True).stdout.strip()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name) / "vault"
        self.repo.mkdir()
        _ = self.git("init", "-q", "-b", "main")
        for key, value in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            _ = self.git("config", key, value)
        _ = (self.repo / "note.md").write_text("note\n", encoding="utf-8")
        _ = self.git("add", "note.md")
        _ = self.git("commit", "-q", "-m", "note")
        self.path = self.repo / "change-review" / "change-review - hana.md"

    def last(self) -> tuple[str, list[str]]:
        """The newest commit's subject and the files it changes."""
        return self.git("log", "-1", "--format=%s"), self.git("show", "--name-only", "--format=", "HEAD").splitlines()

    def test_add_commits_a_new_list_and_then_the_existing_one(self) -> None:
        source, *commits = origin_commits(self.repo.parent)
        merged = commits[-1][:8]
        self.assertEqual(run("add", "--production", DOC, "--list", str(self.path), "--change", "A", "--check", "B", "--see", "C"), (0, "C1\n", ""))
        self.assertEqual(self.last(), ("change-review: hana add C1", ["change-review/change-review - hana.md"]))
        self.assertEqual(run("add", "--production", DOC, "--list", str(self.path), "--change", "C", "--check", "D", "--see", "E", "--repo", str(source), "--merged", merged),
                         (0, "C2\n", ""))
        self.assertEqual(self.last(), (f"change-review: hana row C2 from {merged}", ["change-review/change-review - hana.md"]))
        _ = change_review.append_rows(self.path, [Visible("E", "F", "S"), Visible("G", "H", "S")], date(2026, 10, 15), "61b65bbf", PRODUCTION, record=staff.snapshot())
        self.assertEqual(self.last(), ("change-review: hana rows C3–C4 from 61b65bbf", ["change-review/change-review - hana.md"]))
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_a_verdict_commits_the_list(self) -> None:
        for row, held in (("C1", False), ("C2", True)):
            with self.subTest(held=held):
                _ = run("add", "--production", DOC, "--list", str(self.path), "--change", "A", "--check", "B", "--see", "C")
                command = ("verdict", "--production", DOC, "--list", str(self.path), "--row", row, "--verdict", "works")
                lock = self.repo / ".git/index.lock"
                if held:
                    _ = lock.write_text("", encoding="utf-8")
                code, output, error = run(*command)
                self.assertEqual((code, output), (0, f"{row} landed\n"))
                if held:
                    self.assertIn(f"change review: {self.path} written but not committed: ", error)
                    self.assertEqual(len(error.splitlines()), 1)
                    lock.unlink()
                    code, output, error = run(*command)
                    self.assertEqual(code, 0)
                    self.assertIn("already has this verdict", output)
                self.assertEqual(error, "")
                self.assertEqual(self.last(), (f"change-review: hana verdict {row}", ["change-review/change-review - hana.md"]))
                self.assertIn(f"| {row} | A | works |", self.git("show", "HEAD:change-review/change-review - hana.md"))

    def test_unchanged_edit_keeps_list_bytes_timestamp_and_git_history(self) -> None:
        _ = change_review.append_rows(self.path, [GRID], date(2026, 10, 15), "216c4e1d", PRODUCTION, record=staff.snapshot())
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns, self.git("rev-parse", "HEAD")
        _ = change_review._edit(self.path, PRODUCTION, date(2026, 10, 20),  # pyright: ignore[reportPrivateUsage]
                               lambda lines: len(lines), lambda count: f"unchanged {count}")
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns, self.git("rev-parse", "HEAD")), before)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_verdict_rerun_commits_the_list_saved_before_the_first_commit_stopped(self) -> None:
        _ = change_review.append_rows(self.path, [GRID], date(2026, 10, 15), "216c4e1d", PRODUCTION, record=staff.snapshot())
        previous = self.git("rev-parse", "HEAD")
        with (mock.patch.object(change_review, "_commit", side_effect=KeyboardInterrupt),
              self.assertRaises(KeyboardInterrupt)):
            _ = run("verdict", "--production", DOC, "--list", str(self.path), "--row", "C1", "--verdict", "works")
        saved = self.path.read_bytes(), self.path.stat().st_mtime_ns
        self.assertNotEqual(self.git("status", "--porcelain", "--", str(self.path)), "")
        self.assertEqual(self.git("rev-parse", "HEAD"), previous)
        code, printed, error = run("verdict", "--production", DOC, "--list", str(self.path),
                                   "--row", "C1", "--verdict", "works")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("already has this verdict", printed)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), saved)
        self.assertNotEqual(self.git("rev-parse", "HEAD"), previous)
        self.assertEqual(self.git("rev-list", "--count", f"{previous}..HEAD"), "1")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertIn("| C1 | " + GRID.change + " | works |", self.git("show", "HEAD:change-review/change-review - hana.md"))

    def test_an_edit_commits_the_list(self) -> None:
        _ = run("add", "--production", DOC, "--list", str(self.path), "--change", "A", "--check", "B", "--see", "C")
        _ = run("edit", "--production", DOC, "--list", str(self.path), "--row", "C1", "--change", "D", "--check", "E",
                "--see", "F")
        self.assertEqual(self.last(), ("change-review: hana edit C1", ["change-review/change-review - hana.md"]))
        self.assertIn("| C1 | D |", self.git("show", "HEAD:change-review/change-review - hana.md"))
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_another_staged_file_stays_staged_and_uncommitted(self) -> None:
        _ = (self.repo / "note.md").write_text("staged edit\n", encoding="utf-8")
        _ = (self.repo / "draft.md").write_text("new\n", encoding="utf-8")
        _ = self.git("add", "note.md", "draft.md")
        _ = run("add", "--production", DOC, "--list", str(self.path), "--change", "A", "--check", "B", "--see", "C")
        self.assertEqual(self.last(), ("change-review: hana add C1", ["change-review/change-review - hana.md"]))
        self.assertEqual(self.git("diff", "--cached", "--name-only").splitlines(), ["draft.md", "note.md"])
        self.assertEqual(self.git("show", ":note.md"), "staged edit")

    def test_a_list_outside_any_repository_is_written_and_not_committed(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        outside = Path(directory.name) / "change-review" / "hana.md"
        self.assertEqual(run("add", "--production", DOC, "--list", str(outside), "--change", "A", "--check", "B", "--see", "C"), (0, "C1\n", ""))
        self.assertIn("| C1 | A |", outside.read_text(encoding="utf-8"))
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")

    def test_a_held_index_lock_warns_writes_and_the_next_add_commits_both_rows(self) -> None:
        lock = self.repo / ".git" / "index.lock"
        _ = lock.write_text("", encoding="utf-8")
        code, output, error = run("add", "--production", DOC, "--list", str(self.path), "--change", "A", "--check", "B", "--see", "C")
        self.assertEqual((code, output), (0, "C1\n"))
        self.assertEqual(len(error.splitlines()), 1)
        self.assertIn(f"change review: {self.path} written but not committed: ", error)
        self.assertIn("; the next write commits it", error)
        self.assertIn("| C1 | A |", self.path.read_text(encoding="utf-8"))
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")
        lock.unlink()
        self.assertEqual(run("add", "--production", DOC, "--list", str(self.path), "--change", "C", "--check", "D", "--see", "E"), (0, "C2\n", ""))
        self.assertEqual(self.last(), ("change-review: hana add C2", ["change-review/change-review - hana.md"]))
        committed = self.git("show", "HEAD:change-review/change-review - hana.md")
        self.assertIn("| C1 | A |", committed)
        self.assertIn("| C2 | C |", committed)
        self.assertEqual(self.git("status", "--porcelain"), "")


class ProductionMergeCommitsTest(unittest.TestCase):
    repo: Path = Path()
    own_doc: Path = Path()
    other_doc: Path = Path()
    previous: str = ""
    foreign: str = ""

    def git(self, *arguments: str, cwd: Path | None = None) -> str:
        environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_AUTHOR_DATE": "2026-10-29T12:00:00Z", "GIT_COMMITTER_DATE": "2026-10-29T12:00:00Z"}
        return subprocess.run(["git", "-C", str(cwd or self.repo), "-c", "user.name=t",
                               "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *arguments],
                              capture_output=True, text=True, check=True, env=environment).stdout.strip()

    def commit(self, name: str, message: str) -> str:
        _ = (self.repo / name).write_text(message, encoding="utf-8")
        _ = self.git("add", name)
        _ = self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        origin, seed = root / "origin.git", root / "seed"
        seed.mkdir()
        _ = self.git("init", "--bare", str(origin), cwd=root)
        _ = self.git("init", "-q", "-b", "own-merge", cwd=seed)
        self.repo = seed
        self.previous = self.commit("base.py", "base")
        _ = self.git("remote", "add", "origin", str(origin))
        _ = self.git("push", "-q", "origin", "own-merge")
        _ = self.git("clone", "-q", "-b", "own-merge", str(origin), str(root / "clone"), cwd=root)
        self.repo = root / "clone"
        _ = self.git("checkout", "-q", "-b", "unit")
        _ = self.git("checkout", "-q", "-b", "other-merge")
        self.foreign = self.commit("foreign.py", "checkpoint(other): already reviewed in its production")
        _ = self.git("push", "-q", "origin", "other-merge")
        _ = self.git("checkout", "-q", "unit")
        _ = self.git("merge", "-q", "--no-ff", "-m", "merge other production", "origin/other-merge")
        self.own_doc, self.other_doc = root / "own-production.md", root / "other-production.md"
        for doc, branch in ((self.own_doc, "own-merge"), (self.other_doc, "other-merge")):
            _ = doc.write_text(f"- **Repository:** `{self.repo}`\n- **Merge branch:** `{branch}`\n"
                               + f"- **Change review:** `{root / 'review.md'}`\n", encoding="utf-8")
        _ = self.enterContext(mock.patch.object(change_review, "showrunner_of",
                                               side_effect=change_review.ChangeReviewError("not running")))

    def commits(self) -> list[change_review.Commit]:
        return change_review.unit_commits(self.repo, "HEAD", self.previous, "own-merge",
                                          productions=lambda: (self.own_doc, self.other_doc))

    def test_imported_production_commits_leave_only_the_units_missing_line(self) -> None:
        own = self.commit("unit.py", "checkpoint(unit): own work")
        refused = change_review.missing(self.commits())
        self.assertEqual(len(refused), 1)
        self.assertTrue(refused[0].startswith(own[:8]))
        self.assertNotIn(self.foreign[:8], refused[0])

    def test_imported_production_commits_need_no_cover_when_unit_has_its_line(self) -> None:
        own = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        commits = self.commits()
        self.assertEqual([commit.sha for commit in commits], [own])
        self.assertEqual(change_review.missing(commits), [])

    def test_production_without_review_rows_keeps_its_commits_checked(self) -> None:
        _ = self.other_doc.write_text(f"- **Repository:** `{self.repo}`\n- **Merge branch:** `other-merge`\n")
        _ = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        self.assertEqual([item.split()[0] for item in change_review.missing(self.commits())], [self.foreign[:8]])

    def test_production_in_another_repository_keeps_its_commits_checked(self) -> None:
        other = self.repo.parent / "unrelated"
        other.mkdir()
        _ = self.git("init", "-q", cwd=other)
        _ = self.other_doc.write_text(self.other_doc.read_text().replace(str(self.repo), str(other)))
        _ = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        self.assertEqual([item.split()[0] for item in change_review.missing(self.commits())], [self.foreign[:8]])

    def test_unreadable_production_doc_keeps_the_existing_exclusions(self) -> None:
        own = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        with mock.patch.object(showrunners, "timer_docs", return_value={
            "own": self.own_doc, "other": self.other_doc, "missing": self.repo.parent / "missing.md",
        }):
            commits = change_review.unit_commits(self.repo, "HEAD", self.previous, "own-merge")
        self.assertEqual([commit.sha for commit in commits], [own])
        self.assertEqual(change_review.missing(commits), [])

    def test_linked_worktree_excludes_commits_reviewed_in_the_common_repository(self) -> None:
        checkout = self.repo.parent / "unit-checkout"
        _ = self.git("worktree", "add", "-q", "-b", "linked", str(checkout), "unit")
        self.repo = checkout
        own = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        self.assertEqual([commit.sha for commit in self.commits()], [own])
        self.assertEqual(change_review.missing(self.commits()), [])

    def test_invalid_production_repository_keeps_valid_production_exclusions(self) -> None:
        own = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        invalid_doc = self.repo.parent / "invalid-production.md"
        not_git = self.repo.parent / "not-git"
        not_git.mkdir()
        for repository in (self.repo.parent / "missing-repository", not_git):
            with self.subTest(repository=repository):
                _ = invalid_doc.write_text(f"- **Repository:** `{repository}`\n"
                                          + "- **Merge branch:** `invalid-merge`\n"
                                          + f"- **Change review:** `{self.repo.parent / 'review.md'}`\n",
                                          encoding="utf-8")
                commits = change_review.unit_commits(
                    self.repo, "HEAD", self.previous, "own-merge",
                    productions=lambda: (self.own_doc, invalid_doc, self.other_doc))
                self.assertEqual([commit.sha for commit in commits], [own])
                self.assertEqual(change_review.missing(commits), [])

    def test_incomplete_production_doc_keeps_valid_production_exclusions(self) -> None:
        own = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        invalid_doc = self.repo.parent / "incomplete-production.md"
        _ = invalid_doc.write_text(f"- **Repository:** `{self.repo}`\n"
                                  + f"- **Change review:** `{self.repo.parent / 'review.md'}`\n",
                                  encoding="utf-8")
        commits = change_review.unit_commits(
            self.repo, "HEAD", self.previous, "own-merge",
            productions=lambda: (self.own_doc, invalid_doc, self.other_doc))
        self.assertEqual([commit.sha for commit in commits], [own])
        self.assertEqual(change_review.missing(commits), [])

    def test_registered_productions_are_read_by_default(self) -> None:
        own = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        with mock.patch.object(showrunners, "timer_docs", return_value={"own": self.own_doc, "other": self.other_doc}):
            commits = change_review.unit_commits(self.repo, "HEAD", self.previous, "own-merge")
        self.assertEqual([commit.sha for commit in commits], [own])

    def test_repository_subdirectory_resolves_to_the_same_git_top_level(self) -> None:
        subdirectory = self.repo / "src"
        subdirectory.mkdir()
        _ = self.other_doc.write_text(self.other_doc.read_text().replace(str(self.repo), str(subdirectory)))
        own = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        self.assertEqual([commit.sha for commit in self.commits()], [own])

    def test_absent_origin_branch_does_not_exclude_its_local_commits(self) -> None:
        _ = self.git("update-ref", "-d", "refs/remotes/origin/other-merge")
        _ = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        self.assertEqual([item.split()[0] for item in change_review.missing(self.commits())], [self.foreign[:8]])

    def test_unreadable_timer_registry_keeps_the_existing_exclusions(self) -> None:
        _ = self.commit("unit.py", f"checkpoint(unit): own work\n\n{SEE}")
        with mock.patch.object(showrunners, "timer_docs", side_effect=OSError("cannot read production registry")):
            commits = change_review.unit_commits(self.repo, "HEAD", self.previous, "own-merge")
        self.assertEqual([item.split()[0] for item in change_review.missing(commits)], [self.foreign[:8]])


class RepositoryTest(unittest.TestCase):
    """A unit branch with its own checkpoints and a merge from the merge branch."""

    repo: Path = Path()
    previous: str = ""

    def git(self, *arguments: str, day: str = "2026-10-15T12:00:00-04:00") -> str:
        environment = {**os.environ, "GIT_AUTHOR_DATE": day, "GIT_COMMITTER_DATE": day,
                       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        return subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t",
                               "-c", "commit.gpgsign=false", *arguments],
                              capture_output=True, text=True, check=True, env=environment).stdout.strip()

    def commit(self, name: str, message: str, day: str = "2026-10-15T12:00:00-04:00") -> str:
        _ = (self.repo / name).write_text(message, encoding="utf-8")
        _ = self.git("add", name)
        _ = self.git("commit", "-q", "-m", message, day=day)
        return self.git("rev-parse", "HEAD")

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name)
        _ = self.enterContext(isolated_staff_file(self.repo / "staff.json", {}))
        _ = self.git("init", "-q", "-b", "merge")
        self.previous = self.commit("base.py", "checkpoint(u): base\n\nChange-Review: none — base")
        _ = self.git("checkout", "-q", "-b", "unit")
        _ = self.git("checkout", "-q", "merge")
        _ = self.commit("other.py", "checkpoint(other): no trailer, merged into the unit")
        _ = self.git("update-ref", "refs/remotes/origin/merge", "HEAD")
        _ = self.git("checkout", "-q", "unit")
        _ = self.git("merge", "-q", "--no-ff", "-m", "merge(u): origin/merge", "origin/merge")

    def test_only_the_units_own_checkpoints_are_read(self) -> None:
        checkpoint = self.commit("a.py", f"checkpoint(u): grid\n\n{SEE}")
        commits = change_review.unit_commits(self.repo, checkpoint, self.previous, "merge")
        self.assertEqual([commit.subject for commit in commits], ["checkpoint(u): grid"])
        self.assertEqual(change_review.missing(commits), [])
        self.assertEqual(change_review.visible(commits), [GRID])

        with self.subTest(subject="queue(7): grid"):
            queued = self.commit("a.py", f"queue(7): grid\n\n{SEE}")
            commits = change_review.unit_commits(self.repo, queued, checkpoint, "merge")
            self.assertEqual([commit.subject for commit in commits], ["queue(7): grid"])
            self.assertEqual(change_review.missing(commits), [])
            self.assertEqual(change_review.visible(commits), [GRID])

        _ = self.merged("unit", "Merge initial checkpoints")
        for layout in ("linear", "merged", "sibling", "checkpoints"):
            with self.subTest(layout=layout):
                _ = self.git("checkout", "unit")
                previous = self.git("rev-parse", "HEAD")
                earlier: list[str] = []
                earlier_rows: list[Visible] = []
                if layout == "checkpoints":
                    earlier.append(self.commit("earlier.py",
                                               "checkpoint(u): earlier\n\nChange-Review: Earlier change | Read | Earlier result"))
                    earlier_rows.append(Visible("Earlier change", "Read", "Earlier result"))
                if layout == "merged":
                    _ = self.git("checkout", "-b", "draft-side")
                _ = self.commit(f"draft-{layout}.py",
                                "fix(u): draft\n\nChange-Review: Draft change | Draft check | Draft result")
                if layout == "merged":
                    _ = self.git("checkout", "unit")
                    _ = self.git("merge", "--no-ff", "draft-side", "-m", "Merge worker draft")
                final = self.commit(f"final-{layout}.py", f"checkpoint(u): final\n\n{SEE}")
                _ = self.git("checkout", "-b", f"queue-{layout}", previous if layout == "sibling" else final)
                queued = self.commit(f"queue-{layout}.py",
                                     "queue(7): later change\n\nChange-Review: Queued later | Read | Queue result")
                _ = self.git("checkout", "unit")
                _ = self.git("merge", "--no-ff", f"queue-{layout}", "-m", "Merge queued change")
                tip = self.git("rev-parse", "HEAD")
                unit_commits = change_review.unit_commits(self.repo, tip, previous, "merge")
                merged = self.merged("unit", f"Merge {layout} unit")
                for commits in (unit_commits, change_review.merge_commits(self.repo, merged)):
                    self.assertEqual(change_review.missing(commits), [])
                    self.assertCountEqual([commit.sha for commit in change_review.review_commits(commits)], [*earlier, final, queued])
                    self.assertCountEqual(change_review.visible(commits),
                                          [*earlier_rows, GRID, Visible("Queued later", "Read", "Queue result")])

    def test_a_merge_reads_its_parents_with_one_git_command_and_refuses_unknown_commits(self) -> None:
        checkpoint = self.commit("a.py", f"checkpoint(u): grid\n\n{SEE}")
        merged = self.merged("unit", "Merge u")
        with mock.patch.object(subprocess, "run", wraps=subprocess.run) as commands:
            commits = change_review.merge_commits(self.repo, merged)
        invocations = [cast(list[str], call.args[0]) for call in commands.call_args_list]
        self.assertEqual(sum("--parents" in command for command in invocations), 1)
        self.assertEqual([commit.sha for commit in commits], [checkpoint])
        self.assertEqual(change_review.merge_commits(self.repo, checkpoint), [])
        with self.assertRaises(change_review.ChangeReviewError):
            _ = change_review.merge_commits(self.repo, "f" * 40)

    def test_a_tree_reads_a_branch_first_parent_line_once(self) -> None:
        checkpoint = self.commit("a.py", f"checkpoint(u): grid\n\n{SEE}")
        merged = self.merged("unit", "Merge u")
        doc = self.repo / "production.md"
        _ = doc.write_text(f"- **Repository:** {self.repo}\n- **Merge branch:** merge\n")
        tree = change_review.Tree([doc])
        with mock.patch.object(subprocess, "run", wraps=subprocess.run) as commands:
            self.assertEqual(tree.landed_on(self.repo, merged), doc)
            self.assertIsNone(tree.landed_on(self.repo, checkpoint))
            self.assertEqual(tree.landed_on(self.repo, merged), doc)
        invocations = [cast(list[str], call.args[0]) for call in commands.call_args_list]
        self.assertEqual(sum("--first-parent" in command for command in invocations), 1)

    def test_a_markdown_only_checkpoint_needs_no_line_but_its_line_is_read(self) -> None:
        _ = self.commit("a.md", "checkpoint(u): doc without a line")
        checkpoint = self.commit("b.md", f"checkpoint(u): doc with a line\n\n{SEE}")
        commits = change_review.unit_commits(self.repo, checkpoint, self.previous, "merge")
        self.assertEqual(change_review.missing(commits), [])
        self.assertEqual(change_review.visible(commits), [GRID])

    def test_a_checkpoint_without_a_line_is_refused_even_when_the_named_checkpoint_is_a_merge(self) -> None:
        _ = self.commit("a.py", "checkpoint(u): grid")
        _ = self.git("checkout", "-q", "-b", "side")
        _ = self.commit("b.md", "docs only")
        _ = self.git("checkout", "-q", "unit")
        _ = self.git("merge", "-q", "--no-ff", "-m", "merge(u): side", "side")
        refused = change_review.missing(change_review.unit_commits(self.repo, "HEAD", self.previous, "merge"))
        self.assertEqual(len(refused), 1)
        self.assertIn("checkpoint(u): grid: no Change-Review line", refused[0])

    def test_incoming_merge_trailers_refuse_but_reviewed_history_stays_excluded(self) -> None:
        _ = self.commit("a.py", "plain change")
        _ = self.git("checkout", "-qb", "side")
        _ = self.commit("b.md", "side change")
        _ = self.git("checkout", "-q", "unit")
        for trailer in (SEE, change_review.COVERS + ": malformed"):
            _ = self.git("merge", "-q", "--no-ff", "-m", "Merge side\n\n" + trailer, "side")
            with self.assertRaisesRegex(change_review.ChangeReviewError, "Put the lines on a plain commit after the merge"):
                _ = change_review.unit_commits(self.repo, "HEAD", self.previous, "merge")
            _ = self.git("reset", "--hard", "HEAD^1")
        _ = self.git("merge", "-q", "--no-ff", "-m", "Merge side\n\n" + SEE, "side")
        merged = self.git("rev-parse", "HEAD")
        _ = self.git("update-ref", "refs/remotes/origin/merge", merged)
        plain = self.commit("repair.py", "checkpoint(u): repair\n\n" + SEE)
        self.assertEqual([commit.sha for commit in change_review.unit_commits(self.repo, plain, self.previous, "merge")], [plain])

    def test_incoming_merge_with_a_none_line_is_left_out(self) -> None:
        plain = self.commit("a.py", "plain change")
        _ = self.git("checkout", "-qb", "side")
        side = self.commit("b.md", "side change")
        _ = self.git("checkout", "-q", "unit")
        _ = self.git("merge", "-q", "--no-ff", "-m", "checkpoint(u): merge",
                     "-m", "Change-Review: none — merge only", "side")
        commits = change_review.unit_commits(self.repo, "HEAD", self.previous, "merge")
        self.assertEqual({commit.sha for commit in commits}, {plain, side})


    def test_a_later_checkpoint_covers_one_pushed_without_its_line(self) -> None:
        early = self.commit("a.py", "checkpoint(u): grid")
        self.assertEqual(len(change_review.missing(change_review.unit_commits(self.repo, "HEAD", self.previous, "merge"))), 1)
        _ = self.commit("b.py", f"checkpoint(u): change review\n\n{SEE}\nChange-Review-Covers: {early[:8]}")
        commits = change_review.unit_commits(self.repo, "HEAD", self.previous, "merge")
        self.assertEqual(change_review.missing(commits), [])
        self.assertEqual(change_review.visible(commits), [GRID])

    def test_a_checkpoint_made_before_the_rule_landed_is_not_read(self) -> None:
        _ = self.commit("a.py", "checkpoint(u): before the rule", day="2026-10-14T23:59:00-04:00")
        checkpoint = self.commit("b.py", f"checkpoint(u): grid\n\n{SEE}")
        commits = change_review.unit_commits(self.repo, checkpoint, self.previous, "merge")
        self.assertEqual([commit.subject for commit in commits], ["checkpoint(u): grid"])



    def merged(self, branch: str, message: str) -> str:
        """Merge `branch` into the merge branch, move origin/merge there and return the merge's short hash."""
        _ = self.git("checkout", "-q", "merge")
        _ = self.git("merge", "-q", "--no-ff", "-m", message, branch)
        _ = self.git("update-ref", "refs/remotes/origin/merge", "HEAD")
        return self.git("rev-parse", "--short=8", "HEAD")


    def test_backfill_fills_older_rows_from_their_merges_and_names_a_row_it_cannot_trace(self) -> None:
        _ = self.commit("a.py", f"checkpoint(u): grid\n\n{SEE}")
        unit_merge = self.merged("unit", "Merge u")
        _ = self.git("checkout", "-q", "-b", "side", "merge")
        _ = self.commit("b.py", "a plain commit")
        side_merge = self.merged("side", "Merge side")
        folder = self.repo / "docs"
        folder.mkdir()
        top = folder / "top-production.md"
        _ = top.write_text(f"- **Repository:** {self.repo}\n- **Merge branch:** merge\n", encoding="utf-8")
        kid = folder / "kid-production.md"
        _ = kid.write_text(f"- **Repository:** {self.repo}\n- **Merge branch:** kid\n", encoding="utf-8")
        path = folder / "change-review - top.md"
        _ = path.write_text("## To review\n\n| # | Change | Merged | How to check |\n| --- | --- | --- | --- |\n"
                            + f"| C1 | {GRID.change} | 2026-10-15 · {unit_merge} | x |\n"
                            + f"| C2 | side change | 2026-10-15 · {side_merge} | x |\n"
                            + "| C3 | no hash | 2026-10-15 | x |\n\n"
                            + "## Reviewed\n\n| # | Change | Verdict | Date |\n| --- | --- | --- | --- |\n", encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        _ = recorded_unit("kid", "u", self.repo / "u", kid, plan="docs/u-plan.md")
        top_owner = fixture_member("showrunner", name="top", doc=staff.LiveDoc(remote.local_machine(), top))
        tree = change_review.Tree([top, kid])
        kid_owner = staff.snapshot().by_doc(remote.local_machine(), kid)
        assert kid_owner is not None
        expected = ({"C1": kid_owner.id, "C2": top_owner.id}, {"C3": "no merge hash"})
        self.assertEqual(change_review.backfill(path, tree, write=False), expected)
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(change_review.backfill(path, tree, write=True), expected)
        rows = {row["#"]: row[change_review.PRODUCTION] for row in change_review.pending(path)}
        self.assertEqual(rows, {"C1": kid_owner.id, "C2": top_owner.id, "C3": ""})
        # A second run finds nothing left to fill.
        self.assertEqual(change_review.backfill(path, tree, write=False), ({}, {"C3": "no merge hash"}))

    def test_backfill_credits_a_style_childs_hand_off_change_to_the_parent_that_merged_it(self) -> None:
        _ = self.commit("a.py", f"checkpoint(u): grid\n\n{SEE}")
        handoff = self.merged("unit", "Merge style hand-off")
        folder = self.repo / "docs"
        folder.mkdir()
        top = folder / "top-production.md"
        _ = top.write_text(f"- **Repository:** {self.repo}\n- **Merge branch:** merge\n", encoding="utf-8")
        style = folder / "top-style-production.md"
        _ = style.write_text(f"- **Repository:** {self.repo}\n- **Merge branch:** style/main\n- **Kind:** style\n"
                             + f"- **Parent production:** {top}\n", encoding="utf-8")
        _ = recorded_unit("top-style", "u", self.repo / "u", style, plan="docs/u-plan.md")
        owner = fixture_member("showrunner", name="top", doc=staff.LiveDoc(remote.local_machine(), top))
        child = staff.snapshot().by_doc(remote.local_machine(), style)
        assert child is not None
        _ = staff.move(child.id, staff.ReportsTo(owner.id))
        path = folder / "change-review - top.md"
        _ = path.write_text("## To review\n\n| # | Change | Merged | How to check |\n| --- | --- | --- | --- |\n"
                            + f"| C1 | {GRID.change} | 2026-10-15 · {handoff} | x |\n\n"
                            + "## Reviewed\n\n| # | Change | Verdict | Date |\n| --- | --- | --- | --- |\n", encoding="utf-8")
        self.assertEqual(change_review.backfill(path, change_review.Tree([top, style]), write=True), ({"C1": owner.id}, {}))

    def test_a_root_merge_stamps_each_change_with_the_production_of_its_checkpoints_unit(self) -> None:
        folder = self.repo / "docs"
        folder.mkdir()
        top, kid = folder / "top-production.md", folder / "kid-production.md"
        _ = kid.write_text("# Kid\n", encoding="utf-8")
        _ = top.write_text("# Top\n", encoding="utf-8")
        commits = [change_review.Commit("1", "checkpoint(u-plan): grid", f"checkpoint(u-plan): grid\n\n{SEE}", ()),
                   change_review.Commit("2", "checkpoint(own): grid", f"checkpoint(own): grid\n\n{SEE}", ())]
        unit = recorded_unit("kid", "u", self.repo, kid, plan="docs/u-plan.md")
        _ = recorded_showrunner("top", top)
        _ = recorded_showrunner("kid", kid, parent="top")
        identity = Path(os.environ["UNIT_HISTORY_DIR"]) / "runs/unit.jsonl"
        identity.parent.mkdir(parents=True, exist_ok=True)
        _ = identity.write_text(json.dumps({"schema_version": 1, "event_type": "checkpoint", "checkpoint_commit": "1",
                                            "owner_staff_id": unit.id, "unit": unit.name}) + "\n")
        with mock.patch.object(showrunners, "timer_docs", return_value={"top": top, "kid": kid}):
            self.assertEqual([change.source.production if isinstance(change.source, change_review.UnitSource) else top
                              for change in change_review.publication_changes(commits, top)], [top, kid])




from scripts.production.staff_test_support import StaffTestCase


class BranchOwnerLocationTests(StaffTestCase):
    def test_checkpoint_folder_alias_uses_the_staff_worktree(self) -> None:
        fixture = self.separated_unit_folders()
        owners = change_review.unit_branches([fixture.doc])
        self.assertEqual(owners[fixture.worktree.name].production, fixture.doc)
        self.assertNotIn("director", owners)


if __name__ == "__main__":
    _ = unittest.main()
