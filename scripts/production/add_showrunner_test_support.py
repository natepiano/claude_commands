"""add_showrunner.py through its CLI, with fake herdr, the session records and the
message sender: nothing real starts and nothing real is sent."""
from __future__ import annotations

from scripts.shutdown import remote

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

from scripts.production.cli_fixture import invoke_cli
from scripts.accounts import accounts
from scripts.whoami.account_test_support import write_claude_account
from scripts.production import add_showrunner
from scripts.production import showrunners, showrunner_tree, staff
from scripts.herdr import herdr
from scripts.message import sessions
from scripts.herdr.fake_herdr import CommandResponse
from scripts.production.fake_unit_host import private_process_registry, confirm_tab_close, native_claude, process as write_process

SCRIPT = Path(__file__).with_name("add_showrunner.py")
SHARED = herdr.SHARED_SESSION
PARENT_SESSION = "1c1dbca4-3e25-5434-9285-b225246489a5"
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


from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

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
        _ = self.enterContext(private_process_registry())
        self.timer("top", PARENT_SESSION)
        _ = self.doc("kid")
        owner = staff.by_path("top")
        assert owner is not None
        _ = staff.update_current_session(owner.current_session, staff.native_id(PARENT_SESSION))
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
    if argv[2:4] == ["pane", "process-info"]:
        pid = int(os.environ["FAKE_CLAUDE_PID"])
        path = Path(os.environ["FAKE_HERDR_RESPONSES"])
        responses = json.loads(path.read_text())
        pane_shell = Path(os.environ["STUB_STATE"]) / "pane-shell"
        shell = int(pane_shell.read_text()) if pane_shell.exists() else 998040
        agent_stat = Path(os.environ["HERDR_PROC"]) / str(pid) / "stat"
        live = agent_stat.exists() and int(agent_stat.read_text().rsplit(")", 1)[1].split()[1]) == shell
        foreground = pid if live else shell
        command = "claude" if live else "zsh"
        responses[" ".join(argv)] = {"code": 0, "stdout": json.dumps({"result": {"process_info": {
            "shell_pid": shell, "foreground_process_group_id": foreground,
            "foreground_processes": [{"pid": foreground, "name": command, "argv": [command]}]}}})}
        from scripts.lib.atomic_file import write_text_atomic
        write_text_atomic(path, json.dumps(responses))
    result = main(argv)
    if result == 0 and argv[2:4] == ["workspace", "create"]:
        sys.path.insert(0, os.environ["FIXTURE_CHECKOUT"])
        from scripts.production.fake_unit_host import process
        from scripts.lib.atomic_file import write_text_atomic
        root = Path(os.environ["HERDR_PROC"])
        shell = max(int(item.name) for item in root.iterdir() if item.name.isdecimal()) + 1
        process(os.environ, shell, 1, ["zsh"], {"HERDR_SESSION": argv[1], "HERDR_PANE_ID": "w1:p1"})
        write_text_atomic(Path(os.environ["STUB_STATE"]) / "pane-shell", str(shell))
    if result == 0 and argv[2:4] == ["agent", "start"]:
        sys.path.insert(0, os.environ["FIXTURE_CHECKOUT"])
        from scripts.production.staff_test_support import record_fixture_claude_start
        from scripts.production.fake_unit_host import private_process_registry, process
        from scripts.message import sessions
        pid = int(os.environ["FAKE_CLAUDE_PID"])
        shell = int((Path(os.environ["STUB_STATE"]) / "pane-shell").read_text())
        process(os.environ, pid, shell, ["claude"],
                {"HERDR_SESSION": argv[1], "HERDR_PANE_ID": argv[argv.index("--pane") + 1]},
                started=sessions.process_start(pid) or "1")
        with private_process_registry():
            record_fixture_claude_start(argv)
    if result == 0 and argv[2:4] == ["agent", "prompt"]:
        from datetime import datetime, timezone
        calls = [json.loads(line) for line in Path(os.environ["FAKE_HERDR_LOG"]).read_text().splitlines()]
        start = next(row for row in reversed(calls) if row[:2] == argv[:2] and row[2:4] == ["agent", "start"])
        session_id = json.loads((Path(os.environ["NOTIFIER_SESSIONS_DIR"]) /
                                (os.environ["FAKE_CLAUDE_PID"] + ".json")).read_text())["sessionId"]
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
                    "CODEX_THREAD_ID": PARENT_SESSION,
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
        with patch.dict(os.environ, self.env):
            write_claude_account("claude2", "main@example.invalid", 1, datetime.now(timezone.utc) + timedelta(days=2))
        _ = self.configure_groups()

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

    def doc(self, production_name: str) -> Path:
        path = self.root / f"{production_name}-production.md"
        if not path.exists():
            (self.root / f"{production_name}-checkout").mkdir()
            _ = path.write_text("\n".join([
                f"# {production_name}", "", f"- **Merge branch:** `{production_name}`",
                f"- **Showrunner checkout:** `{self.root / f'{production_name}-checkout'}`",
                f"- **Log:** `docs/plans/{production_name}-log.md`", "- **User zone:** America/Los_Angeles",
                "- **Updates:** every 15 minutes", ""]),
                encoding="utf-8")
        return path

    def timer_conf(self, production_name: str) -> Path:
        _ = self.doc(production_name)
        row = staff.by_path(production_name)
        identity = row.id if row is not None else self.env["FAKE_CLAUDE_SESSION_ID"]
        return self.root / "notifier" / f"showrunner-{identity}" / "conf"

    def timer_text(self, production_name: str, session: str) -> str:
        return f"TARGET=session:{session}\nCHECK=zsh /x/production_check.sh {production_name}\n"

    def timer(self, production_name: str, session: str) -> None:
        _ = fixture_member("showrunner", name=production_name, runtime_id=session,
                           doc=staff.LiveDoc(remote.local_machine(), self.doc(production_name)))
        self.timer_conf(production_name).parent.mkdir(parents=True, exist_ok=True)
        _ = self.timer_conf(production_name).write_text(self.timer_text(production_name, session), encoding="utf-8")

    def close_processes(self) -> None:
        for process in self.processes:
            process.terminate()
            _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
            if process.stdin is not None:
                process.stdin.close()

    def record(self, session: str, name: str = "") -> tuple[str, str]:
        """Create a private record for a live session."""
        process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        native_claude(self.env, process.pid, kernel_process=True)
        return (str(self.root / "sessions" / f"{process.pid}.json"),
                json.dumps({"pid": process.pid, "procStart": sessions.process_start(process.pid),
                            "name": name or f"{session}-name", "sessionId": session, "updatedAt": 1,
                            "messagingSocketPath": str(self.root / f"{session}.sock")}))

    def live(self, session: str, name: str = "") -> None:
        path, text = self.record(session, name)
        _ = Path(path).write_text(text, encoding="utf-8")

    def starts(self, session: str, *, registers: str = "") -> None:
        """The started session comes up as `session`, and registers `registers`'s update timer when named."""
        path, text = self.record(session, registers)
        self.env["FAKE_CLAUDE_PID"] = Path(path).stem
        self.env["FAKE_CLAUDE_SESSION_ID"] = session
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

    def run_command(self, production_name: str, *extra: str, cwd: Path | None = None,
                    subprocess_cli: bool = False) -> subprocess.CompletedProcess[str]:
        doc = self.doc(production_name)
        owner = staff.by_path(production_name)
        if "--resume" in extra and "--cwd" in extra and owner is None:
            owner = fixture_member("showrunner", name=production_name,
                                   runtime_id=extra[extra.index("--resume") + 1],
                                   doc=staff.LiveDoc(remote.local_machine(), doc))
        if owner is not None:
            _ = self.env.setdefault("FAKE_CLAUDE_SESSION_ID", owner.current_session)
        self.prepare_herdr(production_name)
        creation = ["--doc", str(doc)] if owner is None else []
        return self.invoke(["--production", production_name, *creation, "--model", "opus", "--effort", "high", *extra],
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

    def prepare_herdr(self, production_name: str) -> None:
        """Fake herdr answers a launch of `production_name`'s showrunner as a tab of the shared session, which has
        a boot unit; the agent prompt brings the session up. While the shared session is live, it holds
        a tab named `production_name`."""
        _ = self.env.setdefault("FAKE_CLAUDE_PID", "998041")
        native_claude(self.env, int(self.env["FAKE_CLAUDE_PID"]))
        _ = self.env.setdefault("FAKE_CLAUDE_SESSION_ID", fixture_runtime_id("showrunner::" + production_name))
        self.env["FIXTURE_CHECKOUT"] = str(SCRIPT.parents[2])
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
                "result": {"panes": [{"pane_id": "w1:p1", "tab_id": "w1:t1", "label": production_name}]}})),
            "default": CommandResponse(code=0, stdout=json.dumps({"result": {
                "root_pane": {"pane_id": "w1:p1"}, "tab": {"tab_id": "w1:t1"}, "tabs": []}})),
        }
        write_process(self.env, 998040, 1, ["zsh"], {"HERDR_SESSION": SHARED, "HERDR_PANE_ID": "w1:p1"})
        responses[f"--session {SHARED} agent get w1:p1"] = CommandResponse(code=0, stdout=json.dumps({
            "result": {"agent": {"agent_status": "idle"}}}))
        responses[f"--session {SHARED} pane read w1:p1 --source visible --format ansi"] = CommandResponse(
            code=0, stdout="────────\n❯ \n────────")
        for suffix in ("", " resume"):
            prompt = f"/showrunner:produce {self.doc(production_name)}{suffix}"
            responses[f"--session {SHARED} agent prompt w1:p1 {prompt}"] = CommandResponse(
                code=0, stdout="", write_files={**self.on_start, str(self.state / f"live-{SHARED}"): ""})
        responses.update(self.herdr_overrides)
        confirm_tab_close(responses, SHARED, "w1:t1", Path(self.env["FAKE_HERDR_RESPONSES"]))
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

    def parent_of(self, production_name: str) -> str:
        return showrunner_tree.parent_of(production_name)

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
            "pid": 991001, "procStart": "1",
            "sessionId": "cfc4564d-fed3-5803-9a4c-e3119733bb53",
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
