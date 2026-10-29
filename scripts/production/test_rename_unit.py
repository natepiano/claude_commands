"""Rename one unit of a disposable production against a local bare origin and isolated state."""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from contextlib import closing, redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast, final, override
from unittest import mock
from scripts.guilds import roles
from scripts.direct import workers

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_showrunner
import fake_unit_host
import rename_unit
import retire_unit
import unit_lookup
import unit_move
import unit_runs
from rename_state import RenameRefused

if TYPE_CHECKING:
    from scripts.style import style_ledger
    from scripts.shutdown import record as shutdown_record
else:
    import style_ledger
    # rename_unit puts the shutdown folder on the path.
    import record as shutdown_record

SCRIPT = Path(__file__).with_name("rename_unit.py")
SLUG = "demo"

# Fail one git call by its arguments, so a rerun proves the journal resumes there.
REFUSE_GIT = '''import os, runpy, subprocess, sys
from pathlib import Path

original_run = subprocess.run
def refusing_run(command, *args, **kwargs):
    arguments = command[3:] if command[:1] == ["git"] else []
    if arguments == os.environ["RENAME_TEST_REFUSE"].split(" "):
        return subprocess.CompletedProcess(command, 1, "", "simulated failure")
    return original_run(command, *args, **kwargs)
subprocess.run = refusing_run
sys.argv = sys.argv[1:]
sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
runpy.run_path(sys.argv[0], run_name="__main__")
'''


@final
class UnitRenameTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.home = Path()
        self.rust = Path()
        self.checkout = Path()
        self.origin = Path()
        self.doc = Path()
        self.state = Path()
        self.env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / "home"
        self.rust = self.home / "rust"
        self.checkout = self.rust / "claude-demo-trunk"
        self.origin = self.root / "origin.git"
        self.doc = self.checkout / "docs/plans/demo-production.md"
        self.state = self.root / "state/showrunner"
        config = self.home / ".claude"
        for path in (self.checkout, config, self.root / "state/sessions", self.root / "scratch"):
            path.mkdir(parents=True)
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
            "GUILD_ROLES_STATE": str(self.root / "state/guilds/roles"),
            "GUILD_OWNERSHIP_STATE": str(self.root / "state/guilds/ownership"),
            "SHOWRUNNERS_CONFIG": str(config / "showrunners.json"),
            "UNIT_ACTIVE_DIR": str(self.root / "active"),
            "UNIT_HISTORY_DIR": str(self.root / "history"),
            "PROGRESS_SESSION_ROOTS": str(self.root / "sessions"),
            "PHASE_TABLE_VAULT": str(self.root / "vault/showrunners"),
            "HERDR_BIN": str(SCRIPT.parents[1] / "herdr/fake_herdr.py"),
            "HERDR_PROC": str(self.root / "proc"),
            "HERDR_PLATFORM": "linux",
            "FAKE_HERDR_LOG": str(self.root / "herdr.jsonl"),
            "FAKE_HERDR_RESPONSES": str(self.root / "herdr-responses.json"),
            "CLAUDE_CODE_SESSION_ID": "", "USER_ZONE": "UTC", "UNIT_SYNC_WRITES": "0",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
        })
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
        _ = path.write_text(content, encoding="utf-8")

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
        command = [sys.executable, *(["-c", REFUSE_GIT] if refuse else []), str(SCRIPT), action, SLUG, old, new,
                   "--scratch", str(self.root / "scratch")]
        return subprocess.run(command, cwd=self.checkout, env={**self.env, "RENAME_TEST_REFUSE": refuse},
                              capture_output=True, text=True, check=False, timeout=120)

    def start_apply(self) -> subprocess.Popen[str]:
        """apply in the background, so the test can act as the shutdown path while it waits."""
        process = subprocess.Popen([sys.executable, str(SCRIPT), "apply", SLUG, "alpha", "gamma", "--scratch",
                                    str(self.root / "scratch")], cwd=self.checkout, env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(process.kill)
        return process

    def wait_for(self, ready: Callable[[], bool]) -> None:
        deadline = time.monotonic() + 60
        while not ready():
            self.assertLess(time.monotonic(), deadline, "timed out")
            time.sleep(0.1)

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

    def test_worker_migration_conflict_prints_a_refusal_without_a_traceback(self) -> None:
        result = subprocess.run([sys.executable, "-c", 'import runpy, sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(sys.argv[1]).resolve().parents[2]))\nfrom scripts.direct import workers\ndef unavailable(*args, **kwargs):\n    raise workers.WorkerUnavailable("worker ownership changed; rerun apply")\nworkers.move_unit = unavailable\nworkers.rename_unit = unavailable\nworkers.end_unit = unavailable\nsys.argv = sys.argv[1:]\nsys.path.insert(0, str(Path(sys.argv[0]).parent))\nrunpy.run_path(sys.argv[0], run_name="__main__")\n', str(SCRIPT), "apply", SLUG, "alpha", "gamma",
            "--scratch", str(self.root / "scratch")], cwd=self.checkout, env=self.env,
            capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("worker ownership changed; rerun apply", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_plan_writes_nothing_and_names_each_step(self) -> None:
        before = self.snapshot()
        result = self.rename("plan")
        self.assert_ok(result)
        self.assertEqual(self.snapshot(), before)
        self.assertIn(f"branch demo-alpha -> demo-gamma; worktree {self.rust / 'claude-demo-alpha'} -> "
                      + str(self.rust / "claude-demo-gamma"), result.stdout)
        self.assertIn("'chore(demo): rename unit alpha-unit to gamma-unit'", result.stdout)

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

    def test_style_rename_keeps_git_paths_plan_names_and_ticket_ids(self) -> None:
        worktree, plan, database = self.style_production()
        before = self.snapshot()
        result = self.rename("plan", "names-unit", "demo-names-unit")
        self.assert_ok(result)
        self.assertEqual(self.snapshot(), before)
        self.assertIn(f"git: keep branch demo-names; keep worktree {worktree}", result.stdout)
        self.assertNotIn(" -> ", next(line for line in result.stdout.splitlines() if "git:" in line))

        self.assert_ok(self.rename("apply", "names-unit", "demo-names-unit"))
        self.assertEqual(self.git("branch", "--show-current", cwd=worktree), "demo-names")
        self.assertEqual(self.git("show", "--format=%s", "--stat", "HEAD"),
                         "chore(demo): rename unit names-unit to demo-names-unit")
        self.assertIn(f"| demo-names-unit | `{plan}` | `{worktree}` | demo-names |", self.doc.read_text())
        self.assertEqual(self.git("ls-files", "--", str(self.doc)), "")
        for directory in (self.checkout, worktree):
            self.assertIn("> **Production:** demo (demo-names-unit)", (directory / plan).read_text())
            self.assertIn("names-unit history stays.", (directory / plan).read_text())
            self.assertFalse((directory / plan.with_name("demo-names-unit.md")).exists())
        self.assertIn('unit: "demo-names-unit"', (self.root / "issues/style.md").read_text())
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute("SELECT id,unit FROM tickets ORDER BY id").fetchall(), [
                ("names-unit-1", "names-unit"), ("names-unit-2", "demo-names-unit"),
                ("names-unit-3", "demo-names-unit"),
            ])
            self.assertEqual(connection.execute("SELECT unit FROM sync_waits").fetchall(), [("demo-names-unit",)])
        self.assert_ok(self.rename("verify", "names-unit", "demo-names-unit"))

    def test_style_rename_moves_only_its_productions_proposals(self) -> None:
        _ = self.style_production()
        other_doc = self.root / "other/docs/other-style-production.md"
        self.write(other_doc, "# Other style production\n\n- **Kind:** style\n"
                   + f"- **Proposals:** `{self.root / 'issues'}`\n\n## Units\n\n"
                   + "| Unit | Plan | Worktree | Branch |\n| --- | --- | --- | --- |\n"
                   + "| names-unit | docs/style-production/units/names-unit.md | /other/names | other-names |\n")
        owned = self.root / "issues/style.md"
        other = self.root / "issues/other-style.md"
        self.write(other, owned.read_text().replace("proposed_by: demo", "proposed_by: other-style"))
        other_before = other.read_bytes()
        unit_proposal = self.root / "issues/style-unit.md"
        self.write(unit_proposal, owned.read_text().replace("proposed_by: demo", "proposed_by: names-unit (demo)"))

        self.assert_ok(self.rename("apply", "names-unit", "demo-names-unit"))

        for issue in (owned, unit_proposal):
            self.assertIn('unit: "demo-names-unit"', issue.read_text())
        self.assertEqual(other.read_bytes(), other_before)

    def test_full_style_id_can_be_renamed_to_another_full_id(self) -> None:
        slug = "demo-style"
        checkout = self.rust / slug
        worktree = self.rust / f"{slug}-names"
        _ = self.git("worktree", "add", "-b", slug, str(checkout), SLUG)
        _ = self.git("worktree", "add", "-b", f"{slug}-names", str(worktree), SLUG)
        doc = checkout / f"docs/{slug}-production.md"
        plan = "docs/style-production/units/names-unit.md"
        self.write(doc, f"# Production — {slug}\n\n- **Kind:** style\n- **Repository:** `{self.checkout}`\n"
                   + f"- **Merge branch:** `{slug}`\n- **Showrunner checkout:** `{checkout}`\n"
                   + "- **Log:** production.log\n\n## Units\n\n"
                   + "| Unit | Plan | Worktree | Branch |\n| --- | --- | --- | --- |\n"
                   + f"| {slug}-names-unit | {plan} | `{worktree}` | {slug}-names |\n")
        self.write(checkout / plan, f"# Names\n\n> **Production:** {slug} ({slug}-names-unit)\n")
        _ = self.git("add", "docs", cwd=checkout)
        _ = self.git("commit", "-m", "full style unit fixture", cwd=checkout)
        _ = self.git("push", "origin", slug, f"{slug}-names")
        self.write(self.root / f"state/notifier/showrunner-{slug}/conf",
                   "EVERY=15\nTARGET=session:test-showrunner\nCHECK="
                   + shlex.join(["zsh", "/scripts/production_check.sh", str(doc)]) + "\n")
        self.env["STYLE_LEDGER_STATE_DIR"] = str(self.root / "state/style")
        before = self.snapshot()

        result = subprocess.run(
            [sys.executable, str(SCRIPT), "plan", slug, f"{slug}-names-unit", f"{slug}-words-names-unit",
             "--scratch", str(self.root / "scratch")], cwd=checkout, env=self.env,
            capture_output=True, text=True, check=False, timeout=120)

        self.assert_ok(result)
        self.assertIn(f"branch {slug}-names -> {slug}-words-names; worktree {worktree} -> "
                      + str(self.rust / f"{slug}-words-names"), result.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_rename_keeps_the_worker_address_and_rebinds_the_ledger_context(self) -> None:
        run = self.root / "runs" / "carried"
        run.mkdir(parents=True)
        old_worktree = self.rust / "claude-demo-alpha"
        self.write(run / "progress_history_state.json", json.dumps({"production": SLUG,
            "unit": "alpha-unit", "working_dir": str(old_worktree)}))
        with mock.patch.dict(os.environ, self.env):
            store = workers.UnitWorkerLedger(workers.ledger_path(SLUG, "alpha-unit"))
            worker = workers.UnitWorker("w7", "impl", "codex", "kept-model", "xhigh", {},
                workers.CodexThread("original-thread", str(self.root / "codex")), workers.Paused(),
                [workers.WorkerEvent("opened", "fixture", str(run), 1_793_188_800.0, {})])
            with store.locked() as ledger:
                ledger.workers[worker.name] = worker
        self.write(run / "mesh_roster.json", json.dumps({"w7": {"thread_id": "original-thread", "role": "impl",
            "status": "paused", "unit_ledger": str(store.path)}}))
        result = self.rename("apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with mock.patch.dict(os.environ, self.env):
            target = workers.ledger_for_run(run)
            self.assertEqual(target.path, workers.ledger_path(SLUG, "gamma-unit"))
            with target.locked() as ledger:
                self.assertEqual(ledger.workers["w7"], worker)
        self.assertFalse(store.path.exists())
        self.assertEqual(json.loads((run / "mesh_roster.json").read_text())["w7"]["unit_ledger"], str(target.path))

    def test_apply_moves_every_place(self) -> None:
        records = self.home / ".local/state/herdr-panes"
        for session, panes in (("agents", {"w1:p2": "alpha-unit", "w1:p3": "beta-unit"}),
                               ("work", {"w2:p1": "alpha-unit"})):
            self.write(records / f"{session}.json", json.dumps(
                {pane: {"environment": {"SHOWRUNNER_UNIT_ID": unit}} for pane, unit in panes.items()}))
        self.assert_ok(self.rename("apply", "alpha-unit", "gamma-unit"))
        # The old pane's entry would resume the old name; the relaunched unit records its new pane.
        self.assertEqual(json.loads((records / "agents.json").read_text()),
                         {"w1:p3": {"environment": {"SHOWRUNNER_UNIT_ID": "beta-unit"}}})
        self.assertEqual(json.loads((records / "work.json").read_text()), {})
        new = self.rust / "claude-demo-gamma"
        self.assertFalse((self.rust / "claude-demo-alpha").exists())
        self.assertEqual(self.git("branch", "--show-current", cwd=new), "demo-gamma")
        self.assertEqual(self.git("ls-remote", "--heads", "origin", "demo-alpha"), "")
        self.assertEqual(self.git("branch", "--list", "demo-alpha"), "")
        unit_tip = self.git("rev-parse", "demo-gamma")
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/demo-gamma").split()[0], unit_tip)
        # The unit took the later merge-branch work and the rename commit.
        self.assertTrue((new / "NOTES.md").exists())
        self.assertEqual(self.git("show", "-s", "--format=%s", "demo"), "chore(demo): rename unit alpha-unit to gamma-unit")
        _ = self.git("merge-base", "--is-ancestor", "demo", "demo-gamma")
        self.assertEqual(self.git("rev-parse", "demo"), self.git("rev-parse", "origin/demo"))
        doc = self.doc.read_text()
        self.assertIn(f"| gamma-unit | docs/plans/alpha.md | `{new}` | demo-gamma |", doc)
        self.assertIn("| `docs/alpha-unit.md` | gamma-unit | beta-unit (after gamma-unit Phase 2) |", doc)
        self.assertIn("| alpha-unit gate | beta-unit | gamma-unit Phase 2 | alpha-unit merges |", doc)
        self.assertIn("| old gate | alpha-unit | beta-unit | cleared 10-01 when alpha-unit merged |", doc)
        self.assertIn("History mentions alpha-unit and stays.", doc)
        plan = (self.checkout / "docs/plans/alpha.md").read_text()
        self.assertIn("> **Production:** demo (gamma-unit)", plan)
        self.assertIn("alpha-unit history stays in prose.", plan)
        self.assertFalse(self.stall_path("alpha-unit").exists())
        self.assertTrue(self.stall_path("gamma-unit").exists())
        pool = cast(dict[str, dict[str, dict[str, object]]],
                    json.loads((self.root / "state/review-pool/demo/pool.json").read_text()))
        self.assertEqual(pool["reviewers"]["r1"]["lease"], {"unit": "gamma-unit", "ordinal": 3})
        self.assertEqual(pool["reviewers"]["r1"]["thread_id"], "thread-r1")
        self.assertEqual(cast(dict[str, object], pool["reviewers"]["r2"]["lease"])["unit"], "mul_add-unit")
        started = cast(dict[str, object], json.loads((self.root / "history/runs/run-1.jsonl").read_text().splitlines()[0]))
        self.assertEqual((started["working_dir"], started["branch"], started["plan_doc"]),
                         (str(new), "demo-gamma", f"{new}/docs/plans/alpha.md"))
        recorder = cast(dict[str, object], json.loads((self.root / "sessions/s1/progress_history_state.json").read_text()))
        self.assertEqual((recorder["working_dir"], recorder["worktree"]), (str(new), new.name))
        self.assertIn('unit: "gamma-unit"', (self.root / "issues/mine.md").read_text())
        self.assertIn("unit: alpha-unit", (self.root / "issues/other-production.md").read_text())
        self.assertIn("unit: gamma-unit", (self.root / "vault/showrunners/demo-sr/alpha-unit.md").read_text())
        self.assertIn("unit: alpha-unit", (self.root / "vault/showrunners/other-sr/alpha-unit.md").read_text())
        self.assertIs(self.journal()["done"], True)

    def seed_record(self, progress: str = "stopped", name: str = "alpha", host_session: str = "demo-alpha", *,
                    unit: str = "alpha-unit", slug: str = SLUG) -> Path:
        """A stopped unit's shutdown record, as `shutdown.py down` leaves it."""
        old = self.rust / "claude-demo-alpha"
        session: dict[str, object] = {
            "session_id": "sid-alpha", "config_dir": str(self.home / ".claude"), "pid": 4242, "proc_start": "1",
            "name": name, "cwd": f"{old}/sub", "status": "idle", "updated_at": 0, "probe": False,
            "model": {"kind": "no reply yet"},
            "checkout": {"kind": "git", "path": str(old), "head": {"kind": "branch", "name": "demo-alpha"},
                         "upstream": {"kind": "tracking", "ahead": 0}, "dirty": []},
            "run_dirs": [], "codex_servers": [], "timers": [], "kind": "unit",
            "host": {"kind": "unit", "production": slug, "unit": unit, "doc": str(self.doc),
                     "session_name": host_session, "session_host": "herdr",
                     "plan": {"kind": "plan", "path": f"{old}/docs/plans/alpha.md"}}}
        saved: dict[str, object] = {
            "login": "owner@example.com", "label": "claude2", "machine": "natedev",
            "requested_at": "2026-10-09T21:49:10+00:00", "request_id": "request",
            "requested_by": {"kind": "terminal"}, "hold": "session", "then": "stop", "force": "wait for ready",
            "alerts": "none", "probe": False, "session": session, "workers": [], "timers": [],
            "settle_message": {"kind": "not sent"}, "where": {"kind": "not said"},
            "progress": {"kind": progress, "at": "2026-10-09T21:50:00+00:00"}, "stop_issues": [], "trail": [],
            "codex_seats": [{"run_dir": str(self.root / "runs/r1"), "seat": "impl", "role": "impl",
                             "thread_id": "thread-impl", "turn_live": True, "launcher_command": [
                                 "bash", "/scripts/direct/implement.sh", "--to", "impl", str(self.root / "runs/r1"),
                                 str(old), "/runs/r1/prompt.md", "impl", "task", "impl", "working", "0", "impl"]}]}
        self.write(self.root / "runs/r1/impl.resume-prompt.md", "Continue the task you were on.\n")
        script = ("import json, sys; sys.path.insert(0, sys.argv[1]); import record; "
                  + "assert record.create_session(json.loads(sys.stdin.read()))")
        created = subprocess.run([sys.executable, "-c", script, str(SCRIPT.parents[1] / "shutdown")],
                                 input=json.dumps(saved), env=self.env, capture_output=True, text=True,
                                 check=False, timeout=60)
        self.assertEqual(created.returncode, 0, created.stderr)
        log = self.root / "shutdown-calls.jsonl"
        stub = self.root / "fake_shutdown.py"
        self.write(stub, "import json, os, sys\nwith open(os.environ['RENAME_UNIT_SHUTDOWN_LOG'], 'a') as log:\n"
                   + "    _ = log.write(json.dumps(sys.argv[1:]) + '\\n')\n")
        self.env["RENAME_UNIT_SHUTDOWN"] = str(stub)
        self.env["RENAME_UNIT_SHUTDOWN_LOG"] = str(log)
        return log

    def record(self) -> dict[str, object]:
        directory = self.home / ".local/state/shutdown/owner@example.com/sessions"
        return cast(dict[str, object], json.loads((directory / "sid-alpha.json").read_text()))

    def test_restart_record_keeps_one_prefix_for_a_full_style_id(self) -> None:
        slug = "cargo-liner-style"
        _ = self.seed_record(name="names", host_session=f"{slug}-names", unit="names-unit", slug=slug)
        self.write(self.doc, self.doc.read_text().replace("| alpha-unit |", "| names-unit |"))
        worktree = str(self.rust / "claude-demo-alpha")
        rename = rename_unit.UnitRename(
            slug, "names-unit", f"{slug}-names-unit", str(self.doc), str(self.checkout), SLUG,
            f"{slug}-names", f"{slug}-names", worktree, worktree, "", str(self.root / "scratch"),
            account="owner@example.com", session_id="sid-alpha")

        with mock.patch.dict(os.environ, self.env):
            rename_unit._restart_record(rename, lambda: None)  # pyright: ignore[reportPrivateUsage]

        session = cast(dict[str, object], self.record()["session"])
        host = cast(dict[str, object], session["host"])
        self.assertEqual(host["session_name"], "cargo-liner-style-names")
        self.assertEqual((host["unit"], session["name"]), ("cargo-liner-style-names-unit", "cargo-liner-style-names"))

    def test_restart_record_moves_a_feature_host_to_its_new_stem(self) -> None:
        _ = self.seed_record()
        rename = rename_unit.UnitRename(
            SLUG, "alpha-unit", "beta-unit", str(self.doc), str(self.checkout), SLUG,
            "demo-alpha", "demo-beta", str(self.rust / "claude-demo-alpha"),
            str(self.rust / "claude-demo-beta"), "", str(self.root / "scratch"),
            account="owner@example.com", session_id="sid-alpha")

        with mock.patch.dict(os.environ, self.env):
            rename_unit._restart_record(rename, lambda: None)  # pyright: ignore[reportPrivateUsage]

        session = cast(dict[str, object], self.record()["session"])
        self.assertEqual(cast(dict[str, object], session["host"])["session_name"], "demo-beta")

    def test_a_stopped_unit_relaunches_from_its_rewritten_record(self) -> None:
        log = self.seed_record()
        result = self.rename("apply")
        # The fake restart starts nothing, so verify finds the unit not running and holds the journal open.
        self.assert_refused(result, "gamma-unit is not running")
        self.assertEqual((self.journal()["step"], self.journal()["claimed"]), (10, False))
        self.assertEqual([json.loads(line) for line in log.read_text().splitlines()],
                         [["restart", "--only", "sid-alpha", "--here"]])
        saved = self.record()
        session = cast(dict[str, object], saved["session"])
        host = cast(dict[str, object], session["host"])
        new = self.rust / "claude-demo-gamma"
        self.assertEqual((host["unit"], host["session_name"], session["name"], session["cwd"]),
                         ("gamma-unit", "demo-gamma", "gamma", f"{new}/sub"))
        self.assertEqual(cast(dict[str, object], host["plan"])["path"], f"{new}/docs/plans/alpha.md")
        self.assertIn(f"you are now gamma-unit, in worktree {new} on branch demo-gamma, and your plan is at "
                      + f"{new}/docs/plans/alpha.md", cast(str, saved["renamed"]))
        # Each paused Codex seat resumes its own thread in the new worktree, and its resume prompt says so.
        seat = cast(list[dict[str, object]], saved["codex_seats"])[0]
        self.assertEqual(cast(list[str], seat["launcher_command"])[5], str(new))
        self.assertEqual((self.root / "runs/r1/impl.resume-prompt.md").read_text(),
                         "Continue the task you were on.\nWhile you were paused the showrunner renamed your unit: "
                         + f"your worktree is now {new} on branch demo-gamma. {self.rust / 'claude-demo-alpha'} no "
                         + f"longer exists; wherever your instructions name it, use {new}.\n")

    def test_a_session_with_its_own_name_takes_the_new_name(self) -> None:
        _ = self.seed_record(name="builder-report")
        self.assert_refused(self.rename("apply"), "gamma-unit is not running")
        self.assertEqual(cast(dict[str, object], self.record()["session"])["name"], "gamma")
        self.assertEqual(self.journal()["old_session"], "builder-report")

    def test_the_session_name_finds_the_unit_and_only_what_differs_changes(self) -> None:
        # The user names the unit by its session (builder-report); its id already is the new one.
        _ = self.seed_record(name="builder-report", host_session="demo-builder-report")
        planned = self.rename("plan", "builder-report", "alpha")
        self.assert_ok(planned)
        self.assertIn("Claude session builder-report -> alpha", planned.stdout)
        self.assertIn("already matches: the id is alpha-unit", planned.stdout)
        branches = self.git("branch", "--list")
        doc = self.doc.read_text()
        self.assert_refused(self.rename("apply", "builder-report", "alpha"), "alpha-unit is not running")
        session = cast(dict[str, object], self.record()["session"])
        self.assertEqual((session["name"], cast(dict[str, object], session["host"])["session_name"]),
                         ("alpha", "demo-alpha"))
        self.assertEqual((self.git("branch", "--list"), self.doc.read_text()), (branches, doc))
        # The relaunched unit keeps its mark alpha-unit, now under the new session name.
        _ = self.mark_live("alpha-unit", "sid-alpha")
        self.assert_ok(self.rename("apply", "builder-report", "alpha"))
        self.assertEqual(self.journal()["done"], True)

    def test_verify_fails_while_the_old_session_name_is_live(self) -> None:
        _ = self.seed_record(name="builder-report")
        self.assert_refused(self.rename("apply"), "gamma-unit is not running")
        _ = self.mark_live("gamma-unit", "sid-alpha", name="builder-report")
        result = self.rename("apply")
        self.assert_refused(result, "gamma-unit's session is named builder-report, not gamma")
        self.assertIn("is still labelled builder-report", result.stderr)

    def test_a_verify_that_passes_closes_the_journal(self) -> None:
        _ = self.seed_record(name="builder-report")
        self.assert_refused(self.rename("apply"), "gamma-unit is not running")
        end = self.mark_live("gamma-unit", "sid-alpha", name="builder-report")
        self.assert_refused(self.rename("apply"), "gamma-unit's session is named builder-report, not gamma")
        self.assertEqual(self.journal()["done"], False)
        end()
        _ = self.mark_live("gamma-unit", "sid-alpha")
        self.assert_ok(self.rename("verify"))
        self.assertEqual(self.journal()["done"], True)
        self.assert_ok(self.rename("plan", "gamma", "delta"))

    def test_apply_waits_while_the_unit_is_stopping(self) -> None:
        log = self.seed_record(progress="waiting")
        result = self.rename("apply")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("alpha-unit is still waiting after 0 s; rerun apply once it has stopped", result.stderr)
        self.assertEqual(self.journal()["step"], 0)
        # A stop that waits for the unit to go idle is forced at once.
        self.assertEqual([json.loads(line) for line in log.read_text().splitlines()],
                         [["now", "--only", "sid-alpha", "--here"]])

    @unittest.skipIf(sys.platform == "darwin", "the Mac reads working directories with lsof")
    def test_verify_fails_while_a_process_works_in_the_old_worktree(self) -> None:
        _ = self.seed_record()
        self.assert_refused(self.rename("apply"), "gamma-unit is not running")
        _ = self.mark_live("gamma-unit", "sid-alpha")
        old = self.rust / "claude-demo-alpha"
        old.mkdir()
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
            cwd=old, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        assert child.stdin is not None
        assert child.stdout is not None
        self.addCleanup(child.stdin.close)
        self.addCleanup(child.stdout.close)
        # Python must finish startup before its cwd disappears; otherwise getpath can exit first.
        self.assertEqual(child.stdout.readline(), "ready\n")
        # The folder is gone, but the process still has it as its working directory.
        old.rmdir()
        result = self.rename("verify")
        self.assert_refused(result, f"processes {child.pid} still work in {old}")
        child.kill()
        _ = child.wait()
        self.assert_ok(self.rename("verify"))

    def test_verify_fails_on_an_unclassified_stale_reference(self) -> None:
        self.assert_ok(self.rename("apply"))
        self.write(self.root / "state/nightly-review/2026-10-16/report.md", "alpha-unit reviewed\n")
        self.write(self.root / "state/other/names.json", '{"unit": "claude-demo-alphabet"}\n')
        self.write(self.root / "state/other/__pycache__/names.cpython-313.pyc", "alpha-unit\n")
        self.write(self.root / "state/other/names.pyc", "alpha-unit\n")
        self.write(self.root / "state/conversation-pause/scheduled-prompts/sid.json",
                   '["Renamed from alpha-unit to gamma-unit."]\n')
        self.assert_ok(self.rename("verify"))
        stale = self.root / "state/other/cache.json"
        self.write(stale, '{"unit": "alpha-unit"}\n')
        self.write(self.root / "scratch/showrunner_state.json", '{"units": {"alpha-unit": {}}}\n')
        result = self.rename("verify")
        self.assert_refused(result, f"unclassified: {stale} still holds an old name")
        self.assertIn(f"unclassified: {self.root / 'scratch/showrunner_state.json'} still holds", result.stderr)
        self.assertNotIn("nightly-review", result.stderr)
        self.assertNotIn("names.json", result.stderr)

    def test_the_live_lists_take_the_new_name_and_verify_passes(self) -> None:
        # sdlc, 2026-10-21: verify called the showrunner's own `next` text unclassified after a rename.
        self.write(self.root / "scratch/showrunner_state.json", json.dumps({
            "units": [{"unit": "alpha-unit"}], "next": ["merge alpha-unit from demo-alpha", "keep the per-unit gate"],
            "open_for_user": [{"text": "alpha-unit asks", "question": "Does alpha-unit ship?"}]}))
        outstanding = self.state / "outstanding/demo.json"
        self.write(outstanding, json.dumps([{"since": "2026-10-21T09:00", "text": "alpha-unit asks",
                                             "question": "Does alpha-unit ship?"}]))
        self.assert_ok(self.rename("apply"))
        state = cast(dict[str, object], json.loads((self.root / "scratch/showrunner_state.json").read_text()))
        self.assertEqual(state["next"], ["merge gamma-unit from demo-gamma", "keep the per-unit gate"])
        self.assertEqual(state["open_for_user"], [{"text": "gamma-unit asks", "question": "Does gamma-unit ship?"}])
        self.assertEqual(cast(list[dict[str, str]], json.loads(outstanding.read_text()))[0]["text"], "gamma-unit asks")
        self.assert_ok(self.rename("verify"))

    def test_other_showrunners_lists_take_the_new_name_unless_their_production_has_the_id(self) -> None:
        """cargo-liner-style, 2026-10-21: the parent production's showrunner state named the unit."""
        states: dict[str, Path] = {}
        for slug, row in (("parent", "| other-unit | p.md | `/x` | other |"), ("twin", "| alpha-unit | t.md | `/y` | alpha |"),
                          ("orphan", "")):
            doc = self.root / f"{slug}/docs/{slug}-production.md"
            self.write(doc, f"# {slug}\n\n## Units\n\n| Unit | Plan | Worktree | Branch |\n|---|---|---|---|\n{row}\n")
            if slug != "orphan":
                self.write(self.root / f"state/notifier/showrunner-{slug}/conf",
                           "EVERY=15\nTARGET=session:test-showrunner\nCHECK="
                           + shlex.join(["zsh", "/scripts/production_check.sh", str(doc)]) + "\n")
            states[slug] = self.state / slug / "session/showrunner_state.json"
            self.write(states[slug], json.dumps({"next": ["wait for alpha-unit"],
                                                 "open_for_user": [{"text": "alpha-unit asks", "question": "Q?"}]}))
        outstanding = self.state / "outstanding/parent.json"
        self.write(outstanding, json.dumps([{"since": "2026-10-21T09:00", "text": "alpha-unit asks", "question": "Q?"}]))
        self.assert_ok(self.rename("apply"))
        parent = cast(dict[str, object], json.loads(states["parent"].read_text()))
        self.assertEqual(parent["next"], ["wait for gamma-unit"])
        self.assertEqual(parent["open_for_user"], [{"text": "gamma-unit asks", "question": "Q?"}])
        self.assertEqual(cast(list[dict[str, str]], json.loads(outstanding.read_text()))[0]["text"], "gamma-unit asks")
        # twin has its own alpha-unit, and orphan's production cannot be read: both keep their text.
        for slug in ("twin", "orphan"):
            self.assertIn("wait for alpha-unit", states[slug].read_text())
        self.assert_ok(self.rename("verify"))

    def test_mul_add_naming_keeps_the_underscore_id(self) -> None:
        self.assert_ok(self.rename("apply", "mul_add-unit", "fma"))
        self.assertIn(f"| fma-unit | docs/plans/mul-add.md | `{self.rust / 'claude-demo-fma'}` | demo-fma |",
                      self.doc.read_text())

    def test_resume_after_an_interrupted_merge_branch_push(self) -> None:
        self.assert_refused(self.rename("apply", refuse="push origin demo:refs/heads/demo"), "simulated failure")
        journal = self.journal()
        self.assertEqual((journal["step"], journal["done"]), (4, False))
        self.assertEqual(journal["doc_commit"], self.git("rev-parse", "demo"))
        refused = self.rename("apply", "alpha", "delta")
        self.assert_refused(refused, "the unfinished rename is alpha-unit to gamma-unit")
        before = self.snapshot()
        self.assert_refused(self.rename("plan", "beta", "delta"), "resume it first")
        self.assertEqual(self.snapshot(), before)
        self.assert_ok(self.rename("apply"))
        subjects = self.git("log", "--format=%s", "demo").splitlines()
        self.assertEqual(subjects.count("chore(demo): rename unit alpha-unit to gamma-unit"), 1)
        self.assertEqual(self.git("rev-parse", "demo"), self.git("rev-parse", "origin/demo"))

    def test_busy_held_role_blocks_rename_state_then_retry_updates_live_routes(self) -> None:
        old = self.rust / "claude-demo-alpha"
        new = self.rust / "claude-demo-gamma"
        config = self.root / "guilds.conf"
        self.write(config, "[ownership]\nhost=fixture-host\n")
        self.env.update({"GUILD_CONFIG_FILE": str(config), "GUILD_ROLES_HOST": "fixture-host"})
        design = roles.HeldDesign(roles.DesignPlacement("00000000-0000-4000-8000-000000000056", "fixture-host", 1),
            "fixture-repo", str(old), str(old / "docs/plans/alpha.md"), "design", "2026-10-21T12:00:00+00:00",
            roles.UnitHolder("demo", "alpha-unit"), roles.DesignUnbound(),
            (roles.RoleInstance("designer", "original-designer-alias", roles.RoleOpen("original-designer-thread")),
             roles.RoleInstance("reviewer", "original-reviewer-alias", roles.RoleOpen("original-reviewer-thread"))))
        with mock.patch.dict(os.environ, self.env):
            roles.register_repository("fixture-repo", {"fixture-host": str(old)}, inventory_complete=True)
            placement = roles.authority_request("reserve", {"repository_id": "fixture-repo",
                "document": "docs/plans/alpha.md", "host": "fixture-host", "request_id": "rename-fixture"})
            design = roles.HeldDesign(roles.DesignPlacement(str(placement["design_id"]), "fixture-host", 1),
                design.repository_id, design.repository, design.document, design.area, design.created_at_utc,
                design.holder, design.binding, design.roles)
            roles._save(design)  # pyright: ignore[reportPrivateUsage]
            mesh = roles.mesh_directory(design.placement.design_id)
        mesh.mkdir(parents=True)
        roster = {instance.name: {"thread_id": cast(roles.RoleOpen, instance.lifecycle).thread_id,
                                  "status": "running" if instance.role == "designer" else "done", "cwd": str(old)}
                  for instance in design.roles}
        self.write(mesh / "mesh_roster.json", json.dumps(roster))
        result = self.rename("apply")
        self.assert_refused(result, "busy")
        self.assertFalse(self.journal()["done"])
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(roles.read_design(design.placement.design_id).holder, design.holder)
        roster["original-designer-alias"]["status"] = "done"
        self.write(mesh / "mesh_roster.json", json.dumps(roster))
        self.assert_ok(self.rename("apply"))
        with mock.patch.dict(os.environ, self.env):
            renamed = roles.read_design(design.placement.design_id)
        self.assertEqual(renamed.holder, roles.UnitHolder("demo", "gamma-unit"))
        self.assertEqual((renamed.repository, renamed.document), (str(new), str(new / "docs/plans/alpha.md")))
        self.assertEqual([(instance.name, instance.lifecycle) for instance in renamed.roles],
                         [(instance.name, instance.lifecycle) for instance in design.roles])
        self.assertEqual(renamed.placement.generation, 2)
        self.assertEqual({entry["cwd"] for entry in cast(dict[str, dict[str, str]], json.loads((mesh / "mesh_roster.json").read_text())).values()},
                         {str(new)})

    def test_add_unit_refuses_a_unit_being_renamed(self) -> None:
        self.assert_refused(self.rename("apply", refuse="push origin demo:refs/heads/demo"), "simulated failure")
        claim = subprocess.run(
            [sys.executable, "-c", "import rename_state; print(' '.join(sorted(rename_state.rename_claim('demo'))))"],
            cwd=SCRIPT.parent, env=self.env, capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(claim.stdout.strip(), "alpha-unit gamma-unit", claim.stderr)

    def test_refusals(self) -> None:
        cases = [
            (("alpha", "nightly-review-unit"), "is known to code"),
            (("alpha", "mul_add"), "already in the Units table"),
            (("alpha", "bad name"), "letters, numbers"),
            (("missing", "gamma"), "expected one Units row"),
        ]
        for (old, new), text in cases:
            with self.subTest(old=old, new=new):
                self.assert_refused(self.rename("apply", old, new), text)
        self.assertFalse((self.state / SLUG / "rename_unit.json").exists())

    def mark_live(self, unit: str, session_id: str, name: str = "") -> Callable[[], None]:
        """A live Claude in a herdr session marked as `unit` of this production; the result ends it.

        The session and its pane label are called `name`, the unit's stem when it is empty.
        """
        name = name or unit.removesuffix("-unit")
        held = fake_showrunner.write_session(self.root / "state/sessions", name, session_id)
        self.addCleanup(held.close)
        fake_unit_host.write(self.env, [fake_unit_host.HostedUnit(
            f"demo-{unit}", "w1:p1", {"SHOWRUNNER_UNIT": SLUG, "SHOWRUNNER_UNIT_ID": unit}, label=name)])
        fake_unit_host.place_process(self.env, os.getpid(), "w1:p1")

        def end() -> None:
            held.close()
            fake_unit_host.write(self.env, [])
        return end

    def test_one_apply_stops_the_unit_waits_and_relaunches_it_with_no_rerun(self) -> None:
        log = self.seed_record()
        record = self.home / ".local/state/shutdown/owner@example.com/sessions/sid-alpha.json"
        stopped = cast(dict[str, object], json.loads(record.read_text()))
        record.unlink()
        end = self.mark_live("alpha-unit", "sid-alpha")
        self.env.update({"RENAME_UNIT_STOP_WAIT": "60", "RENAME_UNIT_LIVE_WAIT": "60"})
        process = self.start_apply()
        calls: Callable[[], list[list[str]]] = lambda: ([cast(list[str], json.loads(line)) for line in
                                                         log.read_text().splitlines()] if log.exists() else [])
        self.wait_for(lambda: ["down", "--only", "sid-alpha", "--here"] in calls())
        # The shutdown path ends the unit; its record reads stopping, and stopped a moment later.
        end()
        _ = record.write_text(json.dumps({**stopped, "progress": {"kind": "stopping", "at": "2026-10-09T21:49:30+00:00"}}))
        time.sleep(1.5)
        self.assertIsNone(process.poll())
        _ = record.write_text(json.dumps(stopped))
        self.wait_for(lambda: ["restart", "--only", "sid-alpha", "--here"] in calls())
        # The relaunched unit comes up under its new id after the restart returns.
        time.sleep(1.5)
        _ = self.mark_live("gamma-unit", "sid-alpha")
        stdout, stderr = process.communicate(timeout=120)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertIn("stop: waiting — alpha-unit is stopping", stdout)
        self.assertIn("alpha-unit is now gamma-unit, relaunched in", stdout)
        self.assertEqual((self.journal()["done"], self.journal()["claimed"]), (True, False))

    def test_apply_waits_for_a_merge_gate_before_it_writes_the_merge_branch(self) -> None:
        _ = self.seed_record()
        common = subprocess.run(["git", "-C", str(self.checkout), "rev-parse", "--path-format=absolute",
                                 "--git-common-dir"], capture_output=True, text=True, check=True).stdout.strip()
        with (Path(common) / f"showrunner-merge-{SLUG}.lock").open("a") as gate:
            fcntl.flock(gate, fcntl.LOCK_EX)
            process = self.start_apply()
            # Hold the gate until apply says it waits: a fixed sleep let a slow start under
            # load reach the lock after it was released, so it never waited.
            assert process.stdout is not None
            waited = str(process.stdout.readline())  # pyright: ignore[reportAny]
            # The gate holds the merge branch: the rename has not even checked the checkout yet.
            self.assertIsNone(process.poll())
            self.assertFalse((self.state / SLUG / "rename_unit.json").exists())
            fcntl.flock(gate, fcntl.LOCK_UN)
        stdout, stderr = process.communicate(timeout=120)
        self.assertIn("merge lock: waiting — a merge gate or a unit rename is writing demo's merge branch",
                      waited + stdout)
        # The fake restart starts nothing; every step before verify ran once the gate was done.
        self.assertIn("gamma-unit is not running", stderr)
        self.assertEqual(self.journal()["step"], 10)

    def test_a_running_unit_plans_and_apply_stops_it_through_the_shutdown_path(self) -> None:
        log = self.seed_record()
        (self.home / ".local/state/shutdown/owner@example.com/sessions/sid-alpha.json").unlink()
        _ = self.mark_live("alpha-unit", "sid-alpha")
        planned = self.rename("plan")
        self.assert_ok(planned)
        self.assertIn("1. pause alpha-unit and its Codex workers through the shutdown path", planned.stdout)
        applied = self.rename("apply")
        self.assertEqual(applied.returncode, 2, applied.stdout + applied.stderr)
        self.assertIn("alpha-unit is still stopping after 0 s; rerun apply once it has stopped", applied.stderr)
        self.assertEqual([json.loads(line) for line in log.read_text().splitlines()],
                         [["down", "--only", "sid-alpha", "--here"]])
        self.assertEqual(self.journal()["step"], 0)

    def test_a_running_new_id_is_refused(self) -> None:
        _ = self.mark_live("gamma-unit", "sid-gamma")
        self.assert_refused(self.rename("plan"), "gamma-unit already runs in session demo-gamma-unit")

    def test_an_older_run_keeps_the_old_path_as_history(self) -> None:
        old = self.rust / "claude-demo-alpha"
        older = self.root / "history/runs/run-0.jsonl"
        self.write(older, json.dumps({"event_type": "run_started", "run_started_at": 0, "working_dir": str(old)})
                   + "\n" + json.dumps({"event_type": "run_finished"}) + "\n")
        self.assert_ok(self.rename("apply"))
        self.assertIn(str(old), older.read_text())
        newest = cast(dict[str, object], json.loads((self.root / "history/runs/run-1.jsonl").read_text().splitlines()[0]))
        self.assertEqual(newest["working_dir"], str(self.rust / "claude-demo-gamma"))
        self.assert_ok(self.rename("verify"))

    def test_untracked_logs_in_the_checkout_do_not_block_and_stay_out_of_the_commit(self) -> None:
        for name in ("demo-log.md", "demo-log-recovered.md"):
            self.write(self.checkout / "docs/plans" / name, "alpha-unit merged phase 2\n")
        self.write(self.doc, self.doc.read_text() + "a tracked edit\n")
        self.assert_refused(self.rename("apply"), "showrunner checkout must be clean")
        _ = self.git("checkout", "--", str(self.doc))
        self.assert_ok(self.rename("apply"))
        self.assertEqual(self.git("ls-files", "--others", "--exclude-standard").split(),
                         ["docs/plans/demo-log-recovered.md", "docs/plans/demo-log.md"])
        self.assertNotIn("demo-log", self.git("show", "--name-only", "--format=", "HEAD"))
        self.assertEqual((self.checkout / "docs/plans/demo-log.md").read_text(), "alpha-unit merged phase 2\n")

    def test_a_mid_phase_rename_keeps_the_commits_and_the_uncommitted_work(self) -> None:
        # User, 2026-10-18: a unit is renamed at any time, not only between phases.
        _ = self.seed_record()
        worktree = self.rust / "claude-demo-alpha"
        self.write(worktree / "work.md", "phase work\n")
        _ = self.git("add", "work.md", cwd=worktree)
        _ = self.git("commit", "-m", "checkpoint(demo-alpha): phase 1", cwd=worktree)
        checkpoint = self.git("rev-parse", "HEAD", cwd=worktree)
        self.write(worktree / "work.md", "phase work\nmore, not committed\n")
        self.write(worktree / "draft.md", "a new file\n")
        run = self.root / "history/runs/run-1.jsonl"
        self.write(run, run.read_text() + json.dumps(
            {"event_type": "phase_started", "phase_instance_id": "p2", "phase_id": "2"}) + "\n")

        self.assert_refused(self.rename("apply"), "gamma-unit is not running")

        new = self.rust / "claude-demo-gamma"
        self.assertEqual((new / "work.md").read_text(), "phase work\nmore, not committed\n")
        self.assertEqual((new / "draft.md").read_text(), "a new file\n")
        # The branch kept its tip: no merge went into work in progress.
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=new), checkpoint)
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/demo-gamma").split()[0], checkpoint)
        self.assertEqual(self.git("ls-remote", "--heads", "origin", "demo-alpha"), "")
        self.assertEqual(self.journal()["kept"], "phase 2 is open")
        self.assertIn("| gamma-unit | docs/plans/alpha.md |", self.doc.read_text())
        started = cast(dict[str, object], json.loads(run.read_text().splitlines()[0]))
        self.assertEqual(started["working_dir"], str(new))
        self.assertIn("Your work was in progress (phase 2 is open), so your branch kept its commits and took no "
                      + "merge: the rename commit comes with your next merge of demo.", cast(str, self.record()["renamed"]))

    def test_uncommitted_work_between_phases_also_keeps_the_tip(self) -> None:
        worktree = self.rust / "claude-demo-alpha"
        tip = self.git("rev-parse", "HEAD", cwd=worktree)
        self.write(worktree / "draft.md", "a new file\n")
        self.assert_ok(self.rename("apply"))
        new = self.rust / "claude-demo-gamma"
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=new), tip)
        self.assertEqual((new / "draft.md").read_text(), "a new file\n")
        self.assertEqual(self.journal()["kept"], "the worktree has uncommitted work")

    def test_refuses_an_unfinished_production_rename(self) -> None:
        self.write(self.state / SLUG / "rename.json", '{"done": false}\n')
        self.assert_refused(self.rename("apply"), "finish the production rename first")
        self.assertFalse((self.state / SLUG / "rename_unit.json").exists())


# An app-server's idle-stop watcher, as codex_mesh starts it: the run folder is its fourth argument.
WATCHER = 'while kill -0 "$1"; do sleep "$5"; "$4" "$0" idle-stop --session-dir "$3" --pid "$1" --port "$2"; done'
STOPPED_RECORD = {"login": "owner@example.com", "then": "stop", "progress": {"kind": "stopped"},
                  "session": {"session_id": "sid-alpha",
                              "host": {"kind": "unit", "production": SLUG, "unit": "alpha-unit"}}}


class RunRecordLookupTests(unittest.TestCase):
    def test_move_lookups_build_once_and_keep_exact_worktree_spelling(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        runs = root / "history/runs"
        runs.mkdir(parents=True)
        tree = root / "tree"
        tree.mkdir()
        alias = root / "alias"
        alias.symlink_to(tree, target_is_directory=True)
        for worktree, at in ((tree, 2), (alias, 1)):
            events = [{"event_type": "run_started", "working_dir": str(worktree), "run_started_at": at}]
            _ = (runs / f"{worktree.name}.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        with (mock.patch.dict(os.environ, {"UNIT_HISTORY_DIR": str(root / "history")}),
              mock.patch.object(unit_lookup, "read_run_states", wraps=unit_lookup.read_run_states) as reads):
            for lookup in (unit_move.newest_run, unit_move.open_run):
                for worktree in (tree, alias, root / "absent"):
                    with self.subTest(lookup=lookup.__name__, worktree=worktree):
                        reads.reset_mock()
                        expected = runs / f"{worktree.name}.jsonl" if worktree != root / "absent" else None
                        self.assertEqual(lookup(worktree), expected)
                        reads.assert_called_once_with()
            with (runs / "alias.jsonl").open("a", encoding="utf-8") as handle:
                _ = handle.write('{"event_type": "run_finished"}\n')
            self.assertIsNone(unit_move.open_run(alias))
            self.assertEqual(unit_move.newest_run(alias), runs / "alias.jsonl")


class FakeProcess(NamedTuple):
    cwd: Path
    session: int
    arguments: list[str]


@final
class RunProcessTests(unittest.TestCase):
    """What a stopped unit's runs leave in its worktree. The processes are records: nothing real is signalled."""

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.worktree = Path()
        self.processes: dict[int, FakeProcess] = {}
        self.signalled: list[int] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.worktree = self.root / "claude-demo-alpha"
        other = self.root / "claude-demo-beta"
        mine = self.run_folder("run-a", self.worktree)
        theirs = self.run_folder("run-b", other)
        lint = ["python3", "mesh_runs.py", "supervise", str(self.run_record(mine, "lint", 101))]
        _ = self.run_record(mine, "test", 104)
        self.processes = {
            # The unit's detached lint run: the supervisor leads its session, verify.sh and tee are in it.
            101: FakeProcess(self.worktree, 101, lint),
            102: FakeProcess(self.worktree, 101, ["bash", "verify.sh", "lint", "pkg"]),
            103: FakeProcess(self.worktree, 101, ["tee", "-a", "run.log"]),
            # The idle-stop watcher of the run's stopped app-server, and its sleep.
            201: FakeProcess(self.worktree, 201, self.watcher(mine)),
            202: FakeProcess(self.worktree, 201, ["sleep", "300"]),
            # A record whose pid now names another process; another unit's watcher; the user's shell.
            104: FakeProcess(self.worktree, 104, ["cargo", "build"]),
            203: FakeProcess(self.worktree, 203, self.watcher(theirs)),
            900: FakeProcess(self.worktree, 900, ["zsh"]),
            # Another unit's detached run, in its own worktree.
            301: FakeProcess(other, 301, ["python3", "mesh_runs.py", "supervise",
                                          str(self.run_record(theirs, "lint", 301))]),
            302: FakeProcess(other, 301, ["bash", "verify.sh", "lint", "pkg"]),
        }
        self.enterContext(mock.patch.dict(os.environ, {"RETIRE_UNIT_RUN_ROOT": str(self.root / "runs"),
            "UNIT_WORKERS_STATE": str(self.root / "unit-workers")}))
        _ = self.enterContext(mock.patch.object(unit_move, "processes_inside", self.inside))
        _ = self.enterContext(mock.patch.object(retire_unit, "command_line", self.arguments))
        from scripts.agents import mesh_runs
        _ = self.enterContext(mock.patch.object(mesh_runs, "_process_args", self.arguments))
        _ = self.enterContext(mock.patch.object(os, "getsid", self.session))
        _ = self.enterContext(mock.patch.object(os, "kill", self.kill))
        _ = self.enterContext(mock.patch.object(shutdown_record, "all_session_records", lambda: [STOPPED_RECORD]))
        _ = self.enterContext(mock.patch.object(unit_lookup, "marked_units", self.no_units))

    def run_folder(self, name: str, worktree: Path) -> Path:
        worktree.mkdir(parents=True, exist_ok=True)
        folder = self.root / "runs" / name
        (folder / retire_unit.DETACHED_RUNS).mkdir(parents=True)
        _ = (folder / "progress_history_state.json").write_text(json.dumps({"working_dir": str(worktree)}))
        return folder

    def run_record(self, folder: Path, label: str, pid: int) -> Path:
        path = folder / retire_unit.DETACHED_RUNS / f"{label}.json"
        _ = path.write_text(json.dumps({"status": "running", "pid": pid, "label": label, "cwd": str(self.worktree)}))
        return path

    def watcher(self, folder: Path) -> list[str]:
        return ["sh", "-c", WATCHER, "/scripts/agents/codex_mesh.py", "555", "51101", str(folder.resolve()),
                "python3", "300"]

    def no_units(self, _slug: str) -> dict[str, object]:
        return {}

    def survive(self, _pid: int, _signal: int) -> None:
        pass

    def inside(self, worktree: Path) -> list[int]:
        return sorted(pid for pid, item in self.processes.items() if item.cwd == worktree)

    def arguments(self, pid: int) -> list[str]:
        return self.processes[pid].arguments if pid in self.processes else []

    def session(self, pid: int) -> int:
        if pid not in self.processes:
            raise ProcessLookupError(pid)
        return self.processes[pid].session

    def kill(self, pid: int, signal_number: int) -> None:
        if pid not in self.processes:
            raise ProcessLookupError(pid)
        if signal_number:
            self.signalled.append(pid)
            del self.processes[pid]

    def rename(self) -> rename_unit.UnitRename:
        return rename_unit.UnitRename(SLUG, "alpha-unit", "gamma-unit", "", "", SLUG, "demo-alpha", "demo-gamma",
                                      str(self.worktree), str(self.root / "claude-demo-gamma"), "", "")

    def stop(self) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            rename_unit.stop_unit(self.rename(), lambda: None)
        return output.getvalue()

    def test_rename_refuses_a_paused_workers_live_command_inside_the_worktree_without_signalling(self) -> None:
        run = self.root / "runs" / "run-a"
        record = run / retire_unit.DETACHED_RUNS / "lint.json"
        _ = record.write_text(json.dumps({"status": "running", "pid": 101, "worker": "w7", "cwd": str(self.worktree)}))
        _ = (run / "progress_history_state.json").write_text(json.dumps({"working_dir": str(self.worktree),
            "production": SLUG, "unit": "alpha-unit"}))
        with mock.patch.dict(os.environ, {"UNIT_WORKERS_STATE": str(self.root / "unit-workers")}):
            store = workers.UnitWorkerLedger(workers.ledger_path(SLUG, "alpha-unit"))
            with store.locked() as ledger:
                ledger.workers["w7"] = workers.UnitWorker("w7", "impl", "codex", "sol", "high", {},
                    workers.CodexThread("kept-thread", str(self.root / "codex")), workers.Paused(),
                    [workers.WorkerEvent("opened", "fixture", str(run), 1_793_188_800.0, {})])
            with self.assertRaisesRegex(RenameRefused, r"detached run lint of w7 works inside"):
                _ = self.stop()
        self.assertEqual(self.signalled, [])
        self.assertIn(101, self.processes)
        self.assertIn(102, self.processes)
        self.assertTrue(record.exists())

    def test_the_stop_ends_the_runs_supervisor_and_watcher_and_leaves_the_rest(self) -> None:
        printed = self.stop()
        self.assertEqual(sorted(self.signalled), [101, 102, 103, 201, 202])
        self.assertIn("stopped: process 101 (", printed)
        self.assertIn("of the run supervisor 101 of lint in run run-a", printed)
        self.assertIn("of the app-server watcher 201 of run run-a", printed)
        # What the unit's runs do not own still blocks the move.
        with self.assertRaisesRegex(RenameRefused, r"^processes 104, 203, 900 work inside "):
            rename_unit._check_stopped(self.rename())  # pyright: ignore[reportPrivateUsage]

    def test_with_only_its_runs_left_the_stopped_unit_can_move(self) -> None:
        for pid in (104, 203, 900):
            del self.processes[pid]
        _ = self.stop()
        rename_unit._check_stopped(self.rename())  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(sorted(self.processes), [301, 302])

    def test_a_process_that_outlives_its_sigterm_refuses_the_rename(self) -> None:
        self.processes = {pid: item for pid, item in self.processes.items() if pid in (101, 102)}
        _ = self.enterContext(mock.patch.object(os, "kill", self.survive))
        _ = self.enterContext(mock.patch.object(rename_unit, "RUN_STOP_WAIT_SECONDS", 0.0))
        with self.assertRaisesRegex(RenameRefused, r"of lint in run run-a: still runs after SIGTERM"):
            _ = self.stop()

    def serve(self, code: int) -> list[list[str]]:
        """Run run-a's app-server 501 with its code-mode host 502; `codex_mesh.py stop` exits `code`
        and, when it is 0, ends them. The calls come back."""
        folder = self.root / "runs" / "run-a"
        _ = (folder / "mesh_server.json").write_text(json.dumps({"pid": 501, "port": 51101}))
        self.processes = {501: FakeProcess(self.worktree, 501, ["codex", "app-server", "--listen", "ws://x"]),
                          502: FakeProcess(self.worktree, 501, ["codex-code-mode-host"])}
        calls: list[list[str]] = []

        def mesh(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
            del cwd
            calls.append(argv)
            if not code:
                self.processes.clear()
            return subprocess.CompletedProcess(argv, code, "", "" if not code else "no server")

        _ = self.enterContext(mock.patch.object(retire_unit, "run", mesh))
        return calls

    def test_the_stop_ends_the_runs_app_server_and_its_seats(self) -> None:
        """The real case: modules-unit's 8-hour app-server and its code-mode host refused step 1."""
        calls = self.serve(0)
        rename_unit.check_stoppable(self.rename())
        printed = self.stop()
        self.assertEqual([call[2:] for call in calls], [["stop", "--pause", "--session-dir", str(self.root / "runs" / "run-a")]])
        self.assertIn("stopped: app-server pid 501 of run run-a", printed)
        rename_unit._check_stopped(self.rename())  # pyright: ignore[reportPrivateUsage]

    def test_an_app_server_its_stop_misses_gets_sigterm_with_its_children(self) -> None:
        _ = self.serve(1)
        printed = self.stop()
        self.assertEqual(sorted(self.signalled), [501, 502])
        self.assertIn("no server; SIGTERM instead", printed)
        self.assertIn("of the app-server 501 of run run-a", printed)

    def test_a_resumed_rename_ends_a_run_left_under_the_former_root(self) -> None:
        """The real case: the journal was past its stop step before the stop ended runs, and the
        run started under /tmp/claude/delegate, before the rename to /tmp/claude/unit."""
        former = self.root / "delegate"
        folder = former / "run-old"
        (folder / retire_unit.DETACHED_RUNS).mkdir(parents=True)
        _ = (folder / unit_runs.STATE_FILE).write_text(json.dumps({"working_dir": str(self.worktree)}))
        record = self.run_record(folder, "lint-old", 401)
        self.processes = {401: FakeProcess(self.worktree, 401, ["python3", "mesh_runs.py", "supervise", str(record)]),
                          402: FakeProcess(self.worktree, 401, ["bash", "verify.sh", "lint", "pkg"])}
        self.enterContext(mock.patch.dict(os.environ, {"RETIRE_UNIT_RUN_ROOT": ""}))
        _ = self.enterContext(mock.patch.object(unit_runs, "RUN_ROOT", self.root / "unit"))
        _ = self.enterContext(mock.patch.object(unit_runs, "FORMER_RUN_ROOT", former))
        output = io.StringIO()
        with redirect_stdout(output):
            rename_unit._check_stopped(self.rename())  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(sorted(self.signalled), [401, 402])
        self.assertIn("of the run supervisor 401 of lint-old in run run-old", output.getvalue())


if __name__ == "__main__":
    _ = unittest.main()
