"""Check worker reuse, turn settings, and compaction."""
from __future__ import annotations

from scripts.lib.test_models_registry import write_registry
from scripts.lint.memory_test_support import write_config_fixture
import os
from pathlib import Path
from unittest.mock import patch
import json
from typing import cast, final
from scripts.agents import codex_mesh, test_codex_mesh as mesh_fixture
from scripts.direct.assign_launcher_test_support import read_object
from scripts.direct import assign_launcher_test_support as launcher_fixture


setUpModule = mesh_fixture.setUpModule


@final
class AssignReuseTests(launcher_fixture.AssignLauncherFixture):
    def use_fixture_server_start(self) -> None:
        directory = self.session_dir.parent / "restart-server"
        directory.mkdir()
        _ = (directory / "sitecustomize.py").write_text("\n".join((
            "import json, sys",
            "from pathlib import Path",
            "def enter(frame, event, argument):",
            "    if event != 'call' or frame.f_code.co_name != 'main' or not frame.f_code.co_filename.endswith('/agents/codex_mesh.py'): return",
            "    sys.setprofile(None)",
            "    mesh = sys.modules['__main__']",
            "    def start_server(session_dir, workers_home, *extra):",
            f"        Path(session_dir, mesh.SERVER_FILE).write_text(json.dumps({{'pid': {os.getpid()}, 'port': {self.server.port}, 'codex_home': str(workers_home), 'auto_compact_tokens': mesh.AUTO_COMPACT_TOKENS, 'dev_shell_worktree': {str(self.project)!r}}}))",
            f"        return {self.server.port}",
            "    mesh._start_server = start_server",
            "    mesh._start_watcher = lambda *args: mesh.ExistingServerWatcher()",
            "sys.setprofile(enter)",
        )) + "\n")
        _ = cast(object, self.patches.enter_context(patch.dict(os.environ, {
            "PYTHONPATH": os.pathsep.join((str(directory), str(launcher_fixture.ROOT))),
        })))

    def test_a_dead_launcher_and_server_recover_the_same_thread_for_named_dispatch(self) -> None:
        _ = self.successful()
        name, original = next(iter(self.assignment_records().items()))
        original.update({"status": "running", "launcher_pid": 99999999, "turn_id": "lost-turn"})
        self.write_pool_roster({name: original})
        respond = self.server.respond
        self.server.close()
        (self.session_dir / codex_mesh.SERVER_FILE).unlink()
        self.server = mesh_fixture.StubAppServer(respond)
        self.server.hang_guard = self.hang_guard
        self.use_fixture_server_start()
        _ = self.successful("implement", "--to", name)
        self.assertEqual(self.assignment_records()[name]["thread_id"], original["thread_id"])
        self.assertEqual(self.methods("thread/start"), [])
        resumed = [cast(dict[str, object], request["params"])["threadId"]
                   for request in self.methods("thread/resume")]
        self.assertTrue(resumed)
        self.assertEqual(set(resumed), {original["thread_id"]})
        self.assertEqual(cast(dict[str, object], self.methods("turn/start")[-1]["params"])["threadId"],
                         original["thread_id"])
        self.assertEqual(self.turn_starts, 2)

    def test_an_ended_worker_dispatches_again_on_its_recorded_thread(self) -> None:
        _ = self.successful()
        name, original = next(iter(self.assignment_records().items()))
        code, output, errors = self.run_mesh(["end", "--to", name])
        self.assertEqual(code, 0, output + errors)
        self.assertEqual(self.assignment_records()[name]["status"], "ended")
        self.use_fixture_server_start()
        _ = self.successful("implement", "--to", name)
        self.assertEqual(self.assignment_records()[name]["thread_id"], original["thread_id"])
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(cast(dict[str, object], self.methods("turn/start")[-1]["params"])["threadId"],
                         original["thread_id"])
        self.assertEqual(self.turn_starts, 2)

    def assert_reopened_dispatch_keeps_a_thread_when_resume_fails(self, status: str) -> None:
        _ = self.successful()
        name, original = next(iter(self.assignment_records().items()))
        original.update({"status": status, "launcher_pid": 99999999})
        self.write_pool_roster({name: original})
        respond = self.server.respond

        def refuse_resume(request: dict[str, object]) -> list[dict[str, object]] | None:
            if request.get("method") == "thread/loaded/list":
                return [{"id": request["id"], "result": {"data": [original["thread_id"]], "nextCursor": None}}]
            if request.get("method") == "thread/resume":
                return [{"id": request["id"], "error": {"message": "no rollout found for thread id " + str(original["thread_id"])}}]
            return respond(request)

        self.server.respond = refuse_resume
        result = self.launch("implement", "--to", name,
                             execution=launcher_fixture.LauncherScript(launcher_fixture.ASSIGN))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no rollout found", result.stderr)
        self.assertTrue(self.methods("thread/resume"))
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(self.turn_starts, 1)
        self.assertEqual(self.assignment_records()[name]["thread_id"], original["thread_id"])

    def test_a_dead_launchers_reopened_dispatch_refuses_a_missing_thread_without_replacement(self) -> None:
        self.assert_reopened_dispatch_keeps_a_thread_when_resume_fails("running")

    def test_an_ended_workers_reopened_dispatch_refuses_a_missing_thread_without_replacement(self) -> None:
        self.assert_reopened_dispatch_keeps_a_thread_when_resume_fails("ended")

    def test_named_dispatch_reuses_a_thread_and_advances_the_manifest_number(self) -> None:
        _ = self.successful()
        name, original = next(iter(self.assignment_records().items()))
        _ = self.successful("test", "--to", name)
        self.assertEqual(self.assignment_records()[name]["thread_id"], original["thread_id"])
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(self.turn_starts, 2)
        self.assertEqual(read_object(self.session_dir / "phases/7/dispatch_2_paths.json")["worker"], name)

    def test_explicit_turn_settings_reuse_the_named_worker_without_rewriting_its_identity(self) -> None:
        _ = self.successful()
        name, original = next(iter(self.assignment_records().items()))
        identity_path = self.identity_path(name)
        identity = identity_path.read_bytes()
        for options, expected in ((["--model", "gpt-other-sol:high"], ("gpt-other-sol", "high")),
                                  (["--effort", "max"], ("gpt-kept-sol", "max"))):
            with self.subTest(options=options):
                _ = self.successful("implement", "--to", name, *options)
                turn = cast(dict[str, object], self.methods("turn/start")[-1]["params"])
                self.assertEqual((turn["model"], turn["effort"]), expected)
                record = self.assignment_records()[name]
                self.assertEqual((record["turn_model"], record["turn_effort"]), expected)
                self.assertEqual(record["thread_id"], original["thread_id"])
                self.assertEqual(identity_path.read_bytes(), identity)
        self.assertEqual(self.turn_starts, 3)
        self.assertEqual(len(self.methods("thread/start")), 1)

    def test_turn_display_fields_never_choose_the_next_named_dispatch_model_or_effort(self) -> None:
        _ = self.successful("implement")
        roster = self.assignment_records()
        name = next(iter(roster))
        roster[name].update({"turn_model": "gpt-other-sol", "turn_effort": "max"})
        self.write_pool_roster(dict[str, object](roster))
        _ = self.successful("implement", "--to", name)
        turn = cast("dict[str, object]", self.methods("turn/start")[-1]["params"])
        self.assertEqual((turn["model"], turn["effort"]), ("gpt-kept-sol", "high"))

    def test_a_model_override_reuses_an_idle_worker_and_updates_only_its_turn_display(self) -> None:
        _ = self.successful()
        name, before = next(iter(self.assignment_records().items()))
        _ = self.successful("implement", "--model", "gpt-other-sol:high")
        roster = self.assignment_records()
        self.assertEqual(set(roster), {name})
        self.assertEqual(roster[name]["thread_id"], before["thread_id"])
        self.assertEqual((roster[name]["turn_model"], roster[name]["turn_effort"]), ("gpt-other-sol", "high"))
        turn = cast(dict[str, object], self.methods("turn/start")[-1]["params"])
        self.assertEqual((turn["model"], turn["effort"]), ("gpt-other-sol", "high"))
        self.assertEqual(len(self.methods("thread/start")), 1)

    def test_a_saved_identity_model_is_history_while_the_turn_uses_the_registry(self) -> None:
        _ = self.successful()
        name = next(iter(self.assignment_records()))
        identity_path = self.identity_path(name)
        identity = read_object(identity_path)
        identity["model"] = "gpt-other-sol"
        _ = identity_path.write_text(json.dumps(identity))
        _ = self.successful("implement", "--to", name)
        turn = cast(dict[str, object], self.methods("turn/start")[-1]["params"])
        self.assertEqual(turn["model"], "gpt-kept-sol")
        self.assertEqual(read_object(identity_path)["model"], "gpt-other-sol")
        self.assertEqual(self.turn_starts, 2)

    def test_a_legacy_roster_without_a_model_uses_registry_changes_on_the_same_thread(self) -> None:
        _ = self.successful()
        name, record = next(iter(self.assignment_records().items()))
        original_thread = record["thread_id"]
        _ = record.pop("model", None)
        self.write_pool_roster({name: record})
        _ = write_registry(self.registry, {"direct.codex": {"impl": "gpt-other-sol:high"}}, update=True)
        _ = self.successful("implement", "--to", name)
        turn = cast(dict[str, object], self.methods("turn/start")[-1]["params"])
        self.assertEqual(turn["model"], "gpt-other-sol")
        self.assertEqual(self.assignment_records()[name]["thread_id"], original_thread)
        self.assertEqual(len(self.methods("thread/start")), 1)
        _ = self.successful("implement", "--to", name, "--model", "gpt-kept-sol")
        turn = cast(dict[str, object], self.methods("turn/start")[-1]["params"])
        self.assertEqual(turn["model"], "gpt-kept-sol")
        self.assertEqual(self.turn_starts, 3)

    def test_a_large_named_worker_is_compacted_before_its_next_assignment(self) -> None:
        _ = self.successful()
        name, record = next(iter(self.assignment_records().items()))
        record["context_tokens"] = 200_000
        self.write_pool_roster({name: record})
        self.context_sizes = [200_000]
        _ = self.successful("implement", "--to", name)
        methods = [request.get("method") for request in self.server.requests]
        self.assertEqual(methods.count("thread/compact/start"), 1, methods)
        self.assertLess(methods.index("thread/compact/start"),
                        max(index for index, method in enumerate(methods) if method == "turn/start"))
        self.assertEqual(self.turn_starts, 2)

    def test_changed_compaction_config_takes_effect_on_the_next_assignment(self) -> None:
        config = self.session_dir.parent / "direct.conf"
        _ = write_config_fixture(config, "UNIT_COMPACT_ABOVE_TOKENS=300000 # keep this context\n")
        with patch.dict(os.environ, {"CONF_DIR": str(Path(config).parent)}):
            _ = self.successful()
            name, record = next(iter(self.assignment_records().items()))
            thread = record["thread_id"]
            record["context_tokens"] = 200_000
            self.write_pool_roster({name: record})
            self.context_sizes = [200_000, 200_000]
            _ = self.successful("implement", "--to", name)
            self.assertEqual(len(self.methods("thread/compact/start")), 0)
            _ = write_config_fixture(config, "UNIT_COMPACT_ABOVE_TOKENS=100000 # compact before the next turn\n")
            _ = self.successful("implement", "--to", name)
        self.assertEqual(len(self.methods("thread/compact/start")), 1)
        self.assertEqual(self.assignment_records()[name]["thread_id"], thread)
        self.assertEqual(len(self.methods("thread/start")), 1)
        self.assertEqual(self.turn_starts, 3)

    def test_a_compaction_roster_conflict_refuses_assignment_before_claim_or_follow(self) -> None:
        _ = self.successful()
        name, record = next(iter(self.assignment_records().items()))
        record["context_tokens"] = 126299
        roster_path = self.session_dir / codex_mesh.ROSTER_FILE
        self.write_pool_roster({name: record})
        before = roster_path.read_bytes()
        turns_before = self.turn_starts
        trace = self.session_dir / "compact-refusal.jsonl"
        # Only the transport's compaction result is injected. The real coordinator
        # decides whether to claim, follow, alter artifacts or announce continuation.
        self.instrument_launcher(
            "import json, subprocess\nfrom pathlib import Path\nfrom scripts.direct import assign\n" +
            f"trace = Path({str(trace)!r})\nreal_call = assign.Dispatch.mesh_call\n" +
            "def mesh_call(dispatch, verb, *options, **kwargs):\n" +
            "    with trace.open('a') as handle: handle.write(json.dumps(verb) + '\\n')\n" +
            "    if verb == 'compact':\n" +
            "        return subprocess.CompletedProcess(['compact'], 4, '', " +
            "'codex_mesh: compacted, but the roster changed during compaction (status=paused)\\n')\n" +
            "    return real_call(dispatch, verb, *options, **kwargs)\n" +
            "assign.Dispatch.mesh_call = mesh_call\n")
        result = self.launch("implement", "--to", name,
                             execution=launcher_fixture.LauncherScript(launcher_fixture.ASSIGN))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("roster changed during compaction", result.stderr)
        self.assertIn("reopen --to " + name, result.stderr)
        self.assertEqual([json.loads(line) for line in trace.read_text().splitlines()], ["compact"])
        self.assertEqual(roster_path.read_bytes(), before)
        self.assertEqual(self.turn_starts, turns_before)
        self.assertFalse(list(self.session_dir.glob("impl_claim_*.json")))
        board = (self.session_dir / "board.log").read_text()
        self.assertIn("follow-up not sent", board)
        self.assertNotIn("continuing follow-up", board)
