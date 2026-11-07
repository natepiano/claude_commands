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
from collections.abc import Sequence
from typing import final, override

from scripts.lint.build_shape import (AbsoluteRecordedRoot, CompileCountUnknown, CompiledCrates, NoBuildShape,
                         NoCratesCompiled, RecordedOutputPair, RecordedOutputRoots, RecordedRootsUnavailable,
                         RelativeRecordedRoot, StepEvidence, UnknownRecordedRoot, build_shape,
                         index_step_evidence, recorded_output_roots)


def create_step_index(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        _ = connection.execute("CREATE TABLE IF NOT EXISTS steps(host TEXT, repo TEXT, step TEXT, started_at TEXT, "
                               + "call_id TEXT, argv TEXT, crates_compiled INTEGER, worktree TEXT, cwd TEXT, "
                               + "target_directory TEXT, build_directory TEXT, target_dir TEXT, target_folder TEXT)")


def record_root_attempt(path: Path, workspace: str, target: str, build: str, *, cwd: str = "",
                        host: str = "natedev", step: str = "build", call_id: str = "attempt",
                        argv: Sequence[str] = (), target_folder: str = "") -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        _ = connection.execute("INSERT INTO steps(host, step, call_id, worktree, cwd, "
                               + "target_directory, build_directory, argv, target_folder) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (host, step, call_id, workspace, cwd, target, build, json.dumps(list(argv)), target_folder))


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
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.directory = tempfile.TemporaryDirectory()
        self.index = Path(self.directory.name) / "steps.sqlite"
        self.before = datetime(2026, 10, 30, 9, tzinfo=UTC)
        self.cutoff = self.before - timedelta(days=14)

    @override
    def setUp(self) -> None:
        self.addCleanup(self.directory.cleanup)
        create_step_index(self.index)

    def row(self, call_id: str | None, argv: object, *, host: str = "natedev", repo: str = "hana",
            step: str = "build", at: datetime | None = None, crates: int | None = 3) -> None:
        with closing(sqlite3.connect(self.index)) as connection, connection:
            _ = connection.execute("INSERT INTO steps(host, repo, step, started_at, call_id, argv, crates_compiled) "
                                   + "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                   (host, repo, step, (at or self.cutoff).isoformat(), call_id,
                                    json.dumps(argv) if argv is not None else None, crates))

    def test_join_uses_call_and_step_and_filters_host_and_window(self) -> None:
        arguments = ["cargo", "build", "--release", "--features=bevy/trace_chrome", "--bin", "hana"]
        self.row("release", arguments)
        self.row("wrong-host", arguments, host="mac")
        self.row("wrong-repo", arguments, repo="other")
        self.row("release", ["cargo", "check"], step="check")
        self.row("old", arguments, at=self.cutoff - timedelta(microseconds=1))
        self.row("future", arguments, at=self.before)
        self.row("later", arguments, at=self.before + timedelta(seconds=1))
        self.assertEqual(index_step_evidence(self.index, "natedev", "build", self.cutoff, self.before),
                         {("release", "build"): StepEvidence(build_shape(arguments), CompiledCrates(3)),
                          ("wrong-repo", "build"): StepEvidence(build_shape(arguments), CompiledCrates(3))})

    def test_origin_and_checkout_names_find_the_same_call(self) -> None:
        arguments = ["cargo", "build", "--release", "--bin", "hana"]
        self.row("shared-call", arguments, repo="hana-copy")
        self.row("origin-call", arguments, repo="hana")
        self.assertEqual(index_step_evidence(self.index, "natedev", "build", self.cutoff, self.before),
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
        evidence = index_step_evidence(self.index, "natedev", "build", self.cutoff, self.before)
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
        evidence = index_step_evidence(self.index, "natedev", "build", self.cutoff, self.before)
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
        self.assertEqual(index_step_evidence(self.index, "natedev", "nextest", self.cutoff, self.before), {
            ("filtered", "nextest"): StepEvidence(build_shape(filtered), CompiledCrates(3), tuple(filtered)),
            ("full", "nextest"): StepEvidence(build_shape(full), CompiledCrates(3), tuple(full)),
        })

    def test_missing_test_arguments_keep_the_compile_count(self) -> None:
        self.row("count-only", None, step="nextest", crates=7)
        self.assertEqual(index_step_evidence(self.index, "natedev", "nextest", self.cutoff, self.before), {
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
                evidence = index_step_evidence(self.index, "natedev", "build", self.cutoff, self.before)
                self.assertEqual(evidence[call_id, "build"].compilation, expected)

    def test_no_matching_attempt_has_no_evidence_key(self) -> None:
        self.assertEqual(index_step_evidence(self.index, "natedev", "build", self.cutoff, self.before), {})

    def test_single_zero_attempt_proves_no_crates_compiled(self) -> None:
        self.row("zero", ["cargo", "build"], crates=0)
        evidence = index_step_evidence(self.index, "natedev", "build", self.cutoff, self.before)
        self.assertEqual(evidence["zero", "build"].compilation, NoCratesCompiled())

    def test_unknown_attempt_between_zero_attempts_keeps_the_count_unknown(self) -> None:
        for count in (0, None, 0):
            self.row("retried", ["cargo", "build"], crates=count)
        evidence = index_step_evidence(self.index, "natedev", "build", self.cutoff, self.before)
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
                evidence = index_step_evidence(self.index, "natedev", "build", self.cutoff, self.before)
                self.assertEqual(evidence[call_id, "build"].compilation, CompileCountUnknown())

    def test_join_does_not_write_index_or_create_a_missing_index(self) -> None:
        self.row("one", ["cargo", "build", "--release"])
        before = self.index.read_bytes(), self.index.stat().st_mtime_ns
        for repetition in range(2):
            with self.subTest(repetition=repetition):
                self.assertEqual(len(index_step_evidence(self.index, "natedev", "build", self.cutoff, self.before)), 1)
                self.assertEqual((self.index.read_bytes(), self.index.stat().st_mtime_ns), before)
        missing = self.index.with_name("missing.sqlite")
        self.assertEqual(index_step_evidence(missing, "natedev", "build", self.cutoff, self.before), {})
        self.assertFalse(missing.exists())

    def test_index_path_uri_characters_are_literal(self) -> None:
        special = self.index.with_name("index?#.sqlite")
        _ = self.index.rename(special)
        self.index = special
        self.row("literal", ["cargo", "build", "--lib"])
        self.assertEqual(index_step_evidence(special, "natedev", "build", self.cutoff, self.before),
                         {("literal", "build"): StepEvidence(build_shape(["cargo", "build", "--lib"]), CompiledCrates(3))})


@final
class RecordedOutputRootsTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.index = self.root / "steps?#.sqlite"

    @override
    def setUp(self) -> None:
        self.addCleanup(self.directory.cleanup)
        create_step_index(self.index)

    def test_every_step_kind_and_attempt_retains_its_workspace_and_root_pair(self) -> None:
        workspace = str(self.root / "checkout")
        expected: list[RecordedOutputPair] = []
        for number, step in enumerate(("build", "check", "clippy", "mend", "doc", "nextest", "test", "t")):
            target, build = self.root / f"target-{number}", self.root / f"build-{number}"
            record_root_attempt(self.index, workspace, str(target), str(build), step=step, call_id="same-call")
            expected.append(RecordedOutputPair(AbsoluteRecordedRoot(target), AbsoluteRecordedRoot(build)))
        record_root_attempt(self.index, workspace, "/wrong-host/target", "/wrong-host/build", host="other")
        fallback = self.root / "cwd"
        record_root_attempt(self.index, "unknown", str(fallback / "target"), str(fallback / "target"),
                            cwd=str(fallback), call_id="")
        before = self.index.read_bytes(), self.index.stat().st_mtime_ns
        roots = recorded_output_roots(self.index, "natedev")
        self.assertEqual(roots, RecordedOutputRoots({
            AbsoluteRecordedRoot(Path(workspace)): tuple(expected),
            AbsoluteRecordedRoot(fallback): (RecordedOutputPair(AbsoluteRecordedRoot(fallback / "target"),
                                               AbsoluteRecordedRoot(fallback / "target")),),
        }))
        self.assertEqual((self.index.read_bytes(), self.index.stat().st_mtime_ns), before)

    def test_target_overrides_do_not_authorize_recorded_output_pairs(self) -> None:
        workspace, target = self.root / "workspace", self.root / "default-target"
        valid = self.root / "valid"
        record_root_attempt(self.index, str(valid), str(valid / "target"), str(valid / "target"))
        arguments = (["cargo", "build", "--target-dir", str(target)],
                     ["cargo", "build", f"--target-dir={target}"],
                     ["cargo", "build", "--config", f'build.target-dir="{target}"'],
                     ["cargo", "build", f'--config=build.target-dir="{target}"'])
        for argv in arguments:
            record_root_attempt(self.index, str(workspace), str(target), str(target), argv=argv)
        record_root_attempt(self.index, str(workspace), str(target), str(target),
                            target_folder=str(self.root / "different-target"))
        self.assertEqual(recorded_output_roots(self.index, "natedev"), RecordedOutputRoots({
            AbsoluteRecordedRoot(valid): (RecordedOutputPair(AbsoluteRecordedRoot(valid / "target"),
                                                             AbsoluteRecordedRoot(valid / "target")),),
        }))

    def test_unknown_and_relative_roots_have_distinct_states(self) -> None:
        workspace = str(self.root / "workspace")
        record_root_attempt(self.index, workspace, "unknown", "relative/build")
        record_root_attempt(self.index, "", "target", "unknown", cwd=str(self.root / "fallback"))
        self.assertEqual(recorded_output_roots(self.index, "natedev"), RecordedOutputRoots({
            AbsoluteRecordedRoot(Path(workspace)): (RecordedOutputPair(UnknownRecordedRoot(), RelativeRecordedRoot()),),
            AbsoluteRecordedRoot(self.root / "fallback"): (RecordedOutputPair(RelativeRecordedRoot(), UnknownRecordedRoot()),),
        }))

    def test_unavailable_index_has_a_named_state_and_creates_no_index(self) -> None:
        missing = self.root / "missing.sqlite"
        self.assertIsInstance(recorded_output_roots(missing, "natedev"), RecordedRootsUnavailable)
        self.assertFalse(missing.exists())
        _ = self.index.write_text("broken index")
        self.assertIsInstance(recorded_output_roots(self.index, "natedev"), RecordedRootsUnavailable)

    def test_legacy_index_without_build_root_uses_the_target_root(self) -> None:
        workspace, target = str(self.root / "workspace"), self.root / "target"
        record_root_attempt(self.index, workspace, str(target), "unknown")
        with closing(sqlite3.connect(self.index)) as connection, connection:
            _ = connection.execute("ALTER TABLE steps DROP COLUMN build_directory")
        self.assertEqual(recorded_output_roots(self.index, "natedev"), RecordedOutputRoots({
            AbsoluteRecordedRoot(Path(workspace)): (RecordedOutputPair(AbsoluteRecordedRoot(target), AbsoluteRecordedRoot(target)),),
        }))

    def test_root_reader_keeps_the_existing_step_evidence_result(self) -> None:
        argv = ["cargo", "nextest", "run", "--release", "-E", "package(app)"]
        at = datetime(2026, 10, 30, 9, tzinfo=UTC)
        with closing(sqlite3.connect(self.index)) as connection, connection:
            for target, count in (("old-target", 2), ("new-target", 3)):
                _ = connection.execute("INSERT INTO steps(host, step, call_id, started_at, argv, crates_compiled, "
                                       + "worktree, target_directory, build_directory) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                       ("natedev", "nextest", "retry", at.isoformat(), json.dumps(argv), count,
                                        str(self.root), str(self.root / target), str(self.root / (target + "-build"))))
        before = at - timedelta(days=1)
        after = at + timedelta(days=1)
        expected = {("retry", "nextest"): StepEvidence(build_shape(argv), CompiledCrates(5), tuple(argv))}
        self.assertEqual(index_step_evidence(self.index, "natedev", "nextest", before, after), expected)
        roots = recorded_output_roots(self.index, "natedev")
        self.assertIsInstance(roots, RecordedOutputRoots)
        assert isinstance(roots, RecordedOutputRoots)
        self.assertEqual(len(roots.workspaces[AbsoluteRecordedRoot(self.root)]), 2)


if __name__ == "__main__":
    _ = unittest.main()
