"""Prove branch selection and deletion with scratch records and a bare origin."""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from http.client import HTTPConnection
from pathlib import Path
from queue import Queue
from typing import cast, override
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit
from uuid import NAMESPACE_URL, uuid5

from scripts.agents import remote
from scripts.lib import file_lock
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import staff
from scripts.review_links import branch_page, change_page, serve


class BranchForm(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.offered: list[str] = []
        self.checked: list[str] = []
        self.token: str = ""

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        fields = dict(attrs)
        if tag == "input" and fields.get("name") == "branch":
            self.offered.append(fields.get("value") or "")
            if "checked" in fields:
                self.checked.append(fields.get("value") or "")
        if tag == "input" and fields.get("name") == "token":
            self.token = fields.get("value") or ""


@dataclass(frozen=True)
class ScratchBranches:
    root: Path
    repository: Path
    origin: Path
    history: Path
    staff_file: Path
    live_worktree: Path
    merges: dict[str, str]
    publisher: staff.StaffId

    def command(self, *arguments: str) -> str:
        result = subprocess.run(["git", "-C", str(self.repository), *arguments],
                                capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def remote_names(self) -> set[str]:
        return {line.split()[1].removeprefix("refs/heads/")
                for line in self.command("ls-remote", "--heads", "origin").splitlines()}

    def rows(self) -> tuple[branch_page.MergedUnitBranch, ...]:
        return tuple(row for row in self.listing().rows if isinstance(row, branch_page.MergedUnitBranch))

    def listing(self) -> branch_page.BranchListing:
        return branch_page.list_branches(self.repository, staff.snapshot(), (self.history,))

    def page(self) -> branch_page.BranchPage:
        url = branch_page.publish(self.repository)
        return branch_page.page_at(self.root / "reviews", urlsplit(url).path)


@contextmanager
def scratch_branches() -> Generator[ScratchBranches, None, None]:
    with tempfile.TemporaryDirectory(prefix="branch-page-test-") as temporary:
        root = Path(temporary)
        repository, origin = root / "repository", root / "origin.git"
        repository.mkdir()
        history = root / "history"
        (history / "runs").mkdir(parents=True)
        publisher = staff.StaffId(str(uuid5(NAMESPACE_URL, str(root / "production.md"))))
        fixture = ScratchBranches(root, repository, origin, history, root / "staff.json", root / "live", {}, publisher)
        _ = fixture.command("init", "-b", "main")
        _ = fixture.command("config", "user.name", "Branch test")
        _ = fixture.command("config", "user.email", "branch@example.invalid")
        _ = fixture.command("config", "commit.gpgsign", "false")
        _ = fixture.command("init", "--bare", str(origin))
        _ = fixture.command("remote", "add", "origin", str(origin))
        _ = fixture.command("commit", "--allow-empty", "-m", "Initial commit")
        initial = fixture.command("rev-parse", "HEAD")
        foreign = root / "other-repository"
        _ = fixture.command("init", str(foreign))
        _ = fixture.command("-C", str(foreign), "remote", "add", "origin", str(root / "other-origin.git"))
        for key, value in (("user.name", "Branch test"), ("user.email", "branch@example.invalid"),
                           ("commit.gpgsign", "false")):
            _ = fixture.command("-C", str(foreign), "config", key, value)
        _ = fixture.command("-C", str(foreign), "commit", "--allow-empty", "-m", "Other repository")
        _ = fixture.command("-C", str(foreign), "branch", "TEST-foreign-record")
        _ = fixture.command("-C", str(foreign), "branch", "TEST-foreign-owner")
        document = root / "production.md"
        production_id = staff.StaffId(str(uuid5(NAMESPACE_URL, str(document))))
        rows: list[staff.StaffRow] = []
        for name, kind, machine, evidence in (
            ("TEST-merged", "unit", "natedev", "fixed"),
            ("TEST-keep", "unit", "natedev", "fixed"),
            ("TEST-rejected", "unit", "natedev", "fixed"),
            ("TEST-live", "unit", "natedev", "fixed"),
            ("TEST-active<&", "unit", "natedev", "fixed"),
            ("TEST-production", "unit", "natedev", "fixed"),
            ("TEST-unmerged", "unit", "natedev", "fixed"),
            ("TEST-no-unit", "unit", "natedev", "none"),
            ("TEST-foreign-unit", "unit", "mac", "fixed"),
            ("TEST-showrunner-run", "showrunner", "natedev", "fixed"),
            ("TEST-legacy", "unit", "natedev", "legacy"),
            ("TEST-foreign-record", "unit", "natedev", "foreign"),
            ("TEST-foreign-owner", "unit", "natedev", "foreign-fallback"),
            ("TEST-owner-fallback", "unit", "natedev", "owner-fallback"),
            ("TEST-unknown-repository", "unit", "natedev", "unknown"),
            ("TEST-history-only", "unit", "natedev", "history"),
        ):
            _ = fixture.command("checkout", "-b", name, initial)
            _ = fixture.command("commit", "--allow-empty", "-m", f"Commit {name}")
            _ = fixture.command("checkout", "main")
            if name != "TEST-unmerged":
                _ = fixture.command("merge", "--no-ff", name, "-m", f"Merge {name}")
                fixture.merges[name] = fixture.command("rev-parse", "HEAD")
            if evidence == "none":
                continue
            identity = staff.StaffId(str(uuid5(NAMESPACE_URL, str(root / name))))
            tree = (staff.PresentWorktree(machine, fixture.live_worktree) if name == "TEST-live"
                    else staff.PresentWorktree(machine, root / "active") if name == "TEST-active<&"
                    else staff.RemovedWorktree(machine, foreign if evidence == "foreign-fallback"
                                              else repository if evidence in ("foreign", "owner-fallback") else root / "removed" / name))
            row = staff.StaffRow(identity, "showrunner" if kind == "showrunner" else "unit", name,
                                 staff.STAFF_ROOT if kind == "showrunner" else staff.ReportsTo(production_id),
                                 unit_type=staff.NotUnit() if kind == "showrunner" else staff.UnitDetails("direct"),
                                 current_session=identity,
                                 worktree=staff.WorktreeUnfilled() if kind == "showrunner" else tree)
            if evidence != "history":
                rows.append(row)
            event: dict[str, object] = {"event_type": "run_started", "skill": "unit-history", "branch": name,
                                        "working_dir": str(foreign if evidence == "foreign"
                                                           else root / "removed" / name if evidence == "history" else repository),
                                        "run_started_at": 100}
            if evidence in ("foreign-fallback", "owner-fallback", "unknown"):
                del event["working_dir"]
            event["owner_staff_id"] = identity
            if evidence == "legacy":
                event["skill"] = "plan-delegate"
            _ = (history / "runs" / f"{identity}.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
        _ = document.write_text("- **Merge branch:** `TEST-production`\n", encoding="utf-8")
        rows.append(staff.StaffRow(production_id, "showrunner", "production", staff.STAFF_ROOT, current_session=production_id,
                                   doc=staff.LiveDoc("natedev", document)))
        history_document = repository / "docs/plans/former-production.md"
        history_document.parent.mkdir(parents=True)
        _ = history_document.write_text(
            f"- **Repository:** `{repository}`\n\n## Units\n\n"
            + "| Unit | Worktree | Branch |\n| --- | --- | --- |\n"
            + f"| TEST-history-only | `{root / 'removed/TEST-history-only'}` | `TEST-history-only` |\n",
            encoding="utf-8")
        _ = fixture.command("add", "docs/plans/former-production.md")
        _ = fixture.command("commit", "-m", "Retain former unit rows")
        _ = fixture.staff_file.write_text(json.dumps({"schema": 2, "rows": [row.to_json() for row in rows]}), encoding="utf-8")
        _ = fixture.command("worktree", "add", str(fixture.live_worktree), "TEST-live")
        _ = fixture.command("worktree", "add", "-b", "TEST-current<&", str(root / "active"), "main")
        for number in reversed(range(12)):
            name = f"unowned-{number:02}"
            _ = fixture.command("checkout", "-b", name, initial)
            date = f"2000-01-{number + 1:02}T12:00:00+00:00"
            with patch.dict(os.environ, {"GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date}):
                _ = fixture.command("commit", "--allow-empty", "-m", f"Commit {name}")
            _ = fixture.command("checkout", "main")
            _ = fixture.command("merge", "--no-ff", name, "-m", f"Merge {name}")
            fixture.merges[name] = fixture.command("rev-parse", "HEAD")
        _ = fixture.command("push", "origin", "--all")
        with (patch.dict(os.environ, {"STAFF_FILE": str(fixture.staff_file), "UNIT_HISTORY_DIR": str(history),
                                      "REVIEW_LINKS_ROOT": str(root / "reviews"), "REVIEW_LINKS_HOST": "127.0.0.1", "CODEX_THREAD_ID": str(publisher)}),
              patch.object(remote, "local_machine", return_value="natedev")):
            yield fixture


class BranchPageTests(unittest.TestCase):
    def test_lists_only_merged_recorded_natedev_unit_branches_and_checks_each_row(self) -> None:
        for live_directory in ("present", "missing", "pruned", "newest-run", "unrecorded", "remote-main"):
            with self.subTest(live_directory=live_directory), scratch_branches() as fixture:
                if live_directory in ("missing", "pruned", "newest-run", "unrecorded"):
                    shutil.rmtree(fixture.live_worktree)
                if live_directory in ("pruned", "newest-run", "unrecorded"):
                    _ = fixture.command("worktree", "prune", "--expire=now")
                if live_directory == "unrecorded":
                    live = next(row for row in staff.snapshot().rows if row.name == "TEST-live")
                    (fixture.history / "runs" / f"{live.id}.jsonl").unlink()
                    with self.assertRaisesRegex(ValueError, "TEST-live: cannot read the active branch:"):
                        _ = fixture.rows()
                    continue
                if live_directory == "newest-run":
                    live = next(row for row in staff.snapshot().rows if row.name == "TEST-live")
                    old: dict[str, object] = {"event_type": "run_started", "skill": "unit-history", "branch": "TEST-keep",
                                              "owner_staff_id": live.id, "working_dir": str(fixture.repository),
                                              "run_started_at": 1}
                    _ = (fixture.history / "runs/zzz-old.jsonl").write_text(json.dumps(old) + "\n", encoding="utf-8")
                if live_directory == "remote-main":
                    clone = fixture.root / "second-clone"
                    _ = fixture.command("clone", "--branch", "main", str(fixture.origin), str(clone))
                    for key, value in (("user.name", "Branch test"), ("user.email", "branch@example.invalid"),
                                       ("commit.gpgsign", "false")):
                        _ = fixture.command("-C", str(clone), "config", key, value)
                    _ = fixture.command("-C", str(clone), "checkout", "TEST-merged")
                    _ = fixture.command("-C", str(clone), "commit", "--allow-empty", "-m", "New branch commit")
                    _ = fixture.command("-C", str(clone), "checkout", "main")
                    _ = fixture.command("-C", str(clone), "merge", "--no-ff", "TEST-merged", "-m", "Merge new branch commit")
                    fixture.merges["TEST-merged"] = fixture.command("-C", str(clone), "rev-parse", "HEAD")
                    _ = fixture.command("-C", str(clone), "push", "origin", "main", "TEST-merged")
                rows = fixture.rows()
                expected = {"TEST-merged", "TEST-keep", "TEST-rejected", "TEST-legacy", "TEST-owner-fallback",
                            "TEST-history-only", "TEST-active<&"}
                self.assertEqual({row.name for row in rows}, expected)
                page = branch_page.BranchPage(fixture.repository, rows, "test-token", fixture.publisher, fixture.root)
                form = BranchForm()
                content = branch_page.render(page).decode("utf-8")
                form.feed(content)
                content_text = re.sub(r"<[^>]*>", "", content)
                self.assertEqual(set(form.checked), expected)
                for row in rows:
                    source = f"origin/{row.name}" if live_directory == "remote-main" else row.name
                    self.assertEqual(row.unit, row.name)
                    self.assertEqual(row.tip, fixture.command("rev-parse", source))
                    self.assertEqual(row.merge, fixture.merges[row.name])
                    self.assertEqual(row.commit_date, fixture.command("show", "-s", "--format=%cI", source))
                    self.assertEqual(row.merge_date, fixture.command("show", "-s", "--format=%cI", row.merge))
                    self.assertIn(row.merge[:12], content)
                    self.assertIn(row.merge_date, content_text)
                    if row.name == "TEST-active<&":
                        current_branch = fixture.command("-C", str(fixture.root / "active"), "branch", "--show-current")
                        self.assertNotEqual(row.name, current_branch)
                        self.assertEqual(row.unit_status, branch_page.ActiveUnitBranch(current_branch))
                        self.assertIn(f"Unit: {html.escape(row.unit)} — active, now on branch {html.escape(current_branch)}", content)
                    else:
                        self.assertEqual(row.unit_status, branch_page.InactiveUnit())
                        self.assertIn(f"Unit: {html.escape(row.unit)} — not active", content)
                listing = fixture.listing()
                unowned = [row for row in listing.rows if isinstance(row, branch_page.MergedUnownedBranch)]
                expected_unowned = [f"unowned-{number:02}" for number in range(10)]
                self.assertEqual([row.name for row in unowned], expected_unowned)
                eligible = fixture.remote_names() - expected - {"main", "TEST-production", "TEST-live",
                                                               "TEST-current<&", "TEST-unmerged"}
                self.assertEqual(listing.unowned_total, len(eligible))
                all_rows = BranchForm()
                full_content = branch_page.render(fixture.page()).decode("utf-8")
                all_rows.feed(full_content)
                full_content_text = re.sub(r"<[^>]*>", "", full_content)
                self.assertEqual(all_rows.checked, [row.name for row in listing.rows])
                self.assertIn(f"Merged, no owner recorded (10 of {len(eligible)})", full_content)
                self.assertNotIn("TEST-production", all_rows.offered)
                self.assertNotIn("TEST-live", all_rows.offered)
                self.assertNotIn("TEST-current<&", all_rows.offered)
                for row in unowned:
                    self.assertEqual(row.commit_date, fixture.command("show", "-s", "--format=%cI", row.name))
                    self.assertEqual(row.merge, fixture.merges[row.name])
                    self.assertIn(row.commit_date, full_content_text)
                    self.assertIn(row.merge[:12], full_content)
                    self.assertIn(row.merge_date, full_content_text)
                for date_row in (rows[0], unowned[0]):
                    for commit_date, merge_date, commit_markup, merge_markup in (
                        ("2026-11-11T12:00:00+00:00", "2026-11-11T12:00:00+00:00",
                         '<strong class="date-in-order">2026-11-11</strong>T12:00:00+00:00',
                         '<strong class="date-in-order">2026-11-11</strong>T12:00:00+00:00'),
                        ("2026-11-11T14:00:00+03:00", "2026-11-11T12:00:00+00:00",
                         '<strong class="date-in-order">2026-11-11</strong>T14:00:00+03:00',
                         '<strong class="date-in-order">2026-11-11</strong>T12:00:00+00:00'),
                        ("2026-11-11T11:30:00-02:00", "2026-11-11T12:00:00+00:00",
                         '<strong class="date-out-of-order">2026-11-11</strong>T11:30:00-02:00',
                         '<strong class="date-out-of-order">2026-11-11</strong>T12:00:00+00:00'),
                    ):
                        with self.subTest(section=date_row.section, commit_date=commit_date, merge_date=merge_date):
                            dated_row = replace(date_row, commit_date=commit_date, merge_date=merge_date)
                            dated_page = branch_page.BranchPage(fixture.repository, (dated_row,), "test-token", fixture.publisher, fixture.root,
                                                               int(isinstance(dated_row, branch_page.MergedUnownedBranch)))
                            dated_content = branch_page.render(dated_page).decode("utf-8")
                            self.assertIn(f'Last commit: {commit_markup}', dated_content)
                            self.assertIn(f'Merged into main: {dated_row.merge[:12]} · {merge_markup}', dated_content)

    def test_delete_reports_each_result_and_keeps_unchecked_branches(self) -> None:
        for state in ("unchanged", "advanced"):
            with self.subTest(state=state), scratch_branches() as fixture:
                hook = fixture.origin / "hooks" / "update"
                _ = hook.write_text('#!/bin/sh\nif [ "$1" = refs/heads/TEST-rejected ]; then\n'
                                    + '  echo "TEST deletion refused" >&2\n  exit 1\nfi\n', encoding="utf-8")
                hook.chmod(0o755)
                page = branch_page.BranchPage(fixture.repository, fixture.rows(), "test-token", fixture.publisher, fixture.root)
                if state == "advanced":
                    _ = fixture.command("checkout", "TEST-merged")
                    _ = fixture.command("commit", "--allow-empty", "-m", "New work after publication")
                    _ = fixture.command("push", "origin", "TEST-merged")
                before = fixture.remote_names()
                deletion = branch_page.submit(page, urlencode({"token": page.token,
                                                              "branch": ["TEST-rejected", "TEST-merged", "TEST-legacy"]}, doseq=True))
                results = branch_page.delete_selected(deletion)
                self.assertEqual([result.name for result in results], ["TEST-rejected", "TEST-merged", "TEST-legacy"])
                self.assertIn("TEST deletion refused", results[0].message)
                self.assertEqual([result.message for result in results[1:]], ["deleted", "deleted"])
                self.assertEqual(fixture.remote_names(), before - {"TEST-merged", "TEST-legacy"})
                no_owner_page = fixture.page()
                no_owner_rows = [row.name for row in no_owner_page.rows
                                 if isinstance(row, branch_page.MergedUnownedBranch)]
                expected_kept: set[str] = set()
                for selection in (no_owner_rows[::2], []):
                    with self.subTest(selection=selection):
                        before_unowned = fixture.remote_names()
                        kept = set(no_owner_rows) - set(selection)
                        deletion = branch_page.submit(no_owner_page, urlencode(
                            {"token": no_owner_page.token, "branch": selection}, doseq=True))
                        no_owner_results = branch_page.delete_selected(deletion)
                        self.assertEqual([result.name for result in no_owner_results], selection)
                        self.assertEqual(fixture.remote_names(), before_unowned - set(selection))
                        expected_kept.update(kept)
                        saved = cast(list[str], json.loads(
                            (fixture.root / "reviews" / branch_page.KEPT).read_text(encoding="utf-8")))
                        self.assertEqual(set(saved), expected_kept)
                        self.assertTrue(kept <= fixture.remote_names())
                        next_page = fixture.page()
                        self.assertTrue(set(no_owner_rows).isdisjoint(row.name for row in next_page.rows))
                        remaining_unowned = [row for row in next_page.rows
                                            if isinstance(row, branch_page.MergedUnownedBranch)]
                        self.assertEqual(next_page.unowned_total, no_owner_page.unowned_total - len(no_owner_rows))
                        expected_next = ["unowned-10", "unowned-11"] if selection else []
                        self.assertEqual([row.name for row in remaining_unowned[:2]], expected_next)
                        no_owner_page = next_page
                        no_owner_rows = [row.name for row in remaining_unowned]

    def test_invalid_or_stale_selections_preserve_remote_branches(self) -> None:
        for change in ("unlisted", "token"):
            with self.subTest(change=change), scratch_branches() as fixture:
                page = branch_page.BranchPage(fixture.repository, fixture.rows(), "test-token", fixture.publisher, fixture.root)
                token, selected = page.token, "TEST-merged"
                if change == "unlisted":
                    selected = "TEST-no-unit"
                else:
                    token = "wrong-token"
                before = fixture.command("ls-remote", "--heads", "origin")
                body = urlencode({"token": token, "branch": [selected, "TEST-keep"]}, doseq=True)
                with self.assertRaises(ValueError):
                    _ = branch_page.submit(page, body)
                self.assertEqual(fixture.command("ls-remote", "--heads", "origin"), before)
                url = branch_page.publish(fixture.repository)
                root = fixture.root / "reviews"
                path = urlsplit(url).path
                manifest_path = root / path.split("/")[1] / branch_page.MANIFEST
                original = manifest_path.read_text(encoding="utf-8")
                for defect in ("old form", "unknown section", "extra row field", "invalid count", "missing publisher"):
                    with self.subTest(defect=defect):
                        manifest = cast(dict[str, object], json.loads(original))
                        manifest_rows = cast(list[dict[str, object]], manifest["rows"])
                        if defect == "old form":
                            del manifest["unowned_total"]
                            for row in manifest_rows:
                                del row["section"]
                        elif defect == "unknown section":
                            manifest_rows[0]["section"] = "other"
                        elif defect == "extra row field":
                            manifest_rows[-1]["unit"] = "invented"
                        elif defect == "invalid count":
                            manifest["unowned_total"] = 0
                        else:
                            del manifest["publisher"]
                        _ = manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                        with self.assertRaises(ValueError):
                            _ = branch_page.page_at(root, path)
                with patch.dict(os.environ, {"CODEX_THREAD_ID": str(uuid5(NAMESPACE_URL, "unrecorded publisher"))}):
                    with self.assertRaisesRegex(ValueError, "the current session has no staff row"):
                        _ = branch_page.publish(fixture.repository)

    def test_published_page_serves_checked_rows_and_deletes_through_the_form(self) -> None:
        with scratch_branches() as fixture:
            hook = fixture.origin / "hooks" / "update"
            gate = fixture.root / "delete.lock"
            _ = hook.write_text('#!/usr/bin/env python3\nimport fcntl\nimport sys\nfrom pathlib import Path\n'
                                + 'if sys.argv[1] == "refs/heads/TEST-rejected":\n'
                                + '    print("TEST deletion refused", file=sys.stderr)\n    raise SystemExit(1)\n'
                                + 'if sys.argv[1] == "refs/heads/TEST-merged":\n'
                                + f'    with Path({str(gate)!r}).open("a") as lock:\n'
                                + '        fcntl.flock(lock, fcntl.LOCK_EX)\n', encoding="utf-8")
            hook.chmod(0o755)
            rows = fixture.listing().rows
            url = branch_page.publish(fixture.repository)
            root = fixture.root / "reviews"
            page = branch_page.page_at(root, urlsplit(url).path)
            self.assertEqual(page.rows, rows)
            self.assertEqual(page.publisher, fixture.publisher)
            messages: Queue[tuple[staff.StaffId, str, str]] = Queue()
            sent: list[tuple[staff.StaffId, str, str]] = []
            completed: Queue[Path] = Queue()
            started: Queue[Path] = Queue()
            fail_send = False

            def receive(identity: staff.StaffId, sender: str, message: str) -> None:
                nonlocal fail_send
                if fail_send:
                    fail_send = False
                    raise RuntimeError("TEST result send failed")
                sent.append((identity, sender, message))
                messages.put((identity, sender, message))

            delete_branches = serve.ReviewLinksHandler.delete_branches

            def finish_request(handler: serve.ReviewLinksHandler, current_page: branch_page.BranchPage) -> None:
                try:
                    delete_branches(handler, current_page)
                finally:
                    completed.put(current_page.folder)

            delete_selected = branch_page.delete_selected

            def start_deletion(deletion: branch_page.BranchDeletion) -> tuple[branch_page.DeleteResult, ...]:
                started.put(deletion.page.folder)
                return delete_selected(deletion)

            _ = self.enterContext(patch.object(serve.ReviewLinksHandler, "delete_branches", new=finish_request))
            deletions = self.enterContext(patch.object(branch_page, "delete_selected", side_effect=start_deletion))
            _ = self.enterContext(patch.object(change_page, "send_to_staff", side_effect=receive))
            _ = self.enterContext(patch.object(serve, "tailscale_ip", return_value="127.0.0.1"))
            address = serve.tailscale_ip()
            with serve.ReviewLinksServer((address, 0), root) as server:
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
                thread.start()
                try:
                    connection = HTTPConnection(address, server.server_port, timeout=TEST_HANG_GUARD_SECS)
                    self.addCleanup(connection.close)
                    path = urlsplit(url).path
                    connection.request("GET", path)
                    response = connection.getresponse()
                    content = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                    form = BranchForm()
                    form.feed(content)
                    self.assertIn("TEST-merged", form.checked)
                    no_owner_rows = [row.name for row in page.rows if isinstance(row, branch_page.MergedUnownedBranch)]
                    kept_branch = no_owner_rows[0]
                    selected = ["TEST-merged", "TEST-rejected", *no_owner_rows[1:]]
                    body = urlencode({"token": form.token, "branch": selected}, doseq=True)
                    before = fixture.remote_names()
                    with file_lock.exclusive(gate):
                        connection.request("POST", path, body, {"Content-Type": "application/x-www-form-urlencoded"})
                        response = connection.getresponse()
                        content = response.read().decode("utf-8")
                        self.assertEqual(response.status, 200)
                        self.assertIn(f"Deleting {len(selected)} branches. The result and a new link go to production by message.",
                                      content)
                        self.assertEqual(started.get(timeout=TEST_HANG_GUARD_SECS), page.folder)
                        self.assertEqual(fixture.remote_names(), before)
                        saved_kept = cast(list[str], json.loads((root / branch_page.KEPT).read_text(encoding="utf-8")))
                        self.assertIn(kept_branch, saved_kept)
                        self.assertFalse((page.folder / branch_page.USED).exists())
                        for method in ("GET", "POST"):
                            with self.subTest(active_method=method):
                                connection.request(method, path, body if method == "POST" else None,
                                                   {"Content-Type": "application/x-www-form-urlencoded"})
                                response = connection.getresponse()
                                content = response.read().decode("utf-8")
                                self.assertEqual(response.status, 200)
                                self.assertIn("Deleting these branches now. The result comes by message.", content)
                                if method == "POST":
                                    self.assertEqual(completed.get(timeout=TEST_HANG_GUARD_SECS), page.folder)
                                self.assertEqual(fixture.remote_names(), before)
                        self.assertEqual(deletions.call_count, 1)
                        stamps = dict(server.module_stamps)
                        with patch.object(serve, "loaded_module_stamps",
                                          return_value={Path(serve.__file__): serve.ModuleStampUnavailable.UNREADABLE}):
                            server.next_code_check = 0
                            server.service_actions()
                        self.assertEqual(server.module_stamps, stamps)
                    identity, sender, message = messages.get(timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(completed.get(timeout=TEST_HANG_GUARD_SECS), page.folder)
                    self.assertTrue((page.folder / branch_page.USED).exists())
                    with server.branch_deletions_lock:
                        self.assertFalse(server.branch_deletions)
                    self.assertEqual(identity, fixture.publisher)
                    self.assertEqual(sender, change_page.SENDER)
                    self.assertIn("TEST deletion refused", message)
                    self.assertIn("TEST-merged: deleted", message)
                    for name in selected:
                        if name != "TEST-rejected":
                            self.assertIn(f"{name}: deleted", message)
                    self.assertEqual(fixture.remote_names(), before - (set(selected) - {"TEST-rejected"}))
                    next_link = re.search(r"Next page: (\S+) \((\d+) of (\d+) no-owner branches\)", message)
                    if next_link is None:
                        self.fail(message)
                    next_path = urlsplit(next_link[1]).path
                    next_page = branch_page.page_at(root, next_path)
                    self.assertEqual(next_page.publisher, fixture.publisher)
                    self.assertEqual(int(next_link[2]), sum(isinstance(row, branch_page.MergedUnownedBranch)
                                                           for row in next_page.rows))
                    self.assertEqual(int(next_link[3]), page.unowned_total - len(no_owner_rows))
                    connection.request("GET", next_path)
                    response = connection.getresponse()
                    content = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                    self.assertNotIn("No branches are available to delete.", content)
                    retry = BranchForm()
                    retry.feed(content)
                    self.assertIn("TEST-rejected", retry.offered)
                    self.assertIn("TEST-rejected", retry.checked)
                    self.assertIn("TEST-keep", retry.offered)
                    self.assertNotIn("TEST-merged", retry.offered)
                    self.assertNotIn(kept_branch, retry.offered)
                    self.assertIn("unowned-10", retry.offered)
                    self.assertIn("unowned-11", retry.offered)
                    for method in ("GET", "POST"):
                        with self.subTest(method=method):
                            before_reuse = fixture.remote_names()
                            connection.request(method, path, body if method == "POST" else None,
                                               {"Content-Type": "application/x-www-form-urlencoded"})
                            response = connection.getresponse()
                            content = response.read().decode("utf-8")
                            self.assertEqual(response.status, 200)
                            self.assertIn("This page was submitted. The new link comes by message.", content)
                            self.assertEqual(fixture.remote_names(), before_reuse)
                            self.assertEqual(len(sent), 1)
                    self.assertIn(kept_branch, fixture.remote_names())
                    fail_send = True
                    retry_body = urlencode({"token": retry.token, "branch": ["TEST-rejected"]}, doseq=True)
                    connection.request("POST", next_path, retry_body,
                                       {"Content-Type": "application/x-www-form-urlencoded"})
                    response = connection.getresponse()
                    content = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                    self.assertIn("Deleting 1 branches.", content)
                    self.assertEqual(completed.get(timeout=TEST_HANG_GUARD_SECS), next_page.folder)
                    self.assertFalse((next_page.folder / branch_page.USED).exists())
                    self.assertFalse(fail_send)
                    self.assertEqual(len(sent), 1)
                    connection.request("GET", next_path)
                    response = connection.getresponse()
                    content = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                    self.assertNotIn(branch_page.SUBMITTED, content)
                    reopened = BranchForm()
                    reopened.feed(content)
                    self.assertIn("TEST-rejected", reopened.offered)
                    self.assertEqual(fixture.remote_names(), before - (set(selected) - {"TEST-rejected"}))
                    hook.unlink()
                    connection.request("POST", next_path, urlencode({"token": retry.token, "branch": ["TEST-rejected"]},
                                                                    doseq=True),
                                       {"Content-Type": "application/x-www-form-urlencoded"})
                    response = connection.getresponse()
                    content = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                    self.assertIn("Deleting 1 branches.", content)
                    identity, sender, message = messages.get(timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(completed.get(timeout=TEST_HANG_GUARD_SECS), next_page.folder)
                    self.assertIn("TEST-rejected: deleted", message)
                    self.assertEqual(fixture.remote_names(), before - set(selected))
                    final_link = re.search(r"Next page: (\S+)", message)
                    if final_link is None:
                        self.fail(message)
                    final_path = urlsplit(final_link[1]).path
                    final_page = branch_page.page_at(root, final_path)
                    final_selection = [row.name for row in final_page.rows]
                    connection.request("POST", final_path,
                                       urlencode({"token": final_page.token, "branch": final_selection}, doseq=True),
                                       {"Content-Type": "application/x-www-form-urlencoded"})
                    response = connection.getresponse()
                    _ = response.read()
                    self.assertEqual(response.status, 200)
                    identity, sender, message = messages.get(timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(completed.get(timeout=TEST_HANG_GUARD_SECS), final_page.folder)
                    self.assertIn("No branches remain to delete.", message)
                    self.assertEqual(fixture.remote_names(), before - set(selected) - set(final_selection))
                    connection.request("GET", f"/{path.split('/')[1]}/{branch_page.MANIFEST}")
                    response = connection.getresponse()
                    _ = response.read()
                    self.assertEqual(response.status, 404)
                finally:
                    server.shutdown()
                    thread.join(TEST_HANG_GUARD_SECS)


if __name__ == "__main__":
    _ = unittest.main()
