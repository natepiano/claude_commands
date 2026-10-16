"""Library specification tests using fake commands and a fake proc tree."""

from __future__ import annotations

import json
from collections.abc import Callable
import os
import subprocess
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast, override
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import herdr
import trust_survey
from fake_herdr import CommandResponse

GIT_BINARY = shutil.which("git") or "git"


class StartupClock:
    elapsed: float = 0.0


class WaitingAgentCLI:
    """A waiting CLI whose clock also advances the fake pane's processes."""

    def __init__(self, clock: StartupClock, ready_after: float, on_wait: Callable[[], None]) -> None:
        self.clock: StartupClock = clock
        self.ready_after: float = ready_after
        self.on_wait: Callable[[], None] = on_wait
        self.returncode: int | None = None  # Popen's external running-process state.
        self.killed: bool = False

    def __enter__(self) -> WaitingAgentCLI:
        return self

    def __exit__(self, kind: object, value: object, traceback: object) -> None:
        pass

    def poll(self) -> int | None:
        if self.clock.elapsed >= self.ready_after:
            self.returncode = 0
        return self.returncode

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        if timeout is not None:
            self.clock.elapsed += timeout
            self.on_wait()
        if self.poll() is None:
            if timeout is None:
                raise AssertionError("unbounded collection of a running CLI")
            raise subprocess.TimeoutExpired(["fake agent start"], timeout)
        return "CLI completed", ""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class HerdrTests(unittest.TestCase):
    root: Path = Path()
    fake: Path = Path()
    log: Path = Path()
    responses_path: Path = Path()
    responses: dict[str, CommandResponse | list[CommandResponse]] = {}
    proc: Path = Path()
    @property
    def herdr(self) -> herdr.Herdr:
        return herdr.Herdr("fixture")

    @override
    def setUp(self) -> None:
        _ = cast(object, self.enterContext(patch.object(herdr, "_PLATFORM", "linux")))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.fake = self.root / "fake-herdr"
        source = Path(__file__).with_name("fake_herdr.py").read_text()
        _ = self.fake.write_text("#!" + sys.executable + "\n" + source.split("\n", 1)[1])
        self.fake.chmod(0o755)
        self.log = self.root / "log"
        self.responses_path = self.root / "responses.json"
        self.responses = {"default": {"stdout": "", "code": 0}}
        self.proc = self.root / "proc"
        self.proc.mkdir()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HERDR_BIN": str(self.fake), "SYSTEMD_RUN": str(self.fake), "SYSTEMCTL": str(self.fake),
            "HERDR_PROC": str(self.proc), "FAKE_HERDR_LOG": str(self.log),
            "FAKE_HERDR_RESPONSES": str(self.responses_path), "PATH": str(self.root / "no-tools"),
            "HOME": str(self.root / "home"), "XDG_CONFIG_HOME": str(self.root / "config"),
        }, clear=True)))
        self.configure([], "")
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": []}}))

    def configure(self, arguments: list[str], stdout: str = "", code: int = 0, stderr: str = "") -> None:
        self.responses[" ".join(arguments)] = {"stdout": stdout, "code": code, "stderr": stderr}
        _ = self.responses_path.write_text(json.dumps(self.responses))

    def commands(self) -> list[list[str]]:
        return [cast(list[str], json.loads(line)) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def start_commands(self) -> list[list[str]]:
        """Startup commands, excluding read-only lifecycle observations."""
        return [command for command in self.commands()
                if command[2:4] not in (["agent", "get"], ["pane", "process-info"])]

    def configure_herdr(self, arguments: list[str], stdout: str = "", code: int = 0, stderr: str = "") -> None:
        self.configure(["--session", "fixture", *arguments], stdout, code, stderr)

    def process(self, pid: int, parent: int, arguments: list[str], environment: dict[str, str]) -> Path:
        process = self.proc / str(pid)
        process.mkdir()
        _ = process.joinpath("stat").write_text(f"{pid} (fixture with ) parenthesis) S {parent} 0 0\n")
        _ = process.joinpath("cmdline").write_bytes("\0".join([*arguments, ""]).encode())
        _ = process.joinpath("environ").write_bytes("\0".join([*(f"{key}={value}" for key, value in environment.items()), ""]).encode())
        process.joinpath("cwd").symlink_to(self.root)
        return process

    def unix_socket(self, path: Path, inode: int, *, listening: bool = True) -> None:
        table = self.proc / "net" / "unix"
        table.parent.mkdir(exist_ok=True)
        flags = "00010000" if listening else "00000000"
        if not table.exists():
            _ = table.write_text("Num RefCount Protocol Flags Type St Inode Path\n")
        with table.open("a") as output:
            _ = output.write(f"0000000000000000: 00000002 00000000 {flags} 0001 01 {inode} {path}\n")

    def socket_fd(self, process: Path, inode: int, *, fd: int = 3) -> None:
        directory = process / "fd"
        directory.mkdir(exist_ok=True)
        directory.joinpath(str(fd)).symlink_to(f"socket:[{inode}]")

    def darwin(self) -> None:
        _ = cast(object, self.enterContext(patch.object(herdr, "_PLATFORM", "darwin")))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "LAUNCHCTL": str(self.fake), "HERDR_PS": str(self.fake), "HERDR_LSOF": str(self.fake),
        })))

    def darwin_environment(self, pid: int, command: str, environment: str) -> None:
        self.configure(["-o", "command=", "-p", str(pid)], command + "\n")
        self.configure(["eww", "-o", "command=", "-p", str(pid)], command + " " + environment + "\n")

    def darwin_sockets(self, text: str) -> None:
        self.configure(["-n", "-P", "-a", "-U", "-u", str(os.getuid()), "-F", "pfn"], text)

    def test_darwin_starts_clear_restored_workspaces_only_after_launchd_owns_them(self) -> None:
        self.darwin()
        for start in (herdr.start_server, herdr.start_new_server):
            with self.subTest(start=start.__name__):
                self.log.unlink(missing_ok=True)
                self.responses["--session fixture status server"] = [
                    {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0}]
                self.responses["list herdr-fixture.service"] = ([
                    {"stdout": "", "code": 1}, {"stdout": "", "code": 0}]
                    if start is herdr.start_new_server else {"stdout": "", "code": 0})
                self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [
                    {"workspace_id": "w1"}, {"workspace_id": "w2"}]}}))
                outcome = start("fixture", 10_000)
                if start is herdr.start_server:
                    self.assertIsInstance(outcome, herdr.ServerCreated)
                calls = self.commands()
                submission = ["submit", "-l", "herdr-fixture.service", "--", str(self.fake), "--session", "fixture", "server"]
                self.assertIn(submission, calls)
                self.assertEqual(calls[-4:], [
                    ["list", "herdr-fixture.service"], ["--session", "fixture", "workspace", "list"],
                    ["--session", "fixture", "workspace", "close", "w1"],
                    ["--session", "fixture", "workspace", "close", "w2"]])
                self.assertFalse(any("--user" in call for call in calls))

    def test_darwin_adopts_an_answering_server_without_touching_launchd_or_layout(self) -> None:
        self.darwin()
        self.configure_herdr(["status", "server"], "status: running\n")
        self.assertIsInstance(herdr.start_server("fixture", 10_000), herdr.ServerAdopted)
        self.assertEqual(self.commands(), [["--session", "fixture", "status", "server"]])

    def test_darwin_exclusive_start_refuses_a_label_even_without_an_answering_socket(self) -> None:
        self.darwin()
        self.configure_herdr(["status", "server"], "status: stopped\n")
        self.configure(["list", "herdr-fixture.service"], "held label\n")
        with self.assertRaises(herdr.SessionTaken):
            herdr.start_new_server("fixture", 10_000)
        self.assertEqual(self.commands(), [["--session", "fixture", "status", "server"], ["list", "herdr-fixture.service"]])

    def test_darwin_submit_race_reports_a_taken_label_without_clearing_it(self) -> None:
        self.darwin()
        self.configure_herdr(["status", "server"], "status: stopped\n")
        self.responses["list herdr-fixture.service"] = [{"stdout": "", "code": 1}, {"stdout": "held", "code": 0}]
        self.configure(["submit", "-l", "herdr-fixture.service", "--", str(self.fake), "--session", "fixture", "server"],
                       code=1, stderr="launchd submission refused")
        with self.assertRaises(herdr.SessionTaken):
            herdr.start_new_server("fixture", 10_000)
        self.assertFalse(any("workspace" in call for call in self.commands()))

    def test_darwin_answering_server_with_no_owned_label_is_never_cleared(self) -> None:
        self.darwin()
        self.configure(["list", "herdr-fixture.service"], code=1)
        for start in (herdr.start_server, herdr.start_new_server):
            with self.subTest(start=start.__name__):
                self.log.unlink(missing_ok=True)
                self.responses["--session fixture status server"] = [
                    {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0}]
                _ = self.responses_path.write_text(json.dumps(self.responses))
                if start is herdr.start_new_server:
                    with self.assertRaises(herdr.SessionTaken):
                        _ = start("fixture", 10_000)
                else:
                    self.assertIsInstance(start("fixture", 10_000), herdr.ServerAdopted)
                self.assertFalse(any("workspace" in call for call in self.commands()))

    def test_darwin_stop_removes_only_the_named_launchd_label(self) -> None:
        self.darwin()
        herdr.stop_session("foo.service")
        self.assertEqual(self.commands(), [["--session", "foo.service", "server", "stop"],
            ["list", "herdr-foo.service.service"], ["remove", "herdr-foo.service.service"]])

    def test_darwin_environment_uses_only_the_suffix_after_unchanged_arguments(self) -> None:
        self.darwin()
        command = 'claude --prompt "HERDR_PANE_ID=forged CLAUDE_CONFIG_DIR=/wrong"'
        self.darwin_environment(42, command, "HERDR_PANE_ID=w1:p1 CLAUDE_CONFIG_DIR=/real account VALUE=a=b")
        self.assertEqual(herdr.darwin_process_environment(42), {
            "HERDR_PANE_ID": "w1:p1", "CLAUDE_CONFIG_DIR": "/real account", "VALUE": "a=b"})
        self.darwin_environment(43, command, "")
        self.assertEqual(herdr.darwin_process_environment(43), {})

    def test_darwin_environment_refuses_a_changed_command_or_failed_ps(self) -> None:
        self.darwin()
        self.darwin_environment(42, "claude", "HERDR_PANE_ID=w1:p1")
        self.responses["-o command= -p 42"] = [
            {"stdout": "claude\n", "code": 0}, {"stdout": "claude HERDR_PANE_ID=forged\n", "code": 0}]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        with self.assertRaisesRegex(herdr.HerdrFailure, "stable command"):
            _ = herdr.darwin_process_environment(42)
        self.configure(["-o", "command=", "-p", "42"], code=1, stderr="vanished")
        with self.assertRaisesRegex(herdr.HerdrFailure, "vanished"):
            _ = herdr.darwin_process_environment(42)

    def test_darwin_server_environment_uses_the_exact_listener_not_a_client(self) -> None:
        self.darwin()
        for name, directory in (("fixture", self.root / "config/herdr/sessions/fixture"),
                                ("default", self.root / "config/herdr")):
            with self.subTest(name=name):
                self.darwin_sockets(f"p70\nf3\nn{directory}/herdr.sock\n" +
                                    "p71\nf4\nn->0x1234\n")
                self.darwin_environment(70, "herdr server", "ACCOUNT=kept CLAUDE_CONFIG_DIR=/named account")
                self.darwin_environment(71, "herdr session attach fixture", "ACCOUNT=wrong")
                self.assertEqual(herdr.server_environment(name), {"ACCOUNT": "kept", "CLAUDE_CONFIG_DIR": "/named account"})
                self.assertNotIn(["eww", "-o", "command=", "-p", "71"], self.commands())

    def test_darwin_server_environment_refuses_missing_ambiguous_or_unreadable_holders(self) -> None:
        self.darwin()
        path = self.root / "config/herdr/sessions/fixture/herdr.sock"
        for sockets in ("", f"p70\nf3\nn{path}\np71\nf3\nn{path}\n"):
            with self.subTest(sockets=sockets):
                self.darwin_sockets(sockets)
                with self.assertRaisesRegex(herdr.HerdrFailure, "session fixture.*listening socket holder"):
                    _ = herdr.server_environment("fixture")
        self.darwin_sockets(f"p70\nf3\nn{path}\n")
        self.configure(["-o", "command=", "-p", "70"], code=1, stderr="environment unavailable")
        with self.assertRaisesRegex(herdr.HerdrFailure, "session fixture.*environment unavailable"):
            _ = herdr.server_environment("fixture")

    def test_darwin_process_discovery_uses_sockets_uid_cwd_and_real_environment(self) -> None:
        self.darwin()
        uid = os.getuid()
        self.configure(["-U", str(uid), "-o", "pid=,ppid=,uid=,command="],
            f"70 1 {uid} herdr server\n71 70 {uid} zsh\n72 71 {uid} claude\n" +
            f"73 71 {uid + 1} claude\n74 71 {uid} claude HERDR_PANE_ID=forged\n" +
            f"75 71 {uid} codex\n76 71 {uid} claude\n77 1 {uid} unrelated\n")
        self.darwin_sockets(f"p70\nf3\nn{self.root}/config/herdr/sessions/writing/herdr.sock\n")
        self.configure(["--session", "writing", "pane", "list"], json.dumps({"result": {"panes": []}}))
        self.darwin_environment(71, "zsh", "HERDR_PANE_ID=w1:p1 SHOWRUNNER_UNIT=prod SHOWRUNNER_UNIT_ID=unit")
        self.darwin_environment(72, "claude", "HERDR_PANE_ID=w1:p1")
        self.darwin_environment(74, "claude HERDR_PANE_ID=forged", "")
        self.configure(["-o", "command=", "-p", "75"], code=1, stderr="vanished")
        self.darwin_environment(76, "claude", "HERDR_PANE_ID=w1:p2")
        self.configure(["-a", "-p", "71", "-d", "cwd", "-Fn"], "p71\nfcwd\nn/work folder\n")
        self.configure(["-a", "-p", "72", "-d", "cwd", "-Fn"], "p72\nfcwd\nn/work folder\n")
        self.configure(["-a", "-p", "76", "-d", "cwd", "-Fn"], code=1, stderr="gone")
        rows = herdr.herdr_processes()
        self.assertCountEqual(rows, [
            herdr.HerdrProcess(71, herdr.HerdrPlace("writing", "w1:p1"), {"SHOWRUNNER_UNIT": "prod", "SHOWRUNNER_UNIT_ID": "unit"}, "zsh", "/work folder"),
            herdr.HerdrProcess(72, herdr.HerdrPlace("writing", "w1:p1"), {}, "claude", "/work folder"),
            herdr.HerdrProcess(76, herdr.HerdrPlace("writing", "w1:p2"), {}, "claude",
                               herdr.UnreadableCwd("process 76 cwd: gone"))])
        self.assertEqual(herdr.herdr_place_of(72), herdr.HerdrPlace("writing", "w1:p1"))
        self.assertEqual(herdr.herdr_place_of(74), herdr.NotInHerdr())
        for pid in (73, 77):
            self.assertNotIn(["eww", "-o", "command=", "-p", str(pid)], self.commands())

    def test_darwin_live_marked_process_remains_placed_when_cwd_is_unreadable(self) -> None:
        self.darwin()
        uid = os.getuid()
        self.configure(["-U", str(uid), "-o", "pid=,ppid=,uid=,command="],
                       f"70 1 {uid} herdr server\n71 70 {uid} zsh\n72 71 {uid} claude\n")
        self.darwin_sockets(f"p70\nf3\nn{self.root}/config/herdr/sessions/writing/herdr.sock\n")
        self.configure(["--session", "writing", "pane", "list"], json.dumps({"result": {"panes": []}}))
        self.darwin_environment(71, "zsh", "HERDR_PANE_ID=w1:p1 SHOWRUNNER_UNIT=prod SHOWRUNNER_UNIT_ID=unit")
        self.darwin_environment(72, "claude", "HERDR_PANE_ID=w1:p1")
        for output, code, error in (("", 1, "permission denied"), ("p72\nfcwd\n", 0, ""),
                                    ("n/one\nn/two\n", 0, "")):
            with self.subTest(output=output, code=code):
                for pid in (71, 72):
                    self.configure(["-a", "-p", str(pid), "-d", "cwd", "-Fn"], output, code, error)
                processes = {process.pid: process for process in herdr.herdr_processes()}
                self.assertCountEqual(processes, [71, 72])
                self.assertEqual(processes[71].marks, {"SHOWRUNNER_UNIT": "prod", "SHOWRUNNER_UNIT_ID": "unit"})
                for process in processes.values():
                    self.assertIsInstance(process.working_directory, herdr.UnreadableCwd)
                    self.assertEqual(process.place, herdr.HerdrPlace("writing", "w1:p1"))
                self.assertEqual(herdr.herdr_place_of(72), herdr.HerdrPlace("writing", "w1:p1"))

    def test_darwin_attached_clients_exclude_other_users_and_noninteractive_commands(self) -> None:
        self.darwin()
        uid = os.getuid()
        commands = ["herdr session attach writing", "herdr --session writing", "herdr",
                    "herdr --session writing server", "herdr --session writing pane read p1",
                    "herdr --session writing agent list", "herdr --session writing status", "herdr session attach other"]
        table = "".join(f"{80 + index} 1 {uid} {command}\n" for index, command in enumerate(commands))
        self.configure(["-U", str(uid), "-o", "pid=,ppid=,uid=,command="], table + f"99 1 {uid + 1} herdr session attach writing\n")
        self.darwin_environment(81, commands[1], "")
        self.darwin_environment(82, "herdr", "HERDR_SESSION=writing")
        self.assertEqual(herdr.attached_clients("writing"), herdr.ClientsAttached(3))
        self.assertEqual(herdr.attached_clients("missing"), herdr.NoClientAttached())
        for pid in (83, 84, 85, 86, 99):
            self.assertNotIn(["eww", "-o", "command=", "-p", str(pid)], self.commands())
        self.configure(["-U", str(uid), "-o", "pid=,ppid=,uid=,command="], code=1, stderr="cannot read processes")
        self.assertIsInstance(herdr.attached_clients("writing"), herdr.ClientsUnreadable)

    def test_darwin_handoff_finds_reparented_pane_shells_and_their_agents(self) -> None:
        self.darwin()
        uid = os.getuid()
        self.configure(["-U", str(uid), "-o", "pid=,ppid=,uid=,command="],
                       f"70 1 {uid} herdr server\n71 1 {uid} zsh\n72 71 {uid} claude\n")
        self.darwin_sockets(f"p70\nf3\nn{self.root}/config/herdr/sessions/writing/herdr.sock\n")
        self.configure(["--session", "writing", "pane", "list"],
                       json.dumps({"result": {"panes": [{"pane_id": "w1:p1", "label": "kept"}]}}))
        self.configure(["--session", "writing", "pane", "process-info", "--pane", "w1:p1"],
                       json.dumps({"result": {"process_info": {"shell_pid": 71}}}))
        for pid, command in ((71, "zsh"), (72, "claude")):
            self.darwin_environment(pid, command, "HERDR_PANE_ID=w1:p1")
            self.configure(["-a", "-p", str(pid), "-d", "cwd", "-Fn"], f"p{pid}\nfcwd\nn/work\n")
        self.assertCountEqual([row.pid for row in herdr.herdr_processes()], [71, 72])
        self.assertEqual(herdr.herdr_place_of(72), herdr.HerdrPlace("writing", "w1:p1"))

    def test_darwin_unreadable_socket_listing_refuses_server_environment(self) -> None:
        self.darwin()
        self.configure(["-n", "-P", "-a", "-U", "-u", str(os.getuid()), "-F", "pfn"],
                       code=1, stderr="socket inspection denied")
        with self.assertRaisesRegex(herdr.HerdrFailure, "session fixture.*socket inspection denied"):
            _ = herdr.server_environment("fixture")
        self.assertEqual(herdr.herdr_processes(), [])

    def test_binary_missing_and_override(self) -> None:
        self.assertEqual(herdr.herdr_binary(), str(self.fake))
        with patch.dict(os.environ, {"HERDR_BIN": ""}), patch.object(shutil, "which", return_value=None):
            with self.assertRaisesRegex(herdr.HerdrMissing, "herdr is not installed"):
                _ = herdr.herdr_binary()
        with patch.dict(os.environ, {"HERDR_BIN": ""}), patch.object(shutil, "which", return_value="/fixture/herdr"):
            self.assertEqual(herdr.herdr_binary(), "/fixture/herdr")

    def test_argv_and_refusal_preserve_code_and_message(self) -> None:
        self.assertEqual(self.herdr.argv("pane", "list"), [str(self.fake), "--session", "fixture", "pane", "list"])
        self.configure_herdr(["pane", "list"], code=1, stderr=json.dumps({"error": {"code": "not_found", "message": "missing pane"}}))
        with self.assertRaisesRegex(herdr.HerdrFailure, "not_found: missing pane") as raised:
            _ = self.herdr.panes()
        self.assertEqual(raised.exception.code, "not_found")

    def test_invalid_json_is_a_library_failure(self) -> None:
        self.configure_herdr(["pane", "list"], "[]")
        with self.assertRaises(herdr.HerdrFailure):
            _ = self.herdr.panes()

    def test_session_list_keeps_running_only(self) -> None:
        rows: list[herdr.SessionEntry] = [
            {"name": "live", "running": True, "default": False, "session_dir": "/live", "socket_path": "/live/socket"},
            {"name": "stopped", "running": False, "default": True, "session_dir": "/stopped", "socket_path": "/stopped/socket"},
        ]
        self.configure(["--session", "default", "session", "list", "--json"], json.dumps({"sessions": rows}))
        self.assertEqual(herdr.live_sessions(), rows[:1])

    def test_server_start_skips_running_server(self) -> None:
        self.configure_herdr(["status", "server"], "server:\nstatus: running\n")
        self.configure_herdr(["workspace", "list"], json.dumps({
            "result": {"workspaces": [{"workspace_id": "w1"}]},
        }))
        outcome = herdr.start_server("fixture", 10_000)
        self.assertIsInstance(outcome, herdr.ServerAdopted)
        self.assertEqual(self.commands(), [["--session", "fixture", "status", "server"]])

    def test_new_server_refuses_a_running_destination_without_starting_a_unit(self) -> None:
        self.configure_herdr(["status", "server"], "status: running\n")
        with self.assertRaises(herdr.SessionTaken) as raised:
            herdr.start_new_server("fixture", 10_000)
        self.assertEqual(raised.exception.name, "fixture")
        self.assertEqual(self.commands(), [["--session", "fixture", "status", "server"]])

    def test_new_server_refuses_a_unit_taken_after_the_status_probe(self) -> None:
        self.configure_herdr(["status", "server"], "status: stopped\n")
        creation = ["--user", "--unit=herdr-fixture.service", "--collect", str(self.fake),
                    "--session", "fixture", "server"]
        self.configure(creation, code=1, stderr="Unit herdr-fixture.service was already loaded or has a fragment file.")
        with self.assertRaises(herdr.SessionTaken):
            herdr.start_new_server("fixture", 10_000)
        self.assertEqual(self.commands(), [["--session", "fixture", "status", "server"], creation])

    def test_new_server_does_not_poll_after_an_unrelated_unit_creation_failure(self) -> None:
        self.configure_herdr(["status", "server"], "status: stopped\n")
        creation = ["--user", "--unit=herdr-fixture.service", "--collect", str(self.fake),
                    "--session", "fixture", "server"]
        self.configure(creation, code=1, stderr="fixture manager unavailable")
        with self.assertRaisesRegex(herdr.HerdrFailure, "fixture manager unavailable"):
            herdr.start_new_server("fixture", 10_000)
        self.assertEqual(self.commands(), [["--session", "fixture", "status", "server"], creation])

    def test_new_server_waits_for_its_exclusively_created_unit_to_answer(self) -> None:
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0},
        ]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        herdr.start_new_server("fixture", 10_000)
        self.assertEqual(self.commands(), [
            ["--session", "fixture", "status", "server"],
            ["--user", "--unit=herdr-fixture.service", "--collect", str(self.fake), "--session", "fixture", "server"],
            ["--session", "fixture", "status", "server"],
            ["--user", "is-active", "herdr-fixture.service"],
            ["--session", "fixture", "workspace", "list"],
        ])

    def test_server_start_deadline(self) -> None:
        self.configure_herdr(["status", "server"], "status: stopped\n")
        with self.assertRaisesRegex(herdr.HerdrFailure, "server fixture not answering after 0 ms"):
            _ = herdr.start_server("fixture", 0)
        self.assertEqual(self.commands()[-1], ["--user", "--unit=herdr-fixture.service", "--collect", str(self.fake), "--session", "fixture", "server"])

    def test_server_start_polls_after_launch(self) -> None:
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0},
        ]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        outcome = herdr.start_server("fixture", 10_000)
        self.assertIsInstance(outcome, herdr.ServerCreated)
        self.assertEqual(self.commands(), [
            ["--session", "fixture", "status", "server"],
            ["--user", "--unit=herdr-fixture.service", "--collect", str(self.fake), "--session", "fixture", "server"],
            ["--session", "fixture", "status", "server"],
            ["--user", "is-active", "herdr-fixture.service"],
            ["--session", "fixture", "workspace", "list"],
        ])

    def test_spawned_servers_close_every_restored_workspace_before_returning(self) -> None:
        self.configure_herdr(["workspace", "list"], json.dumps({
            "id": "cli:workspace:list", "result": {"type": "workspace_list", "workspaces": [
                {"workspace_id": "w1", "focused": True, "active_tab_id": "w1:t1",
                 "label": "fixture", "agent_status": "unknown", "number": 1,
                 "pane_count": 1, "tab_count": 1},
                {"workspace_id": "w2", "focused": False},
            ]},
        }))
        for start in (herdr.start_server, herdr.start_new_server):
            with self.subTest(start=start.__name__):
                self.log.unlink(missing_ok=True)
                self.responses["--session fixture status server"] = [
                    {"stdout": "status: stopped\n", "code": 0},
                    {"stdout": "status: running\n", "code": 0},
                ]
                _ = self.responses_path.write_text(json.dumps(self.responses))
                _ = start("fixture", 10_000)
                self.assertEqual(self.commands(), [
                    ["--session", "fixture", "status", "server"],
                    ["--user", "--unit=herdr-fixture.service", "--collect", str(self.fake), "--session", "fixture", "server"],
                    ["--session", "fixture", "status", "server"],
                    ["--user", "is-active", "herdr-fixture.service"],
                    ["--session", "fixture", "workspace", "list"],
                    ["--session", "fixture", "workspace", "close", "w1"],
                    ["--session", "fixture", "workspace", "close", "w2"],
                ])

    def test_spawned_servers_report_session_and_workspace_when_close_fails(self) -> None:
        self.configure_herdr(["workspace", "list"], json.dumps({
            "result": {"workspaces": [{"workspace_id": "w1"}]},
        }))
        self.configure_herdr(["workspace", "close", "w1"], code=1, stderr="fixture close refused")
        for start in (herdr.start_server, herdr.start_new_server):
            with self.subTest(start=start.__name__):
                self.log.unlink(missing_ok=True)
                self.responses["--session fixture status server"] = [
                    {"stdout": "status: stopped\n", "code": 0},
                    {"stdout": "status: running\n", "code": 0},
                ]
                _ = self.responses_path.write_text(json.dumps(self.responses))
                with self.assertRaisesRegex(herdr.HerdrFailure, "session fixture:.*workspace w1:.*fixture close refused"):
                    _ = start("fixture", 10_000)
                self.assertEqual(self.commands()[-1], ["--session", "fixture", "workspace", "close", "w1"])

    def test_answering_server_with_inactive_spawned_unit_is_never_cleared(self) -> None:
        self.configure(["--user", "is-active", "herdr-fixture.service"], "inactive\n", code=3)
        self.configure_herdr(["workspace", "list"], json.dumps({
            "result": {"workspaces": [{"workspace_id": "w1"}]},
        }))
        for start in (herdr.start_server, herdr.start_new_server):
            with self.subTest(start=start.__name__):
                self.log.unlink(missing_ok=True)
                self.responses["--session fixture status server"] = [
                    {"stdout": "status: stopped\n", "code": 0},
                    {"stdout": "status: running\n", "code": 0},
                ]
                _ = self.responses_path.write_text(json.dumps(self.responses))
                if start is herdr.start_new_server:
                    with self.assertRaises(herdr.SessionTaken) as raised:
                        _ = start("fixture", 10_000)
                    self.assertEqual(raised.exception.name, "fixture")
                else:
                    outcome = start("fixture", 10_000)
                    self.assertIsInstance(outcome, herdr.ServerAdopted)
                self.assertEqual(self.commands(), [
                    ["--session", "fixture", "status", "server"],
                    ["--user", "--unit=herdr-fixture.service", "--collect", str(self.fake), "--session", "fixture", "server"],
                    ["--session", "fixture", "status", "server"],
                    ["--user", "is-active", "herdr-fixture.service"],
                ])

    def test_stop_stops_only_named_active_unit(self) -> None:
        herdr.stop_session("fixture")
        self.assertEqual(self.commands(), [
            ["--session", "fixture", "server", "stop"], ["--user", "is-active", "herdr-fixture.service"],
            ["--user", "stop", "herdr-fixture.service"],
        ])

    def test_stop_checks_unit_after_server_refusal(self) -> None:
        self.configure_herdr(["server", "stop"], code=1, stderr="server failed")
        with self.assertRaises(herdr.HerdrFailure):
            herdr.stop_session("fixture")
        self.assertEqual(self.commands()[-1], ["--user", "stop", "herdr-fixture.service"])

    def test_open_pane_passes_environment_and_renames(self) -> None:
        arguments = ["workspace", "create", "--label", "worker", "--cwd", "/repo", "--env", "SHOWRUNNER_UNIT=prod", "--env", "CLAUDE_CONFIG_DIR=/account", "--env", "HERDR_AGENT_PANE=1", "--no-focus"]
        self.configure_herdr(arguments, json.dumps({"result": {"root_pane": {"pane_id": "pane-1"}}}))
        self.assertEqual(herdr.open_pane("fixture", "worker", "/repo", {"SHOWRUNNER_UNIT": "prod", "CLAUDE_CONFIG_DIR": "/account"}), "pane-1")
        self.assertEqual(self.commands(), [["--session", "fixture", *arguments], ["--session", "fixture", "pane", "rename", "pane-1", "worker"]])

    def test_promptless_start_never_sends_input(self) -> None:
        self.herdr.start_agent("worker", "pane-1", "claude", ["-n", "solo"], herdr.NoOpeningPrompt(), 90_000)
        self.assertEqual(self.start_commands(), [["--session", "fixture", "agent", "start", "worker", "--kind",
                                           "claude", "--pane", "pane-1", "--timeout", "90000", "--", "-n", "solo"]])

    def test_session_name_contract_refuses_before_external_commands(self) -> None:
        operations: tuple[Callable[[str], object], ...] = (
            herdr.server_unit, herdr.stop_session,
            lambda name: herdr.start_server(name, 10_000),
            lambda name: herdr.start_new_server(name, 10_000),
        )
        for name in ("", "a b", ".", "..", "a" * 65, "é", "a/b"):
            for operation in operations:
                with self.subTest(name=name, operation=operation):
                    with self.assertRaisesRegex(ValueError, "ASCII.*never"):
                        _ = operation(name)
        self.assertEqual(self.commands(), [])

    def test_valid_name_starts_and_stops_its_exact_unit(self) -> None:
        self.configure(["--session", "a-b", "status", "server"], "status: stopped")
        with self.assertRaises(herdr.HerdrFailure):
            _ = herdr.start_server("a-b", 0)
        herdr.stop_session("a-b")
        commands = self.commands()
        self.assertIn(["--user", "--unit=herdr-a-b.service", "--collect", str(self.fake), "--session", "a-b", "server"], commands)
        self.assertIn(["--user", "stop", "herdr-a-b.service"], commands)
        self.assertTrue(all("herdr-a b" not in part for command in commands for part in command))

    def test_service_suffix_in_a_session_name_never_aliases_another_unit(self) -> None:
        for name in ("foo", "foo.service"):
            self.configure(["--session", name, "status", "server"], "status: stopped")
            with self.assertRaises(herdr.HerdrFailure):
                herdr.start_new_server(name, 0)
        self.assertEqual([command[1] for command in self.commands() if command[0] == "--user"],
                         ["--unit=herdr-foo.service", "--unit=herdr-foo.service.service"])
        self.log.unlink()
        herdr.stop_session("foo.service")
        self.assertEqual(self.commands(), [
            ["--session", "foo.service", "server", "stop"],
            ["--user", "is-active", "herdr-foo.service.service"],
            ["--user", "stop", "herdr-foo.service.service"],
        ])

    def test_generated_name_fits_suffix_and_launches_under_the_contract(self) -> None:
        for text, suffix in (("space é" * 20, "123"), (".", ""), ("..", ""), ("", "")):
            name = herdr.generated_session_name(text, suffix)
            self.configure(["--session", name, "status", "server"], "status: running")
            _ = herdr.start_server(name, 10_000)
            self.assertLessEqual(len(name.encode("ascii")), 64)
            if suffix:
                self.assertTrue(name.endswith("-123"))
            self.assertEqual(self.commands()[-1], ["--session", name, "status", "server"])

    def test_attached_clients_count_only_interactive_clients_for_the_session(self) -> None:
        clients = [["herdr", "session", "attach", "fixture"], ["herdr", "--session", "fixture"],
                   ["herdr", "--session=fixture"], ["herdr", "--session", "other", "session", "attach", "fixture"]]
        excluded = [["herdr", "--session", "fixture", *command] for command in (
            ["server"], ["pane", "read", "p1"], ["agent", "list"], ["status"],
            ["status", "server"], ["workspace", "create"], ["session", "list"], ["--help"], ["--version"],
        )]
        excluded.extend([["herdr", "session", "attach", "other"], ["herdr", "--session", "other"],
                         ["echo", "herdr", "session", "attach", "fixture"]])
        for pid, argv in enumerate(clients + excluded, 100):
            _ = self.process(pid, 1, argv, {})
        self.assertEqual(herdr.attached_clients("fixture"), herdr.ClientsAttached(4))
        self.assertEqual(herdr.attached_clients("missing"), herdr.NoClientAttached())

    def test_attached_clients_handle_default_unreadable_and_vanished_processes(self) -> None:
        vanished = self.process(10, 1, ["herdr"], {})
        vanished.joinpath("cmdline").unlink()
        _ = self.process(11, 1, ["herdr"], {})
        self.assertEqual(herdr.attached_clients("default"), herdr.ClientsAttached(1))
        with patch.dict(os.environ, {"HERDR_PROC": str(self.root / "missing")}):
            self.assertIsInstance(herdr.attached_clients("default"), herdr.ClientsUnreadable)
        vanished.joinpath("cmdline").mkdir()
        self.assertIsInstance(herdr.attached_clients("default"), herdr.ClientsUnreadable)

    def test_bare_client_uses_environment_session_but_explicit_argv_wins(self) -> None:
        _ = self.process(10, 1, ["herdr"], {"HERDR_SESSION": "writing"})
        _ = self.process(11, 1, ["herdr", "--session", "other"], {"HERDR_SESSION": "writing"})
        excluded = [self.process(pid, 1, arguments, {"HERDR_SESSION": "writing"})
                    for pid, arguments in enumerate((["herdr", "server"], ["herdr", "agent", "list"],
                                                     ["claude"]), 20)]
        original = Path.read_bytes

        def read(path: Path) -> bytes:
            if path.name == "environ" and path.parent in excluded:
                raise AssertionError("non-client environment was opened")
            return original(path)

        with patch.object(Path, "read_bytes", read):
            self.assertEqual(herdr.attached_clients("writing"), herdr.ClientsAttached(1))
            self.assertEqual(herdr.attached_clients("other"), herdr.ClientsAttached(1))
            self.assertEqual(herdr.attached_clients("default"), herdr.NoClientAttached())

    def test_process_discovery_reads_environ_only_under_socket_owned_servers(self) -> None:
        server = self.process(10, 1, ["handoff-server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        _ = self.process(11, 10, ["zsh"], {})
        _ = self.process(12, 11, ["claude"], {"HERDR_PANE_ID": "p1"})
        unrelated = self.process(20, 1, ["claude"], {"HERDR_PANE_ID": "p2"})
        _ = (server / "cgroup").write_text("0::/user.slice/app.slice/herdr-fixture.service\n")
        _ = (unrelated / "cgroup").write_text("0::/user.slice/app.slice/herdr-other.service\n")
        original = Path.read_bytes
        opened: list[Path] = []

        def read(path: Path) -> bytes:
            opened.append(path)
            if path == unrelated / "environ":
                raise AssertionError("unrelated environment was opened")
            return original(path)

        with patch.object(Path, "read_bytes", read):
            rows = herdr.herdr_processes()
        self.assertEqual([(row.pid, row.place) for row in rows], [(12, herdr.HerdrPlace("fixture", "p1"))])
        self.assertIn(self.proc / "12/environ", opened)
        self.assertNotIn(server / "environ", opened)

    def test_handoff_survivors_keep_their_unit_after_reparenting(self) -> None:
        server = self.process(30, 1, ["herdr", "server", "--handoff-import", "/fixture/import", "token"], {})
        self.unix_socket(self.root / "config/herdr/sessions/writing/herdr.sock", 100)
        self.socket_fd(server, 100)
        shell = self.process(11, 1, ["zsh"], {"HERDR_PANE_ID": "p1"})
        claude = self.process(12, 11, ["claude"], {"HERDR_PANE_ID": "p1", "HERDR_SESSION": "stale",
                              "SHOWRUNNER_UNIT": "show", "SHOWRUNNER_UNIT_ID": "unit"})
        _ = self.process(5, 1, ["subreaper"], {})
        # setsid in the replacement does not change these inherited memberships.
        for membership in ("0::", "1:name=systemd:"):
            for process in (server, shell):
                _ = (process / "cgroup").write_text(f"{membership}/user.slice/app.slice/herdr-writing.service\n")
            _ = (claude / "cgroup").write_text(f"{membership}/user.slice/app.slice/herdr-writing.service/agents\n")
            for parent in (1, 5):
                _ = (shell / "stat").write_text(f"11 (zsh) S {parent} 0 0\n")
                with self.subTest(orphan_parent=parent, membership=membership):
                    rows = herdr.herdr_processes()
                    self.assertEqual({row.pid: row.place for row in rows},
                                     {11: herdr.HerdrPlace("writing", "p1"), 12: herdr.HerdrPlace("writing", "p1")})
                    self.assertEqual(next(row.marks for row in rows if row.pid == 12),
                                     {"SHOWRUNNER_UNIT": "show", "SHOWRUNNER_UNIT_ID": "unit"})
                    self.assertEqual(herdr.herdr_place_of(12), herdr.HerdrPlace("writing", "p1"))

    def test_shared_scope_handoff_uses_preserved_shells_without_reading_unrelated_environments(self) -> None:
        servers = [self.process(pid, 1, ["herdr", "server"], {}) for pid in (30, 40)]
        for server, name in zip(servers, ("writing", "other")):
            self.unix_socket(self.root / f"config/herdr/sessions/{name}/herdr.sock", int(server.name))
            self.socket_fd(server, int(server.name))
        shell = self.process(11, 1, ["zsh"], {"HERDR_PANE_ID": "w1:p1"})
        claude = self.process(12, 11, ["claude"], {"HERDR_PANE_ID": "w1:p1", "HERDR_SESSION": "stale",
                              "SHOWRUNNER_UNIT": "show", "SHOWRUNNER_UNIT_ID": "unit"})
        other_shell = self.process(21, 1, ["zsh"], {"HERDR_PANE_ID": "w1:p1"})
        unrelated = self.process(20, 1, ["unrelated"], {"HERDR_PANE_ID": "w1:p1"})
        _ = self.process(5, 1, ["subreaper"], {})
        for name, pid in (("writing", 11), ("other", 21)):
            self.configure(["--session", name, "pane", "list"],
                           json.dumps({"result": {"panes": [{"pane_id": "w1:p1"}]}}))
            self.configure(["--session", name, "pane", "process-info", "--pane", "w1:p1"],
                           json.dumps({"result": {"process_info": {"shell_pid": pid}}}))
        original = Path.read_bytes

        def read(path: Path) -> bytes:
            if path.name == "environ" and path.parent in (*servers, unrelated, self.proc / "5"):
                raise AssertionError("unrelated environment was opened")
            return original(path)

        for membership in ("0::", "1:name=systemd:"):
            for process in (*servers, shell, claude, other_shell, unrelated):
                _ = (process / "cgroup").write_text(f"{membership}/user.slice/session-7.scope\n")
            for parent in (1, 5):
                _ = (shell / "stat").write_text(f"11 (zsh) S {parent} 0 0\n")
                with self.subTest(parent=parent, membership=membership), patch.object(Path, "read_bytes", read):
                    rows = herdr.herdr_processes()
                    self.assertEqual({row.pid: row.place for row in rows}, {
                        11: herdr.HerdrPlace("writing", "w1:p1"),
                        12: herdr.HerdrPlace("writing", "w1:p1"),
                        21: herdr.HerdrPlace("other", "w1:p1"),
                    })
                    self.assertEqual(next(row.marks for row in rows if row.pid == 12),
                                     {"SHOWRUNNER_UNIT": "show", "SHOWRUNNER_UNIT_ID": "unit"})
                    self.assertEqual(herdr.herdr_place_of(12), herdr.HerdrPlace("writing", "w1:p1"))

    def test_agent_not_ready_reads_screen_without_input(self) -> None:
        arguments = ["agent", "start", "worker", "--kind", "claude", "--pane", "pane-1", "--timeout", "100", "--", "--model", "fixture-model"]
        self.configure_herdr(arguments, code=1, stderr=json.dumps({"error": {"code": "agent_not_ready", "message": "trust dialog"}}))
        self.configure_herdr(["pane", "read", "pane-1", "--source", "visible"], "Trust this folder?")
        with self.assertRaises(herdr.AgentNotReady) as raised:
            self.herdr.start_agent("worker", "pane-1", "claude", ["--model", "fixture-model"], herdr.OpeningPrompt("opening\nprompt"), 100)
        self.assertEqual(raised.exception.screen, "Trust this folder?")
        self.assertEqual(self.start_commands(), [["--session", "fixture", *arguments], ["--session", "fixture", "pane", "read", "pane-1", "--source", "visible"]])

    def startup_process(self, pid: int, arguments: list[str]) -> None:
        process = self.process(pid, 10 if pid != 10 else 1, arguments, {})
        fields = ["S", str(10 if pid != 10 else 1), str(pid), "10", "1", "10",
                  *(["0"] * 13), str(pid * 100)]
        _ = (process / "stat").write_text(f"{pid} (pane process) {' '.join(fields)}\n")
        task = process / "task" / str(pid)
        task.mkdir(parents=True)
        _ = (task / "children").write_text("")

    def startup_observation(self, arguments: list[str]) -> str:
        return json.dumps({"result": {"process_info": {
            "shell_pid": 10, "foreground_process_group_id": 10 if arguments == ["zsh"] else 11,
            "foreground_processes": [{"pid": 10 if arguments == ["zsh"] else 11,
                                      "name": arguments[0], "argv": arguments}],
        }}})

    def startup_watch(self, clock: StartupClock, ready_after: float, on_wait: Callable[[], None],
                      observation: Callable[[], str]) -> tuple[WaitingAgentCLI, list[tuple[float, float]]]:
        process = WaitingAgentCLI(clock, ready_after, on_wait)
        calls: list[tuple[float, float]] = []

        def run(argv: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
            calls.append((clock.elapsed, timeout))
            payload = observation()
            if argv[3:5] == ["agent", "get"]:
                payload = json.dumps({"result": {"agent": {
                    "name": "worker", "agent_status": "unknown", "launch_pending": True,
                }}})
            return subprocess.CompletedProcess(argv, 0, payload, "")

        _ = self.enterContext(patch.object(herdr, "run_program", run))
        _ = self.enterContext(patch.object(subprocess, "Popen", return_value=process))
        _ = self.enterContext(patch.object(herdr, "time", SimpleNamespace(monotonic=lambda: clock.elapsed)))
        return process, calls

    def test_start_detects_a_brief_agent_exit_while_registration_stays_pending(self) -> None:
        self.startup_process(10, ["zsh"])
        self.startup_process(11, ["claude", "--not-a-real-flag"])
        clock = StartupClock()
        children = self.proc / "10/task/10/children"

        def advance() -> None:
            _ = children.write_text("11" if 0.2 <= clock.elapsed < 0.3 else "")
            if clock.elapsed >= 0.3:
                shutil.rmtree(self.proc / "11", ignore_errors=True)

        process, calls = self.startup_watch(clock, 90, advance, lambda: self.startup_observation(["zsh"]))
        with self.assertRaisesRegex(herdr.AgentExited, "fixture:pane-1.*exited"):
            self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.NoOpeningPrompt(), 90_000)
        self.assertLess(clock.elapsed, 1)
        self.assertTrue(process.killed)
        self.assertEqual(len(calls), 1)

    def test_start_keeps_an_identified_pid_alive_after_exec_and_parent_change(self) -> None:
        self.startup_process(10, ["zsh"])
        self.startup_process(11, ["claude", "--resume", "missing-id"])
        children = self.proc / "10/task/10/children"
        clock = StartupClock()

        def advance() -> None:
            _ = children.write_text("11" if clock.elapsed < 0.3 else "")
            if clock.elapsed >= 0.3:
                # An exec changes argv/comm but preserves PID and start time.
                _ = (self.proc / "11/cmdline").write_bytes(b"/nix/store/runtime/bin/runtime\0")
                stat = (self.proc / "11/stat").read_text().replace("pane process", "runtime").replace("S 10 ", "S 1 ")
                _ = (self.proc / "11/stat").write_text(stat)

        process, calls = self.startup_watch(clock, 4.3, advance, lambda: self.startup_observation(["zsh"]))
        self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.NoOpeningPrompt(), 90_000)
        self.assertEqual(process.returncode, 0)
        self.assertFalse(process.killed)
        self.assertEqual(len(calls), 1)

    def test_start_keeps_a_wrapper_child_alive_after_its_parent_exits(self) -> None:
        self.startup_process(10, ["zsh"])
        self.startup_process(11, ["claude"])
        self.startup_process(12, ["/nix/store/runtime/bin/runtime"])
        stat = self.proc / "12/stat"
        _ = stat.write_text(stat.read_text().replace("S 10 ", "S 11 "))
        _ = (self.proc / "11/task/11/children").write_text("12")
        children = self.proc / "10/task/10/children"
        clock = StartupClock()

        def advance() -> None:
            _ = children.write_text("11" if clock.elapsed < 0.3 else "")
            if clock.elapsed >= 0.3:
                shutil.rmtree(self.proc / "11", ignore_errors=True)
                _ = stat.write_text(stat.read_text().replace("S 11 ", "S 1 "))

        process, calls = self.startup_watch(clock, 4.3, advance, lambda: self.startup_observation(["zsh"]))
        self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.NoOpeningPrompt(), 90_000)
        self.assertEqual(process.returncode, 0)
        self.assertFalse(process.killed)
        self.assertEqual(len(calls), 1)

    def test_start_detects_exit_of_an_agent_forked_by_another_shell_thread(self) -> None:
        self.startup_process(10, ["zsh"])
        self.startup_process(11, ["claude", "--not-a-real-flag"])
        task = self.proc / "10/task/20"
        task.mkdir()
        children = task / "children"
        clock = StartupClock()

        def advance() -> None:
            _ = children.write_text("11" if 0.2 <= clock.elapsed < 0.3 else "")
            if clock.elapsed >= 0.3:
                shutil.rmtree(self.proc / "11", ignore_errors=True)

        process, calls = self.startup_watch(clock, 90, advance, lambda: self.startup_observation(["zsh"]))
        with self.assertRaises(herdr.AgentExited):
            self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.NoOpeningPrompt(), 90_000)
        self.assertLess(clock.elapsed, 1)
        self.assertTrue(process.killed)
        self.assertEqual(len(calls), 1)

    def test_start_ignores_prompt_helpers_before_a_healthy_agent(self) -> None:
        for arguments in (["git", "status", "claude"], ["node", "prompt-helper.js", "claude"]):
            with self.subTest(helper=arguments):
                for pid in (10, 11):
                    shutil.rmtree(self.proc / str(pid), ignore_errors=True)
                self.startup_process(10, ["zsh"])
                self.startup_process(11, arguments)
                clock = StartupClock()
                children = self.proc / "10/task/10/children"

                def advance() -> None:
                    _ = children.write_text("11" if clock.elapsed < 0.2 or clock.elapsed >= 0.4 else "")
                    if clock.elapsed >= 0.4:
                        _ = (self.proc / "11/cmdline").write_bytes(b"claude\0")

                process, calls = self.startup_watch(clock, 1.5, advance, lambda: self.startup_observation(["zsh"]))
                self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.NoOpeningPrompt(), 90_000)
                self.assertFalse(process.killed)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(len(calls), 1)

    def test_start_uses_slow_herdr_observations_when_proc_is_unreadable(self) -> None:
        clock = StartupClock()

        def observation() -> str:
            return self.startup_observation(["claude"] if clock.elapsed >= 0.4 else ["zsh"])

        process, calls = self.startup_watch(clock, 4.3, lambda: None, observation)
        self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.NoOpeningPrompt(), 90_000)
        self.assertFalse(process.killed)
        self.assertLessEqual(len(calls), 1 + int(clock.elapsed * 2))
        self.assertTrue(all(later[0] - earlier[0] >= 0.5 for earlier, later in zip(calls, calls[1:])))

    def test_start_falls_back_to_process_info_to_confirm_shell_return(self) -> None:
        clock = StartupClock()

        def observation() -> str:
            return self.startup_observation(["claude"] if 0.4 <= clock.elapsed < 0.9 else ["zsh"])

        process, calls = self.startup_watch(clock, 90, lambda: None, observation)
        with self.assertRaises(herdr.AgentExited):
            self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.NoOpeningPrompt(), 90_000)
        self.assertLess(clock.elapsed, 2)
        self.assertTrue(process.killed)
        self.assertGreaterEqual(len(calls), 3)

    def test_start_collects_cli_completion_after_a_slow_observation_crosses_deadline(self) -> None:
        clock = StartupClock()

        def observation() -> str:
            if clock.elapsed > 0:
                clock.elapsed += 1.0
            return self.startup_observation(["zsh"])

        process, calls = self.startup_watch(clock, 0.7, lambda: None, observation)
        result = self.herdr.start_command(["fake-herdr", "agent", "start"], "claude", "pane-1", 1)
        self.assertEqual(result.stdout, "CLI completed")
        self.assertFalse(process.killed)
        self.assertGreater(clock.elapsed, 1)
        self.assertTrue(all(timeout <= 1 - called_at for called_at, timeout in calls))
        self.assertLess(calls[-1][1], 0.5)

    def test_start_does_not_treat_an_unreadable_observation_as_an_exit(self) -> None:
        source = self.fake.read_text().replace(
            '    responses = cast(',
            '    if argv[2:4] == ["agent", "start"]:\n'
            + '        import time\n'
            + '        time.sleep(0.3)\n'
            + '    responses = cast(',
        )
        _ = self.fake.write_text(source)
        self.configure_herdr(["pane", "process-info", "--pane", "pane-1"], code=1, stderr="unreadable")
        self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.OpeningPrompt("continue"), 90_000)
        self.assertTrue(any(command[2:4] == ["pane", "process-info"] for command in self.commands()))
        self.assertEqual(self.commands()[-1], ["--session", "fixture", "agent", "prompt", "pane-1", "continue"])

    def test_start_maps_an_explicit_process_exit_to_the_cleanup_error(self) -> None:
        self.configure_herdr(
            ["agent", "start", "worker", "--kind", "claude", "--pane", "pane-1", "--timeout", "90000", "--"],
            code=1, stderr=json.dumps({"error": {"code": "agent_start_failed",
                                              "message": "agent process exited before becoming interactive"}}))
        with self.assertRaises(herdr.AgentExited):
            self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.NoOpeningPrompt(), 90_000)
        self.assertEqual(len(self.start_commands()), 1)

    def test_start_sends_exactly_one_multiline_prompt(self) -> None:
        self.herdr.start_agent("worker", "pane-1", "codex", ["-m", "fixture-model"], herdr.OpeningPrompt("opening\nprompt"), 200)
        self.assertEqual(self.start_commands(), [
            ["--session", "fixture", "agent", "start", "worker", "--kind", "codex", "--pane", "pane-1", "--timeout", "200", "--", "-m", "fixture-model"],
            ["--session", "fixture", "agent", "prompt", "pane-1", "opening\nprompt"],
        ])

    def start_responses(self, timeout_ms: int, responses: list[CommandResponse]) -> list[str]:
        arguments = ["--session", "fixture", "agent", "start", "worker", "--kind", "claude",
                     "--pane", "pane-1", "--timeout", str(timeout_ms), "--", "--resume", "conversation"]
        self.responses[" ".join(arguments)] = responses
        _ = self.responses_path.write_text(json.dumps(self.responses))
        return arguments

    def enforce_start_timeout_minimum(self) -> None:
        command = herdr.Herdr.start_command

        def constrained_command(client: herdr.Herdr, argv: list[str], kind: str, pane: str,
                                timeout: float) -> subprocess.CompletedProcess[str]:
            result = command(client, argv, kind, pane, timeout)
            if argv[3:5] == ["agent", "start"] and int(argv[argv.index("--timeout") + 1]) <= 3000:
                return subprocess.CompletedProcess(argv, 1, "", json.dumps({"error": {
                    "code": "invalid_agent_timeout", "message": "timeout must exceed 3000 ms",
                }}))
            return result

        _ = cast(object, self.enterContext(patch.object(herdr.Herdr, "start_command", constrained_command)))

    def test_start_retries_busy_then_prompts_once(self) -> None:
        self.enforce_start_timeout_minimum()
        busy: CommandResponse = {"stdout": "", "code": 1, "stderr": json.dumps({"error": {
            "code": "agent_pane_busy", "message": "shell not yet available",
        }})}
        arguments = self.start_responses(10_000, [busy, busy, {"stdout": "", "code": 0}])
        pause = Mock()
        with patch.object(herdr, "time", SimpleNamespace(monotonic=lambda: 0, sleep=pause)):
            self.herdr.start_agent("worker", "pane-1", "claude", ["--resume", "conversation"], herdr.OpeningPrompt("continue\nworking"), 10_000)
        self.assertEqual(self.start_commands(), [arguments, arguments, arguments,
                                          ["--session", "fixture", "agent", "prompt", "pane-1", "continue\nworking"]])
        self.assertEqual([call.args for call in pause.call_args_list], [(0.1,), (0.1,)])

    def test_start_busy_retries_share_deadline_and_raise_last_failure(self) -> None:
        self.enforce_start_timeout_minimum()
        attempts: list[list[str]] = []
        for timeout_ms, message in ((3200, "first busy"), (3100, "last busy")):
            attempts.append(self.start_responses(timeout_ms, [{"stdout": "", "code": 1, "stderr": json.dumps({
                "error": {"code": "agent_pane_busy", "message": message},
            })}]))
        elapsed = [0.0]

        def advance(seconds: float) -> None:
            elapsed[0] += seconds

        with patch.object(herdr, "time", SimpleNamespace(monotonic=lambda: elapsed[0], sleep=advance)):
            with self.assertRaisesRegex(herdr.HerdrFailure, "last busy") as raised:
                self.herdr.start_agent("worker", "pane-1", "claude", ["--resume", "conversation"], herdr.OpeningPrompt("continue"), 3200)
        self.assertEqual(raised.exception.code, "agent_pane_busy")
        self.assertEqual(self.start_commands(), attempts)

    def test_start_initial_small_timeout_keeps_herdr_refusal_without_retry(self) -> None:
        self.enforce_start_timeout_minimum()
        for timeout_ms in (2999, 3000):
            with self.subTest(timeout_ms=timeout_ms):
                self.log.unlink(missing_ok=True)
                arguments = self.start_responses(timeout_ms, [{"stdout": "", "code": 0}])
                pause = Mock()
                with patch.object(herdr, "time", SimpleNamespace(monotonic=lambda: 0, sleep=pause)):
                    with self.assertRaises(herdr.HerdrFailure) as raised:
                        self.herdr.start_agent("worker", "pane-1", "claude", ["--resume", "conversation"], herdr.OpeningPrompt("continue"), timeout_ms)
                self.assertEqual(raised.exception.code, "invalid_agent_timeout")
                self.assertEqual(self.start_commands(), [arguments])
                pause.assert_not_called()

    def test_start_does_not_retry_other_failure_codes(self) -> None:
        for code in ("agent_blocked", "invalid_agent_name", "not_found"):
            with self.subTest(code=code):
                self.log.unlink(missing_ok=True)
                arguments = self.start_responses(10_000, [{"stdout": "", "code": 1, "stderr": json.dumps({
                    "error": {"code": code, "message": "start refused"},
                })}, {"stdout": "", "code": 0}])
                pause = Mock()
                with patch.object(herdr, "time", SimpleNamespace(monotonic=lambda: 0, sleep=pause)):
                    with self.assertRaisesRegex(herdr.HerdrFailure, "start refused") as raised:
                        self.herdr.start_agent("worker", "pane-1", "claude", ["--resume", "conversation"], herdr.OpeningPrompt("continue"), 10_000)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(self.start_commands(), [arguments])
                pause.assert_not_called()

    def test_claude_start_passes_only_extra_arguments_after_separator(self) -> None:
        self.herdr.start_agent(
            "worker", "pane-1", kind="claude",
            agent_arguments=["--resume", "conversation", "--model", "fixture-model"],
            opening=herdr.OpeningPrompt("continue"), timeout_ms=10_000,
        )
        start, prompt = self.start_commands()
        self.assertEqual(start, [
            "--session", "fixture", "agent", "start", "worker", "--kind", "claude",
            "--pane", "pane-1", "--timeout", "10000", "--", "--resume", "conversation",
            "--model", "fixture-model",
        ])
        self.assertEqual(start.count("claude"), 1)
        self.assertEqual(start[start.index("--") + 1:], ["--resume", "conversation", "--model", "fixture-model"])
        self.assertEqual(prompt, ["--session", "fixture", "agent", "prompt", "pane-1", "continue"])

    def test_agent_executable_argument_is_refused_before_any_herdr_call(self) -> None:
        for kind in ("claude", "codex"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError, "agent_arguments must omit"):
                    self.herdr.start_agent(
                        "worker", "pane-1", kind=kind, agent_arguments=[kind, "--resume", "conversation"],
                        opening=herdr.OpeningPrompt("continue"), timeout_ms=10_000,
                    )
        self.assertEqual(self.commands(), [])

    def test_module_start_refuses_executable_before_pane_lookup(self) -> None:
        with self.assertRaisesRegex(ValueError, "agent_arguments must omit"):
            herdr.start_agent(
                "fixture", "pane-1", kind="claude", agent_arguments=["claude", "--resume", "conversation"],
                opening=herdr.OpeningPrompt("continue"), timeout_ms=10_000,
            )
        self.assertEqual(self.commands(), [])

    def test_module_start_passes_extra_arguments_and_uses_pane_label(self) -> None:
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [{"pane_id": "pane-1", "label": "worker"}]}}))
        herdr.start_agent(
            "fixture", "pane-1", kind="claude", agent_arguments=["--resume", "conversation"],
            opening=herdr.OpeningPrompt("continue"), timeout_ms=10_000,
        )
        self.assertEqual(self.start_commands(), [
            ["--session", "fixture", "pane", "list"],
            ["--session", "fixture", "agent", "start", "worker", "--kind", "claude", "--pane", "pane-1",
             "--timeout", "10000", "--", "--resume", "conversation"],
            ["--session", "fixture", "agent", "prompt", "pane-1", "continue"],
        ])

    def test_agent_names_are_valid_for_method_and_module_launches(self) -> None:
        for label, expected in (("Kept Runner", "kept-runner"), ("7runner", "a-7runner"),
                                ("X" * 40, "x" * 32), ("", "a-7pane-1")):
            for module_launch in (False, True):
                with self.subTest(label=label, module_launch=module_launch):
                    self.log.unlink(missing_ok=True)
                    if module_launch:
                        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
                            {"pane_id": "7pane-1", "label": label},
                        ]}}))
                        herdr.start_agent("fixture", "7pane-1", "claude", ["--model", "opus"], herdr.OpeningPrompt("continue"), 200)
                    else:
                        self.herdr.start_agent(label or "7pane-1", "7pane-1", "claude", ["--model", "opus"], herdr.OpeningPrompt("continue"), 200)
                    start, prompt = self.start_commands()[-2:]
                    self.assertEqual(start, [
                        "--session", "fixture", "agent", "start", expected, "--kind", "claude",
                        "--pane", "7pane-1", "--timeout", "200", "--", "--model", "opus",
                    ])
                    self.assertEqual(prompt, ["--session", "fixture", "agent", "prompt", "7pane-1", "continue"])

    def test_pane_activity_distinguishes_shell_from_foreground_commands(self) -> None:
        for foreground, names, expected in (
            (3392858, ("zsh",), herdr.PaneAtShell()),
            (3393000, ("claude", "node"), herdr.PaneRunning(("claude", "node"))),
            (3393001, ("zsh",), herdr.PaneRunning(("zsh",))),
        ):
            with self.subTest(foreground=foreground):
                self.configure_herdr(["pane", "process-info", "--pane", "w1:p1"], json.dumps({
                    "id": "cli:pane:process_info", "result": {"type": "pane_process_info", "process_info": {
                        "pane_id": "w1:p1", "shell_pid": 3392858,
                        "foreground_process_group_id": foreground,
                        "foreground_processes": [{"name": name, "pid": foreground + index,
                            "argv": [name], "cmdline": name, "cwd": "/tmp"} for index, name in enumerate(names)],
                    }},
                }))
                self.assertEqual(self.herdr.pane_activity("w1:p1"), expected)

    def test_pane_activity_rejects_malformed_process_info(self) -> None:
        valid: dict[str, object] = {"shell_pid": 10, "foreground_process_group_id": 10,
                                   "foreground_processes": [{"name": "zsh"}]}
        malformed: list[object] = [
            None, [], {}, {**valid, "shell_pid": "10"}, {**valid, "shell_pid": True},
            {**valid, "foreground_process_group_id": 0},
            {**valid, "foreground_process_group_id": None},
            {**valid, "foreground_processes": {}},
            {**valid, "foreground_processes": ["zsh"]},
            {**valid, "foreground_processes": [{}]},
            {**valid, "foreground_processes": [{"name": 10}]},
        ]
        for info in malformed:
            with self.subTest(info=info):
                self.configure_herdr(["pane", "process-info", "--pane", "w1:p1"], json.dumps({
                    "result": {"process_info": info},
                }))
                with self.assertRaises(herdr.HerdrFailure):
                    _ = self.herdr.pane_activity("w1:p1")

    def test_prompt_refusal_raises_blocked_without_retry(self) -> None:
        self.configure_herdr(["agent", "prompt", "pane-1", "work"], code=1, stderr=json.dumps({"error": {"code": "agent_blocked", "message": "permission dialog"}}))
        with self.assertRaises(herdr.AgentBlocked):
            self.herdr.prompt("pane-1", "work")
        self.assertEqual(len(self.commands()), 1)

    def test_readiness_and_wait_states(self) -> None:
        self.configure_herdr(["agent", "get", "pane-1"], json.dumps({"result": {"agent": {"agent_status": "idle", "interactive_ready": True}}}))
        self.assertEqual(self.herdr.readiness("pane-1"), {"agent_status": "idle", "interactive_ready": True})
        arguments = ["agent", "wait", "pane-1", "--until", "done", "--timeout", "20"]
        self.configure_herdr(arguments, json.dumps({"result": {"agent": {"agent_status": "done"}}}))
        self.assertEqual(self.herdr.wait_until("pane-1", "done", 20), herdr.ObservedAgentState("done"))
        self.configure_herdr(arguments, code=1, stderr=json.dumps({"error": {"code": "timeout", "message": "deadline"}}))
        self.assertEqual(self.herdr.wait_until("pane-1", "done", 20), herdr.WaitTimedOut())
        with patch.object(herdr, "run_program", side_effect=subprocess.TimeoutExpired("fake", 1)):
            self.assertEqual(self.herdr.wait_until("pane-1", "done", 20), herdr.WaitTimedOut())
        self.configure_herdr(arguments, json.dumps({"result": {"agent": {"agent_status": "blocked"}}}))
        with self.assertRaises(herdr.AgentBlocked):
            _ = self.herdr.wait_until("pane-1", "done", 20)

    def test_read_sources_formats_keys_and_close(self) -> None:
        _ = self.herdr.read("pane-1", 40, joined=False, ansi=False)
        _ = self.herdr.read("pane-1", 80, joined=True, ansi=True)
        self.herdr.send_keys("pane-1", "ctrl-c")
        self.herdr.close_pane("pane-1")
        self.assertEqual(self.commands(), [
            ["--session", "fixture", "pane", "read", "pane-1", "--source", "recent", "--lines", "40"],
            ["--session", "fixture", "pane", "read", "pane-1", "--source", "recent-unwrapped", "--lines", "80", "--format", "ansi"],
            ["--session", "fixture", "pane", "send-keys", "pane-1", "ctrl-c"],
            ["--session", "fixture", "pane", "close", "pane-1"],
        ])

    def test_process_place_named_default_and_nonherdr(self) -> None:
        server = self.process(10, 1, ["/fixture/herdr", "--session", "fixture", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        _ = self.process(11, 10, ["/bin/shell"], {})
        _ = self.process(12, 11, ["/bin/sleep", "600"], {"HERDR_PANE_ID": "pane-1"})
        self.assertEqual(herdr.herdr_place_of(12), herdr.HerdrPlace("fixture", "pane-1"))
        server = self.process(20, 1, ["/fixture/herdr", "server"], {})
        self.unix_socket(self.root / "config/herdr/herdr.sock", 200)
        self.socket_fd(server, 200)
        _ = self.process(21, 20, ["/bin/sleep"], {"HERDR_PANE_ID": "pane-default"})
        self.assertEqual(herdr.herdr_place_of(21), herdr.HerdrPlace("default", "pane-default"))
        _ = self.process(30, 1, ["/bin/program", "server"], {"HERDR_PANE_ID": "pane-foreign"})
        self.assertEqual(herdr.herdr_place_of(30), herdr.NotInHerdr())
        self.assertEqual(herdr.herdr_place_of(11), herdr.NotInHerdr())
        self.assertEqual(herdr.herdr_place_of(99), herdr.NotInHerdr())

    def test_server_environment_finds_attach_and_handoff_servers_by_socket(self) -> None:
        commands = (["/fixture/herdr", "server"], ["/fixture/herdr", "--handoff", "replacement"])
        for offset, arguments in enumerate(commands):
            name = f"fixture{offset}"
            with self.subTest(arguments=arguments):
                environment = {"HERDR_SESSION": name, "CLAUDE_CONFIG_DIR": "/account",
                               "CLAUDE_CODE_REMOTE_MEMORY_DIR": "/memory", "VALUE": "one=two"}
                path = self.root / "config" / "herdr" / "sessions" / name / "herdr.sock"
                inode = 813310542 + offset * 2
                self.unix_socket(path, inode)
                self.unix_socket(path, inode + 1, listening=False)
                server = self.process(10 + offset, 1, arguments, environment)
                self.socket_fd(server, inode)
                self.socket_fd(server, inode, fd=4)
                # A connected client shares the path, but never the listener inode.
                client = self.process(20 + offset, 1, ["/fixture/herdr", "--session", name,
                    "session", "attach", name], {"CLAUDE_CONFIG_DIR": "/client-account"})
                self.socket_fd(client, inode + 1)
                # An argv match without the listening fd must not win discovery.
                _ = self.process(30 + offset, 1, ["/fixture/herdr", "--session", name, "server"],
                                 {"CLAUDE_CONFIG_DIR": "/old-server-account"})
                self.assertEqual(herdr.server_environment(name), environment)

    def test_server_environment_uses_root_socket_for_default_session(self) -> None:
        self.unix_socket(self.root / "config/herdr/herdr.sock", 100)
        server = self.process(10, 1, ["/fixture/herdr", "server"], {"ACCOUNT": "default-server"})
        self.socket_fd(server, 100)
        self.unix_socket(self.root / "config/herdr/sessions/default/herdr.sock", 200)
        decoy = self.process(20, 1, ["/fixture/herdr", "server"], {"ACCOUNT": "nested-decoy"})
        self.socket_fd(decoy, 200)
        self.assertEqual(herdr.server_environment("default"), {"ACCOUNT": "default-server"})

    def test_server_environment_uses_config_directory_with_spaces(self) -> None:
        path = self.root / "configuration with spaces/herdr/sessions/fixture/herdr.sock"
        self.unix_socket(path, 100)
        server = self.process(10, 1, ["/fixture/herdr", "server"], {"ACCOUNT": "configured"})
        self.socket_fd(server, 100)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "configuration with spaces")}):
            self.assertEqual(herdr.server_environment("fixture"), {"ACCOUNT": "configured"})

    def test_server_environment_defaults_to_home_config_directory(self) -> None:
        path = self.root / "home/.config/herdr/sessions/fixture/herdr.sock"
        self.unix_socket(path, 100)
        server = self.process(10, 1, ["/fixture/herdr", "server"], {"ACCOUNT": "home"})
        self.socket_fd(server, 100)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}):
            self.assertEqual(herdr.server_environment("fixture"), {"ACCOUNT": "home"})

    def test_server_environment_requires_a_listener_for_the_exact_session(self) -> None:
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100, listening=False)
        self.unix_socket(self.root / "config/herdr/sessions/other/herdr.sock", 200)
        process = self.process(10, 1, ["/fixture/herdr", "--session", "fixture", "server"], {})
        self.socket_fd(process, 100)
        with self.assertRaisesRegex(herdr.HerdrFailure, "session fixture.*listening socket, found 0"):
            _ = herdr.server_environment("fixture")

    def test_server_environment_never_uses_a_connected_client_as_holder(self) -> None:
        path = self.root / "config/herdr/sessions/fixture/herdr.sock"
        self.unix_socket(path, 100)
        self.unix_socket(path, 101, listening=False)
        client = self.process(10, 1, ["/fixture/herdr", "--session", "fixture", "session", "attach", "fixture"], {})
        self.socket_fd(client, 101)
        _ = self.process(20, 1, ["/fixture/herdr", "--session", "fixture", "server"], {})
        with self.assertRaisesRegex(herdr.HerdrFailure, "session fixture.*socket holder, found 0"):
            _ = herdr.server_environment("fixture")

    def test_server_environment_refuses_two_listener_holders(self) -> None:
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        first = self.process(10, 1, ["/fixture/herdr", "server"], {"ACCOUNT": "first"})
        second = self.process(20, 1, ["/fixture/herdr", "--handoff"], {"ACCOUNT": "second"})
        self.socket_fd(first, 100)
        self.socket_fd(second, 100)
        with self.assertRaisesRegex(herdr.HerdrFailure, "session fixture.*socket holder, found 2"):
            _ = herdr.server_environment("fixture")

    def test_server_environment_refuses_ambiguous_listeners(self) -> None:
        path = self.root / "config/herdr/sessions/fixture/herdr.sock"
        self.unix_socket(path, 100)
        self.unix_socket(path, 200)
        for pid, inode in ((10, 100), (20, 200)):
            process = self.process(pid, 1, ["/fixture/herdr", "server"], {})
            self.socket_fd(process, inode)
        with self.assertRaisesRegex(herdr.HerdrFailure, "session fixture.*listening socket, found 2"):
            _ = herdr.server_environment("fixture")

    def test_server_environment_reports_unreadable_environment(self) -> None:
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        process = self.process(10, 1, ["/fixture/herdr", "server"], {})
        self.socket_fd(process, 100)
        process.joinpath("environ").unlink()
        process.joinpath("environ").mkdir()
        with self.assertRaisesRegex(herdr.HerdrFailure, "session fixture.*cannot read process environment"):
            _ = herdr.server_environment("fixture")

    def test_server_environment_reports_unreadable_socket_table(self) -> None:
        self.proc.joinpath("net", "unix").mkdir(parents=True)
        with self.assertRaisesRegex(herdr.HerdrFailure, "session fixture.*cannot read listening sockets"):
            _ = herdr.server_environment("fixture")

    def test_processes_skip_unreadable_and_limit_marks(self) -> None:
        server = self.process(10, 1, ["/fixture/herdr", "--session", "fixture", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        _ = self.process(12, 10, ["/bin/claude", "--model", "fixture"], {"HERDR_PANE_ID": "pane-1", "SHOWRUNNER_UNIT": "prod", "SHOWRUNNER_UNIT_ID": "unit", "SECRET": "never returned"})
        unreadable = self.process(13, 10, ["/bin/sleep"], {"HERDR_PANE_ID": "pane-2"})
        unreadable.joinpath("environ").unlink()
        unreadable.joinpath("environ").mkdir()
        self.assertEqual(herdr.herdr_processes(), [herdr.HerdrProcess(12, herdr.HerdrPlace("fixture", "pane-1"), {"SHOWRUNNER_UNIT": "prod", "SHOWRUNNER_UNIT_ID": "unit"}, "claude", str(self.root))])

    def test_ancestry_loop_and_vanished_cwd_are_skipped(self) -> None:
        _ = self.process(10, 11, ["/bin/sh"], {"HERDR_PANE_ID": "pane-1"})
        _ = self.process(11, 10, ["/bin/sh"], {})
        self.assertEqual(herdr.herdr_place_of(10), herdr.NotInHerdr())
        server = self.process(20, 1, ["/fixture/herdr", "server"], {})
        self.unix_socket(self.root / "config/herdr/herdr.sock", 100)
        self.socket_fd(server, 100)
        process = self.process(21, 20, ["/bin/sleep"], {"HERDR_PANE_ID": "pane-2"})
        process.joinpath("cwd").unlink()
        self.assertEqual(herdr.herdr_processes(), [])

    def test_trust_ignores_inherited_git_repository_and_configuration(self) -> None:
        requested = self.root / "requested"
        foreign = self.root / "foreign"
        requested.mkdir()
        foreign.mkdir()
        clean_environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        clean_environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        for directory in (requested, foreign):
            result = subprocess.run([GIT_BINARY, "init", str(directory)], env=clean_environment,
                                    capture_output=True, text=True, check=False, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        config = self.root / "claude.json"
        _ = config.write_text(json.dumps({"projects": {
            str(foreign): {"hasTrustDialogAccepted": True},
            str(requested): {"hasTrustDialogAccepted": False},
        }}))
        hostile_environment = {
            "PATH": str(Path(GIT_BINARY).parent), "GIT_DIR": str(foreign / ".git"),
            "GIT_WORK_TREE": str(foreign), "GIT_COMMON_DIR": str(foreign / ".git"),
            "GIT_CONFIG_GLOBAL": "/fixture/foreign-global", "GIT_CONFIG_SYSTEM": "/fixture/foreign-system",
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*",
        }
        with patch.dict(os.environ, hostile_environment), patch.object(subprocess, "run", wraps=subprocess.run) as invoked:
            self.assertEqual(herdr.claude_trust(requested, config), herdr.Untrusted(requested, config, (requested,)))
            self.assertEqual(herdr.repository_trust_key(self.root), self.root)
            environment = cast(dict[str, str], invoked.call_args.kwargs["env"])
        self.assertEqual({key: value for key, value in environment.items() if key.startswith("GIT_")},
                         {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})

    def test_herdr_clients_and_their_children_keep_the_owning_session(self) -> None:
        server = self.process(10, 1, ["/fixture/herdr", "--session", "owner", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/owner/herdr.sock", 100)
        self.socket_fd(server, 100)
        commands = [
            ["--session", "target", "agent", "prompt", "w2:p1", "server"],
            ["--session", "target", "server", "stop"],
            ["--session=target", "server", "reload-config"],
            ["--session", "target", "status", "server"],
        ]
        for offset, arguments in enumerate(commands):
            pid = 20 + offset * 2
            _ = self.process(pid, 10, ["/fixture/herdr", *arguments], {"HERDR_PANE_ID": "pane-owner"})
            _ = self.process(pid + 1, pid, ["/bin/child"], {
                "HERDR_PANE_ID": "pane-owner", "SHOWRUNNER_UNIT": "production", "SHOWRUNNER_UNIT_ID": "unit",
            })
            with self.subTest(arguments=arguments):
                self.assertEqual(herdr.herdr_place_of(pid), herdr.HerdrPlace("owner", "pane-owner"))
                self.assertEqual(herdr.herdr_place_of(pid + 1), herdr.HerdrPlace("owner", "pane-owner"))
        processes = herdr.herdr_processes()
        self.assertEqual(len(processes), len(commands) * 2)
        self.assertTrue(all(process.place.session == "owner" for process in processes))
        self.assertEqual([process.marks for process in processes if process.command == "child"],
                         [{"SHOWRUNNER_UNIT": "production", "SHOWRUNNER_UNIT_ID": "unit"}] * len(commands))

    def test_socket_ownership_decides_place_despite_conflicting_argv(self) -> None:
        server = self.process(10, 1, ["/fixture/herdr", "--handoff", "--session=other", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/owner/herdr.sock", 100)
        self.socket_fd(server, 100)
        _ = self.process(11, 10, ["/bin/sleep"], {"HERDR_PANE_ID": "pane-owner"})
        self.assertEqual(herdr.herdr_place_of(11), herdr.HerdrPlace("owner", "pane-owner"))

    def test_plain_calls_and_session_listing_translate_timeouts(self) -> None:
        with patch.object(herdr, "run_program", side_effect=subprocess.TimeoutExpired("fake", 10)):
            with self.assertRaisesRegex(herdr.HerdrFailure, "herdr pane list: no answer after 10 s"):
                _ = self.herdr.run("pane", "list")
            with self.assertRaisesRegex(herdr.HerdrFailure, "herdr pane list: no answer after 10 s"):
                _ = self.herdr.result("pane", "list")
            with self.assertRaisesRegex(herdr.HerdrFailure, "herdr session list: no answer after 10 s"):
                _ = herdr.live_sessions()

    def test_trust_uses_explicit_config_and_common_repository(self) -> None:
        config = self.root / "claude.json"
        _ = config.write_text(json.dumps({"projects": {"/repository": {"hasTrustDialogAccepted": True}}}))
        with patch.object(herdr, "run_program", return_value=subprocess.CompletedProcess(["git"], 0, "/repository/.git\n", "")):
            self.assertEqual(herdr.claude_trust(Path("/repository/worktree"), config),
                             herdr.Trusted(Path("/repository/worktree"), config, Path("/repository")))
        with patch.object(herdr, "run_program", return_value=subprocess.CompletedProcess(["git"], 1, "", "")):
            self.assertEqual(herdr.repository_trust_key(self.root), self.root)

    def trust_git(self, folder: Path, root: Path, main: Path) -> None:
        def result(argv: list[str], timeout: float = 10.0, *,
                   environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            _ = timeout, environment
            self.assertEqual(argv[2], str(folder))
            path = root if argv[-1] == "--show-toplevel" else main / ".git"
            return subprocess.CompletedProcess(argv, 0, str(path) + "\n", "")

        _ = cast(object, self.enterContext(patch.object(herdr, "run_program", side_effect=result)))

    def test_each_exact_folder_repository_root_and_main_key_trusts(self) -> None:
        folder = self.root / "worktree" / "nested"
        root = folder.parent
        main = self.root / "main"
        config = self.root / "claude.json"
        self.trust_git(folder, root, main)
        for key in (folder, root, main):
            with self.subTest(key=key):
                _ = config.write_text(json.dumps({"projects": {str(key): {"hasTrustDialogAccepted": True}}}))
                self.assertEqual(herdr.claude_trust(folder, config), herdr.Trusted(folder, config, key))

    def test_ancestor_trust_does_not_authorize_child_repository(self) -> None:
        folder = self.root / "worktree" / "nested"
        main = self.root / "main"
        config = self.root / "claude.json"
        _ = config.write_text(json.dumps({"projects": {str(self.root): {"hasTrustDialogAccepted": True}}}))
        self.trust_git(folder, folder.parent, main)
        self.assertEqual(herdr.claude_trust(folder, config),
                         herdr.Untrusted(folder, config, (folder, folder.parent, main)))

    def test_only_boolean_true_accepts_project_trust(self) -> None:
        config = self.root / "claude.json"
        with patch.object(herdr, "run_program", return_value=subprocess.CompletedProcess(["git"], 1, "", "")):
            refused_values: tuple[object, ...] = (False, 1, "true", None, {}, [])
            for value in refused_values:
                with self.subTest(value=value):
                    _ = config.write_text(json.dumps({"projects": {str(self.root): {"hasTrustDialogAccepted": value}}}))
                    self.assertEqual(herdr.claude_trust(self.root, config), herdr.Untrusted(self.root, config, (self.root,)))

    def test_missing_or_malformed_account_file_refuses_without_contents(self) -> None:
        config = self.root / "claude.json"
        self.assertEqual(herdr.claude_trust(self.root, config),
                         herdr.UnreadableTrustFile(self.root, config, "FileNotFoundError"))
        for contents, error_class in (("private malformed contents", "JSONDecodeError"),
                                      ("[]", "ValueError"), ('{"projects": []}', "ValueError")):
            with self.subTest(error_class=error_class, contents=contents):
                _ = config.write_text(contents)
                result = herdr.claude_trust(self.root, config)
                self.assertEqual(result, herdr.UnreadableTrustFile(self.root, config, error_class))
                self.assertEqual(str(result), f"cannot read {config} to check Claude trust ({error_class})")
                self.assertNotIn(contents, repr(result))
        with patch.object(Path, "read_text", side_effect=PermissionError("private exception detail")):
            result = herdr.claude_trust(self.root, config)
            self.assertEqual(str(result), f"cannot read {config} to check Claude trust (PermissionError)")
            self.assertNotIn("private exception detail", repr(result))
        _ = config.write_bytes(b"\xffprivate content")
        self.assertEqual(str(herdr.claude_trust(self.root, config)),
                         f"cannot read {config} to check Claude trust (UnicodeDecodeError)")

    def test_unreadable_account_file_identifies_the_claude_trust_check(self) -> None:
        config = self.root / "account file.json"
        self.assertEqual(str(herdr.UnreadableTrustFile(self.root, config, "PermissionError")),
                         f"cannot read {config} to check Claude trust (PermissionError)")

    def test_new_worktree_checks_main_checkout_not_trusted_showrunner_worktree(self) -> None:
        showrunner = self.root / "showrunner"
        main = self.root / "main"
        config = self.root / "claude.json"
        _ = config.write_text(json.dumps({"projects": {str(showrunner): {"hasTrustDialogAccepted": True}}}))

        def result(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            path = Path(argv[2]) if argv[-1] == "--show-toplevel" else main / ".git"
            return subprocess.CompletedProcess(argv, 0, str(path) + "\n", "")

        with patch.object(herdr, "run_program", side_effect=result):
            checked_folder = herdr.repository_trust_key(showrunner)
            self.assertEqual(checked_folder, main)
            self.assertEqual(herdr.claude_trust(checked_folder, config), herdr.Untrusted(main, config, (main,)))

    def test_survey_counts_nested_repositories_and_submodules_but_skips_generated_trees(self) -> None:
        checkout = self.root / "checkout"
        (checkout / ".git").mkdir(parents=True)
        (checkout / "nested" / ".git").mkdir(parents=True)
        submodule = checkout / "submodule"
        submodule.mkdir()
        _ = (submodule / ".git").write_text("gitdir: ../.git/modules/submodule\n")
        for generated in (".git", "target", "node_modules"):
            (checkout / generated / "ignored" / ".git").mkdir(parents=True)
        self.assertEqual(trust_survey.descendant_repositories(self.root), 3)
        self.assertEqual(trust_survey.descendant_repositories(checkout), 2)
        self.assertEqual(trust_survey.project_kind(self.root, self.root, self.root), "ancestor of 3 repos")


if __name__ == "__main__":
    _ = unittest.main()
