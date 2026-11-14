"""The morty holder is the native conversation on the producer row named morty."""
from __future__ import annotations

from unittest import mock

from scripts.message import morty_identity
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id


class MortyIdentityTests(StaffTestCase):
    def test_the_holder_is_the_row_and_follows_a_new_conversation(self) -> None:
        first, second = fixture_runtime_id("morty-first"), fixture_runtime_id("morty-second")
        self.assertEqual(morty_identity.holder(), morty_identity.NoMortyHolder())
        row = fixture_member("producer", name="morty", runtime_id=first)
        self.assertEqual(morty_identity.holder(), morty_identity.MortyHolder(staff.StaffId(first)))
        _ = staff.update_current_session(row.current_session, staff.native_id(second))
        self.assertEqual(morty_identity.holder(), morty_identity.MortyHolder(staff.StaffId(second)))

    def test_a_row_of_another_kind_or_name_is_not_the_holder(self) -> None:
        _ = fixture_member("showrunner", name="morty", runtime_id=fixture_runtime_id("unit-morty"))
        _ = fixture_member("producer", name="brain", runtime_id=fixture_runtime_id("producer-brain"))
        self.assertEqual(morty_identity.holder(), morty_identity.NoMortyHolder())

    def test_an_unreadable_or_absent_record_gives_no_holder_and_the_retained_copy_is_never_read(self) -> None:
        _ = fixture_member("producer", name="morty", runtime_id=fixture_runtime_id("morty"))
        for error in (staff.StaffUnreadable("natedev not reached"), staff.StaffAbsent("no staff record")):
            with (self.subTest(error=type(error).__name__), mock.patch.object(staff, "snapshot", side_effect=error),
                  mock.patch.object(staff, "local_snapshot", wraps=staff.local_snapshot) as retained):
                self.assertEqual(morty_identity.holder(), morty_identity.NoMortyHolder())
            retained.assert_not_called()
