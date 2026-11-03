"""Build history must distinguish Cargo profiles, features and targets."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import final, override

from build_shape import build_shape, index_shapes


class BuildShapeTests(unittest.TestCase):
    def test_profiles_and_each_target_form_have_distinct_keys(self) -> None:
        cases: list[tuple[list[str], str, tuple[str, ...]]] = [
            ([], "dev", ("all",)),
            (["--release"], "release", ("all",)),
            (["--profile", "small"], "small", ("all",)),
            (["--profile=small"], "small", ("all",)),
            (["--bin", "hana"], "dev", ("bin:hana",)),
            (["--bin=hana"], "dev", ("bin:hana",)),
            (["--example", "scene"], "dev", ("example:scene",)),
            (["--example=scene"], "dev", ("example:scene",)),
            (["--lib"], "dev", ("lib",)),
        ]
        keys: dict[tuple[str, tuple[str, ...]], str] = {}
        for arguments, profile, targets in cases:
            with self.subTest(arguments=arguments):
                shape = build_shape(["cargo", "build", *arguments])
                self.assertEqual(shape.profile, profile)
                self.assertEqual(shape.targets, targets)
                keys[profile, targets] = shape.key
        self.assertEqual(len(set(keys.values())), len(keys))

    def test_feature_forms_sort_and_deduplicate_into_the_same_key(self) -> None:
        separated = build_shape(["cargo", "build", "--features", "z,a a", "--features", "b,z"])
        equals = build_shape(["cargo", "build", "--features=b,a,z"])
        self.assertEqual(separated.features, ("a", "b", "z"))
        self.assertEqual(separated, equals)
        self.assertEqual(separated.key, equals.key)

    def test_feature_modes_do_not_share_history_with_default_features(self) -> None:
        arguments = ["cargo", "build", "--features=a"]
        plain = build_shape(arguments)
        all_features = build_shape([*arguments, "--all-features"])
        no_default = build_shape([*arguments, "--no-default-features"])
        both = build_shape([*arguments, "--all-features", "--no-default-features"])
        self.assertIn("--all-features", all_features.features)
        self.assertIn("--no-default-features", no_default.features)
        self.assertEqual(len({shape.key for shape in (plain, all_features, no_default, both)}), 4)

    def test_target_order_is_stable_and_multiple_targets_keep_their_identity(self) -> None:
        first = build_shape(["cargo", "build", "--bin", "hana", "--example", "scene", "--lib"])
        second = build_shape(["cargo", "build", "--lib", "--example=scene", "--bin=hana"])
        self.assertEqual(first, second)
        self.assertNotEqual(first.key, build_shape(["cargo", "build", "--lib"]).key)

    def test_arguments_after_separator_are_not_cargo_shape_flags(self) -> None:
        shape = build_shape(["cargo", "build", "--", "--release", "--features", "wrong", "--lib"])
        self.assertEqual(shape, build_shape(["cargo", "build"]))

    def test_shape_is_frozen_and_key_is_unambiguous(self) -> None:
        shape = build_shape(["cargo", "build", "--profile", "dev", "--features", "a,b"])
        with self.assertRaises(FrozenInstanceError):
            setattr(shape, "profile", "release")
        self.assertNotEqual(shape.key, build_shape(["cargo", "build", "--profile", "dev:a,b"]).key)


@final
class IndexShapeTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.directory = tempfile.TemporaryDirectory()
        self.index = Path(self.directory.name) / "index.sqlite"
        self.before = datetime(2026, 10, 30, 9, tzinfo=UTC)
        self.cutoff = self.before - timedelta(days=14)

    @override
    def setUp(self) -> None:
        self.addCleanup(self.directory.cleanup)
        with closing(sqlite3.connect(self.index)) as connection, connection:
            _ = connection.execute("CREATE TABLE steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, "
                                   + "call_id TEXT, argv TEXT)")

    def row(self, call_id: str | None, argv: object, *, host: str = "natedev", repo: str = "hana",
            step: str = "build", at: datetime | None = None) -> None:
        with closing(sqlite3.connect(self.index)) as connection, connection:
            _ = connection.execute("INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)",
                                   (host, repo, step, (at or self.cutoff).isoformat(), call_id,
                                    json.dumps(argv) if argv is not None else None))

    def test_join_uses_call_and_step_and_filters_host_repository_and_window(self) -> None:
        arguments = ["cargo", "build", "--release", "--features=bevy/trace_chrome", "--bin", "hana"]
        self.row("release", arguments)
        self.row("wrong-host", arguments, host="mac")
        self.row("wrong-repo", arguments, repo="other")
        self.row("release", ["cargo", "check"], step="check")
        self.row("old", arguments, at=self.cutoff - timedelta(microseconds=1))
        self.row("future", arguments, at=self.before)
        self.row("later", arguments, at=self.before + timedelta(seconds=1))
        self.assertEqual(index_shapes(self.index, "natedev", "build", self.cutoff, self.before),
                         {("release", "build"): build_shape(arguments),
                          ("wrong-repo", "build"): build_shape(arguments)})

    def test_origin_and_checkout_names_find_the_same_call(self) -> None:
        arguments = ["cargo", "build", "--release", "--bin", "hana"]
        self.row("shared-call", arguments, repo="hana-copy")
        self.row("origin-call", arguments, repo="hana")
        self.assertEqual(index_shapes(self.index, "natedev", "build", self.cutoff, self.before),
                         {("shared-call", "build"): build_shape(arguments),
                          ("origin-call", "build"): build_shape(arguments)})

    def test_absent_or_invalid_arguments_and_call_ids_supply_no_shape(self) -> None:
        cases: list[tuple[str | None, object]] = [
            (None, ["cargo", "build"]), ("", ["cargo", "build"]),
            ("missing", None), ("empty", []), ("object", {}),
            ("mixed", ["cargo", 1]), ("broken-flag", ["cargo", "build", "--profile"]),
        ]
        for call_id, argv in cases:
            self.row(call_id, argv)
        with closing(sqlite3.connect(self.index)) as connection, connection:
            _ = connection.execute("INSERT INTO steps VALUES ('natedev', 'hana', 'build', ?, 'broken', 'not json')",
                                   (self.cutoff.isoformat(),))
        self.assertEqual(index_shapes(self.index, "natedev", "build", self.cutoff, self.before), {})

    def test_join_does_not_write_index_or_create_a_missing_index(self) -> None:
        self.row("one", ["cargo", "build", "--release"])
        before = self.index.read_bytes(), self.index.stat().st_mtime_ns
        for repetition in range(2):
            with self.subTest(repetition=repetition):
                self.assertEqual(len(index_shapes(self.index, "natedev", "build", self.cutoff, self.before)), 1)
                self.assertEqual((self.index.read_bytes(), self.index.stat().st_mtime_ns), before)
        missing = self.index.with_name("missing.sqlite")
        self.assertEqual(index_shapes(missing, "natedev", "build", self.cutoff, self.before), {})
        self.assertFalse(missing.exists())

    def test_index_path_uri_characters_are_literal(self) -> None:
        special = self.index.with_name("index?#.sqlite")
        _ = self.index.rename(special)
        self.index = special
        self.row("literal", ["cargo", "build", "--lib"])
        self.assertEqual(index_shapes(special, "natedev", "build", self.cutoff, self.before),
                         {("literal", "build"): build_shape(["cargo", "build", "--lib"])})


if __name__ == "__main__":
    _ = unittest.main()
