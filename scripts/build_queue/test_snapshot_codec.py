"""Validate the tagged decoder with both Python union forms."""
from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict
import unittest

from scripts.build_queue import snapshot_codec as codec


@dataclass(frozen=True)
class Ready:
    count: int


@dataclass(frozen=True)
class Unavailable:
    reason: str


class Reading(TypedDict):
    state: Literal["ready"] | Literal["waiting"]
    value: Ready | Unavailable
    detail: NotRequired[str]


class SnapshotCodecTests(unittest.TestCase):
    def test_literal_unions_accept_each_member_and_refuse_unknown_values(self) -> None:
        schema = Literal["ready"] | Literal["waiting"]
        for value in ("ready", "waiting"):
            self.assertEqual(codec.parse_snapshot_value(value, schema), value)
        for value in ("unknown", 0, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _ = codec.parse_snapshot_value(value, schema)

    def test_nested_union_records_keep_tags_and_optional_fields(self) -> None:
        for record in (Ready(3), Unavailable("not read")):
            raw = {"state": "waiting", "value": codec.snapshot_value(record)}
            self.assertEqual(codec.parse_snapshot_value(raw, Reading),
                             {"state": "waiting", "value": record})
            raw["detail"] = "fixture"
            self.assertEqual(codec.parse_snapshot_value(raw, Reading),
                             {"state": "waiting", "value": record, "detail": "fixture"})
        with self.assertRaises(ValueError):
            _ = codec.parse_snapshot_value({"state": "ready", "value": {"kind": "Other"}}, Reading)

    def test_scalar_unions_keep_strict_types_and_finite_numbers(self) -> None:
        schema = list[int | str | float]
        self.assertEqual(codec.parse_snapshot_value([1, "sample", 2.5], schema), [1, "sample", 2.5])
        for value in (True, float("nan"), float("inf"), None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _ = codec.parse_snapshot_value([value], schema)


if __name__ == "__main__":
    _ = unittest.main()
