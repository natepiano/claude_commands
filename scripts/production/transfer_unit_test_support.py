"""Move one unit between two disposable productions of one repository, against a local bare origin and isolated state."""

from __future__ import annotations

import hashlib
import json
import os
import select
import shlex
import shutil
from collections.abc import Callable
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import ClassVar, cast, override
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_showrunner
from scripts.production import fake_unit_host

from scripts.herdr import fake_herdr
from scripts.direct import review_pool
from scripts.production import staff, showrunners as pool_showrunners
from scripts.production.cli_fixture import invoke_cli
from scripts.production.rename_test_support import bind_staff_fixture
from scripts.production.staff_test_support import StaffTestCase
from scripts.lib.atomic_file import write_text_atomic
import transfer_unit
import retire_unit
import pane_record
import progress_history
import tidy_wrapped
import stall_watch
from rename_state import RenameRefused
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

# load 136 on natedev, 2026-10-29

SCRIPT = Path(__file__).with_name("transfer_unit.py")


class UnitTransferFixture(StaffTestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.rust: Path = Path()
        self.giving: Path = Path()
        self.receiving: Path = Path()
        self.unit: Path = Path()
        self.env: dict[str, str] = {}
        self.commands: list[list[str]] = []
        self.showrunner_addresses: dict[str, str] = {}

    fixture_root: ClassVar[Path]
    snapshot: ClassVar[Path]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory(prefix="transfer-unit-test-")
        cls.addClassCleanup(directory.cleanup)
        cls.fixture_root = Path(directory.name) / "case"
        cls.snapshot = Path(directory.name) / "snapshot"
        builder = cls()
        builder.bind_fixture(cls.fixture_root)
        builder.build_fixture()
        _ = shutil.copytree(cls.fixture_root, cls.snapshot)
        shutil.rmtree(cls.fixture_root)

    @override
    def setUp(self) -> None:
        super().setUp()
        # Preserve absolute Git worktree and document paths, with private writable copies.
        _ = shutil.copytree(self.snapshot, self.fixture_root)
        self.addCleanup(shutil.rmtree, self.fixture_root)
        self.bind_fixture(self.fixture_root)
        bind_staff_fixture(self, self.env, {"demo": ("alpha-unit", "beta-unit"), "other": ("gamma-unit",)})
        self.write(self.root / "state/notifier/showrunner-other/conf",
                   "TARGET=session:other-showrunner\nCHECK=" + shlex.join(["zsh", "/scripts/production_check.sh",
                   str(self.receiving / "docs/plans/other-production.md")]) + "\n")
        self.showrunner_addresses = {slug: self.running_showrunner(slug, session_id)
                                     for slug, session_id in (("demo", "test-showrunner"),
                                                               ("other", "other-showrunner"))}
        for module, name, value in (
            (pool_showrunners, "NOTIFIER_STATE_DIR", self.root / "state/notifier"),
            (pool_showrunners, "SHOWRUNNER_STATE_DIR", self.root / "state/showrunner"),
            (pool_showrunners, "CONFIG", self.root / "home/.claude/showrunners.json"),
            (pool_showrunners, "SESSIONS_DIR", self.root / "state/sessions"),
            (progress_history, "SESSION_ROOTS", (self.root / "sessions",)),
            (pane_record, "RECORD_DIR", self.root / "home/.local/state/herdr-panes"),
            (pane_record, "PROC", self.root / "proc"),
            (tidy_wrapped, "STATE_DIR", self.root / "state/showrunner"),
            (tidy_wrapped, "NOTIFIER_STATE_DIR", self.root / "state/notifier"),
            (stall_watch, "STATE_DIR", self.root / "state/stall-watch"),
            (stall_watch, "SESSIONS_DIR", self.root / "state/sessions"),
            (stall_watch, "PROJECTS_DIR", self.root / "home/.claude/projects"),
        ):
            _ = self.enterContext(mock.patch.object(module, name, value))

    def bind_fixture(self, root: Path) -> None:
        self.root = root
        home = self.root / "home"
        self.rust = home / "rust"
        self.giving = self.rust / "claude-demo-trunk"
        self.receiving = self.rust / "claude-other-trunk"
        self.unit = self.rust / "claude-demo-alpha"
        config = home / ".claude"
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
            "SHOWRUNNERS_SEND": str(self.root / "fake_send.py"),
            "TRANSFER_MESSAGE_LOG": str(self.root / "messages.jsonl"),
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

    def build_fixture(self) -> None:
        for path in (self.giving, self.root / "home/.claude", self.root / "state/sessions"):
            path.mkdir(parents=True)
        self.write(self.root / "herdr-responses.json",
                   json.dumps({"default": {"stdout": '{"sessions": []}', "code": 0}}))
        self.write(self.root / "fake_send.py",
                   "import json, os, sys\nfrom pathlib import Path\n"
                   + f"sys.path.insert(0, {str(SCRIPT.parents[2])!r})\n"
                   + "from scripts.lib.atomic_file import write_text_atomic\n"
                   + "path = Path(os.environ['TRANSFER_MESSAGE_LOG'])\n"
                   + "previous = path.read_text() if path.exists() else ''\n"
                   + "write_text_atomic(path, previous + json.dumps(sys.argv[1:]) + '\\n')\n"
                   + "if sys.argv[sys.argv.index('--to') + 1] == os.environ.get('TRANSFER_SEND_FAIL_TO'):\n"
                   + "    print('fixture recipient unavailable', file=sys.stderr)\n"
                   + "    raise SystemExit(3)\n"
                   + "print('SENT: fixture')\n")
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
        write_text_atomic(path, content)

    def add_destination(self, slug: str) -> Path:
        checkout = self.rust / f"claude-{slug}-trunk"
        _ = self.git("branch", slug)
        _ = self.git("push", "origin", slug)
        _ = self.git("worktree", "add", str(checkout), slug)
        doc = checkout / f"docs/plans/{slug}-production.md"
        self.write(doc, self.production_doc(slug, checkout, "| Unit | Plan | Worktree | Branch |",
                                          "| --- | --- | --- | --- |"))
        _ = self.git("add", str(doc), cwd=checkout)
        _ = self.git("commit", "-m", f"fixture production {slug}", cwd=checkout)
        _ = self.git("push", "origin", slug, cwd=checkout)
        self.write(self.root / f"state/notifier/showrunner-{slug}/conf",
                   f"TARGET=session:{slug}-showrunner\nCHECK=" + shlex.join([
                       "zsh", "/scripts/production_check.sh", str(doc)]) + "\n")
        _ = staff.ensure("showrunner", name=slug)
        return doc

    def add_transfer_unit(self, stem: str) -> None:
        plan = self.giving / f"docs/plans/{stem}.md"
        self.write(plan, f"# {stem}\n\n> **Production: demo** — unit `{stem}-unit`; "
                   + "production doc `docs/plans/demo-production.md`\n")
        _ = self.git("add", str(plan))
        _ = self.git("commit", "-m", f"fixture plan {stem}")
        _ = self.git("push", "origin", "demo")
        _ = self.git("worktree", "add", "-b", f"demo-{stem}", str(self.rust / f"claude-demo-{stem}"), "demo")
        _ = self.git("push", "origin", f"demo-{stem}")

    def git(self, *arguments: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", *arguments], cwd=cwd or self.giving, env=self.env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, (arguments, result.stdout, result.stderr))
        return result.stdout.strip()

    def stall_path(self, slug: str, unit: str) -> Path:
        digest = hashlib.sha256(f"{slug}\0{unit}".encode()).hexdigest()
        return self.root / "state/stall-watch" / f"{digest}.json"

    def assert_ok(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_refused(self, result: subprocess.CompletedProcess[str], text: str) -> None:
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(text, result.stderr)

    def seed_record(self) -> Path:
        """A prior shutdown record and a command log that exposes any forbidden stop or restart."""
        home = self.root / "home"
        saved = fake_unit_host.stopped_record(home / ".claude", self.unit,
            doc=self.giving / "docs/plans/demo-production.md", host_session="demo-alpha",
            branch="demo-alpha", plan=self.unit / "docs/plans/alpha.md")
        script = ("import json, sys; sys.path.insert(0, sys.argv[1]); import shutdown_record; "
                  + "assert shutdown_record.create_session(json.loads(sys.stdin.read()))")
        created = subprocess.run([sys.executable, "-c", script, str(SCRIPT.parents[1] / "shutdown")],
                                 input=json.dumps(saved), env=self.env, capture_output=True, text=True,
                                 check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(created.returncode, 0, created.stderr)
        log = self.root / "shutdown-calls.jsonl"
        stub = self.root / "fake_shutdown.py"
        self.write(stub, "import json, os, sys\nwith open(os.environ['RENAME_UNIT_SHUTDOWN_LOG'], 'a') as log:\n"
                   + "    _ = log.write(json.dumps(sys.argv[1:]) + '\\n')\n")
        self.env["RENAME_UNIT_SHUTDOWN"] = str(stub)
        self.env["RENAME_UNIT_SHUTDOWN_LOG"] = str(log)
        return log

    def moved_unit_with_leftover_marks(self) -> None:
        _ = self.seed_record()
        self.assert_ok(self.transfer("apply"))
        fake_unit_host.write(self.env, [
            fake_unit_host.HostedUnit("old", "w3:p3Y", {"SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit"}),
            fake_unit_host.HostedUnit("moved", "w4:p1", {"SHOWRUNNER_UNIT": "other", "SHOWRUNNER_UNIT_ID": "alpha-unit"})])
        held = fake_showrunner.write_session(self.root / "state/sessions", "alpha", "sid-alpha")
        self.addCleanup(held.close)
        fake_unit_host.place_process(self.env, os.getpid(), "w4:p1")
        fake_unit_host.process(self.env, 991030, 991000, ["python3", "-m", "unittest"],
                               {"HERDR_PANE_ID": "w3:p3Y", "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit"})

    def move_giving_checkout(self) -> None:
        old = self.giving
        self.giving = old.with_name("claude-demo-relocated")
        _ = old.rename(self.giving)
        _ = self.git("worktree", "repair")
        doc = self.giving / "docs/plans/demo-production.md"
        self.write(doc, doc.read_text().replace(str(old), str(self.giving)))
        _ = self.git("add", str(doc))
        _ = self.git("commit", "-m", "relocate giving checkout")
        conf = self.root / "state/notifier/showrunner-demo/conf"
        self.write(conf, conf.read_text().replace(str(old), str(self.giving)))
        self.assertFalse(old.exists())

    def assert_transfer_resumed(self, path: Path) -> None:
        self.assert_ok(self.transfer("apply"))
        saved = cast(dict[str, object], json.loads(path.read_text()))
        self.assertIs(saved["done"], True)
        self.assertNotIn("alpha-unit", (self.giving / "docs/plans/demo-production.md").read_text())
        self.assertIn("alpha-unit", (self.receiving / "docs/plans/other-production.md").read_text())
        self.assert_ok(self.transfer("verify"))

    def live_pid(self) -> int:
        """Keep a registry holder alive until cleanup closes its input pipe."""
        child = subprocess.Popen([sys.executable, "-c",
            "import sys; print('ready', flush=True); sys.stdin.read()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def close() -> None:
            _ = child.communicate(timeout=TEST_HANG_GUARD_SECS)

        self.addCleanup(close)
        assert child.stdout is not None
        ready, _, _ = select.select([child.stdout], [], [], TEST_HANG_GUARD_SECS)
        self.assertTrue(ready, "registry holder did not start")
        self.assertEqual(child.stdout.readline(), b"ready\n")
        return child.pid

    def transfer_process(self, verb: str, unit: str = "alpha-unit", to: Path | None = None) -> subprocess.CompletedProcess[str]:
        to_doc = to or self.receiving / "docs/plans/other-production.md"
        return subprocess.run([sys.executable, str(SCRIPT), verb, "demo", unit, str(to_doc),
                               "--scratch", str(self.root / "scratch")], cwd=self.giving, env=self.env,
                              capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

    def transfer(self, verb: str, unit: str = "alpha-unit", to: Path | None = None) -> subprocess.CompletedProcess[str]:
        if self._testMethodName == "test_plan_lists_the_steps_and_changes_nothing":
            return self.transfer_process(verb, unit, to)
        arguments = [verb, "demo", unit, str(to or self.receiving / "docs/plans/other-production.md"),
                     "--scratch", str(self.root / "scratch")]
        return self.invoke(arguments)

    def invoke(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        original_run = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)

        def run(command: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.commands.append(command.copy())
            environment = cast(dict[str, str], kwargs.get("env") or dict(os.environ))
            cwd = Path(cast(str, kwargs.get("cwd") or os.getcwd()))
            if command[0] == self.env["HERDR_BIN"]:
                return invoke_cli(fake_herdr, lambda: fake_herdr.main(command[1:]), command[1:], environment, cwd)
            if command[:2] == [sys.executable, str(transfer_unit.POOL)]:
                return invoke_cli(review_pool, review_pool.main, command[2:], environment, cwd, production_state=True)
            return original_run(command, *args, **kwargs)

        with (mock.patch.object(retire_unit, "MAC_CLAIMS", self.root / "home/.local/state/mac-test/claims"),
              mock.patch.object(subprocess, "run", run)):
            return invoke_cli(transfer_unit, lambda: transfer_unit.main(arguments), arguments,
                              self.env, self.giving, production_state=True)

    def pause_before_source_commit(self) -> Path:
        def pause(transfer: transfer_unit.UnitTransfer, save: Callable[[], None]) -> None:
            del transfer, save
            raise RenameRefused("fixture pause before source commit")

        steps = tuple(step._replace(run=pause) if step.run.__name__ == "_commit_from" else step
                      for step in transfer_unit.STEPS)
        with mock.patch.object(transfer_unit, "STEPS", steps):
            result = self.invoke(["apply", "demo", "alpha-unit", str(self.receiving / "docs/plans/other-production.md"),
                                  "--scratch", str(self.root / "scratch")])
        self.assert_refused(result, "fixture pause before source commit")
        path = self.root / "state/showrunner/demo/transfer_unit.json"
        saved = cast(dict[str, object], json.loads(path.read_text()))
        self.assertEqual((saved["step"], saved["done"]),
                         (next(index for index, step in enumerate(transfer_unit.STEPS)
                               if step.run.__name__ == "_commit_from"), False))
        for name in ("from_doc", "to_doc", "from_checkout", "to_checkout"):
            self.assertNotIn(name, saved)
        return path

    def messages(self) -> list[list[str]]:
        path = self.root / "messages.jsonl"
        return [cast(list[str], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def running_showrunner(self, slug: str, session_id: str) -> str:
        """Publish a real fixture socket for the shared sender's slug lookup."""
        held = fake_showrunner.write_session(self.root / "state/sessions", slug, session_id, self.live_pid())
        self.addCleanup(held.close)
        return f"uds:{held.getsockname()}"

    def remove_showrunner_session(self, session_id: str) -> None:
        """Make a timer's recipient absent without changing the production's source."""
        for path in (self.root / "state/sessions").glob("*.json"):
            record = cast(dict[str, object], json.loads(path.read_text()))
            if record["sessionId"] == session_id:
                path.unlink()

    def running_unit(self) -> int:
        """Publish a running director through the existing host and session fixtures."""
        fake_unit_host.write(self.env, [fake_unit_host.HostedUnit("agents", "w1:p1", {
            "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit",
        }, label="alpha")])
        pid = self.live_pid()
        socket = fake_showrunner.write_session(self.root / "state/sessions", "alpha", "sid-alpha", pid)
        self.addCleanup(socket.close)
        fake_unit_host.place_process(self.env, pid, "w1:p1")
        self.configure_tab_move()
        return pid

    def configure_tab_move(self) -> None:
        """Publish source placement and the shared pane-move response."""
        path = Path(self.env["FAKE_HERDR_RESPONSES"])
        responses = cast(dict[str, object], json.loads(path.read_text()))
        replies = {
            "workspace list": {"workspaces": [{"workspace_id": "w1", "label": "demo"},
                                                {"workspace_id": "w2", "label": "other"}]},
            "pane list": {"panes": [{"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1"}]},
            "pane move w1:p1 --new-tab --workspace w2": {
                "move_result": {"created_tab": {"tab_id": "w2:t1"}, "pane": {"pane_id": "w1:p1"}}},
            "tab rename w2:t1 alpha": {},
            "tab list --workspace w2": {"tabs": [{"tab_id": "w2:t1", "label": "alpha", "workspace_id": "w2"}]},
            "tab list": {"tabs": [{"tab_id": "w2:t1", "label": "alpha", "workspace_id": "w2"}]},
        }
        for command, result in replies.items():
            responses[f"--session agents {command}"] = {"stdout": json.dumps({"result": result}), "code": 0}
        after = {**responses, "--session agents pane list": {"stdout": json.dumps({"result": {
            "panes": [{"pane_id": "w1:p1", "workspace_id": "w2", "tab_id": "w2:t1"}],
        }}), "code": 0}}
        move = cast(dict[str, object], responses["--session agents pane move w1:p1 --new-tab --workspace w2"])
        move["write_files"] = {str(path): json.dumps(after)}
        self.write(path, json.dumps(responses))
