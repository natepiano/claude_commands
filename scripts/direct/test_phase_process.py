"""Fixture tests for phase process costs and document history."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from scripts.direct import phase_process as process


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

    def test_boundary_seam_selects_completed_finish_and_keeps_overlapping_phases(self) -> None:
        events = (*phase_events(), event("phase_started", 50, phase="other"),
                  event("phase_finished", 150, phase="other"),
                  event("phase_started", 170, phase="canceled"),
                  event("phase_finished", 180, phase="canceled", status="canceled"))
        phases = process.phase_boundaries(events, process.Window(100, 151))
        self.assertEqual([(phase.instance, phase.start, phase.end) for phase in phases],
                         [("phase", 10, 100), ("other", 50, 150)])
        self.assertEqual(process.phase_boundaries(events, process.Window(151, 200)), ())

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
            with sqlite3.connect(path) as db:
                _ = db.execute("CREATE TABLE turns (machine, family, agent_id, parent_id, at, input, cache_write_5m, cache_write_1h, cache_read, output)")
                rows = [("natedev", "claude", "director", "", "1970-01-01T00:00:30+00:00", 1, 2, 3, 4, 5),
                        ("mac", "claude", "director", "", "1970-01-01T00:00:30+00:00", 9, 0, 0, 0, 9),
                        ("natedev", "claude", "worker", "director", "1970-01-01T00:00:30+00:00", 9, 0, 0, 0, 9),
                        ("natedev", "claude", "director", "", "1970-01-01T00:03:20+00:00", 9, 0, 0, 0, 9)]
                _ = db.executemany("INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            before = path.read_bytes()
            turns = process.read_usage(path, WINDOW)
            self.assertEqual(process.read_usage(path, WINDOW, [DIRECTOR]), turns)
            self.assertEqual(process.read_usage(path, WINDOW, [process.DirectorSession("claude", "absent")]), ())
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(turns, (process.UsageTurn(DIRECTOR, 30, 10, 5),))

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
        self.assertLessEqual(len(text.splitlines()), 120)
        self.assertLess(len(text.encode()), 25000)
        pipeline_top = text.split("Doc pipeline: top 10", 1)[1].split("Earliest doc requires", 1)[0]
        self.assertNotIn("plan-001.md", pipeline_top)
        self.assertIn("plan-030.md", pipeline_top)
        self.assertIn("other (n=20)", text)
        self.assertIn("other (n=10)", text)
        # A window appears only in a table title, never padded into each item row.
        self.assertTrue(all(" | " in line for line in text.splitlines() if window.label() in line))
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


if __name__ == "__main__":
    _ = unittest.main()
