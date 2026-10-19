"""Rename one unit of a disposable production against a local bare origin and isolated state."""

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

import fake_showrunner
import fake_unit_host

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
            "HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(config),
            "XDG_CONFIG_HOME": str(self.home / ".config"), "XDG_STATE_HOME": str(self.root / "state"),
            "NOTIFIER_STATE_DIR": str(self.root / "state/notifier"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "state/sessions"),
            "SHOWRUNNER_STATE_DIR": str(self.state),
            "STALL_WATCH_STATE_DIR": str(self.root / "state/stall-watch"),
            "REVIEW_POOL_STATE": str(self.root / "state/review-pool"),
            "SHOWRUNNERS_CONFIG": str(config / "showrunners.json"),
            "PLAN_DELEGATE_ACTIVE_DIR": str(self.root / "active"),
            "PLAN_DELEGATE_HISTORY_DIR": str(self.root / "history"),
            "PROGRESS_SESSION_ROOTS": str(self.root / "sessions"),
            "PHASE_TABLE_VAULT": str(self.root / "vault/showrunners"),
            "HERDR_BIN": str(SCRIPT.parents[1] / "herdr/fake_herdr.py"),
            "HERDR_PROC": str(self.root / "proc"),
            "HERDR_PLATFORM": "linux",
            "FAKE_HERDR_LOG": str(self.root / "herdr.jsonl"),
            "FAKE_HERDR_RESPONSES": str(self.root / "herdr-responses.json"),
            "CLAUDE_CODE_SESSION_ID": "", "USER_ZONE": "UTC", "PLAN_DELEGATE_SYNC_WRITES": "0",
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
        self.write(self.root / "state/review-pool/demo/memory/alpha-unit.md", "alpha memory\n")
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

    def test_plan_writes_nothing_and_names_each_step(self) -> None:
        before = self.snapshot()
        result = self.rename("plan")
        self.assert_ok(result)
        self.assertEqual(self.snapshot(), before)
        self.assertIn(f"branch demo-alpha -> demo-gamma; worktree {self.rust / 'claude-demo-alpha'} -> "
                      + str(self.rust / "claude-demo-gamma"), result.stdout)
        self.assertIn("'chore(demo): rename unit alpha-unit to gamma-unit'", result.stdout)

    def test_apply_moves_every_place(self) -> None:
        self.assert_ok(self.rename("apply", "alpha-unit", "gamma-unit"))
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
        self.assertTrue((self.root / "state/review-pool/demo/memory/gamma-unit.md").exists())
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

    def seed_record(self, progress: str = "stopped", name: str = "alpha") -> Path:
        """A stopped unit's shutdown record, as `shutdown.py down` leaves it."""
        old = self.rust / "claude-demo-alpha"
        session: dict[str, object] = {
            "session_id": "sid-alpha", "config_dir": str(self.home / ".claude"), "pid": 4242, "proc_start": "1",
            "name": name, "cwd": f"{old}/sub", "status": "idle", "updated_at": 0, "probe": False,
            "model": {"kind": "no reply yet"},
            "checkout": {"kind": "git", "path": str(old), "head": {"kind": "branch", "name": "demo-alpha"},
                         "upstream": {"kind": "tracking", "ahead": 0}, "dirty": []},
            "run_dirs": [], "codex_servers": [], "timers": [], "kind": "unit",
            "host": {"kind": "unit", "production": SLUG, "unit": "alpha-unit", "doc": str(self.doc),
                     "session_name": "demo-alpha", "session_host": "herdr",
                     "plan": {"kind": "plan", "path": f"{old}/docs/plans/alpha.md"}}}
        saved: dict[str, object] = {
            "login": "owner@example.com", "label": "claude2", "machine": "natedev",
            "requested_at": "2026-10-09T21:49:10+00:00", "request_id": "request",
            "requested_by": {"kind": "terminal"}, "hold": "session", "then": "stop", "force": "wait for ready",
            "alerts": "none", "probe": False, "session": session, "workers": [], "timers": [],
            "settle_message": {"kind": "not sent"}, "where": {"kind": "not said"},
            "progress": {"kind": progress, "at": "2026-10-09T21:50:00+00:00"}, "stop_issues": [], "trail": []}
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

    def test_a_chosen_session_name_stays(self) -> None:
        _ = self.seed_record(name="showrunner-fixer")
        self.assert_refused(self.rename("apply"), "gamma-unit is not running")
        self.assertEqual(cast(dict[str, object], self.record()["session"])["name"], "showrunner-fixer")

    def test_apply_waits_while_the_unit_is_stopping(self) -> None:
        _ = self.seed_record(progress="waiting")
        result = self.rename("apply")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("alpha-unit is stopping (waiting); rerun apply once it has stopped", result.stderr)
        self.assertEqual(self.journal()["step"], 0)

    def test_verify_fails_on_an_unclassified_stale_reference(self) -> None:
        self.assert_ok(self.rename("apply"))
        self.write(self.root / "state/nightly-review/2026-10-16/report.md", "alpha-unit reviewed\n")
        self.write(self.root / "state/other/names.json", '{"unit": "claude-demo-alphabet"}\n')
        self.assert_ok(self.rename("verify"))
        stale = self.root / "state/other/cache.json"
        self.write(stale, '{"unit": "alpha-unit"}\n')
        self.write(self.root / "scratch/showrunner_state.json", '{"units": {"alpha-unit": {}}}\n')
        result = self.rename("verify")
        self.assert_refused(result, f"unclassified: {stale} still holds an old name")
        self.assertIn(f"unclassified: {self.root / 'scratch/showrunner_state.json'} still holds", result.stderr)
        self.assertNotIn("nightly-review", result.stderr)
        self.assertNotIn("names.json", result.stderr)

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

    def test_add_unit_refuses_a_unit_being_renamed(self) -> None:
        self.assert_refused(self.rename("apply", refuse="push origin demo:refs/heads/demo"), "simulated failure")
        claim = subprocess.run(
            [sys.executable, "-c", "import rename_state; print(' '.join(sorted(rename_state.rename_claim('demo'))))"],
            cwd=SCRIPT.parent, env=self.env, capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(claim.stdout.strip(), "alpha-unit gamma-unit", claim.stderr)

    def test_refusals(self) -> None:
        cases = [
            (("alpha", "review-unit"), "is known to code"),
            (("alpha", "mul_add"), "already in the Units table"),
            (("alpha", "bad name"), "letters, numbers"),
            (("missing", "gamma"), "expected one Units row"),
        ]
        for (old, new), text in cases:
            with self.subTest(old=old, new=new):
                self.assert_refused(self.rename("apply", old, new), text)
        self.assertFalse((self.state / SLUG / "rename_unit.json").exists())

    def mark_live(self, unit: str, session_id: str) -> None:
        """A live Claude in a herdr session marked as `unit` of this production."""
        held = fake_showrunner.write_session(self.root / "state/sessions", unit.removesuffix("-unit"), session_id)
        self.addCleanup(held.close)
        fake_unit_host.write(self.env, [fake_unit_host.HostedUnit(
            f"demo-{unit}", "w1:p1", {"SHOWRUNNER_UNIT": SLUG, "SHOWRUNNER_UNIT_ID": unit})])
        fake_unit_host.place_process(self.env, os.getpid(), "w1:p1")

    def test_a_running_unit_plans_and_apply_stops_it_through_the_shutdown_path(self) -> None:
        log = self.seed_record()
        (self.home / ".local/state/shutdown/owner@example.com/sessions/sid-alpha.json").unlink()
        self.mark_live("alpha-unit", "sid-alpha")
        planned = self.rename("plan")
        self.assert_ok(planned)
        self.assertIn("1. stop alpha-unit through the shutdown path", planned.stdout)
        applied = self.rename("apply")
        self.assertEqual(applied.returncode, 2, applied.stdout + applied.stderr)
        self.assertIn("alpha-unit is stopping; rerun apply once it has stopped", applied.stderr)
        self.assertEqual([json.loads(line) for line in log.read_text().splitlines()],
                         [["down", "--only", "sid-alpha", "--here"]])
        self.assertEqual(self.journal()["step"], 0)

    def test_a_running_new_id_is_refused(self) -> None:
        self.mark_live("gamma-unit", "sid-gamma")
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


if __name__ == "__main__":
    _ = unittest.main()
