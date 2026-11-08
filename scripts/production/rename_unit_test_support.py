"""Use private staff records and Git repositories for unit rename tests."""
from __future__ import annotations

from scripts.shutdown import remote

import json
import os
import shutil
import shlex
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable, Generator, Iterable
from contextlib import AbstractContextManager, closing, contextmanager
from pathlib import Path
from typing import ClassVar, Literal, cast, override
from unittest import mock


from scripts.herdr import fake_herdr
from scripts.lint.memory_test_support import config_environment
from scripts.production.cli_fixture import invoke_cli
from scripts.production.rename_test_support import bind_staff_fixture
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id, StaffTestCase
from scripts.production import staff, unit_config, fake_unit_host
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import rename
from scripts.shutdown import pane_record
from scripts.style import style_ledger

SCRIPT = Path(__file__).with_name("rename.py")
SLUG = "demo"


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
        bind_staff_fixture(self, self.env, {SLUG: ("alpha", "mul_add")},
                           locations={(SLUG, "alpha"): self.rust / "claude-demo-alpha",
                                      (SLUG, "mul_add"): self.rust / "claude-demo-mul-add"},
                           plans={(SLUG, "alpha"): Path("docs/plans/alpha.md"),
                                  (SLUG, "mul_add"): Path("docs/plans/mul-add.md")})
        _ = self.enterContext(mock.patch.object(pane_record, "RECORD_DIR", self.home / ".local/state/herdr-panes"))
        _ = self.enterContext(mock.patch.object(pane_record, "PROC", self.root / "proc"))

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
            **config_environment(self.root),
            "UNIT_WORKERS_STATE": str(self.root / "state/unit-workers"),
            "RETIRE_UNIT_RUN_ROOT": str(self.root / "runs"),
            "UNIT_RUN_ROOT": str(self.root / "sessions"),
            "HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(config),
            "CODEX_HOME": str(self.root / "codex-home"),
            "XDG_CONFIG_HOME": str(self.home / ".config"), "XDG_STATE_HOME": str(self.root / "state"),
            "NOTIFIER_STATE_DIR": str(self.root / "state/notifier"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "state/sessions"),
            "SHOWRUNNER_STATE_DIR": str(self.state),
            # No waits by default: a test that needs one sets it.
            "RENAME_UNIT_STOP_WAIT": "0", "RENAME_UNIT_LIVE_WAIT": "0",
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
                   "# alpha\n\n> **Production:** demo (alpha)\n\nalpha history stays in prose.\n")
        self.write(self.checkout / "docs/plans/mul-add.md", "# mul_add\n\n> **Production:** demo (mul_add)\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "fixture")
        _ = self.git("push", "-u", "origin", SLUG)
        for stem in ("alpha", "mul-add"):
            _ = self.git("worktree", "add", "-b", f"demo-{stem}", str(self.rust / f"claude-demo-{stem}"), SLUG)
            _ = self.git("push", "origin", f"demo-{stem}")
        self.write(self.root / "state/notifier/showrunner-demo/conf",
                   "EVERY=15\nTARGET=session:test-showrunner\nCHECK="
                   + shlex.join(["zsh", "/scripts/production_check.sh", fixture_runtime_id(f"showrunner::{SLUG}")]) + "\n")
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
            "## Hub files", "", "| File | Owner unit | Other units that touch it |", "| --- | --- | --- |",
            "| `docs/alpha-unit.md` | alpha | beta (after alpha Phase 2) |",
            "", "## Gates", "", "| Gate | Waiting | Waits on | Clears when |", "| --- | --- | --- | --- |",
            "| alpha gate | beta | alpha Phase 2 | alpha merges |",
            "| old gate | alpha | beta | cleared 10-01 when alpha merged |",
            "", "## Production rules", "", "History mentions alpha and stays.", "",
        ])

    def seed_state(self) -> None:
        old = self.rust / "claude-demo-alpha"
        run = self.root / "history/runs/run-1.jsonl"
        self.write(run, "\n".join(json.dumps(event) for event in (
            {"event_type": "run_started", "run_started_at": 1, "working_dir": str(old),
             "worktree": old.name, "branch": "demo-alpha", "plan_doc": f"{old}/docs/plans/alpha.md"},
            {"event_type": "phase_started", "phase_instance_id": "p1", "phase_id": "1"},
            {"event_type": "phase_finished", "phase_instance_id": "p1", "phase_id": "1"})) + "\n")
        self.write(self.root / "sessions/s1/progress_history_state.json", json.dumps({
            "history_file": str(run), "production": SLUG, "unit": "alpha", "worktree": old.name, "branch": "demo-alpha",
            "plan_doc": f"{old}/docs/plans/alpha.md"}))
        for name, plan in (("mine", "docs/plans/alpha.md"), ("other-production", "docs/plans/elsewhere.md")):
            self.write(self.root / f"issues/{name}.md", "\n".join([
                "---", "status: open", "tags: [proposal]", "unit: alpha", f"plan: {plan}",
                "date_modified: 2026-10-01", "---", "", "A proposal.", ""]))
        for folder, production in (("demo-sr", "demo"), ("other-sr", "other")):
            self.write(self.root / f"vault/showrunners/{folder}/alpha-unit.md", "\n".join([
                "---", "phase_table: true", f"production: {production}", "unit: alpha", "---", "",
                "# alpha", ""]))

    def write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, content)

    def git(self, *arguments: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", *arguments], cwd=cwd or self.checkout, env=self.env,
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, (arguments, result.stdout, result.stderr))
        return result.stdout.strip()


    def rename(self, action: str, old: str = "alpha", new: str = "gamma", *,
               refuse: str = "") -> subprocess.CompletedProcess[str]:
        arguments = [action, f"{SLUG}/{old}", new]
        return self.invoke(arguments, refuse=refuse)

    def invoke(self, arguments: list[str], *, cwd: Path | None = None,
               refuse: str = "") -> subprocess.CompletedProcess[str]:
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
        with mock.patch.object(subprocess, "run", run):
            return invoke_cli(rename, lambda: rename.main(arguments), arguments,
                              self.env, cwd or self.checkout, production_state=True)

    def assert_ok(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
        self.write(self.doc, self.production_doc().replace(
            "- **Log:**", f"- **Kind:** style\n- **Repository:** `{self.checkout}`\n- **Log:**"))
        parent = next(row for row in staff.snapshot().find(kind="showrunner") if row.name == SLUG)
        unit = fixture_member("unit", name="names", parent=parent.id,
                           worktree=staff.PresentWorktree(remote.local_machine(), worktree))
        _ = unit_config.write(worktree, unit_config.UnitConfig(
            unit_config.PlanFile(plan), unit_config.NoAppPort(), ()))
        for directory in (self.checkout, worktree):
            self.write(directory / plan, "# Names\n\n> **Production:** demo (names)\n"
                       + "> **Standing:** style_plan.py\n\nnames history stays.\n")
        self.write(self.root / "issues/style.md", "---\nstatus: open\ntags: [proposal, style]\n"
                   + "proposed_by: demo\nunit: names\ndecision: approved\n"
                   + "date_modified: 2026-10-01\n---\nA style proposal.\n")
        self.env["STYLE_LEDGER_STATE_DIR"] = str(self.root / "state/style")
        database = self.root / "state/style" / self.checkout.name / "ledger.sqlite"
        with mock.patch.dict(os.environ, self.env):
            with style_ledger.Ledger(self.checkout, database) as ledger:
                ledger.wait_for_sync(unit.id)
        with closing(sqlite3.connect(database)) as connection, connection:
            _ = connection.executemany(
                "INSERT INTO tickets(id,unit,kind,family,rule,crate,files,sites,rules,rule_blobs,state,"
                + "branch,checkpoint,created_at,updated_at) VALUES (?,?,'read','names','','.','[]','[]','[]','{}',?,"
                + "'demo-names','','2026-10-01','2026-10-01')",
                [("names-unit-1", "names", "merged"), ("names-unit-2", "names", "assigned"),
                 ("names-unit-3", "names", "queued")],
            )
        return worktree, plan, database


class FolderRenameFixture(StaffTestCase):
    """Keep folder rename inputs in one private staff record and Git repository."""

    old_name: str = "source"
    new_name: str = "destination"
    home: Path = Path()
    checkout: Path = Path()
    source: Path = Path()
    destination: Path = Path()
    records: Path = Path()
    configuration: Path = Path()
    env: dict[str, str] = {}

    @property
    def owner(self) -> staff.StaffRow:
        """The original row remains the expected identity after each move."""
        parent = staff.native_id(fixture_runtime_id("showrunner::folder-production"))
        identity = staff.native_id(fixture_runtime_id(f"unit:{parent}:{self.old_name}"))
        return staff.StaffRow(identity, "unit", self.old_name, staff.ReportsTo(parent),
            current_session=staff.native_id(fixture_runtime_id("folder-current-after-clear")),
            worktree=staff.PresentWorktree(remote.local_machine(), self.source))

    @override
    def setUp(self) -> None:
        super().setUp()
        self.home = self.root / "home"
        self.checkout = self.root / "repository"
        self.source = self.home / "rust" / f"cargo-liner-{self.old_name}"
        self.destination = self.source.with_name(f"cargo-liner-{self.new_name}")
        self.records = self.root / "launches"
        self.configuration = self.root / "production.conf"
        self.env = {
            **config_environment(self.root),
            "HOME": str(self.home), "HERDR_BIN": "/nonexistent/herdr",
            "HERDR_PROC": str(self.root / "proc"), "HERDR_PLATFORM": "linux",
            "CODEX_HOME": str(self.home / ".codex"), "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "HERDR_PANE_RECORD_DIR": str(self.records),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
        }
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(os.environ, self.env)))
        self.home.mkdir(exist_ok=True)
        (self.root / "proc").mkdir()
        self.checkout.mkdir()
        self.source.parent.mkdir(parents=True)
        self.records.mkdir()
        _ = self.git("init", "-b", "main")
        _ = self.git("config", "user.name", "Folder Rename Test")
        _ = self.git("config", "user.email", "rename@example.invalid")
        _ = self.git("config", "gc.auto", "0")
        _ = (self.checkout / "kept.txt").write_text("Keep this work.\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "fixture")
        _ = self.git("worktree", "add", "-b", f"cargo-liner-{self.old_name}", str(self.source))
        owner = fixture_member("showrunner", name="folder-production",
            doc=staff.LiveDoc(remote.local_machine(), self.checkout / "production.md"))
        row = fixture_member("unit", name=self.old_name, parent=owner.id,
            worktree=staff.PresentWorktree(remote.local_machine(), self.source))
        _ = staff.update_current_session(row.current_session,
            staff.native_id(fixture_runtime_id("folder-current-after-clear")))
        _ = self.configuration.write_text((Path(__file__).resolve().parents[2] / "config/production.conf").read_text())
        self.transcript("claude")

    def git(self, *arguments: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", "-C", str(cwd or self.checkout), *arguments],
            capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def command(self, *, new: str = "") -> subprocess.CompletedProcess[str]:
        from scripts.production import rename as folder_rename

        arguments = ["apply", f"folder-production/{self.old_name}", new or self.new_name]
        return invoke_cli(folder_rename, lambda: folder_rename.main(arguments), arguments,
                          dict(os.environ), self.checkout)

    def publish_session_process(self, kind: Literal["claude", "codex"], *, live: bool, pid: int = 998041) -> None:
        """Publish private process evidence for the current conversation."""
        row = staff.by_id(self.owner.id)
        if row is None or not isinstance(row.worktree, staff.PresentWorktree):
            raise AssertionError("The fixture needs a present worktree")
        folder = row.worktree
        fake_unit_host.process(os.environ, 998040, 1, ["zsh"], {}, cwd=folder.path)
        arguments = [kind, "resume", self.owner.current_session] if kind == "codex" else [kind]
        environment = {"CODEX_HOME": str(self.home / ".codex")} if kind == "codex" else {
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"), "CLAUDE_CODE_SESSION_ID": self.owner.current_session}
        fake_unit_host.process(os.environ, pid, 998040 if live else 1, arguments if live else [],
                               environment, cwd=folder.path)
        descriptor = self.root / "proc" / str(pid) / "fd/4"
        descriptor.parent.mkdir(exist_ok=True)
        descriptor.unlink(missing_ok=True)
        if live and kind == "codex":
            record = self.root / "rollout-folder-current.jsonl"
            _ = record.write_text(json.dumps({"type": "session_meta", "payload": {
                "id": self.owner.current_session}}) + "\n")
            descriptor.symlink_to(record)

    def transcript(self, kind: Literal["claude", "codex"]) -> None:
        for directory in (self.home / ".claude", self.home / ".codex"):
            if directory.exists():
                shutil.rmtree(directory)
        if kind == "claude":
            path = self.home / ".claude/projects/previous-folder" / f"{self.owner.current_session}.jsonl"
        else:
            path = self.home / ".codex/sessions/2026/11/04" / f"rollout-fixture-{self.owner.current_session}.jsonl"
        path.parent.mkdir(parents=True)
        _ = path.write_text("{}\n")


class FolderSessionFixture:
    """Keep fake session state across an interrupted command and its retry."""

    def __init__(self, owner: staff.StaffRow, *, live: bool) -> None:
        self.owner: staff.StaffRow = owner
        self.live: bool = live
        self.stops: list[str] = []
        self.starts: list[staff.StaffRow] = []
        self.resumes: list[tuple[str, list[str]]] = []

    def stop(self, row: staff.StaffRow, _client: object) -> bool:
        if row.current_session != self.owner.current_session:
            raise AssertionError("The move must stop current_session")
        if self.live:
            self.stops.append(row.current_session)
        self.live = False
        return True

    def start(self, row: staff.StaffRow, _record: staff.StaffSnapshot, _client: object,
              **_options: object) -> str:
        if self.live:
            raise AssertionError("The move must never start a second session")
        if row.current_session != self.owner.current_session:
            raise AssertionError("The move must resume current_session")
        from scripts.shutdown import restart

        kind, _environment, arguments = restart.session_launch(str(row.current_session), row.name)
        self.resumes.append((kind, arguments))
        if not isinstance(row.worktree, staff.PresentWorktree) or not row.worktree.path.is_dir():
            raise OSError("The resume directory is absent")
        self.starts.append(row)
        self.live = True
        return "w1:p1"

    @contextmanager
    def installed(self) -> Generator[None]:
        from scripts.shutdown import restart, pane_record

        def live_members(rows: Iterable[staff.StaffRow], _record: staff.StaffSnapshot) -> list[staff.StaffRow]:
            return [row for row in rows if self.live and row.id == self.owner.id]

        with (mock.patch.object(restart, "live_members", side_effect=live_members),
              mock.patch.object(restart, "stop_row", side_effect=self.stop),
              mock.patch.object(restart, "start_row", side_effect=self.start),
              mock.patch.object(pane_record, "launch_for_session", return_value=None),
              mock.patch.object(os, "kill", side_effect=AssertionError("No real process may receive a signal"))):
            yield
