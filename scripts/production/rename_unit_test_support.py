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
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, cast, override
from unittest import mock
from scripts.herdr import fake_herdr
from scripts.direct.wait_for_test_state import wait_until
from scripts.production.cli_fixture import invoke_cli
from scripts.production.rename_test_support import bind_staff_fixture
from scripts.production.staff_test_support import StaffTestCase
from scripts.shutdown import stop_resume
from scripts.production import staff
from scripts.lib.atomic_file import write_text_atomic

sys.path.insert(0, str(Path(__file__).resolve().parent))

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
        bind_staff_fixture(self, self.env, {SLUG: ("alpha-unit", "mul_add-unit")})
        # These modules also bind paths at import time; environment changes alone
        # do not isolate their journals, locks, or stall-watch state.
        for module, name, value in (
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
    def shutdown_boundary(self) -> Iterator[None]:
        """Use the same recording stop fake for in-process and child-process apply."""
        with (mock.patch.object(rename_unit, "SHUTDOWN", Path(self.env.get("RENAME_UNIT_SHUTDOWN")
                                                               or str(SCRIPT.parents[1] / "shutdown/shutdown.py"))),
              mock.patch.object(stop_resume, "stop", side_effect=self.stop_session)):
            yield

    def stop_session(self, identity: str) -> stop_resume.Stopped:
        rename_unit.run_shutdown("stop", identity)
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
            self.assertTrue(chunk, f"stdout closed before {expected!r}: {output!r}")
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
        return worktree, plan, database

    def seed_record(self, progress: Literal["waiting", "ready", "stopped"] = "stopped", name: str = "alpha", host_session: str = "demo-alpha", *,
                    unit: str = "alpha-unit", slug: str = SLUG) -> Path:
        """A stopped unit's shutdown record, from before the conductor was removed."""
        old = self.rust / "claude-demo-alpha"
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
        stub = self.root / "fake_shutdown.py"
        self.write(stub, "import json, os, sys\nwith open(os.environ['RENAME_UNIT_SHUTDOWN_LOG'], 'a') as log:\n"
                   + "    _ = log.write(json.dumps(sys.argv[1:]) + '\\n')\n")
        self.env["RENAME_UNIT_SHUTDOWN"] = str(stub)
        self.env["RENAME_UNIT_SHUTDOWN_LOG"] = str(log)
        return log

    def record(self) -> dict[str, object]:
        directory = self.home / ".local/state/shutdown/owner@example.com/sessions"
        return cast(dict[str, object], json.loads((directory / "sid-alpha.json").read_text()))

    def mark_live(self, unit: str, session_id: str, name: str = "") -> Callable[[], None]:
        """A live Claude in a herdr session marked as `unit` of this production; the result ends it.

        The session and its pane label are called `name`, the unit's stem when it is empty.
        """
        name = name or unit.removesuffix("-unit")
        held = fake_showrunner.write_session(self.root / "state/sessions", name, session_id)
        self.addCleanup(held.close)
        fake_unit_host.write(self.env, [fake_unit_host.HostedUnit(
            f"demo-{unit}", "w1:p1", {"SHOWRUNNER_UNIT": SLUG, "SHOWRUNNER_UNIT_ID": unit}, label=name)])
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
    with fixture.shutdown_boundary():
        return rename_unit.main(["apply", SLUG, "alpha", "gamma", "--scratch", str(root / "scratch")])
