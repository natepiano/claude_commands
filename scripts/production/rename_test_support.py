"""Private Git fixtures and in-process production rename commands."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar, cast, override
from unittest import mock
from collections.abc import Sequence
from contextlib import ExitStack
from types import SimpleNamespace

from scripts.astra import design_roles
from scripts.direct import workers
from scripts.lib.socket_test_support import short_socket_directory
from scripts.production import fake_unit_host
import fake_herdr
import rename
import rename_unit
import showrunners
import stall_watch
import tidy_wrapped
import unit_move
from cli_fixture import invoke_cli

SCRIPT = Path(__file__).with_name("rename.py")

OLD = "old"

NEW = "new"

UNITS = ("alpha-unit", "beta-unit")

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

INTERRUPT_UNIT_GIT = '''import os, runpy, subprocess, sys

original_run = subprocess.run
def refusing_run(command, *args, **kwargs):
    operation = os.environ["RENAME_TEST_REFUSE"]
    arguments = command[3:] if command[:1] == ["git"] else []
    blocked = ((operation == "repair" and arguments[:2] == ["worktree", "repair"])
               or (operation == "finish-delete" and arguments[:3] == ["update-ref", "-d", "refs/heads/old"])
               or (operation == "unit-push" and "push" in arguments
                   and "new-alpha-unit:refs/heads/new-alpha-unit" in arguments)
               or (operation == "unit-doc" and arguments == ["diff", "--name-only", "-z", "HEAD"])
               or (operation == "merge-push" and arguments == ["push", "origin", "new:refs/heads/new"]))
    if blocked:
        return subprocess.CompletedProcess(command, 1, "", "simulated " + operation + " failure")
    return original_run(command, *args, **kwargs)
subprocess.run = refusing_run
sys.argv = sys.argv[1:]
sys.path.insert(0, str(__import__("pathlib").Path(sys.argv[0]).resolve().parent))
runpy.run_path(sys.argv[0], run_name="__main__")
'''

OVERWRITE_TRUST = '''import json, os, runpy, sys
from pathlib import Path
from unit_move import time

attempts = []
original_sleep = time.sleep
def rewrite_from_memory(seconds):
    if seconds != 2:
        return original_sleep(seconds)
    attempts.append(seconds)
    Path(os.environ["RENAME_TEST_WAITS"]).write_text(json.dumps(attempts))
    if len(attempts) <= int(os.environ["RENAME_TEST_OVERWRITES"]):
        path = Path(os.environ["RENAME_TEST_CONFIG"])
        fields = json.loads(path.read_text())
        fields["projects"].pop(os.environ["RENAME_TEST_NEW_PATH"], None)
        path.write_text(json.dumps(fields))
time.sleep = rewrite_from_memory
sys.argv = sys.argv[1:]
sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
runpy.run_path(sys.argv[0], run_name="__main__")
'''

# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300

def run_rename(arguments: list[str], environment: dict[str, str], cwd: Path,
               wrapper: str = "") -> subprocess.CompletedProcess[str]:
    """Keep real Git and filesystem work; restore every binding the wrappers replace."""
    real_run = subprocess.run

    def run(argv: Sequence[str], *, capture_output: bool = False, text: bool = False, check: bool = False,
            timeout: float | None = None, env: dict[str, str] | None = None,
            cwd: str | Path | None = None) -> subprocess.CompletedProcess[str]:
        if argv[0] == str(Path(fake_herdr.__file__)):
            return invoke_cli(fake_herdr, lambda: fake_herdr.main(list(argv[1:])), list(argv[1:]),
                              dict(os.environ) if env is None else env,
                              Path.cwd() if cwd is None else Path(cwd))
        return real_run(argv, capture_output=capture_output, text=text, check=check,
                        timeout=timeout, env=env, cwd=cwd)

    def trust_readback(seconds: float) -> None:
        # The fixtures have no concurrent Claude writer. The overwrite wrapper
        # injects that writer at each verification attempt and counts all retries.
        if seconds != 2:
            raise AssertionError(f"unexpected rename wait: {seconds}")

    with ExitStack() as scope:
        for module, fields in (
            (showrunners, {"CONFIG": Path(environment["SHOWRUNNERS_CONFIG"]),
                           "NOTIFIER_STATE_DIR": Path(environment["NOTIFIER_STATE_DIR"]),
                           "SESSIONS_DIR": Path(environment["NOTIFIER_SESSIONS_DIR"]),
                           "SHOWRUNNER_STATE_DIR": Path(environment["SHOWRUNNER_STATE_DIR"])}),
            (tidy_wrapped, {"STATE_DIR": Path(environment["SHOWRUNNER_STATE_DIR"]),
                            "NOTIFIER_STATE_DIR": Path(environment["NOTIFIER_STATE_DIR"])}),
            (stall_watch, {"STATE_DIR": Path(environment["STALL_WATCH_STATE_DIR"])}),
        ):
            for name, value in fields.items():
                _ = scope.enter_context(mock.patch.object(module, name, value))
        _ = scope.enter_context(mock.patch.object(subprocess, "run", side_effect=run))
        _ = scope.enter_context(mock.patch.object(unit_move, "time", SimpleNamespace(sleep=trust_readback)))
        # Failure injection scripts assign these names just as they do in a CLI
        # subprocess. Each call restores them before another test can observe it.
        for module, names in ((os, ("rename", "replace")),
                              (workers, ("move_unit", "rename_unit", "end_unit", "repoint_unit_worktree")),
                              (rename_unit, ("repoint_shutdown_worktree",))):
            for name in names:
                _ = scope.enter_context(mock.patch.object(module, name, cast(object, getattr(module, name))))

        def main() -> int:
            if wrapper:
                with mock.patch.object(sys, "argv", ["-c", str(rename.__file__), *arguments]):
                    exec(compile(wrapper, "<rename failure injection>", "exec"), {"__name__": "__main__"})
                return 0
            return rename.main(arguments)

        return invoke_cli(rename, main, arguments, environment, cwd)


class ProductionRenameFixture(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.home: Path = Path()
        self.checkout: Path = Path()
        self.origin: Path = Path()
        self.doc: Path = Path()
        self.new_doc: Path = Path()
        self.notifier: Path = Path()
        self.state: Path = Path()
        self.stall: Path = Path()
        self.pool: Path = Path()
        self.worktrees: Path = Path()
        self.env: dict[str, str] = {}
        self.old_tip: str = ""

    fixture_root: ClassVar[Path]

    fixture_snapshot: ClassVar[Path]

    fixture_tip: ClassVar[str]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory(prefix="rename-test-")
        cls.addClassCleanup(directory.cleanup)
        cls.fixture_root = Path(directory.name).resolve() / "case"
        cls.fixture_snapshot = cls.fixture_root.with_name("snapshot")
        builder = cls()
        builder.bind_fixture(cls.fixture_root)
        builder.build_fixture()
        cls.fixture_tip = builder.old_tip
        _ = shutil.copytree(cls.fixture_root, cls.fixture_snapshot, symlinks=True)
        shutil.rmtree(cls.fixture_root)

    @override
    def setUp(self) -> None:
        # Linked worktree metadata and the committed documents name absolute
        # paths. Restore private files, refs and origin at the seed's same path.
        _ = shutil.copytree(self.fixture_snapshot, self.fixture_root, symlinks=True)
        self.addCleanup(shutil.rmtree, self.fixture_root)
        self.bind_fixture(self.fixture_root)
        self.old_tip = self.fixture_tip

    def bind_fixture(self, root: Path) -> None:
        self.root = root
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
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update({
            "UNIT_WORKERS_STATE": str(self.root / "state/unit-workers"),
            "RETIRE_UNIT_RUN_ROOT": str(self.root / "runs"),
            "UNIT_RUN_ROOT": str(self.root / "runs"),
            "HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "SHUTDOWN_STATE_DIR": str(self.home / ".local/state/shutdown"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "NOTIFIER_STATE_DIR": str(self.notifier),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "state/sessions"),
            "SHOWRUNNER_STATE_DIR": str(self.state),
            "STALL_WATCH_STATE_DIR": str(self.stall), "REVIEW_POOL_STATE": str(self.pool),
            "SHOWRUNNERS_CONFIG": str(self.home / ".claude/showrunners.json"),
            "UNIT_ACTIVE_DIR": str(self.root / "active"),
            "UNIT_HISTORY_DIR": str(self.root / "history"),
            "PHASE_TABLE_VAULT": str(self.root / "vault/showrunners"),
            "HERDR_BIN": str(SCRIPT.parents[1] / "herdr/fake_herdr.py"),
            "HERDR_PROC": str(self.root / "proc"),
            "HERDR_PLATFORM": "linux",
            "FAKE_HERDR_LOG": str(self.root / "herdr.jsonl"),
            "FAKE_HERDR_RESPONSES": str(self.root / "herdr-responses.json"),
            "CLAUDE_CODE_SESSION_ID": "", "USER_ZONE": "UTC",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
        })

    def build_fixture(self) -> None:
        for path in (self.home, self.checkout, self.notifier, self.stall, self.pool, self.worktrees):
            path.mkdir(parents=True)
        config = self.home / ".claude"
        config.mkdir()
        (self.root / "state/sessions").mkdir(parents=True)
        self.write(self.root / "herdr-responses.json",
                   json.dumps({"default": {"stdout": '{"sessions": []}', "code": 0}}))
        _ = self.git("init", "--bare", str(self.origin))
        _ = self.git("init", "-b", OLD)
        _ = self.git("config", "user.name", "Production Rename Test")
        _ = self.git("config", "user.email", "rename@example.invalid")
        # The snapshots compare every file, .git included: git's own background gc or maintenance
        # writing a temporary pack file mid-test is not the rename's doing (seen at load 92).
        for repo in (self.checkout, self.origin):
            for key, value in (("gc.auto", "0"), ("maintenance.auto", "false")):
                _ = self.git("-C", str(repo), "config", key, value)
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
        if self._testMethodName == "test_apply_moves_state_and_commits_documents_on_the_new_merge_branch":
            return subprocess.run([sys.executable, str(SCRIPT), action, source, destination],
                                  cwd=self.checkout, env=self.env, capture_output=True,
                                  text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        return self.run_command(action, source, destination, wrapper=FAIL_STATE_MOVE if fail_state else "")

    def assert_success(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def snapshot(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file() and path != self.root / "herdr.jsonl"}

    def journal(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads((self.state / "new/rename.json").read_text()))

    def completed_steps(self) -> int:
        return len(cast(list[str], self.journal()["completed"]))

    def unit_worktree(self, slug: str, unit: str) -> Path:
        return self.worktrees / f"repo.name_with_punctuation-{slug}-{unit}"

    def prepare_unit_moves(self) -> None:
        text = self.doc.read_text()
        for unit in UNITS:
            old = self.worktrees / unit
            destination = self.unit_worktree(OLD, unit)
            _ = self.git("worktree", "move", str(old), str(destination))
            text = text.replace(f"`{old}`", f"`{destination}`")
            _ = self.git("push", "origin", f"old-{unit}")
        self.write(self.doc, text)
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "Record conventional unit worktree paths")

    def run_command(self, *arguments: str, wrapper: str = "", **environment: str) -> subprocess.CompletedProcess[str]:
        return run_rename(list(arguments), {**self.env, **environment}, self.checkout, wrapper)

    def seed_shutdown_record(self, progress: str = "stopped") -> Path:
        old = self.unit_worktree(OLD, "alpha-unit")
        session: dict[str, object] = {
            "session_id": "sid-alpha", "config_dir": str(self.home / ".claude"), "pid": 4242, "proc_start": "1",
            "name": "alpha", "cwd": f"{old}/sub", "status": "idle", "updated_at": 0, "probe": False,
            "model": {"kind": "no reply yet"},
            "checkout": {"kind": "git", "path": str(old), "head": {"kind": "branch", "name": "old-alpha-unit"},
                         "upstream": {"kind": "tracking", "ahead": 0}, "dirty": []},
            "run_dirs": [], "codex_servers": [], "timers": [], "kind": "unit",
            "host": {"kind": "unit", "production": OLD, "unit": "alpha-unit", "doc": str(self.doc),
                     "session_name": "old-alpha-unit", "session_host": "herdr",
                     "plan": {"kind": "plan", "path": f"{old}/docs/plans/alpha-unit.md"}}}
        worker = {**session, "session_id": "sid-worker", "name": "worker", "cwd": f"{old}/worker",
                  "kind": "worker", "host": {"kind": "terminal"},
                  "owner": {"kind": "director", "session_id": "sid-alpha"}}
        saved: dict[str, object] = {
            "login": "owner@example.com", "label": "claude2", "machine": "natedev",
            "requested_at": "2026-10-09T21:49:10+00:00", "request_id": "request",
            "requested_by": {"kind": "terminal"}, "hold": "session", "then": "stop", "force": "wait for ready",
            "alerts": "none", "probe": False, "session": session, "workers": [worker], "timers": [],
            "settle_message": {"kind": "not sent"}, "where": {"kind": "not said"},
            "progress": {"kind": progress, "at": "2026-10-09T21:50:00+00:00"}, "stop_issues": [], "trail": [],
            "codex_seats": [{"run_dir": str(self.root / "runs/r1"), "seat": "impl", "role": "impl",
                             "thread_id": "thread-impl", "turn_live": True, "launcher_command": [
                                 "bash", "/scripts/direct/implement.sh", str(old), f"{old}/prompt.md"]}]}
        self.write(self.root / "runs/r1/impl.resume-prompt.md", "Continue the task.\n")
        script = ("import json, sys; sys.path.insert(0, sys.argv[1]); import shutdown_record; "
                  "assert shutdown_record.create_session(json.loads(sys.stdin.read()))")
        created = subprocess.run([sys.executable, "-c", script, str(SCRIPT.parents[1] / "shutdown")],
                                 input=json.dumps(saved), env=self.env, capture_output=True, text=True,
                                 check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(created.returncode, 0, created.stderr)
        return self.home / ".local/state/shutdown/owner@example.com/sessions/sid-alpha.json"

    def marked_unit(self, unit: str) -> None:
        fake_unit_host.write(self.env, [fake_unit_host.HostedUnit(
            "unit-before-rename", "w4:p1", {"SHOWRUNNER_UNIT": OLD, "SHOWRUNNER_UNIT_ID": unit})])

    def live_unit(self) -> None:
        fake_unit_host.place_process(self.env, os.getpid(), "w4:p1")
        path = Path(self.enterContext(short_socket_directory())) / "live.sock"
        held = socket.socket(socket.AF_UNIX)
        held.bind(str(path))
        self.addCleanup(held.close)
        self.write(self.root / "state/sessions/live.json", json.dumps({
            "pid": os.getpid(), "sessionId": "live-unit", "name": "unit-before-rename",
            "messagingSocketPath": str(path), "updatedAt": 1,
            "tmux": ""}))

    def seed_unit_config(self, directory: Path, unit: str) -> Path:
        old = self.unit_worktree(OLD, unit)
        self.write(directory / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(old)) / "session.jsonl",
                   "historical transcript for old\n")
        config = self.home / ".claude.json" if directory == self.home / ".claude" else directory / ".claude.json"
        self.write(config, json.dumps({"theme": "dark", "projects": {
            str(old): {"hasTrustDialogAccepted": True, "allowedTools": ["Read"]},
            str(self.root / "unrelated"): {"hasTrustDialogAccepted": False}}}))
        return config

    def assert_unit_moved(self, unit: str) -> None:
        old, new = self.unit_worktree(OLD, unit), self.unit_worktree(NEW, unit)
        self.assertFalse(old.exists())
        self.assertTrue((new / ".git").is_file())
        self.assertEqual(self.git("-C", str(new), "branch", "--show-current"), f"new-{unit}")
        self.assertEqual(self.git("rev-parse", f"new-{unit}"), self.old_tip)
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)", f"refs/heads/old-{unit}"), "")
        self.assertEqual(self.git("ls-remote", "origin", f"refs/heads/old-{unit}"), "")
        self.assertEqual(self.git("ls-remote", "origin", f"refs/heads/new-{unit}"),
                         f"{self.old_tip}\trefs/heads/new-{unit}")
        self.assertIn(f"worktree {new}\n", self.git("worktree", "list", "--porcelain") + "\n")
        row = next(line for line in self.new_doc.read_text().splitlines() if line.startswith(f"| {unit} |"))
        self.assertIn(f"`{new}`", row)
        self.assertIn(f"`new-{unit}`", row)
        self.assertNotIn(f"old-{unit}", row)
        self.assertEqual(self.git("status", "--porcelain"), "")
        moves = cast(dict[str, dict[str, object]], self.journal()["unit_moves"])
        self.assertEqual(moves[unit]["step"], 5)

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
        self.assertTrue((self.pool / "old").exists())
        self.assertFalse((self.pool / "new").exists())
        self.assertEqual((self.pool / "old/pool.json").read_text(), '{"generation": 7, "reviewers": {}}\n')
        self.assertEqual((self.pool / "old/alpha-unit/2").read_text(), "retained reviewer lease\n")
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
        self.assertEqual(self.completed_steps(), 8)
        self.assertIs(self.journal()["done"], False)

    def held_design(self, repository_id: str, checkout: Path, holder: design_roles.Holder) -> design_roles.HeldDesign:
        """A design `holder` holds, with an open designer whose roster entry runs in `checkout`."""
        with mock.patch.dict(os.environ, self.env):
            design_roles.register_repository(repository_id, {"fixture-host": str(checkout)}, inventory_complete=True)
            placement = design_roles.authority_request("reserve", {"repository_id": repository_id, "document": "design.md",
                                                            "host": "fixture-host", "request_id": repository_id})
            design = design_roles.HeldDesign(
                design_roles.DesignPlacement(str(placement["design_id"]), "fixture-host", 1), repository_id, str(checkout),
                str(checkout / "design.md"), "design", "2026-10-22T12:00:00+00:00", holder, design_roles.DesignUnbound(),
                (design_roles.RoleInstance("designer", f"old-{repository_id}-astra-designer", design_roles.RoleOpen("designer-thread")),
                 design_roles.RoleInstance("reviewer", f"old-{repository_id}-astra-reviewer", design_roles.RoleNotOpened())))
            design_roles._save(design)  # pyright: ignore[reportPrivateUsage]
            self.write(design_roles.mesh_directory(design.placement.design_id) / "mesh_roster.json", json.dumps(
                {design.roles[0].name: {"thread_id": "designer-thread", "status": "done", "cwd": str(checkout)}}))
        return design

    def read_held(self, design: design_roles.HeldDesign) -> tuple[design_roles.HeldDesign, str]:
        """The design as saved now, and its designer's launch directory."""
        with mock.patch.dict(os.environ, self.env):
            current = design_roles.read_design(design.placement.design_id)
            roster = cast(dict[str, dict[str, str]], json.loads(
                (design_roles.mesh_directory(design.placement.design_id) / "mesh_roster.json").read_text()))
        return current, roster[design.roles[0].name]["cwd"]

    def seed_paused_unit_worker(self) -> Path:
        old = self.unit_worktree(OLD, "alpha-unit")
        run = self.root / "runs/before-move"
        self.write(run / workers.STATE, json.dumps({"status": "active", "working_dir": str(old),
            "plan_doc": str(old / "docs/plans/alpha-unit.md"), "run_id": run.name}))
        self.write(run / "working_dir", str(old))
        with mock.patch.dict(os.environ, self.env):
            store = workers.ledger_for_run(run)
            worker = workers.UnitWorker("w7", "impl", "codex", "kept-model", "xhigh", {},
                workers.CodexThread("kept-thread", str(self.root / "codex-home")), workers.Paused(),
                [workers.WorkerEvent("opened", "before move", str(run), 1.0)])
            with store.locked() as ledger:
                ledger.workers["w7"] = worker
        self.write(run / workers.ROSTER, json.dumps({"w7": {
            "thread_id": "kept-thread", "codex_home": str(self.root / "codex-home"),
            "role": "impl", "family": "codex", "model": "kept-model", "effort": "xhigh",
            "status": "paused", "cwd": str(old / "subdir"), "unit_ledger": str(store.path)}}))
        return run
