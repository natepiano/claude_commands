"""Registration behavior against a private seeded fixture."""

from __future__ import annotations

import json
import unittest
from typing import cast

from scripts.production.update_registration_test_support import RegistrationFixture

from scripts.production.update_registration_test_support import setUpModule as setUpModule


class RegistrationTests(RegistrationFixture):
    def test_open_lists_only_the_productions_own_items_in_full_never_those_below(self) -> None:
        for slug in ("example", "kid", "grandkid", "stranger"):
            self.timer(slug)
        self.attach("kid", "example")
        self.attach("grandkid", "kid")
        self.write_open_for("example", {"since": "2026-10-06T09:00", "text": "choose layout"},
                            {"since": "2026-10-05T07:00", "text": "review colors", "after": "2099-01-02T08:00"})
        self.write_open_for("kid", {"since": "2026-10-06T08:00", "text": "merge today, or after the release?"})
        self.write_open_for("grandkid", {"since": "2026-09-28T10:30", "text": "keep the old key binding?"})
        self.write_open_for("stranger", {"since": "2026-10-01T08:00", "text": "not this tree's"})
        # The showrunners below answer for their own items (user, 2026-10-27).
        self.assertEqual(self.open_lines(), [
            "1. example, since Tue Oct 06 09:00: choose layout",
            "deferred until Fri Jan 02 08:00: example: review colors",
            "open: ok — 1 due, 1 deferred",
        ])

    def test_open_names_an_unreadable_list(self) -> None:
        self.timer("example")
        _ = self.outstanding().write_text("{bad", encoding="utf-8")
        self.assertEqual(self.open_lines(), [
            "example: needs-user list unreadable",
            "open: ok — 0 due, 0 deferred, 1 unreadable",
        ])

    def test_a_middle_showrunner_lists_its_own_items_and_marks_one_below_never_above(self) -> None:
        for slug in ("top", "example", "kid"):
            self.timer(slug)
        self.attach("example", "top")
        self.attach("kid", "example")
        self.write_open_for("example", {"since": "2026-10-06T09:00", "text": "choose layout"})
        self.write_open_for("kid", {"since": "2026-10-06T08:00", "text": "merge today?"})
        self.write_open_for("top", {"since": "2026-10-06T07:00", "text": "ship it?"})
        self.assertEqual(self.open_lines(), [
            "1. example, since Tue Oct 06 09:00: choose layout",
            "open: ok — 1 due, 0 deferred",
        ])
        # `/next all-below` can show the kid's item, so it can be marked from here; the top's never.
        marked = self.run_command("mark-important", "--item", "merge today?")
        self.assertEqual(marked.stdout.strip(), "mark-important: ok — merge today?", marked.stdout + marked.stderr)
        self.assertNotEqual(self.run_command("mark-important", "--item", "ship it?").returncode, 0)

    def test_open_with_nothing_attached_lists_its_own_items_or_none(self) -> None:
        self.assertEqual(self.open_lines(), ["open: ok — 0 due, 0 deferred"])
        self.write_open_for("example", {"since": "2026-10-06T09:00", "text": "choose layout"})
        self.assertEqual(self.open_lines(), [
            "1. example, since Tue Oct 06 09:00: choose layout",
            "open: ok — 1 due, 0 deferred",
        ])

    def test_open_and_next_put_the_most_urgent_first_and_keep_the_shown_item(self) -> None:
        self.write_open_for("example", {"since": "2026-10-01T09:00", "text": "old normal"},
                            {"since": "2026-10-06T09:00", "text": "needs the old one", "urgency": "blocks",
                             "follows": "later one"},
                            {"since": "2026-10-02T09:00", "text": "later one", "urgency": "later"},
                            {"since": "2026-10-05T09:00", "text": "by friday", "urgency": "soon", "due": "2026-10-09"})
        self.assertEqual(self.open_lines(), [
            "1. example [later], since Fri Oct 02 09:00: later one",
            "2. example [blocks], since Tue Oct 06 09:00: needs the old one",
            "3. example [soon, due 2026-10-09], since Mon Oct 05 09:00: by friday",
            "4. example, since Thu Oct 01 09:00: old normal",
            "open: ok — 4 due, 0 deferred",
        ])
        shown = self.run_command("next")
        self.assertEqual(shown.stdout.splitlines(), ["example [later], since Fri Oct 02 09:00: later one",
                                                     "next: ok — 3 more due"])
        marked = self.run_command("mark-important", "--item", "old normal")
        self.assertEqual(marked.stdout.strip(), "mark-important: ok — old normal", marked.stdout + marked.stderr)
        # The shown item stays first until it is answered; the user's mark takes the next place.
        lines = self.open_lines()
        self.assertEqual(lines[0], "1. example [later], since Fri Oct 02 09:00: later one")
        self.assertTrue(lines[1].startswith("2. example [user, marked "), lines[1])
        self.write_open_for("example", *cast(list[dict[str, str]], json.loads(self.outstanding().read_text())[:2]))
        self.assertTrue(self.run_command("next").stdout.startswith("example [user, marked "))
        self.assertEqual(self.run_command("mark-important", "--item", "nothing like it").returncode, 2)


if __name__ == "__main__":
    _ = unittest.main()
