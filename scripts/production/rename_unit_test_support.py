"""Rename one unit of a disposable production against a local bare origin and isolated state."""

from __future__ import annotations

import hashlib
import shutil
import json
import os
import select
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Generator
from datetime import datetime, timezone, tzinfo
from enum import Enum
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, cast, final, override
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts.herdr import fake_herdr
from scripts.direct.wait_for_test_state import wait_until
from scripts.production.cli_fixture import invoke_cli
from scripts.production.rename_test_support import bind_staff_fixture
from scripts.production.staff_test_support import StaffTestCase, unit_membership
from scripts.shutdown import stop_resume
import sessions
from scripts.production import staff
from scripts.lib.atomic_file import write_text_atomic

import fake_showrunner
from scripts.production import fake_unit_host
import rename_unit
import retire_unit
import stall_watch
import tidy_wrapped
import pane_record
import progress_history

if TYPE_CHECKING:
    from scripts.style import style_ledger
    from scripts.shutdown import shutdown_record
else:
    import style_ledger
    # rename_unit puts the shutdown folder on the path.
    import shutdown_record

SCRIPT = Path(__file__).with_name("rename_unit.py")
SLUG = "demo"

# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300


@final
class RetainedDirector:
    """Record shell commands and process generations in the existing pane-shell fixture."""

    def __init__(self, case: UnitRenameFixture) -> None:
        import herdr
        import unit_lookup
        import unit_move
        lookup = unit_lookup.marked_unit
        lookup_all = unit_lookup.marked_units
        observed = unit_lookup.observed_unit_marks
        inside = unit_move.processes_inside
        session_liveness = sessions.session_liveness
        self.case = case
        entries = pane_record.read("agents")
        prior = entries.get("w1:p1")
        worktree = Path(prior["cwd"]).parent if prior else case.rust / "claude-demo-alpha"
        unit = prior["environment"]["SHOWRUNNER_UNIT_ID"] if prior else "alpha-unit"
        slug = prior["environment"]["SHOWRUNNER_UNIT"] if prior else SLUG
        self.pane = fake_unit_host.PaneShellFixture(case, worktree,
            Path(case.env["HERDR_PROC"]), live=True, slug=slug, unit=unit, env=case.env)
        self.starts: list[list[str]] = []
        self.stops: list[str] = []
        self.orders: list[str] = []
        self.generation = 1
        self.pane_title = "alpha"
        self.tab_title = "alpha"
        self.ids: tuple[tuple[str, str, str], ...] = (("w1:p1", "terminal-alpha", "w1:t1"),
                    ("w1:p2", "terminal-neighbour", "w1:t2"))
        run = herdr.Herdr.run
        order = herdr.Herdr.order_tabs

        def ordered(client: herdr.Herdr, workspace: str, saved: herdr.SavedLayout) -> dict[str, str]:
            self.orders.append(workspace)
            return order(client, workspace, saved)

        def command(client: herdr.Herdr, *arguments: str) -> str:
            reply = run(client, *arguments)
            if arguments[:2] == ("pane", "rename"):
                self.pane_title = arguments[3]
            elif arguments[:2] == ("tab", "rename"):
                self.tab_title = arguments[3]
            elif arguments[:2] == ("pane", "run"):
                argv = shlex.split(arguments[3])
                self.starts.append(argv)
                self.generation += 1
                entry = pane_record.read("agents")["w1:p1"]
                self.pane.unit = entry["environment"]["SHOWRUNNER_UNIT_ID"]
                self.pane.worktree = Path(entry["cwd"])
                self.pane.found = unit_lookup.MarkedUnit(self.pane.unit,
                    unit_lookup.LiveClaude(entry["name"], entry["session_id"], "/fixture/socket", self.pane.director),
                    self.pane.place)
                fake_unit_host.process(self.pane.environment, self.pane.shell, 991000, ["zsh"],
                    self.pane.marks(), session=self.pane.shell, cwd=self.pane.worktree)
                self.pane.parent(self.pane.shell)
            if arguments[:2] in (("pane", "rename"), ("tab", "rename"), ("pane", "run")):
                self.publish()
            return reply

        def liveness(identity: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive | sessions.SessionLivenessUnknown:
            found = self.pane.found
            entry = pane_record.read("agents").get("w1:p1")
            if entry is not None and identity == entry["session_id"]:
                return (sessions.LiveSessionProcess(self.pane.director) if found is not None
                        and isinstance(found.claude, unit_lookup.LiveClaude) else sessions.SessionNotLive())
            return session_liveness(identity)

        _ = case.enterContext(mock.patch.object(herdr.Herdr, "run", command))
        _ = case.enterContext(mock.patch.object(herdr.Herdr, "order_tabs", ordered))
        _ = case.enterContext(mock.patch.object(herdr, "shared_session", return_value="agents"))
        _ = case.enterContext(mock.patch.object(case, "stop_session", side_effect=self.stop))
        _ = case.enterContext(mock.patch.object(sessions, "session_liveness", side_effect=liveness))
        def generation(pid: int) -> str:
            case.assertEqual(pid, self.pane.director)
            return str(self.generation)

        from scripts.whoami import account
        _ = case.enterContext(mock.patch.object(sessions, "process_start", side_effect=generation))
        _ = case.enterContext(mock.patch.object(account, "process_config_dir",
            return_value=account.ConfiguredClaudeFolder(case.home / ".claude4")))
        _ = case.enterContext(mock.patch.object(stop_resume, "_detach"))
        def marked(slug: str, unit: str) -> unit_lookup.MarkedUnit | None:
            return self.pane.marked_unit(slug, unit) if (slug, unit) == (self.pane.slug, self.pane.unit) else lookup(slug, unit)
        def marked_units(slug: str) -> dict[str, unit_lookup.MarkedUnit]:
            return {**lookup_all(slug), **self.pane.marked_units(slug)}
        def marks() -> list[tuple[str, unit_lookup.MarkedUnit]]:
            others = [pair for pair in observed() if pair[1].place != self.pane.place]
            found = self.pane.found
            return [*others, (self.pane.slug, found)] if found is not None else others
        def processes(path: Path) -> list[int]:
            return sorted(set(inside(path)) | set(self.pane.inside_pids(path)))
        _ = case.enterContext(mock.patch.object(unit_lookup, "marked_unit", side_effect=marked))
        _ = case.enterContext(mock.patch.object(unit_lookup, "marked_units", side_effect=marked_units))
        _ = case.enterContext(mock.patch.object(unit_lookup, "observed_unit_marks", side_effect=marks))
        _ = case.enterContext(mock.patch.object(unit_move, "processes_inside",
            side_effect=processes))
        self.configure(slug, unit, worktree, name=prior["name"] if prior else "alpha",
                       session_id=prior["session_id"] if prior else "sid-alpha", live=True)

    def configure(self, slug: str, unit: str, worktree: Path, *, name: str = "", live: bool = False,
                  session_id: str = "sid-alpha") -> None:
        import unit_lookup
        row = unit_membership(slug, unit)
        self.pane.slug, self.pane.unit, self.pane.worktree = slug, unit, worktree
        self.pane_title = self.tab_title = name or unit.removesuffix("-unit")
        config = self.case.home / ".claude4"
        worktree.mkdir(parents=True, exist_ok=True)
        (worktree / "sub").mkdir(exist_ok=True)
        self.case.write(config / ".claude.json", json.dumps({"projects": {
            str(worktree): {"hasTrustDialogAccepted": True},
            str(worktree / "sub"): {"hasTrustDialogAccepted": True}}}))
        pane_record.write("agents", pane_record.PaneLaunch(pane_id="w1:p1", session_id=session_id,
            config_dir=str(config), cwd=str(worktree / "sub"), name=self.pane_title, model="sonnet",
            remote_control=True, environment={"SHOWRUNNER_UNIT": slug, "SHOWRUNNER_UNIT_ID": unit, "STAFF_ID": row.id},
            flags_read=True, ended=False, written_at="2026-10-31T11:00:00+00:00"))
        self.pane.found = unit_lookup.MarkedUnit(unit,
            unit_lookup.LiveClaude(self.pane_title, session_id, "/fixture/socket", self.pane.director)
            if live else unit_lookup.ClaudeNotRunning(), self.pane.place)
        self.pane.parent(self.pane.shell)
        if not live:
            self.pane.stopped()
        self.publish()

    def reset(self) -> None:
        """Restore process observations after saved_fixture restores the isolated files."""
        import unit_lookup
        self.pane_title = self.tab_title = "alpha"
        self.pane.unit, self.pane.worktree = "alpha-unit", self.case.rust / "claude-demo-alpha"
        self.pane.parent(self.pane.shell)
        self.pane.found = unit_lookup.MarkedUnit("alpha-unit",
            unit_lookup.LiveClaude("alpha", "sid-alpha", "/fixture/socket", self.pane.director), self.pane.place)
        self.starts.clear()
        self.stops.clear()
        self.orders.clear()

    def stop(self, identity: str) -> stop_resume.Stopped:
        self.stops.append(identity)
        self.pane.stopped()
        self.publish()
        return stop_resume.Stopped(identity, self.pane.director)

    def publish(self) -> None:
        import unit_lookup
        live = self.pane.found
        conversation = (live.claude.session_id if live is not None and isinstance(live.claude, unit_lookup.LiveClaude) else None)
        path = Path(self.case.env["FAKE_HERDR_RESPONSES"])
        responses = cast(dict[str, object], json.loads(path.read_text()))
        responses["--session agents pane process-info --pane w1:p1"] = {"code": 0,
            "stdout": json.dumps({"result": {"process_info": {"shell_pid": self.pane.shell,
                "foreground_process_group_id": self.pane.director if conversation else self.pane.shell,
                "foreground_processes": [{"name": "claude", "pid": self.pane.director}] if conversation else [{"name": "zsh"}]}}})}
        for arguments, key, rows in (
            ("pane list", "panes", [{"pane_id": pane, "terminal_id": terminal, "tab_id": tab,
                "workspace_id": "w1", "label": self.pane_title if pane == "w1:p1" else "neighbour",
                "agent_session": {"value": conversation} if conversation and pane == "w1:p1" else None}
                for pane, terminal, tab in self.ids]),
            ("tab list --workspace w1", "tabs", [{"tab_id": tab,
                "label": self.tab_title if pane == "w1:p1" else "neighbour"} for pane, _, tab in self.ids]),
            ("workspace list", "workspaces", [{"workspace_id": "w1", "label": self.pane.slug, "active_tab_id": "w1:t1"}]),
        ):
            responses[f"--session agents {arguments}"] = {"code": 0, "stdout": json.dumps({"result": {key: rows}})}
        content = json.dumps(responses)
        if path.read_text() != content:
            self.case.write(path, content)


class FixtureClock(datetime):
    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None) -> datetime:
        return datetime(2026, 10, 31, 11, 0, tzinfo=timezone.utc).astimezone(tz)


class DirectorFixtureState(Enum):
    ABSENT = "no retained director configured"


class UnitRenameFixture(StaffTestCase):

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.home: Path = Path()
        self.rust: Path = Path()
        self.checkout: Path = Path()
        self.origin: Path = Path()
        self.doc: Path = Path()
        self.state: Path = Path()
        self.env: dict[str, str] = {}
        self.director_state: RetainedDirector | DirectorFixtureState = DirectorFixtureState.ABSENT

    retained_by_default: ClassVar[bool] = True
    @property
    def director(self) -> RetainedDirector:
        assert isinstance(self.director_state, RetainedDirector), "configure the retained director first"
        return self.director_state

    @director.setter
    def director(self, value: RetainedDirector) -> None:
        self.director_state = value

    fixture_root: ClassVar[Path]
    fixture_snapshot: ClassVar[Path]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory(prefix="rename-unit-test-")
        cls.addClassCleanup(directory.cleanup)
        cls.fixture_root = Path(directory.name) / "case"
        cls.fixture_snapshot = Path(directory.name) / "snapshot"
        builder = cls()
        builder.bind_fixture(cls.fixture_root)
        builder.build_fixture()
        _ = shutil.copytree(cls.fixture_root, cls.fixture_snapshot)
        shutil.rmtree(cls.fixture_root)

    @override
    def setUp(self) -> None:
        super().setUp()
        # Git worktrees and production docs embed absolute paths. Each independent
        # copy returns to the seed's original path, with its own refs and origin.
        _ = shutil.copytree(self.fixture_snapshot, self.fixture_root)
        self.addCleanup(shutil.rmtree, self.fixture_root)
        self.bind_fixture(self.fixture_root)
        bind_staff_fixture(self, self.env, {SLUG: ("alpha-unit", "mul_add-unit")})
        # These modules also bind paths at import time; environment changes alone
        # do not isolate their journals, locks, or stall-watch state.
        for module, name, value in (
            (rename_unit, "datetime", FixtureClock),
            (pane_record, "datetime", FixtureClock),
            (progress_history, "SESSION_ROOTS", (self.root / "sessions",)),
            (pane_record, "RECORD_DIR", self.home / ".local/state/herdr-panes"),
            (pane_record, "PROC", self.root / "proc"),
            (tidy_wrapped, "STATE_DIR", self.state),
            (tidy_wrapped, "NOTIFIER_STATE_DIR", self.root / "state/notifier"),
            (stall_watch, "STATE_DIR", self.root / "state/stall-watch"),
            (stall_watch, "SESSIONS_DIR", self.root / "state/sessions"),
            (stall_watch, "PROJECTS_DIR", self.home / ".claude/projects"),
        ):
            _ = self.enterContext(mock.patch.object(module, name, value))

        if self.retained_by_default:
            self.director = RetainedDirector(self)
            _ = self.director.stop("sid-alpha")
            self.director.stops.clear()

    def bind_fixture(self, root: Path) -> None:
        self.root = root
        self.home = self.root / "home"
        self.rust = self.home / "rust"
        self.checkout = self.rust / "claude-demo-trunk"
        self.origin = self.root / "origin.git"
        self.doc = self.checkout / "docs/plans/demo-production.md"
        self.state = self.root / "state/showrunner"
        config = self.home / ".claude"
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update({
            "UNIT_WORKERS_STATE": str(self.root / "state/unit-workers"),
            "RETIRE_UNIT_RUN_ROOT": str(self.root / "runs"),
            "HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(config),
            "XDG_CONFIG_HOME": str(self.home / ".config"), "XDG_STATE_HOME": str(self.root / "state"),
            "NOTIFIER_STATE_DIR": str(self.root / "state/notifier"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "state/sessions"),
            "SHOWRUNNER_STATE_DIR": str(self.state),
            "STALL_WATCH_STATE_DIR": str(self.root / "state/stall-watch"),
            # No waits by default: a test that needs one sets it.
            "RENAME_UNIT_STOP_WAIT": "0", "RENAME_UNIT_LIVE_WAIT": "0",
            "REVIEW_POOL_STATE": str(self.root / "state/review-pool"),
            "DESIGN_ROLES_STATE": str(self.root / "state/astra/design-roles"),
            "DESIGN_OWNERSHIP_STATE": str(self.root / "state/astra/design-ownership"),
            "SHOWRUNNERS_CONFIG": str(config / "showrunners.json"),
            "UNIT_ACTIVE_DIR": str(self.root / "active"),
            "UNIT_HISTORY_DIR": str(self.root / "history"),
            "PROGRESS_SESSION_ROOTS": str(self.root / "sessions"),
            "PHASE_TABLE_VAULT": str(self.root / "vault/showrunners"),
            "HERDR_BIN": str(SCRIPT.parents[1] / "herdr/fake_herdr.py"),
            "HERDR_PROC": str(self.root / "proc"),
            "HERDR_PANE_RECORD_DIR": str(self.home / ".local/state/herdr-panes"),
            "HERDR_PLATFORM": "linux",
            "FAKE_HERDR_LOG": str(self.root / "herdr.jsonl"),
            "FAKE_HERDR_RESPONSES": str(self.root / "herdr-responses.json"),
            "CLAUDE_CODE_SESSION_ID": "", "USER_ZONE": "UTC", "UNIT_SYNC_WRITES": "0",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
        })

    def build_fixture(self) -> None:
        for path in (self.checkout, self.home / ".claude", self.root / "state/sessions", self.root / "scratch"):
            path.mkdir(parents=True)
        self.write(self.root / "herdr-responses.json",
                   json.dumps({"default": {"stdout": '{"sessions": []}', "code": 0}}))
        _ = self.git("init", "--bare", str(self.origin))
        _ = self.git("init", "-b", SLUG)
        for key, value in (("user.name", "Unit Rename Test"), ("user.email", "rename@example.invalid"),
                           ("gc.auto", "0"), ("maintenance.auto", "false")):
            _ = self.git("config", key, value)
        _ = self.git("remote", "add", "origin", str(self.origin))
        self.write(self.doc, self.production_doc())
        self.write(self.checkout / "docs/plans/alpha.md",
                   "# alpha\n\n> **Production:** demo (alpha-unit)\n\nalpha-unit history stays in prose.\n")
        self.write(self.checkout / "docs/plans/mul-add.md", "# mul_add\n\n> **Production:** demo (mul_add-unit)\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "fixture")
        _ = self.git("push", "-u", "origin", SLUG)
        for stem in ("alpha", "mul-add"):
            _ = self.git("worktree", "add", "-b", f"demo-{stem}", str(self.rust / f"claude-demo-{stem}"), SLUG)
            _ = self.git("push", "origin", f"demo-{stem}")
        self.write(self.root / "state/notifier/showrunner-demo/conf",
                   "EVERY=15\nTARGET=session:test-showrunner\nCHECK="
                   + shlex.join(["zsh", "/scripts/production_check.sh", str(self.doc)]) + "\n")
        # A merge-branch commit the unit has not taken yet: the rename merges it first.
        self.write(self.checkout / "NOTES.md", "after the unit branched\n")
        _ = self.git("add", "NOTES.md")
        _ = self.git("commit", "-m", "merge(demo-beta): later work")
        _ = self.git("push", "origin", SLUG)
        self.seed_state()

    def production_doc(self) -> str:
        return "\n".join([
            "# Production — demo", "", "## Production Context", "", "- **Merge branch:** `demo`",
            f"- **Showrunner checkout:** `{self.checkout}`", "- **Log:** `production.log`",
            f"- **Proposals:** `{self.root / 'issues'}`", "",
            "## Units", "", "| Unit | Plan | Worktree | Branch |", "| --- | --- | --- | --- |",
            f"| alpha-unit | docs/plans/alpha.md | `{self.rust / 'claude-demo-alpha'}` | demo-alpha |",
            f"| mul_add-unit | docs/plans/mul-add.md | `{self.rust / 'claude-demo-mul-add'}` | demo-mul-add |",
            "", "## Hub files", "", "| File | Owner unit | Other units that touch it |", "| --- | --- | --- |",
            "| `docs/alpha-unit.md` | alpha-unit | beta-unit (after alpha-unit Phase 2) |",
            "", "## Gates", "", "| Gate | Waiting | Waits on | Clears when |", "| --- | --- | --- | --- |",
            "| alpha-unit gate | beta-unit | alpha-unit Phase 2 | alpha-unit merges |",
            "| old gate | alpha-unit | beta-unit | cleared 10-01 when alpha-unit merged |",
            "", "## Production rules", "", "History mentions alpha-unit and stays.", "",
        ])

    def seed_state(self) -> None:
        old = self.rust / "claude-demo-alpha"
        self.write(self.stall_path("alpha-unit"), '{"stretch": 17}\n')
        self.write(self.root / "state/review-pool/demo/pool.json", json.dumps({"generation": 1, "reviewers": {
            name: {"type": "codex", "thread_id": f"thread-{name}", "prime_hash": "h",
                   "lease": {"unit": unit, "ordinal": 3}, "busy_pid": None, "reviews": 2}
            for name, unit in (("r1", "alpha-unit"), ("r2", "mul_add-unit"))}}))
        run = self.root / "history/runs/run-1.jsonl"
        self.write(run, "\n".join(json.dumps(event) for event in (
            {"event_type": "run_started", "run_started_at": 1, "working_dir": str(old),
             "worktree": old.name, "branch": "demo-alpha", "plan_doc": f"{old}/docs/plans/alpha.md"},
            {"event_type": "phase_started", "phase_instance_id": "p1", "phase_id": "1"},
            {"event_type": "phase_finished", "phase_instance_id": "p1", "phase_id": "1"})) + "\n")
        self.write(self.root / "sessions/s1/progress_history_state.json", json.dumps({
            "history_file": str(run), "working_dir": str(old), "worktree": old.name, "branch": "demo-alpha",
            "plan_doc": f"{old}/docs/plans/alpha.md"}))
        for name, plan in (("mine", "docs/plans/alpha.md"), ("other-production", "docs/plans/elsewhere.md")):
            self.write(self.root / f"issues/{name}.md", "\n".join([
                "---", "status: open", "tags: [proposal]", "unit: alpha-unit", f"plan: {plan}",
                "date_modified: 2026-10-01", "---", "", "A proposal.", ""]))
        for folder, production in (("demo-sr", "demo"), ("other-sr", "other")):
            self.write(self.root / f"vault/showrunners/{folder}/alpha-unit.md", "\n".join([
                "---", "phase_table: true", f"production: {production}", "unit: alpha-unit", "---", "",
                "# alpha-unit", ""]))

    def write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, content)

    def git(self, *arguments: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", *arguments], cwd=cwd or self.checkout, env=self.env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, (arguments, result.stdout, result.stderr))
        return result.stdout.strip()

    def stall_path(self, unit: str) -> Path:
        digest = hashlib.sha256(f"{SLUG}\0{unit}".encode()).hexdigest()
        return self.root / "state/stall-watch" / f"{digest}.json"

    def rename(self, action: str, old: str = "alpha", new: str = "gamma", *,
               refuse: str = "") -> subprocess.CompletedProcess[str]:
        arguments = [action, SLUG, old, new, "--scratch", str(self.root / "scratch")]
        return self.invoke(arguments, refuse=refuse)

    def invoke(self, arguments: list[str], *, cwd: Path | None = None,
               refuse: str = "") -> subprocess.CompletedProcess[str]:
        if self.retained_by_default:
            entries = pane_record.read("agents")
            entry = entries.get("w1:p1")
            if entry is not None and self.director.pane.unit != entry["environment"].get("SHOWRUNNER_UNIT_ID"):
                self.director.configure(entry["environment"]["SHOWRUNNER_UNIT"],
                    entry["environment"]["SHOWRUNNER_UNIT_ID"], Path(entry["cwd"]).parent,
                    name=entry["name"], session_id=entry["session_id"])
        original_run = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)

        def run(command: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            # Keep the real git wrapper's error handling and every other real
            # command. Only the requested git failure and the fake CLI are local.
            if refuse and command[:1] == ["git"] and command[3:] == refuse.split(" "):
                return subprocess.CompletedProcess(command, 1, "", "simulated failure")
            if command[0] == self.env["HERDR_BIN"]:
                environment = cast(dict[str, str], kwargs.get("env") or dict(os.environ))
                result = invoke_cli(fake_herdr, lambda: fake_herdr.main(command[1:]), command[1:],
                                    environment, Path(cast(str, kwargs.get("cwd") or os.getcwd())))
                return subprocess.CompletedProcess(command, result.returncode, result.stdout, result.stderr)
            return original_run(command, *args, **kwargs)

        # These values are normally bound at interpreter startup. Rebind them for
        # each invocation, including tests that change the shutdown stand-in.
        with (self.shutdown_boundary(),
              mock.patch.object(rename_unit, "LIVE_WAIT_SECONDS", float(self.env["RENAME_UNIT_LIVE_WAIT"])),
              mock.patch.object(retire_unit, "MAC_CLAIMS", self.home / ".local/state/mac-test/claims"),
              mock.patch.object(subprocess, "run", run)):
            return invoke_cli(rename_unit, lambda: rename_unit.main(arguments), arguments,
                              self.env, cwd or self.checkout, production_state=True)

    @contextmanager
    def shutdown_boundary(self) -> Generator[None]:
        """Use the same recording stop fake for in-process and child-process apply."""
        with mock.patch.object(stop_resume, "stop", side_effect=self.stop_session):
            yield

    def stop_session(self, identity: str) -> stop_resume.Stopped:
        return stop_resume.Stopped(identity, 1)

    def start_apply(self) -> subprocess.Popen[str]:
        """apply in the background, so the test can act as the shutdown path while it waits."""
        bootstrap = ("import sys; from pathlib import Path; "
                     + f"sys.path.insert(0, {str(SCRIPT.parents[2])!r}); "
                     + "from scripts.production.rename_unit_test_support import apply_in_fixture; "
                     + "raise SystemExit(apply_in_fixture(Path(sys.argv[1])))")
        process = subprocess.Popen([sys.executable, "-c", bootstrap, str(self.root)],
                                   cwd=self.checkout, env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(process.kill)
        return process

    def dead_server_watcher(self, worktree: Path, folder: Path) -> subprocess.Popen[str]:
        """A literal sh watcher of another worktree's run, blocked until signalled after its server dies."""
        server = subprocess.Popen([sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(server.wait, timeout=TEST_HANG_GUARD_SECS)
        self.addCleanup(server.kill)
        assert server.stdin is not None and server.stdout is not None
        self.addCleanup(server.stdin.close)
        self.addCleanup(server.stdout.close)
        _ = self.wait_for_output(server, "ready")
        folder.mkdir(parents=True)
        self.write(folder / "progress_history_state.json", json.dumps({"working_dir": str(worktree.parent / "another-worktree")}))
        self.write(folder / "mesh_server.json", json.dumps({"pid": server.pid, "port": 51101}))
        # The sleep stand-in reports entry and blocks on a pipe. No elapsed time decides when
        # the server dies or whether the watcher releases the worktree.
        script = ('sleep() { printf "sleeping\\n"; read -r wake; }; '
                  'while kill -0 "$1" 2>/dev/null; do sleep "$5"; '
                  '"$4" "$0" idle-stop --session-dir "$3" --pid "$1" --port "$2" && exit 0; done')
        watcher = subprocess.Popen(["sh", "-c", script,
            str(SCRIPT.parents[1] / "agents/codex_mesh.py"), str(server.pid), "51101", str(folder),
            sys.executable, "300"], cwd=worktree, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, start_new_session=True)
        self.addCleanup(watcher.wait, timeout=TEST_HANG_GUARD_SECS)
        self.addCleanup(watcher.kill)
        assert watcher.stdin is not None and watcher.stdout is not None
        self.addCleanup(watcher.stdin.close)
        self.addCleanup(watcher.stdout.close)
        _ = self.wait_for_output(watcher, "sleeping")
        server.kill()
        _ = server.wait(timeout=TEST_HANG_GUARD_SECS)
        return watcher

    def wait_for(self, ready: Callable[[], bool]) -> None:
        """Wait for a stand-in's file event; elapsed time is only a hang guard."""
        wait_until(ready, self.root, timeout=TEST_HANG_GUARD_SECS)

    def wait_for_output(self, process: subprocess.Popen[str], expected: str) -> str:
        """Read through an event without buffering output needed by communicate()."""
        assert process.stdout is not None
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        output = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, f"timed out waiting for {expected!r}")
            readable, _, _ = select.select([process.stdout], [], [], remaining)
            self.assertTrue(readable, f"timed out waiting for {expected!r}")
            chunk = os.read(process.stdout.fileno(), 1)
            if not chunk:
                stdout, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.fail(f"stdout closed before {expected!r}: {output!r}{stdout}; {stderr}")
            output.extend(chunk)
            if chunk == b"\n" and expected in output.decode():
                return output.decode()

    def assert_ok(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_refused(self, result: subprocess.CompletedProcess[str], text: str) -> None:
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(text, result.stderr)

    def journal(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads((self.state / SLUG / "rename_unit.json").read_text()))

    def snapshot(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file() and path != self.root / "herdr.jsonl"}

    def style_production(self) -> tuple[Path, Path, Path]:
        worktree = self.rust / "demo-names"
        _ = self.git("worktree", "add", "-b", "demo-names", str(worktree), SLUG)
        _ = self.git("push", "origin", "demo-names")
        _ = self.git("rm", str(self.doc.relative_to(self.checkout)))
        _ = self.git("commit", "-m", "style production excludes its control files")
        _ = self.git("push", "origin", SLUG)
        plan = Path("docs/style-production/units/names-unit.md")
        self.write(self.checkout / ".git/info/exclude", "docs/plans/demo-production.md\ndocs/style-production/\n")
        text = self.production_doc().replace(
            "- **Log:**", f"- **Kind:** style\n- **Repository:** `{self.checkout}`\n- **Log:**")
        text = text.replace(
            f"| alpha-unit | docs/plans/alpha.md | `{self.rust / 'claude-demo-alpha'}` | demo-alpha |",
            f"| names-unit | `{plan}` | `{worktree}` | demo-names |")
        self.write(self.doc, text)
        parent = next(row for row in staff.snapshot().find(kind="showrunner") if row.name == SLUG)
        _ = staff.ensure("unit", name="names", parent=parent.id)
        for directory in (self.checkout, worktree):
            self.write(directory / plan, "# Names\n\n> **Production:** demo (names-unit)\n"
                       + "> **Standing:** style_plan.py\n\nnames-unit history stays.\n")
        self.write(self.root / "issues/style.md", "---\nstatus: open\ntags: [proposal, style]\n"
                   + "proposed_by: demo\nunit: names-unit\ndecision: approved\n"
                   + "date_modified: 2026-10-01\n---\nA style proposal.\n")
        self.env["STYLE_LEDGER_STATE_DIR"] = str(self.root / "state/style")
        database = self.root / "state/style" / self.checkout.name / "ledger.sqlite"
        with mock.patch.dict(os.environ, self.env):
            with style_ledger.Ledger(self.checkout, database) as ledger:
                ledger.wait_for_sync("names-unit")
        with closing(sqlite3.connect(database)) as connection, connection:
            _ = connection.executemany(
                "INSERT INTO tickets(id,unit,kind,family,rule,crate,files,sites,rules,rule_blobs,state,"
                + "branch,checkpoint,created_at,updated_at) VALUES (?,?,'read','names','','.','[]','[]','[]','{}',?,"
                + "'demo-names','','2026-10-01','2026-10-01')",
                [("names-unit-1", "names-unit", "merged"), ("names-unit-2", "names-unit", "assigned"),
                 ("names-unit-3", "names-unit", "queued")],
            )
        self.director.configure(SLUG, "names-unit", worktree)
        return worktree, plan, database

    def seed_record(self, progress: Literal["waiting", "ready", "stopped"] = "stopped", name: str = "alpha", host_session: str = "demo-alpha", *,
                    unit: str = "alpha-unit", slug: str = SLUG) -> Path:
        """A stopped unit's shutdown record, from before the conductor was removed."""
        old = self.rust / "claude-demo-alpha"
        if self.retained_by_default and slug == SLUG and unit == "alpha-unit":
            self.director.configure(slug, unit, old, name=name)
        saved = fake_unit_host.stopped_record(self.home / ".claude", old,
            name=name, cwd=old / "sub", production=slug, unit=unit, doc=self.doc,
            host_session=host_session, branch="demo-alpha", plan=old / "docs/plans/alpha.md", progress=progress)
        saved["codex_seats"] = [{"run_dir": str(self.root / "runs/r1"), "seat": "impl", "role": "impl",
                                 "thread_id": "thread-impl", "turn_live": True, "launcher_command": [
                                     "bash", "/scripts/direct/implement.sh", "--to", "impl", str(self.root / "runs/r1"),
                                     str(old), "/runs/r1/prompt.md", "impl", "task", "impl", "working", "0", "impl"]}]
        self.write(self.root / "runs/r1/impl.resume-prompt.md", "Continue the task you were on.\n")
        with mock.patch.dict(os.environ, self.env):
            self.assertTrue(shutdown_record.create_session(saved))
        log = self.root / "shutdown-calls.jsonl"
        self.write(log, "")
        import herdr
        start_session = stop_resume._start_session  # pyright: ignore[reportPrivateUsage]
        def record_start(tab: herdr.TabSession, entry: pane_record.PaneLaunch, account: str, message: str) -> None:
            with log.open("a") as calls:
                _ = calls.write(json.dumps(["resume", entry["session_id"]]) + "\n")
            start_session(tab, entry, account, message)
        _ = self.enterContext(mock.patch.object(stop_resume, "_start_session", side_effect=record_start))
        return log

    def record(self) -> dict[str, object]:
        directory = self.home / ".local/state/shutdown/owner@example.com/sessions"
        return cast(dict[str, object], json.loads((directory / "sid-alpha.json").read_text()))

    def record_live_launch(self) -> None:
        self.director.configure(SLUG, "alpha-unit", self.rust / "claude-demo-alpha", live=True)

    def mark_live(self, unit: str, session_id: str, name: str = "") -> Callable[[], None]:
        """A live Claude in a herdr session marked as `unit` of this production; the result ends it.

        The session and its pane label are called `name`, the unit's stem when it is empty.
        """
        name = name or unit.removesuffix("-unit")
        if self.retained_by_default and unit == self.director.pane.unit:
            worktree = self.home / "worktrees" / unit.removesuffix("-unit")
            if not worktree.exists():
                worktree = self.rust / "claude-demo-alpha"
            self.director.configure(SLUG, unit, worktree, name=name, live=True, session_id=session_id)
            def stopped() -> None:
                self.director.pane.stopped()
                self.director.publish()
            return stopped
        row = unit_membership(SLUG, unit)
        held = fake_showrunner.write_session(self.root / "state/sessions", name, session_id)
        self.addCleanup(held.close)
        fake_unit_host.write(self.env, [fake_unit_host.HostedUnit(
            f"demo-{unit}", "w1:p1", {"SHOWRUNNER_UNIT": SLUG, "SHOWRUNNER_UNIT_ID": unit,
                                    "STAFF_ID": row.id}, label=name)])
        fake_unit_host.place_process(self.env, os.getpid(), "w1:p1", exec_shell=True)

        def end() -> None:
            held.close()
            fake_unit_host.write(self.env, [])
        return end



def apply_in_fixture(root: Path) -> int:
    """Run apply with real process output and the existing fixture's stop boundary."""
    fixture = UnitRenameFixture()
    fixture.bind_fixture(root)
    fixture.env = dict(os.environ)
    fixture.director = RetainedDirector(fixture)
    with fixture.shutdown_boundary():
        return rename_unit.main(["apply", SLUG, "alpha", "gamma", "--scratch", str(root / "scratch")])
