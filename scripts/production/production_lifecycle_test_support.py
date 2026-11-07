"""Exercise production lifecycle steps in disposable git repositories."""

from __future__ import annotations

from scripts.shutdown import remote

import json
import os
import re
import subprocess
import sys
import tempfile
import shutil
import unittest
from pathlib import Path
from typing import ClassVar, cast, override
from unittest import mock

from scripts.production import fake_unit_host
from scripts.production import production_lifecycle
from scripts.production import showrunners, staff, unit_config
from scripts.production import unit_lookup
from scripts.production import tidy_wrapped
from scripts.production.cli_fixture import invoke_cli


SCRIPT = Path(__file__).with_name("production_lifecycle.py")
STUB = '''#!__PYTHON__
import os
import shutil
from pathlib import Path
import sys

state = Path(os.environ["LIFECYCLE_TEST_STATE"])
name = Path(sys.argv[0]).name
with (state / "calls").open("a") as output:
    output.write(name + " " + " ".join(sys.argv[1:])
                 + (" cwd=" + os.getcwd() if name == "cargo-berth" else "") + "\\n")
if name == "ssh":
    print("rc=" + (state / "mac-rc").read_text().strip() if (state / "mac-rc").exists() else "rc=0")
    raise SystemExit(0)
if name == "cargo-berth" and sys.argv[1:2] == ["board"]:
    print((state / "reservations.json").read_text() if (state / "reservations.json").exists() else
          '{"output_contract_version": 1, "verb": "board", "status": "unconfigured", "payload": {"kind": "no_facts"}}')
    raise SystemExit(int((state / "board-exit").read_text()) if (state / "board-exit").exists() else 4)
if name == "notifier.sh" and sys.argv[1:2] == ["remove"]:
    timer = state / "notifier" / sys.argv[2]
    if timer.exists():
        shutil.rmtree(timer)
    raise SystemExit(0)
if name in ("notifier.sh", "showrunners.py", "cargo-berth", "py"):
    raise SystemExit(0)
raise SystemExit(99)
'''
# The session records and the message sender showrunners.py reads: nothing real is looked up or sent.
SESSIONS = '''import json, os, pathlib, sys
command, target = sys.argv[1:]
for path in pathlib.Path(os.environ["NOTIFIER_SESSIONS_DIR"]).glob("*.json"):
    record = json.loads(path.read_text())
    if command == "socket" and target == "session:" + record["sessionId"]:
        print(record["messagingSocketPath"])
        raise SystemExit(0)
raise SystemExit(1)
'''
SEND = '''import json, os, sys
with open(os.environ["TEST_SENT"], "a") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
'''


from scripts.production.staff_test_support import fixture_member, StaffTestCase
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import config_environment

class LifecycleFixture(unittest.TestCase):
    staff: StaffTestCase = StaffTestCase()
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.origin: Path = Path()
        self.checkout: Path = Path()
        self.alpha: Path = Path()
        self.beta: Path = Path()
        self.doc: Path = Path()
        self.log: Path = Path()
        self.state: Path = Path()
        self.home: Path = Path()
        self.phase_table_vault: Path = Path()
        self.env: dict[str, str] = {}

    fixture_root: ClassVar[Path]
    fixture_snapshot: ClassVar[Path]
    fixture_env: ClassVar[dict[str, str]]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        folder = tempfile.TemporaryDirectory(prefix="lifecycle-test-")
        cls.addClassCleanup(folder.cleanup)
        cls.fixture_root = Path(folder.name).resolve() / "case"
        cls.fixture_root.mkdir()
        cls.fixture_snapshot = cls.fixture_root.with_name("snapshot")
        builder = cls()
        builder.root = cls.fixture_root
        builder.build_fixture()
        cls.fixture_env = builder.env.copy()
        _ = shutil.copytree(cls.fixture_root, cls.fixture_snapshot)
        shutil.rmtree(cls.fixture_root)

    @override
    def setUp(self) -> None:
        socket_for = showrunners.socket_for
        labeled_sessions = unit_lookup.labeled_sessions
        self.staff = StaffTestCase()
        self.staff.setUp()
        self.addCleanup(self.staff.doCleanups)
        _ = self.enterContext(mock.patch.object(showrunners, "socket_for", socket_for))
        _ = self.enterContext(mock.patch.object(unit_lookup, "labeled_sessions", labeled_sessions))
        _ = shutil.copytree(self.fixture_snapshot, self.fixture_root)
        self.addCleanup(shutil.rmtree, self.fixture_root)
        self.root = self.fixture_root
        self.bind_fixture()
        self.env = self.fixture_env.copy()
        self.env["STAFF_FILE"] = str(self.staff.staff_file)
        self.env["HERDR_PANE_RECORD_DIR"] = str(self.staff.source_root / "panes")
        self.staff.register_production("example", self.doc)
        owner = fixture_member("showrunner", name="example", doc=staff.LiveDoc(remote.local_machine(), self.doc))
        self.write(self.state / "notifier" / f"showrunner-{owner.id}" / "conf",
                   f"TARGET=session:{owner.current_session}\nCHECK=production_check.sh {owner.id}\n")
        for name, folder in (("alpha", self.alpha), ("beta", self.beta)):
            _ = fixture_member("unit", name=name, parent=owner.id,
                               worktree=staff.PresentWorktree(remote.local_machine(), folder))

    def bind_fixture(self) -> None:
        self.origin = self.root / "origin.git"
        self.checkout = self.root / "checkout"
        self.alpha = self.root / "alpha"
        self.beta = self.root / "beta"
        self.doc = self.checkout / "docs/plans/example-production.md"
        self.log = self.checkout / "production.log"
        self.state = self.root / "state"
        self.home = self.root / "home"
        self.phase_table_vault = self.root / "vault" / "showrunners"

    def build_fixture(self) -> None:
        self.bind_fixture()
        self.state.mkdir()
        self.home.mkdir()
        self.phase_table_vault.parent.mkdir()
        bin_path = self.root / "bin"
        bin_path.mkdir()
        for name in ("claude", "systemd-run", "ssh", "nix", "gh", "zsh", "cargo-berth"):
            self.stub(bin_path / name)
        for relative in (".claude/scripts/message/notifier.sh",
                         ".claude/scripts/production/showrunners.py",
                         ".claude/scripts/lib/py"):
            self.stub(self.home / relative)
        self.env = {**os.environ, "HOME": str(self.home), "LIFECYCLE_TEST_STATE": str(self.state),
                    "PATH": str(bin_path) + os.pathsep + os.environ.get("PATH", ""),
                    "CLAUDE_CODE_SESSION_ID": "test-showrunner",
                    "SHOWRUNNER_SESSION": "environment-showrunner",
                    "NOTIFIER_STATE_DIR": str(self.state / "notifier"),
                    "NOTIFIER_SESSIONS_DIR": str(self.state / "sessions"),
                    "PHASE_TABLE_VAULT": str(self.phase_table_vault),
                    "SHOWRUNNER_STATE_DIR": str(self.state / "showrunner"),
                    "SHOWRUNNERS_SESSIONS": str(self.root / "sessions.py"),
                    "SHOWRUNNERS_SEND": str(self.root / "send.py"), "TEST_SENT": str(self.state / "sent.jsonl")}
        self.env.update(fake_unit_host.environment(self.root))
        self.env.update(config_environment(self.root / "config"))
        self.write(self.root / "sessions.py", SESSIONS)
        self.write(self.root / "send.py", SEND)
        self.checkout.mkdir()
        _ = self.git("init", "--bare", str(self.origin), cwd=self.root)
        _ = self.git("init", "-b", "main", cwd=self.checkout)
        _ = self.git("config", "user.name", "Lifecycle Test")
        _ = self.git("config", "user.email", "lifecycle@example.invalid")
        _ = self.git("remote", "add", "origin", str(self.origin))
        self.write(self.checkout / "README.md", "base\n")
        self.write(self.checkout / "docs/plans/alpha.md", "# Alpha plan\n")
        self.write(self.checkout / "docs/plans/beta.md", "# Beta plan\n")
        self.production_doc("planned")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "initial production plan")
        _ = self.git("push", "-u", "origin", "main")

    def stub(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.write(path, STUB.replace("__PYTHON__", sys.executable))
        path.chmod(0o644 if path.name == "showrunners.py" else 0o755)

    def write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content, encoding="utf-8")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        process = subprocess.run(["git", *args], cwd=cwd or self.checkout, env=self.env,
                                 capture_output=True, text=True, check=False)
        self.assertEqual(process.returncode, 0, (args, process.stdout, process.stderr))
        return process.stdout.strip()

    def production_doc(self, status: str, *, promote: str = "", close_out: str = "") -> None:
        lines = [
            "# Production — example", "",
            f"> **Status: PRODUCTION — {status}.** Example production.",
            "", "## Production Context", "",
            "- **Merge branch:** `production`",
            f"- **Showrunner checkout:** `{self.checkout}`",
            "- **Log:** `production.log`",
            "- **User zone:** America/Los_Angeles",
            "- **Updates:** every 15 minutes",
            "- **Push:** git",
        ]
        if promote:
            lines.append(f"- **Promote:** {promote}")
        lines.extend(["", "## Close-out", ""])
        if close_out:
            lines.append(close_out)
        lines.extend(["", "## Production rules", "", "- No CI, smoke launch, Mac run, or validate_and_push.sh."])
        self.write(self.doc, "\n".join(lines) + "\n")

    def running(self) -> None:
        _ = self.git("switch", "-c", "production")
        self.production_doc("running")
        _ = self.git("add", str(self.doc))
        _ = self.git("commit", "-m", "production(example): plans for 2 units")
        _ = self.git("push", "-u", "origin", "production")
        _ = self.git("worktree", "add", "-b", "alpha-branch", str(self.alpha), "production")
        _ = self.git("worktree", "add", "-b", "beta-branch", str(self.beta), "production")
        _ = self.git("push", "origin", "alpha-branch", "beta-branch")
        runner = fixture_member("showrunner", name="example",
                              doc=staff.LiveDoc(remote.local_machine(), self.doc))
        for name, worktree in (("alpha", self.alpha), ("beta", self.beta)):
            row = fixture_member("unit", name=name, parent=runner.id,
                               worktree=staff.PresentWorktree(remote.local_machine(), worktree))
            _ = unit_config.write(worktree, unit_config.UnitConfig(
                unit_config.PlanFile(Path(f"docs/plans/{name}.md")), unit_config.NoAppPort(), ()))
            self.staff.pane(f"fixture:{name}", session_id=row.id, environment={
                "SHOWRUNNER_UNIT": "example", "SHOWRUNNER_UNIT_ID": name,
            })
            _ = self.staff.session_transcript(row.id, worktree, home=self.home)
        common_dir = Path(self.git("rev-parse", "--git-common-dir"))
        with (self.checkout / common_dir / "info/exclude").open("a", encoding="utf-8") as output:
            _ = output.write("production.log\n")
        self.write(self.log, "# Production log — example\n\n### STATE\nold state\n")

    def park_fixture_unit(self, name: str, worktree: Path, branch: str) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == name)
        with mock.patch.dict(os.environ, self.env):
            _ = unit_config.park(row.id, worktree, unit_config.RetiredSession("2026-10-31", row.id, unit_config.Branch(branch)))

    def run_lifecycle(self, action: str, *extra: str) -> subprocess.CompletedProcess[str]:
        # Restore import-time state just as a fresh CLI would compute it.
        with mock.patch.object(tidy_wrapped, "STATE_DIR", self.state / "showrunner"), \
             mock.patch.object(tidy_wrapped, "NOTIFIER_STATE_DIR", self.state / "notifier"):
            return invoke_cli(production_lifecycle, production_lifecycle.main,
                              [action, "--production", "example", *extra],
                              self.env, self.checkout, production_state=True)

    def run_lifecycle_process(self, action: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), action, "--production", "example",
                               *extra], cwd=self.checkout, env=self.env, capture_output=True,
                              text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

    def assert_step(self, result: subprocess.CompletedProcess[str], state: str) -> None:
        self.assertRegex(result.stdout, rf"(?m)^[\w-]+: {state} — .+")
        self.assertEqual(result.returncode, 0 if state == "ok" else 2,
                         result.stdout + result.stderr)

    def assert_no_ci_skips(self, output: str) -> None:
        steps = cast(list[str], re.findall(r"(?m)^([\w-]+): ok — skipped: no CI$", output))
        for name in ("ci", "smoke", "mac"):
            self.assertTrue(any(name in step.lower() for step in steps), output)

    def side_commit(self, worktree: Path, branch: str, unit: str, phase: str,
                    *, shrink: bool = False) -> tuple[str, str]:
        relative = f"{unit}-{phase}{'-shrink' if shrink else ''}.txt"
        self.write(worktree / relative, relative + "\n")
        _ = self.git("add", relative, cwd=worktree)
        _ = self.git("commit", "-m", relative, cwd=worktree)
        commit = self.git("rev-parse", "HEAD", cwd=worktree)
        _ = self.git("push", "origin", branch, cwd=worktree)
        subject = f"Merge {unit} phase {phase}{' shrink' if shrink else ''} ({commit[:7]}) into production"
        _ = self.git("merge", "--no-ff", "-m", subject, branch)
        return commit, self.git("rev-parse", "HEAD")

    def merge_tip(self) -> str:
        self.write(self.checkout / "merge-only.txt", "merge\n")
        _ = self.git("add", "merge-only.txt")
        _ = self.git("commit", "-m", "merge work")
        _ = self.git("push", "origin", "production")
        return self.git("rev-parse", "HEAD")

    def promotion_checkout(self, name: str) -> Path:
        destination = self.root / name
        _ = self.git("clone", "-b", "main", str(self.origin), str(destination), cwd=self.root)
        _ = self.git("config", "user.name", "Lifecycle Test", cwd=destination)
        _ = self.git("config", "user.email", "lifecycle@example.invalid", cwd=destination)
        return destination

    def owner_id(self, production_name: str = "example") -> staff.StaffId:
        return next(row.id for row in staff.find(kind="showrunner") if row.name == production_name)

    def showrunner(self, production_name: str, *, live: bool = False) -> None:
        """Publish the private owner and its update timer."""
        doc = self.root / f"{production_name}-production.md"
        self.write(doc, f"# {production_name}\n")
        owners = [row for row in staff.find(kind="showrunner") if row.name == production_name]
        owner = (staff.set_doc(owners[0].id, staff.LiveDoc(remote.local_machine(), doc)) if owners
                 else fixture_member("showrunner", name=production_name, doc=staff.LiveDoc(remote.local_machine(), doc)))
        self.write(self.state / "notifier" / f"showrunner-{owner.id}" / "conf",
                   f"TARGET=session:{owner.current_session}\nCHECK=zsh /x/production_check.sh {owner.id}\n")
        if live:
            self.write(self.state / "sessions" / f"{os.getpid()}.json",
                       json.dumps({"pid": os.getpid(), "name": f"{production_name}-name", "sessionId": owner.current_session,
                                   "messagingSocketPath": str(self.root / f"{production_name}.sock")}))

    def reports_to(self, child: str, parent: str) -> Path:
        path = self.state / "showrunner" / self.owner_id(child) / "parent"
        self.write(path, parent + "\n")
        return path

    def sent(self) -> list[list[str]]:
        path = self.state / "sent.jsonl"
        if not path.exists():
            return []
        return [cast(list[str], json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines()]

    def merged_units(self) -> None:
        self.running()
        _ = self.side_commit(self.alpha, "alpha-branch", "alpha", "1")
        _ = self.side_commit(self.beta, "beta-branch", "beta", "1")
        _ = self.git("push", "origin", "production")

    def calls(self) -> str:
        return (self.state / "calls").read_text() if (self.state / "calls").exists() else ""

    def write_phase_note(self, name: str, production: str, unit: str) -> Path:
        note = self.phase_table_vault / "showrunner" / f"{name}.md"
        self.write(
            note,
            "\n".join(
                (
                    "---",
                    "phase_table: true",
                    f"production: {production}",
                    f"unit: {unit}",
                    "---",
                    "",
                    f"# {name}",
                    "",
                )
            ),
        )
        return note
