"""Named assignment dispatches through the real recorder and loopback app-server."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import cast, final
from unittest.mock import patch

from scripts.agents import codex_mesh, test_codex_mesh as mesh_fixture
from scripts.production import staff, worker_admission
from scripts.production.staff_test_support import fixture_worker, transcript_fixture
from scripts.usage import agents_log
from datetime import datetime, timezone
from scripts.direct.assign_launcher_test_support import ASSIGN, AssignLauncherFixture, LauncherScript, read_object
from scripts.lint import style_review
from scripts.lib.test_models_registry import RegistryReadProbe, write_registry


def setUpModule() -> None:
    mesh_fixture.setUpModule()


@final
class AssignLauncherTests(AssignLauncherFixture):
    def test_claude_worker_returns_through_start_and_then_follows_its_codex_thread(self) -> None:
        self.commit_project()
        Path(os.environ["NOTIFIER_SESSIONS_DIR"]).mkdir(parents=True, exist_ok=True)
        owner = staff.native_id(os.environ["CODEX_THREAD_ID"])
        row = fixture_worker("switch1", owner, backend=staff.ClaudeBackend())
        source, evidence = transcript_fixture(row, staff.ClaudeBackend())
        agents_log.append(row.current_session, "interactive", None, datetime.now(timezone.utc))
        roster = self.session_dir / codex_mesh.ROSTER_FILE
        _ = roster.write_text(json.dumps({row.name: {"staff_id": row.id, "status": "done"}}))
        commands = self.session_dir.parent / "mesh-commands.jsonl"
        releases = self.session_dir.parent / "source-releases.jsonl"
        directory = self.session_dir.parent / "instrument-mesh"
        directory.mkdir()
        code = "\n".join((
            "import json, os, sys",
            "from pathlib import Path",
            "def enter(frame, event, argument):",
            "    if event != 'call' or frame.f_code.co_name != 'main' or not frame.f_code.co_filename.endswith('/agents/codex_mesh.py'): return",
            "    sys.setprofile(None)",
            "    mesh = sys.modules['__main__']",
            f"    with Path({str(commands)!r}).open('a') as output: output.write(json.dumps(sys.argv[1:]) + chr(10))",
            "    def start_server(session_dir, workers_home, *extra):",
            f"        Path(session_dir, mesh.SERVER_FILE).write_text(json.dumps({{'pid': os.getpid(), 'port': {self.server.port}, 'codex_home': str(workers_home), 'auto_compact_tokens': mesh.AUTO_COMPACT_TOKENS, 'dev_shell_worktree': {str(self.project)!r}}}))",
            f"        return {self.server.port}",
            "    mesh._start_server = start_server",
            "    mesh._start_watcher = lambda *args: mesh.ExistingServerWatcher()",
            "    def observe_release(frame, event, result):",
            "        if event == 'return' and frame.f_code.co_name == 'release_source' and frame.f_code.co_filename.endswith('/shutdown/stop_resume.py'):",
            "            row = frame.f_locals['row']",
            f"            with Path({str(releases)!r}).open('a') as output: output.write(json.dumps({{'session': row.current_session, 'result': type(result).__name__}}) + chr(10))",
            "    sys.setprofile(observe_release)",
            "sys.setprofile(enter)",
        ))
        _ = (directory / "sitecustomize.py").write_text(code)
        with patch.dict(os.environ, {"PYTHONPATH": str(directory) + os.pathsep + str(ASSIGN.parents[2])}):
            for live_server, named in ((False, True), (True, True), (True, False)):
                with self.subTest(live_server=live_server, named=named):
                    if not live_server:
                        (self.session_dir / codex_mesh.SERVER_FILE).unlink(missing_ok=True)
                    else:
                        mesh_fixture.write_server_record(self.session_dir, self.server.port, self.home / ".codex",
                                                         str(self.project))
                    if not named:
                        agents_log.append(row.current_session, "impl", None, datetime.now(timezone.utc))
                    commands.unlink(missing_ok=True)
                    self.server.requests.clear()
                    result = self.launch("implement", *(("--to", row.name) if named else ()))
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    called = [cast(list[str], json.loads(line))[0] for line in commands.read_text().splitlines()]
                    self.assertIn("start", called)
                    self.assertNotIn("can-follow", called)
                    self.assertNotIn("follow", called)
                    current = staff.by_id(row.id)
                    assert current is not None
                    self.assertNotEqual(current.current_session, row.current_session)
                    self.assertEqual(current.to_json() | {"current_session": row.current_session}, row.to_json())
                    self.assertEqual(len(self.methods("turn/start")), 1)
                    extract = worker_admission.handoff_path(self.session_dir, row)
                    self.assertIn(evidence[0][0], extract.read_text())
                    opening = self.turn_inputs[-1]
                    self.assertLess(opening.index(str(extract)), opening.index(self.prompt.read_text()))
                    self.assertIn(str(source), opening)
                    stopped = [cast(dict[str, object], json.loads(line)) for line in releases.read_text().splitlines()]
                    self.assertEqual(stopped[-1], {"session": row.current_session, "result": "SourceReleased"})
                    commands.unlink()
                    self.server.requests.clear()
                    follow = self.launch("implement", "--to", row.name)
                    self.assertEqual(follow.returncode, 0, follow.stdout + follow.stderr)
                    called = [cast(list[str], json.loads(line))[0] for line in commands.read_text().splitlines()]
                    self.assertIn("can-follow", called)
                    self.assertIn("follow", called)
                    self.assertNotIn("start", called)
                    self.assertEqual(staff.by_id(row.id), current)
                    self.assertEqual(len(self.methods("turn/start")), 1)
                    _ = staff.update_current_session(current.current_session, staff.native_id(row.current_session))
                    _ = roster.write_text(json.dumps({row.name: {"staff_id": row.id, "status": "done"}}))

    def test_codex_retry_starts_with_the_saved_handoff_and_accepted_or_unrelated_work_follows(self) -> None:
        self.commit_project()
        _ = self.successful("implement", "--new-worker", "retry1")
        row = worker_admission.member(self.session_dir, "retry1", staff.local_snapshot())
        assert row is not None
        roster = self.session_dir / codex_mesh.ROSTER_FILE
        original = read_object(roster)
        _ = self.prompt.write_text("Continue the interrupted switch.\n")
        request = worker_admission.assignment_id(self.prompt)
        extract = worker_admission.handoff_path(self.session_dir, row)
        _ = extract.write_text(f"{worker_admission.HANDOFF_SOURCE_PREFIX}{self.session_dir / 'source.jsonl'}\nKeep the source work.\n")
        commands = self.session_dir.parent / "retry-commands.jsonl"
        directory = self.session_dir.parent / "retry-observer"
        directory.mkdir()
        _ = (directory / "sitecustomize.py").write_text("\n".join((
            "import json, sys",
            "from pathlib import Path",
            "def observe(frame, event, argument):",
            "    if event == 'call' and frame.f_code.co_name == 'main' and frame.f_code.co_filename.endswith('/agents/codex_mesh.py'):",
            f"        with Path({str(commands)!r}).open('a') as output: output.write(json.dumps(sys.argv[1:]) + chr(10))",
            "        sys.setprofile(None)",
            "sys.setprofile(observe)",
        )))
        cases = (("prepared", request, True), ("sending", request, True), ("", request, True),
                 ("accepted", request, False), ("sending", "another-prompt", False))
        with patch.dict(os.environ, {"PYTHONPATH": str(directory) + os.pathsep + str(ASSIGN.parents[2])}):
            for state, identity, pending in cases:
                with self.subTest(state=state, identity=identity):
                    entry = cast(dict[str, object], original[row.name]).copy()
                    entry.update(assignment_id=identity, dispatch_state=state)
                    _ = roster.write_text(json.dumps({row.name: entry}))
                    commands.unlink(missing_ok=True)
                    self.server.requests.clear()
                    result = self.launch("implement", "--to", row.name)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    called = [cast(list[str], json.loads(line))[0] for line in commands.read_text().splitlines()]
                    self.assertIn("start" if pending else "follow", called)
                    self.assertNotIn("can-follow" if pending else "start", called)
                    if pending:
                        self.assertNotIn("follow", called)
                        opening = self.turn_inputs[-1]
                        self.assertLess(opening.index(str(extract)), opening.index(self.prompt.read_text()))
                    self.assertEqual(self.methods("thread/start"), [])
                    self.assertEqual(staff.by_id(row.id), row)

    def test_assignment_shares_one_generation_through_allocation_and_refreshes_the_next_launch(self) -> None:
        probe = RegistryReadProbe(self.session_dir.parent, self.registry)
        _ = write_registry(self.registry, {"direct.codex": {"impl": "gpt-kept-sol:high"},
                                           "codex.agents": {"gpt-kept-sol": "high", "gpt-other-sol": "high,max"},
                                           "direct.options": {"codex_service_tier": "fast"}})
        original = self.server.respond
        edited = False

        @self.hang_guard.callback
        def change_after_creation(request: dict[str, object]) -> list[dict[str, object]] | None:
            nonlocal edited
            if request.get("method") == "thread/start" and not edited:
                edited = True
                _ = write_registry(self.registry, {"direct.codex": {"impl": "gpt-other-sol:max"},
                                                   "codex.agents": {"gpt-other-sol": "high,max"},
                                                   "direct.options": {"codex_service_tier": "flex"}})
            return original(request)

        self.server.respond = change_after_creation
        with patch.dict(os.environ, probe.environment(os.environ)):
            for expected in (("gpt-kept-sol", "high", "fast"), ("gpt-other-sol", "max", "flex")):
                with self.subTest(expected=expected):
                    probe.clear()
                    self.server.requests.clear()
                    _ = self.successful(execution=LauncherScript(ASSIGN))
                    turns = self.methods("turn/start")
                    self.assertEqual(len(turns), 1)
                    params = cast(dict[str, object], turns[0]["params"])
                    self.assertEqual((params.get("model"), params.get("effort"), params.get("serviceTier")), expected)
                    self.assertEqual(len(probe.reads()), 1, probe.reads())
        self.assertTrue(edited)
    def test_only_a_live_claude_or_codex_ancestor_permits_assignment(self) -> None:
        for family in ("claude", "codex"):
            with self.subTest(family=family):
                result = self.run_launcher(["bash", str(ASSIGN), "--help"], family=family)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for family in ("systemd", "bash", "codex-helper"):
            with self.subTest(parent=family):
                result = self.run_launcher(["bash", str(ASSIGN), "--help"], family=family,
                                           isolate_ancestors=True)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("requires a live Claude or Codex ancestor", result.stderr)
        self.assertEqual(self.methods("thread/start"), [])

    def test_dispatch_has_a_durable_worker_row_before_submitting_work(self) -> None:
        from scripts.production import staff
        _ = self.successful()
        self.assertIn("As your last act, write your summary to",
                      (self.session_dir / "phases/7/dispatch_1_prompt.txt").read_text())
        name = next(iter(self.assignment_records()))
        rows = staff.snapshot().find(kind="worker", parent=staff.StaffId(os.environ["CODEX_THREAD_ID"]))
        self.assertEqual([row.name for row in rows], [name])

    def test_assignment_dispatch_uses_the_backend_thread_as_the_published_worker_id(self) -> None:
        from scripts.production import staff
        _ = self.successful()
        records = self.assignment_records()
        self.assertEqual(len(records), 1)
        name, record = next(iter(records.items()))
        thread = str(record["thread_id"])
        row = staff.by_id(staff.StaffId(thread))
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual((row.id, row.name, row.parent), (thread, name, staff.ReportsTo(staff.StaffId(os.environ["CODEX_THREAD_ID"]))))
        self.assertTrue(self.claims)
        self.assertTrue(all(str(entry["thread_id"]) == row.id for entry in self.claims))

    def test_unreadable_staff_starts_no_worker(self) -> None:
        record = Path(os.environ["STAFF_FILE"])
        _ = record.write_text("{broken")
        result = self.launch(execution=LauncherScript(ASSIGN))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("invalid staff JSON:", result.stderr)
        self.assertEqual(self.methods("thread/start"), [])
        self.assertEqual(record.read_text(), "{broken")

    def test_rerun_review_dispatch_keeps_its_lens_and_read_only_thread(self) -> None:
        _ = self.successful("review", "--lens", "rerun", "--pass", "1")
        starts = self.methods("thread/start")
        self.assertEqual(cast(dict[str, object], starts[0]["params"])["sandbox"], "read-only")
        self.assertTrue((self.session_dir / "phases/7/project-review_status_rerun_1").exists())

    def test_recorder_ad_hoc_phase_dispatches_into_adhoc_directory(self) -> None:
        self.record("start-phase", "--phase-id", "ad hoc", "--phase-title", "Ad hoc review")
        self.write_phase_prompt("ad hoc", "Ad hoc review")
        _ = self.successful("review", "--phase", "ad hoc")
        phase = self.session_dir / "phases/adhoc"
        self.assertEqual((phase / "project-review_status_1").read_text(), "done\n")
        self.assertTrue((phase / "dispatch_1_prompt.txt").is_file())
        self.assertTrue((phase / "project-review_summary_1.txt").read_text().strip())
        self.assertFalse((self.session_dir / "phases/ad hoc").exists())
        self.assertEqual(cast(dict[str, object], self.methods("thread/start")[0]["params"])["sandbox"], "read-only")
        self.assertEqual(len(self.methods("turn/start")), 1)

    def test_unsafe_phase_ids_are_refused_before_opening_a_thread(self) -> None:
        for phase in (".", "..", "../escape", "nested/phase", "other phase"):
            with self.subTest(phase=phase):
                result = self.launch("review", "--phase", phase)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("invalid phase id:", result.stderr)
        self.assertEqual(self.methods("thread/start"), [])

    def test_missing_required_options_and_positionals_refuse_before_opening_a_thread(self) -> None:
        for options in ([], ["--prompt", str(self.prompt)], ["--role", "test"], ["test", str(self.prompt)]):
            with self.subTest(options=options):
                result = self.run_launcher(["bash", str(ASSIGN), *options])
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("usage", (result.stdout + result.stderr).lower())
        self.assertEqual(self.methods("thread/start"), [])

    def test_future_roles_are_refused_before_any_thread_is_opened(self) -> None:
        for role in ("research", "propose"):
            with self.subTest(role=role):
                result = self.launch(role)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.methods("thread/start"), [])

    def test_writer_paths_are_required_and_review_paths_are_refused(self) -> None:
        for role in ("implement", "test", "fix"):
            with self.subTest(role=role):
                self.assertEqual(self.launch(role, paths=False).returncode, 2)
        self.assertEqual(self.launch("review", "--paths", str(self.paths)).returncode, 2)
        self.assertEqual(self.methods("thread/start"), [])

    def test_unsafe_empty_and_escaping_paths_refuse_before_claim(self) -> None:
        outside = self.project.parent / "outside"
        outside.mkdir()
        (self.project / "linked").symlink_to(outside, target_is_directory=True)
        for path in ("", "../secret", "/tmp/secret", "src/../secret", "linked/secret"):
            with self.subTest(path=path):
                _ = self.paths.write_text(path + "\n")
                result = self.launch()
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                if path:
                    self.assertIn(path, result.stdout + result.stderr)
        self.assertEqual(self.methods("thread/start"), [])

    def test_writer_dispatch_records_paths_rules_identity_and_a_finished_pass(self) -> None:
        _ = self.successful(execution=LauncherScript(ASSIGN))
        name, record = next(iter(self.assignment_records().items()))
        self.assertEqual(record["role"], "impl")
        self.assertEqual(self.turn_starts, 1)
        prompt = self.turn_inputs[0]
        self.assertIn(self.prompt.read_text().strip(), prompt)
        for text in ("mesh_runs.py start", "Simple, Fast and Beautiful", "poll"):
            self.assertIn(text, prompt)
        manifest = self.session_dir / "phases/7/dispatch_1_paths.json"
        self.assertEqual(read_object(manifest), {"worker": name, "round": 1,
                                               "paths": ["src/worker.py", "tests/test_worker.py"]})
        self.assertEqual((manifest.parent / "impl_status").read_text().strip(), "done")
        self.assertTrue((manifest.parent / "impl_summary.txt").is_file())
        self.assertTrue((manifest.parent / "impl_agent.log").is_file())
        finished = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual(len(finished), 1, self.events())
        self.assertTrue(self.claims)
        launcher_pid = self.claims[0].get("launcher_pid")
        self.assertIsInstance(launcher_pid, int)
        self.assertTrue(self.reservations, "reservation must survive until can-follow probes the server")
        self.assertEqual(self.reservations[-1].get("allocation_owner_pid"), launcher_pid)

    def test_review_is_read_only_and_thread_board_and_summary_use_the_shown_context(self) -> None:
        _ = self.successful("review", "--lens", "contract", "--pass", "1")
        name = next(iter(self.assignment_records()))
        starts = self.methods("thread/start")
        self.assertEqual(cast(dict[str, object], starts[0]["params"])["sandbox"], "read-only")
        names = [str(cast(dict[str, object], request["params"])["name"])
                 for request in self.methods("thread/name/set")]
        self.assertTrue(names)
        for shown in names:
            self.assertIn("fixture-production · fixture ·", shown)
            self.assertNotIn(f"({name})", shown)
        board = (self.session_dir / "board.log").read_text()
        self.assertIn(names[-1], board)
        self.assertTrue((self.session_dir / "phases/7/project-review_status_contract_1").exists())

    def test_style_assignment_is_prepared_and_captured_through_assign_on_retry(self) -> None:
        self.prepare_style_project()
        _ = (self.project / "new.rs").write_text("fn first() {}\n")
        original = self.server.respond
        outcomes = ["Error", "Findings"]
        prepared: list[Path] = []

        @self.hang_guard.callback
        def style_reply(request: dict[str, object]) -> list[dict[str, object]] | None:
            frames = original(request)
            if request.get("method") != "turn/start" or frames is None:
                return frames
            params = cast(dict[str, object], request["params"])
            inputs = cast(list[dict[str, object]], params["input"])
            prompt = str(inputs[0]["text"])
            self.assertEqual(prompt.count("## Phase style review"), 1)
            self.assertNotIn("write your summary to", prompt)
            diff_line = next(line for line in prompt.splitlines() if line.startswith("- Diff "))
            diff = Path(diff_line.split(": ", 1)[1])
            self.assertIn("+fn ", diff.read_text())
            prepared.append(diff)
            report = ("1. minor: fixture finding at new.rs:1\n" + style_review.BEGIN +
                      "\n| # | Rule | Result |\n| 1 | Fixture | Pass |\nStyle review: " +
                      outcomes.pop(0) + "\n" + style_review.END)
            frames.insert(-1, {"method": "item/completed", "params": {
                "threadId": params["threadId"], "item": {"type": "agentMessage", "text": report}}})
            return frames

        self.server.respond = style_reply
        phase = self.session_dir / "phases/7"
        _ = self.successful("review", "--lens", "contract", "--style-since", "HEAD")
        self.assertEqual(read_object(phase / "style_review_state.json")["state"], "owed")
        name, record = next(iter(self.assignment_records().items()))
        before = prepared[0].read_bytes()
        _ = (self.project / "new.rs").write_text("fn second() {}\n")
        _ = self.successful("review", "--to", name, "--pass", "2", "--style-since", "HEAD")
        self.assertEqual(read_object(phase / "style_review_state.json")["state"], "complete")
        self.assertIn("Style review: Findings", (phase / "phase_style_review.md").read_text())
        self.assertIn("fixture finding", (phase / "project-review_summary_2.txt").read_text())
        self.assertNotEqual(prepared[0].parent, prepared[1].parent)
        self.assertEqual(prepared[0].read_bytes(), before)
        self.assertIn("+fn second", prepared[1].read_text())
        self.assertEqual(self.assignment_records()[name]["thread_id"], record["thread_id"])
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(cast(dict[str, object], self.methods("turn/start")[-1]["params"])["sandboxPolicy"],
                         {"type": "readOnly", "networkAccess": False})

    def test_style_preparation_failure_through_assign_keeps_code_review_running(self) -> None:
        self.prepare_style_project()
        _ = self.successful("review", "--style-since", "missing-baseline")
        phase = self.session_dir / "phases/7"
        self.assertEqual(read_object(phase / "style_review_state.json")["state"], "owed")
        self.assertIn("invalid baseline", (phase / "phase_style_review.md").read_text())
        self.assertEqual((phase / "project-review_status_1").read_text().strip(), "done")
        self.assertEqual(len(self.methods("turn/start")), 1)

    def test_assign_style_range_excludes_other_work_in_flight(self) -> None:
        self.prepare_style_project()
        baseline = style_review.git(self.project, "rev-parse", "HEAD").stdout.decode().strip()
        _ = (self.project / "included.txt").write_text("pinned addition\n")
        self.commit_project("pinned range")
        end = style_review.git(self.project, "rev-parse", "HEAD").stdout.decode().strip()
        _ = (self.project / "included.txt").write_text("outside the range\n")
        _ = (self.project / "untracked.rs").write_text("fn outside() {}\n")
        _ = self.successful("review", "--style-since", baseline, "--style-until", end)
        phase = self.session_dir / "phases/7"
        state = read_object(phase / "style_review_state.json")
        directory = Path(str(state["input_directory"]))
        self.assertEqual(state["baseline"], baseline)
        self.assertEqual(state["until"], end)
        self.assertEqual((directory / "style-review-additions.txt").read_text(), "+pinned addition\n")
        self.assertIn("- Rust change: no", (directory / "prompt.md").read_text())
        self.assertIn(f"- Range: {baseline}..{end}", (directory / "prompt.md").read_text())

    def test_missing_run_calls_prepare_and_prints_the_prepared_folder(self) -> None:
        prepared = self.prepare_launcher()
        environment = os.environ.copy()
        for key in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "UNIT_RUN_DIR", "UNIT_SESSION_DIR"):
            _ = environment.pop(key, None)
        with patch.dict(os.environ, environment, clear=True):
            result = self.launch(session=False, execution=LauncherScript(ASSIGN))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(prepared.exists())
        self.assertIn(str(self.session_dir), result.stdout)
        self.assertEqual(self.turn_starts, 1)

    def test_a_fresh_run_takes_its_phase_title_from_the_dispatch_prompt(self) -> None:
        (self.session_dir / "progress_history_state.json").unlink()
        shutil.rmtree(self.history)
        shutil.rmtree(self.session_dir / "phases", ignore_errors=True)
        prepared = self.prepare_launcher()
        director = os.environ["CODEX_THREAD_ID"]
        transcript = Path.home() / ".codex/sessions" / f"rollout-{director}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        _ = transcript.write_text("\n".join(json.dumps(record) for record in (
            {"type": "session_meta", "payload": {"id": director}},
            {"type": "turn_context", "payload": {"model": "fixture-director", "effort": "high"}},
        )) + "\n")
        self.addCleanup(transcript.unlink, missing_ok=True)
        environment = os.environ.copy()
        for key in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "UNIT_RUN_DIR", "UNIT_SESSION_DIR"):
            _ = environment.pop(key, None)
        environment["CODEX_THREAD_ID"] = director
        with patch.dict(os.environ, environment, clear=True):
            result = self.launch(session=False, execution=LauncherScript(ASSIGN))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(prepared.exists())
        phase = cast(dict[str, object], read_object(self.session_dir / "progress_history_state.json")["phase"])
        self.assertEqual((phase["id"], phase["title"]), ("7", "Assignment core"))
        self.assertEqual(self.turn_starts, 1)
        self.assertTrue(any(event.get("event_type") == "pass_finished" for event in self.events()))

    def test_default_session_uses_the_callers_marker_instead_of_starting_another_run(self) -> None:
        caller = os.environ["CODEX_THREAD_ID"]
        marker = Path(os.environ["UNIT_ACTIVE_DIR"]) / caller
        marker.parent.mkdir(parents=True, exist_ok=True)
        _ = marker.write_text(str(self.session_dir))
        with patch.dict(os.environ, {"CODEX_THREAD_ID": caller}):
            result = self.launch(session=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.turn_starts, 1)
        self.assertEqual(marker.read_text(), str(self.session_dir))

    def test_fix_requires_a_cause_and_a_wrong_phase_prompt_never_claims_the_worker(self) -> None:
        refused = self.launch("fix")
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertEqual(self.turn_starts, 0)
        _ = self.successful("fix", "--fix-cause", "repair failed acceptance gate")
        self.assertTrue(any(event.get("pass_kind") == "fix" for event in self.events()))
        _ = self.prompt.write_text("Phase 6 — Another phase\nDo the wrong work.\n")
        result = self.launch()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.turn_starts, 1)

    def test_a_failed_worker_records_an_error_pass_and_returns_worker_failure(self) -> None:
        self.outcomes = ["failed"]
        result = self.launch()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual((self.session_dir / "phases/7/impl_status").read_text().strip(), "error")
        ended = [event for event in self.events() if event.get("event_type") == "pass_finished"]
        self.assertEqual(len(ended), 1, self.events())
        self.assertEqual(ended[0].get("status"), "error")
        self.assertNotIn("launcher_pid", next(iter(self.assignment_records().values())))
