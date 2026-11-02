"""Shared staff routing fixtures and checks for both count readers."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterable
from typing import cast
import unittest
from pathlib import Path
from unittest import mock

from scripts.production import change_review, change_routing, staff
from scripts.production.staff_test_support import fixture_member
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


def checkpoint_rows(root: Path, documents: dict[str, Path], lists: dict[Path, str],
                    *, selected_rows: dict[Path, set[str]] | None = None) -> None:
    """Give the selected fixture rows real checkpoints and staff records."""
    repo = root / "routing-repo"
    repo.mkdir(exist_ok=True)

    def git(*arguments: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True,
                              text=True, check=True).stdout.strip()

    if not (repo / ".git").exists():
        _ = git("init", "-b", "main")
        _ = git("config", "user.email", "fixture@example.invalid")
        _ = git("config", "user.name", "Routing fixture")
    runners: dict[str, staff.StaffRow] = {}
    for name, doc in documents.items():
        runners[name] = fixture_member("showrunner", name=name, doc=staff.LiveDoc("natedev", doc))
        text = doc.read_text() if doc.exists() else f"# {name}\n"
        if "**Showrunner checkout:**" not in text:
            text += f"\n- **Showrunner checkout:** `{repo}`\n"
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
            _ = git("commit", "--allow-empty", "-m", f"checkpoint({name}): {row['Change']}")
            sha = git("rev-parse", "HEAD")
            worktree = root / "routing-worktrees" / name
            if worktree.exists():
                _ = subprocess.run(["git", "-C", str(worktree), "checkout", "-B", name, sha],
                                   capture_output=True, text=True, check=True)
            else:
                _ = git("worktree", "add", "-b", name, str(worktree), sha)
            _ = fixture_member("unit", name=name, parent=runners[owner].id,
                               worktree=staff.PresentWorktree("natedev", worktree))
            if "| Merged |" not in text:
                lines = text.splitlines()
                header = next(i for i, line in enumerate(lines) if line.startswith("| # |"))
                lines[header] += " Merged |"
                lines[header + 1] += " --- |"
                for i in range(header + 2, len(lines)):
                    if lines[i].startswith("| C"):
                        lines[i] += " |"
                text = "\n".join(lines) + "\n"
            columns = next(line for line in text.splitlines() if line.startswith("| # |")).strip("|").split("|")
            index = [column.strip() for column in columns].index("Merged")
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if line.startswith(f"| {row['#']} |"):
                    cells = line.strip("|").split("|")
                    cells[index] = " " + ((row.get("Merged", "") + " ").lstrip() + sha).strip() + " "
                    lines[i] = "|" + "|".join(cells) + "|"
                    break
            text = "\n".join(lines) + "\n"
        if not text.startswith("---\n"):
            docs = "".join(f"  - {doc}\n" for doc in documents.values())
            text = f"---\nshowrunner: {default}\nproductions:\n{docs}---\n" + text
        _ = path.write_text(text)


class StaffRoutingTests(unittest.TestCase):
    def test_only_named_fixture_rows_receive_unit_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, {"STAFF_FILE": str(root / "staff.json"), "XDG_CACHE_HOME": str(root / "cache")}):
                listing = root / "change-review - top.md"
                _ = listing.write_text("## To review\n\n| # | Change | Merged | Production |\n"
                                       + "| --- | --- | --- | --- |\n"
                                       + "| C1 | Own change | 2026-10-01 | kid |\n"
                                       + "| C2 | Unit change | 2026-10-02 | kid |\n")
                docs = {name: root / f"{name}-production.md" for name in ("top", "kid")}
                checkpoint_rows(root, docs, {listing: "top"}, selected_rows={listing: {"C2"}})
                own, unit = change_review.pending(listing)
                router = change_routing.StaffRouting(staff.snapshot(), docs, change_review)
                self.assertEqual(own["Merged"], "2026-10-01")
                self.assertIsInstance(router.owner(own, "top"), change_routing.ShowrunnerOwnChange)
                self.assertEqual(router.owner(own, "top").tab, "top")
                self.assertIsInstance(router.owner(unit, "top"), change_routing.StaffRoutedChange)
                self.assertEqual(router.owner(unit, "top").tab, "kid")

    def test_a_production_cell_cannot_hide_a_checkpoint_in_another_staff_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, {"STAFF_FILE": str(root / "staff.json"), "XDG_CACHE_HOME": str(root / "cache")}):
                docs: dict[str, Path] = {}
                rows: dict[str, dict[str, str]] = {}
                for name in ("a", "b"):
                    folder = root / name
                    folder.mkdir()
                    listing = folder / f"change-review - {name}.md"
                    _ = listing.write_text(f"## To review\n\n| # | Change |\n| --- | --- |\n| C1 | Change {name} |\n")
                    doc = folder / f"{name}-production.md"
                    checkpoint_rows(folder, {name: doc}, {listing: name})
                    docs[name] = doc
                    rows[name] = change_review.pending(listing)[0]
                row = rows["a"] | {"Production": "b"}
                router = change_routing.StaffRouting(staff.snapshot(), docs, change_review)
                owner = router.owner(row, "b")
                self.assertIsInstance(owner, change_routing.StaffRoutedChange)
                self.assertEqual(owner.tab, "a")
                self.assertEqual(router.owner(row | {"Production": "a"}, "b"), owner)

    def test_a_qualified_checkpoint_path_uses_the_resolved_staff_unit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, {"STAFF_FILE": str(root / "staff.json"), "XDG_CACHE_HOME": str(root / "cache")}):
                listing = root / "change-review - builds.md"
                _ = listing.write_text("## To review\n\n| # | Change |\n| --- | --- |\n| C1 | First |\n")
                doc = root / "builds-production.md"
                checkpoint_rows(root, {"builds": doc}, {listing: "builds"})
                record = staff.snapshot()
                unit = next(row for row in record.rows if row.kind == "unit")
                repo = root / "routing-repo"
                _ = subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "-m",
                                    f"checkpoint(builds/{unit.name}): qualified"], capture_output=True, check=True)
                merged = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                        capture_output=True, text=True, check=True).stdout.strip()
                owner = change_routing.StaffRouting(record, {"builds": doc}, change_review).owner(
                    {"#": "C2", "Change": "qualified", "Merged": merged}, "elsewhere")
                self.assertIsInstance(owner, change_routing.StaffRoutedChange)
                assert isinstance(owner, change_routing.StaffRoutedChange)
                self.assertEqual(owner.unit_id, unit.id)
                self.assertEqual(owner.tab, "builds")

    def test_concurrent_history_writers_use_separate_files_and_leave_a_valid_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, {"STAFF_FILE": str(root / "staff.json"), "XDG_CACHE_HOME": str(root / "cache")}):
                listing = root / "change-review - top.md"
                _ = listing.write_text("## To review\n\n| # | Change |\n| --- | --- |\n| C1 | First |\n")
                doc = root / "top-production.md"
                checkpoint_rows(root, {"top": doc}, {listing: "top"})
                router = change_routing.StaffRouting(staff.snapshot(), {"top": doc}, change_review)
                repo = root / "routing-repo/.git"
                barrier = threading.Barrier(2)
                read = router.history.read_commit_parents

                def read_together(repo: Path, revisions: Iterable[str]) -> dict[str, tuple[str, ...]]:
                    parents = read(repo, revisions)
                    _ = barrier.wait(TEST_HANG_GUARD_SECS)
                    return parents

                replace_file = Path.replace
                replaced: list[Path] = []
                written = threading.Barrier(2)

                def replace_together(source: Path, target: Path) -> Path:
                    replaced.append(source)
                    _ = written.wait(TEST_HANG_GUARD_SECS)
                    return replace_file(source, target)

                with (mock.patch.object(change_review, "read_commit_parents", side_effect=read_together),
                      mock.patch.object(Path, "replace", new=replace_together)):
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        first = executor.submit(router.history_records, repo)
                        second = executor.submit(router.history_records, repo)
                        one, two = first.result(TEST_HANG_GUARD_SECS), second.result(TEST_HANG_GUARD_SECS)
                self.assertEqual(one, two)
                path = router.history_path(repo)
                stored = cast(dict[str, object], json.loads(path.read_text()))
                self.assertEqual(set(cast(dict[str, object], stored["commits"])), set(one))
                self.assertEqual(len(set(replaced)), 2)
                self.assertEqual(list(path.parent.iterdir()), [path])
                self.assertEqual(router.history_records(repo), one)

    def test_a_failed_cache_replacement_removes_its_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, {"STAFF_FILE": str(root / "staff.json"), "XDG_CACHE_HOME": str(root / "cache")}):
                listing = root / "change-review - top.md"
                _ = listing.write_text("## To review\n\n| # | Change |\n| --- | --- |\n| C1 | First |\n")
                doc = root / "top-production.md"
                checkpoint_rows(root, {"top": doc}, {listing: "top"})
                router = change_routing.StaffRouting(staff.snapshot(), {"top": doc}, change_review)
                repo = root / "routing-repo/.git"
                with mock.patch.object(Path, "replace", side_effect=OSError("disk unavailable")):
                    records = router.history_records(repo)
                self.assertIn(change_review.pending(listing)[0]["Merged"], records)
                self.assertEqual(list(router.history_path(repo).parent.iterdir()), [])

    def test_history_cache_invalidates_when_a_new_checkpoint_changes_the_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, {"STAFF_FILE": str(root / "staff.json"), "XDG_CACHE_HOME": str(root / "cache")}):
                listing = root / "change-review - top.md"
                _ = listing.write_text("## To review\n\n| # | Change |\n| --- | --- |\n| C1 | First |\n")
                doc = root / "top-production.md"
                checkpoint_rows(root, {"top": doc}, {listing: "top"})
                row = change_review.pending(listing)[0]
                first = change_routing.StaffRouting(staff.snapshot(), {"top": doc}, change_review).owner(row, "top")
                self.assertIsInstance(first, change_routing.StaffRoutedChange)
                repo = root / "routing-repo"
                _ = subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "-m", "checkpoint(unknown): next"], check=True, capture_output=True)
                row["Merged"] = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                                                capture_output=True, text=True).stdout.strip()
                second = change_routing.StaffRouting(staff.snapshot(), {"top": doc}, change_review).owner(row, "top")
                self.assertIsInstance(second, change_routing.UnroutedChange)
                self.assertEqual(second.tab, "unrouted")

    def test_a_remote_configuration_change_keeps_the_history_cache_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, {"STAFF_FILE": str(root / "staff.json"), "XDG_CACHE_HOME": str(root / "cache")}):
                listing = root / "change-review - top.md"
                _ = listing.write_text("## To review\n\n| # | Change |\n| --- | --- |\n| C1 | First |\n")
                doc = root / "top-production.md"
                checkpoint_rows(root, {"top": doc}, {listing: "top"})
                row = change_review.pending(listing)[0]
                first = change_routing.StaffRouting(staff.snapshot(), {"top": doc}, change_review).owner(row, "top")
                _ = subprocess.run(["git", "-C", str(root / "routing-repo"), "remote", "add", "origin",
                                    "https://example.invalid/routing.git"], capture_output=True, check=True)
                with mock.patch.object(subprocess, "run", side_effect=AssertionError("the cached graph requires no Git command")):
                    second = change_routing.StaffRouting(staff.snapshot(), {"top": doc}, change_review).owner(row, "top")
                self.assertEqual(second, first)

    def test_delivered_marks_leave_the_count_but_failed_messages_keep_the_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            listing = root / "change-review - top.md"
            row = {"#": "C1"}
            record = change_routing.delivery_path(root, listing, "C1")
            for fields, expected in (({"validation": {"state": "sent", "attempt": "validation-attempt"}}, False),
                                     ({"validation": {"state": "failed"}}, True),
                                     ({"question": {"question_id": "q1", "text": "Why?", "delivery": None}}, False),
                                     ({"question": {"question_id": "q1", "text": "Why?", "answer": "Because"}}, True)):
                with self.subTest(fields=fields):
                    _ = record.write_text(json.dumps(fields))
                    self.assertEqual(change_routing.waits_for_user(listing, row, root), expected)

    def test_an_unknown_checkpoint_counts_as_unrouted_on_the_page_and_footer_and_saves_without_sending(self) -> None:
        from datetime import date
        from scripts.production import footer_counts, showrunners
        from scripts.review_links import change_browser, change_page

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {"STAFF_FILE": str(root / "staff.json"), "CHANGE_REVIEW_DIR": str(root),
                           "XDG_CACHE_HOME": str(root / "cache"), "REVIEW_LINKS_ROOT": str(root / "links"),
                           "NEXT_STATE_DIR": str(root / "next"), "FEATURE_LOG_VAULT": str(root / "vault")}
            with mock.patch.dict(os.environ, environment):
                listing = root / "change-review - natedev.md"
                _ = listing.write_text("## To review\n\n| # | Change | Production |\n| --- | --- | --- |\n| C1 | Known | natedev |\n")
                docs = {name: root / f"{name}-production.md" for name in ("natedev", "builds")}
                checkpoint_rows(root, docs, {listing: "natedev"})
                repo = root / "routing-repo"

                def git(*arguments: str) -> str:
                    return subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True,
                                          text=True, check=True).stdout.strip()

                _ = git("checkout", "-b", "aws-unit")
                _ = git("commit", "--allow-empty", "-m", "checkpoint(aws-unit): missing staff")
                _ = git("checkout", "main")
                _ = git("merge", "--no-ff", "aws-unit", "-m", "Merge aws-unit phase 1 into builds")
                _ = change_review.append_rows(listing, [change_review.Visible("Unknown AWS change", "Read", "Proof")],
                                               date(2026, 11, 2), git("rev-parse", "HEAD"), docs["builds"])
                _ = listing.write_text(listing.read_text().replace("| C2 |", "| C777 |"))
                runner = staff.snapshot().by_path("builds")
                parent = staff.snapshot().by_path("natedev")
                assert runner is not None and parent is not None
                _ = staff.move(runner.id, parent.id)
                import change_review as cli_review
                with (mock.patch.object(change_review, "LISTS", root), mock.patch.object(cli_review, "LISTS", root),
                      mock.patch.object(showrunners, "timer_docs", return_value=docs)):
                    browser = change_page.ReviewBrowser("secret")
                    status = change_page.DeliveryStatus(root / "links")
                    prepared = change_browser.prepare(browser, status)
                    unknown = next(item for item in prepared.changes if item.row["#"] == "C777")
                    self.assertEqual(unknown.tab, "unrouted")
                    self.assertIn("No staff record for aws-unit; no message goes out", change_browser.render(browser, status).decode())
                    counts = {tab: sum(item.tab == tab and item.waiting for item in prepared.changes) for tab in prepared.queues}
                    self.assertEqual(counts["unrouted"], 1)
                    for tab, doc in docs.items():
                        self.assertEqual(counts[tab], footer_counts.changes(doc))
                    self.assertEqual(counts["unrouted"], footer_counts.unrouted_changes(docs["natedev"]))
                    self.assertIn("1 unrouted", footer_counts.counts_line(footer_counts.read_counts(docs["natedev"])) or "")
                    from urllib.parse import urlencode
                    fields = {"row": "natedev/C777", "token": change_browser.row_token(browser, "natedev/C777"),
                              "outcome": "works", "feedback": ""}
                    with mock.patch.object(change_page, "send_message") as send:
                        delivery, answer = change_browser.submit(browser, urlencode(fields), status)
                        change_page.deliver(delivery, status)
                    self.assertEqual(answer, "works")
                    send.assert_not_called()
                    self.assertIsInstance(status.slots(change_page.ReviewPage(listing, docs["builds"], (), ""), "C777").verdict,
                                          change_page.NoDelivery)
