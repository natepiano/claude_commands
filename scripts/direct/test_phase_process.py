"""Fixture tests for phase process costs and document history."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import patch

from scripts.direct import phase_process as process, plan_history
from scripts.usage import collect


DIRECTOR = process.DirectorSession("claude", "director")
WINDOW = process.Window(0, 200)


def event(kind: str, at: float, *, run: str = "run", phase: str = "phase", instance: str = "",
          step: str = "", status: str = "completed", director: process.DirectorSession = DIRECTOR,
          plan: str = "docs/plans/example.md", directory: str = "/fixture") -> process.Event:
    return process.Event(run, phase, "1", kind, at, instance, step, status, director, directory, plan, "main")


def phase_events() -> tuple[process.Event, ...]:
    return (event("run_started", 1), event("phase_started", 10),
            event("pass_started", 20, instance="impl", step="implementation"),
            event("pass_finished", 60, instance="impl", step="implementation"),
            event("phase_finished", 100), event("phase_started", 120, phase="next"))


def repair_report_events() -> tuple[process.Event, ...]:
    events: list[process.Event] = [event("run_started", 1)]
    for index, implementation in enumerate((60, 60, 60, 20, 10)):
        start = 10 + index * 100
        phase = str(index)
        events.extend((event("phase_started", start, phase=phase),
                       event("pass_started", start + 10, phase=phase, instance="impl", step="implementation"),
                       event("pass_finished", start + 10 + implementation, phase=phase, instance="impl", step="implementation"),
                       event("phase_finished", start + 90, phase=phase)))
    for phase, start, end in (("3", 340, 380), ("4", 430, 450), ("4", 460, 500)):
        events.extend((event("pass_started", start, phase=phase, instance=str(start), step="fix"),
                       event("pass_finished", end, phase=phase, instance=str(start), step="fix")))
    return tuple(events)


def parsed_phase_records(records: list[dict[str, object]]) -> tuple[process.Event, ...]:
    events: list[process.Event] = []
    for record in records:
        raw: dict[str, object] = {"run_id": "run", "phase_id": "1", "phase_instance_id": "phase", **record}
        parsed = process.parse_event(raw)
        if isinstance(parsed, process.NotRecorded):
            raise AssertionError(parsed.reason)
        events.append(parsed)
    return tuple(events)


def commit_fixture_documents(root: Path, documents: dict[str, str], message: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").is_dir():
        _ = subprocess.run(["git", "init", "-qb", "main", str(root)], check=True, capture_output=True)
    for name, text in documents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text)
    _ = subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    _ = subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.test", "-c", "commit.gpgsign=false",
                        "commit", "-qm", message], check=True, capture_output=True)


OLD_HASH = hashlib.sha256(b"old contract\n").hexdigest()
NEW_HASH = hashlib.sha256(b"short route\n").hexdigest()
ROUTE = process.RouteHashes(frozenset((OLD_HASH,)), frozenset((NEW_HASH,)))


def contract_event(kind: str, at: float, *, phase: str = "phase", run: str = "run",
                   direct_hash: str = OLD_HASH, director: process.DirectorSession = DIRECTOR) -> process.Event:
    parsed = process.parse_event({
        "run_id": run, "phase_instance_id": phase, "phase_id": phase,
        "event_type": kind, "timestamp_epoch": at, "status": "completed",
        "main_agent": {"family": director.family, "session_id": director.session},
        "worktree": run, "branch": run, "working_dir": f"/fixture/{run}",
        "contract_revision": {"main_head": {"state": "Recorded", "commit": "fixture-head"},
                              "direct_md": {"state": "Recorded", "sha256": direct_hash},
                              "files": {"docs/direct/phase_checks.md": {"state": "Recorded", "sha256": OLD_HASH}}},
    })
    if isinstance(parsed, process.NotRecorded):
        raise AssertionError(parsed)
    return parsed


def trial_events(direct_hash: str = OLD_HASH) -> tuple[process.Event, ...]:
    return (contract_event("run_started", 1), contract_event("phase_started", 10, direct_hash=direct_hash),
            contract_event("phase_finished", 100, direct_hash=direct_hash),
            contract_event("phase_started", 120, phase="next", direct_hash=direct_hash))


def transcript_record(kind: str, at: float, **fields: object) -> dict[str, object]:
    return {"type": kind, "timestamp": datetime.fromtimestamp(at, UTC).isoformat(), **fields}


def request_record(at: float, key: str, raw_input: int, blocks: list[dict[str, object]]) -> dict[str, object]:
    return transcript_record("assistant", at, requestId=key,
                             message={"id": key, "content": blocks,
                                      "usage": {"input_tokens": raw_input, "cache_read_input_tokens": 20,
                                                "cache_creation_input_tokens": 30, "output_tokens": 1}})


class TrialProcessTests(unittest.TestCase):
    def test_start_and_finish_hashes_select_the_named_route_side(self) -> None:
        for direct_hash, expected in ((OLD_HASH, process.BeforeChange), (NEW_HASH, process.AfterChange)):
            with self.subTest(side=expected.__name__):
                events = trial_events(direct_hash)
                trial = process.select_trial_phases(events, WINDOW, ROUTE)[0]
                self.assertIsInstance(trial.exposure, expected)
                self.assertEqual(trial.interval, process.PhaseWithGap(120))

    def test_absent_changed_missing_invalid_and_unclassified_exposure_stay_unknown(self) -> None:
        events = trial_events()
        start, finish = events[1:3]
        missing = replace(start, record={**start.record, "contract_revision": {"direct_md": {"state": "Missing", "reason": "file gone"}}})
        invalid = contract_event("phase_started", 10, direct_hash="bad")
        cases = (
            (replace(start, record={}), finish, ROUTE, "phase_started: direct.md not recorded"),
            (start, replace(finish, record={}), ROUTE, "phase_finished: direct.md not recorded"),
            (missing, finish, ROUTE, "Missing: file gone"),
            (invalid, finish, ROUTE, "invalid direct.md sha256"),
            (start, contract_event("phase_finished", 100, direct_hash=NEW_HASH), ROUTE, "hash changed"),
            (start, finish, process.RouteHashes(frozenset(), frozenset()), "neither side"),
            (start, finish, process.RouteHashes(frozenset((OLD_HASH,)), frozenset((OLD_HASH,))), "both sides"),
            (start, finish, process.NotRecorded("route", "revision absent"), "revision absent"),
        )
        for first, last, route, reason in cases:
            with self.subTest(reason=reason):
                exposure = process.trial_exposure(first, last, route)
                self.assertIsInstance(exposure, process.ExposureNotRecorded)
                self.assertIn(reason, cast(process.ExposureNotRecorded, exposure).reason)

    def test_route_revision_uses_git_bytes_and_does_not_label_dirty_bytes(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp/claude") as folder:
            root = Path(folder)
            direct = "commands/unit/direct.md"
            commit_fixture_documents(root, {direct: "old contract\n"}, "Old route")
            commit_fixture_documents(root, {direct: "short route\n"}, "Install route")
            revision = process.git_output(root, ["rev-parse", "HEAD"]).strip()
            _ = (root / direct).write_text("dirty route\n")
            route = process.read_route_hashes(root, revision)
            self.assertEqual(route, ROUTE)
            self.assertEqual((root / direct).read_text(), "dirty route\n")
            dirty_hash = hashlib.sha256((root / direct).read_bytes()).hexdigest()
            events = trial_events(dirty_hash)
            self.assertIsInstance(process.trial_exposure(events[1], events[2], route), process.ExposureNotRecorded)
            self.assertIsInstance(process.read_route_hashes(root, "no-such-revision"), process.NotRecorded)

    def test_before_anchor_classifies_recorded_hashes_without_a_route_commit(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp/claude") as folder:
            root = Path(folder)
            direct = "commands/unit/direct.md"
            commit_fixture_documents(root, {direct: "earlier contract\n"}, "Earlier before")
            commit_fixture_documents(root, {direct: "old contract\n"}, "Before anchor")
            anchor = process.git_output(root, ["rev-parse", "HEAD"]).strip()
            route = process.read_exposure_hashes(root, "", anchor)
            earlier_hash = hashlib.sha256(b"earlier contract\n").hexdigest()
            for direct_hash in (OLD_HASH, earlier_hash):
                with self.subTest(direct_hash=direct_hash):
                    trial = process.select_trial_phases(trial_events(direct_hash), WINDOW, route)[0]
                    self.assertEqual(trial.exposure, process.BeforeChange(direct_hash))
                    self.assertEqual(trial.interval, process.PhaseWithGap(120))

    def test_before_anchor_does_not_include_contract_changes_after_it(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp/claude") as folder:
            root = Path(folder)
            direct = "commands/unit/direct.md"
            commit_fixture_documents(root, {direct: "old contract\n"}, "Before anchor")
            anchor = process.git_output(root, ["rev-parse", "HEAD"]).strip()
            commit_fixture_documents(root, {direct: "short route\n"}, "Later contract")
            route = process.read_exposure_hashes(root, "", anchor)
            trial = process.select_trial_phases(trial_events(NEW_HASH), WINDOW, route)[0]
            self.assertIsInstance(trial.exposure, process.ExposureNotRecorded)
            self.assertIn("neither side", cast(process.ExposureNotRecorded, trial.exposure).reason)
            self.assertIsInstance(process.select_trial_phases(trial_events(), WINDOW, route)[0].exposure,
                                  process.BeforeChange)

    def test_explicit_before_anchor_can_make_route_side_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp/claude") as folder:
            root = Path(folder)
            direct = "commands/unit/direct.md"
            commit_fixture_documents(root, {direct: "old contract\n"}, "Before")
            commit_fixture_documents(root, {direct: "short route\n"}, "Route")
            revision = process.git_output(root, ["rev-parse", "HEAD"]).strip()
            route = process.read_exposure_hashes(root, revision, revision)
            trial = process.select_trial_phases(trial_events(NEW_HASH), WINDOW, route)[0]
            self.assertIsInstance(trial.exposure, process.ExposureNotRecorded)
            self.assertIn("both sides", cast(process.ExposureNotRecorded, trial.exposure).reason)
            route_only = process.read_exposure_hashes(root, revision, "")
            self.assertIsInstance(process.select_trial_phases(trial_events(NEW_HASH), WINDOW, route_only)[0].exposure,
                                  process.AfterChange)

    def test_absent_options_and_invalid_before_anchor_keep_exposure_unknown(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp/claude") as folder:
            root = Path(folder)
            commit_fixture_documents(root, {"commands/unit/direct.md": "old contract\n"}, "Before")
            blob = process.git_output(root, ["rev-parse", "HEAD:commands/unit/direct.md"]).strip()
            for revision, anchor, reason in (("", "", "neither --before-at nor --route-revision supplied"),
                                             ("", "absent", "before anchor is not a commit"),
                                             ("", blob, "before anchor is not a commit"),
                                             ("absent", "HEAD", "route revision or its parent is absent")):
                with self.subTest(revision=revision, anchor=anchor):
                    route = process.read_exposure_hashes(root, revision, anchor)
                    self.assertIsInstance(route, process.NotRecorded)
                    trial = process.select_trial_phases(trial_events(), WINDOW, route)[0]
                    self.assertIsInstance(trial.exposure, process.ExposureNotRecorded)
                    self.assertIn(reason, cast(process.ExposureNotRecorded, trial.exposure).reason)

    def test_start_window_includes_full_phase_and_gap_beyond_window(self) -> None:
        events = trial_events()
        trials = process.select_trial_phases(events, process.Window(10, 11), ROUTE)
        self.assertEqual([(trial.phase.start, trial.phase.end, trial.interval) for trial in trials],
                         [(10, 100, process.PhaseWithGap(120))])
        self.assertEqual(process.select_trial_phases(events, process.Window(0, 10), ROUTE), ())

    def test_no_following_phase_is_counted_separately(self) -> None:
        trials = process.select_trial_phases(trial_events()[:-1], WINDOW, ROUTE)
        self.assertIsInstance(trials[0].interval, process.NoFollowingPhase)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_trial(trials, (), WINDOW, "before")
        self.assertIn("NoFollowingPhase n=1", output.getvalue())
        self.assertRegex(output.getvalue(), r"weighted tokens\s+0\s+—")

    def test_next_phase_must_start_with_the_same_hash(self) -> None:
        for next_start in (contract_event("phase_started", 120, phase="next", direct_hash=NEW_HASH),
                           event("phase_started", 120, phase="next")):
            with self.subTest(next_start=next_start.record):
                trial = process.select_trial_phases((*trial_events()[:-1], next_start), WINDOW, ROUTE)[0]
                self.assertIsInstance(trial.exposure, process.BeforeChange)
                self.assertIsInstance(trial.interval, process.ExposureNotRecorded)

    def test_open_or_finished_overlap_with_one_director_is_shared(self) -> None:
        for finish in ((), (contract_event("phase_finished", 150, run="other"),)):
            with self.subTest(finished=bool(finish)):
                events = (*trial_events(), contract_event("run_started", 2, run="other"),
                          contract_event("phase_started", 90, run="other"), *finish)
                trial = process.select_trial_phases(events, process.Window(10, 11), ROUTE)[0]
                self.assertIsInstance(trial.interval, process.SharedDirector)

    def test_gap_overlap_is_shared_but_other_directors_do_not_exclude(self) -> None:
        for director, expected in ((DIRECTOR, process.SharedDirector),
                                   (process.DirectorSession("claude", "other"), process.PhaseWithGap)):
            with self.subTest(director=director):
                events = (*trial_events(), contract_event("run_started", 2, run="other", director=director),
                          contract_event("phase_started", 105, run="other", director=director))
                self.assertIsInstance(process.select_trial_phases(events, process.Window(10, 11), ROUTE)[0].interval, expected)

    def test_overlapping_phases_in_the_same_run_are_shared(self) -> None:
        events = (*trial_events()[:-1], contract_event("phase_started", 50, phase="next"))
        self.assertIsInstance(process.select_trial_phases(events, WINDOW, ROUTE)[0].interval, process.SharedDirector)

    def test_weighted_tokens_include_gap_and_keep_raw_classes_apart(self) -> None:
        trial = process.select_trial_phases(trial_events(), WINDOW, ROUTE)[0]
        turns = (process.UsageTurn(DIRECTOR, 10, 10, 2, "claude-sonnet-4-6", 4, 3, 20),
                 process.UsageTurn(DIRECTOR, 100, 7, 1, "claude-opus-4-6", 0, 0, 10),
                 process.UsageTurn(DIRECTOR, 120, 999, 999, "unknown"),
                 process.UsageTurn(DIRECTOR, 9, 999, 999, "unknown"),
                 process.UsageTurn(process.DirectorSession("claude", "worker"), 50, 999, 999, "unknown"))
        result = process.trial_tokens(trial, DIRECTOR, turns)
        # 10 + 4*1.25 + 3*2 + 20*.1 + 2*5; then 7 + 10*.1 + 1*5.
        self.assertEqual(result, process.TrialTokens(46.0, 54, 3, 2))

    def test_turn_time_selects_dated_weights_and_unknown_never_becomes_zero(self) -> None:
        at = datetime(2026, 10, 10, 21, 35, tzinfo=UTC).timestamp()
        phase = process.PhaseBoundary("run", "phase", "1", at - 10, at + 10)
        trial = process.TrialPhase(phase, process.BeforeChange(OLD_HASH), process.PhaseWithGap(at + 20))
        known = process.UsageTurn(DIRECTOR, at, 10, 2, "gpt-6.1-sol", 0, 0, 20)
        self.assertEqual(process.trial_tokens(trial, DIRECTOR, (known,)), process.TrialTokens(21.0, 30, 2, 1))
        for unknown in (replace(known, at=at - 1), replace(known, model="not-weighted")):
            result = process.trial_tokens(trial, DIRECTOR, (known, unknown))
            self.assertIsInstance(result, process.TrialTokens)
            self.assertIsInstance(cast(process.TrialTokens, result).weighted, process.Unweighted)
            self.assertEqual(cast(process.TrialTokens, result).raw_input, 60)
        self.assertIsInstance(process.trial_tokens(trial, DIRECTOR, ()), process.NotRecorded)

    def test_reload_context_uses_first_request_after_matching_result_and_counts_once(self) -> None:
        records = [
            transcript_record("system", 5, subtype="compact_boundary"),
            transcript_record("system", 20, subtype="compact_boundary"),
            request_record(21, "read", 2, [{"type": "tool_use", "id": "wrong", "name": "Read", "input": {"file_path": "/fixture/other.md"}}]),
            request_record(22, "contract", 3, [{"type": "tool_use", "id": "contract-read", "name": "Read", "input": {"file_path": "/home/fixture/.claude/commands/unit/direct.md"}}]),
            request_record(23, "too-early", 999, []),
            transcript_record("user", 24, message={"content": [{"type": "tool_result", "tool_use_id": "wrong"}]}),
            transcript_record("user", 25, message={"content": [{"type": "tool_result", "tool_use_id": "contract-read"}]}),
            request_record(26, "after", 400, []), request_record(27, "after", 400, []),
            request_record(28, "later", 900, []),
            transcript_record("system", 100, subtype="compact_boundary"),
        ]
        with tempfile.TemporaryDirectory(dir="/tmp/claude") as folder:
            path = Path(folder) / "director.jsonl"
            _ = path.write_text("\n".join(json.dumps(record) for record in records))
            before = path.read_bytes()
            transcript = process.read_transcript(path)
            self.assertEqual(path.read_bytes(), before)
        phase = process.PhaseBoundary("run", "phase", "1", 10, 100)
        self.assertEqual(process.measure_reloads(phase, transcript, frozenset(("commands/unit/direct.md",))),
                         process.PhaseReloads(1, (450,)))

    def test_step_read_after_route_is_a_reload_and_second_compaction_has_no_reload(self) -> None:
        phase = process.phase_boundaries(trial_events(NEW_HASH), WINDOW)[0]
        entries = (process.CompactionBoundary(20), process.ContractRead(21, "step", "docs/direct/phase_checks.md", "read"),
                   process.ReadResult(22, "step", False), process.ContextRequest(23, "next", 150),
                   process.CompactionBoundary(40), process.ContextRequest(41, "no-read", 900))
        result = process.measure_reloads(phase, process.DirectorTranscript(entries),
                                         process.reload_paths(trial_events(NEW_HASH), phase, process.AfterChange(NEW_HASH)))
        self.assertEqual(result, process.PhaseReloads(2, (150, process.NoReload("no contract Read after compact_boundary"))))
        before = process.measure_reloads(phase, process.DirectorTranscript(entries),
                                          process.reload_paths(trial_events(), phase, process.BeforeChange(OLD_HASH)))
        self.assertIsInstance(cast(tuple[object, ...], cast(process.PhaseReloads, before).contexts)[0], process.NoReload)

    def test_no_compaction_no_reload_failed_read_and_absent_transcript_are_distinct(self) -> None:
        phase = process.PhaseBoundary("run", "phase", "1", 10, 100)
        paths = frozenset(("commands/unit/direct.md",))
        self.assertEqual(process.measure_reloads(phase, process.DirectorTranscript(()), paths), process.PhaseReloads(0, process.NoCompaction()))
        for failed in (True, False):
            with self.subTest(failed=failed):
                entries: tuple[process.TranscriptEntry, ...] = (process.CompactionBoundary(20), process.ContractRead(21, "read", "commands/unit/direct.md", "a"))
                if failed:
                    entries += (process.ReadResult(22, "read", True),)
                result = process.measure_reloads(phase, process.DirectorTranscript(entries), paths)
                self.assertIsInstance(result, process.PhaseReloads)
                self.assertIsInstance(cast(tuple[object, ...], cast(process.PhaseReloads, result).contexts)[0], process.NoReload)
        absent = process.NotRecorded("fixture", "transcript absent")
        self.assertEqual(process.measure_reloads(phase, absent, paths), absent)
        with tempfile.TemporaryDirectory(dir="/tmp/claude") as folder:
            mapping = process.read_director_transcripts(Path(folder), (DIRECTOR,))
            self.assertIsInstance(mapping[DIRECTOR], process.NotRecorded)

    def test_new_boundary_stops_the_previous_reload_search(self) -> None:
        phase = process.PhaseBoundary("run", "phase", "1", 10, 100)
        entries = (process.CompactionBoundary(20), process.ContractRead(21, "read", "commands/unit/direct.md", "a"),
                   process.CompactionBoundary(22), process.ReadResult(23, "read", False), process.ContextRequest(24, "b", 999))
        result = process.measure_reloads(phase, process.DirectorTranscript(entries), frozenset(("commands/unit/direct.md",)))
        self.assertIsInstance(result, process.PhaseReloads)
        contexts = cast(process.PhaseReloads, result).contexts
        self.assertTrue(isinstance(contexts, tuple) and all(isinstance(item, process.NoReload) for item in contexts))

    def test_missing_request_after_result_is_not_a_zero_context(self) -> None:
        phase = process.PhaseBoundary("run", "phase", "1", 10, 100)
        entries = (process.CompactionBoundary(20), process.ContractRead(21, "read", "commands/unit/direct.md", "a"),
                   process.ReadResult(22, "read", False), process.ContextRequest(100, "outside", 700))
        result = process.measure_reloads(phase, process.DirectorTranscript(entries), frozenset(("commands/unit/direct.md",)))
        contexts = cast(process.PhaseReloads, result).contexts
        self.assertIsInstance(cast(tuple[object, ...], contexts)[0], process.NotRecorded)

    def test_units_filter_uses_recorded_labels_and_keeps_complete_runs(self) -> None:
        events = (*trial_events(), *(replace(entry, run="other", branch="other", directory="/fixture/other",
                                             record={**entry.record, "run_id": "other", "worktree": "other"}) for entry in trial_events()))
        self.assertEqual(process.filter_units(events, frozenset(("run",))), trial_events())
        self.assertEqual(process.filter_units(events, frozenset(("absent",))), ())

    def test_report_exposure_filter_counts_unknowns_without_including_their_cost(self) -> None:
        before = process.select_trial_phases(trial_events(), WINDOW, ROUTE)[0]
        after = replace(before, exposure=process.AfterChange(NEW_HASH))
        unknown = replace(before, exposure=process.ExposureNotRecorded("old event"), interval=process.NoFollowingPhase())
        measures = (process.TrialMeasures(before, process.TrialTokens(40.0, 50, 2, 1), process.PhaseReloads(0, process.NoCompaction())),
                    process.TrialMeasures(after, process.TrialTokens(999.0, 999, 999, 1), process.PhaseReloads(1, (900,))))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_trial((before, after, unknown), measures, WINDOW, "before")
        text = output.getvalue()
        self.assertIn("ExposureNotRecorded n=1", text)
        self.assertIn("ExposureNotRecorded: old event n=1", text)
        self.assertIn("NoFollowingPhase n=1", text)
        self.assertIn("NoCompaction n=1 phases", text)
        self.assertRegex(text, r"(?m)^weighted tokens\s+1\s+40\s+40\s+40$")
        self.assertNotIn("999", text)

    def test_trial_measures_fit_phone_with_large_counts_and_missing_records(self) -> None:
        trial = process.select_trial_phases(trial_events(), WINDOW, ROUTE)[0]
        absent = process.NotRecorded("fixture usage", "no token records")
        measures = (
            process.TrialMeasures(trial, process.TrialTokens(2_442_949.1, 20_100_000, 61_000, 1),
                                  process.PhaseReloads(1, (2_700_000,))),
            process.TrialMeasures(trial, absent, process.PhaseReloads(0, process.NoCompaction())),
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_trial((trial, trial), measures, WINDOW, "before")
        text = output.getvalue()
        section = text[text.index("Trial measures"):text.index("Context size after reload")].strip()
        lines = section.splitlines()
        for line in lines:
            self.assertLessEqual(len(line), 60, line)
        self.assertIn("eligible phases, natedev only", lines[0])
        self.assertIn("Director tokens: phase plus gap; compactions: phase", section)
        self.assertRegex(section, r"(?m)^weighted tokens\s+1\s+2\.4M\s+2\.4M\s+2\.4M$")
        self.assertRegex(section, r"(?m)^raw input with caches\s+1\s+20\.1M\s+20\.1M\s+20\.1M$")
        self.assertRegex(section, r"(?m)^raw output\s+1\s+61K\s+61K\s+61K$")
        self.assertRegex(section, r"(?m)^compactions\s+2\s+0\.5\s+1\s+1$")
        self.assertEqual([line for line in lines if line.startswith("not recorded:")], [
            "not recorded: weighted tokens n=1", "not recorded: raw input with caches n=1",
            "not recorded: raw output n=1",
        ])
        self.assertNotIn("not recorded", next(line for line in lines if line.startswith("measure")))
        # Other tables retain their full values and missing-count column.
        self.assertRegex(text, r"first request after contract Read result\s+1\s+2,700,000\s+2,700,000\s+2,700,000\s+0")

    def test_trial_table_headings_are_no_wider_than_empty_measure_rows(self) -> None:
        trial = process.select_trial_phases(trial_events()[:-1], WINDOW, ROUTE)[0]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_trial((trial,), (), WINDOW, "before")
        text = output.getvalue()
        lines = text[text.index("Trial measures"):text.index("Context size after reload")].strip().splitlines()
        row_width = min(len(line) for line in lines[3:])
        for heading in lines[:3]:
            self.assertLessEqual(len(heading), row_width, heading)
        self.assertTrue(all(len(line) <= 60 for line in lines), lines)

    def test_trial_measures_shorten_counts_at_thousand_and_million_boundaries(self) -> None:
        trial = process.select_trial_phases(trial_events(), WINDOW, ROUTE)[0]
        for value, expected in ((999.0, "999"), (1_000.0, "1K"), (54_998.0, "55K"),
                                (999_999.0, "1.0M"), (1_000_000.0, "1.0M"), (2_700_000.0, "2.7M")):
            with self.subTest(value=value):
                measures = (process.TrialMeasures(trial, process.TrialTokens(value, int(value), int(value), 1),
                                                  process.PhaseReloads(0, process.NoCompaction())),)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    process.render_trial((trial,), measures, WINDOW, "before")
                text = output.getvalue()
                section = text[text.index("Trial measures"):text.index("Context size after reload")].strip()
                self.assertRegex(section, rf"(?m)^weighted tokens\s+1\s+{re.escape(expected)}\s+{re.escape(expected)}\s+{re.escape(expected)}$")
                self.assertNotIn("not recorded:", section)
                self.assertTrue(all(len(line) <= 60 for line in section.splitlines()), section)

    def test_trial_measures_fit_phone_with_billions_and_trillions(self) -> None:
        trial = process.select_trial_phases(trial_events(), WINDOW, ROUTE)[0]
        for value, expected in ((10_000_000_000, "10.0B"), (12_300_000_000, "12.3B"),
                                (1_000_000_000_000, "1.0T"), (999_900_000_000_000, "999.9T")):
            with self.subTest(value=value):
                measures = (process.TrialMeasures(trial, process.TrialTokens(float(value), value, value, 1),
                                                  process.PhaseReloads(0, process.NoCompaction())),)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    process.render_trial((trial,), measures, WINDOW, "before")
                text = output.getvalue()
                section = text[text.index("Trial measures"):text.index("Context size after reload")].strip()
                for label in ("weighted tokens", "raw input with caches", "raw output"):
                    self.assertRegex(section, rf"(?m)^{label}\s+1\s+{re.escape(expected)}\s+{re.escape(expected)}\s+{re.escape(expected)}$")
                for line in section.splitlines():
                    self.assertLessEqual(len(line), 60, line)

    def test_trial_measures_promote_counts_that_round_to_the_next_unit(self) -> None:
        trial = process.select_trial_phases(trial_events(), WINDOW, ROUTE)[0]
        for value, expected in ((999_499, "999K"), (999_500, "1.0M"), (999_999, "1.0M"),
                                (999_949_999, "999.9M"), (999_950_000, "1.0B"),
                                (999_999_999, "1.0B"), (999_949_999_999, "999.9B"),
                                (999_950_000_000, "1.0T"), (999_999_999_999, "1.0T")):
            with self.subTest(value=value):
                measures = (process.TrialMeasures(trial, process.TrialTokens(float(value), value, value, 1),
                                                  process.PhaseReloads(0, process.NoCompaction())),)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    process.render_trial((trial,), measures, WINDOW, "before")
                text = output.getvalue()
                section = text[text.index("Trial measures"):text.index("Context size after reload")].strip()
                for label in ("weighted tokens", "raw input with caches", "raw output"):
                    self.assertRegex(section, rf"(?m)^{label}\s+1\s+{re.escape(expected)}\s+{re.escape(expected)}\s+{re.escape(expected)}$")
                self.assertTrue(all(len(line) <= 60 for line in section.splitlines()), section)

    def test_cli_filters_fixture_units_and_exposure_and_reads_usage_through_gap(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp/claude") as folder:
            root = Path(folder)
            contract = root / ".claude"
            commit_fixture_documents(contract, {"commands/unit/direct.md": "old contract\n"}, "Before")
            commit_fixture_documents(contract, {"commands/unit/direct.md": "short route\n"}, "After")
            revision = process.git_output(contract, ["rev-parse", "HEAD"]).strip()
            runs = root / "history/runs"
            runs.mkdir(parents=True)
            records = [entry.record for entry in trial_events()]
            other = process.DirectorSession("claude", "other-director")
            records.extend(contract_event(kind, at, run="other", director=other).record for kind, at in
                           (("run_started", 1), ("phase_started", 10), ("phase_finished", 100)))
            short = process.DirectorSession("claude", "short-director")
            records.extend(contract_event(kind, at, run="short", director=short).record for kind, at in
                           (("run_started", 1), ("phase_started", 10.25), ("phase_finished", 10.75)))
            _ = (runs / "fixture.jsonl").write_text("\n".join(json.dumps(record) for record in records))
            path = root / "usage.db"
            with closing(sqlite3.connect(path)) as db, db:
                _ = db.execute("CREATE TABLE turns (machine, family, agent_id, parent_id, at, input, cache_write_5m, cache_write_1h, cache_read, output, model)")
                _ = db.executemany("INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                    ("natedev", "claude", "director", "", "1970-01-01T00:00:10+00:00", 10, 0, 0, 0, 2, "claude-opus-4-6"),
                    ("natedev", "claude", "director", "", "1970-01-01T00:01:50+00:00", 20, 0, 0, 0, 1, "claude-opus-4-6"),
                    ("natedev", "claude", "director", "", "1970-01-01T00:02:00+00:00", 999, 0, 0, 0, 999, "claude-opus-4-6"),
                ])
            transcript = contract / "projects/project/director.jsonl"
            transcript.parent.mkdir(parents=True)
            _ = transcript.write_text("\n".join(json.dumps(record) for record in (
                transcript_record("system", 20, subtype="compact_boundary"),
                request_record(21, "read", 1, [{"type": "tool_use", "id": "read", "name": "Read", "input": {"file_path": "commands/unit/direct.md"}}]),
                transcript_record("user", 22, message={"content": [{"type": "tool_result", "tool_use_id": "read"}]}),
                request_record(23, "reloaded", 100, []),
            )))
            args = ["phase_process.py", "baseline", "--units", "run,short", "--exposure", "before", "--route-revision", revision,
                    "--since", "1970-01-01T00:00:10+00:00", "--until", "1970-01-01T00:00:11+00:00", "--detail", "trial"]
            output = io.StringIO()
            with patch.object(Path, "home", return_value=root), \
                    patch.object(plan_history, "history_root", return_value=root / "history"), \
                    patch.object(collect, "store_path", return_value=path), \
                    patch.object(process, "__file__", str(root / "scripts/direct/phase_process.py")), \
                    patch.object(sys, "argv", args), contextlib.redirect_stdout(output):
                self.assertEqual(process.main(), 0)
            text = output.getvalue()
            self.assertIn("BeforeChange n=2", text)
            self.assertIn("NoFollowingPhase n=1", text)
            self.assertIn("run/phase", text)
            self.assertNotIn("other/phase", text)
            self.assertRegex(text, r"(?m)^weighted tokens\s+1\s+45\s+45\s+45$")
            self.assertRegex(text, r"first request after contract Read result\s+1\s+150\s+150\s+150\s+0")

    def test_trial_phase_time_uses_start_selected_eligible_phases_and_recorded_units(self) -> None:
        events = trial_events()
        window = process.Window(10, 11)
        selected = process.select_trial_phases(events, window, ROUTE)
        self.assertEqual(process.phase_boundaries(events, window), ())
        self.assertEqual(len(selected), 1)
        other = replace(selected[0], phase=replace(selected[0].phase, run="second-run", end=40))
        missing_next = replace(selected[0], phase=replace(selected[0].phase, end=999), interval=process.NoFollowingPhase())
        second_start = replace(events[0], run="second-run", record={"worktree": "unit-a"})
        first_start = replace(events[0], record={"worktree": "unit-a"})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_trial_phase_time((first_start, second_start), (*selected, other, missing_next), window)
        self.assertIn(window.label(), output.getvalue())
        self.assertRegex(output.getvalue(), r"unit-a\s+2\s+60\.0s\s+90\.0s\s+120\.0s\s+0")
        self.assertNotIn("999", output.getvalue())

    def test_baseline_trial_phase_time_matches_exposure_filter_in_both_report_modes(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp/claude") as folder:
            root = Path(folder)
            commit_fixture_documents(root / ".claude", {"commands/unit/direct.md": "old contract\n"}, "Before")
            anchor = process.git_output(root / ".claude", ["rev-parse", "HEAD"]).strip()
            runs = root / "history/runs"
            runs.mkdir(parents=True)
            records = [entry.record for entry in trial_events()]
            other_director = process.DirectorSession("claude", "other")
            records.extend(contract_event(kind, at, run="other", direct_hash=NEW_HASH, director=other_director).record
                           for kind, at in (("run_started", 1), ("phase_started", 10), ("phase_finished", 500)))
            records.append(contract_event("phase_started", 510, run="other", phase="next", direct_hash=NEW_HASH,
                                          director=other_director).record)
            _ = (runs / "fixture.jsonl").write_text("\n".join(json.dumps(record) for record in records))
            for detail in ((), ("--detail", "trial")):
                with self.subTest(detail=detail):
                    args = ["phase_process.py", "baseline", "--exposure", "before", "--before-at", anchor,
                            "--since", "1970-01-01T00:00:10Z", "--until", "1970-01-01T00:00:11Z", *detail]
                    output = io.StringIO()
                    with patch.object(Path, "home", return_value=root), \
                            patch.object(plan_history, "history_root", return_value=root / "history"), \
                            patch.object(collect, "store_path", return_value=root / "absent.db"), \
                            patch.object(process, "__file__", str(root / "scripts/direct/phase_process.py")), \
                            patch.object(sys, "argv", args), contextlib.redirect_stdout(output):
                        self.assertEqual(process.main(), 0)
                    time_table = output.getvalue().split("Trial phase time per unit", 1)[1]
                    self.assertRegex(time_table, r"run\s+1\s+90\.0s\s+90\.0s\s+90\.0s\s+0")
                    self.assertNotIn("other", time_table)
                    self.assertIn("ExposureNotRecorded n=1", output.getvalue())

    def test_local_date_window_uses_the_user_zone(self) -> None:
        with patch.dict(os.environ, {"USER_ZONE": "America/New_York", "USER_ZONE_CONF": "/nonexistent"}):
            self.assertEqual(process.date_epoch("2026-10-28T09:00:00"), process.date_epoch("2026-10-28T13:00:00+00:00"))

    def test_cli_before_anchor_keeps_mixed_open_and_other_exclusion_counts(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp/claude") as folder:
            root = Path(folder)
            commit_fixture_documents(root / ".claude", {"commands/unit/direct.md": "old contract\n"}, "Before anchor")
            records: list[dict[str, object]] = []
            for run in ("before", "unknown", "last", "shared-a", "shared-b", "mixed", "open", "outside"):
                director = process.DirectorSession("claude", "shared" if run.startswith("shared-") else run)
                for kind, at, phase in (("run_started", 1, "phase"), ("phase_started", 10, "phase"),
                                        ("phase_finished", 100, "phase"), ("phase_started", 120, "next")):
                    if run in ("open", "outside") and kind == "phase_finished":
                        continue
                    if run in ("last", "mixed", "open", "outside") and phase == "next":
                        continue
                    record = {**contract_event(kind, at, run=run, phase=phase, director=director).record,
                              "phase_id": "1"}
                    if run == "unknown":
                        _ = record.pop("contract_revision")
                    records.append(record)
            records.append({"run_id": "mixed", "phase_instance_id": "phase", "phase_id": "1",
                            "event_type": "pass_finished", "timestamp_epoch": 60, "pass_started_at": 20,
                            "pass_elapsed_seconds": 40, "pass_kind": "impl", "pass_instance_id": "impl",
                            "pass_activity": "Implement Phase 7"})
            runs = root / "history/runs"
            runs.mkdir(parents=True)
            _ = (runs / "fixture.jsonl").write_text("\n".join(json.dumps(record) for record in records))
            args = ["phase_process.py", "baseline", "--units", "before,unknown,last,shared-a,shared-b,mixed,open",
                    "--exposure", "before", "--before-at", "HEAD",
                    "--since", "1970-01-01T00:00:10+00:00", "--until", "1970-01-01T00:01:41+00:00"]
            for detail in ((), ("--detail", "trial")):
                with self.subTest(detail=detail):
                    output = io.StringIO()
                    with patch.object(Path, "home", return_value=root), \
                            patch.object(plan_history, "history_root", return_value=root / "history"), \
                            patch.object(collect, "store_path", return_value=root / "absent-usage.db"), \
                            patch.object(process, "__file__", str(root / "scripts/direct/phase_process.py")), \
                            patch.object(sys, "argv", [*args, *detail]), contextlib.redirect_stdout(output):
                        self.assertEqual(process.main(), 0)
                    text = output.getvalue()
                    self.assertRegex(text, r"Trial exclusions before exposure filter[^\n]*mixed n=1; open unmixed n=1")
                    self.assertIn("ExposureNotRecorded n=1", text)
                    self.assertIn("NoFollowingPhase n=1", text)
                    self.assertIn("SharedDirector n=2", text)
                    self.assertRegex(text, r"(?m)^weighted tokens\s+0\s+—\s+—\s+—$")
                    self.assertIn("not recorded: weighted tokens n=1\n", text)
                    if not detail:
                        self.assertIn("Excluded from per-phase costs: mixed n=1", text)
                        self.assertIn("open unmixed n=1", text)


class PhaseProcessTests(unittest.TestCase):
    def test_wall_steps_share_overlap_and_preserve_director_gaps(self) -> None:
        events = (*phase_events(), event("activity_started", 40, instance="gate", step="verification"),
                  event("activity_finished", 80, instance="gate", step="verification"),
                  event("pass_started", 25, instance="second", step="implementation"),
                  event("pass_finished", 55, instance="second", step="implementation"))
        result = process.measure_phase_steps(events, process.phase_boundaries(events, WINDOW))
        costs = {cost.step: cost for cost in result.costs}
        self.assertEqual(costs["pass: implementation"].total, 30)
        self.assertEqual(costs["activity: verification"].total, 30)
        self.assertEqual(costs["director time"].total, 30)
        self.assertEqual(sum(cost.total for cost in result.costs), result.wall)
        self.assertAlmostEqual(costs["director time"].wall_share, 1 / 3)
        self.assertEqual(result.gaps[0].median, 20)

    def test_window_edges_select_completed_finish_and_keep_overlapping_phases(self) -> None:
        events = (*phase_events(), event("phase_started", 50, phase="other"),
                  event("phase_finished", 150, phase="other"),
                  event("phase_started", 170, phase="canceled"),
                  event("phase_finished", 180, phase="canceled", status="canceled"))
        phases = process.phase_boundaries(events, process.Window(100, 151))
        self.assertEqual([(phase.instance, phase.start, phase.end) for phase in phases],
                         [("phase", 10, 100), ("other", 50, 150)])
        self.assertEqual(process.phase_boundaries(events, process.Window(151, 200)), ())
        self.assertEqual(process.phase_boundaries(events, process.Window(0, 100)), ())

    def test_finished_phase_keeps_pass_coverage_when_it_starts_before_the_window(self) -> None:
        events = (event("run_started", 80), event("phase_started", 90),
                  event("pass_started", 100, instance="impl", step="implementation"),
                  event("pass_finished", 140, instance="impl", step="implementation"),
                  event("phase_finished", 150))
        phases = process.phase_boundaries(events, process.Window(100, 200))
        self.assertEqual([(phase.start, phase.end) for phase in phases], [(90, 150)])
        allocations = process.allocated_step_seconds(events, phases)
        self.assertEqual(allocations[0]["pass: implementation"], 40)
        self.assertNotIn("unmatched step", allocations[0])

    def test_rounds_parallel_repairs_are_worker_time_not_wall_share(self) -> None:
        records: list[dict[str, object]] = [
            {"event_type": "phase_started", "timestamp_epoch": 10},
            {"event_type": "phase_finished", "timestamp_epoch": 100, "status": "completed"},
        ]
        for instance in ("fix-a", "fix-b"):
            records.extend((
                {"event_type": "pass_started", "timestamp_epoch": 20, "pass_started_at": 20,
                 "pass_instance_id": instance, "pass_kind": "fix", "pass_activity": "merge conflict repair"},
                {"event_type": "pass_finished", "timestamp_epoch": 80, "pass_started_at": 20,
                 "pass_instance_id": instance, "pass_kind": "fix", "pass_activity": "merge conflict repair",
                 "pass_elapsed_seconds": 60},
            ))
        records.extend((
            {"event_type": "activity_started", "timestamp_epoch": 20,
             "activity_instance_id": "merge", "activity_label": "merge"},
            {"event_type": "activity_finished", "timestamp_epoch": 80,
             "activity_instance_id": "merge", "activity_label": "merge"},
        ))
        events = parsed_phase_records(records)
        phases = process.phase_boundaries(events, WINDOW)
        self.assertEqual(process.fix_time(phases), (process.FixWorkerTime(120, 1),))
        self.assertGreater(phases[0].repair.seconds, phases[0].end - phases[0].start)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_steps(events, WINDOW, False)
        text = output.getvalue()
        wall_table, worker_table = text.split("Fix costs", 1)
        self.assertNotIn("fix worker time", wall_table)
        self.assertNotIn("activity: merge", wall_table)
        self.assertIn("120.0s", worker_table)
        self.assertNotIn("not counted here", text)

    def test_finish_only_fix_suppresses_only_its_merge_coverage(self) -> None:
        for start_fields in ({"pass_started_at": 20}, {}):
            with self.subTest(start_fields=start_fields):
                events = parsed_phase_records([
                    {"event_type": "phase_started", "timestamp_epoch": 10},
                    {"event_type": "activity_started", "timestamp_epoch": 15,
                     "activity_instance_id": "merge", "activity_label": "merge"},
                    {"event_type": "pass_finished", "timestamp_epoch": 80,
                     "pass_instance_id": "fix", "pass_kind": "fix",
                     "pass_activity": "merge conflict repair", "pass_elapsed_seconds": 60,
                     **start_fields},
                    {"event_type": "activity_finished", "timestamp_epoch": 85,
                     "activity_instance_id": "merge", "activity_label": "merge"},
                    {"event_type": "phase_finished", "timestamp_epoch": 100, "status": "completed"},
                ])
                phases = process.phase_boundaries(events, WINDOW)
                self.assertEqual(process.fix_time(phases), (process.FixWorkerTime(60, 1),))
                costs = {cost.step: cost.total for cost in process.measure_phase_steps(events, phases).costs}
                self.assertEqual(costs["activity: merge"], 10)
                self.assertEqual(costs["director time"], 20)

    def test_rounds_open_phase_has_named_state_and_no_epoch_finish(self) -> None:
        events = parsed_phase_records([
            {"event_type": "phase_started", "timestamp_epoch": 10},
            {"event_type": "pass_started", "timestamp_epoch": 20,
             "pass_kind": "fix", "pass_instance_id": "open"},
        ])
        states = process.phase_states(events)
        self.assertEqual(states, (process.OpenPhase("run", "phase", 10),))
        self.assertEqual(process.phase_boundaries(events, WINDOW), ())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_steps(events, WINDOW, False)
        self.assertIn("open unmixed n=1", output.getvalue())

    def test_rounds_mixed_phase_is_excluded_from_costs_and_reported(self) -> None:
        events = parsed_phase_records([
            {"event_type": "phase_started", "timestamp_epoch": 10},
            {"event_type": "pass_finished", "timestamp_epoch": 60, "pass_started_at": 20,
             "pass_elapsed_seconds": 40, "pass_kind": "impl", "pass_instance_id": "impl",
             "pass_activity": "Implement Phase 7"},
            {"event_type": "phase_finished", "timestamp_epoch": 100, "status": "completed"},
        ])
        self.assertIsInstance(process.phase_states(events)[0], process.MixedPhase)
        self.assertEqual(process.phase_boundaries(events, WINDOW), ())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_steps(events, WINDOW, False)
        self.assertIn("mixed n=1", output.getvalue())
        self.assertEqual(process.measure_phase_steps(events, process.phase_boundaries(events, WINDOW)).wall, 0)

    def test_step_exclusion_counts_keep_finished_and_open_phases_from_before_the_window(self) -> None:
        events = parsed_phase_records([
            {"event_type": "phase_started", "timestamp_epoch": 90},
            {"event_type": "pass_finished", "timestamp_epoch": 140, "pass_started_at": 100,
             "pass_elapsed_seconds": 40, "pass_kind": "impl", "pass_instance_id": "impl",
             "pass_activity": "Implement Phase 7"},
            {"event_type": "phase_finished", "timestamp_epoch": 150, "status": "completed"},
            {"event_type": "phase_started", "timestamp_epoch": 50, "phase_instance_id": "open"},
        ])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_steps(events, process.Window(100, 200), False)
        self.assertIn("mixed n=1 (finished in window or open)", output.getvalue())
        self.assertIn("open unmixed n=1 (started before window end)", output.getvalue())

    def test_ledger_only_round_counts_without_worker_time(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "run.jsonl"
            records = [
                {"event_type": "phase_started", "timestamp_epoch": 10, "run_id": "run", "phase_id": "1", "phase_instance_id": "phase"},
                {"event_type": "finding_batch_dispatched", "timestamp_epoch": 30, "phase_instance_id": "phase", "round": 1, "covered": ["F1"]},
                {"event_type": "phase_finished", "timestamp_epoch": 100, "run_id": "run", "phase_id": "1", "phase_instance_id": "phase", "status": "completed"},
            ]
            _ = path.write_text("\n".join(json.dumps(record) for record in records))
            events, problems = process.read_events((path,))
        self.assertEqual(problems, ())
        self.assertEqual(process.fix_time(process.phase_boundaries(events, WINDOW)), (process.FixWorkerTime(0, 1),))

    def test_closure_review_stays_in_review_wall_costs(self) -> None:
        events = parsed_phase_records([
            {"event_type": "phase_started", "timestamp_epoch": 10},
            {"event_type": "pass_started", "timestamp_epoch": 20, "pass_started_at": 20,
             "pass_instance_id": "closure", "pass_kind": "review", "pass_activity": "Closure review"},
            {"event_type": "pass_finished", "timestamp_epoch": 60, "pass_started_at": 20,
             "pass_instance_id": "closure", "pass_kind": "review", "pass_elapsed_seconds": 40},
            {"event_type": "phase_finished", "timestamp_epoch": 100, "status": "completed"},
        ])
        phases = process.phase_boundaries(events, WINDOW)
        costs = {cost.step: cost.total for cost in process.measure_phase_steps(events, phases).costs}
        self.assertEqual(costs["pass: review"], 40)
        self.assertEqual(process.fix_time(phases), (process.FixWorkerTime(0, 0),))

    def test_document_after_dispatch_is_separate_from_lead_costs(self) -> None:
        pipeline = (
            process.DocMilestone("good", "good.md", process.DocCommit("a", 10, "", ""), 30, 50.0, "", ""),
            process.DocMilestone("late", "late.md", process.DocCommit("b", 60, "", ""), 30, 50.0, "", ""),
            process.DocMilestone("late", "proposal.md", process.DocCommit("c", 70, "", ""), 30, 50.0, "", ""),
        )
        leads = process.plan_lead_times(pipeline)
        self.assertIsInstance(leads[1].doc_to_phase, process.DocAfterDispatch)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_pipeline(pipeline, WINDOW, False)
        text = output.getvalue()
        self.assertIn("doc after dispatch: n plans=1; n documents=2", text)
        rows = {parts[0]: parts[1:] for line in text.splitlines() if len(parts := re.split(r"  +", line)) > 1}
        self.assertEqual(rows["first doc to first phase"], ["1", "40.0s", "40.0s", "40.0s", "0"])
        self.assertEqual(rows["run to first phase"], ["1", "20.0s", "20.0s", "20.0s", "0"])
        self.assertNotIn("late", text.split("Doc pipeline: top 10", 1)[1].split("Earliest doc requires", 1)[0])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_pipeline(pipeline, WINDOW, True)
        self.assertIn("doc after dispatch", output.getvalue())
        self.assertIn("late.md", output.getvalue())

    def test_late_documents_preserve_early_leads_and_missing_source_state(self) -> None:
        absent = process.NotRecorded("git log", "source absent")
        pipeline = (
            process.DocMilestone("early", "plan.md", process.DocCommit("a", 60, "", ""), 30, 50.0, "", ""),
            process.DocMilestone("early", "proposal.md", process.DocCommit("b", 10, "", ""), 30, 50.0, "", ""),
            process.DocMilestone("unknown", "late.md", process.DocCommit("c", 70, "", ""), 30, 50.0, "", ""),
            process.DocMilestone("unknown", "missing.md", absent, 30, 50.0, "", ""),
        )
        leads = process.plan_lead_times(pipeline)
        self.assertEqual((leads[0].doc_to_run, leads[0].run_to_phase, leads[0].doc_to_phase), (20, 20, 40))
        self.assertIsInstance(leads[1].doc_to_phase, process.NotRecorded)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_pipeline(pipeline, WINDOW, False)
        text = output.getvalue()
        self.assertIn("doc after dispatch: n plans=0; n documents=2", text)
        rows = {parts[0]: parts[1:] for line in text.splitlines() if len(parts := re.split(r"  +", line)) > 1}
        self.assertEqual(rows["first doc to first phase"], ["1", "40.0s", "40.0s", "40.0s", "1"])
        self.assertEqual(rows["run to first phase"], ["2", "20.0s", "20.0s", "40.0s", "0"])
        self.assertIn("early", text.split("Doc pipeline: top 10", 1)[1])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_pipeline(pipeline, WINDOW, True)
        late_rows = [line for line in output.getvalue().splitlines() if line.startswith("doc after dispatch")]
        self.assertEqual(len(late_rows), 2)
        self.assertTrue(any("plan.md" in line for line in late_rows))
        self.assertTrue(any("late.md" in line for line in late_rows))

    def test_history_label_normalization_and_bad_event_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            records: list[dict[str, object]] = [
                {"event_id": "one", "event_type": "activity_started", "timestamp_epoch": 10,
                 "activity_label": "plan fold-in 4", "activity_instance_id": "fold"},
                {"event_id": "one", "event_type": "activity_started", "timestamp_epoch": 10},
                {"event_type": "phase_started", "timestamp_epoch": 12, "spine_version": 99},
            ]
            _ = path.write_text("\n".join(json.dumps(record) for record in records) + "\nbroken\n")
            events, problems = process.read_events([path])
        self.assertEqual([entry.step for entry in events], ["plan update"])
        self.assertEqual(len(problems), 2)
        self.assertIn("unsupported spine version", problems[0])
        self.assertIn("invalid JSON", problems[1])

    def test_director_tokens_use_run_identity_and_pass_bounds(self) -> None:
        worker = process.DirectorSession("codex", "worker")
        events = (*phase_events(), event("pass_started", 70, instance="review", step="review", director=worker),
                  event("pass_finished", 90, instance="review", step="review", director=worker))
        turns = (process.UsageTurn(DIRECTOR, 15, 90, 10), process.UsageTurn(DIRECTOR, 40, 180, 20),
                 process.UsageTurn(DIRECTOR, 95, 90, 10), process.UsageTurn(worker, 40, 999, 999),
                 process.UsageTurn(DIRECTOR, 100, 999, 999))
        rows = process.measure_director_tokens(events, process.phase_boundaries(events, WINDOW), turns)
        cost = rows[0].cost
        self.assertIsInstance(cost, process.TokenCost)
        if isinstance(cost, process.TokenCost):
            self.assertEqual((cost.turns, cost.input, cost.output), (3, 360, 40))
            self.assertEqual((cost.before_share, cost.after_share), (.25, .25))

    def test_unmapped_tokens_name_the_missing_source(self) -> None:
        events = tuple(entry for entry in phase_events() if entry.kind != "run_started")
        result = process.measure_director_tokens(events, process.phase_boundaries(events, WINDOW), ())
        self.assertIsInstance(result[0].cost, process.NotRecorded)
        self.assertIn("run_started.main_agent.session_id", cast(process.NotRecorded, result[0].cost).source)

    def test_open_review_start_makes_after_last_pass_share_unknown(self) -> None:
        events = (event("run_started", 1), event("phase_started", 10),
                  event("pass_started", 20, instance="impl", step="implementation"),
                  event("pass_finished", 40, instance="impl", step="implementation"),
                  event("pass_started", 50, instance="review", step="review"),
                  event("phase_finished", 100))
        turns = (process.UsageTurn(DIRECTOR, 60, 90, 10),)
        rows = process.measure_director_tokens(events, process.phase_boundaries(events, WINDOW), turns)
        cost = rows[0].cost
        self.assertIsInstance(cost, process.TokenCost)
        if isinstance(cost, process.TokenCost):
            self.assertEqual((cost.input, cost.output, cost.before_share), (90, 10, 0))
            self.assertIsInstance(cost.after_share, process.NotRecorded)
            self.assertIn("last pass end unknown", cast(process.NotRecorded, cost.after_share).reason)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_tokens(rows, WINDOW, False)
        row = next(line for line in output.getvalue().splitlines() if line.startswith("after last pass share"))
        self.assertEqual(re.split(r"  +", row)[1:], ["0", "—", "—", "—", "1"])

    def test_readonly_usage_filters_machine_workers_and_window(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "usage.db"
            with closing(sqlite3.connect(path)) as db, db:
                _ = db.execute("CREATE TABLE turns (machine, family, agent_id, parent_id, at, input, cache_write_5m, cache_write_1h, cache_read, output, model)")
                rows = [("natedev", "claude", "director", "", "1970-01-01T00:00:30+00:00", 1, 2, 3, 4, 5),
                        ("mac", "claude", "director", "", "1970-01-01T00:00:30+00:00", 9, 0, 0, 0, 9),
                        ("natedev", "claude", "worker", "director", "1970-01-01T00:00:30+00:00", 9, 0, 0, 0, 9),
                        ("natedev", "claude", "director", "", "1970-01-01T00:03:20+00:00", 9, 0, 0, 0, 9)]
                _ = db.executemany("INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                   [(*row, "claude-opus-4-6") for row in rows])
            before = path.read_bytes()
            turns = process.read_usage(path, WINDOW)
            self.assertEqual(process.read_usage(path, WINDOW, [DIRECTOR]), turns)
            self.assertEqual(process.read_usage(path, WINDOW, [process.DirectorSession("claude", "absent")]), ())
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(turns, (process.UsageTurn(DIRECTOR, 30, 1, 5, "claude-opus-4-6", 2, 3, 4),))
        if isinstance(turns, tuple):
            self.assertEqual(turns[0].raw_input, 10)

    def test_contract_sums_full_reads_and_separates_final_gate(self) -> None:
        direct = b"Read `~/.claude/docs/direct/work.md` in full each phase.\nRead `~/.claude/docs/direct/final_gate_commit.md` in full once.\n"
        result = process.measure_contract([("commands/unit/direct.md", direct),
                                           ("docs/direct/work.md", b"one\ntwo\n"),
                                           ("docs/direct/final_gate_commit.md", b"end\n"),
                                           ("docs/direct/unused.md", b"no\n")])
        costs = {row.path: row for row in result}
        self.assertEqual(costs["docs/direct/work.md"].lines, 2)
        self.assertEqual(costs["docs/direct/work.md"].bytes, 8)
        self.assertEqual(costs["docs/direct/final_gate_commit.md"].cadence, "final gate only")
        self.assertNotIn("docs/direct/unused.md", costs)

    def test_pipeline_sources_and_commands_do_not_read_body_references(self) -> None:
        text = "# Plan\nProduction: `docs/production.md`\nSource: `docs/plans/header.md`\n## Source\n`docs/plans/design.md` via /plan:to_phased_plan\n`/fixture/issues/proposal.md`\n## Work\n`docs/plans/unrelated.md`\n"
        self.assertEqual(process.source_paths(text), ("/fixture/issues/proposal.md", "docs/plans/design.md", "docs/plans/header.md"))
        commit = process.DocCommit("abc", 10, "compile via /plan:to_phased_plan", text)
        source = process.DocCommit("def", 2, "design /team_review", "# Design")
        plan = process.PlanHistory("plan", ("run",), 30, 50, (commit,), (("design", (source,)),), "")
        rows = process.measure_doc_pipeline([plan])
        self.assertEqual([(row.document, row.first_commit) for row in rows], [("plan", commit), ("design", source)])
        self.assertEqual(rows[0].commands, "/plan:to_phased_plan")
        self.assertEqual(rows[1].commands, "/team_review")

    def test_markdown_source_links_resolve_from_plan_directory(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plan = root / "docs/plans/example.md"
            commit_fixture_documents(root, {
                "docs/plans/example.md": "# Plan\nSource: [Design](design.md), [Notes](../notes/other.md#detail), `docs/bare.md`\n## Work\n### Phase 1 — Build · status: todo\n",
                "docs/plans/design.md": "# Design\n",
                "docs/notes/other.md": "# Notes\n",
                "docs/bare.md": "# Bare\n",
            }, "Initial docs")
            histories = process.load_plan_histories(
                (event("run_started", 2_000_000_000, directory=str(root), plan=str(plan)),),
                process.Window(1_999_999_999, 2_000_000_010))
            sources = dict(histories[0].sources)
            self.assertEqual(set(sources), {str(root / path) for path in
                                           ("docs/plans/design.md", "docs/notes/other.md", "docs/bare.md")})
            self.assertEqual(sources[str(root / "docs/plans/design.md")][0].text, "# Design\n")
            self.assertTrue(all(commits for commits in sources.values()))

    def test_current_header_sources_are_united_with_initial_header(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plan = root / "docs/plans/example.md"
            commit_fixture_documents(root, {
                "docs/plans/example.md": "# Plan\nSource: `docs/initial.md`\n## Work\n### Phase 1 — Build · status: todo\n",
                "docs/initial.md": "# Initial source\n",
                "docs/current.md": "# Current source\n",
            }, "Initial lineage")
            _ = plan.write_text("# Plan\nSource: `docs/current.md`\n## Work\n### Phase 1 — Build · status: todo\n")
            histories = process.load_plan_histories(
                (event("run_started", 2_000_000_000, directory=str(root), plan=str(plan)),),
                process.Window(1_999_999_999, 2_000_000_010))
            self.assertEqual({path for path, commits in histories[0].sources if commits},
                             {str(root / "docs/initial.md"), str(root / "docs/current.md")})
            self.assertIn("docs/current.md", plan.read_text())

    def test_absolute_plan_in_another_repository_uses_document_history(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run_root, plan_root, source_root = root / "run", root / "plan", root / "source"
            source = source_root / "proposal.md"
            plan = plan_root / "docs/plans/example.md"
            commit_fixture_documents(run_root, {"README.md": "# Run repo\n"}, "Run repository")
            commit_fixture_documents(source_root, {"proposal.md": "# Proposal\n"}, "Created via /issue")
            commit_fixture_documents(plan_root, {"docs/plans/example.md":
                f"# Plan\nSource: `{source}`\n## Work\n### Phase 1 — Build · status: todo\n"}, "Compile /plan:to_phased_plan")
            histories = process.load_plan_histories(
                (event("run_started", 2_000_000_000, directory=str(run_root), plan=str(plan)),),
                process.Window(1_999_999_999, 2_000_000_010))
            self.assertEqual(histories[0].problem, "")
            self.assertEqual(histories[0].commits[0].message, "Compile /plan:to_phased_plan")
            self.assertEqual(histories[0].sources[0][1][0].message, "Created via /issue")
            self.assertEqual(process.doc_commits(str(run_root), str(plan)), histories[0].commits)
            self.assertEqual(process.source_history(run_root, str(source), 2_000_000_010)[0].text, "# Proposal\n")

    def test_issue_command_is_reported_from_proposal_commit(self) -> None:
        proposal = process.DocCommit("proposal", 10, "Created via /issue", "# Proposal\n")
        plan = process.PlanHistory("plan", ("run",), 30, 40, (), (("proposal", (proposal,)),), "")
        rows = process.measure_doc_pipeline((plan,))
        self.assertEqual(next(row.commands for row in rows if row.document == "proposal"), "/issue")

    def test_churn_counts_future_edits_pending_resolution_and_phase_insertions(self) -> None:
        base = "### Phase 1 — Build · status: todo\n#### Work Order\nA\n### Phase 2 — Ship · status: todo\n#### Work Order\nB\n"
        opened = base + "**Pending decision: API**\nNeed a choice.\n"
        resolved = base.replace("\nB\n", "\nnew B\n") + "### Phase 2a — Check · status: todo\n#### Work Order\nC\n"
        plan = process.PlanHistory("plan", ("run",), 10, 20,
                                   (process.DocCommit("a", 5, "initial", base),
                                    process.DocCommit("b", 50, "decision", opened),
                                    process.DocCommit("c", 110, "Split Phase 2 for checks", resolved)), (), "")
        events = (event("phase_started", 20), event("phase_finished", 100))
        row = process.measure_plan_churn([plan], events, WINDOW)[0]
        self.assertEqual((row.commits, row.todo_edits, row.decisions_opened, row.decisions_closed), (2, 1, 1, 1))
        self.assertEqual(row.inserted_phases, ("2a",))
        self.assertIn("Split Phase 2 for checks", row.split)

    def test_render_names_window_counts_and_missing_sources(self) -> None:
        output = io.StringIO()
        with patch.dict(os.environ, {"USER_ZONE": "America/New_York", "USER_ZONE_CONF": "/nonexistent"}), contextlib.redirect_stdout(output):
            process.render_baseline(phase_events(), WINDOW, process.NotRecorded("fixture usage", "absent"), (), (), ())
        text = output.getvalue()
        self.assertIn("natedev only", text)
        self.assertIn("fixture usage: absent", text)
        self.assertIn("1969-12-31 19:00:00 EST", text)
        self.assertIn("n phases", text)

    def test_baseline_starts_with_window_largest_step_and_repaired_phase_median(self) -> None:
        window = process.Window(0, 501)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_baseline(repair_report_events(), window, process.NotRecorded("fixture usage", "absent"), (), (), ())
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[:3], [
            f"The window {window.label()} has 5 completed phases.",
            "The largest step is pass: implementation, at 210.0s across 5 phases.",
            "Median repair time is 50.0s across 2 repaired phases.",
        ])
        self.assertTrue(lines[3].startswith("Phase process baseline"))

    def test_fix_distributions_exclude_phases_without_a_repair(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_steps(repair_report_events(), process.Window(0, 501), False)
        text = output.getvalue()
        rows = {cells[0]: cells[1:] for line in text.splitlines()
                if (cells := re.split(r"  +", line)) and cells[0] in ("fix worker time", "repair rounds")}
        self.assertEqual(rows["fix worker time"], ["2", "50.0s", "60.0s", "100.0s", "0"])
        self.assertEqual(rows["repair rounds"], ["2", "1.5", "2", "3", "0"])
        self.assertIn("3 of 5 phases had no repair.", text)

    def test_baseline_without_repairs_keeps_the_median_unrecorded(self) -> None:
        for events in ((), phase_events()):
            with self.subTest(events=bool(events)):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    process.render_baseline(events, WINDOW, process.NotRecorded("fixture usage", "absent"), (), (), ())
                text = output.getvalue()
                self.assertEqual(text.splitlines()[2], "No phases had a repair; median repair time is not recorded.")
                self.assertRegex(text, r"fix worker time\s+0\s+—\s+—\s+—\s+0")

    def test_duration_switches_to_minutes_only_above_one_thousand_seconds(self) -> None:
        for seconds, expected in ((0, "0.0s"), (999.9, "999.9s"), (1000, "1000.0s"), (1000.1, "16.7 min"),
                                  (1404, "23.4 min"), (3600, "60.0 min"), (-1000, "-1000.0s"), (-1000.1, "-16.7 min")):
            with self.subTest(seconds=seconds):
                self.assertEqual(process.duration(seconds), expected)

    def test_duration_switches_to_hours_at_two_hours(self) -> None:
        for seconds, expected in ((7199.9, "120.0 min"), (7200, "2.0 h"), (7200.1, "2.0 h"),
                                  (9000, "2.5 h"), (5943036, "1650.8 h"),
                                  (-7199.9, "-120.0 min"), (-7200, "-2.0 h"), (-9000, "-2.5 h")):
            with self.subTest(seconds=seconds):
                self.assertEqual(process.duration(seconds), expected)

    def test_baseline_formats_long_phase_gap_and_document_times_in_minutes(self) -> None:
        events = (event("run_started", 1), event("phase_started", 10),
                  event("pass_started", 20, instance="impl", step="implementation"),
                  event("pass_finished", 1424, instance="impl", step="implementation"),
                  event("pass_started", 2000, instance="fix", step="fix"),
                  event("pass_finished", 3200, instance="fix", step="fix"),
                  event("phase_finished", 4010), event("phase_started", 5510, phase="next"))
        plan = process.PlanHistory("fixture", ("run",), 2000, 2100,
                                   (process.DocCommit("fixture", 500, "Initial plan", "# Plan"),), (), "")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_baseline(events, process.Window(0, 6000), process.NotRecorded("fixture usage", "absent"), (), (plan,), ())
        text = output.getvalue()
        self.assertRegex(text, r"pass: implementation\s+1\s+23\.4 min")
        self.assertRegex(text, r"fix worker time\s+1\s+20\.0 min")
        self.assertRegex(text, r"phase end to next phase start\s+1\s+25\.0 min")
        self.assertRegex(text, r"first doc to run\s+1\s+25\.0 min")
        self.assertNotRegex(text, r"\d+(?:\.\d+)?h\b")
        self.assertTrue(all(float(match.group(1)) <= 1000 for match in re.finditer(r"\b(\d+(?:\.\d+)?)s\b", text)))

    def test_default_report_is_bounded_and_detail_selects_all_rows_of_one_table(self) -> None:
        events: list[process.Event] = []
        turns: list[process.UsageTurn] = []
        plans: list[process.PlanHistory] = []
        for index in range(1, 31):
            run = f"run-{index:03}"
            start = float(index * 100)
            events.extend((event("run_started", start - 10, run=run),
                           event("phase_started", start, run=run),
                           event("phase_finished", start + 90, run=run)))
            turns.append(process.UsageTurn(DIRECTOR, start + 10, index * 100, index * 10))
            plan = f"/fixture/docs/plans/plan-{index:03}.md"
            base = "### Phase 1 — Build · status: todo\n#### Work Order\nA\n"
            plans.append(process.PlanHistory(plan, (run,), start - 10, start,
                         (process.DocCommit(f"{index:08x}", 0, "initial", base),), (), ""))
        window = process.Window(0, 4000)
        contract = tuple(process.ContractFile(f"commands/file-{index}.md", index, index * 10, "per phase")
                         for index in range(1, 21))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_baseline(events, window, tuple(turns), contract, plans, ())
        text = output.getvalue()
        self.assertLessEqual(len(text.splitlines()), 124)
        self.assertLess(len(text.encode()), 25000)
        pipeline_top = text.split("Doc pipeline: top 10", 1)[1].split("Earliest doc requires", 1)[0]
        self.assertNotIn("plan-001.md", pipeline_top)
        self.assertIn("plan-030.md", pipeline_top)
        self.assertIn("other (n=20)", text)
        self.assertIn("other (n=10)", text)
        # After the summary, windows appear in table titles, never in each item row.
        self.assertTrue(all(" | " in line for line in text.splitlines()[1:] if window.label() in line))
        detail = io.StringIO()
        with contextlib.redirect_stdout(detail):
            process.render_detail("doc-pipeline", events, window, tuple(turns), contract, plans)
        self.assertIn("plan-001.md", detail.getvalue())
        self.assertIn("plan-030.md", detail.getvalue())
        self.assertNotIn("Director tokens", detail.getvalue())
        self.assertNotIn("Plan churn", detail.getvalue())

    def test_token_summary_quantiles_missing_counts_and_other_totals(self) -> None:
        tokens = tuple(process.PhaseTokens(process.PhaseBoundary("run", str(index), str(index), index, index + 1),
                       process.TokenCost(2, index * 100, index * 10, index / 100, .2)) for index in range(1, 26))
        tokens += (process.PhaseTokens(process.PhaseBoundary("missing", "x", "x", 0, 1),
                                      process.NotRecorded("fixture usage", "absent")),)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_tokens(tokens, WINDOW, False)
        rows = {parts[0]: parts[1:] for line in output.getvalue().splitlines()
                if len(parts := re.split(r"  +", line)) > 1}
        self.assertEqual(rows["input per phase"], ["25", "1,300", "2,300", "32,500", "1"])
        self.assertEqual(rows["other (n=15)"], ["15", "12,000", "1,200", "30"])
        self.assertEqual(rows["before first pass share"], ["25", "13.0%", "23.0%", "—", "1"])
        self.assertIn("fixture usage: absent", output.getvalue())

    def test_plan_summaries_use_source_lead_and_count_missing_evidence(self) -> None:
        absent = process.NotRecorded("fixture", "missing")
        pipeline = (process.DocMilestone("plan-a", "plan-a", process.DocCommit("a", 10, "", ""), 30, 50, "", ""),
                    process.DocMilestone("plan-a", "source-a", process.DocCommit("b", 0, "", ""), 30, 50, "", ""),
                    process.DocMilestone("plan-b", "plan-b", process.DocCommit("c", 20, "", ""), 40, absent, "", ""),
                    process.DocMilestone("plan-c", "plan-c", process.DocCommit("d", 10, "", ""), 60, 80, "", ""),
                    process.DocMilestone("plan-c", "source-c", absent, 60, 80, "", ""))
        leads = process.plan_lead_times(pipeline)
        self.assertEqual((leads[0].doc_to_run, leads[0].run_to_phase, leads[0].doc_to_phase), (30, 20, 50))
        self.assertIsInstance(leads[2].doc_to_run, process.NotRecorded)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_pipeline(pipeline, WINDOW, False)
        rows = {parts[0]: parts[1:] for line in output.getvalue().splitlines()
                if len(parts := re.split(r"  +", line)) > 1}
        self.assertEqual(rows["first doc to run"], ["2", "25.0s", "30.0s", "50.0s", "1"])
        self.assertEqual(rows["first doc to first phase"], ["1", "50.0s", "50.0s", "50.0s", "2"])
        churn = (process.PlanChurn("a", 5, 2, 3, 1, ("2", "3"), "a: Split phase 1", ""),
                 process.PlanChurn("b", 1, 0, 1, 1, (), "not recorded", ""),
                 process.PlanChurn("c", 0, 0, 0, 0, (), "not recorded", "missing history"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_churn(churn, WINDOW, False)
        rows = {parts[0]: parts[1:] for line in output.getvalue().splitlines()
                if len(parts := re.split(r"  +", line)) > 1}
        self.assertEqual(rows["Work Order edits"], ["2", "1", "2", "2", "1"])
        self.assertEqual(rows["decisions opened"], ["2", "2", "3", "4", "1"])
        self.assertEqual(rows["named phase splits"], ["1", "1", "1", "1", "2"])

    def test_small_activity_tail_preserves_allocation_before_merging(self) -> None:
        events = (event("phase_started", 0), event("phase_finished", 1000),
                  event("pass_started", 0, instance="impl", step="implementation"),
                  event("pass_finished", 500, instance="impl", step="implementation"),
                  event("activity_started", 100, instance="a", step="a"),
                  event("activity_finished", 100.3, instance="a", step="a"),
                  event("activity_started", 100.1, instance="b", step="b"),
                  event("activity_finished", 100.4, instance="b", step="b"))
        phases = process.phase_boundaries(events, process.Window(0, 1001))
        steps = process.measure_phase_steps(events, phases)
        compact = process.compact_steps(events, phases, steps)
        tail = next(row for row in compact if row.step.startswith("activity: other"))
        self.assertAlmostEqual(tail.total, 7 / 30)
        self.assertEqual((tail.step, tail.n), ("activity: other (2 labels)", 1))
        self.assertAlmostEqual(sum(row.total for row in compact), 1000)

    def test_contract_follows_command_reads_one_level_and_marks_cadence(self) -> None:
        direct = (b"Read `~/.claude/commands/unit/checkpoint.md` in full each phase.\n"
                  b"Read `~/.claude/commands/unit/add_ons.md` in full each phase.\n"
                  b"Read `~/.claude/docs/direct/run_phase_review.md` in full each phase.\n"
                  b"Read `~/.claude/docs/direct/write_prompt_contract.md` in full before dispatch.\n"
                  b"Read `~/.claude/docs/direct/final_gate_commit.md` in full.\n"
                  b"Read `~/.claude/commands/unit/brief.md` in full.\n"
                  b"| <PhaseChecks/> | `docs/direct/phase_checks.md` | - |\n")
        review = (b"Read `~/.claude/commands/plan/phase_review.md` in full.\n"
                  b"Read `~/.claude/commands/plan/shrink.md` in full.\n")
        files = [("commands/unit/direct.md", direct), ("docs/direct/run_phase_review.md", review),
                 ("commands/plan/phase_review.md", b"Read `docs/grandchild.md` in full.\n"),
                 ("commands/plan/shrink.md", b"Shrink.\n"), ("commands/unit/checkpoint.md", b"Commit.\n"),
                 ("commands/unit/add_ons.md", b"Consider.\n"), ("docs/direct/phase_checks.md", b"Check.\n"),
                 ("docs/direct/write_prompt_contract.md", b"Prompt.\n"),
                 ("docs/direct/final_gate_commit.md", b"Read `commands/plan/to_as_built.md` in full.\n"),
                 ("commands/unit/brief.md", b"Brief.\n"), ("docs/grandchild.md", b"No.\n"),
                 ("commands/plan/to_as_built.md", b"Final only.\n")]
        costs = {row.path: row for row in process.measure_contract(files)}
        self.assertEqual(costs["commands/unit/checkpoint.md"].bytes, 8)
        self.assertEqual(costs["commands/unit/add_ons.md"].cadence, "per phase")
        self.assertEqual(costs["commands/plan/shrink.md"].lines, 1)
        self.assertEqual(costs["docs/direct/phase_checks.md"].cadence, "per phase")
        self.assertEqual(costs["docs/direct/write_prompt_contract.md"].cadence, "per dispatch")
        self.assertEqual(costs["commands/unit/brief.md"].cadence, "on request")
        self.assertNotIn("docs/grandchild.md", costs)
        self.assertNotIn("commands/plan/to_as_built.md", costs)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            process.render_contract(tuple(costs.values()), WINDOW, True)
        summary = next(line for line in output.getvalue().splitlines() if line.startswith("sum per phase"))
        self.assertEqual(int(re.split(r"  +", summary)[3]), sum(len(entry[1]) for entry in files[:8]))

    def test_git_history_reads_actual_commits_without_worktree_changes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def git(*args: str) -> str:
                return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout
            _ = git("init", "-q")
            _ = git("config", "user.name", "Fixture")
            _ = git("config", "user.email", "fixture@example.test")
            path = root / "docs/plans/example.md"
            path.parent.mkdir(parents=True)
            _ = path.write_text("# Plan\n### Phase 1 — Build · status: todo\n#### Work Order\nBuild.\n")
            _ = git("add", ".")
            _ = git("-c", "commit.gpgsign=false", "commit", "-qm", "/plan:to_phased_plan")
            _ = path.write_text(path.read_text() + "More detail.\n")
            _ = git("add", ".")
            _ = git("-c", "commit.gpgsign=false", "commit", "-qm", "Edit Work Order")
            before = git("status", "--porcelain")
            commits = process.doc_commits(str(root), "docs/plans/example.md")
            self.assertEqual(git("status", "--porcelain"), before)
            start = commits[-1].at + 1
            events = (event("run_started", start, directory=str(root)),
                      event("phase_started", start + 1, directory=str(root)),
                      event("run_started", start + 10, run="resume", directory=str(root)))
            plans = process.load_plan_histories(events, process.Window(start + 9, start + 20))
            self.assertEqual((plans[0].started, plans[0].first_phase), (start, start + 1))
            renamed = root / "docs/as-built/renamed.md"
            renamed.parent.mkdir(parents=True)
            _ = git("mv", "docs/plans/example.md", "docs/as-built/renamed.md")
            _ = git("-c", "commit.gpgsign=false", "commit", "-qm", "Archive the plan")
            followed = process.doc_commits(str(root), "docs/as-built/renamed.md")
            self.assertEqual(len(followed), 3)
            self.assertEqual(followed[0].text, commits[0].text)
            self.assertEqual(followed[-1].text, commits[-1].text)
        self.assertEqual(len(commits), 2)
        self.assertNotIn("More detail.", commits[0].text)
        self.assertIn("More detail.", commits[1].text)


class DesignRecommendationTests(unittest.TestCase):
    @property
    def document(self) -> str:
        return (Path(__file__).resolve().parents[2] / "docs/plans/sdlc-phases-design.md").read_text()

    @property
    def recommendation(self) -> str:
        return self.document.split("## Recommendation\n", 1)[1].split("## Steps\n", 1)[0]

    @property
    def steps(self) -> str:
        return self.document.split("## Steps\n", 1)[1]

    def required_match(self, pattern: str, text: str) -> str:
        match = re.search(pattern, text)
        if match is None:
            self.fail(f"Missing design commitment matching {pattern!r}")
        return match.group(1)

    def test_full_route_estimates_and_targets_agree_with_the_recommendation(self) -> None:
        row = next(line for line in self.recommendation.splitlines() if line.startswith("| Full A |"))
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        tokens = self.required_match(r"(\d+–\d+%)", cells[1])
        gap = cells[2]
        hours = self.required_match(r"(\d+–\d+)", cells[3])
        candidate = self.document.split("**Full A.", 1)[1].split("**B.", 1)[0]
        full_step = self.steps.split("3. **Full A", 1)[1].split("4. **G", 1)[0]
        evidence_row = next(line for line in self.steps.splitlines() if line.startswith("| Full A |"))
        self.assertEqual(self.required_match(r"Total:\s*(\d+–\d+) work hours", candidate), hours)
        self.assertEqual(self.required_match(r"Total:\s*(\d+–\d+) h", full_step), hours)
        self.assertEqual(self.required_match(r"\| (\d+–\d+) with recording", evidence_row), hours)
        for section in (candidate, full_step):
            with self.subTest(section=section[:30]):
                self.assertIn(tokens, section)
                self.assertIn(gap, section)
        comparison = next(line for line in self.document.splitlines() if line.startswith("| Full A:"))
        self.assertIn(tokens, comparison)
        self.assertIn(gap, comparison)
        for match in re.finditer(r"Full A: (\d[^|\n]+)", self.document):
            rank = match.group(1)
            self.assertIn(tokens, rank)
            self.assertIn(gap, rank)
        rationale = self.document.split("## Recommendation", 1)[0].strip().rsplit("\n\n", 1)[1]
        self.assertEqual(self.required_match(r"full A's target is\s*\d+–\d+% over (\d+–\d+) h", rationale), hours)

    def test_every_step_one_statement_agrees_with_the_first_approved_step(self) -> None:
        first_step = self.required_match(r"\n1\. \*\*([^—]+) —", self.steps).strip()
        mentions = [paragraph for paragraph in self.document.split("\n\n") if "Step 1" in paragraph]
        self.assertGreater(len(mentions), 1)
        for paragraph in mentions:
            with self.subTest(paragraph=paragraph[:60]):
                self.assertIn(first_step.casefold(), paragraph.casefold())
        recommended = self.required_match(r"Step 1 is ([^:]+):", self.recommendation)
        self.assertEqual(first_step.casefold(), recommended.casefold())

    def test_review_wait_figures_agree_with_the_recommendation(self) -> None:
        row = next(line for line in self.recommendation.splitlines() if line.startswith("| C |"))
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        tokens = cells[1]
        gap = self.required_match(r"(\d+–\d+ s)", cells[2])
        hours = cells[3]
        comparison = next(line for line in self.document.splitlines() if line.startswith("| C:"))
        self.assertIn(tokens, comparison)
        self.assertIn(gap, comparison)
        for match in re.finditer(r"C: (0[^|\n]+)", self.document):
            rank = match.group(1)
            self.assertIn(tokens, rank)
            self.assertIn(gap, rank)
        evidence = next(line for line in self.steps.splitlines() if line.startswith("| C |"))
        self.assertEqual(self.required_match(r"\| (\d+–\d+) with recording", evidence), hours)
        alternative = self.steps.split("6. **C then D", 1)[1].split("7. **E", 1)[0]
        self.assertEqual(self.required_match(r"Total C: (\d+–\d+) h", alternative), hours)
        self.assertIn(tokens, alternative)
        self.assertIn(gap, alternative)


if __name__ == "__main__":
    _ = unittest.main()
