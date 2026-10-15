"""Rename a disposable production against a local bare origin and isolated state."""

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


SCRIPT = Path(__file__).with_name("rename.py")
OLD = "old"
NEW = "new"
UNITS = ("alpha-unit", "beta-unit")

# Fail an actual filesystem move in step 4, after the preceding CLI steps have run.
# Both directory-move primitives are covered without adding a failure switch to production code.
FAIL_STATE_MOVE = '''import os, runpy, sys
from pathlib import Path

state = Path(os.environ["SHOWRUNNER_STATE_DIR"])
blocked = state / "review-paused" / "old.json"
def refusing_move(original):
    def move(source, destination, *args, **kwargs):
        if Path(source) == blocked:
            raise OSError("simulated step 4 state move failure")
        return original(source, destination, *args, **kwargs)
    return move
os.rename = refusing_move(os.rename)
os.replace = refusing_move(os.replace)
sys.argv = sys.argv[1:]
sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
runpy.run_path(sys.argv[0], run_name="__main__")
'''

CHECK_TIMER_HANDOFF = '''import os, runpy, subprocess, sys
from pathlib import Path

sys.argv = sys.argv[1:]
sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
import showrunners

notifier = Path(os.environ["NOTIFIER_STATE_DIR"])
old_timer = notifier / "showrunner-old"
new_timer = notifier / "showrunner-new"
seen = []
def readable_timer(instance):
    checked = showrunners.checked_doc(instance)
    assert isinstance(checked, showrunners.CheckedDoc), checked
    context = checked.path.read_text().split("## Production Context", 1)[1].split("\\n## ", 1)[0]
    assert "- **Formerly:** `old`" in context, context
    assert instance.name.removeprefix("showrunner-") in showrunners.timer_slugs()
original_run = subprocess.run
def observing_run(command, *args, **kwargs):
    moving_doc = len(command) > 3 and command[0] == "git" and command[3] == "mv"
    if moving_doc:
        readable_timer(old_timer)
    result = original_run(command, *args, **kwargs)
    if moving_doc:
        readable_timer(old_timer)
        seen.append("doc")
    return result
original_rename = os.rename
def observing_rename(source, destination, *args, **kwargs):
    moving_timer = Path(source) == old_timer
    if moving_timer:
        readable_timer(old_timer)
    result = original_rename(source, destination, *args, **kwargs)
    if moving_timer:
        readable_timer(new_timer)
        assert showrunners.canonical_slug("old") == "new"
        seen.append("timer")
    return result
subprocess.run = observing_run
os.rename = observing_rename
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit as exit:
    if exit.code == 0:
        assert seen == ["doc", "timer"], seen
    raise
'''

FAIL_PLAN_REWRITE = '''import os, runpy, sys
from pathlib import Path

original_replace = os.replace
def refusing_replace(source, destination, *args, **kwargs):
    if Path(destination).name == "alpha-unit.md":
        raise OSError("simulated unit header rewrite failure")
    return original_replace(source, destination, *args, **kwargs)
os.replace = refusing_replace
sys.argv = sys.argv[1:]
sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
runpy.run_path(sys.argv[0], run_name="__main__")
'''

FAIL_GIT_MOVE_INDEX = '''import runpy, subprocess, sys
from pathlib import Path

original_run = subprocess.run
def interrupted_move(command, *args, **kwargs):
    if len(command) > 3 and command[0] == "git" and command[3] == "mv":
        checkout = Path(command[2])
        (checkout / command[-2]).replace(checkout / command[-1])
        return subprocess.CompletedProcess(command, 1, "", "simulated git mv index failure")
    return original_run(command, *args, **kwargs)
subprocess.run = interrupted_move
sys.argv = sys.argv[1:]
sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
runpy.run_path(sys.argv[0], run_name="__main__")
'''


@final
class ProductionRenameTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.home = Path()
        self.checkout = Path()
        self.origin = Path()
        self.doc = Path()
        self.new_doc = Path()
        self.notifier = Path()
        self.state = Path()
        self.stall = Path()
        self.pool = Path()
        self.worktrees = Path()
        self.env: dict[str, str] = {}
        self.old_tip = ""

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / "home"
        self.checkout = self.root / "checkout"
        self.origin = self.root / "origin.git"
        self.doc = self.checkout / "docs/plans/old-production.md"
        self.new_doc = self.doc.with_name("new-production.md")
        self.notifier = self.root / "state/notifier"
        self.state = self.root / "state/showrunner"
        self.stall = self.root / "state/stall-watch"
        self.pool = self.root / "state/review-pool"
        self.worktrees = self.home / "worktrees"
        for path in (self.home, self.checkout, self.notifier, self.stall, self.pool, self.worktrees):
            path.mkdir(parents=True)
        config = self.home / ".claude"
        config.mkdir()
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update({
            "HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(config),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "NOTIFIER_STATE_DIR": str(self.notifier),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "state/sessions"),
            "SHOWRUNNER_STATE_DIR": str(self.state),
            "STALL_WATCH_STATE_DIR": str(self.stall), "REVIEW_POOL_STATE": str(self.pool),
            "SHOWRUNNERS_CONFIG": str(config / "showrunners.json"),
            "PLAN_DELEGATE_ACTIVE_DIR": str(self.root / "active"),
            "PLAN_DELEGATE_HISTORY_DIR": str(self.root / "history"),
            "CLAUDE_CODE_SESSION_ID": "", "USER_ZONE": "UTC",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
        })
        _ = self.git("init", "--bare", str(self.origin))
        _ = self.git("init", "-b", OLD)
        _ = self.git("config", "user.name", "Production Rename Test")
        _ = self.git("config", "user.email", "rename@example.invalid")
        _ = self.git("remote", "add", "origin", str(self.origin))
        self.write(self.doc, self.production_doc())
        for unit in UNITS:
            header = f"> **Production: {OLD}**" if unit == "alpha-unit" else f"> **Production:** {OLD}"
            self.write(self.plan_path(unit),
                       f"# {unit}\n\n{header} — unit `{unit}`; "
                       + "production doc `docs/plans/old-production.md`\n\n"
                       + "Historical example: old stays in ordinary prose.\n")
        self.write(self.checkout / "README.md", "A production whose history says old.\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "merge(old-alpha): fixture history")
        _ = self.git("push", "-u", "origin", OLD)
        self.old_tip = self.git("rev-parse", "HEAD")
        for unit in UNITS:
            _ = self.git("worktree", "add", "-b", f"old-{unit}", str(self.worktrees / unit), OLD)
        self.write(self.notifier / "showrunner-old/conf",
                   "EVERY=15\nTARGET=session:test-showrunner\nCHECK="
                   + shlex.join(["zsh", "/scripts/production_check.sh", str(self.doc)])
                   + "\nPROMPT_FILE=unchanged-prompt\n")
        self.write(self.notifier / "showrunner-old/prompt", "Historical prompt mentions old.\n")
        for relative, content in self.saved_state().items():
            self.write(self.state / relative, content)
        self.write(self.state / "child/parent", "old\n")
        self.write(self.state / "other/parent", "oldish\n")
        for unit in UNITS:
            self.write(self.stall_path(OLD, unit), json.dumps({"unit": unit, "stretch": 17}) + "\n")
        self.write(self.stall_path("unrelated", "alpha-unit"), '{"stretch": 99}\n')
        self.write(self.pool / "old/pool.json", '{"generation": 7, "reviewers": {}}\n')
        self.write(self.pool / "old/alpha-unit/2", "retained reviewer lease\n")

    def write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content, encoding="utf-8")

    def git(self, *arguments: str) -> str:
        result = subprocess.run(["git", *arguments], cwd=self.checkout, env=self.env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, (arguments, result.stdout, result.stderr))
        return result.stdout.strip()

    def plan_path(self, unit: str) -> Path:
        return self.checkout / "docs/plans" / f"{unit}.md"

    def stall_path(self, slug: str, unit: str) -> Path:
        digest = hashlib.sha256(f"{slug}\0{unit}".encode()).hexdigest()
        return self.stall / f"{digest}.json"

    def production_doc(self) -> str:
        rows = [f"| {unit} | docs/plans/{unit}.md | `{self.worktrees / unit}` | old-{unit} |"
                for unit in UNITS]
        return "\n".join([
            "# Production — old", "", "> **Status: PRODUCTION — running.**", "",
            "## Production Context", "", "- **Merge branch:** `old`",
            f"- **Showrunner checkout:** `{self.checkout}`", "- **Log:** `production.log`", "",
            "## Units", "", "| Unit | Plan | Worktree | Branch |", "| --- | --- | --- | --- |",
            *rows, "", "## Production rules", "", "History mentions old and stays unchanged.", "",
        ])

    def saved_state(self) -> dict[str, str]:
        return {"footers-off/old": "", "review-paused/old.json": '{"reason": "old review"}\n',
                "outstanding/old.json": '{"question": "old question"}\n',
                "old/parent": "top\n", "old/log-backups/phase.log": "merge(old): retained log\n"}

    def run_rename(self, action: str, destination: str = NEW, *, fail_state: bool = False,
                   source: str = OLD) -> subprocess.CompletedProcess[str]:
        command = [sys.executable]
        if fail_state:
            command.extend(["-c", FAIL_STATE_MOVE])
        command.extend([str(SCRIPT), action, source, destination])
        return subprocess.run(command, cwd=self.checkout, env=self.env, capture_output=True,
                              text=True, check=False, timeout=60)

    def assert_success(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def snapshot(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def journal(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads((self.state / "new/rename.json").read_text()))

    def assert_applied(self) -> None:
        self.assertFalse(self.doc.exists())
        text = self.new_doc.read_text(encoding="utf-8")
        context = text.split("## Production Context", 1)[1].split("\n## ", 1)[0]
        self.assertRegex(context, r"(?m)^- \*\*Formerly:\*\*.*`old`")
        self.assertIn("- **Merge branch:** `new`", context)
        self.assertIn("History mentions old and stays unchanged.", text)
        for unit in UNITS:
            plan = self.plan_path(unit).read_text(encoding="utf-8")
            self.assertRegex(plan, r"(?m)^> \*\*Production:(?: new\*\*|\*\* new) — unit")
            self.assertIn("production doc `docs/plans/new-production.md`", plan)
            self.assertIn("Historical example: old stays in ordinary prose.", plan)
            self.assertIn(f"| old-{unit} |", text)
            self.assertEqual(self.git("rev-parse", f"old-{unit}"), self.old_tip)
            self.assertTrue((self.worktrees / unit / ".git").exists())
            self.assertFalse(self.stall_path(OLD, unit).exists())
            stretch = cast(object, json.loads(self.stall_path(NEW, unit).read_text()))
            self.assertEqual(stretch, {"unit": unit, "stretch": 17})
        self.assertFalse((self.notifier / "showrunner-old").exists())
        conf = (self.notifier / "showrunner-new/conf").read_text(encoding="utf-8")
        self.assertIn(str(self.new_doc), conf)
        self.assertNotIn(str(self.doc), conf)
        self.assertIn("TARGET=session:test-showrunner\n", conf)
        self.assertEqual((self.notifier / "showrunner-new/prompt").read_text(),
                         "Historical prompt mentions old.\n")
        for relative, content in self.saved_state().items():
            old_path = self.state / relative
            new_path = self.state / relative.replace("old", "new", 1)
            self.assertFalse(old_path.exists(), str(old_path))
            self.assertEqual(new_path.read_text(), content)
        self.assertEqual((self.state / "child/parent").read_text(), "new\n")
        self.assertEqual((self.state / "other/parent").read_text(), "oldish\n")
        self.assertEqual(self.stall_path("unrelated", "alpha-unit").read_text(), '{"stretch": 99}\n')
        self.assertFalse((self.pool / "old").exists())
        self.assertEqual((self.pool / "new/pool.json").read_text(), '{"generation": 7, "reviewers": {}}\n')
        self.assertEqual((self.pool / "new/alpha-unit/2").read_text(), "retained reviewer lease\n")
        self.assertEqual(self.git("rev-parse", OLD), self.old_tip)
        self.assertEqual(self.git("rev-parse", "new^"), self.old_tip)
        self.assertEqual(self.git("log", "-1", "--format=%s", NEW), "chore(new): rename production from old")
        self.assertEqual(self.git("log", "-1", "--format=%s", OLD), "merge(old-alpha): fixture history")
        committed = self.git("diff-tree", "--no-commit-id", "--name-only", "-r", NEW).splitlines()
        self.assertEqual(set(committed), {"docs/plans/old-production.md", "docs/plans/new-production.md",
                                         *(f"docs/plans/{unit}.md" for unit in UNITS)})
        new_tip = self.git("rev-parse", NEW)
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/new"), f"{new_tip}\trefs/heads/new")
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/old"), f"{self.old_tip}\trefs/heads/old")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.journal()["step"], 7)
        self.assertIs(self.journal()["done"], False)

    def test_plan_lists_every_production_move_without_writing(self) -> None:
        before = self.snapshot()
        result = self.run_rename("plan")
        self.assert_success(result)
        for path in (self.doc, self.new_doc, *(self.plan_path(unit) for unit in UNITS),
                     self.notifier / "showrunner-old", self.notifier / "showrunner-new",
                     *(self.state / directory / slug for directory in ("footers-off", "") for slug in (OLD, NEW)),
                     *(self.state / directory / f"{slug}.json"
                       for directory in ("review-paused", "outstanding") for slug in (OLD, NEW)),
                     self.state / "child/parent",
                     *(self.stall_path(slug, unit) for unit in UNITS for slug in (OLD, NEW)),
                     self.pool / "old", self.pool / "new"):
            self.assertIn(str(path), result.stdout)
        self.assertIn(f"{self.state}/*/parent", result.stdout)
        steps = [int(line.split(".", 1)[0]) for line in result.stdout.splitlines()]
        self.assertEqual(set(steps), set(range(1, 8)))
        self.assertEqual(steps, sorted(steps))
        self.assertIn("merge branch old -> new", result.stdout)
        self.assertIn("push origin new", result.stdout)
        self.assertIn("keep old until finish", result.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_apply_moves_state_and_commits_documents_on_the_new_merge_branch(self) -> None:
        self.assert_success(self.run_rename("apply"))
        self.assert_applied()

    def test_timer_keeps_a_readable_document_through_doc_and_instance_moves(self) -> None:
        result = subprocess.run([sys.executable, "-c", CHECK_TIMER_HANDOFF, str(SCRIPT), "apply", OLD, NEW],
                                cwd=self.checkout, env=self.env, capture_output=True, text=True,
                                check=False, timeout=60)
        self.assert_success(result)

    def test_apply_extends_existing_former_names_in_production_context(self) -> None:
        self.write(self.doc, self.doc.read_text().replace(
            "## Production Context\n", "## Production Context\n\n- **Formerly:** `ancient`, `earlier`\n"))
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "Record earlier production names")
        self.assert_success(self.run_rename("apply"))
        context = self.new_doc.read_text().split("## Production Context", 1)[1].split("\n## ", 1)[0]
        formerly = [line for line in context.splitlines() if line.startswith("- **Formerly:**")]
        self.assertEqual(len(formerly), 1, context)
        for slug in ("ancient", "earlier", OLD):
            self.assertIn(f"`{slug}`", formerly[0])

    def test_a_former_source_name_resumes_before_the_timer_moves(self) -> None:
        self.write(self.doc, self.doc.read_text().replace(
            "## Production Context\n", "## Production Context\n\n- **Formerly:** `ancient`\n"))
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "Record the source production's earlier name")
        base = self.git("rev-parse", OLD)
        failed = subprocess.run(
            [sys.executable, "-c", FAIL_PLAN_REWRITE, str(SCRIPT), "apply", "ancient", NEW],
            cwd=self.checkout, env=self.env, capture_output=True, text=True, check=False, timeout=60)
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertIn("simulated unit header rewrite failure", failed.stderr)
        self.assertEqual(self.journal()["step"], 1)
        self.assertTrue((self.notifier / "showrunner-old/conf").exists())
        self.assertFalse((self.notifier / "showrunner-new").exists())
        self.assert_success(self.run_rename("apply", source="ancient"))
        self.assertEqual(self.journal()["step"], 7)
        self.assertEqual(self.git("rev-parse", "new^"), base)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_a_move_interrupted_before_index_update_commits_only_the_new_doc(self) -> None:
        failed = subprocess.run(
            [sys.executable, "-c", FAIL_GIT_MOVE_INDEX, str(SCRIPT), "apply", OLD, NEW],
            cwd=self.checkout, env=self.env, capture_output=True, text=True, check=False, timeout=60)
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertIn("simulated git mv index failure", failed.stderr)
        self.assertEqual(self.journal()["step"], 0)
        self.assertFalse(self.doc.exists())
        self.assertTrue(self.new_doc.exists())
        self.assertEqual(self.git("ls-files", "--", "docs/plans/old-production.md",
                                  "docs/plans/new-production.md"), "docs/plans/old-production.md")
        self.assert_success(self.run_rename("apply"))
        production_docs = [path for path in self.git("ls-tree", "-r", "--name-only", NEW).splitlines()
                           if path.endswith("-production.md")]
        self.assertEqual(production_docs, ["docs/plans/new-production.md"])
        self.assert_applied()

    def test_retired_plan_prose_is_skipped_but_its_stall_state_moves(self) -> None:
        retired = "| retired-unit | (retired; use `/unit:report` for history) | — | — |\n"
        self.write(self.doc, self.doc.read_text().replace(
            "\n## Production rules", retired + "\n## Production rules"))
        self.write(self.stall_path(OLD, "retired-unit"), '{"stretch": 23}\n')
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "Record a retired unit with a quoted command")
        before = self.snapshot()
        planned = self.run_rename("plan")
        self.assert_success(planned)
        self.assertEqual(self.snapshot(), before)
        self.assertIn(str(self.stall_path(NEW, "retired-unit")), planned.stdout)
        self.assertNotIn("/unit:report", planned.stdout)
        self.assert_success(self.run_rename("apply"))
        self.assertIn(retired, self.new_doc.read_text())
        self.assertFalse(self.stall_path(OLD, "retired-unit").exists())
        self.assertEqual(self.stall_path(NEW, "retired-unit").read_text(), '{"stretch": 23}\n')
        committed = self.git("diff-tree", "--no-commit-id", "--name-only", "-r", NEW).splitlines()
        self.assertEqual(set(committed), {"docs/plans/old-production.md", "docs/plans/new-production.md",
                                         *(f"docs/plans/{unit}.md" for unit in UNITS)})

    def test_check_rewrites_an_escaped_space_path_and_preserves_other_shell_text(self) -> None:
        directory = self.doc.parent / "my checkout"
        directory.mkdir()
        moved_doc = directory / self.doc.name
        _ = self.git("mv", str(self.doc), str(moved_doc))
        self.doc = moved_doc
        self.new_doc = directory / "new-production.md"
        _ = self.git("commit", "-m", "Keep the production doc under a path with spaces")
        prefix = "CHECK=env 'NOTE=keep  spacing'  zsh /scripts/production_check.sh "
        suffix = " --literal 'other  argument'\n"
        escaped = str(self.doc).replace(" ", "\\ ")
        conf = self.notifier / "showrunner-old/conf"
        original = conf.read_text()
        updated = "".join(prefix + escaped + suffix if line.startswith("CHECK=") else line
                          for line in original.splitlines(keepends=True))
        self.write(conf, updated)
        self.assert_success(self.run_rename("apply"))
        expected = "".join(prefix + shlex.quote(str(self.new_doc)) + suffix if line.startswith("CHECK=") else line
                           for line in original.splitlines(keepends=True))
        self.assertEqual((self.notifier / "showrunner-new/conf").read_text(), expected)
        self.assertTrue(self.new_doc.exists())
        self.assertFalse(self.doc.exists())

    def test_formerly_is_inserted_without_splitting_the_context_list(self) -> None:
        self.assert_success(self.run_rename("apply"))
        context = self.new_doc.read_text().split("## Production Context", 1)[1].split("## Units", 1)[0]
        self.assertEqual(context, "\n\n- **Formerly:** `old`\n- **Merge branch:** `new`\n"
                         + f"- **Showrunner checkout:** `{self.checkout}`\n- **Log:** `production.log`\n\n")

    def test_apply_resumes_after_a_step_four_filesystem_failure(self) -> None:
        failed = self.run_rename("apply", fail_state=True)
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertIn("simulated step 4 state move failure", failed.stdout + failed.stderr)
        self.assertTrue(self.new_doc.exists())
        self.assertTrue((self.notifier / "showrunner-new/conf").exists())
        self.assertFalse((self.notifier / "showrunner-old").exists())
        self.assertTrue((self.pool / "old/pool.json").exists())
        for unit in UNITS:
            self.assertTrue(self.stall_path(OLD, unit).exists())
            self.assertRegex(self.plan_path(unit).read_text(),
                             r"(?m)^> \*\*Production:(?: new\*\*|\*\* new) — unit")
        self.assertEqual(self.journal()["step"], 3)
        self.assertIs(self.journal()["done"], False)
        self.assert_success(self.run_rename("apply"))
        self.assert_applied()
        before = self.snapshot()
        self.assert_success(self.run_rename("apply"))
        self.assertEqual(self.snapshot(), before)

    def test_invalid_and_reserved_destinations_refuse_without_writes(self) -> None:
        for destination in ("../escape", "bad.slug", "footers-off", "review-paused", "outstanding"):
            for action in ("plan", "apply"):
                with self.subTest(destination=destination, action=action):
                    before = self.snapshot()
                    result = self.run_rename(action, destination)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(destination, result.stdout + result.stderr)
                    self.assertEqual(self.snapshot(), before)

    def test_current_and_former_timer_slugs_are_unavailable_as_destinations(self) -> None:
        taken = self.checkout / "docs/plans/taken-production.md"
        self.write(taken, "# Taken\n\n## Production Context\n\n- **Formerly:** `retired`, `older`\n")
        self.write(self.notifier / "showrunner-taken/conf",
                   "TARGET=session:taken\nCHECK="
                   + shlex.join(["zsh", "/scripts/production_check.sh", str(taken)]) + "\n")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "Register another production")
        for destination in ("taken", "retired", "older"):
            for action in ("plan", "apply"):
                with self.subTest(destination=destination, action=action):
                    before = self.snapshot()
                    result = self.run_rename(action, destination)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(destination, result.stdout + result.stderr)
                    self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    _ = unittest.main()
