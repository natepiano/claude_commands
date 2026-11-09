"""Build history must distinguish Cargo profiles, features and targets."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from collections.abc import Generator
from contextlib import closing, contextmanager
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import final, override
from unittest.mock import patch

from scripts.lint.build_shape import (CompileCountUnknown, CompiledCrates, NoBuildShape, NoCratesCompiled,
                         StepEvidence, build_shape, index_step_evidence, step_index)

from scripts.buildlog.step_outputs_test_support import create_step_index


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
class IndexStepEvidenceTests(unittest.TestCase):
    index: Path = Path()
    before: datetime = datetime(2026, 10, 30, 9, tzinfo=UTC)
    cutoff: datetime = before - timedelta(days=14)

    @override
    def setUp(self) -> None:
        self.index = Path(self.enterContext(tempfile.TemporaryDirectory())) / "steps.sqlite"
        create_step_index(self.index)

    def evidence(self, *call_ids: str, step: str = "build") -> dict[tuple[str, str], StepEvidence]:
        return index_step_evidence(self.index, "natedev", (step,), call_ids, self.before)

    def row(self, call_id: str | None, argv: object, *, host: str = "natedev", repo: str = "hana",
            step: str = "build", at: datetime | None = None, crates: int | None = 3) -> None:
        with closing(sqlite3.connect(self.index)) as connection, connection:
            _ = connection.execute("INSERT INTO steps(host, repo, step, started_at, call_id, argv, crates_compiled) "
                                   + "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                   (host, repo, step, (at or self.cutoff).isoformat(), call_id,
                                    json.dumps(argv) if argv is not None else None, crates))

    def test_join_filters_host_and_excludes_later_attempts(self) -> None:
        arguments = ["cargo", "build", "--release", "--features=bevy/trace_chrome", "--bin", "hana"]
        self.row("release", arguments)
        self.row("wrong-host", arguments, host="mac")
        self.row("wrong-repo", arguments, repo="other")
        self.row("release", ["cargo", "check"], step="check")
        self.row("future", arguments, at=self.before)
        self.row("later", arguments, at=self.before + timedelta(seconds=1))
        self.assertEqual(self.evidence("release", "wrong-host", "wrong-repo", "future", "later"),
                         {("release", "build"): StepEvidence(build_shape(arguments), CompiledCrates(3)),
                          ("wrong-repo", "build"): StepEvidence(build_shape(arguments), CompiledCrates(3))})

    def test_only_window_call_ids_join_even_when_other_attempts_are_recent(self) -> None:
        arguments = ["cargo", "build", "--release"]
        self.row("window", arguments, at=self.cutoff - timedelta(days=1), crates=4)
        self.row("outside", arguments, at=self.before - timedelta(seconds=1), crates=9)
        self.assertEqual(
            self.evidence("window"),
            {("window", "build"): StepEvidence(build_shape(arguments), CompiledCrates(4))})

    def test_evidence_query_uses_the_call_index_with_a_host_index_present(self) -> None:
        self.row("selected", ["cargo", "build"])
        with closing(sqlite3.connect(self.index)) as connection, connection:
            _ = connection.execute("CREATE INDEX steps_call ON steps(call_id)")
            _ = connection.execute("CREATE INDEX steps_host_started ON steps(host, started_at)")
        queries: list[str] = []

        @contextmanager
        def traced_index(path: Path) -> Generator[tuple[sqlite3.Connection, set[str]]]:
            with step_index(path) as (connection, columns):
                connection.set_trace_callback(queries.append)
                yield connection, columns

        with patch("scripts.lint.build_shape.step_index", side_effect=traced_index):
            self.assertEqual(self.evidence("selected"), {
                ("selected", "build"): StepEvidence(build_shape(["cargo", "build"]), CompiledCrates(3)),
            })
        query, = (query for query in queries if query.startswith("SELECT call_id,"))
        with closing(sqlite3.connect(self.index)) as connection:
            plan = connection.execute("EXPLAIN QUERY PLAN " + query).fetchall()
        self.assertIn("steps_call", str(plan))

    def test_later_attempts_cannot_change_evidence_before_admission(self) -> None:
        arguments = ["cargo", "build"]
        self.row("retried", arguments, crates=2)
        self.row("retried", arguments, at=self.before, crates=5)
        self.row("retried", arguments, at=self.before + timedelta(seconds=1), crates=7)
        self.assertEqual(
            self.evidence("retried"),
            {("retried", "build"): StepEvidence(build_shape(arguments), CompiledCrates(2))})

    def test_origin_and_checkout_names_find_the_same_call(self) -> None:
        arguments = ["cargo", "build", "--release", "--bin", "hana"]
        self.row("shared-call", arguments, repo="hana-copy")
        self.row("origin-call", arguments, repo="hana")
        self.assertEqual(self.evidence("shared-call", "origin-call"),
                         {("shared-call", "build"): StepEvidence(build_shape(arguments), CompiledCrates(3)),
                          ("origin-call", "build"): StepEvidence(build_shape(arguments), CompiledCrates(3))})

    def test_absent_or_invalid_arguments_and_call_ids_supply_no_shape(self) -> None:
        cases: list[tuple[str | None, object]] = [
            (None, ["cargo", "build"]), ("", ["cargo", "build"]),
            ("missing", None), ("empty", []), ("object", {}),
            ("mixed", ["cargo", 1]), ("broken-flag", ["cargo", "build", "--profile"]),
        ]
        for call_id, argv in cases:
            self.row(call_id, argv)
        with closing(sqlite3.connect(self.index)) as connection, connection:
            _ = connection.execute("INSERT INTO steps(host, repo, step, started_at, call_id, argv, crates_compiled) "
                                   + "VALUES ('natedev', 'hana', 'build', ?, 'broken', 'not json', 3)",
                                   (self.cutoff.isoformat(),))
        evidence = self.evidence("", "missing", "empty", "object", "mixed", "broken-flag", "broken")
        self.assertEqual(evidence, {
            (name, "build"): StepEvidence(NoBuildShape(), CompiledCrates(3))
            for name in ("missing", "empty", "object", "mixed", "broken-flag", "broken")
        })

    def test_shape_and_compile_count_have_independent_evidence(self) -> None:
        arguments = ["cargo", "build", "--release"]
        self.row("both", arguments, crates=8)
        self.row("count-only", None, crates=0)
        self.row("shape-only", arguments, crates=None)
        self.row("unmatched", arguments, host="other-host")
        evidence = self.evidence("both", "count-only", "shape-only", "unmatched")
        self.assertEqual(evidence, {
            ("both", "build"): StepEvidence(build_shape(arguments), CompiledCrates(8)),
            ("count-only", "build"): StepEvidence(NoBuildShape(), NoCratesCompiled()),
            ("shape-only", "build"): StepEvidence(build_shape(arguments), CompileCountUnknown()),
        })

    def test_indexed_test_arguments_remain_available_for_selection(self) -> None:
        filtered = ["cargo", "nextest", "run", "-E",
                    "package(cargo-handler) & (test(zeta) | test(alpha) | test(zeta))"]
        full = ["cargo", "nextest", "run", "-E", "package(cargo-handler)"]
        self.row("filtered", filtered, step="nextest")
        self.row("full", full, step="nextest")
        self.assertEqual(self.evidence("filtered", "full", step="nextest"), {
            ("filtered", "nextest"): StepEvidence(NoBuildShape(), CompiledCrates(3), tuple(filtered)),
            ("full", "nextest"): StepEvidence(NoBuildShape(), CompiledCrates(3), tuple(full)),
        })

    def test_missing_test_arguments_keep_the_compile_count(self) -> None:
        self.row("count-only", None, step="nextest", crates=7)
        self.assertEqual(self.evidence("count-only", step="nextest"), {
            ("count-only", "nextest"): StepEvidence(NoBuildShape(), CompiledCrates(7), ()),
        })

    def test_repeated_attempts_combine_compile_states_in_either_order(self) -> None:
        cases: tuple[tuple[int | None, int | None,
                           CompiledCrates | NoCratesCompiled | CompileCountUnknown], ...] = (
            (3, 7, CompiledCrates(10)),
            (3, 0, CompiledCrates(3)),
            (3, None, CompiledCrates(3)),
            (0, 7, CompiledCrates(7)),
            (0, 0, NoCratesCompiled()),
            (0, None, CompileCountUnknown()),
            (None, 7, CompiledCrates(7)),
            (None, 0, CompileCountUnknown()),
            (None, None, CompileCountUnknown()),
        )
        for index, (first, second, expected) in enumerate(cases):
            call_id = f"attempts-{index}"
            self.row(call_id, ["cargo", "build"], crates=first)
            self.row(call_id, ["cargo", "build"], crates=second)
            with self.subTest(first=first, second=second):
                evidence = self.evidence(call_id)
                self.assertEqual(evidence[call_id, "build"].compilation, expected)

    def test_no_matching_attempt_has_no_evidence_key(self) -> None:
        self.assertEqual(self.evidence("unmatched"), {})

    def test_single_zero_attempt_proves_no_crates_compiled(self) -> None:
        self.row("zero", ["cargo", "build"], crates=0)
        evidence = self.evidence("zero")
        self.assertEqual(evidence["zero", "build"].compilation, NoCratesCompiled())

    def test_unknown_attempt_between_zero_attempts_keeps_the_count_unknown(self) -> None:
        for count in (0, None, 0):
            self.row("retried", ["cargo", "build"], crates=count)
        evidence = self.evidence("retried")
        self.assertEqual(evidence["retried", "build"].compilation, CompileCountUnknown())

    def test_undated_attempt_prevents_a_zero_compile_result(self) -> None:
        for index, stamp in enumerate((None, "invalid")):
            call_id = f"undated-{index}"
            self.row(call_id, ["cargo", "build"], crates=0)
            self.row(call_id, ["cargo", "build"], crates=0)
            with closing(sqlite3.connect(self.index)) as connection, connection:
                _ = connection.execute("UPDATE steps SET started_at=? WHERE rowid="
                                       + "(SELECT MAX(rowid) FROM steps WHERE call_id=?)", (stamp, call_id))
            with self.subTest(started_at=stamp):
                evidence = self.evidence(call_id)
                self.assertEqual(evidence[call_id, "build"].compilation, CompileCountUnknown())

    def test_join_does_not_write_index_or_create_a_missing_index(self) -> None:
        self.row("one", ["cargo", "build", "--release"])
        before = self.index.read_bytes(), self.index.stat().st_mtime_ns
        for repetition in range(2):
            with self.subTest(repetition=repetition):
                self.assertEqual(len(self.evidence("one")), 1)
                self.assertEqual((self.index.read_bytes(), self.index.stat().st_mtime_ns), before)
        missing = self.index.with_name("missing.sqlite")
        self.index = missing
        self.assertEqual(self.evidence("one"), {})
        self.assertFalse(missing.exists())

    def test_index_path_uri_characters_are_literal(self) -> None:
        special = self.index.with_name("index?#.sqlite")
        _ = self.index.rename(special)
        self.index = special
        self.row("literal", ["cargo", "build", "--lib"])
        self.assertEqual(self.evidence("literal"),
                         {("literal", "build"): StepEvidence(build_shape(["cargo", "build", "--lib"]), CompiledCrates(3))})


if __name__ == "__main__":
    _ = unittest.main()
