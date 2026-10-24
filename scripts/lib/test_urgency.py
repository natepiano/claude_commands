#!/usr/bin/env python3
"""urgency.py: the order every queue shows its items in, and the pin on the item /next showed."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import urgency  # noqa: E402
from scripts.lib.urgency import Entry, Urgency, UrgencyError  # noqa: E402


def at(day: int, hour: int = 9) -> datetime:
    return datetime(2026, 10, day, hour, 0)


def keys(entries: list[Entry[str]]) -> list[str]:
    return [entry.key for entry in entries]


def shown(found: tuple[str, Entry[str]] | None) -> str:
    return found[1].key if found is not None else ""


class OrderTest(unittest.TestCase):
    def test_levels_then_value_then_oldest(self) -> None:
        entries = [
            Entry("old-normal", "", since=at(1)),
            Entry("later", "", Urgency(urgency.LATER), since=at(1)),
            Entry("new-normal", "", since=at(5)),
            Entry("blocks", "", Urgency(urgency.BLOCKS), since=at(9)),
            Entry("soon-late", "", urgency.read("soon", "2026-10-30"), since=at(1)),
            Entry("soon-early", "", urgency.read("soon", "2026-10-21T15:00"), since=at(9)),
            Entry("user-old", "", urgency.mark(at(2)), since=at(9)),
            Entry("user-new", "", urgency.mark(at(3)), since=at(1)),
        ]
        self.assertEqual(keys(urgency.order(entries)), ["user-new", "user-old", "blocks", "soon-early", "soon-late",
                                                        "old-normal", "new-normal", "later"])

    def test_higher_value_first_inside_a_level(self) -> None:
        entries = [Entry("small", "", since=at(1), value=(3.0,)), Entry("large", "", since=at(9), value=(12.0,))]
        self.assertEqual(keys(urgency.order(entries)), ["large", "small"])

    def test_prerequisite_moves_up_to_the_item_that_needs_it(self) -> None:
        entries = [Entry("first", "", Urgency(urgency.LATER)),
                   Entry("needs-first", "", Urgency(urgency.BLOCKS), after="first"),
                   Entry("other", "")]
        self.assertEqual(keys(urgency.order(entries)), ["first", "needs-first", "other"])

    def test_closed_prerequisite_and_cycle_keep_every_item(self) -> None:
        self.assertEqual(keys(urgency.order([Entry("a", "", after="gone")])), ["a"])
        cycle = urgency.order([Entry("a", "", after="b"), Entry("b", "", after="a")])
        self.assertEqual(sorted(keys(cycle)), ["a", "b"])

    def test_current_leads_while_open(self) -> None:
        entries = [Entry("quiet", "", Urgency(urgency.LATER)), Entry("loud", "", urgency.mark(at(4)))]
        self.assertEqual(keys(urgency.order(entries, current="quiet")), ["quiet", "loud"])
        self.assertEqual(keys(urgency.order(entries, current="answered")), ["loud", "quiet"])

    def test_oldest_and_newest_order_by_age_and_a_level_breaks_a_tie(self) -> None:
        entries = [Entry("blocks-new", "", Urgency(urgency.BLOCKS), since=at(5)),
                   Entry("old", "", since=at(1)), Entry("old-later", "", Urgency(urgency.LATER), since=at(1)),
                   Entry("no-age", "")]
        self.assertEqual(keys(urgency.order(entries, by=urgency.OLDEST)), ["old", "old-later", "blocks-new", "no-age"])
        self.assertEqual(keys(urgency.order(entries, by=urgency.NEWEST)), ["blocks-new", "old", "old-later", "no-age"])
        with self.assertRaises(UrgencyError):
            _ = urgency.order(entries, by="loudest")

    def test_a_moved_item_goes_last_in_every_order_until_marked_again(self) -> None:
        entries = [Entry("blocks", "", Urgency(urgency.BLOCKS), since=at(3)), Entry("a", "", since=at(1)),
                   Entry("b", "", since=at(2))]
        ended = {"blocks": at(6), "a": at(5)}
        self.assertEqual(keys(urgency.order(entries, ended=ended)), ["b", "a", "blocks"])
        self.assertEqual(keys(urgency.order(entries, by=urgency.OLDEST, ended=ended)), ["b", "a", "blocks"])
        marked = [Entry("a", "", urgency.mark(at(7)), since=at(1)), *entries[::2]]
        self.assertEqual(keys(urgency.order(marked, ended=ended)), ["a", "b", "blocks"])


class ReadTest(unittest.TestCase):
    def test_soon_needs_due_and_due_needs_soon(self) -> None:
        with self.assertRaises(UrgencyError):
            _ = urgency.read("soon")
        with self.assertRaises(UrgencyError):
            _ = urgency.read("blocks", "2026-10-22")
        with self.assertRaises(UrgencyError):
            _ = urgency.read("urgent")
        self.assertEqual(urgency.read(), Urgency())

    def test_fields_round_trip_and_store_nothing_for_normal(self) -> None:
        self.assertEqual(urgency.fields(Urgency()), {})
        soon = urgency.read("soon", "2026-10-22")
        self.assertEqual(urgency.fields(soon, "review_"), {"review_urgency": "soon", "review_due": "2026-10-22"})
        marked = urgency.mark(datetime(2026, 10, 20, 14, 3, 27))
        self.assertEqual(urgency.from_fields(urgency.fields(marked)), marked)
        self.assertEqual(urgency.fields(marked)["marked"], "2026-10-20T14:03")

    def test_cell_round_trip(self) -> None:
        self.assertEqual(urgency.cell(Urgency()), "")
        soon = urgency.read("soon", "2026-10-22")
        self.assertEqual(urgency.cell(soon, "C3"), "soon, due 2026-10-22, after C3")
        self.assertEqual(urgency.parse_cell("soon, due 2026-10-22, after C3"), (soon, "C3"))
        self.assertEqual(urgency.parse_cell(""), (Urgency(), ""))
        with self.assertRaises(UrgencyError):
            _ = urgency.parse_cell("blocks, whenever")

    def test_keep_mark(self) -> None:
        old = urgency.mark(at(2))
        self.assertEqual(urgency.keep_mark(Urgency(urgency.LATER), old, at(5)), old)
        self.assertEqual(urgency.keep_mark(Urgency(urgency.USER), Urgency(), at(5)), urgency.mark(at(5)))
        self.assertEqual(urgency.keep_mark(Urgency(urgency.LATER), Urgency(), at(5)), Urgency(urgency.LATER))


class PinTest(unittest.TestCase):
    def test_shown_item_stays_until_it_leaves(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"NEXT_STATE_DIR": folder}):
            first = [Entry("a", "", since=at(1)), Entry("b", "", since=at(2))]
            self.assertEqual(getattr(urgency.top(first, "change-x"), "key", None), "a")
            # A more urgent item arrives: the shown one stays current.
            arrived = [*first, Entry("c", "", Urgency(urgency.BLOCKS))]
            self.assertEqual(getattr(urgency.top(arrived, "change-x"), "key", None), "a")
            # After its verdict the top of the new order is shown and pinned.
            self.assertEqual(getattr(urgency.top(arrived[1:], "change-x"), "key", None), "c")
            self.assertEqual(urgency.pinned("change-x"), "c")
            empty: list[Entry[str]] = []
            self.assertIsNone(urgency.top(empty, "change-x"))
            self.assertEqual(urgency.pinned("other/queue"), "")

    def test_oldest_shows_the_oldest_and_pins_it(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"NEXT_STATE_DIR": folder}):
            entries = [Entry("blocks", "", Urgency(urgency.BLOCKS), since=at(3)), Entry("old", "", since=at(1))]
            self.assertEqual(getattr(urgency.top(entries, "q"), "key", None), "blocks")
            self.assertEqual(getattr(urgency.top(entries, "q", urgency.OLDEST), "key", None), "old")
            self.assertEqual(urgency.pinned("q"), "old")
            self.assertEqual(getattr(urgency.top(entries, "q", urgency.NEWEST), "key", None), "blocks")

    def test_move_to_end_unpins_the_item_and_forgets_it_once_it_leaves(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"NEXT_STATE_DIR": folder}):
            entries = [Entry("a", "", since=at(1)), Entry("b", "", since=at(2))]
            self.assertEqual(getattr(urgency.top(entries, "q"), "key", None), "a")
            urgency.move_to_end("q", "a", at(5))
            self.assertEqual((urgency.pinned("q"), urgency.ended("q")), ("", {"a": at(5)}))
            self.assertEqual(keys(urgency.queue_order(entries, "q")), ["b", "a"])
            self.assertEqual(getattr(urgency.top(entries, "q"), "key", None), "b")
            self.assertEqual(urgency.ended("q"), {"a": at(5)})
            self.assertEqual(getattr(urgency.top(entries[1:], "q"), "key", None), "b")
            self.assertEqual(urgency.ended("q"), {})

    def test_across_queues_level_then_age_and_the_shown_item_stays(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"NEXT_STATE_DIR": folder}):
            queues = {"change": [Entry("c1", "", since=at(1), value=(9.0,))],
                      "question": [Entry("q1", "", Urgency(urgency.SOON, due=at(9)), since=at(4)),
                                   Entry("q2", "", Urgency(urgency.SOON, due=at(8)), since=at(5))]}
            self.assertEqual(urgency.top_across(queues, "next-x"), ("question", queues["question"][1]))
            self.assertEqual((urgency.pinned("question"), urgency.pinned("next-x")), ("q2", "q2"))
            # A more urgent item arrives: the shown one stays.
            queues["change"].append(Entry("c2", "", Urgency(urgency.BLOCKS), since=at(6)))
            self.assertEqual(shown(urgency.top_across(queues, "next-x")), "q2")
            self.assertEqual(shown(urgency.top_across(queues, "next-x", urgency.OLDEST)), "c1")
            self.assertEqual(shown(urgency.top_across(queues, "next-x", urgency.NEWEST)), "c2")
            # Moved to the end, it leaves the front; the most urgent item takes its place.
            urgency.move_to_end("change", "c2", at(7))
            self.assertEqual(urgency.top_across(queues, "next-x"), ("question", queues["question"][1]))
            urgency.move_to_end("question", "q2", at(7))
            self.assertEqual(urgency.top_across(queues, "next-x"), ("question", queues["question"][0]))
            empty: dict[str, list[Entry[str]]] = {"change": []}
            self.assertIsNone(urgency.top_across(empty, "next-x"))


if __name__ == "__main__":
    _ = unittest.main()
