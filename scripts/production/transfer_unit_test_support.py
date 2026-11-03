"""Use private staff records and Git repositories for unit transfer tests."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, cast, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts.herdr import fake_herdr
from scripts.production import showrunners
from scripts.production.cli_fixture import invoke_cli
from scripts.production.rename_test_support import bind_staff_fixture
from scripts.production.staff_test_support import StaffTestCase, unit_membership
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
import transfer_unit

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
        bind_staff_fixture(self, self.env, {"demo": ("alpha-unit", "beta-unit"), "other": ("gamma-unit",)},
                           locations={("demo", "alpha-unit"): self.unit,
                                      ("demo", "beta-unit"): self.rust / "claude-demo-beta",
                                      ("other", "gamma-unit"): self.root / "absent-gamma"})
        row = unit_membership("demo", "alpha-unit")
        self.pane("w1:p1", session_id="5ad615f2-4e04-5121-a102-9d6b2e2a6eb3", environment={
            "SHOWRUNNER_UNIT": "demo", "SHOWRUNNER_UNIT_ID": "alpha-unit", "CLAUDE_CODE_SESSION_ID": row.id,
        })
        self.write(self.root / "state/notifier/showrunner-other/conf",
                   "TARGET=session:cc9c5b71-6f71-5499-a4bb-e6ad5d02930d\nCHECK=" + shlex.join(["zsh", "/scripts/production_check.sh",
                   str(self.receiving / "docs/plans/other-production.md")]) + "\n")
        for module, name, value in (
            (showrunners, "NOTIFIER_STATE_DIR", self.root / "state/notifier"),
            (showrunners, "SHOWRUNNER_STATE_DIR", self.root / "state/showrunner"),
            (showrunners, "CONFIG", self.root / "home/.claude/showrunners.json"),
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
                   json.dumps({"default": {"stdout": '{"sessions": []}', "code": 0},
                               "--session agents pane list": {"stdout": '{"result": {"panes": []}}', "code": 0}}))
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
                   "EVERY=15\nTARGET=session:8f83b18e-2d35-5935-a408-9f483d34afff\nCHECK="
                   + shlex.join(["zsh", "/scripts/production_check.sh",
                                 str(self.giving / "docs/plans/demo-production.md")]) + "\n")
        self.write(self.stall_path("demo", "alpha-unit"), '{"stretch": 17}\n')

    def production_doc(self, slug: str, checkout: Path, *table: str) -> str:
        return "\n".join(["# Production — " + slug, "", "## Production Context", "",
                          f"- **Merge branch:** `{slug}`", f"- **Showrunner checkout:** `{checkout}`",
                          "- **Log:** `production.log`", "", "## Units", "", *table, "",
                          "## Production rules", "", "Rules.", ""])

    def write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, content)

    def git(self, *arguments: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", *arguments], cwd=cwd or self.giving, env=self.env,
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, (arguments, result.stdout, result.stderr))
        return result.stdout.strip()

    def stall_path(self, slug: str, unit: str) -> Path:
        digest = hashlib.sha256(f"{slug}\0{unit}".encode()).hexdigest()
        return self.root / "state/stall-watch" / f"{digest}.json"

    def assert_ok(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def transfer(self, verb: str, unit: str = "alpha-unit", to: Path | None = None) -> subprocess.CompletedProcess[str]:
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
            return original_run(command, *args, **kwargs)

        with mock.patch.object(subprocess, "run", run):
            return invoke_cli(transfer_unit, lambda: transfer_unit.main(arguments), arguments,
                              self.env, self.giving, production_state=True)

    def messages(self) -> list[list[str]]:
        path = self.root / "messages.jsonl"
        return [cast(list[str], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []
