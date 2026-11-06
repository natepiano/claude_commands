"""Exercise add_unit through its command line with isolated git and launcher state."""

from __future__ import annotations

from scripts.production.staff_test_support import unit_membership, fixture_member, ClaudeBackendFixture, StaffTestCase, fixture_runtime_id

from scripts.lib import names
from scripts.lib.test_names import fixture_names

import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, tzinfo
from pathlib import Path
from typing import cast, final
from unittest.mock import call, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shutdown"))
from scripts.production import add_unit
from scripts.production import unit_lookup
from scripts.message import sessions
from scripts.herdr.herdr import HerdrPlace
from scripts.herdr import herdr
from scripts.herdr.fake_herdr import CommandResponse


from scripts.production.add_unit_test_support import AddUnitTestCase, configure_test_zone, SCRIPT, SHARED, RemoteControlDeadlineClock


LAUNCHED_AT = datetime(2026, 10, 6, 12, tzinfo=ZoneInfo("America/Los_Angeles"))


def setUpModule() -> None:
    configure_test_zone()


from scripts.production import staff, unit_config
from scripts.production.production_doc import Production, read_production

class NativeClaudeUnitTests(StaffTestCase):
    def test_resume_uses_the_renamed_native_owner_for_arguments_and_tab(self) -> None:
        document = self.production("demo")
        _ = document.write_text(document.read_text() + "\n- **Merge branch:** `main`\n"
                                + f"- **Showrunner checkout:** `{self.root}`\n- **Log:** `demo-log.md`\n")
        runner = fixture_member("showrunner", name="demo", doc=staff.LiveDoc("natedev", document))
        owner = fixture_member("unit", name="saved-name", parent=runner.id)
        current = staff.native_id(fixture_runtime_id("renamed-unit-current"))
        _ = staff.update_current_session(owner.current_session, current)
        _ = staff.rename(owner.id, "row-name")
        _ = fixture_member("unit", name="saved-name", parent=runner.id, runtime_id=fixture_runtime_id("decoy"))
        request = add_unit.UnitLaunch(
            production=read_production(runner.name), branch="kept", worktree=self.root,
            plan=add_unit.PlanGiven(Path("plan.md")), port=add_unit.OmittedCell(),
            session=add_unit.ResumedSession(current, self.root), timeout=5,
            identity=add_unit.UnitIdentity("saved-name", "saved-name"))
        from scripts.accounts.accounts import NamedAccount

        with (patch.object(herdr, "start_server"), patch.object(herdr, "launch_agent", return_value=
              herdr.AgentLaunched("w1:p1", herdr.PromptSubmitted(LAUNCHED_AT))) as launch):
            _ = add_unit.launch_session(request, add_unit.DirectorAgent("fixture", add_unit.DefaultEffort()),
                "resume", NamedAccount("claude1", self.root))
        arguments = cast(list[str], launch.call_args.kwargs["agent_arguments"])
        self.assertEqual(arguments[arguments.index("-n") + 1], "row-name")
        self.assertEqual(arguments[arguments.index("--remote-control") + 1], "row-name")
        self.assertEqual(launch.call_args.args[2], "row-name")
        self.assertNotIn("STAFF_ID", cast(dict[str, str], launch.call_args.args[4]))
        self.assertFalse(any(key.startswith("SHOWRUNNER_") for key in cast(dict[str, str], launch.call_args.args[4])))

    def test_fresh_unit_uses_the_observed_claude_id_before_its_opening_prompt(self) -> None:
        document = self.production("demo")
        _ = document.write_text(document.read_text() + "\n- **Merge branch:** `main`\n"
                                + f"- **Showrunner checkout:** `{self.root}`\n- **Log:** `demo-log.md`\n")
        runner = fixture_member("showrunner", name="demo", doc=staff.LiveDoc("natedev", document))
        production = read_production(runner.name)
        folder = self.root / "fresh"
        folder.mkdir()
        identity = fixture_runtime_id("fresh-Claude")
        backend = ClaudeBackendFixture(identity, self.root / "claude-account")
        request = add_unit.UnitLaunch(
            production=production, branch="fresh", worktree=folder,
            plan=add_unit.PlanGiven(Path("plan.md")), port=add_unit.OmittedCell(),
            session=add_unit.NewSession(), timeout=5,
            identity=add_unit.UnitIdentity("fresh", "fresh"))
        from scripts.accounts.accounts import NamedAccount

        with (patch.object(herdr, "start_server"), patch.object(herdr, "launch_agent", side_effect=backend.launch)):
            outcome = add_unit.launch_session(request, add_unit.DirectorAgent("fixture", add_unit.DefaultEffort()),
                "/unit:direct plan.md", NamedAccount("claude1", backend.configuration))
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        row = backend.before_opening.by_id(staff.StaffId(identity))
        assert row is not None
        self.assertEqual((row.id, row.name, row.parent), (identity, "fresh", staff.ReportsTo(runner.id)))
        self.assertNotIn("--session-id", backend.arguments)
        self.assertNotIn("--resume", backend.arguments)
        self.assertNotIn("STAFF_ID", backend.environment)
        self.assertFalse(any(key.startswith("SHOWRUNNER_") for key in backend.environment))


@final
class AddUnitLaunchTests(AddUnitTestCase):
    def assert_observed_identity(self, row: staff.StaffRow) -> None:
        pid = int(self.env["FAKE_CLAUDE_PID"])
        registry = sessions.read_session(Path(self.env["NOTIFIER_SESSIONS_DIR"]) / f"{pid}.json")
        assert not isinstance(registry, sessions.UnreadableSessionRecord)
        self.assertEqual(registry["sessionId"], self.env["FAKE_CLAUDE_SESSION_ID"])
        with patch.dict(os.environ, self.env):
            self.assertEqual(registry.get("procStart"), sessions.process_start(pid))
        self.assertEqual((row.id, row.current_session), (registry["sessionId"], registry["sessionId"]))

    def assert_staff_folder(self, name: str, folder: Path) -> None:
        with patch.dict(os.environ, self.env):
            row = next(row for row in staff.find(kind="unit") if row.name == name)
        self.assertEqual(row.worktree, staff.PresentWorktree("natedev", folder))

    def assert_unit_config(self, folder: Path, plan: unit_config.PlanFile | unit_config.StandbyPlan,
                           port: unit_config.AppPort | None = None) -> None:
        config = unit_config.read(folder)
        assert isinstance(config, unit_config.UnitConfig)
        self.assertEqual(config.plan, plan)
        if port is not None:
            self.assertEqual(config.port, port)

    def assert_staff_units(self, *names: str) -> None:
        with patch.dict(os.environ, self.env):
            self.assertEqual([row.name for row in staff.find(kind="unit")], list(names))

    def pin_launch_clock(self) -> None:
        def now(zone: tzinfo | None = None) -> datetime:
            return LAUNCHED_AT.astimezone(zone)

        _ = self.enterContext(patch.object(add_unit, "datetime", wraps=datetime, now=now))
        # Keep the subprocess CLI check while giving it the same clock as in-process calls.
        script = self.root / "clocked_add_unit.py"
        _ = script.write_text(
            "import sys\nfrom datetime import datetime\nfrom unittest.mock import patch\n"
            + "from scripts.production import add_unit\n"
            + f"at = datetime.fromisoformat({LAUNCHED_AT.isoformat()!r})\n"
            + "with patch.object(add_unit, 'datetime', wraps=datetime) as clock:\n"
            + "    clock.now.side_effect = lambda zone: at.astimezone(zone)\n"
            + "    raise SystemExit(add_unit.main(sys.argv[1:]))\n", encoding="utf-8")
        _ = self.enterContext(patch("scripts.production.add_unit_test_support.SCRIPT", script))

    def test_real_brief_admission_can_switch_to_its_approved_plan(self) -> None:
        _ = self.successful("alpha", "--brief", "Write a full plan.", subprocess_cli=True)
        worktree = self.root / "project-alpha"
        before = unit_config.read(worktree)
        assert isinstance(before, unit_config.UnitConfig)
        assert isinstance(before.plan, unit_config.PlanFile)
        stub = self.checkout / before.plan.path
        source = stub.read_bytes()
        # End the brief's fixture session before opening the approved plan.
        (self.state / f"herdr-live-{SHARED}").unlink()
        for pid in (101, 102):
            shutil.rmtree(self.root / "proc" / str(pid))
        before_calls = len(self.herdr_calls())
        _ = self.successful("alpha", "--plan", "docs/plans/given.md", subprocess_cli=True)
        self.assertEqual(unit_config.read(worktree), unit_config.UnitConfig(
            unit_config.PlanFile(Path("docs/plans/given.md")), before.port, before.responsibilities))
        prompts = [row[-1] for row in self.herdr_calls()[before_calls:] if row[2:4] == ["agent", "prompt"]]
        self.assertEqual(prompts, ["/unit:direct docs/plans/given.md"])
        self.assert_staff_folder("alpha", self.root / "project-alpha")
        self.assertEqual(stub.read_bytes(), source)

    def test_adopting_an_existing_config_without_a_port_flag_keeps_its_recorded_port(self) -> None:
        worktree = self.root / "project-alpha"
        _ = self.git("worktree", "add", "-b", "build-followups-alpha", str(worktree), "build-followups")
        expected = unit_config.UnitConfig(unit_config.PlanFile(Path("docs/plans/given.md")),
                                         unit_config.AppPort(8123), ())
        _ = unit_config.write(worktree, expected)
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(unit_config.read(worktree), expected)
        path = worktree / ".unit.toml"
        before = path.read_bytes(), path.stat().st_mtime_ns
        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--responsibility", "nightly-review")
        self.assertEqual(unit_config.read(worktree), expected)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_an_existing_config_port_conflict_refuses_before_admission_or_launch(self) -> None:
        worktree = self.root / "project-alpha"
        _ = self.git("worktree", "add", "-b", "build-followups-alpha", str(worktree), "build-followups")
        _ = unit_config.write(worktree, unit_config.UnitConfig(
            unit_config.PlanFile(Path("docs/plans/given.md")), unit_config.AppPort(8123), ()))
        path = worktree / ".unit.toml"
        before = path.read_bytes(), path.stat().st_mtime_ns, self.staff.staff_file.read_bytes(), self.doc.read_bytes()
        result = self.cli("alpha", "--plan", "docs/plans/given.md", "--port", "8124")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(str(path), result.stdout + result.stderr)
        self.assertIn("port", result.stdout + result.stderr)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns,
                          self.staff.staff_file.read_bytes(), self.doc.read_bytes()), before)
        self.assertFalse(any(command[2:4] == ["agent", "start"] for command in self.herdr_calls()))

    def test_exclude_refusal_stops_config_publication_and_worker_launch(self) -> None:
        from scripts.lib import git_exclude

        worktree = self.root / "project-alpha"
        with patch.object(git_exclude, "ensure_excluded",
                          side_effect=git_exclude.ExcludeRefused(f"{worktree}: fixture exclude refusal")) as exclude:
            result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("fixture exclude refusal", result.stdout + result.stderr)
        exclude.assert_called_once_with(worktree, "/.unit.toml")
        self.assertFalse((worktree / ".unit.toml").exists())
        self.assertFalse(any(command[2:4] == ["agent", "start"] for command in self.herdr_calls()))

    def test_plan_launch_admits_its_real_worktree_and_writes_port_and_repeatable_responsibilities(self) -> None:
        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--port", "8123",
                            "--responsibility", "nightly", "--responsibility", "build-check",
                            "--responsibility", "nightly")
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        worktree = self.root / "project-alpha"
        self.assertEqual(row.worktree, staff.PresentWorktree("natedev", worktree))
        self.assertEqual(unit_config.read(worktree), unit_config.UnitConfig(
            unit_config.PlanFile(Path("docs/plans/given.md")), unit_config.AppPort(8123), ("nightly", "build-check")))
        ignored = subprocess.run(["git", "-C", str(worktree), "check-ignore", "-q", ".unit.toml"], check=False)
        self.assertEqual(ignored.returncode, 0)
        tracked = subprocess.run(["git", "-C", str(worktree), "ls-files", "--error-unmatch", ".unit.toml"],
                                 capture_output=True, check=False)
        self.assertNotEqual(tracked.returncode, 0)
        self.assertNotIn("STAFF_ID", self.pane_environment())
        self.assert_observed_identity(row)

    def test_explicit_paths_and_named_responsibilities_ignore_document_owns(self) -> None:
        _ = self.doc.write_text(self.doc.read_text() +
                               "## Units\n\n| Unit | Owns |\n| --- | --- |\n| alpha | ignored/path |\n")
        _ = self.successful("alpha", "--standby", "--responsibility", "nightly-review",
                            "--responsibility", "scripts/production/add_unit.py",
                            "--responsibility", "scripts/production/unit_config.py",
                            "--responsibility", "settings.json")
        config = unit_config.read(self.root / "project-alpha")
        assert isinstance(config, unit_config.UnitConfig)
        self.assertEqual(config.responsibilities, tuple(sorted((
            "nightly-review", "scripts/production/add_unit.py", "scripts/production/unit_config.py",
            "settings.json"))))

    def test_invalid_explicit_responsibilities_refuse_without_writes(self) -> None:
        for responsibility in ("", "all production scripts", "../outside"):
            with self.subTest(responsibility=responsibility):
                before = self.repository_state(), self.staff.staff_file.read_bytes(), self.doc.read_bytes()
                result = self.cli("alpha", "--brief", "Repair the scripts", "--responsibility", responsibility)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("responsibilities: invalid name or repository path", result.stderr)
                self.assertEqual((self.repository_state(), self.staff.staff_file.read_bytes(), self.doc.read_bytes()), before)
                self.assertFalse((self.root / "project-alpha").exists())
                self.assertFalse((self.checkout / "docs/plans/build-followups-alpha.md").exists())
                self.assertFalse(self.log.exists())

    def test_missing_units_table_allows_admission_without_responsibilities(self) -> None:
        before = self.doc.read_bytes()
        result = self.cli("alpha", "--standby")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("alpha", result.stdout)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertTrue((self.root / "project-alpha").exists())
        self.assertEqual(unit_config.read(self.root / "project-alpha"), unit_config.UnitConfig(
            unit_config.StandbyPlan(), unit_config.NoAppPort(), ()))

    def test_named_responsibilities_are_written_once(self) -> None:
        _ = self.successful("alpha", "--standby", "--responsibility", "nightly-review",
                            "--responsibility", "build-check", "--responsibility", "nightly-review")
        config = unit_config.read(self.root / "project-alpha")
        assert isinstance(config, unit_config.UnitConfig)
        self.assertEqual(config.responsibilities, ("build-check", "nightly-review"))

    def test_standby_launch_writes_the_standby_plan_in_its_unit_config(self) -> None:
        _ = self.successful("alpha", "--standby")
        self.assertEqual(unit_config.read(self.root / "project-alpha"), unit_config.UnitConfig(
            unit_config.StandbyPlan(), unit_config.NoAppPort(), ()))

    def test_permanent_launch_writes_the_permanent_plan_in_its_unit_config(self) -> None:
        _ = self.successful("alpha", "--permanent")
        self.assertEqual(unit_config.read(self.root / "project-alpha"), unit_config.UnitConfig(
            unit_config.PermanentPlan(), unit_config.NoAppPort(), ()))

    def test_brief_launch_writes_its_existing_relative_stub_plan_in_the_unit_config(self) -> None:
        _ = self.successful("alpha", "--brief", "Build the missing reader.")
        config = unit_config.read(self.root / "project-alpha")
        assert isinstance(config, unit_config.UnitConfig)
        self.assertIsInstance(config.plan, unit_config.PlanFile)
        assert isinstance(config.plan, unit_config.PlanFile)
        self.assertFalse(config.plan.path.is_absolute())
        self.assertTrue((self.checkout / config.plan.path).is_file())

    def test_existing_unit_name_under_another_production_requires_an_explicit_move(self) -> None:
        old_doc = self.staff.production("old", "alpha", workspace="original")
        original = fixture_member("showrunner", name="original", doc=staff.LiveDoc("natedev", old_doc))
        old = fixture_member("unit", name="alpha", parent=original.id)
        before = self.staff.staff_file.read_bytes()
        document = old_doc.read_bytes()
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                result = self.cli("alpha", "--plan", "docs/plans/given.md", subprocess_cli=True)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stderr,
                    "add_unit: alpha is under original; to move it, run: staff.py move original/alpha build-followups\n")
                self.assertEqual(staff.by_id(old.id), old)
                self.assertEqual(self.staff.staff_file.read_bytes(), before)
                self.assertEqual(old_doc.read_bytes(), document)
        self.assertEqual(self.herdr_calls(), [])

    def test_new_launch_carries_the_ensured_staff_identity(self) -> None:
        _ = self.successful("alpha", "--standby")
        rows = [row for row in staff.find(kind="unit") if row.name == "alpha"]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("STAFF_ID", self.pane_environment())
        self.assert_observed_identity(rows[0])

    def test_resumed_launch_carries_the_ensured_staff_identity(self) -> None:
        _ = self.successful("alpha", "--standby", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(self.checkout))
        rows = [row for row in staff.find(kind="unit") if row.name == "alpha"]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("STAFF_ID", self.pane_environment())
        self.assert_observed_identity(rows[0])

    def test_unit_admission_distinguishes_pending_backend_from_published_membership(self) -> None:
        admit = add_unit.admit_unit
        requests: list[add_unit.UnitLaunch] = []
        pending: list[add_unit.UnitAdmissionPending | add_unit.UnitMembershipPublished] = []

        def observe(request: add_unit.UnitLaunch, *,
                    record: staff.StaffSnapshot) -> add_unit.UnitAdmissionPending | add_unit.UnitMembershipPublished:
            result = admit(request, record=record)
            requests.append(request)
            pending.append(result)
            self.assertEqual(staff.find(kind="unit"), [])
            self.assertFalse(any(command[2:4] == ["agent", "start"] for command in self.herdr_calls()))
            return result

        with patch.object(add_unit, "admit_unit", side_effect=observe):
            _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(len(pending), 1)
        self.assertIsInstance(pending[0], add_unit.UnitAdmissionPending)
        published = admit(requests[0])
        self.assertIsInstance(published, add_unit.UnitMembershipPublished)
        assert isinstance(published, add_unit.UnitMembershipPublished)
        self.assertEqual(published.row, staff.by_current_session(self.env["FAKE_CLAUDE_SESSION_ID"]))
        self.assertEqual(published.row.name, "alpha")

    def test_add_unit_rerun(self) -> None:
        original = add_unit.ensure_staff_unit

        for after in (False, True):
            before = self.staff.staff_file.read_bytes()

            def interrupted(production: Production, unit: str, *,
                            worktree: staff.PresentWorktree | staff.WorktreeUnfilled,
                            runtime_id: str = "") -> staff.StaffRow:
                self.assertEqual(runtime_id, self.env["FAKE_CLAUDE_SESSION_ID"])
                self.assertEqual(self.staff.staff_file.read_bytes(), before)
                if after:
                    _ = original(production, unit, worktree=worktree, runtime_id=runtime_id)
                raise RuntimeError("stopped at staff step")

            with patch.object(add_unit, "ensure_staff_unit", side_effect=interrupted):
                stopped = self.cli("alpha", "--plan", "docs/plans/given.md")
            self.assertEqual(stopped.returncode, 1, stopped.stderr)
            if not after:
                self.assertEqual(self.staff.staff_file.read_bytes(), before)
                self.assertEqual(staff.find(kind="unit"), [])
            self.assertNotIn("STAFF_ID", self.pane_environment())
        starts = sum(command[2:4] == ["agent", "start"] for command in self.herdr_calls())
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        record = staff.snapshot()
        units = record.find(kind="unit")
        self.assertEqual(len(units), 1)
        self.assertEqual((units[0].name, units[0].id), ("alpha", self.env["FAKE_CLAUDE_SESSION_ID"]))
        self.assertEqual(sum(command[2:4] == ["agent", "start"] for command in self.herdr_calls()), starts)
        document, data, head = self.doc.read_bytes(), self.staff.staff_file.read_bytes(), self.git("rev-parse", "HEAD")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual((self.doc.read_bytes(), self.staff.staff_file.read_bytes(), self.git("rev-parse", "HEAD")),
                         (document, data, head))

    def test_new_row_with_a_running_labeled_session_refuses_before_any_write(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        owner = unit_membership("build-followups", "alpha")
        owner = staff.update_current_session(owner.current_session,
            staff.native_id("62ccbd64-f22e-5647-b20e-7899aa39d766"))
        owner = staff.set_worktree(owner.id, staff.PresentWorktree("natedev", prior), expected=owner.worktree)
        document = self.doc.read_bytes()
        repository = self.repository_state()
        head = self.git("rev-parse", "build-followups")
        plans = {path: path.read_bytes() for path in self.plan.parent.iterdir() if path.is_file()}
        marked = unit_lookup.LabeledSession("alpha",
            unit_lookup.LiveClaude("alpha", owner.current_session, "/fixture/socket", 4242),
            HerdrPlace("running-host", "w1:p1"), identity=unit_lookup.LiveSession(owner.current_session))
        modes = (("--plan", "docs/plans/given.md"), ("--brief", "Write the missing plan."),
                 ("--standby",), ("--permanent",))
        for mode in modes:
            launches = ((),) if mode[0] == "--brief" else (
                (), ("--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior), "--autocompact", "200k"))
            for flags in launches:
                with self.subTest(mode=mode, flags=flags):
                    arguments = ["--production", self.production_name, "alpha", *mode, *flags]
                    with (patch.object(add_unit, "launched_unit", return_value=add_unit.LabeledSessionFound(marked)),
                          patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
                          patch.object(add_unit, "launch_session") as launched,
                          patch.object(add_unit, "wait_for_remote_control", return_value=marked) as waited,
                          patch.object(herdr, "close_agent") as closed):
                        result = self.cli("alpha", *mode, *flags)

                    self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                    self.assertEqual(result.stderr,
                        "add_unit: unit alpha already runs in session running-host but has no .unit.toml; "
                        + "end that session, then run " + shlex.join(["add_unit.py", *arguments]) + "\n")
                    self.assertEqual(self.doc.read_bytes(), document)
                    self.assertEqual(self.git("rev-parse", "build-followups"), head)
                    self.assertEqual(self.repository_state(), repository)
                    self.assertEqual({path: path.read_bytes() for path in self.plan.parent.iterdir()
                                      if path.is_file()}, plans)
                    self.assertFalse((self.root / "project-alpha").exists())
                    self.assertEqual(self.git("branch", "--list", "build-followups-alpha"), "")
                    self.assertFalse((Path(self.env["HOME"]) / ".local/state/unit-launch").exists())
                    self.assertFalse(self.log.exists())
                    self.assertFalse(self.config.exists())
                    self.assertEqual(self.servers_started(), [])
                    launched.assert_not_called()
                    waited.assert_not_called()
                    closed.assert_not_called()

    def test_missing_production_doc_refuses_with_a_nonzero_exit_before_any_launch(self) -> None:
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        errors = io.StringIO()
        missing = self.checkout / "missing-production.md"
        _ = fixture_member("showrunner", name="missing", doc=staff.LiveDoc("natedev", missing))
        with patch.dict(os.environ, self.env), redirect_stderr(errors):
            code = add_unit.main(["--production", "missing", "alpha", "--permanent",
                                  "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(self.root)])
        self.assertEqual(code, 2, errors.getvalue())
        self.assertIn("cannot read production doc:", errors.getvalue())
        self.assertIn(str(missing), errors.getvalue())
        self.assert_no_launch_change(document, head)

    def test_new_work_launch_still_waits_for_remote_control(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result, error, commands, launches, _writes = self.run_with_labeled_session(
                unit_lookup.ClaudeNotRunning(), already_labeled=False
            )
        self.assertEqual(result, 0, error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 1)
        self.assertEqual(output.getvalue(), "alpha started: herdr session attach agents, workspace build-followups, tab alpha\n")

    def test_unit_start_not_ready_prints_screen_and_sends_no_prompt_or_key(self) -> None:
        screen = "Do you trust this folder?"
        self.herdr_overrides["agent start"] = CommandResponse(
            code=1, stdout=json.dumps({"error": {"code": "agent_not_ready", "message": "dialog"}}))
        self.herdr_overrides[f"--session {SHARED} pane read w1:p1 --source visible"] = CommandResponse(code=0, stdout=screen)
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("opening prompt not submitted:", result.stderr)
        self.assertIn(screen, result.stderr)
        self.assertFalse(any(row[2:4] == ["server", "stop"] for row in self.herdr_calls()))
        self.assertFalse(any(row[2:4] in (["agent", "prompt"], ["pane", "send-keys"], ["pane", "send-text"])
                             for row in self.herdr_calls()))

    def test_a_prompt_never_typed_closes_the_new_session_so_a_rerun_starts_it(self) -> None:
        for unit, typed, ending in (("alpha", False, "its session is closed: run the same command again"),
                                    ("beta", True, "prompt typed 1 times")):
            with self.subTest(typed=typed):
                place = herdr.HerdrPlace(SHARED, "w1:p1")
                found = add_unit.LabeledSessionFound(unit_lookup.LabeledSession(f"{unit}-unit",
                    unit_lookup.ClaudeNotRunning(), place,
                    identity=unit_lookup.LiveSession(fixture_runtime_id("unsubmitted:" + unit))))
                outcome = herdr.PromptNotSubmitted("destination never became ready for input"
                                                   + ("; prompt typed 1 times" if typed else ""), never_typed=not typed)
                with (patch.object(herdr, "start_agent", return_value=outcome),
                      patch.object(add_unit, "launched_unit", side_effect=[add_unit.NoLabeledSession(), found]),
                      patch.object(herdr, "close_agent") as closed):
                    result = self.cli(unit, "--plan", "docs/plans/given.md")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertTrue(result.stderr.rstrip().endswith(ending), result.stderr)
                self.assertEqual(closed.call_args_list, [] if typed else [call(SHARED, "w1:p1")])

    def test_job_name_configuration_does_not_change_tab_or_pane_session_name(self) -> None:
        with patch.object(names, "runtime_names", return_value=fixture_names(director="coordinator")):
            result = self.successful("alpha", "--plan", "docs/plans/given.md")
        calls = self.herdr_calls()
        self.assertIn(["--session", SHARED, "tab", "rename", "w1:t1", "alpha"], calls)
        self.assertIn(["--session", SHARED, "pane", "rename", "w1:p1", "alpha"], calls)
        start = next(row for row in calls if row[2:4] == ["agent", "start"])
        arguments = start[start.index("--") + 1:]
        self.assertEqual(arguments[arguments.index("-n") + 1], "alpha")
        self.assertIn("tab alpha", result.stdout)
        self.assertNotIn("tab build-followups ·", result.stdout)

    def test_unit_launch_passes_a_typed_opening_prompt_once(self) -> None:
        with patch.object(herdr, "start_agent", wraps=herdr.start_agent) as start:
            result = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 0)
        start.assert_called_once()
        opening = cast(object, start.call_args.args[4])
        self.assertIsInstance(opening, herdr.OpeningPrompt)
        assert isinstance(opening, herdr.OpeningPrompt)
        self.assertEqual(opening.text, "/unit:direct docs/plans/given.md")
        self.assert_staff_folder("alpha", self.root / "project-alpha")
        self.assertEqual(self.git("rev-parse", "build-followups-alpha"),
                         self.git("rev-parse", "build-followups"))
        self.assertEqual((self.root / "project-alpha/docs/plans/given.md").read_bytes(), self.plan.read_bytes())

    def test_server_start_timeout_exits_one_with_its_text_instead_of_a_traceback(self) -> None:
        timeout = subprocess.TimeoutExpired("fixture server start", 10)
        errors = io.StringIO()
        with (patch.dict(os.environ, self.env), redirect_stderr(errors),
              patch.object(herdr, "start_server", side_effect=timeout) as start,
              patch.object(herdr, "open_tab") as opened,
              patch.object(herdr, "start_agent") as agent):
            code = add_unit.main(["--production", self.production_name, "alpha", "--plan", "docs/plans/given.md"])
        self.assertEqual(code, 1)
        self.assertEqual(errors.getvalue(), f"add_unit: {timeout}\n")
        start.assert_called_once_with(SHARED, 90_000)
        opened.assert_not_called()
        agent.assert_not_called()

    def test_a_running_shared_server_is_adopted_and_gets_the_units_tab(self) -> None:
        self.herdr_overrides[f"--session {SHARED} status server"] = CommandResponse(code=0, stdout="status: running\n")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.servers_started(), [])
        self.assertEqual(self.opened()[:6], ["--session", SHARED, "workspace", "create", "--label", "build-followups"])

    def test_new_work_still_refuses_a_row_off_the_naming_pattern(self) -> None:
        recorded = self.commit_row_off_the_naming_pattern()
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("trunk", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(
            result.stderr,
            f"add_unit: unit trunk Worktree is {str(recorded)!r}; expected {str(self.checkout)!r}\n",
        )
        self.assert_no_launch_change(document, head)

    def test_standby_launch_records_state_without_writing_a_plan(self) -> None:
        self.pin_launch_clock()
        _ = self.successful("alpha", "--standby", "--port", "8123", subprocess_cli=True)
        worktree = self.root / "project-alpha"
        self.assert_staff_folder("alpha", self.root / "project-alpha")
        self.assert_unit_config(worktree, unit_config.StandbyPlan(), unit_config.AppPort(8123))
        self.assertNotIn("add unit", self.git("log", "-1", "--format=%s"))
        self.assertFalse((self.checkout / "docs/plans/build-followups-alpha.md").exists())
        self.assertFalse((worktree / "docs/plans/build-followups-alpha.md").exists())
        # Adding a unit registers nothing: the showrunner's update timer already names the doc.
        self.assertFalse(self.config.exists())
        self.assertEqual({key: value for key, value in self.pane_environment().items() if key.startswith("SHOWRUNNER_")}, {})
        command = self.opening_prompt()
        prompt = (f"You are alpha in production build-followups (doc {self.doc}), "
                  "under the showrunner, whose `SendMessage` address `~/.claude/scripts/lib/py "
                  "~/.claude/scripts/production/showrunners.py address build-followups` prints (look it up "
                  "before each message: it changes when the showrunner restarts), on standby. "
                  f"Work only in your worktree {worktree}, "
                  "branch build-followups-alpha. Do nothing until the showrunner sends you work.")
        self.assertIn(prompt, command)
        self.assert_director_flags("opus", "xhigh")
        self.assertRegex(self.log.read_text(),
                         rf"^- {LAUNCHED_AT:%H:%M %Z}: added alpha \(standby\), worktree ")

    def test_permanent_launch_says_wrap_keeps_it(self) -> None:
        _ = self.successful("alpha", "--permanent")
        self.assert_staff_folder("alpha", self.root / "project-alpha")
        self.assertIn("on standby. You are permanent: wrap never retires you. Work only in your worktree",
                      self.opening_prompt())

    def test_the_nightly_review_unit_runs_from_rust_under_the_review_settings(self) -> None:
        folder = Path(self.env["HOME"]) / "rust"
        folder.mkdir()
        config = Path(self.env["HOME"]) / ".claude2/.claude.json"
        _ = config.write_text(json.dumps({
            "oauthAccount": {"emailAddress": "owner@example.com"},
            "projects": {**self.trusted_projects(), str(folder): {"hasTrustDialogAccepted": True}}}))
        result = self.successful("nightly-review", "--permanent", "--responsibility", "nightly-review")
        self.assertIn("nightly-review started: herdr session attach agents, workspace build-followups, tab nightly-review", result.stdout)
        self.assertEqual(self.opened()[7], str(folder))
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(config.parent))
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--remote-control"):], [
            "--remote-control", "nightly-review", "-n", "nightly-review",
            "--add-dir", "/etc/nixos", "--add-dir", self.env["HOME"] + "/.claude",
            "--settings", str(SCRIPT.parents[1] / "nightly_review/settings.json")])
        prompt = self.opening_prompt()
        self.assertIn("You run read-only from ~/rust", prompt)
        self.assertIn("Each night the nightly timer sends you", prompt)
        self.assertEqual(sum(row[2:4] == ["agent", "prompt"] for row in self.herdr_calls()), 1)
        self.assertNotIn("claude", arguments)
        self.assertNotIn(prompt, arguments)

    def test_standby_rejects_other_modes_before_any_change(self) -> None:
        original = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        for args in (("--standby", "--plan", "docs/plans/given.md"),
                     ("--standby", "--brief", "Write a plan")):
            with self.subTest(args=args):
                result = self.cli("alpha", *args)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(len((result.stdout + result.stderr).strip().splitlines()), 1)
                self.assertEqual(self.doc.read_text(), original)
                self.assertEqual(self.git("rev-parse", "HEAD"), head)
                self.assertFalse(self.config.exists())
                self.assertFalse(self.log.exists())
                self.assertFalse((self.root / "project-alpha").exists())
                self.assertEqual(self.servers_started(), [])

    def test_plan_adds_worktree_launches_and_records(self) -> None:
        self.pin_launch_clock()
        pushed = self.git("--git-dir", str(self.origin), "rev-parse", "refs/heads/build-followups")
        result = self.successful("alpha", "--plan", "docs/plans/given.md", "--port", "8123")
        worktree = self.root / "project-alpha"
        self.assert_staff_folder("alpha", worktree)
        # An add pushes nothing: the unit branch waits for its first checkpoint push, which sets the upstream.
        self.assertEqual(self.git("--git-dir", str(self.origin), "rev-parse", "refs/heads/build-followups"), pushed)
        self.assertEqual(self.git("--git-dir", str(self.origin), "for-each-ref", "--format=%(refname)", "refs/heads"),
                         "refs/heads/build-followups")
        self.assertEqual(self.git("for-each-ref", "--format=%(refname) upstream=%(upstream)",
                                  "refs/heads/build-followups-alpha"),
                         "refs/heads/build-followups-alpha upstream=")
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree),
                         "build-followups-alpha")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=worktree),
                         self.git("rev-parse", "build-followups"))
        self.assertEqual(self.servers_started(), [["--user", "start", "herdr-agents.service"]])
        self.assertEqual(self.opened()[:8], ["--session", SHARED, "workspace", "create", "--label", "build-followups",
                                             "--cwd", str(worktree)])
        self.assertIn(["--session", SHARED, "tab", "rename", "w1:t1",
                       "alpha"], self.herdr_calls())
        self.assertIn(["--session", SHARED, "pane", "rename", "w1:p1", "alpha"], self.herdr_calls())
        self.assertEqual({key: value for key, value in self.pane_environment().items()
                          if key.startswith(("SHOWRUNNER_", "CLAUDE_", "HERDR_")) or key == "ENABLE_TOOL_SEARCH"},
                         {"ENABLE_TOOL_SEARCH": "true", "HERDR_AGENT_PANE": "1",
            "CLAUDE_CONFIG_DIR": str(Path(self.env["HOME"]) / ".claude2"),
            "CLAUDE_CODE_REMOTE_MEMORY_DIR": str(Path(self.env["HOME"]) / ".claude")})
        arguments = self.agent_arguments()
        self.assertNotIn("--session-id", arguments)
        self.assertNotIn("--resume", arguments)
        self.assertEqual(arguments,
            ["--model", "opus", "--effort", "xhigh", "--remote-control", "alpha", "-n", "alpha", "--settings", '{"disableAgentView": true}'])
        self.assertEqual(self.opening_prompt(), "/unit:direct docs/plans/given.md")
        self.assertEqual(len([row for row in self.herdr_calls() if row[2:4] == ["agent", "prompt"]]), 1)
        # Adding a unit registers nothing: the showrunner's update timer already names the doc.
        self.assertFalse(self.config.exists())
        self.assertRegex(self.log.read_text(),
                         rf"^- {LAUNCHED_AT:%H:%M %Z}: added alpha \(plan\), worktree ")
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertIn("alpha started: herdr session attach agents, workspace build-followups, tab alpha", result.stdout)

    def test_a_review_named_unit_opens_unit_direct(self) -> None:
        _ = self.successful("review", "--plan", "docs/plans/given.md")
        self.assertIn(shlex.quote("/unit:direct docs/plans/given.md"), self.launch_command())
        self.assertNotIn("/review:direct", self.launch_command())

    def test_a_rerun_finds_the_unit_by_its_mark_after_its_tab_was_renamed(self) -> None:
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.herdr_overrides[f"--session {SHARED} pane list"] = CommandResponse(code=0, stdout=json.dumps({
            "result": {"panes": [{"pane_id": "w1:p1", "tab_id": "w1:t1", "label": "renamed"}]}}))
        result = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(len(self.servers_started()), 1)
        self.assertEqual(sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls()), 1)
        self.assertIn("alpha started: herdr session attach agents, workspace build-followups, tab alpha", result.stdout)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def existing_config(self, plan: unit_config.PlanFile | unit_config.StandbyPlan, branch: str) -> None:
        worktree = self.root / "project-alpha"
        _ = self.git("worktree", "add", "-b", branch, str(worktree), "build-followups")
        _ = unit_config.write(worktree, unit_config.UnitConfig(plan, unit_config.NoAppPort(), ()))

    def test_an_existing_config_on_another_branch_refuses_before_launch(self) -> None:
        self.existing_config(unit_config.PlanFile(Path("docs/plans/given.md")), "other-branch")
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("Branch is 'other-branch'; expected 'build-followups-alpha'", result.stderr)
        self.assertEqual((self.doc.read_text(), self.git("rev-parse", "HEAD"), self.servers_started()), (document, head, []))

    def test_an_existing_standby_config_uses_standby_mode(self) -> None:
        self.existing_config(unit_config.StandbyPlan(), "build-followups-alpha")
        head = self.git("rev-parse", "HEAD")
        _ = self.successful("alpha", "--standby")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertIn("added alpha (standby)", self.log.read_text())

    def test_another_units_worktree_refuses_before_launch(self) -> None:
        _ = self.prepare_restored_worktree("beta", self.root / "project-alpha")
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("already used by beta", result.stderr)
        self.assertEqual((self.doc.read_text(), self.git("rev-parse", "HEAD"), self.servers_started()), (document, head, []))

    def test_check_refuses_invalid_session_name_without_any_change(self) -> None:
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("new director", "--plan", "docs/plans/given.md", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766",
                          "--cwd", str(self.root / "prior-session"), "--check", "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertIn("name must contain only letters", result.stderr)
        self.assert_no_launch_change(document, head)

    def test_berth_target_is_set_only_when_berth_config_exists(self) -> None:
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertNotIn("cargoBerthTarget", self.git("config", "--local", "--list"))

    def test_berth_target_is_set_when_config_exists(self) -> None:
        berth = self.checkout / ".claude/config/berth.toml"
        _ = berth.parent.mkdir(parents=True)
        _ = berth.write_text("", encoding="utf-8")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.git("config", "branch.build-followups-alpha.cargoBerthTarget"),
                         "build-followups")

    def test_brief_commits_stub_with_verbatim_words_and_approval_prompt(self) -> None:
        self.pin_launch_clock()
        words = "Investigate the slow build.\nKeep the user's exact wording."
        result = self.successful("alpha", "--brief", words)
        stub = self.checkout / "docs/plans/build-followups-alpha.md"
        self.assertTrue(stub.exists())
        content = stub.read_text()
        self.assertIn("# alpha", content)
        self.assertNotIn("Production:", content)
        self.assertIn("## Source", content)
        self.assertIn(words, content)
        self.assertIn(f"{LAUNCHED_AT:%Y-%m-%d %H:%M %Z}", content)
        self.assert_staff_folder("alpha", self.root / "project-alpha")
        self.assert_unit_config(self.root / "project-alpha", unit_config.PlanFile(Path("docs/plans/build-followups-alpha.md")))
        self.assertEqual(self.git("show", "--pretty=format:", "--name-only", "HEAD"), "docs/plans/build-followups-alpha.md")
        prompt = self.opening_prompt()
        self.assertIn("Write the full phased plan there", prompt)
        self.assertIn("wait for its approval before you run /unit:direct docs/plans/build-followups-alpha.md", prompt)
        self.assertIn("alpha started: herdr session attach agents, workspace build-followups, tab alpha", result.stdout)
        self.assert_director_flags("opus", "xhigh")

    def test_branch_takes_the_production_name_and_the_stub_sits_beside_the_production_doc(self) -> None:
        _ = self.git("checkout", "-b", "init/catalyst")
        self.doc = self.checkout / "docs/hana/tool-based-ui-production.md"
        _ = self.doc.parent.mkdir(parents=True)
        _ = self.doc.write_text(self.production_doc("init/catalyst"), encoding="utf-8")
        _ = self.git("add", "docs/hana/tool-based-ui-production.md")
        _ = self.git("commit", "-m", "production under another merge branch")
        self.production_name = "hana"
        _ = self.staff.record_production(self.production_name, self.doc)
        _ = self.successful("lfo", "--brief", "Add the LFO tool")
        worktree = self.root / "project-lfo"
        self.assert_staff_folder("lfo", worktree)
        self.assert_unit_config(worktree, unit_config.PlanFile(Path("docs/hana/hana-lfo.md")))
        self.assertEqual(self.git("branch", "--show-current", cwd=worktree), "hana-lfo")
        self.assertEqual(self.git("show", "--pretty=format:", "--name-only", "HEAD"), "docs/hana/hana-lfo.md")
        self.assertFalse((self.checkout / "docs/plans/init").exists())
        self.assertEqual(self.git("branch", "--list", "init/catalyst-lfo"), "")

    def test_timeout_keeps_session_then_rerun_finishes_without_duplicate_steps(self) -> None:
        _ = (self.state / "ready").unlink()
        # Exercise a pending poll and deadline expiry without sleeping ten seconds.
        clock = RemoteControlDeadlineClock()
        with patch.object(add_unit, "time", clock):
            first = self.cli("alpha", "--plan", "docs/plans/given.md", "--timeout", "10")
        self.assertEqual(clock.sleeps, [0.5])
        self.assertEqual(first.returncode, 1, (first.stdout, first.stderr))
        self.assertIn("/remote-control did not become active", first.stderr)
        self.assertIn("(herdr agents:", first.stderr)
        self.assertEqual(first.stderr.count("anomaly: alpha: registry has no remote-control evidence\n"), 1)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.log.exists())
        commit = self.git("rev-parse", "HEAD")
        _ = (self.state / "ready").touch()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.git("rev-parse", "HEAD"), commit)
        self.assert_staff_units("alpha")
        self.assertEqual(len(self.servers_started()), 1)
        # Adding a unit registers nothing: the showrunner's update timer already names the doc.
        self.assertFalse(self.config.exists())
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_refusals_change_no_repository_or_registry_state(self) -> None:
        cases: tuple[tuple[str, tuple[str, ...]], ...] = (
            ("no mode", ("alpha",)),
            ("two modes", ("alpha", "--plan", "docs/plans/given.md", "--brief", "words")),
            ("missing plan", ("alpha", "--plan", "docs/plans/missing.md")),
        )
        initial = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        for label, arguments in cases:
            with self.subTest(label=label):
                result = self.cli(*arguments)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(len((result.stdout + result.stderr).strip().splitlines()), 1)
                self.assertEqual(self.doc.read_text(), initial)
                self.assertEqual(self.git("rev-parse", "HEAD"), head)
                self.assertFalse(self.config.exists())

    def test_refuses_missing_production_field(self) -> None:
        original = self.doc.read_text()
        _ = self.doc.write_text(original.replace("- **Log:** `docs/plans/build-followups-log.md` — git-excluded\n", ""))
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertFalse((self.root / "project-alpha").exists())
        self.assert_staff_units()

    def test_a_doc_without_a_user_zone_line_still_adds_a_unit(self) -> None:
        # The zone is the user's setting; a production doc no longer has to name one.
        original = self.doc.read_text()
        _ = self.doc.write_text(original.replace("- **User zone:** America/Los_Angeles — reports use PDT\n", ""))
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assert_staff_folder("alpha", self.root / "project-alpha")

    def test_refuses_checkout_off_merge_branch(self) -> None:
        _ = self.git("checkout", "-b", "wrong-branch")
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertFalse((self.root / "project-alpha").exists())
        self.assert_staff_units()

    def test_new_work_records_no_scheduled_prompt(self) -> None:
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")

        # herdr records the typed opening prompt under script-prompts; no scheduled prompt is kept.
        self.assertFalse((self.pause_state / "scheduled-prompts").exists())

    def commit_brief_row(self) -> Path:
        stub = self.checkout / "docs/plans/build-followups-alpha.md"
        _ = stub.write_text(
            "# Alpha\n\n> **Production: build-followups** — unit `alpha`\n\n"
            + "## Source\n\nWrite a full plan.\n", encoding="utf-8")
        _ = self.git("add", "docs/plans/build-followups-alpha.md")
        _ = self.git("commit", "-m", "production(build-followups): add unit alpha (brief)")
        self.existing_config(unit_config.PlanFile(Path("docs/plans/build-followups-alpha.md")), "build-followups-alpha")
        return stub

    def test_brief_row_records_plan_mode_on_its_stub_and_reuses_running_plan(self) -> None:
        stub = self.commit_brief_row()
        content = stub.read_bytes()
        head = self.git("rev-parse", "HEAD")

        _ = self.successful("alpha", "--plan", "docs/plans/build-followups-alpha.md")

        self.assertEqual(self.opening_prompt(), "/unit:direct docs/plans/build-followups-alpha.md")
        self.assertEqual(len(self.servers_started()), 1)
        self.assertEqual(stub.read_bytes(), content)
        plan_head = self.git("rev-parse", "HEAD")
        self.assertNotEqual(plan_head, head)
        self.assertEqual(self.git("log", "-1", "--format=%s"),
                         "production(build-followups): add unit alpha (plan)")
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}"), self.git("rev-parse", f"{head}^{{tree}}"))
        self.assertIn("added alpha (plan)", self.log.read_text())
        prompts = [row for row in self.herdr_calls() if row[2:4] == ["agent", "prompt"]]
        marked = unit_lookup.LabeledSession("alpha",
            unit_lookup.LiveClaude("alpha", "plan-session-id", "/fixture/socket", 4242),
            HerdrPlace("plan-host", "w1:p1"), identity=unit_lookup.LiveSession(unit_membership("build-followups", "alpha").current_session))

        with patch.object(add_unit, "launched_unit", return_value=add_unit.LabeledSessionFound(marked)), \
                patch.object(add_unit, "wait_for_remote_control", return_value=marked):
            retry = self.successful("alpha", "--plan", "docs/plans/build-followups-alpha.md")

        self.assertNotIn("runs its brief", retry.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), plan_head)
        self.assertEqual(stub.read_bytes(), content)
        self.assertEqual(len(self.servers_started()), 1)
        self.assertEqual([row for row in self.herdr_calls() if row[2:4] == ["agent", "prompt"]], prompts)

    def test_brief_row_uses_new_plan(self) -> None:
        stub = self.commit_brief_row()
        content = stub.read_bytes()
        head = self.git("rev-parse", "HEAD")

        _ = self.successful("alpha", "--plan", "docs/plans/given.md")

        self.assertEqual(self.opening_prompt(), "/unit:direct docs/plans/given.md")
        self.assertEqual(len(self.servers_started()), 1)
        self.assertNotEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual((self.root / "project-alpha/docs/plans/given.md").read_bytes(), self.plan.read_bytes())
        self.assertEqual(stub.read_bytes(), content)
        self.assertIn("added alpha (plan)", self.log.read_text())

    def test_running_brief_refuses_plan_mode_before_any_write(self) -> None:
        stub = self.commit_brief_row()
        worktree = self.root / "project-alpha"
        owner = unit_membership("build-followups", "alpha")
        owner = staff.update_current_session(owner.current_session,
            staff.native_id("62ccbd64-f22e-5647-b20e-7899aa39d766"))
        owner = staff.set_worktree(owner.id, staff.PresentWorktree("natedev", worktree), expected=owner.worktree)
        marker = worktree / "in-progress.txt"
        _ = marker.write_text("Work from the brief.\n", encoding="utf-8")
        _ = self.log.write_text("The brief is still running.\n", encoding="utf-8")
        repository = self.repository_state()
        history = self.git("log", "--format=%H:%s", "build-followups")
        worktree_status = self.git("status", "--porcelain", cwd=worktree)
        saved = {path: path.read_bytes() for path in (
            self.doc, self.log, stub, self.plan, marker,
            worktree / "docs/plans/build-followups-production.md",
            worktree / "docs/plans/build-followups-alpha.md",
        )}
        marked = unit_lookup.LabeledSession("alpha",
            unit_lookup.LiveClaude("alpha", owner.current_session, "/fixture/socket", 4242),
            HerdrPlace("brief-host", "w1:p1"), identity=unit_lookup.LiveSession(owner.current_session))

        launches = ((), ("--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(worktree)))
        for plan in ("docs/plans/given.md", "docs/plans/build-followups-alpha.md"):
            for flags in launches:
                with self.subTest(plan=plan, flags=flags):
                    with patch.object(add_unit, "launched_unit", return_value=add_unit.LabeledSessionFound(marked)), \
                            patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()), \
                            patch.object(add_unit, "wait_for_remote_control", return_value=marked) as waited:
                        result = self.cli("alpha", "--plan", plan, *flags)

                    self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                    self.assertIn("unit alpha runs its brief in session brief-host; end that session, then run",
                                  result.stderr)
                    self.assertIn(f"--plan {plan}", result.stderr)
                    self.assertEqual(self.repository_state(), repository)
                    self.assertEqual(self.git("log", "--format=%H:%s", "build-followups"), history)
                    self.assertEqual(self.git("status", "--porcelain", cwd=worktree), worktree_status)
                    self.assertEqual({path: path.read_bytes() for path in saved}, saved)
                    self.assertEqual(self.servers_started(), [])
                    self.assertFalse(any(row[2:4] == ["agent", "prompt"] for row in self.herdr_calls()))
                    waited.assert_not_called()

    def test_brief_row_resume_with_missing_replacement_plan_refuses_before_any_write(self) -> None:
        stub = self.commit_brief_row()
        worktree = self.root / "project-alpha"
        marker = worktree / "in-progress.txt"
        _ = marker.write_text("Keep the brief work.\n", encoding="utf-8")
        _ = self.log.write_text("The brief is stopped.\n", encoding="utf-8")
        repository = self.repository_state()
        history = self.git("log", "--format=%H:%s", "build-followups")
        worktree_status = self.git("status", "--porcelain", cwd=worktree)
        saved = {path: path.read_bytes() for path in (
            self.doc, self.log, stub, self.plan, marker,
            worktree / "docs/plans/build-followups-production.md",
            worktree / "docs/plans/build-followups-alpha.md",
        )}

        with patch.object(add_unit, "launched_unit", return_value=add_unit.NoLabeledSession()), \
                patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()):
            result = self.cli("alpha", "--plan", "docs/plans/missing.md", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766",
                              "--cwd", str(worktree))

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("plan docs/plans/missing.md is missing on build-followups",
                      result.stderr)
        self.assertEqual(self.repository_state(), repository)
        self.assertEqual(self.git("log", "--format=%H:%s", "build-followups"), history)
        self.assertEqual(self.git("status", "--porcelain", cwd=worktree), worktree_status)
        self.assertEqual({path: path.read_bytes() for path in saved}, saved)
        self.assertFalse((worktree / "docs/plans/missing.md").exists())
        self.assertEqual(self.servers_started(), [])
        self.assertFalse(any(row[2:4] == ["agent", "prompt"] for row in self.herdr_calls()))

    def test_brief_source_without_add_subject_uses_late_plan_in_existing_worktree(self) -> None:
        stub = self.commit_brief_row()
        _ = self.git("commit", "--amend", "-m", "prepare a brief unit")
        worktree = self.root / "project-alpha"
        unit_tip = self.git("rev-parse", "HEAD", cwd=worktree)
        marker = worktree / "in-progress.txt"
        _ = marker.write_text("Keep the unit's work.\n", encoding="utf-8")
        plan_path = "docs/plans/approved-later.md"
        late_plan = self.checkout / plan_path
        _ = late_plan.write_text(
            "# Approved plan\n\n> **Production: build-followups** — unit `alpha`\n\n"
            + "## Phases\n\n1. Finish the unit.\n", encoding="utf-8")
        _ = self.git("add", plan_path)
        _ = self.git("commit", "-m", "approve the full plan")
        content = stub.read_bytes()
        self.assertFalse((worktree / plan_path).exists())

        _ = self.successful("alpha", "--plan", plan_path)

        self.assertEqual(self.opening_prompt(), f"/unit:direct {plan_path}")
        self.assertEqual(len(self.servers_started()), 1)
        self.assert_unit_config(worktree, unit_config.PlanFile(Path(plan_path)))
        self.assertEqual((worktree / plan_path).read_bytes(), late_plan.read_bytes())
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=worktree), unit_tip)
        self.assertEqual(marker.read_text(), "Keep the unit's work.\n")
        self.assertEqual(stub.read_bytes(), content)

    def test_standby_row_refuses_plan_mode_with_the_mode_and_command(self) -> None:
        self.existing_config(unit_config.StandbyPlan(), "build-followups-alpha")
        original = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")

        result = self.cli("alpha", "--plan", "docs/plans/given.md")

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("unit alpha was added in standby mode; plan mode cannot reuse its row", result.stderr)
        self.assertIn("--standby", result.stderr)
        self.assertNotIn("already taken", result.stderr)
        self.assertEqual((self.doc.read_text(), self.git("rev-parse", "HEAD"), self.servers_started()), (original, head, []))

    def test_kebab_alias_refuses_existing_branch_and_worktree_row(self) -> None:
        _ = self.successful("mul_add", "--plan", "docs/plans/given.md")
        original = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("mul-add", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(self.doc.read_text(), original)
        self.assert_staff_units("mul_add")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_existing_local_or_origin_branch_refuses_before_any_write(self) -> None:
        for location in ("local", "origin"):
            with self.subTest(location=location):
                branch = "build-followups-alpha"
                if location == "local":
                    _ = self.git("branch", branch)
                else:
                    _ = self.git("branch", "-D", branch)
                    _ = self.git("push", "origin", f"HEAD:refs/heads/{branch}")
                original = self.doc.read_text()
                head = self.git("rev-parse", "HEAD")
                result = self.cli("alpha", "--brief", "Write a plan")
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(self.doc.read_text(), original)
                self.assertEqual(self.git("rev-parse", "HEAD"), head)
                self.assertFalse((self.checkout / "docs/plans/build-followups-alpha.md").exists())

    def test_occupied_worktree_refuses_before_row_commit_or_stub(self) -> None:
        occupied = self.root / "project-alpha"
        occupied.mkdir()
        _ = (occupied / "marker").write_text("keep", encoding="utf-8")
        original = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--brief", "Write a plan")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(self.doc.read_text(), original)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual((occupied / "marker").read_text(), "keep")
        self.assertFalse((self.checkout / "docs/plans/build-followups-alpha.md").exists())

    def test_unrelated_repository_on_expected_branch_refuses_before_commit(self) -> None:
        occupied = self.root / "project-alpha"
        occupied.mkdir()
        _ = self.git("init", "-b", "build-followups-alpha", cwd=occupied)
        original = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(self.doc.read_text(), original)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_another_live_session_leaves_the_launch_alone(self) -> None:
        self.herdr_overrides["--session default session list --json"] = CommandResponse(code=0, stdout=json.dumps(
            herdr.SessionListResult(sessions=[herdr.SessionEntry(name="alpha2", running=True, default=False,
                session_dir="/fixture", socket_path="/fixture/socket")])))
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(len(self.servers_started()), 1)

    def test_remote_control_timeout_names_the_unit_and_place(self) -> None:
        request = add_unit.UnitLaunch(
            production=read_production(self.production_name), identity=add_unit.UnitIdentity("alpha", "alpha"),
            branch="build-followups-alpha", worktree=self.root / "project-alpha", plan=add_unit.Standby(),
            port=add_unit.OmittedCell(), session=add_unit.NewSession(), timeout=0,
        )
        marked = unit_lookup.LabeledSession("alpha", unit_lookup.ClaudeNotRunning(),
            HerdrPlace("native", "w1:p1"), identity=unit_lookup.LiveSession(fixture_runtime_id("remote-control-alpha")))
        errors = io.StringIO()
        with patch.object(add_unit, "launched_unit", return_value=add_unit.LabeledSessionFound(marked)), redirect_stderr(errors):
            with self.assertRaises(RuntimeError) as caught:
                _ = add_unit.wait_for_remote_control(request)
        self.assertEqual(str(caught.exception), "alpha (herdr native:w1:p1): /remote-control did not become active")
        self.assertEqual(errors.getvalue(), "anomaly: alpha: registry has no remote-control evidence\n")

    def test_remote_control_reads_the_session_registry(self) -> None:
        request = add_unit.UnitLaunch(
            production=read_production(self.production_name), identity=add_unit.UnitIdentity("alpha", "alpha"),
            branch="build-followups-alpha", worktree=self.root / "project-alpha", plan=add_unit.Standby(),
            port=add_unit.OmittedCell(), session=add_unit.NewSession(), timeout=0,
        )
        registry = self.root / "registry"
        registry.mkdir()
        place = HerdrPlace("native", "w1:p1")
        live = unit_lookup.LiveClaude("alpha", "62ccbd64-f22e-5647-b20e-7899aa39d766", "/socket", 4242)
        identity = unit_lookup.LiveSession(live.session_id)
        starting = unit_lookup.LabeledSession("alpha", unit_lookup.ClaudeNotRunning(), place, identity=identity)
        marked = unit_lookup.LabeledSession("alpha", live, place, identity=identity)
        cases: tuple[tuple[str, dict[str, str], bool], ...] = (
            ("bridged", {"bridgeSessionId": "session_bridge"}, True),
            ("no bridge", {}, False),
            ("empty bridge", {"bridgeSessionId": ""}, False),
            ("another session's record", {"sessionId": "session-other", "bridgeSessionId": "session_bridge"}, False))
        for label, extra, active in cases:
            with self.subTest(label):
                _ = (registry / "4242.json").write_text(json.dumps({
                    "pid": 4242, "sessionId": "62ccbd64-f22e-5647-b20e-7899aa39d766", "name": "alpha", "messagingSocketPath": "/socket",
                    "updatedAt": 1, **extra}))
                # The first lookup finds the native identity before the record names the live Claude.
                with patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(registry)}), \
                        patch.object(add_unit, "launched_unit", side_effect=[
                            add_unit.LabeledSessionFound(starting), add_unit.LabeledSessionFound(marked)]), \
                        redirect_stderr(errors := io.StringIO()):
                    if active:
                        self.assertEqual(add_unit.wait_for_remote_control(request), marked)
                        self.assertEqual(errors.getvalue(), "")
                    else:
                        with self.assertRaisesRegex(RuntimeError, "did not become active$"):
                            _ = add_unit.wait_for_remote_control(request)
                        self.assertEqual(errors.getvalue(), "anomaly: alpha: registry has no remote-control evidence\n")

    def test_underscore_name_and_checkout_without_trunk_suffix(self) -> None:
        renamed = self.root / "project"
        _ = self.checkout.rename(renamed)
        self.checkout = renamed
        self.doc = renamed / "docs/plans/build-followups-production.md"
        self.plan = renamed / "docs/plans/given.md"
        _ = (Path(self.env["HOME"]) / ".claude2/.claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "owner@example.com"},
            "projects": self.trusted_projects()}), encoding="utf-8")
        _ = self.doc.write_text(self.production_doc(), encoding="utf-8")
        _ = self.git("add", "docs/plans/build-followups-production.md")
        _ = self.git("commit", "-m", "move checkout")
        _ = self.git("push", "origin", "build-followups")
        _ = self.staff.record_production("build-followups", self.doc)
        _ = self.successful("mul_add", "--plan", "docs/plans/given.md")
        self.assert_staff_folder("mul_add", self.root / "project-mul-add")
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD",
                                  cwd=self.root / "project-mul-add"), "build-followups-mul-add")


if __name__ == "__main__":
    _ = unittest.main()
