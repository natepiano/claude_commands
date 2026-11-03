"""Exercise add_unit through its command line with isolated git and launcher state."""

from __future__ import annotations

from scripts.production.staff_test_support import fixture_member, ClaudeBackendFixture, StaffTestCase, fixture_runtime_id

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
import add_unit
import production_units
import unit_lookup
import session_pane
import sessions
from herdr import HerdrPlace
import herdr
from scripts.herdr.fake_herdr import CommandResponse


from add_unit_test_support import AddUnitTestCase, configure_test_zone, SCRIPT, SHARED, RemoteControlDeadlineClock


LAUNCHED_AT = datetime(2026, 10, 6, 12, tzinfo=ZoneInfo("America/Los_Angeles"))


def setUpModule() -> None:
    configure_test_zone()


from scripts.production import production_units as shared_production_units
from scripts.production import staff, unit_config

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
        _ = fixture_member("unit", name="saved-name", parent=runner.id, runtime_id=fixture_runtime_id("decoy-unit"))
        request = add_unit.UnitLaunch(
            production=production_units.read_production(document), branch="kept", worktree=self.root,
            plan=add_unit.PlanGiven(Path("plan.md")), port=add_unit.OmittedCell(), owns=add_unit.OmittedCell(),
            session=add_unit.ResumedSession(current, self.root), launch_kind=add_unit.NewWorkLaunch(), timeout=5,
            identity=add_unit.UnitIdentity("saved-name-unit", "saved-name"))
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

    def test_fresh_unit_uses_the_observed_claude_id_before_its_opening_prompt(self) -> None:
        document = self.production("demo")
        _ = document.write_text(document.read_text() + "\n- **Merge branch:** `main`\n"
                                + f"- **Showrunner checkout:** `{self.root}`\n- **Log:** `demo-log.md`\n")
        runner = fixture_member("showrunner", name="demo", doc=staff.LiveDoc("natedev", document))
        production = production_units.read_production(document)
        folder = self.root / "fresh-unit"
        folder.mkdir()
        identity = fixture_runtime_id("fresh-Claude-unit")
        backend = ClaudeBackendFixture(identity, self.root / "claude-account")
        request = add_unit.UnitLaunch(
            production=production, branch="fresh", worktree=folder,
            plan=add_unit.PlanGiven(Path("plan.md")), port=add_unit.OmittedCell(), owns=add_unit.OmittedCell(),
            session=add_unit.NewSession(), launch_kind=add_unit.NewWorkLaunch(), timeout=5,
            identity=add_unit.UnitIdentity("fresh-unit", "fresh"))
        from scripts.accounts.accounts import NamedAccount

        with (patch.object(herdr, "start_server"), patch.object(herdr, "launch_agent", side_effect=backend.launch)):
            outcome = add_unit.launch_session(request, add_unit.DirectorAgent("fixture", add_unit.DefaultEffort()),
                "/unit:direct plan.md", NamedAccount("claude1", backend.configuration))
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        row = backend.before_opening.by_id(staff.StaffId(identity))
        assert row is not None
        self.assertEqual((row.id, row.name, row.parent), (identity, "fresh", runner.id))
        self.assertNotIn("--session-id", backend.arguments)
        self.assertNotIn("--resume", backend.arguments)
        self.assertNotIn("STAFF_ID", backend.environment)


@final
class AddUnitLaunchTests(AddUnitTestCase):
    def assert_observed_identity(self, row: staff.StaffRow) -> None:
        pid = int(self.env["FAKE_CLAUDE_PID"])
        registry = sessions.read_session(Path(self.env["NOTIFIER_SESSIONS_DIR"]) / f"{pid}.json")
        assert not isinstance(registry, sessions.UnreadableSessionRecord)
        self.assertEqual(registry["sessionId"], self.env["FAKE_CLAUDE_SESSION_ID"])
        self.assertEqual(registry.get("procStart"), sessions.process_start(pid))
        self.assertEqual((row.id, row.current_session), (registry["sessionId"], registry["sessionId"]))

    def pin_launch_clock(self) -> None:
        def now(zone: tzinfo | None = None) -> datetime:
            return LAUNCHED_AT.astimezone(zone)

        _ = self.enterContext(patch.object(add_unit, "datetime", wraps=datetime, now=now))
        # Keep the subprocess CLI check while giving it the same clock as in-process calls.
        script = self.root / "clocked_add_unit.py"
        _ = script.write_text(
            "import sys\nfrom datetime import datetime\nfrom unittest.mock import patch\n"
            + f"sys.path.insert(0, {str(Path(add_unit.__file__).parent)!r})\n"
            + "import add_unit\n"
            + f"at = datetime.fromisoformat({LAUNCHED_AT.isoformat()!r})\n"
            + "with patch.object(add_unit, 'datetime', wraps=datetime) as clock:\n"
            + "    clock.now.side_effect = lambda zone: at.astimezone(zone)\n"
            + "    raise SystemExit(add_unit.main(sys.argv[1:]))\n", encoding="utf-8")
        _ = self.enterContext(patch("add_unit_test_support.SCRIPT", script))

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
        self.assertIn(self.unit_row(), self.doc.read_text())
        self.assertEqual(stub.read_bytes(), source)

    def test_adopting_a_prepared_row_without_a_port_flag_keeps_its_recorded_port(self) -> None:
        self.commit_prepared_row(self.unit_row(port="8123"))
        worktree = self.root / "project-alpha"
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        expected = unit_config.UnitConfig(unit_config.PlanFile(Path("docs/plans/given.md")),
                                         unit_config.AppPort(8123), ())
        self.assertEqual(unit_config.read(worktree), expected)
        path = worktree / ".unit.toml"
        before = path.read_bytes(), path.stat().st_mtime_ns
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(unit_config.read(worktree), expected)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertIn(self.unit_row(port="8123"), self.doc.read_text())

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
                            "--responsibility", "nightly", "--responsibility", "build-check")
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

    def test_standby_launch_writes_the_standby_plan_in_its_unit_config(self) -> None:
        _ = self.successful("alpha", "--standby")
        self.assertEqual(unit_config.read(self.root / "project-alpha"), unit_config.UnitConfig(
            unit_config.StandbyPlan(), unit_config.NoAppPort(), ()))

    def test_permanent_launch_writes_the_permanent_plan_in_its_unit_config(self) -> None:
        _ = self.successful("alpha", "--permanent")
        self.assertEqual(unit_config.read(self.root / "project-alpha"), unit_config.UnitConfig(
            unit_config.PermanentPlan(), unit_config.NoAppPort(), ()))

    def test_admission_uses_the_shared_port_parser_and_units_lookup(self) -> None:
        raw_port = "15733 (after it clashed with startup on 15731, 2026-10-02)"
        self.commit_prepared_row(self.unit_row(port=raw_port))
        with (patch.object(shared_production_units, "lookup_unit", wraps=shared_production_units.lookup_unit) as lookup,
              patch.object(unit_config, "parse_port_cell", wraps=unit_config.parse_port_cell) as parser):
            _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        value = unit_config.read(self.root / "project-alpha", cleanup=False)
        assert isinstance(value, unit_config.UnitConfig)
        self.assertEqual(value.port, unit_config.AppPort(15733))
        self.assertGreater(lookup.call_count, 0)
        parser.assert_any_call(raw_port)

    def test_new_unit_does_not_take_a_port_from_another_productions_row(self) -> None:
        other = self.root / "other-production.md"
        _ = other.write_text("## Units\n\n| Unit | Port |\n| --- | --- |\n| alpha-unit | 15733 (note) |\n")
        _ = fixture_member("showrunner", name="other", doc=staff.LiveDoc("natedev", other))
        _ = self.successful("alpha", "--standby")
        value = unit_config.read(self.root / "project-alpha", cleanup=False)
        assert isinstance(value, unit_config.UnitConfig)
        self.assertEqual(value.port, unit_config.NoAppPort())

    def test_new_unit_keeps_its_supplied_port_beside_another_productions_row(self) -> None:
        other = self.root / "other-production.md"
        _ = other.write_text("## Units\n\n| Unit | Port |\n| --- | --- |\n| alpha-unit | 15733 (note) |\n")
        _ = fixture_member("showrunner", name="other", doc=staff.LiveDoc("natedev", other))
        _ = self.successful("alpha", "--standby", "--port", "8123")
        value = unit_config.read(self.root / "project-alpha", cleanup=False)
        assert isinstance(value, unit_config.UnitConfig)
        self.assertEqual(value.port, unit_config.AppPort(8123))

    def test_brief_launch_writes_its_existing_relative_stub_plan_in_the_unit_config(self) -> None:
        _ = self.successful("alpha", "--brief", "Build the missing reader.")
        config = unit_config.read(self.root / "project-alpha")
        assert isinstance(config, unit_config.UnitConfig)
        self.assertIsInstance(config.plan, unit_config.PlanFile)
        assert isinstance(config.plan, unit_config.PlanFile)
        self.assertFalse(config.plan.path.is_absolute())
        self.assertTrue((self.checkout / config.plan.path).is_file())

    def test_existing_unit_name_under_another_production_requires_an_explicit_move(self) -> None:
        old_doc = self.staff.production("old", "alpha-unit", workspace="original")
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
                self.assertIn("alpha-unit", old_doc.read_text())
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
        process = self.root / "proc" / str(os.getpid())
        process.mkdir(parents=True, exist_ok=True)
        fields = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()
        fields[1] = "102"
        _ = (process / "stat").write_text(f"{os.getpid()} (claude) " + " ".join(fields))
        _ = (process / "cmdline").write_bytes(b"claude\0")
        _ = (process / "environ").write_bytes(b"HERDR_PANE_ID=w1:p1\0")
        _ = (process / "status").write_text(f"Uid:\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\n")

        for after in (False, True):
            before = self.staff.staff_file.read_bytes()

            def interrupted(production: production_units.Production, unit: str, *,
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
            self.assertEqual(self.doc.read_text().count("| alpha-unit |"), 1)
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

    def test_new_row_with_a_running_marked_unit_refuses_before_any_write(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        document = self.doc.read_bytes()
        repository = self.repository_state()
        head = self.git("rev-parse", "build-followups")
        plans = {path: path.read_bytes() for path in self.plan.parent.iterdir() if path.is_file()}
        marked = unit_lookup.MarkedUnit("alpha-unit",
            unit_lookup.LiveClaude("alpha", "running-id", "/fixture/socket", 4242),
            HerdrPlace("running-host", "w1:p1"))
        modes = (("--plan", "docs/plans/given.md"), ("--brief", "Write the missing plan."),
                 ("--standby",), ("--permanent",))
        for mode in modes:
            launches = ((),) if mode[0] == "--brief" else (
                (), ("--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(prior), "--autocompact", "200k"))
            for flags in launches:
                with self.subTest(mode=mode, flags=flags):
                    arguments = ["--production", str(self.doc), "alpha", *mode, *flags]
                    with (patch.object(add_unit, "launched_unit", return_value=add_unit.MarkedUnitFound(marked)),
                          patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()),
                          patch.object(add_unit, "launch_session") as launched,
                          patch.object(add_unit, "wait_for_remote_control", return_value=marked) as waited,
                          patch.object(herdr, "close_agent") as closed):
                        result = self.cli("alpha", *mode, *flags)

                    self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                    self.assertEqual(result.stderr,
                        "add_unit: unit alpha-unit already runs in session running-host but has no Units row; "
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
        with patch.dict(os.environ, self.env), redirect_stderr(errors):
            code = add_unit.main(["--production", str(missing), "alpha", "--permanent",
                                  "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(self.root)])
        self.assertEqual(code, 2, errors.getvalue())
        self.assertIn("cannot read production doc:", errors.getvalue())
        self.assertIn(str(missing), errors.getvalue())
        self.assert_no_launch_change(document, head)

    def test_new_work_launch_still_waits_for_remote_control(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result, error, commands, launches, _writes = self.run_with_marked_unit(
                add_unit.NewWorkLaunch(), unit_lookup.ClaudeNotRunning(), already_marked=False
            )
        self.assertEqual(result, 0, error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 1)
        self.assertEqual(output.getvalue(), "alpha-unit started: herdr session attach agents, workspace director, tab alpha\n")

    def test_unit_start_not_ready_prints_screen_and_sends_no_prompt_or_key(self) -> None:
        screen = "Do you trust this folder?"
        self.herdr_overrides["agent start"] = CommandResponse(
            code=1, stdout=json.dumps({"error": {"code": "agent_not_ready", "message": "dialog"}}))
        self.herdr_overrides[f"--session {SHARED} pane read w1:p1 --source visible"] = CommandResponse(code=0, stdout=screen)
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("add_unit: the session is not ready:\n" + screen, result.stderr)
        self.assertFalse(any(row[2:4] == ["server", "stop"] for row in self.herdr_calls()))
        self.assertFalse(any(row[2:4] in (["agent", "prompt"], ["pane", "send-keys"], ["pane", "send-text"])
                             for row in self.herdr_calls()))

    def test_a_prompt_never_typed_closes_the_new_session_so_a_rerun_starts_it(self) -> None:
        for unit, typed, ending in (("alpha", False, "its session is closed: run the same command again"),
                                    ("beta", True, "prompt typed 1 times")):
            with self.subTest(typed=typed):
                place = herdr.HerdrPlace(SHARED, "w1:p1")
                found = add_unit.MarkedUnitFound(unit_lookup.MarkedUnit(f"{unit}-unit", unit_lookup.ClaudeNotRunning(), place))
                outcome = herdr.PromptNotSubmitted("destination never became ready for input"
                                                   + ("; prompt typed 1 times" if typed else ""), never_typed=not typed)
                with (patch.object(herdr, "start_agent", return_value=outcome),
                      patch.object(add_unit, "launched_unit", side_effect=[add_unit.NoMarkedUnit(), found]),
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
        self.assertIn(self.unit_row(), self.doc.read_text())
        self.assertEqual(self.git("log", "-1", "--format=%s", "build-followups"),
                         "production(build-followups): add unit alpha-unit (plan)")
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
            code = add_unit.main(["--production", str(self.doc), "alpha", "--plan", "docs/plans/given.md"])
        self.assertEqual(code, 1)
        self.assertEqual(errors.getvalue(), f"add_unit: {timeout}\n")
        start.assert_called_once_with(SHARED, 90_000)
        opened.assert_not_called()
        agent.assert_not_called()

    def test_a_running_shared_server_is_adopted_and_gets_the_units_tab(self) -> None:
        self.herdr_overrides[f"--session {SHARED} status server"] = CommandResponse(code=0, stdout="status: running\n")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.servers_started(), [])
        self.assertEqual(self.opened()[:6], ["--session", SHARED, "workspace", "create", "--label", "director"])

    def test_new_work_still_refuses_a_row_off_the_naming_pattern(self) -> None:
        recorded = self.commit_row_off_the_naming_pattern()
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("trunk", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(
            result.stderr,
            f"add_unit: unit trunk-unit Worktree is {str(recorded)!r}; expected {str(self.checkout)!r}\n",
        )
        self.assert_no_launch_change(document, head)

    def test_standby_launch_records_state_without_writing_a_plan(self) -> None:
        self.pin_launch_clock()
        _ = self.successful("alpha", "--standby", "--port", "8123", "--owns", "src/alpha", subprocess_cli=True)
        worktree = self.root / "project-alpha"
        self.assertIn(self.unit_row(plan="standby", port="8123", owns="src/alpha"),
                      self.doc.read_text())
        self.assertEqual(self.git("log", "-1", "--format=%s"),
                         "production(build-followups): add unit alpha-unit (standby)")
        self.assertEqual(self.git("show", "--pretty=format:", "--name-only", "HEAD"),
                         "docs/plans/build-followups-production.md")
        self.assertFalse((self.checkout / "docs/plans/build-followups-alpha.md").exists())
        self.assertFalse((worktree / "docs/plans/build-followups-alpha.md").exists())
        # Adding a unit registers nothing: the showrunner's update timer already names the doc.
        self.assertFalse(self.config.exists())
        self.assertEqual({key: value for key, value in self.pane_environment().items() if key.startswith("SHOWRUNNER_")}, {"SHOWRUNNER_UNIT": "build-followups", "SHOWRUNNER_UNIT_ID": "alpha-unit"})
        command = self.opening_prompt()
        prompt = (f"You are alpha-unit in production build-followups (doc {self.doc}), "
                  "under the showrunner, whose `SendMessage` address `~/.claude/scripts/lib/py "
                  "~/.claude/scripts/production/showrunners.py address build-followups` prints (look it up "
                  "before each message: it changes when the showrunner restarts), on standby. "
                  f"Work only in your worktree {worktree}, "
                  "branch build-followups-alpha. Do nothing until the showrunner sends you work.")
        self.assertIn(prompt, command)
        self.assert_director_flags("opus", "xhigh")
        self.assertRegex(self.log.read_text(),
                         rf"^- {LAUNCHED_AT:%H:%M %Z}: added alpha-unit \(standby\), worktree ")

    def test_permanent_launch_writes_a_permanent_row_and_says_wrap_keeps_it(self) -> None:
        _ = self.successful("alpha", "--permanent")
        self.assertIn(self.unit_row(plan="permanent"), self.doc.read_text())
        self.assertEqual(self.git("log", "-1", "--format=%s"),
                         "production(build-followups): add unit alpha-unit (permanent)")
        self.assertIn("on standby. You are permanent: wrap never retires you. Work only in your worktree",
                      self.opening_prompt())

    def test_the_nightly_review_unit_runs_from_rust_under_the_review_settings(self) -> None:
        folder = Path(self.env["HOME"]) / "rust"
        folder.mkdir()
        config = Path(self.env["HOME"]) / ".claude2/.claude.json"
        _ = config.write_text(json.dumps({
            "oauthAccount": {"emailAddress": "owner@example.com"},
            "projects": {**self.trusted_projects(), str(folder): {"hasTrustDialogAccepted": True}}}))
        result = self.successful("nightly-review", "--permanent")
        self.assertIn("nightly-review-unit started: herdr session attach agents, workspace director, tab nightly-review", result.stdout)
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

    def test_plan_adds_row_commits_worktree_launches_and_records(self) -> None:
        self.pin_launch_clock()
        pushed = self.git("--git-dir", str(self.origin), "rev-parse", "refs/heads/build-followups")
        result = self.successful("alpha", "--plan", "docs/plans/given.md", "--port", "8123",
                                 "--owns", "src/alpha")
        worktree = self.root / "project-alpha"
        row = self.unit_row(port="8123", owns="src/alpha")
        self.assertIn(row, self.doc.read_text())
        self.assertEqual(self.git("show", "--pretty=format:", "--name-only", "HEAD"),
                         "docs/plans/build-followups-production.md")
        self.assertIn("production(build-followups): add unit alpha-unit (plan)",
                      self.git("log", "-1", "--format=%s"))
        # An add pushes nothing: the unit's commit waits for the next merge push, and the unit branch
        # for its first checkpoint push, which sets the upstream.
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
        self.assertEqual(self.opened()[:8], ["--session", SHARED, "workspace", "create", "--label", "director",
                                             "--cwd", str(worktree)])
        self.assertIn(["--session", SHARED, "tab", "rename", "w1:t1",
                       "alpha"], self.herdr_calls())
        self.assertIn(["--session", SHARED, "pane", "rename", "w1:p1", "alpha"], self.herdr_calls())
        self.assertEqual({key: value for key, value in self.pane_environment().items()
                          if key.startswith(("SHOWRUNNER_", "CLAUDE_", "HERDR_")) or key == "ENABLE_TOOL_SEARCH"},
                         {"SHOWRUNNER_UNIT": "build-followups",
            "SHOWRUNNER_UNIT_ID": "alpha-unit", "ENABLE_TOOL_SEARCH": "true", "HERDR_AGENT_PANE": "1",
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
                         rf"^- {LAUNCHED_AT:%H:%M %Z}: added alpha-unit \(plan\), worktree ")
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertIn("alpha-unit started: herdr session attach agents, workspace director, tab alpha", result.stdout)

    def test_a_review_named_unit_opens_unit_direct(self) -> None:
        _ = self.successful("review", "--plan", "docs/plans/given.md")
        self.assertIn(shlex.quote("/unit:direct docs/plans/given.md"), self.launch_command())
        self.assertNotIn("/review:direct", self.launch_command())

    def test_a_doc_that_still_has_the_session_column_gets_a_dash_there(self) -> None:
        _ = self.doc.write_text(self.production_doc(session_column=True), encoding="utf-8")
        _ = self.git("commit", "-am", "an older production doc")
        _ = self.git("push", "origin", "build-followups")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--port", "8123")
        self.assertIn(f"| alpha-unit | docs/plans/given.md | {self.root / 'project-alpha'} | build-followups-alpha "
                      + "| — | 8123 | — |", self.doc.read_text())

    def test_a_rerun_finds_the_unit_by_its_mark_after_its_tab_was_renamed(self) -> None:
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.herdr_overrides[f"--session {SHARED} pane list"] = CommandResponse(code=0, stdout=json.dumps({
            "result": {"panes": [{"pane_id": "w1:p1", "tab_id": "w1:t1", "label": "renamed"}]}}))
        result = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(len(self.servers_started()), 1)
        self.assertEqual(sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls()), 1)
        self.assertIn("alpha-unit started: herdr session attach agents, workspace director, tab alpha", result.stdout)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_prepared_row_launches_without_rewriting_or_committing_it(self) -> None:
        self.commit_prepared_row(self.prepared_row())
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.doc.read_text(), document)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertNotIn("add unit", self.git("log", "-1", "--format=%s"))
        self.assertEqual(self.git("branch", "--show-current", cwd=self.root / "project-alpha"),
                         "build-followups-alpha")
        self.assertEqual(len(self.servers_started()), 1)
        # Adding a unit registers nothing: the showrunner's update timer already names the doc.
        self.assertFalse(self.config.exists())
        self.assertIn("added alpha-unit (plan)", self.log.read_text())

    def test_prepared_row_refuses_mismatched_identity_cells_before_launch(self) -> None:
        cases = (("Plan", self.prepared_row(plan="docs/plans/other.md"),
                  "docs/plans/other.md", "docs/plans/given.md"),
                 ("Branch", self.prepared_row(branch="other-branch"),
                  "other-branch", "build-followups-alpha"),
                 ("Worktree", self.prepared_row(),
                  str(self.root / "other-worktree"), str(self.root / "project-alpha")))
        for cell, row, actual, expected in cases:
            with self.subTest(cell=cell):
                self.commit_prepared_row(row)
                if cell == "Worktree":
                    parent = next(owner for owner in staff.snapshot().find(kind="showrunner")
                                  if isinstance(owner.doc, staff.LiveDoc) and owner.doc.path == self.doc)
                    member = next(row for row in staff.snapshot().find(kind="unit")
                                  if row.name == "alpha" and row.parent == parent.id)
                    _ = staff.set_worktree(member.id, staff.PresentWorktree("natedev", self.root / "other-worktree"), expected=member.worktree)
                document = self.doc.read_text()
                head = self.git("rev-parse", "HEAD")
                result = self.cli("alpha", "--plan", "docs/plans/given.md")
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(len(result.stderr.strip().splitlines()), 1)
                self.assertIn(cell, result.stderr)
                self.assertIn(f"is {actual!r}; expected {expected!r}", result.stderr)
                self.assert_no_launch_change(document, head)

    def test_prepared_row_refuses_different_supplied_port_before_launch(self) -> None:
        self.commit_prepared_row(self.prepared_row())
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md", "--port", "9000")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertIn("Port is '8123'; expected '9000'", result.stderr)
        self.assert_no_launch_change(document, head)

    def test_prepared_row_refuses_different_supplied_owns_before_launch(self) -> None:
        self.commit_prepared_row(self.prepared_row())
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md", "--owns", "src/beta")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertIn("Owns is 'src/alpha'; expected 'src/beta'", result.stderr)
        self.assert_no_launch_change(document, head)

    def test_prepared_standby_row_uses_standby_mode(self) -> None:
        self.commit_prepared_row(self.prepared_row(plan="standby"))
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        _ = self.successful("alpha", "--standby")
        self.assertEqual(self.doc.read_text(), document)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertIn("added alpha-unit (standby)", self.log.read_text())

    def test_other_prepared_row_reserves_backticked_branch(self) -> None:
        self.commit_prepared_row(self.prepared_row(name="beta"))
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertIn("branch or worktree", result.stderr)
        self.assert_no_launch_change(document, head)

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
        self.assertIn("> **Production: build-followups** — unit `alpha-unit`; production doc", content)
        self.assertIn("## Source", content)
        self.assertIn(words, content)
        self.assertIn(f"{LAUNCHED_AT:%Y-%m-%d %H:%M %Z}", content)
        self.assertIn(self.unit_row(plan="docs/plans/build-followups-alpha.md"), self.doc.read_text())
        self.assertEqual(set(self.git("show", "--pretty=format:", "--name-only", "HEAD").splitlines()),
                         {"docs/plans/build-followups-production.md", "docs/plans/build-followups-alpha.md"})
        prompt = self.opening_prompt()
        self.assertIn("Write the full phased plan there", prompt)
        self.assertIn("wait for its approval before you run /unit:direct docs/plans/build-followups-alpha.md", prompt)
        self.assertIn("alpha-unit started: herdr session attach agents, workspace director, tab alpha", result.stdout)
        self.assert_director_flags("opus", "xhigh")

    def test_branch_takes_the_slug_and_the_stub_sits_beside_the_production_doc(self) -> None:
        _ = self.git("checkout", "-b", "init/catalyst")
        self.doc = self.checkout / "docs/hana/tool-based-ui-production.md"
        _ = self.doc.parent.mkdir(parents=True)
        _ = self.doc.write_text(self.production_doc("init/catalyst"), encoding="utf-8")
        _ = self.git("add", "docs/hana/tool-based-ui-production.md")
        _ = self.git("commit", "-m", "production under another merge branch")
        (self.staff.source_root / "notifier/showrunner-build-followups/conf").unlink()
        _ = self.staff.record_production("tool-based-ui", self.doc)
        _ = self.successful("lfo", "--brief", "Add the LFO tool")
        worktree = self.root / "project-lfo"
        self.assertIn(self.unit_row("lfo", branch="tool-based-ui-lfo", worktree=worktree,
                                    plan="docs/hana/tool-based-ui-lfo.md"), self.doc.read_text())
        self.assertEqual(self.git("branch", "--show-current", cwd=worktree), "tool-based-ui-lfo")
        self.assertEqual(set(self.git("show", "--pretty=format:", "--name-only", "HEAD").splitlines()),
                         {"docs/hana/tool-based-ui-production.md", "docs/hana/tool-based-ui-lfo.md"})
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
        self.assertIn("Waiting for remote control", first.stdout + first.stderr)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.log.exists())
        commit = self.git("rev-parse", "HEAD")
        _ = (self.state / "ready").touch()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.git("rev-parse", "HEAD"), commit)
        self.assertEqual(self.doc.read_text().count("| alpha-unit |"), 1)
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

    def test_refuses_plan_without_production_header(self) -> None:
        _ = self.plan.write_text("# No production header\n", encoding="utf-8")
        _ = self.git("add", "docs/plans/given.md")
        _ = self.git("commit", "-m", "remove production header")
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertNotIn("| alpha-unit |", self.doc.read_text())

    def test_refuses_missing_production_field(self) -> None:
        original = self.doc.read_text()
        _ = self.doc.write_text(original.replace("- **Log:** `docs/plans/build-followups-log.md` — git-excluded\n", ""))
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertNotIn("| alpha-unit |", self.doc.read_text())

    def test_a_doc_without_a_user_zone_line_still_adds_a_unit(self) -> None:
        # The zone is the user's setting; a production doc no longer has to name one.
        original = self.doc.read_text()
        _ = self.doc.write_text(original.replace("- **User zone:** America/Los_Angeles — reports use PDT\n", ""))
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertIn("| alpha-unit |", self.doc.read_text())

    def test_refuses_checkout_off_merge_branch(self) -> None:
        _ = self.git("checkout", "-b", "wrong-branch")
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertNotIn("| alpha-unit |", self.doc.read_text())

    def test_refuses_taken_row(self) -> None:
        original = self.doc.read_text()
        changed = original.replace("## Gates", self.unit_row(branch="different") + "\n\n## Gates")
        _ = self.doc.write_text(changed)
        taken = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(taken.returncode, 2, (taken.stdout, taken.stderr))
        self.assertEqual(self.doc.read_text(), changed)
        self.assertEqual(self.servers_started(), [])

    def test_new_work_records_no_scheduled_prompt(self) -> None:
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")

        # herdr records the typed opening prompt under script-prompts; no scheduled prompt is kept.
        self.assertFalse((self.pause_state / "scheduled-prompts").exists())

    def commit_brief_row(self) -> Path:
        stub = self.checkout / "docs/plans/build-followups-alpha.md"
        _ = stub.write_text(
            "# Alpha\n\n> **Production: build-followups** — unit `alpha-unit`\n\n"
            + "## Source\n\nWrite a full plan.\n", encoding="utf-8")
        self.commit_prepared_row(self.unit_row(plan="docs/plans/build-followups-alpha.md"))
        _ = self.git("add", "docs/plans/build-followups-alpha.md")
        _ = self.git("commit", "--amend", "-m", "production(build-followups): add unit alpha-unit (brief)")
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
                         "production(build-followups): add unit alpha-unit (plan)")
        self.assertEqual(self.git("rev-parse", "HEAD^{tree}"), self.git("rev-parse", f"{head}^{{tree}}"))
        self.assertIn("added alpha-unit (plan)", self.log.read_text())
        prompts = [row for row in self.herdr_calls() if row[2:4] == ["agent", "prompt"]]
        marked = unit_lookup.MarkedUnit("alpha-unit",
            unit_lookup.LiveClaude("alpha", "plan-session-id", "/fixture/socket", 4242),
            HerdrPlace("plan-host", "w1:p1"))

        with patch.object(add_unit, "launched_unit", return_value=add_unit.MarkedUnitFound(marked)), \
                patch.object(add_unit, "wait_for_remote_control", return_value=marked):
            retry = self.successful("alpha", "--plan", "docs/plans/build-followups-alpha.md")

        self.assertNotIn("runs its brief", retry.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), plan_head)
        self.assertEqual(stub.read_bytes(), content)
        self.assertEqual(len(self.servers_started()), 1)
        self.assertEqual([row for row in self.herdr_calls() if row[2:4] == ["agent", "prompt"]], prompts)

    def test_brief_row_uses_new_plan_and_commits_its_plan_cell(self) -> None:
        stub = self.commit_brief_row()
        content = stub.read_bytes()
        head = self.git("rev-parse", "HEAD")

        _ = self.successful("alpha", "--plan", "docs/plans/given.md")

        self.assertEqual(self.opening_prompt(), "/unit:direct docs/plans/given.md")
        self.assertEqual(len(self.servers_started()), 1)
        self.assertEqual(self.doc.read_text().count("| alpha-unit |"), 1)
        self.assertIn(self.unit_row(), self.doc.read_text())
        self.assertNotEqual(self.git("rev-parse", "HEAD"), head)
        committed = self.git("show", "HEAD:docs/plans/build-followups-production.md")
        self.assertIn(self.unit_row(), committed)
        self.assertIn(self.unit_row(), (self.root / "project-alpha/docs/plans/build-followups-production.md").read_text())
        self.assertEqual((self.root / "project-alpha/docs/plans/given.md").read_bytes(), self.plan.read_bytes())
        self.assertEqual(stub.read_bytes(), content)
        self.assertIn("added alpha-unit (plan)", self.log.read_text())

    def test_running_brief_refuses_plan_mode_before_any_write(self) -> None:
        stub = self.commit_brief_row()
        worktree = self.root / "project-alpha"
        _ = self.git("worktree", "add", "-b", "build-followups-alpha", str(worktree))
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
        marked = unit_lookup.MarkedUnit("alpha-unit",
            unit_lookup.LiveClaude("alpha", "brief-session-id", "/fixture/socket", 4242),
            HerdrPlace("brief-host", "w1:p1"))

        launches = ((), ("--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766", "--cwd", str(worktree)))
        for plan in ("docs/plans/given.md", "docs/plans/build-followups-alpha.md"):
            for flags in launches:
                with self.subTest(plan=plan, flags=flags):
                    with patch.object(add_unit, "launched_unit", return_value=add_unit.MarkedUnitFound(marked)), \
                            patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()), \
                            patch.object(add_unit, "wait_for_remote_control", return_value=marked) as waited:
                        result = self.cli("alpha", "--plan", plan, *flags)

                    self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                    self.assertIn("unit alpha-unit runs its brief in session brief-host; end that session, then run",
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
        _ = self.git("worktree", "add", "-b", "build-followups-alpha", str(worktree))
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

        with patch.object(add_unit, "launched_unit", return_value=add_unit.NoMarkedUnit()), \
                patch.object(sessions, "session_liveness", return_value=sessions.SessionNotLive()):
            result = self.cli("alpha", "--plan", "docs/plans/missing.md", "--resume", "62ccbd64-f22e-5647-b20e-7899aa39d766",
                              "--cwd", str(worktree))

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("plan docs/plans/missing.md is missing on build-followups or lacks the Production header",
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
        _ = self.git("worktree", "add", "-b", "build-followups-alpha", str(worktree))
        unit_tip = self.git("rev-parse", "HEAD", cwd=worktree)
        marker = worktree / "in-progress.txt"
        _ = marker.write_text("Keep the unit's work.\n", encoding="utf-8")
        plan_path = "docs/plans/approved-later.md"
        late_plan = self.checkout / plan_path
        _ = late_plan.write_text(
            "# Approved plan\n\n> **Production: build-followups** — unit `alpha-unit`\n\n"
            + "## Phases\n\n1. Finish the unit.\n", encoding="utf-8")
        _ = self.git("add", plan_path)
        _ = self.git("commit", "-m", "approve the full plan")
        content = stub.read_bytes()
        self.assertFalse((worktree / plan_path).exists())

        _ = self.successful("alpha", "--plan", plan_path)

        self.assertEqual(self.opening_prompt(), f"/unit:direct {plan_path}")
        self.assertEqual(len(self.servers_started()), 1)
        self.assertIn(self.unit_row(plan=plan_path), self.doc.read_text())
        self.assertIn(self.unit_row(plan=plan_path),
                      self.git("show", "HEAD:docs/plans/build-followups-production.md"))
        self.assertEqual((worktree / plan_path).read_bytes(), late_plan.read_bytes())
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=worktree), unit_tip)
        self.assertEqual(marker.read_text(), "Keep the unit's work.\n")
        self.assertEqual(stub.read_bytes(), content)

    def test_standby_row_refuses_plan_mode_with_the_mode_and_command(self) -> None:
        self.commit_prepared_row(self.unit_row(plan="standby"))
        original = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")

        result = self.cli("alpha", "--plan", "docs/plans/given.md")

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("unit alpha-unit was added in standby mode; plan mode cannot reuse its row", result.stderr)
        self.assertIn("--standby", result.stderr)
        self.assertNotIn("already taken", result.stderr)
        self.assert_no_launch_change(original, head)

    def test_kebab_alias_refuses_existing_branch_and_worktree_row(self) -> None:
        _ = self.successful("mul_add", "--plan", "docs/plans/given.md")
        original = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("mul-add", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(self.doc.read_text(), original)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertNotIn("| mul-add-unit |", self.doc.read_text())

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
        _ = self.doc.write_text(self.doc.read_text().replace(
            "## Gates", self.unit_row() + "\n\n## Gates"), encoding="utf-8")
        original = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(self.doc.read_text(), original)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_another_live_session_leaves_the_launch_alone_and_the_pane_is_read_by_its_id(self) -> None:
        self.herdr_overrides["--session default session list --json"] = CommandResponse(code=0, stdout=json.dumps(
            herdr.SessionListResult(sessions=[herdr.SessionEntry(name="alpha2", running=True, default=False,
                session_dir="/fixture", socket_path="/fixture/socket")])))
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(len(self.servers_started()), 1)
        self.assertIn(["--session", SHARED, "pane", "read", "w1:p1", "--source", "recent", "--lines", "60"], self.herdr_calls())

    def test_remote_control_polling_reads_each_host_through_its_place(self) -> None:
        request = add_unit.UnitLaunch(
            production=production_units.read_production(self.doc), identity=add_unit.UnitIdentity("alpha-unit", "alpha"),
            branch="build-followups-alpha", worktree=self.root / "project-alpha", plan=add_unit.Standby(),
            port=add_unit.OmittedCell(), owns=add_unit.OmittedCell(), session=add_unit.NewSession(),
            launch_kind=add_unit.NewWorkLaunch(), timeout=0,
        )
        for place in (HerdrPlace("native", "w1:p1"),):
            with self.subTest(place=place):
                marked = unit_lookup.MarkedUnit("alpha-unit", unit_lookup.ClaudeNotRunning(), place)
                with patch.object(add_unit, "launched_unit", return_value=add_unit.MarkedUnitFound(marked)), \
                        patch.object(session_pane, "read_place", return_value="/remote-control is active") as read:
                    self.assertEqual(add_unit.wait_for_remote_control(request), marked)
                read.assert_called_once_with(place)
                with patch.object(add_unit, "launched_unit", return_value=add_unit.MarkedUnitFound(marked)), \
                        patch.object(session_pane, "read_place", side_effect=session_pane.HerdrFailure("unreadable")):
                    with self.assertRaisesRegex(RuntimeError, "did not become active\\nunreadable"):
                        _ = add_unit.wait_for_remote_control(request)

    def test_remote_control_is_read_from_the_session_record_after_the_pane_line_scrolls_off(self) -> None:
        request = add_unit.UnitLaunch(
            production=production_units.read_production(self.doc), identity=add_unit.UnitIdentity("alpha-unit", "alpha"),
            branch="build-followups-alpha", worktree=self.root / "project-alpha", plan=add_unit.Standby(),
            port=add_unit.OmittedCell(), owns=add_unit.OmittedCell(), session=add_unit.NewSession(),
            launch_kind=add_unit.NewWorkLaunch(), timeout=0,
        )
        registry = self.root / "registry"
        registry.mkdir()
        place = HerdrPlace("native", "w1:p1")
        live = unit_lookup.LiveClaude("alpha", "62ccbd64-f22e-5647-b20e-7899aa39d766", "/socket", 4242)
        starting = unit_lookup.MarkedUnit("alpha-unit", unit_lookup.ClaudeNotRunning(), place)
        marked = unit_lookup.MarkedUnit("alpha-unit", live, place)
        busy_pane = "⏺ Reading the plan\n  ⎿  Read 400 lines"
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
                # The first lookup finds the mark before the record names the live Claude.
                with patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(registry)}), \
                        patch.object(add_unit, "launched_unit", side_effect=[
                            add_unit.MarkedUnitFound(starting), add_unit.MarkedUnitFound(marked)]), \
                        patch.object(session_pane, "read_place", return_value=busy_pane):
                    if active:
                        self.assertEqual(add_unit.wait_for_remote_control(request), marked)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "did not become active\\n⏺ Reading the plan"):
                            _ = add_unit.wait_for_remote_control(request)

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
        self.assertIn(self.unit_row("mul_add", branch="build-followups-mul-add",
                                    worktree=self.root / "project-mul-add"), self.doc.read_text())
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD",
                                  cwd=self.root / "project-mul-add"), "build-followups-mul-add")


if __name__ == "__main__":
    _ = unittest.main()
