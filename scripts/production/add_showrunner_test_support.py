"""add_showrunner.py through its CLI, with fake herdr, the session records and the
message sender: nothing real starts and nothing real is sent."""
from __future__ import annotations

import json
import os
import shutil
import time
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import ClassVar, cast, override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production.cli_fixture import invoke_cli
from scripts.accounts import accounts
from scripts.production import add_showrunner
from scripts.production import showrunners
from scripts.herdr import herdr
from scripts.herdr.fake_herdr import CommandResponse

# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300

SCRIPT = Path(__file__).with_name("add_showrunner.py")
SHARED = herdr.SHARED_SESSION
PARENT_SESSION = "11111111-2222-3333-4444-555555555555"
STUB = '''#!__PYTHON__
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

state = Path(os.environ["STUB_STATE"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with (state / "events.jsonl").open("a") as events:
    events.write(json.dumps({"command": name, "args": args,
                             "claude_env": sorted(key for key in os.environ if key.startswith("CLAUDE_"))}) + "\\n")
if name == "systemd-run":
    raise SystemExit(subprocess.run(args[3:], check=False).returncode)
'''
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


from scripts.production.staff_test_support import StaffTestCase

class AddShowrunnerFixture(unittest.TestCase):
    staff: StaffTestCase = StaffTestCase()
    root: Path = Path()
    state: Path = Path()
    env: dict[str, str] = {}
    on_start: dict[str, str] = {}
    herdr_overrides: dict[str, CommandResponse] = {}
    processes: list[subprocess.Popen[bytes]] = []

    fixture_root: ClassVar[Path]
    fixture_snapshot: ClassVar[Path]
    fixture_env: ClassVar[dict[str, str]]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory(prefix="add-showrunner-test-")
        cls.addClassCleanup(directory.cleanup)
        cls.fixture_root = Path(directory.name).resolve() / "case"
        cls.fixture_snapshot = cls.fixture_root.parent / "snapshot"
        builder = cls()
        builder.root = cls.fixture_root
        builder.root.mkdir()
        builder.build_fixture()
        cls.fixture_env = builder.env.copy()
        _ = shutil.copytree(cls.fixture_root, cls.fixture_snapshot, symlinks=True)
        shutil.rmtree(cls.fixture_root)

    @override
    def setUp(self) -> None:
        socket_for = showrunners.socket_for
        self.staff = StaffTestCase()
        self.staff.setUp()
        self.addCleanup(self.staff.doCleanups)
        _ = self.enterContext(patch.object(showrunners, "socket_for", socket_for))
        # Account links, trust entries and timer records embed absolute paths.
        # Restore private files and symlinks at the seed's original path.
        self.root = self.fixture_root
        _ = shutil.copytree(self.fixture_snapshot, self.root, symlinks=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.state = self.root / "stub"
        self.env = self.fixture_env.copy()
        self.env["STAFF_FILE"] = str(self.staff.staff_file)
        self.env["HERDR_PANE_RECORD_DIR"] = str(self.staff.source_root / "panes")
        self.on_start = {}
        self.herdr_overrides = {}
        self.processes = []
        self.addCleanup(self.close_processes)
        self.live(PARENT_SESSION, "top")

    def build_fixture(self) -> None:
        self.state = self.root / "stub"
        self.state.mkdir()
        bin_path = self.root / "bin"
        bin_path.mkdir()
        for name in ("systemd-run",):
            stub = bin_path / name
            _ = stub.write_text(STUB.replace("__PYTHON__", sys.executable), encoding="utf-8")
            stub.chmod(0o755)
        for name in ("home", "sessions"):
            (self.root / name).mkdir()
        _ = (self.root / "sessions.py").write_text(SESSIONS, encoding="utf-8")
        _ = (self.root / "send.py").write_text(SEND, encoding="utf-8")
        fake = bin_path / "submitting-herdr"
        source = (SCRIPT.parents[1] / "herdr/fake_herdr.py").read_text()
        source = source.replace('responses.get("default", {"stdout": "", "code": 0})',
            'responses.get(" ".join(argv[2:4]), responses.get("default", {"stdout": "", "code": 0}))')
        source = source.replace('raise SystemExit(main(sys.argv[1:]))', 'raise SystemExit(submitting_main(sys.argv[1:]))')
        hook = r'''def submitting_main(argv):
    result = main(argv)
    if result == 0 and argv[2:4] == ["agent", "prompt"]:
        from datetime import datetime, timezone
        calls = [json.loads(line) for line in Path(os.environ["FAKE_HERDR_LOG"]).read_text().splitlines()]
        start = next(row for row in reversed(calls) if row[:2] == argv[:2] and row[2:4] == ["agent", "start"])
        flag = "--resume" if "--resume" in start else "--session-id"
        session_id = start[start.index(flag) + 1]
        config = next(value.split("=", 1)[1] for row in reversed(calls) for value in row
                      if row[:2] == argv[:2] and value.startswith("CLAUDE_CONFIG_DIR="))
        path = Path(config) / "projects/fixture" / (session_id + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as transcript:
            transcript.write(json.dumps({"type": "user", "timestamp": datetime.now(timezone.utc).isoformat(),
                                        "message": {"role": "user", "content": argv[-1]}}) + "\n")
    return result

'''
        source = source.replace('if __name__ == "__main__":', hook + 'if __name__ == "__main__":')
        _ = fake.write_text(source)
        fake.chmod(0o755)
        self.on_start = {}
        self.herdr_overrides = {}
        self.processes = []
        self.env = {**{key: value for key, value in os.environ.items() if not key.startswith("CLAUDE_")},
                    "HERDR_PLATFORM": "linux",
                    "HOME": str(self.root / "home"), "STUB_STATE": str(self.state),
                    "PATH": str(bin_path) + os.pathsep + os.environ.get("PATH", ""),
                    "HERDR_BIN": str(fake),
                    "SYSTEMD_RUN": str(fake),
                    "SYSTEMCTL": str(fake),
                    "HERDR_PROC": str(self.root / "proc"),
                    "FAKE_HERDR_LOG": str(self.state / "herdr.jsonl"),
                    "FAKE_HERDR_RESPONSES": str(self.state / "herdr-responses.json"),
                    "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
                    "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
                    "SHOWRUNNER_STATE_DIR": str(self.root / "showrunner"),
                    "SHOWRUNNERS_SESSIONS": str(self.root / "sessions.py"),
                    "SHOWRUNNERS_SEND": str(self.root / "send.py"), "TEST_SENT": str(self.root / "sent.jsonl"),
                    # The parent runs the command, so its own session and account are in the caller's environment.
                    "CLAUDE_CODE_SESSION_ID": PARENT_SESSION, "CLAUDE_CONFIG_DIR": str(self.root / "unit-account"),
                    "ADD_SHOWRUNNER_WAIT_S": "0", "ADD_SHOWRUNNER_POLL_S": "0.01",
                    "ADD_SHOWRUNNER_CLAUDE_JSON": str(self.root / "claude.json")}
        # The folders these tests start sessions in are trusted; the trust tests change that.
        self.trust(self.root, self.root / "kid-checkout", self.root / "elsewhere")
        home = Path(self.env["HOME"])
        (home / ".claude").mkdir()
        _ = (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "main@example.invalid"}}))
        notes = home / "notes"
        notes.mkdir()
        self.env["AGENT_NOTES_DIR"] = str(notes)
        _ = (notes / "claude2.md").write_text("---\nlogin: main@example.invalid\n---\n")
        now = datetime.now(timezone.utc)
        file = home / ".local/state/agent-notes/usage.json"
        file.parent.mkdir(parents=True)
        _ = file.write_text(json.dumps({"main@example.invalid": {"weekly": {"left": 1,
            "at": now.isoformat(), "resets": (now + timedelta(days=2)).isoformat()}}}))
        with patch.dict(os.environ, self.env):
            self.assertEqual(accounts.setup("claude2"), [])
            _ = (accounts.account_dir("claude2") / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "main@example.invalid"}}))
        _ = self.configure_groups()
        self.timer("top", PARENT_SESSION)

    def configure_groups(self, *, units: str = "claude2", showrunners: str = "claude2") -> Path:
        path = Path(self.env["HOME"]) / ".local/state/accounts/groups.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"units": units, "showrunners": showrunners}))
        return path

    def trust(self, *folders: Path, extra: dict[str, object] | None = None) -> None:
        """The showrunner account's config, trusting exactly `folders`, with `extra` fields beside them."""
        projects = {str(folder.resolve()): {"hasTrustDialogAccepted": True, "allowedTools": ["SECRET-TOOL"]}
                    for folder in folders}
        _ = (self.root / "claude.json").write_text(json.dumps({**(extra or {}), "projects": projects}), encoding="utf-8")

    def doc(self, slug: str) -> Path:
        path = self.root / f"{slug}-production.md"
        if not path.exists():
            (self.root / f"{slug}-checkout").mkdir()
            _ = path.write_text("\n".join([
                f"# {slug}", "", f"- **Merge branch:** `{slug}`",
                f"- **Showrunner checkout:** `{self.root / f'{slug}-checkout'}`",
                f"- **Log:** `docs/plans/{slug}-log.md`", "- **User zone:** America/Los_Angeles",
                "- **Updates:** every 15 minutes", "", "## Units", "", "| Unit | Plan |", "| --- | --- |", ""]),
                encoding="utf-8")
        return path

    def timer_conf(self, slug: str) -> Path:
        return self.root / "notifier" / f"showrunner-{slug}" / "conf"

    def timer_text(self, slug: str, session: str) -> str:
        return f"TARGET=session:{session}\nCHECK=zsh /x/production_check.sh {self.doc(slug)}\n"

    def timer(self, slug: str, session: str) -> None:
        self.timer_conf(slug).parent.mkdir(parents=True)
        _ = self.timer_conf(slug).write_text(self.timer_text(slug, session), encoding="utf-8")

    def close_processes(self) -> None:
        for process in self.processes:
            process.terminate()
            _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
            if process.stdin is not None:
                process.stdin.close()

    def record(self, session: str, name: str = "") -> tuple[str, str]:
        """A live session's record: where it is kept and what it holds. Its name is read from a live process's;
        a showrunner's name is its production's slug."""
        process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        return (str(self.root / "sessions" / f"{process.pid}.json"),
                json.dumps({"pid": process.pid, "name": name or f"{session}-name", "sessionId": session,
                            "messagingSocketPath": str(self.root / f"{session}.sock")}))

    def live(self, session: str, name: str = "") -> None:
        path, text = self.record(session, name)
        _ = Path(path).write_text(text, encoding="utf-8")

    def starts(self, session: str, *, registers: str = "") -> None:
        """The started session comes up as `session`, and registers `registers`'s update timer when named."""
        path, text = self.record(session, registers)
        self.on_start[path] = text
        if registers:
            self.on_start[str(self.timer_conf(registers))] = self.timer_text(registers, session)
        for destination in self.on_start:
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
        if registers and not self.timer_conf(registers).exists():
            # Fake herdr writes files into existing folders. Before launch this
            # timer belongs to an ended session, so it does not claim the child.
            _ = self.timer_conf(registers).write_text(self.timer_text(registers, "not-started"))

    def sent_to(self) -> list[str]:
        path = self.root / "sent.jsonl"
        if not path.exists():
            return []
        sent = [cast(list[str], json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines()]
        return [arguments[arguments.index("--to") + 1] for arguments in sent]

    def run_command(self, slug: str, *extra: str, cwd: Path | None = None,
                    subprocess_cli: bool = False) -> subprocess.CompletedProcess[str]:
        self.prepare_herdr(slug)
        return self.invoke(["--production", str(self.doc(slug)), "--model", "opus", "--effort", "high", *extra],
                           cwd=cwd, subprocess_cli=subprocess_cli)

    def invoke(self, arguments: list[str], *, cwd: Path | None = None,
               subprocess_cli: bool = False) -> subprocess.CompletedProcess[str]:
        if subprocess_cli:
            return subprocess.run([sys.executable, str(SCRIPT), *arguments], env=self.env,
                                  capture_output=True, text=True, check=False, cwd=cwd,
                                  timeout=TEST_HANG_GUARD_SECS)
        real_run = subprocess.run

        def run(command: list[str], *, capture_output: bool = False, text: bool = False,
                check: bool = False, timeout: float | None = None, env: dict[str, str] | None = None,
                cwd: Path | str | None = None, stdout: int | None = None,
                stderr: int | None = None, input: str | None = None) -> subprocess.CompletedProcess[str]:
            child_env = dict(os.environ) if env is None else env
            child_cwd = Path.cwd() if cwd is None else Path(cwd)
            executable = Path(shutil.which(command[0], path=child_env.get("PATH")) or command[0])
            script, args = executable, command[1:]
            if executable.name.startswith("python") and len(command) > 1:
                script, args = Path(command[1]), command[2:]
            if script.is_relative_to(self.root) and script.is_file():
                fake = ModuleType("add_showrunner_tool_fixture")
                fake.__file__ = str(script)

                def main() -> int:
                    exec(compile(script.read_text(), str(script), "exec"), {**vars(fake), "__name__": "__main__"})
                    return 0

                result = invoke_cli(fake, main, args, child_env, child_cwd)
                if check and result.returncode:
                    raise subprocess.CalledProcessError(result.returncode, command, result.stdout, result.stderr)
                return result
            return real_run(command, capture_output=capture_output, text=text, check=check,
                            timeout=timeout, env=env, cwd=cwd, stdout=stdout, stderr=stderr, input=input)

        elapsed = 0.0

        def advance(seconds: float) -> None:
            nonlocal elapsed
            # Registry fixtures change on the next read; no asynchronous writer
            # needs wall time. Keep the requested interval observable.
            with (self.root / "polls.jsonl").open("a") as output:
                _ = output.write(json.dumps(seconds) + "\n")
            elapsed += seconds

        with (patch.object(subprocess, "run", run),
              patch.object(add_showrunner, "WAIT_S", float(self.env["ADD_SHOWRUNNER_WAIT_S"])),
              patch.object(add_showrunner, "POLL_S", float(self.env["ADD_SHOWRUNNER_POLL_S"])),
              patch.object(add_showrunner, "time", SimpleNamespace(monotonic=lambda: elapsed,
                                                                  time=time.time, sleep=advance))):
            return invoke_cli(add_showrunner, lambda: add_showrunner.main(arguments), arguments,
                              self.env, cwd or Path.cwd(), production_state=True)

    def prepare_herdr(self, slug: str) -> None:
        """Fake herdr answers a launch of `slug`'s showrunner as a tab of the shared session, which has
        a boot unit; the agent prompt brings the session up. While the shared session is live, it holds
        a tab named `slug`."""
        sessions = [herdr.SessionEntry(name=path.name.removeprefix("live-"), running=True, default=False,
                    session_dir="/fixture", socket_path="/fixture/socket") for path in self.state.glob("live-*")]
        responses: dict[str, CommandResponse | list[CommandResponse]] = {
            "--session default session list --json": CommandResponse(code=0, stdout=json.dumps(
                herdr.SessionListResult(sessions=sessions))),
            f"--session {SHARED} status server": [CommandResponse(code=0, stdout="status: stopped\n"),
                                                 CommandResponse(code=0, stdout="status: running\n")],
            f"--user show -p FragmentPath --value herdr-{SHARED}.service": CommandResponse(
                code=0, stdout=f"{self.env['HOME']}/.config/systemd/user/herdr-{SHARED}.service\n"),
            f"--session {SHARED} workspace list": CommandResponse(
                code=0, stdout=json.dumps({"result": {"workspaces": []}})),
            f"--session {SHARED} pane list": CommandResponse(code=0, stdout=json.dumps({
                "result": {"panes": [{"pane_id": "w1:p1", "tab_id": "w1:t1", "label": slug}]}})),
            "default": CommandResponse(code=0, stdout=json.dumps({"result": {
                "root_pane": {"pane_id": "w1:p1"}, "tab": {"tab_id": "w1:t1"}, "tabs": []}})),
        }
        responses[f"--session {SHARED} agent get w1:p1"] = CommandResponse(code=0, stdout=json.dumps({
            "result": {"agent": {"agent_status": "idle", "interactive_ready": True}}}))
        responses[f"--session {SHARED} pane read w1:p1 --source visible --format ansi"] = CommandResponse(
            code=0, stdout="────────\n❯ \n────────")
        for suffix in ("", " resume"):
            prompt = f"/showrunner:produce {self.doc(slug)}{suffix}"
            responses[f"--session {SHARED} agent prompt w1:p1 {prompt}"] = CommandResponse(
                code=0, stdout="", write_files={**self.on_start, str(self.state / f"live-{SHARED}"): ""})
        responses.update(self.herdr_overrides)
        _ = Path(self.env["FAKE_HERDR_RESPONSES"]).write_text(json.dumps(responses))

    def events(self, command: str) -> list[dict[str, object]]:
        path = self.state / "events.jsonl"
        if not path.exists():
            return []
        records = [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()]
        return [record for record in records if record["command"] == command]

    def herdr_calls(self) -> list[list[str]]:
        path = Path(self.env["FAKE_HERDR_LOG"])
        return [cast(list[str], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def started(self) -> list[str]:
        return next(row for row in self.herdr_calls() if row[2:4] == ["agent", "start"])

    def opened(self) -> list[str]:
        return next(row for row in self.herdr_calls() if row[2:4] == ["workspace", "create"])

    def parent_of(self, slug: str) -> str:
        path = self.root / "showrunner" / slug / "parent"
        return path.read_text(encoding="utf-8").strip() if path.exists() else ""

    def refused(self, result: subprocess.CompletedProcess[str], cause: str) -> None:
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("add_showrunner:", result.stderr)
        self.assertIn(cause, result.stderr)
        self.assertFalse(any("workspace" in row or "server" in row or "agent" in row
                             for row in self.herdr_calls()))

    def named_account(self) -> Path:
        with patch.dict(os.environ, self.env, clear=True):
            notes = Path(self.env["AGENT_NOTES_DIR"])
            _ = (notes / "claude3.md").write_text("---\nlogin: target@example.invalid\n---\n")
            self.assertEqual(accounts.setup("claude3"), [])
            directory = accounts.account_dir("claude3")
            _ = (directory / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "target@example.invalid"}}))
            # The trust checks use a separate fixture, so selection does not expose any live home.
            _ = (directory / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "target@example.invalid"},
                "projects": {str(self.root): {"hasTrustDialogAccepted": True},
                             str(self.root / "kid-checkout"): {"hasTrustDialogAccepted": True}}}))
        _ = self.configure_groups(showrunners="claude3")
        return directory

    def resume_record(self) -> None:
        _ = (self.root / "sessions/resume.json").write_text(json.dumps({
            "pid": 991001,
            "sessionId": "abc-123",
            "name": "kid",
            "messagingSocketPath": "",
            "updatedAt": 1,
            "tmux": "",
        }))

    def flaky_registry(self, failures: int) -> None:
        """Once the started session registers its timer, the registry cannot be read for `failures` looks."""
        count = self.root / "registry-failures"
        _ = (self.root / "flaky_sessions.py").write_text(
            "import os, pathlib, runpy, sys\n"
            + f"count, started = pathlib.Path({str(count)!r}), pathlib.Path({str(self.state / 'live-agents')!r})\n"
            + "seen = int(count.read_text()) if count.exists() else 0\n"
            + f"if started.exists() and seen < {failures}:\n"
            + "    count.write_text(str(seen + 1))\n"
            + "    print('one or more registry files could not be read', file=sys.stderr)\n"
            + "    raise SystemExit(3)\n"
            + f"runpy.run_path({str(self.root / 'sessions.py')!r}, run_name='__main__')\n", encoding="utf-8")
        self.env["SHOWRUNNERS_SESSIONS"] = str(self.root / "flaky_sessions.py")
