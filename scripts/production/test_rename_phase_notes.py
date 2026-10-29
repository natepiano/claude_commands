"""Keep phase notes writable when a production takes a new name."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import final, override
from unittest import mock

from scripts.direct import phase_table
from scripts.production import fake_showrunner
from scripts.production import rename_test_support as rename_tests


OLD_LINK = "https://github.com/natepiano/claude_commands/blob/main/docs/plans/old-production.md"
NEW_LINK = "https://github.com/natepiano/claude_commands/blob/main/docs/plans/new-production.md"
BODY = (
    "\n# Saved phase — café\r\n\n"
    "production: old\n"
    f'production_file: "{OLD_LINK}"\r\n'
    "| Phase | Status |\n| --- | --- |\r\n| 1 | done |\n\n"
    "No final newline."
).encode("utf-8")


def write_note(vault: Path, folder: str, name: str, owner: str, *,
               unit: str = "alpha-unit", body: bytes = BODY) -> Path:
    path = vault / folder / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "---\nphase_table: true\n"
        f"showrunner: {folder}\n{owner}\nunit: {unit}\n"
        'now: "Phase 1 — Saved work"\n'
        "tags: [keep, old]\n---\n"
    )
    _ = path.write_bytes(header.encode("utf-8") + body)
    return path


def note_fields(path: Path) -> dict[str, str]:
    header, separator, _body = path.read_bytes().partition(b"\n---\n")
    if not separator:
        raise AssertionError(f"phase note has no frontmatter end: {path}")
    fields: dict[str, str] = {}
    for line in header.decode("utf-8").splitlines()[1:]:
        key, separator, value = line.partition(":")
        if separator:
            fields[key] = value.strip()
    return fields


@final
class PhaseNoteRetargetTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.vault = Path()
        self.new_doc = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.vault = self.root / "vault/showrunners"
        self.new_doc = self.root / "docs/new-production.md"
        self.new_doc.parent.mkdir()
        _ = self.new_doc.write_text("# New production\n", encoding="utf-8")
        self.enterContext(mock.patch.dict(os.environ, {"PHASE_TABLE_VAULT": str(self.vault)}))

    def test_retarget_preserves_body_and_other_fields_and_does_not_write_twice(self) -> None:
        note = write_note(self.vault, "runner", "alpha-unit", f'production_file: "{OLD_LINK}"')
        fields = note_fields(note)
        del fields["production_file"]

        rewritten = phase_table.retarget_phase_notes("old", self.new_doc)

        self.assertEqual(rewritten, [note])
        self.assertEqual(json.loads(note_fields(note).pop("production_file")), self.new_doc.as_uri())
        after_fields = note_fields(note)
        del after_fields["production_file"]
        self.assertEqual(after_fields, fields)
        self.assertEqual(note.read_bytes().partition(b"\n---\n")[2], BODY)
        after_bytes = note.read_bytes()
        after_stat = note.stat()

        self.assertEqual(phase_table.retarget_phase_notes("old", self.new_doc), [])

        self.assertEqual(note.read_bytes(), after_bytes)
        self.assertEqual((note.stat().st_ino, note.stat().st_mtime_ns),
                         (after_stat.st_ino, after_stat.st_mtime_ns))
        self.assertEqual(list(note.parent.iterdir()), [note])

    def test_retarget_replaces_the_file_instead_of_editing_it_in_place(self) -> None:
        note = write_note(self.vault, "runner", "alpha-unit", f'production_file: "{OLD_LINK}"')
        before = note.read_bytes()
        old_file = self.root / "old-note.md"
        os.link(note, old_file)

        self.assertEqual(phase_table.retarget_phase_notes("old", self.new_doc), [note])

        self.assertEqual(old_file.read_bytes(), before)
        self.assertNotEqual(note.read_bytes(), before)
        self.assertNotEqual(note.stat().st_ino, old_file.stat().st_ino)

    def test_retarget_waits_for_the_showrunner_folder_lock(self) -> None:
        note = write_note(self.vault, "runner", "alpha-unit", f'production_file: "{OLD_LINK}"')
        before = note.read_bytes()
        progress = Event()
        blocked = Event()
        real_flock = fcntl.flock

        def observe_flock(fd: int, operation: int) -> None:
            if operation & fcntl.LOCK_EX:
                try:
                    real_flock(fd, operation | fcntl.LOCK_NB)
                except BlockingIOError:
                    blocked.set()
                    progress.set()
                    real_flock(fd, operation)
            else:
                real_flock(fd, operation)

        def retarget() -> list[Path]:
            try:
                return phase_table.retarget_phase_notes("old", self.new_doc)
            finally:
                progress.set()

        folder_fd = os.open(note.parent, os.O_RDONLY)
        try:
            real_flock(folder_fd, fcntl.LOCK_EX)
            with mock.patch.object(fcntl, "flock", side_effect=observe_flock):
                with ThreadPoolExecutor(max_workers=1) as worker:
                    pending = worker.submit(retarget)
                    try:
                        self.assertTrue(progress.wait(timeout=2), "retarget did not reach the folder lock")
                        self.assertTrue(blocked.is_set(), "retarget did not wait for the held folder lock")
                        self.assertFalse(pending.done())
                        self.assertEqual(note.read_bytes(), before)
                    finally:
                        real_flock(folder_fd, fcntl.LOCK_UN)
                    self.assertEqual(pending.result(timeout=2), [note])
        finally:
            os.close(folder_fd)

        self.assertEqual(json.loads(note_fields(note)["production_file"]), self.new_doc.as_uri())
        self.assertEqual(note.read_bytes().partition(b"\n---\n")[2], BODY)
        self.assertEqual(list(note.parent.iterdir()), [note])

    def test_retarget_finds_legacy_notes_in_each_showrunner_folder(self) -> None:
        first = write_note(self.vault, "runner-before", "alpha", "production: old")
        second = write_note(self.vault, "runner-now", "beta", "production: old", unit="beta-unit")

        self.assertEqual(set(phase_table.retarget_phase_notes("old", self.new_doc)), {first, second})

        for note, unit in ((first, "alpha-unit"), (second, "beta-unit")):
            with self.subTest(note=note):
                fields = note_fields(note)
                self.assertNotIn("production", fields)
                self.assertEqual(json.loads(fields["production_file"]), self.new_doc.as_uri())
                self.assertEqual(fields["unit"], unit)
                self.assertEqual(note.read_bytes().partition(b"\n---\n")[2], BODY)
                self.assertEqual(phase_table.owned_notes("new", unit), [note])

    def test_link_owner_takes_precedence_and_other_notes_stay_unchanged(self) -> None:
        owned = write_note(self.vault, "runner", "mine", f'production_file: "{OLD_LINK}"')
        other_link = OLD_LINK.replace("old-production.md", "other-production.md")
        others = [
            write_note(self.vault, "runner", "other-link", f'production_file: "{other_link}"'),
            write_note(self.vault, "runner", "other-legacy", "production: other"),
            write_note(self.vault, "runner", "link-wins",
                       f'production_file: "{other_link}"\nproduction: old'),
            write_note(self.vault, "runner", "oldish",
                       f'production_file: "{OLD_LINK.replace("old-production", "oldish-production")}"'),
            write_note(self.vault, "runner", "handwritten", "production: old"),
        ]
        handwritten = others[-1]
        _ = handwritten.write_bytes(handwritten.read_bytes().replace(b"phase_table: true", b"phase_table: false"))
        before = {note: note.read_bytes() for note in others}

        self.assertEqual(phase_table.retarget_phase_notes("old", self.new_doc), [owned])

        self.assertEqual({note: note.read_bytes() for note in others}, before)
        self.assertEqual(phase_table.owned_notes("new", "alpha-unit"), [owned])

    def test_link_owned_note_drops_its_legacy_field(self) -> None:
        note = write_note(self.vault, "runner", "alpha",
                          f'production_file: "{OLD_LINK}"\nproduction: old')

        self.assertEqual(phase_table.retarget_phase_notes("old", self.new_doc), [note])

        fields = note_fields(note)
        self.assertNotIn("production", fields)
        self.assertEqual(json.loads(fields["production_file"]), self.new_doc.as_uri())
        self.assertEqual(note.read_bytes().partition(b"\n---\n")[2], BODY)

    def test_missing_vault_is_no_work_and_is_not_created(self) -> None:
        self.assertEqual(phase_table.retarget_phase_notes("old", self.new_doc), [])
        self.assertFalse(self.vault.parent.exists())


@final
class ProductionRenamePhaseNoteTests(unittest.TestCase):
    @classmethod
    @override
    def setUpClass(cls) -> None:
        rename_tests.ProductionRenameFixture.setUpClass()
        cls.addClassCleanup(rename_tests.ProductionRenameFixture.doClassCleanups)

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture = rename_tests.ProductionRenameFixture()
        self.vault = Path()

    @override
    def setUp(self) -> None:
        self.fixture = rename_tests.ProductionRenameFixture()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.vault = self.fixture.root / "vault/showrunners"
        self.fixture.env["PHASE_TABLE_VAULT"] = str(self.vault)

    def test_apply_allows_the_next_real_phase_table_write_and_links_the_main_doc(self) -> None:
        fixture = self.fixture
        main = fixture.root / "main"
        main.mkdir()
        _ = fixture.git("-C", str(main), "init", "-b", "main")
        _ = fixture.git("-C", str(main), "remote", "add", "origin",
                        "git@github.com:natepiano/claude_commands.git")
        fixture.write(fixture.doc, fixture.doc.read_text().replace(
            "## Production Context\n", f"## Production Context\n\n- **Promote:** {main}\n"))
        fixture.write(fixture.plan_path("alpha-unit"), fixture.plan_path("alpha-unit").read_text()
                      + "\n### Phase 1 — Next delivery · status: todo\n")
        _ = fixture.git("add", "docs/plans")
        _ = fixture.git("commit", "-m", "Add the phase and main checkout")
        note = write_note(self.vault, "runner-now", "alpha-unit", f'production_file: "{OLD_LINK}"')

        fixture.assert_success(fixture.run_rename("apply"))

        self.assertEqual(json.loads(note_fields(note)["production_file"]), NEW_LINK)
        held = fake_showrunner.write_session(fixture.root / "state/sessions", "runner-now", "test-showrunner")
        self.addCleanup(held.close)
        session = fixture.root / "phase-session"
        fixture.write(session / "progress_history_state.json", json.dumps({
            "working_dir": str(fixture.checkout),
            "project_plan_doc": str(fixture.plan_path("alpha-unit")),
            "status": "active", "phase": None,
        }))
        result = subprocess.run(
            [sys.executable, str(Path(phase_table.__file__)), "refresh", "--session-dir", str(session)],
            cwd=fixture.checkout, env=fixture.env, capture_output=True, text=True, check=False, timeout=30,
        )

        fixture.assert_success(result)
        self.assertEqual(json.loads(note_fields(note)["production_file"]), NEW_LINK)
        self.assertIn("Next delivery", note.read_text(encoding="utf-8"))
        self.assertNotIn("Saved phase", note.read_text(encoding="utf-8"))
        self.assertEqual(list(self.vault.glob("*/*.md")), [note])

    def test_repair_prints_each_rewritten_path_and_only_changes_owned_notes(self) -> None:
        fixture = self.fixture
        fixture.assert_success(fixture.run_rename("apply"))
        first = write_note(self.vault, "runner-before", "alpha", f'production_file: "{OLD_LINK}"')
        second = write_note(self.vault, "runner-now", "beta", "production: old", unit="beta-unit")
        other = write_note(self.vault, "runner-now", "other", "production: other")
        before = fixture.snapshot()

        result = fixture.run_command("repair-notes", "old", "new")

        fixture.assert_success(result)
        self.assertEqual(set(result.stdout.splitlines()), {str(first), str(second)})
        self.assertEqual(result.stderr, "")
        after = fixture.snapshot()
        changed = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
        self.assertEqual(changed, {str(first.relative_to(fixture.root)), str(second.relative_to(fixture.root))})
        for note in (first, second):
            self.assertEqual(json.loads(note_fields(note)["production_file"]), fixture.new_doc.as_uri())
            self.assertNotIn("production", note_fields(note))
            self.assertEqual(note.read_bytes().partition(b"\n---\n")[2], BODY)
        self.assertEqual(other.read_bytes(), before[str(other.relative_to(fixture.root))])

        second_result = fixture.run_command("repair-notes", "old", "new")

        fixture.assert_success(second_result)
        self.assertEqual(second_result.stdout.strip(), "no phase notes to repair")
        self.assertEqual(fixture.snapshot(), after)

    def test_repair_with_no_vault_prints_no_phase_notes_to_repair(self) -> None:
        fixture = self.fixture
        fixture.assert_success(fixture.run_rename("apply"))
        before = fixture.snapshot()

        result = fixture.run_command("repair-notes", "old", "new")

        fixture.assert_success(result)
        self.assertEqual(result.stdout.strip(), "no phase notes to repair")
        self.assertEqual(result.stderr, "")
        self.assertFalse(self.vault.exists())
        self.assertEqual(fixture.snapshot(), before)

    def test_unit_move_retargets_a_note_written_after_apply(self) -> None:
        fixture = self.fixture
        fixture.prepare_unit_moves()
        fixture.marked_unit("alpha-unit")
        fixture.assert_success(fixture.run_rename("apply"))
        note = write_note(self.vault, "runner", "alpha-unit", f'production_file: "{OLD_LINK}"')

        fixture.assert_success(fixture.run_command("unit", "new", "alpha-unit"))

        self.assertEqual(json.loads(note_fields(note)["production_file"]), fixture.new_doc.as_uri())
        self.assertEqual(note.read_bytes().partition(b"\n---\n")[2], BODY)
        self.assertTrue(fixture.unit_worktree("new", "alpha-unit").is_dir())

    def test_apply_resumes_an_old_journal_and_repairs_notes_without_a_new_commit(self) -> None:
        fixture = self.fixture
        fixture.assert_success(fixture.run_rename("apply"))
        journal = fixture.journal()
        # These are all the apply steps before phase-note repair was added.
        journal["completed"] = [
            "_rename_doc", "_rewrite_plans", "_rename_timer", "_rename_showrunner_state",
            "_rename_stall_watch", "_rename_review_pool", "_rename_unit_workers", "_rename_branch",
        ]
        fixture.write(fixture.state / "new/rename.json", json.dumps(journal))
        note = write_note(self.vault, "runner", "alpha-unit", f'production_file: "{OLD_LINK}"')
        tip = fixture.git("rev-parse", "new")

        fixture.assert_success(fixture.run_rename("apply"))

        self.assertEqual(json.loads(note_fields(note)["production_file"]), fixture.new_doc.as_uri())
        self.assertEqual(fixture.git("rev-parse", "new"), tip)
        self.assertEqual(note.read_bytes().partition(b"\n---\n")[2], BODY)

    def test_plan_lists_phase_note_repair_without_changing_notes(self) -> None:
        fixture = self.fixture
        note = write_note(self.vault, "runner", "alpha-unit", f'production_file: "{OLD_LINK}"')
        before = fixture.snapshot()

        result = fixture.run_rename("plan")

        fixture.assert_success(result)
        self.assertRegex(result.stdout, r"(?im)^.*phase notes?.*$")
        self.assertEqual(fixture.snapshot(), before)
        self.assertEqual(json.loads(note_fields(note)["production_file"]), OLD_LINK)


if __name__ == "__main__":
    _ = unittest.main()
