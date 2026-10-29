"""Move one unit between two disposable productions of one repository, against a local bare origin and isolated state."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, final, override
from unittest import mock

from scripts.astra import design_roles
from scripts.direct import workers

SCRIPT = Path(__file__).with_name("transfer_unit.py")


@final
class UnitTransferTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.rust = Path()
        self.giving = Path()
        self.receiving = Path()
        self.unit = Path()
        self.env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        home = self.root / "home"
        self.rust = home / "rust"
        self.giving = self.rust / "claude-demo-trunk"
        self.receiving = self.rust / "claude-other-trunk"
        self.unit = self.rust / "claude-demo-alpha"
        config = home / ".claude"
        for path in (self.giving, config, self.root / "state/sessions"):
            path.mkdir(parents=True)
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update({
            "UNIT_WORKERS_STATE": str(self.root / "state/unit-workers"),
            "RETIRE_UNIT_RUN_ROOT": str(self.root / "runs"),
            "HOME": str(home), "CLAUDE_CONFIG_DIR": str(config),
            "XDG_CONFIG_HOME": str(home / ".config"), "XDG_STATE_HOME": str(self.root / "state"),
            "NOTIFIER_STATE_DIR": str(self.root / "state/notifier"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "state/sessions"),
            "SHOWRUNNER_STATE_DIR": str(self.root / "state/showrunner"),
            "STALL_WATCH_STATE_DIR": str(self.root / "state/stall-watch"),
            "RENAME_UNIT_STOP_WAIT": "0", "RENAME_UNIT_LIVE_WAIT": "0",
            "REVIEW_POOL_STATE": str(self.root / "state/review-pool"),
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
        origin = self.root / "origin.git"
        _ = self.git("init", "--bare", str(origin))
        _ = self.git("init", "-b", "demo")
        for key, value in (("user.name", "Unit Transfer Test"), ("user.email", "transfer@example.invalid"),
                           ("gc.auto", "0"), ("maintenance.auto", "false")):
            _ = self.git("config", key, value)
        _ = self.git("remote", "add", "origin", str(origin))
        self.write(self.giving / "docs/plans/demo-production.md", self.production_doc(
            "demo", self.giving, "| Unit | Plan | Worktree | Branch |", "| --- | --- | --- | --- |",
            f"| alpha-unit | docs/plans/alpha.md | `{self.unit}` | demo-alpha |",
            f"| beta-unit | docs/plans/beta.md | `{self.rust / 'claude-demo-beta'}` | demo-beta |"))
        self.write(self.giving / "docs/plans/other-production.md", self.production_doc(
            "other", self.receiving, "| Unit | Plan | Worktree | Branch | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- |", "| gamma-unit | docs/plans/gamma.md | `/x` | other-gamma | — | — |"))
        self.write(self.giving / "docs/plans/alpha.md",
                   "# alpha\n\n> **Production: demo** — unit `alpha-unit`; production doc "
                   + "`docs/plans/demo-production.md`\n\nalpha prose stays.\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "fixture")
        _ = self.git("push", "-u", "origin", "demo")
        _ = self.git("branch", "other")
        _ = self.git("push", "origin", "other")
        _ = self.git("worktree", "add", str(self.receiving), "other")
        _ = self.git("worktree", "add", "-b", "demo-alpha", str(self.unit), "demo")
        _ = self.git("push", "origin", "demo-alpha")
        self.write(self.root / "state/notifier/showrunner-demo/conf",
                   "EVERY=15\nTARGET=session:test-showrunner\nCHECK="
                   + shlex.join(["zsh", "/scripts/production_check.sh",
                                 str(self.giving / "docs/plans/demo-production.md")]) + "\n")
        self.write(self.stall_path("demo", "alpha-unit"), '{"stretch": 17}\n')
        # A reviewer with no thread retires without a Codex call.
        self.write(self.root / "state/review-pool/demo/pool.json", json.dumps({"generation": 1, "reviewers": {
            "r1": {"type": "codex", "thread_id": "", "prime_hash": "h", "lease": {"unit": "alpha-unit", "ordinal": 3},
                   "busy_pid": None, "reviews": 2}}}))

    def production_doc(self, slug: str, checkout: Path, *table: str) -> str:
        return "\n".join(["# Production — " + slug, "", "## Production Context", "",
                          f"- **Merge branch:** `{slug}`", f"- **Showrunner checkout:** `{checkout}`",
                          "- **Log:** `production.log`", "", "## Units", "", *table, "",
                          "## Production rules", "", "Rules.", ""])

    def write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content, encoding="utf-8")

    def git(self, *arguments: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", *arguments], cwd=cwd or self.giving, env=self.env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, (arguments, result.stdout, result.stderr))
        return result.stdout.strip()

    def stall_path(self, slug: str, unit: str) -> Path:
        digest = hashlib.sha256(f"{slug}\0{unit}".encode()).hexdigest()
        return self.root / "state/stall-watch" / f"{digest}.json"

    def transfer(self, verb: str, unit: str = "alpha-unit", to: Path | None = None) -> subprocess.CompletedProcess[str]:
        to_doc = to or self.receiving / "docs/plans/other-production.md"
        return subprocess.run([sys.executable, str(SCRIPT), verb, "demo", unit, str(to_doc),
                               "--scratch", str(self.root / "scratch")], cwd=self.giving, env=self.env,
                              capture_output=True, text=True, check=False, timeout=120)

    def assert_ok(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_refused(self, result: subprocess.CompletedProcess[str], text: str) -> None:
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(text, result.stderr)

    def seed_record(self) -> Path:
        """A stopped unit's shutdown record, as `shutdown.py down` leaves it, and a shutdown stub that logs calls."""
        home = self.root / "home"
        session: dict[str, object] = {
            "session_id": "sid-alpha", "config_dir": str(home / ".claude"), "pid": 4242, "proc_start": "1",
            "name": "alpha", "cwd": str(self.unit), "status": "idle", "updated_at": 0, "probe": False,
            "model": {"kind": "no reply yet"},
            "checkout": {"kind": "git", "path": str(self.unit), "head": {"kind": "branch", "name": "demo-alpha"},
                         "upstream": {"kind": "tracking", "ahead": 0}, "dirty": []},
            "run_dirs": [], "codex_servers": [], "timers": [], "kind": "unit",
            "host": {"kind": "unit", "production": "demo", "unit": "alpha-unit",
                     "doc": str(self.giving / "docs/plans/demo-production.md"), "session_name": "demo-alpha",
                     "session_host": "herdr", "plan": {"kind": "plan", "path": f"{self.unit}/docs/plans/alpha.md"}}}
        saved: dict[str, object] = {
            "login": "owner@example.com", "label": "claude2", "machine": "natedev",
            "requested_at": "2026-10-09T21:49:10+00:00", "request_id": "request",
            "requested_by": {"kind": "terminal"}, "hold": "session", "then": "stop", "force": "wait for ready",
            "alerts": "none", "probe": False, "session": session, "workers": [], "timers": [],
            "settle_message": {"kind": "not sent"}, "where": {"kind": "not said"},
            "progress": {"kind": "stopped", "at": "2026-10-09T21:50:00+00:00"}, "stop_issues": [], "trail": [],
            "codex_seats": []}
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

    def test_a_stopped_unit_relaunches_from_a_record_that_names_the_receiving_production(self) -> None:
        log = self.seed_record()
        result = self.transfer("apply")
        # The fake restart starts nothing, so verify finds the unit not running and holds the journal open.
        self.assert_refused(result, "alpha-unit is not running under other")
        self.assertEqual([json.loads(line) for line in log.read_text().splitlines()],
                         [["restart", "--only", "sid-alpha", "--here"]])
        path = self.root / "home/.local/state/shutdown/owner@example.com/sessions/sid-alpha.json"
        saved = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        host = cast(dict[str, object], cast(dict[str, object], saved["session"])["host"])
        receiving = self.receiving / "docs/plans/other-production.md"
        self.assertEqual((host["production"], host["doc"], host["unit"], host["session_name"]),
                         ("other", str(receiving), "alpha-unit", "other-alpha"))
        note = cast(str, saved["renamed"])
        self.assertIn(f"moved you to production other: your production doc is now {receiving}", note)
        self.assertIn(f"Your worktree {self.unit}, your branch demo-alpha and its commits stay.", note)

    def test_plan_lists_the_steps_and_changes_nothing(self) -> None:
        before = self.git("rev-parse", "demo", "other", "demo-alpha")
        result = self.transfer("plan")
        self.assert_ok(result)
        self.assertIn("stop alpha-unit and its Codex workers through the shutdown path", result.stdout)
        self.assertIn("8. verify: alpha-unit shows under other and nowhere under demo", result.stdout)
        self.assertEqual(self.git("rev-parse", "demo", "other", "demo-alpha"), before)

    def test_worker_migration_conflict_prints_a_refusal_without_a_traceback(self) -> None:
        result = subprocess.run([sys.executable, "-c", 'import runpy, sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(sys.argv[1]).resolve().parents[2]))\nfrom scripts.direct import workers\ndef unavailable(*args, **kwargs):\n    raise workers.WorkerUnavailable("worker ownership changed; rerun apply")\nworkers.move_unit = unavailable\nworkers.rename_unit = unavailable\nworkers.end_unit = unavailable\nsys.argv = sys.argv[1:]\nsys.path.insert(0, str(Path(sys.argv[0]).parent))\nrunpy.run_path(sys.argv[0], run_name="__main__")\n', str(SCRIPT), "apply", "demo", "alpha-unit",
            str(self.receiving / "docs/plans/other-production.md"), "--scratch", str(self.root / "scratch")],
            cwd=self.giving, env=self.env, capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("worker ownership changed; rerun apply", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_transfer_rebinds_the_writer_ledger_without_changing_its_thread_or_owner(self) -> None:
        run = self.root / "runs" / "carried"
        run.mkdir(parents=True)
        state = run / "progress_history_state.json"
        self.write(state, json.dumps({"production": "demo", "unit": "alpha-unit", "working_dir": str(self.unit)}))
        with mock.patch.dict(os.environ, self.env):
            store = workers.UnitWorkerLedger(workers.ledger_path("demo", "alpha-unit"))
            worker = workers.UnitWorker("w7", "impl", "codex", "kept-model", "xhigh", {},
                workers.CodexThread("original-thread", str(self.root / "codex")), workers.Paused(),
                [workers.WorkerEvent("opened", "fixture", str(run), 1_793_188_800.0, {})])
            with store.locked() as ledger:
                ledger.workers[worker.name] = worker
        roster = run / "mesh_roster.json"
        self.write(roster, json.dumps({"w7": {"thread_id": "original-thread", "role": "impl",
            "status": "paused", "unit_ledger": str(store.path)}}))
        record = run / "mesh_runs" / "kept-command.json"
        with subprocess.Popen([sys.executable, "-c", "import sys; from pathlib import Path; sys.stdin.read(); Path('command-finished').write_text(str(Path.cwd()))",
                               "mesh_runs.py", "supervise", str(record)],
                              cwd=self.unit, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as command:
            self.write(record, json.dumps({"worker": "w7", "status": "running", "pid": command.pid, "cwd": str(self.unit)}))
            self.assert_ok(self.transfer("apply"))
            self.assertIsNone(command.poll(), "transfer ended the paused worker's detached command")
            _output, errors = command.communicate(timeout=5)
            self.assertEqual(command.returncode, 0, errors.decode())
            self.assertEqual((self.unit / "command-finished").read_text(), str(self.unit))
            self.assertTrue(record.exists())
        with mock.patch.dict(os.environ, self.env):
            target = workers.ledger_for_run(run)
            self.assertEqual(target.path, workers.ledger_path("other", "alpha-unit"))
            with target.locked() as ledger:
                self.assertEqual(ledger.workers["w7"], worker)
        self.assertFalse(store.path.exists())
        self.assertEqual(json.loads(roster.read_text())["w7"]["unit_ledger"], str(target.path))

    def test_apply_moves_the_row_header_and_state_and_keeps_the_branch(self) -> None:
        tip = self.git("rev-parse", "demo-alpha")
        self.assert_ok(self.transfer("apply"))
        receiving = (self.receiving / "docs/plans/other-production.md").read_text(encoding="utf-8")
        self.assertIn(f"| alpha-unit | docs/plans/alpha.md | `{self.unit}` | demo-alpha | — | — |", receiving)
        giving = (self.giving / "docs/plans/demo-production.md").read_text(encoding="utf-8")
        self.assertNotIn("alpha-unit", giving)
        self.assertIn("beta-unit", giving)
        self.assertEqual(self.git("log", "-1", "--format=%s", "other"), "chore(other): transfer unit alpha-unit from demo")
        self.assertEqual(self.git("log", "-1", "--format=%s", "demo"), "chore(demo): transfer unit alpha-unit to other")
        plan = (self.unit / "docs/plans/alpha.md").read_text(encoding="utf-8")
        self.assertIn("> **Production: other** — unit `alpha-unit`; production doc `docs/plans/other-production.md`", plan)
        self.assertIn("alpha prose stays.", plan)
        # The branch keeps its commits and gains only the header commit, which is pushed.
        self.assertEqual(self.git("rev-parse", "demo-alpha^"), tip)
        self.assertEqual(self.git("rev-parse", "origin/demo-alpha"), self.git("rev-parse", "demo-alpha"))
        self.assertTrue(self.unit.is_dir())
        self.assertFalse(self.stall_path("demo", "alpha-unit").exists())
        self.assertEqual(self.stall_path("other", "alpha-unit").read_text(encoding="utf-8"), '{"stretch": 17}\n')
        pool = (self.root / "state/review-pool/demo/pool.json").read_text(encoding="utf-8")
        self.assertNotIn("alpha-unit", pool)
        self.assert_ok(self.transfer("verify"))

    def test_a_held_role_moves_to_the_receiving_holder_and_keeps_its_thread_and_paths(self) -> None:
        config = self.root / "design_roles.conf"
        self.write(config, "[ownership]\nhost=fixture-host\n")
        self.env.update({"DESIGN_ROLES_CONFIG": str(config), "DESIGN_ROLES_HOST": "fixture-host",
                         "DESIGN_ROLES_STATE": str(self.root / "state/astra/design-roles"),
                         "DESIGN_OWNERSHIP_STATE": str(self.root / "state/astra/design-ownership")})
        document = str(self.unit / "docs/plans/alpha.md")
        with mock.patch.dict(os.environ, self.env):
            design_roles.register_repository("fixture-repo", {"fixture-host": str(self.unit)}, inventory_complete=True)
            placement = design_roles.authority_request("reserve", {"repository_id": "fixture-repo",
                "document": "docs/plans/alpha.md", "host": "fixture-host", "request_id": "transfer-fixture"})
            design = design_roles.HeldDesign(design_roles.DesignPlacement(str(placement["design_id"]), "fixture-host", 1),
                "fixture-repo", str(self.unit), document, "design", "2026-10-22T12:00:00+00:00",
                design_roles.UnitHolder("demo", "alpha-unit"), design_roles.DesignUnbound(),
                (design_roles.RoleInstance("designer", "demo-design-astra-designer", design_roles.RoleOpen("designer-thread")),
                 design_roles.RoleInstance("reviewer", "demo-design-astra-reviewer", design_roles.RoleNotOpened())))
            design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
            mesh = design_roles.mesh_directory(design.placement.design_id)
        self.write(mesh / "mesh_roster.json", json.dumps({"demo-design-astra-designer": {
            "thread_id": "designer-thread", "status": "done", "cwd": str(self.unit)}}))
        self.assert_ok(self.transfer("apply"))
        with mock.patch.dict(os.environ, self.env):
            moved = design_roles.read_design(design.placement.design_id)
        self.assertEqual(moved.holder, design_roles.UnitHolder("other", "alpha-unit"))
        self.assertEqual((moved.repository, moved.document, moved.roles), (design.repository, design.document, design.roles))
        self.assertEqual(moved.placement.generation, 2)
        roster = cast(dict[str, dict[str, str]], json.loads((mesh / "mesh_roster.json").read_text(encoding="utf-8")))
        self.assertEqual(roster["demo-design-astra-designer"]["cwd"], str(self.unit))
        self.assert_ok(self.transfer("verify"))

    def test_work_in_progress_stays_and_only_the_header_is_committed(self) -> None:
        # showrunner, 2026-10-21: hierarchies-unit had its plan and other files edited, not committed.
        plan = self.unit / "docs/plans/alpha.md"
        self.write(plan, plan.read_text(encoding="utf-8") + "A new step, not committed.\n")
        self.write(self.unit / "measure.py", "staged = True\n")
        _ = self.git("add", "measure.py", cwd=self.unit)
        tip = self.git("rev-parse", "demo-alpha")
        self.assert_ok(self.transfer("apply"))
        self.assertEqual(self.git("rev-parse", "demo-alpha^"), tip)
        self.assertEqual(self.git("diff", "--name-only", "demo-alpha^", "demo-alpha"), "docs/plans/alpha.md")
        committed = self.git("show", "demo-alpha:docs/plans/alpha.md")
        self.assertIn("> **Production: other**", committed)
        self.assertNotIn("A new step", committed)
        text = plan.read_text(encoding="utf-8")
        self.assertIn("> **Production: other**", text)
        self.assertIn("A new step, not committed.", text)
        self.assertEqual(self.git("status", "--porcelain", cwd=self.unit), "M docs/plans/alpha.md\nA  measure.py")
        self.assertEqual(self.git("diff", "--cached", "--name-only", cwd=self.unit), "measure.py")

    def test_plan_refuses_a_receiving_doc_with_uncommitted_changes_before_any_stop(self) -> None:
        doc = self.receiving / "docs/plans/other-production.md"
        self.write(doc, doc.read_text(encoding="utf-8") + "edited\n")
        self.assert_refused(self.transfer("plan"), "other-production.md has uncommitted changes")

    def test_a_merge_in_progress_is_refused(self) -> None:
        self.write(self.unit / "NOTES.md", "unit side\n")
        _ = self.git("add", "NOTES.md", cwd=self.unit)
        _ = self.git("commit", "-m", "unit side", cwd=self.unit)
        self.write(self.receiving / "NOTES.md", "receiving side\n")
        _ = self.git("add", "NOTES.md", cwd=self.receiving)
        _ = self.git("commit", "-m", "receiving side", cwd=self.receiving)
        merged = subprocess.run(["git", "merge", "--no-edit", "demo-alpha"], cwd=self.receiving, env=self.env,
                                capture_output=True, text=True, check=False)
        self.assertNotEqual(merged.returncode, 0)
        self.assert_refused(self.transfer("plan"), "a merge is in progress")

    def test_giving_only_commits_are_refused(self) -> None:
        self.write(self.giving / "LATER.md", "giving only\n")
        _ = self.git("add", "LATER.md")
        _ = self.git("commit", "-m", "giving only")
        _ = self.git("push", "origin", "demo")
        _ = self.git("merge", "--no-edit", "demo", cwd=self.unit)
        self.assert_refused(self.transfer("plan"), "commit(s) that other lacks")

    def test_another_repository_is_refused(self) -> None:
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        _ = self.git("init", "-b", "far", cwd=elsewhere)
        self.write(elsewhere / "docs/plans/far-production.md", self.production_doc(
            "far", elsewhere, "| Unit | Plan | Worktree | Branch |", "| --- | --- | --- | --- |"))
        self.assert_refused(self.transfer("plan", to=elsewhere / "docs/plans/far-production.md"),
                            "another repository")

    def test_a_unit_without_a_worktree_here_is_refused(self) -> None:
        self.assert_refused(self.transfer("plan", unit="beta-unit"), "a Mac unit is not moved")


if __name__ == "__main__":
    _ = unittest.main()
