"""Test timer migration with private state folders."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest import mock

from scripts.lib.atomic_file import write_text_atomic
from scripts.production import migrate_showrunner_timers as migration, session_staff, showrunners, staff, staff_sources
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id


class TimerMigrationTests(StaffTestCase):
    def timer(self, name: str) -> tuple[staff.StaffRow, Path]:
        doc = self.root / f"{name}-production.md"
        write_text_atomic(doc, "# Production\n")
        row = fixture_member("showrunner", name=name, doc=staff.LiveDoc("natedev", doc))
        folder = self.root / "timers" / f"showrunner-{name}"
        folder.mkdir(parents=True)
        write_text_atomic(folder / "conf", f"TARGET=session:{row.id}\nEVERY=17\nSOCKET=/tmp/socket\n"
                          + f"CHECK=zsh /tools/production_check.sh {doc}\nZONE=UTC\n")
        write_text_atomic(folder / "state", "ENABLED=0\nNEXT_DUE=123\n")
        write_text_atomic(folder / "prompt", "Keep the prompt.\n")
        return row, folder

    def test_rerun_preserves_timer_state_and_fixed_owner_after_clear(self) -> None:
        row, old = self.timer("old")
        before = (old / "state").read_bytes()
        _ = staff.update_current_session(row.current_session, staff.native_id(fixture_runtime_id("clear")))
        self.assertEqual(migration.migrate(old.parent), [str(old.with_name(f"showrunner-{row.id}"))])
        destination = old.with_name(f"showrunner-{row.id}")
        self.assertEqual((destination / "state").read_bytes(), before)
        self.assertEqual((destination / "prompt").read_text(), "Keep the prompt.\n")
        self.assertEqual((destination / "conf").read_text(), f"TARGET=session:{row.id}\nEVERY=17\n"
                         + f"SOCKET=/tmp/socket\nCHECK=zsh /tools/production_check.sh {row.id}\nZONE=UTC\n")
        self.assertEqual(migration.migrate(destination.parent), [])
        with mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", destination.parent):
            timer = showrunners.timer_for(row, staff.snapshot())
        self.assertIsInstance(timer, showrunners.TimerRegistration)
        assert isinstance(timer, showrunners.TimerRegistration)
        self.assertEqual(timer.slug, row.name)
        self.assertEqual(showrunners.checked_doc(destination),
                         showrunners.CheckedDoc(self.root / "old-production.md"))

    def test_migrated_timer_keeps_the_production_label_and_supplied_staff(self) -> None:
        row, folder = self.timer("hana")
        _ = migration.migrate(folder.parent)
        row = staff.rename(row.id, "hana-owner")
        record = staff.snapshot()
        with (mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", folder.parent),
              mock.patch.object(staff, "snapshot", side_effect=AssertionError("staff was read again"))):
            sources = showrunners.read_sources(unreadable=showrunners.UnreadableTimerPolicy.RAISE, record=record)
            runners = showrunners.registered_showrunners(known_sessions={}, membership=record)
            members = staff_sources.showrunner_members(record)
        self.assertEqual([source.timer.slug for source in sources], ["hana"])
        self.assertEqual([runner["slug"] for runner in runners], ["hana"])
        self.assertEqual(members, {"hana": row})

    def test_production_membership_does_not_require_a_matching_timer_target(self) -> None:
        row, folder = self.timer("member")
        source = showrunners.read_bound_source(folder, self.root / "member-production.md")
        source = replace(source, timer=source.timer._replace(target="session:unrecorded"))
        sources = session_staff.MemberSources({"member": source}, ())
        self.assertEqual(sources.member(session_staff.ShowrunnerMember("member"), staff.snapshot()),
                         session_staff.MemberFound(row))

    def test_interrupted_folder_move_finishes_on_rerun(self) -> None:
        first, old = self.timer("a")
        second, later = self.timer("b")
        rename = Path.rename

        def interrupt(path: Path, target: Path) -> Path:
            if path == later:
                raise OSError("interrupted")
            return rename(path, target)

        with mock.patch.object(Path, "rename", interrupt), self.assertRaisesRegex(OSError, "interrupted"):
            _ = migration.migrate(old.parent)
        self.assertTrue(old.with_name(f"showrunner-{first.id}").exists())
        self.assertEqual(migration.migrate(old.parent), [str(later.with_name(f"showrunner-{second.id}"))])
        self.assertEqual(migration.migrate(old.parent), [])

    def test_unavailable_ownership_preserves_files(self) -> None:
        row, folder = self.timer("unknown")
        before = (folder / "conf").read_bytes()
        self.forget_rows(row.id)
        with self.assertRaisesRegex(ValueError, "ownership is unavailable"):
            _ = migration.migrate(folder.parent)
        self.assertEqual((folder / "conf").read_bytes(), before)

    def test_remote_or_timerless_staff_remains_a_member(self) -> None:
        row, folder = self.timer("remote")
        _ = migration.migrate(folder.parent)
        folder = folder.with_name(f"showrunner-{row.id}")
        remote = replace(row, doc=staff.LiveDoc("other-machine", self.root / "remote-production.md"))
        with mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((remote,))):
            self.assertIsInstance(showrunners.checked_doc(folder), showrunners.NoCheckedDoc)
        with mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.root / "absent"):
            self.assertIsInstance(showrunners.timer_for(row, staff.snapshot()), showrunners.NoTimer)
        self.assertEqual(staff.snapshot().by_id(row.id), row)

    def test_timer_lookup_does_not_require_a_document(self) -> None:
        row = fixture_member("showrunner", name="without-document")
        folder = self.root / "timers" / f"showrunner-{row.id}"
        folder.mkdir(parents=True)
        write_text_atomic(folder / "conf", f"TARGET=session:{row.id}\nEVERY=17\n")
        record = staff.snapshot()
        with mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", folder.parent):
            timer = showrunners.timer_for(row, record)
            runners = showrunners.registered_showrunners(known_sessions={}, membership=record)
        self.assertIsInstance(timer, showrunners.TimerRegistration)
        assert isinstance(timer, showrunners.TimerRegistration)
        self.assertEqual(timer.target, f"session:{row.id}")
        self.assertEqual(len(runners), 1)
        self.assertEqual(runners[0]["slug"], row.name)
        self.assertEqual(runners[0]["doc"], "")
        self.assertEqual(runners.lookup_failures, ())
