"""Library specification tests using fake commands and a fake proc tree."""

from __future__ import annotations

import json
import os
import subprocess
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

import herdr
from fake_herdr import CommandResponse

GIT_BINARY = shutil.which("git") or "git"


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
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
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
        }, clear=True)))
        self.configure([], "")

    def configure(self, arguments: list[str], stdout: str = "", code: int = 0, stderr: str = "") -> None:
        self.responses[" ".join(arguments)] = {"stdout": stdout, "code": code, "stderr": stderr}
        _ = self.responses_path.write_text(json.dumps(self.responses))

    def commands(self) -> list[list[str]]:
        return [cast(list[str], json.loads(line)) for line in self.log.read_text().splitlines()] if self.log.exists() else []

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
        herdr.start_server("fixture", 10_000)
        self.assertEqual(self.commands(), [["--session", "fixture", "status", "server"]])

    def test_server_start_deadline_and_sanitized_unit(self) -> None:
        self.configure_herdr(["status", "server"], "status: stopped\n")
        with self.assertRaisesRegex(herdr.HerdrFailure, "server fixture not answering after 0 ms"):
            herdr.start_server("fixture", 0)
        self.assertEqual(self.commands()[-1], ["--user", "--unit=herdr-fixture", "--collect", str(self.fake), "--session", "fixture", "server"])
        self.assertEqual(herdr.server_unit("one /two:é"), "herdr-one--two--")

    def test_server_start_polls_after_launch(self) -> None:
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0},
        ]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        herdr.start_server("fixture", 10_000)
        self.assertEqual(len(self.commands()), 3)

    def test_stop_stops_only_named_active_unit(self) -> None:
        herdr.stop_session("fixture")
        self.assertEqual(self.commands(), [
            ["--session", "fixture", "server", "stop"], ["--user", "is-active", "herdr-fixture"],
            ["--user", "stop", "herdr-fixture"],
        ])

    def test_stop_checks_unit_after_server_refusal(self) -> None:
        self.configure_herdr(["server", "stop"], code=1, stderr="server failed")
        with self.assertRaises(herdr.HerdrFailure):
            herdr.stop_session("fixture")
        self.assertEqual(self.commands()[-1], ["--user", "stop", "herdr-fixture"])

    def test_open_pane_passes_environment_and_renames(self) -> None:
        arguments = ["workspace", "create", "--label", "worker", "--cwd", "/repo", "--env", "SHOWRUNNER_UNIT=prod", "--env", "CLAUDE_CONFIG_DIR=/account", "--no-focus"]
        self.configure_herdr(arguments, json.dumps({"result": {"root_pane": {"pane_id": "pane-1"}}}))
        self.assertEqual(herdr.open_pane("fixture", "worker", "/repo", {"SHOWRUNNER_UNIT": "prod", "CLAUDE_CONFIG_DIR": "/account"}), "pane-1")
        self.assertEqual(self.commands(), [["--session", "fixture", *arguments], ["--session", "fixture", "pane", "rename", "pane-1", "worker"]])

    def test_agent_not_ready_reads_screen_without_input(self) -> None:
        arguments = ["agent", "start", "worker", "--kind", "claude", "--pane", "pane-1", "--timeout", "100", "--", "--model", "fixture-model"]
        self.configure_herdr(arguments, code=1, stderr=json.dumps({"error": {"code": "agent_not_ready", "message": "trust dialog"}}))
        self.configure_herdr(["pane", "read", "pane-1", "--source", "visible"], "Trust this folder?")
        with self.assertRaises(herdr.AgentNotReady) as raised:
            self.herdr.start_agent("worker", "pane-1", "claude", ["--model", "fixture-model"], "opening\nprompt", 100)
        self.assertEqual(raised.exception.screen, "Trust this folder?")
        self.assertEqual(self.commands(), [["--session", "fixture", *arguments], ["--session", "fixture", "pane", "read", "pane-1", "--source", "visible"]])

    def test_start_sends_exactly_one_multiline_prompt(self) -> None:
        self.herdr.start_agent("worker", "pane-1", "codex", ["-m", "fixture-model"], "opening\nprompt", 200)
        self.assertEqual(self.commands(), [
            ["--session", "fixture", "agent", "start", "worker", "--kind", "codex", "--pane", "pane-1", "--timeout", "200", "--", "-m", "fixture-model"],
            ["--session", "fixture", "agent", "prompt", "pane-1", "opening\nprompt"],
        ])

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
        _ = self.process(10, 1, ["/fixture/herdr", "--session", "fixture", "server"], {})
        _ = self.process(11, 10, ["/bin/shell"], {})
        _ = self.process(12, 11, ["/bin/sleep", "600"], {"HERDR_PANE_ID": "pane-1"})
        self.assertEqual(herdr.herdr_place_of(12), herdr.HerdrPlace("fixture", "pane-1"))
        _ = self.process(20, 1, ["/fixture/herdr", "server"], {})
        _ = self.process(21, 20, ["/bin/sleep"], {"HERDR_PANE_ID": "pane-default"})
        self.assertEqual(herdr.herdr_place_of(21), herdr.HerdrPlace("default", "pane-default"))
        _ = self.process(30, 1, ["/bin/program", "server"], {"HERDR_PANE_ID": "pane-foreign"})
        self.assertEqual(herdr.herdr_place_of(30), herdr.NotInHerdr())
        self.assertEqual(herdr.herdr_place_of(11), herdr.NotInHerdr())
        self.assertEqual(herdr.herdr_place_of(99), herdr.NotInHerdr())

    def test_processes_skip_unreadable_and_limit_marks(self) -> None:
        _ = self.process(10, 1, ["/fixture/herdr", "--session", "fixture", "server"], {})
        _ = self.process(12, 10, ["/bin/claude", "--model", "fixture"], {"HERDR_PANE_ID": "pane-1", "SHOWRUNNER_UNIT": "prod", "SHOWRUNNER_UNIT_ID": "unit", "SECRET": "never returned"})
        unreadable = self.process(13, 10, ["/bin/sleep"], {"HERDR_PANE_ID": "pane-2"})
        unreadable.joinpath("environ").unlink()
        unreadable.joinpath("environ").mkdir()
        self.assertEqual(herdr.herdr_processes(), [herdr.HerdrProcess(12, herdr.HerdrPlace("fixture", "pane-1"), {"SHOWRUNNER_UNIT": "prod", "SHOWRUNNER_UNIT_ID": "unit"}, "claude", str(self.root))])

    def test_ancestry_loop_and_vanished_cwd_are_skipped(self) -> None:
        _ = self.process(10, 11, ["/bin/sh"], {"HERDR_PANE_ID": "pane-1"})
        _ = self.process(11, 10, ["/bin/sh"], {})
        self.assertEqual(herdr.herdr_place_of(10), herdr.NotInHerdr())
        _ = self.process(20, 1, ["/fixture/herdr", "server"], {})
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
            self.assertEqual(herdr.trusted_by_claude(requested, config), herdr.ProjectTrust(requested, False))
            self.assertEqual(herdr.repository_trust_key(self.root), self.root)
            environment = cast(dict[str, str], invoked.call_args.kwargs["env"])
        self.assertEqual({key: value for key, value in environment.items() if key.startswith("GIT_")},
                         {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})

    def test_herdr_clients_and_their_children_keep_the_owning_session(self) -> None:
        _ = self.process(10, 1, ["/fixture/herdr", "--session", "owner", "server"], {})
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

    def test_server_session_equals_option_and_global_flags(self) -> None:
        _ = self.process(10, 1, ["/fixture/herdr", "--handoff", "--session=owner", "server"], {})
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
            self.assertEqual(herdr.trusted_by_claude(Path("/repository/worktree"), config), herdr.ProjectTrust(Path("/repository"), True))
        with patch.object(herdr, "run_program", return_value=subprocess.CompletedProcess(["git"], 1, "", "")):
            self.assertEqual(herdr.repository_trust_key(self.root), self.root)


if __name__ == "__main__":
    _ = unittest.main()
