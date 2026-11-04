"""Use private staff records and Git repositories for unit rename tests."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts.herdr import fake_herdr
from scripts.lint.memory_test_support import config_environment
from scripts.production.cli_fixture import invoke_cli
from scripts.production.rename_test_support import bind_staff_fixture
from scripts.production.staff_test_support import fixture_member, StaffTestCase
from scripts.production import staff, unit_config
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
import rename_unit
import pane_record
if TYPE_CHECKING:
    from scripts.style import style_ledger
else:
    import style_ledger

SCRIPT = Path(__file__).with_name("rename_unit.py")
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
        bind_staff_fixture(self, self.env, {SLUG: ("alpha-unit", "mul_add-unit")},
                           locations={(SLUG, "alpha-unit"): self.rust / "claude-demo-alpha",
                                      (SLUG, "mul_add-unit"): self.rust / "claude-demo-mul-add"},
                           plans={(SLUG, "alpha-unit"): Path("docs/plans/alpha.md"),
                                  (SLUG, "mul_add-unit"): Path("docs/plans/mul-add.md")})
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
            "HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(config),
            "CODEX_HOME": str(self.root / "codex-home"),
            "XDG_CONFIG_HOME": str(self.home / ".config"), "XDG_STATE_HOME": str(self.root / "state"),
            "NOTIFIER_STATE_DIR": str(self.root / "state/notifier"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "state/sessions"),
            "SHOWRUNNER_STATE_DIR": str(self.state),
            "STALL_WATCH_STATE_DIR": str(self.root / "state/stall-watch"),
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
            "## Hub files", "", "| File | Owner unit | Other units that touch it |", "| --- | --- | --- |",
            "| `docs/alpha-unit.md` | alpha-unit | beta-unit (after alpha-unit Phase 2) |",
            "", "## Gates", "", "| Gate | Waiting | Waits on | Clears when |", "| --- | --- | --- | --- |",
            "| alpha-unit gate | beta-unit | alpha-unit Phase 2 | alpha-unit merges |",
            "| old gate | alpha-unit | beta-unit | cleared 10-01 when alpha-unit merged |",
            "", "## Production rules", "", "History mentions alpha-unit and stays.", "",
        ])

    def seed_state(self) -> None:
        old = self.rust / "claude-demo-alpha"
        self.write(self.stall_path("alpha-unit"), '{"stretch": 17}\n')
        run = self.root / "history/runs/run-1.jsonl"
        self.write(run, "\n".join(json.dumps(event) for event in (
            {"event_type": "run_started", "run_started_at": 1, "working_dir": str(old),
             "worktree": old.name, "branch": "demo-alpha", "plan_doc": f"{old}/docs/plans/alpha.md"},
            {"event_type": "phase_started", "phase_instance_id": "p1", "phase_id": "1"},
            {"event_type": "phase_finished", "phase_instance_id": "p1", "phase_id": "1"})) + "\n")
        self.write(self.root / "sessions/s1/progress_history_state.json", json.dumps({
            "history_file": str(run), "production": SLUG, "unit": "alpha-unit", "worktree": old.name, "branch": "demo-alpha",
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
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
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
            return invoke_cli(rename_unit, lambda: rename_unit.main(arguments), arguments,
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
        _ = fixture_member("unit", name="names", parent=parent.id,
                           worktree=staff.PresentWorktree("natedev", worktree))
        _ = unit_config.write(worktree, unit_config.UnitConfig(
            unit_config.PlanFile(plan), unit_config.NoAppPort(), ()))
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
        return worktree, plan, database
