"""Registration steps against a disposable production and notifier."""

from __future__ import annotations

from scripts.agents import remote

import io
from types import ModuleType
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock
from pathlib import Path
from typing import ClassVar, TypedDict, cast, override



from scripts.production import update_registration
registration = update_registration
from scripts.production import showrunners as showrunners
from scripts.production import staff, unit_names
from scripts.production import dailies_render as dailies_render
from scripts.production.production_doc import read_production as read_production
from scripts.production.cli_fixture import invoke_cli, production_paths
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production.staff_test_support import (
    fixture_member, isolated_staff_file, recorded_showrunner, showrunner_launch, showrunner_relationship,
)

from scripts.notify import review as review

SCRIPT = Path(__file__).with_name("update_registration.py")


class QuestionRegistration(TypedDict):
    time: str
    text: str


NOTIFIER = '''#!__PYTHON__
import json
import os
from pathlib import Path
import sys

root = Path(os.environ["REGISTRATION_TEST_STATE"])
args = sys.argv[1:]
with (root / "notifier-argv.jsonl").open("a", encoding="utf-8") as output:
    output.write(json.dumps(args) + "\\n")
instance = root / ("instance-" + args[1]) if len(args) > 1 else root / "missing"
if args[0] == "status":
    if not instance.exists():
        print(f"no such instance: {args[1]}", file=sys.stderr)
        raise SystemExit(1)
    mode = "stopped" if instance.read_text(encoding="utf-8") == "stopped" else "enabled"
    print(f"{args[1]} → session:test every 15 min, {mode}")
    print("next_due=1791334500 (2026-10-06 17:55 PDT)")
elif args[0] == "new":
    instance.touch()
    timer = Path(os.environ["NOTIFIER_STATE_DIR"]) / args[1]
    timer.mkdir(parents=True, exist_ok=True)
    target = args[args.index("--to") + 1] if "--to" in args else ""
    (timer / "conf").write_text(f"TARGET={target}\\n", encoding="utf-8")
    print("next_due=1791334500 (2026-10-06 17:55 PDT)")
elif args[0] == "remove":
    instance.unlink(missing_ok=True)
elif args[0] == "stop":
    _ = instance.write_text("stopped", encoding="utf-8")
else:
    raise SystemExit(2)
'''


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


class RegistrationFixture(unittest.TestCase):
    root: Path = Path()
    home: Path = Path()
    checkout: Path = Path()
    doc: Path = Path()
    owner_id: staff.StaffId = staff.StaffId("")
    state: Path = Path()
    config: Path = Path()
    env: dict[str, str] = {}

    fixture_root: ClassVar[Path]
    fixture_snapshot: ClassVar[Path]
    command_snapshot: ClassVar[Path]
    needs_command_copy: ClassVar[bool] = False

    @classmethod
    @override
    def setUpClass(cls) -> None:
        folder = tempfile.TemporaryDirectory(prefix="registration-test-")
        cls.addClassCleanup(folder.cleanup)
        cls.fixture_root = Path(folder.name).resolve() / "case"
        cls.fixture_snapshot = cls.fixture_root.with_name("snapshot")
        cls.command_snapshot = cls.fixture_root.with_name("command")
        builder = cls()
        try:
            builder.bind_fixture(cls.fixture_root)
            builder.build_fixture()
            if cls.needs_command_copy:
                builder.build_command_copy(cls.command_snapshot)
        finally:
            builder.doCleanups()
        _ = shutil.copytree(cls.fixture_root, cls.fixture_snapshot)
        shutil.rmtree(cls.fixture_root)

    @override
    def setUp(self) -> None:
        self.addCleanup(shutil.rmtree, self.fixture_root)
        _ = shutil.copytree(self.fixture_snapshot, self.fixture_root)
        self.bind_fixture(self.fixture_root)
        self.owner_id = next(row.id for row in staff.snapshot().find(kind="showrunner")
                             if isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)) and row.doc.path == self.doc)

    def bind_fixture(self, root: Path) -> None:
        self.root = root
        self.home = self.root / "home"
        self.checkout = self.root / "checkout"
        self.state = self.root / "state"
        self.config = self.root / "showrunners.json"
        self.doc = self.checkout / "docs/plans/registration-notes.md"
        bin_dir = self.root / "bin"
        self.env = {**os.environ, "HOME": str(self.home), "USER_ZONE": "America/Los_Angeles", "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                    "REGISTRATION_TEST_STATE": str(self.state), "SHOWRUNNER_STATE_DIR": str(self.state / "showrunner"),
                    "NOTIFIER_STATE_DIR": str(self.state / "notifier"), "SHOWRUNNERS_CONFIG": str(self.config),
                    "USAGE_STATE_DIR": str(self.state / "usage"),
                    "MAC_TEST_STATE_DIR": str(self.state / "mac-test"), "NEXT_STATE_DIR": str(self.state / "next"),
                    "CLAUDE_CODE_SESSION_ID": "current-session-id", "NOTIFIER_SESSIONS_DIR": str(self.state / "sessions"),
                    "HERDR_PANE_RECORD_DIR": str(self.state / "pane-records"), "HERDR_SESSION": "agents"}
        _ = self.enterContext(isolated_staff_file(self.state / "staff/staff.json", self.env))

    def build_fixture(self) -> None:
        self.root.mkdir()
        self.home.mkdir()
        self.checkout.mkdir()
        self.state.mkdir()
        notifier = self.home / ".claude/scripts/message/notifier.sh"
        notifier.parent.mkdir(parents=True)
        _ = notifier.write_text(NOTIFIER.replace("__PYTHON__", sys.executable), encoding="utf-8")
        notifier.chmod(0o755)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        # The command text names zsh; this shim runs only the disposable Python notifier.
        zsh = bin_dir / "zsh"
        _ = zsh.write_text("#!/bin/sh\nexec '" + sys.executable + "' \"$@\"\n", encoding="utf-8")
        zsh.chmod(0o755)
        # Register refuses a session not named for its production's name: both live sessions are named example.
        (self.state / "sessions").mkdir()
        for pid, session in ((os.getpid(), "current-session-id"), (os.getppid(), "resume-session-id")):
            _ = (self.state / "sessions" / f"{pid}.json").write_text(
                json.dumps({"pid": pid, "sessionId": session, "name": "example"}), encoding="utf-8")
        _ = self.git("init", "-b", "production", str(self.checkout), cwd=self.root)
        _ = self.git("config", "user.name", "Registration Test")
        _ = self.git("config", "user.email", "registration@example.invalid")
        self.doc.parent.mkdir(parents=True)
        # A linked worktree, where `.git` is a file: herdr is never asked whether its unit is gone.
        alpha = self.root / "alpha"
        alpha.mkdir()
        _ = (alpha / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        (self.root / "issues").mkdir()
        _ = self.doc.write_text("\n".join((
            "# Production — example", "", "## Production Context", "",
            "- **Merge branch:** `production`", f"- **Showrunner checkout:** `{self.checkout}`",
            "- **Log:** `production.log`",
            "- **User zone:** America/Los_Angeles", "- **Updates:** every 15 minutes",
            f"- **Proposals:** `{self.root / 'issues'}`", "",
        )), encoding="utf-8")
        runner = recorded_showrunner("example", self.doc)
        self.owner_id = runner.id
        _ = fixture_member("unit", name="alpha", parent=runner.id,
                           worktree=staff.PresentWorktree(remote.local_machine(), alpha))
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "initial")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        process = subprocess.run(["git", *args], cwd=cwd or self.checkout, env=self.env if self.env else None,
                                 capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, process.stderr)
        return process.stdout.strip()

    def fixture_owner_name(self) -> str:
        return next(row.name for row in staff.snapshot().find(kind="showrunner")
                    if row.id == self.owner_id)

    def run_command(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        environment = env or self.env
        real_run = subprocess.run
        notifier = self.home / ".claude/scripts/message/notifier.sh"

        def run(command: list[str], *, cwd: Path | None = None, capture_output: bool = False,
                text: bool = False, check: bool = False, timeout: float = TEST_HANG_GUARD_SECS) -> subprocess.CompletedProcess[str]:
            if command[:2] == ["zsh", str(notifier)]:
                stub = ModuleType("registration_notifier_fixture")
                stub.__file__ = str(notifier)

                def main() -> int:
                    exec(compile(notifier.read_text(), str(notifier), "exec"),
                         {**vars(stub), "__name__": "__main__"})
                    return 0

                return invoke_cli(stub, main, command[2:], dict(os.environ), Path.cwd())
            return real_run(command, cwd=cwd, capture_output=capture_output, text=text, check=check, timeout=timeout)

        with (mock.patch.object(subprocess, "run", run),
              mock.patch.object(showrunners, "SCRATCH_ROOT", Path(environment.get("SHOWRUNNER_SCRATCH_ROOT", "/tmp")))):
            return invoke_cli(registration, registration.main, [*args, "--production", self.fixture_owner_name()],
                              environment, self.checkout, production_state=True)

    def notifier_calls(self) -> list[list[str]]:
        path = self.state / "notifier-argv.jsonl"
        return ([cast(list[str], json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines()]
                if path.exists() else [])

    def outstanding(self) -> Path:
        owner = next(row for row in staff.snapshot().find(kind="showrunner")
                     if row.id == self.owner_id)
        path = self.state / "showrunner/outstanding" / f"{owner.id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def build_command_copy(self, tree: Path) -> None:
        _ = self.question_instance()
        _ = shutil.copytree(SCRIPT.parent.parent, tree / "scripts",
                            ignore=shutil.ignore_patterns("__pycache__"))
        hooks = tree / "scripts/hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        _ = shutil.copy2(SCRIPT.parent.parent / "hooks/showrunner_footer.py", hooks)
        template = tree / "commands/showrunner/produce.md"
        template.parent.mkdir(parents=True)
        _ = shutil.copy2(SCRIPT.parents[2] / "commands/showrunner/produce.md", template)

    def copied_command(self) -> Path:
        tree = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="command-copy-", dir=self.root)))
        _ = shutil.copytree(self.command_snapshot, tree, dirs_exist_ok=True)
        return tree / "scripts/production/update_registration.py"

    def run_copy(self, script: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(script), *args, "--production", self.fixture_owner_name()],
                              cwd=self.checkout, env=self.env, capture_output=True, text=True, check=False,
                              timeout=TEST_HANG_GUARD_SECS)

    def question_state(self, items: list[dict[str, str] | str]) -> Path:
        path = self.root / "questions.json"
        _ = path.write_text(json.dumps({"open_for_user": items}))
        return path

    def run_questions(self, state: Path) -> str:
        with (mock.patch.dict(os.environ, self.env), production_paths(self.env),
              mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.state / "notifier"),
              redirect_stdout(io.StringIO()) as output):
            registration.questions(read_production("example"), state)
        return output.getvalue()

    def question_instance(self) -> Path:
        instance = self.state / "notifier" / f"showrunner-{self.owner_id}"
        instance.mkdir(parents=True, exist_ok=True)
        _ = (instance / "conf").write_text(f"TARGET=session:{self.owner_id}\n")
        return instance

    def timer(self, name: str) -> None:
        """A production with a complete update timer, so the tree counts it."""
        _ = self.document_for(name)
        row_id = self.fixture_staff_id(name=name)
        row = staff.by_id(row_id)
        assert row is not None
        instance = self.state / "notifier" / f"showrunner-{row.id}"
        instance.mkdir(parents=True)
        _ = (instance / "conf").write_text(
            f"TARGET=session:{row.id}\nCHECK=zsh /x/production_check.sh {row.id}\n", encoding="utf-8")
        showrunner_launch(row, row.id, self.env)

    def attach(self, child: str, parent: str) -> None:
        _ = showrunner_relationship(child, parent)

    def document_for(self, name: str) -> Path:
        if name == "example":
            return self.doc
        document = self.doc.with_name(f"{name}-production.md")
        if not document.exists():
            write_text_atomic(document, self.doc.read_text().replace("example", name))
        return document

    def fixture_staff_id(self, reference: str = "", name: str = "example") -> staff.StaffId:
        record = staff.initial_snapshot()
        fixed = record.by_id(staff.StaffId(reference)) if reference else None
        if fixed is not None:
            return fixed.id
        matches = [row for row in record.find(kind="showrunner") if row.name == name]
        owner = matches[0] if matches else fixture_member("showrunner", name=name)
        _ = staff.set_doc(owner.id, staff.LiveDoc(remote.local_machine(), self.document_for(name)))
        if not reference or reference == "showrunner":
            return owner.id
        return fixture_member("unit", name=unit_names.name(reference), parent=owner.id,
                              worktree=staff.PresentWorktree(remote.local_machine(), self.root / unit_names.name(reference))).id

    def write_open_for(self, name: str, *items: dict[str, str]) -> None:
        owner = self.fixture_staff_id(name=name)
        folder = self.root / "issues"
        for path in folder.glob("*.md"):
            if f"production_id: {owner}\n" in path.read_text():
                path.unlink()
        reminders: list[dict[str, str]] = []
        for index, item in enumerate(items):
            if "question" not in item:
                reminders.append(item)
                continue
            fields = {**item, "asked_by": self.fixture_staff_id(item.get("asked_by", ""), name=name)}
            text = (f"---\nrecord_type: question\ntags: [question]\nstatus: open\nproduction_id: {owner}\n"
                    + f"asked_by: {fields['asked_by']}\nregistration: {json.dumps(fields)}\n---\n"
                    + f"# {fields['text']}\n\n{fields['question']}\n")
            _ = (folder / f"question-fixture-{owner}-{index}.md").write_text(text)
        _ = self.outstanding().with_name(f"{owner}.json").write_text(json.dumps(reminders), encoding="utf-8")

    def open_rows(self) -> list[dict[str, str]]:
        with mock.patch.dict(os.environ, self.env), production_paths(self.env):
            rows = registration.open_entries(read_production("example"), registration.QuestionStaff.read())
            return cast(list[dict[str, str]], cast(object, rows))

    def read_items(self, path: Path | None = None) -> list[dailies_render.Outstanding]:
        with mock.patch.dict(os.environ, self.env), production_paths(self.env):
            return registration.read_open_items(path or self.outstanding(), document=self.doc)

    def question_record_path(self, title: str) -> Path:
        paths: list[Path] = []
        for path in (self.root / "issues").glob("*.md"):
            lines = path.read_text().splitlines()
            if "record_type: question" in lines and "status: open" in lines:
                fields = cast(dict[str, str], json.loads(next(line.removeprefix("registration: ")
                              for line in lines if line.startswith("registration: "))))
                if fields["text"] == title:
                    paths.append(path)
        self.assertEqual(len(paths), 1, title)
        return paths[0]

    def open_lines(self) -> list[str]:
        listed = self.run_command("open")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        return listed.stdout.splitlines()

    def run_ask(self, text: str, question: str, unit: str = "") -> str:
        with (mock.patch.dict(os.environ, self.env), production_paths(self.env),
              mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", self.state / "notifier"),
              redirect_stdout(io.StringIO()) as output):
            registration.ask(read_production("example"), text, question, unit, dailies_render.Ranking())
        return output.getvalue()
