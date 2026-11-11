"""Prove branch selection and deletion with scratch records and a bare origin."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from http.client import HTTPConnection
from pathlib import Path
from typing import override
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit
from uuid import NAMESPACE_URL, uuid5

from scripts.agents import remote
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import staff
from scripts.review_links import branch_page, serve


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

    def command(self, *arguments: str) -> str:
        result = subprocess.run(["git", "-C", str(self.repository), *arguments],
                                capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def remote_names(self) -> set[str]:
        return {line.split()[1].removeprefix("refs/heads/")
                for line in self.command("ls-remote", "--heads", "origin").splitlines()}

    def rows(self) -> tuple[branch_page.MergedUnitBranch, ...]:
        return branch_page.list_branches(self.repository, staff.snapshot(), (self.history,))


@contextmanager
def scratch_branches() -> Generator[ScratchBranches, None, None]:
    with tempfile.TemporaryDirectory(prefix="branch-page-test-") as temporary:
        root = Path(temporary)
        repository, origin = root / "repository", root / "origin.git"
        repository.mkdir()
        history = root / "history"
        (history / "runs").mkdir(parents=True)
        fixture = ScratchBranches(root, repository, origin, history, root / "staff.json", root / "live", {})
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
                    else staff.RemovedWorktree(machine, foreign if evidence == "foreign-fallback"
                                              else repository if evidence in ("foreign", "owner-fallback") else root / "removed" / name))
            row = staff.StaffRow(identity, "showrunner" if kind == "showrunner" else "unit", name,
                                 staff.STAFF_ROOT if kind == "showrunner" else staff.ReportsTo(production_id),
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
            if evidence == "legacy":
                event.update(skill="plan-delegate", main_agent={"session_id": identity})
            else:
                event["owner_staff_id"] = identity
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
        _ = fixture.command("push", "origin", "--all")
        with (patch.dict(os.environ, {"STAFF_FILE": str(fixture.staff_file), "UNIT_HISTORY_DIR": str(history),
                                      "REVIEW_LINKS_ROOT": str(root / "reviews"), "REVIEW_LINKS_HOST": "127.0.0.1"}),
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
                expected = {"TEST-merged", "TEST-keep", "TEST-rejected", "TEST-legacy", "TEST-owner-fallback", "TEST-history-only"}
                self.assertEqual({row.name for row in rows}, expected)
                page = branch_page.BranchPage(fixture.repository, rows, "test-token")
                form = BranchForm()
                content = branch_page.render(page).decode("utf-8")
                form.feed(content)
                self.assertEqual(set(form.checked), expected)
                for row in rows:
                    source = f"origin/{row.name}" if live_directory == "remote-main" else row.name
                    self.assertEqual(row.unit, row.name)
                    self.assertEqual(row.tip, fixture.command("rev-parse", source))
                    self.assertEqual(row.merge, fixture.merges[row.name])
                    self.assertEqual(row.commit_date, fixture.command("show", "-s", "--format=%cI", source))
                    self.assertEqual(row.merge_date, fixture.command("show", "-s", "--format=%cI", row.merge))
                    self.assertIn(row.merge[:12], content)
                    self.assertIn(row.merge_date, content)

    def test_delete_reports_each_result_and_keeps_unchecked_branches(self) -> None:
        for state in ("unchanged", "advanced"):
            with self.subTest(state=state), scratch_branches() as fixture:
                hook = fixture.origin / "hooks" / "update"
                _ = hook.write_text('#!/bin/sh\nif [ "$1" = refs/heads/TEST-rejected ]; then\n'
                                    + '  echo "TEST deletion refused" >&2\n  exit 1\nfi\n', encoding="utf-8")
                hook.chmod(0o755)
                page = branch_page.BranchPage(fixture.repository, fixture.rows(), "test-token")
                if state == "advanced":
                    _ = fixture.command("checkout", "TEST-merged")
                    _ = fixture.command("commit", "--allow-empty", "-m", "New work after publication")
                    _ = fixture.command("push", "origin", "TEST-merged")
                before = fixture.remote_names()
                results = branch_page.submit(page, urlencode({"token": page.token,
                                                             "branch": ["TEST-rejected", "TEST-merged", "TEST-legacy"]}, doseq=True))
                self.assertEqual([result.name for result in results], ["TEST-rejected", "TEST-merged", "TEST-legacy"])
                self.assertIn("TEST deletion refused", results[0].message)
                self.assertEqual([result.message for result in results[1:]], ["deleted", "deleted"])
                self.assertEqual(fixture.remote_names(), before - {"TEST-merged", "TEST-legacy"})
                content = branch_page.render(page, results).decode("utf-8")
                self.assertIn("TEST deletion refused", content)
                self.assertIn("TEST-merged: deleted", content)

    def test_invalid_or_stale_selections_preserve_remote_branches(self) -> None:
        for change in ("unlisted", "token"):
            with self.subTest(change=change), scratch_branches() as fixture:
                page = branch_page.BranchPage(fixture.repository, fixture.rows(), "test-token")
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

    def test_published_page_serves_checked_rows_and_deletes_through_the_form(self) -> None:
        with scratch_branches() as fixture:
            hook = fixture.origin / "hooks" / "update"
            _ = hook.write_text('#!/bin/sh\nif [ "$1" = refs/heads/TEST-rejected ]; then\n'
                                + '  echo "TEST deletion refused" >&2\n  exit 1\nfi\n', encoding="utf-8")
            hook.chmod(0o755)
            url = branch_page.publish(fixture.repository)
            root = fixture.root / "reviews"
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
                    before = fixture.remote_names()
                    connection.request("POST", path, urlencode({"token": form.token,
                                                               "branch": ["TEST-merged", "TEST-rejected"]}, doseq=True),
                                       {"Content-Type": "application/x-www-form-urlencoded"})
                    response = connection.getresponse()
                    content = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                    self.assertIn("TEST-merged: deleted", content)
                    self.assertEqual(fixture.remote_names(), before - {"TEST-merged"})
                    retry = BranchForm()
                    retry.feed(content)
                    self.assertIn("TEST-rejected", retry.offered)
                    self.assertEqual(retry.checked, ["TEST-rejected"])
                    self.assertIn("TEST-keep", retry.offered)
                    self.assertNotIn("TEST-merged", retry.offered)
                    hook.unlink()
                    connection.request("POST", path, urlencode({"token": retry.token, "branch": retry.checked}, doseq=True),
                                       {"Content-Type": "application/x-www-form-urlencoded"})
                    response = connection.getresponse()
                    content = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                    self.assertIn("TEST-rejected: deleted", content)
                    self.assertEqual(fixture.remote_names(), before - {"TEST-merged", "TEST-rejected"})
                    cleared = BranchForm()
                    cleared.feed(content)
                    self.assertEqual(cleared.checked, [])
                    connection.request("GET", f"/{path.split('/')[1]}/{branch_page.MANIFEST}")
                    response = connection.getresponse()
                    _ = response.read()
                    self.assertEqual(response.status, 404)
                finally:
                    server.shutdown()
                    thread.join(TEST_HANG_GUARD_SECS)


if __name__ == "__main__":
    _ = unittest.main()
