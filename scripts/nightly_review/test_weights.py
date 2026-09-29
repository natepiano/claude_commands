"""Allocation of the nightly review's rust target weights."""

from __future__ import annotations

import unittest

from weights import allocate


class AllocateTests(unittest.TestCase):
    def test_square_root_shares_with_a_floor_of_one(self) -> None:
        activity = {"cargo-liner": 713, "bevy_brp": 124, "showrunner": 114, "obsidian_knife": 13, "nateroids": 29}
        self.assertEqual(allocate(activity, 8),
                         {"cargo-liner": 3, "bevy_brp": 2, "showrunner": 1, "obsidian_knife": 1, "nateroids": 1})

    def test_idle_targets_still_get_one(self) -> None:
        self.assertEqual(allocate({"a": 0, "b": 0, "c": 50}, 5), {"a": 1, "b": 1, "c": 3})
        self.assertEqual(allocate({"a": 0, "b": 0}, 2), {"a": 1, "b": 1})

    def test_too_few_nights_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            _ = allocate({"a": 1, "b": 1, "c": 1}, 2)


if __name__ == "__main__":
    _ = unittest.main()
