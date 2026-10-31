"""A rename retains its director terminal and resumes only unfinished work."""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar, cast
from unittest import mock

from scripts.production.rename_unit_test_support import RetainedDirector, UnitRenameFixture
from scripts.production.rename_rerun_test_support import saved_fixture
from scripts.production import staff
from scripts.shutdown import stop_resume
import herdr
import pane_record
import rename_unit


class RetainedRenameTests(UnitRenameFixture):
    retained_by_default: ClassVar[bool] = False
    def assert_retained(self, director: RetainedDirector) -> None:
        rows = herdr.Herdr("agents").panes()
        self.assertEqual(tuple((p["pane_id"], p.get("terminal_id"), p.get("tab_id")) for p in rows), director.ids)
        self.assertEqual((rows[0].get("label"), rows[0]["session_id"], director.tab_title),
                         ("gamma", "sid-alpha", "gamma"))
        calls = [cast(list[str], json.loads(line)) for line in (self.root / "herdr.jsonl").read_text().splitlines()]
        self.assertEqual([c for c in calls if c[2:4] in (["pane", "close"], ["tab", "close"],
            ["pane", "move"], ["tab", "new"], ["pane", "split"], ["tab", "order"], ["agent", "start"])], [])
        self.assertEqual(director.orders, [])
        self.assert_ok(self.rename("verify"))

    def test_apply_resumes_the_same_terminal_with_mapped_launch_and_context(self) -> None:
        director = RetainedDirector(self)
        original = pane_record.read("agents")["w1:p1"]
        with mock.patch.object(stop_resume, "resume", wraps=stop_resume.resume) as resumed:
            applied = self.rename("apply")
        self.assertEqual(director.pane.close_calls(), [])
        self.assert_ok(applied)
        resumed.assert_called_once()
        self.assert_retained(director)
        entry = pane_record.read("agents")["w1:p1"]
        self.assertEqual((entry["cwd"], entry["name"], entry["environment"]["SHOWRUNNER_UNIT_ID"]),
                         (str(self.home / "worktrees/gamma/sub"), "gamma", "gamma-unit"))
        for field in ("config_dir", "session_id", "model", "remote_control"):
            self.assertEqual(entry[field], original[field])
        self.assertEqual(entry["environment"]["STAFF_ID"], original["environment"]["STAFF_ID"])
        self.assertEqual(entry["environment"]["SHOWRUNNER_UNIT"], "demo")
        self.assertEqual(len(director.starts), 1)
        argv = director.starts[0]
        self.assertIn(str(self.home / "worktrees/gamma/sub"), argv)
        self.assertIn("SHOWRUNNER_UNIT_ID=gamma-unit", argv)
        self.assertIn("STAFF_ID=" + original["environment"]["STAFF_ID"], argv)
        self.assertEqual(argv[argv.index("exec") + 1], "claude4")
        self.assertEqual(argv[argv.index("--resume") + 1], "sid-alpha")
        self.assertIn("you are now gamma-unit", argv[-1])
        self.assertIn(str(self.home / "worktrees/gamma"), argv[-1])

    def test_verify_refuses_replaced_pane_terminal_tab_or_conversation(self) -> None:
        director = RetainedDirector(self)
        applied = self.rename("apply")
        self.assertEqual(director.pane.close_calls(), [])
        self.assert_ok(applied)
        original = director.ids
        for index, replacement in enumerate(("w1:p9", "replacement-terminal", "w1:t9")):
            with self.subTest(identity=index):
                changed = list(original[0])
                changed[index] = replacement
                director.ids = ((changed[0], changed[1], changed[2]), original[1])
                director.publish()
                self.assert_refused(self.rename("verify"), ("journaled terminal moved to pane w1:p9",
                    "journaled conversation is on another terminal",
                    "journaled pane moved to another tab or has no tab identity")[index])
                director.ids = original
        import unit_lookup
        director.pane.found = unit_lookup.MarkedUnit("gamma-unit",
            unit_lookup.LiveClaude("gamma", "sid-replacement", "/fixture/socket", director.pane.director), director.pane.place)
        director.publish()
        self.assert_refused(self.rename("verify"), "conversation")

    def test_missing_pane_refuses_before_plan_or_apply_changes_sources(self) -> None:
        responses_path = Path(self.env["FAKE_HERDR_RESPONSES"])
        responses = cast(dict[str, object], json.loads(responses_path.read_text()))
        responses["--session agents pane list"] = {"code": 0, "stdout": json.dumps({"result": {"panes": []}})}
        self.write(responses_path, json.dumps(responses))
        before = self.snapshot()
        for action in ("plan", "apply"):
            with self.subTest(action=action):
                result = self.rename(action)
                self.assert_refused(result, "alpha-unit: retained pane is absent; a legacy rename may have closed it")
                self.assertEqual(sorted(row.name for row in staff.find(kind="unit")), ["alpha", "mul_add"])
                self.assertEqual(self.doc.read_bytes(), before[str(self.doc.relative_to(self.root))])
                self.assertTrue((self.rust / "claude-demo-alpha/.git").exists())

    def test_unknown_launch_flags_refuse_before_plan_or_apply_writes(self) -> None:
        _ = RetainedDirector(self)
        entry = pane_record.read("agents")["w1:p1"]
        entry["flags_read"] = False
        pane_record.write("agents", entry)
        before = self.snapshot()
        for action in ("plan", "apply"):
            with self.subTest(action=action):
                self.assert_refused(self.rename(action), "alpha-unit: pane launch evidence is incomplete; readable launch flags are required")
                self.assertEqual(self.snapshot(), before)

    def test_closed_terminal_from_an_old_journal_requires_explicit_recovery(self) -> None:
        director = RetainedDirector(self)
        save = rename_unit._save  # pyright: ignore[reportPrivateUsage]
        def interrupt(path: Path, renamed: rename_unit.UnitRename) -> None:
            save(path, renamed)
            if renamed.step == 1:
                raise OSError("stop boundary")
        with mock.patch.object(rename_unit, "_save", side_effect=interrupt):
            self.assert_refused(self.rename("apply"), "stop boundary")
        director.ids = director.ids[1:]
        director.publish()
        before = self.snapshot()
        result = self.rename("apply")
        self.assert_refused(result, "alpha-unit: retained pane is absent; a legacy rename may have closed it")
        self.assertIn("Run:", result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(director.starts, [])

    def test_second_apply_changes_no_file_or_claude_generation(self) -> None:
        director = RetainedDirector(self)
        applied = self.rename("apply")
        self.assertEqual(director.pane.close_calls(), [])
        self.assert_ok(applied)
        before = self.snapshot()
        calls = (list(director.stops), list(director.starts), director.generation)
        result = self.rename("apply")
        self.assert_ok(result)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertIn("unchanged", result.stdout)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((director.stops, director.starts, director.generation), calls)
        self.assert_retained(director)

    def test_exit_around_relaunch_progress_save_resumes_in_the_same_terminal(self) -> None:
        director = RetainedDirector(self)
        save = rename_unit._save  # pyright: ignore[reportPrivateUsage]
        relaunch_step = rename_unit.step_number(rename_unit._relaunch)  # pyright: ignore[reportPrivateUsage]
        with saved_fixture(self.root) as restore:
            for saved in (False, True):
                with self.subTest(progress_saved=saved):
                    restore()
                    director.reset()
                    original_ids = director.ids
                    def exited(path: Path, renamed: rename_unit.UnitRename) -> None:
                        if renamed.step == relaunch_step:
                            if saved:
                                save(path, renamed)
                            director.pane.stopped()
                            director.publish()
                            raise OSError("Claude exited at relaunch progress save")
                        save(path, renamed)
                    with mock.patch.object(rename_unit, "_save", side_effect=exited):
                        self.assert_refused(self.rename("apply"), "Claude exited at relaunch progress save")
                    self.assertEqual(self.journal()["step"], relaunch_step if saved else relaunch_step - 1)
                    self.assertEqual(len(director.starts), 1)
                    generation = director.generation
                    self.assert_ok(self.rename("apply"))
                    self.assertEqual(director.ids, original_ids)
                    self.assertEqual(director.pane.close_calls(), [])
                    self.assertEqual(len(director.starts), 2)
                    self.assertEqual(director.generation, generation + 1)
                    self.assertEqual(director.stops, ["sid-alpha"])
                    self.assertIs(self.journal()["done"], True)
                    self.assert_retained(director)

    def test_each_step_boundary_retries_without_replacing_the_terminal(self) -> None:
        director = RetainedDirector(self)
        save = rename_unit._save  # pyright: ignore[reportPrivateUsage]
        with saved_fixture(self.root) as restore:
            for boundary in range(len(rename_unit.STEPS) + 1):
                with self.subTest(boundary=boundary):
                    restore()
                    director.reset()
                    fired = False
                    def interrupt(path: Path, renamed: rename_unit.UnitRename) -> None:
                        nonlocal fired
                        save(path, renamed)
                        if renamed.step == boundary and not fired:
                            fired = True
                            raise OSError("step boundary")
                    with mock.patch.object(rename_unit, "_save", side_effect=interrupt):
                        self.assert_refused(self.rename("apply"), "step boundary")
                    applied = self.rename("apply")
                    self.assertEqual(director.pane.close_calls(), [])
                    self.assert_ok(applied)
                    self.assert_retained(director)
                    self.assertEqual(len(director.starts), 1)
                    self.assertEqual(director.stops, ["sid-alpha"])
                    timings = cast(dict[str, dict[str, str]], self.journal()["step_times"])
                    self.assertEqual(set(timings), {str(n) for n in range(1, len(rename_unit.STEPS) + 1)})
                    for value in timings.values():
                        start = datetime.fromisoformat(value["started_at"])
                        end = datetime.fromisoformat(value["ended_at"])
                        self.assertEqual((start.utcoffset(), end.utcoffset()), (timezone.utc.utcoffset(start), timezone.utc.utcoffset(end)))
                        self.assertLessEqual(start, end)

    def test_each_preparation_side_effect_and_successful_resume_retries(self) -> None:
        director = RetainedDirector(self)
        write = pane_record.write
        run = herdr.Herdr.run
        resume = cast(Callable[..., stop_resume.ClaudeUp | stop_resume.ResumeFailed | stop_resume.SessionNotFound], stop_resume.resume)
        with saved_fixture(self.root) as restore:
            for effect in ("record", "pane", "tab", "resume"):
                with self.subTest(effect=effect):
                    restore()
                    director.reset()
                    fired = False
                    def fail() -> None:
                        nonlocal fired
                        if not fired:
                            fired = True
                            raise OSError("after " + effect)
                    def record(session: str, entry: pane_record.PaneLaunch) -> None:
                        write(session, entry)
                        if effect == "record" and entry["name"] == "gamma":
                            fail()
                    def command(client: herdr.Herdr, *arguments: str) -> str:
                        result = run(client, *arguments)
                        if arguments[:2] == (effect, "rename"):
                            fail()
                        return result
                    def resumed(*args: object, **kwargs: object) -> object:
                        result = resume(*args, **kwargs)
                        if effect == "resume" and isinstance(result, stop_resume.ClaudeUp):
                            fail()
                        return result
                    with (mock.patch.object(pane_record, "write", side_effect=record),
                          mock.patch.object(herdr.Herdr, "run", command),
                          mock.patch.object(stop_resume, "resume", side_effect=resumed)):
                        self.assert_refused(self.rename("apply"), "after " + effect)
                    self.assertTrue(fired)
                    applied = self.rename("apply")
                    self.assertEqual(director.pane.close_calls(), [])
                    self.assert_ok(applied)
                    self.assert_retained(director)
                    self.assertEqual(len(director.starts), 1)
                    self.assertEqual(director.stops, ["sid-alpha"])
