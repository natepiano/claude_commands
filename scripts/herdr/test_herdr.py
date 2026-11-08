"""Library specification tests using fake commands and a fake proc tree."""

from __future__ import annotations


import json
import io
import ctypes
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
from collections.abc import Callable, Generator
from datetime import datetime, timedelta, timezone
import os
import signal
import subprocess
import shutil
import shlex
import math
import sys
import tempfile
import time
import unittest
from pathlib import Path
from scripts.lib.script_envelope import script_prompt
from types import SimpleNamespace
from typing import BinaryIO, Literal, TextIO, cast, final, override
from unittest.mock import Mock, call, patch


from scripts.herdr import herdr
from scripts.herdr import pane_launch
from scripts.herdr import fake_herdr
from scripts.message import codex_daemon
from scripts.herdr.fake_herdr import CommandResponse
from scripts.lib.agent_scope import agents_slice
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import staff, fake_unit_host
from scripts.message import sessions
from scripts.message.sessions_test_support import copy_session_modules
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id, isolated_staff_file, unit_membership

GIT_BINARY = shutil.which("git") or "git"
BASH_BINARY = shutil.which("bash") or "bash"
NATIVE_PROCESS_START = "Tue Nov  3 08:00:00 2026"
NATIVE_PROCESS_MICROSECONDS = 1793692800123456
DARWIN_BSD_INFO = herdr._DarwinProcessBSDInfo  # pyright: ignore[reportPrivateUsage]


@contextmanager
def native_process_commands(pid: int, *, generations: tuple[herdr.AgentProcessIdentity, ...] = ()) -> Generator[None]:
    """Supply private process facts for both platform readers."""
    program = herdr.run_program
    starts: dict[int, list[int | herdr.ProcessGenerationUnavailable]] = {}
    for identity in generations or (herdr.AgentProcessIdentity(pid, NATIVE_PROCESS_MICROSECONDS),):
        starts.setdefault(identity.pid, []).append(identity.start_ticks)

    def generation(observed_pid: int, flavor: int, argument: int, buffer: object, size: int) -> int:
        if observed_pid not in starts:
            raise AssertionError(f"unexpected process: {observed_pid}")
        if (flavor, argument, size) != (3, 0, ctypes.sizeof(DARWIN_BSD_INFO)):
            raise AssertionError(f"unexpected proc_pidinfo request: {flavor}, {argument}, {size}")
        readings = starts[observed_pid]
        started = readings.pop(0) if len(readings) > 1 else readings[0]
        if isinstance(started, herdr.ProcessGenerationUnavailable):
            return 0
        info = ctypes.cast(cast(ctypes.c_void_p, buffer), ctypes.POINTER(DARWIN_BSD_INFO)).contents
        info.pbi_pid = observed_pid
        info.pbi_start_tvsec, info.pbi_start_tvusec = divmod(started, 1_000_000)
        return size

    def process_start(observed_pid: int) -> str:
        if observed_pid != pid:
            raise AssertionError(f"unexpected process: {observed_pid}")
        return NATIVE_PROCESS_START

    def run(argv: list[str], timeout: float = herdr.PROGRAM_TIMEOUT_SECONDS, *,
            environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["-o", "lstart=,tty=", "-p", str(pid)]:
            return subprocess.CompletedProcess(argv, 0, NATIVE_PROCESS_START + " ??\n", "")
        if argv[1:] == ["-a", "-p", str(pid), "-Fn"]:
            return subprocess.CompletedProcess(argv, 1, "", "")
        return program(argv, timeout, environment=environment)

    with (patch.object(herdr, "run_program", side_effect=run),
          patch.object(ctypes, "CDLL"),
          patch.object(ctypes, "CFUNCTYPE", return_value=Mock(return_value=generation)),
          patch.object(sessions, "process_start", side_effect=process_start)):
        yield


@contextmanager
def codex_launch_evidence(root: Path, argv: list[str]) -> Generator[dict[str, object]]:
    """Supply one Codex process and keep its launch in a private store."""
    pid = 998070
    identity = fixture_runtime_id("herdr-codex-launch")
    proc = root / "codex-launch-proc"
    (proc / str(pid) / "fd").mkdir(parents=True, exist_ok=True)
    _ = (proc / str(pid) / "cmdline").write_bytes("\0".join(argv).encode())
    if "resume" not in argv:
        rollout = root / "rollout-launch.jsonl"
        write_text_atomic(rollout, json.dumps({"type": "session_meta", "payload": {"id": identity}}) + "\n")
        (proc / str(pid) / "fd" / "7").symlink_to(rollout)

    def environment(observed_pid: int) -> dict[str, str]:
        if observed_pid != pid:
            raise AssertionError(f"unexpected process: {observed_pid}")
        return {"CODEX_THREAD_ID": identity, "CODEX_HOME": str(root / ".codex2")}

    with (patch.dict(os.environ, {"HERDR_PROC": str(proc),
                                      "HERDR_PANE_RECORD_DIR": str(root / "codex-launch-records")}),
          isolated_staff_file(root / "codex-staff.json", {}),
          native_process_commands(pid),
          patch.object(herdr.Herdr, "panes", return_value=[
              {"pane_id": "w1:p1", "session_id": "fixture-agent", "workspace_id": "w1"}]),
          patch.object(herdr, "process_environment", side_effect=environment)):
        yield {"process_info": {"foreground_processes": [
            {"pid": pid, "argv": argv, "cwd": str(root)}]}}


# herdr 0.9.0, Mac, 2026-10-16 19:47 EDT: verbatim startup-shell list entries.
STARTUP_PANE_JSON = '{"agent_status":"unknown","cwd":"/Users/natemccoy","focused":false,"foreground_cwd":"/Users/natemccoy","pane_id":"wG:p1","revision":0,"scroll":{"max_offset_from_bottom":0,"offset_from_bottom":0,"viewport_rows":61},"tab_id":"wG:t1","terminal_id":"term_65dfdc73afbe63","workspace_id":"wG"}'
STARTUP_WORKSPACE_JSON = '{"active_tab_id":"wG:t1","agent_status":"unknown","focused":false,"label":"~","number":1,"pane_count":1,"tab_count":1,"workspace_id":"wG"}'


def startup_shell(workspace_id: str) -> tuple[dict[str, object], dict[str, object]]:
    """Adapt the captured shell's identity and home directory for this fixture."""
    workspace = herdr.decode_object(STARTUP_WORKSPACE_JSON, "captured startup workspace")
    pane = herdr.decode_object(STARTUP_PANE_JSON, "captured startup pane")
    workspace.update(workspace_id=workspace_id, active_tab_id=workspace_id + ":t1")
    pane.update(workspace_id=workspace_id, pane_id=workspace_id + ":p1", tab_id=workspace_id + ":t1",
                cwd=str(Path.home()), foreground_cwd=str(Path.home()))
    return workspace, pane


class NativePaneSessionTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HERDR_BIN": str(Path(herdr.__file__).with_name("fake_herdr.py")),
        })))

    def test_pane_agent_process_reads_only_the_foreground_agent_without_native_identity(self) -> None:
        from scripts.herdr.test_worker_panes import PaneWorld

        world = PaneWorld()
        world.locations["w1:p1"] = {"pane_id": "w1:p1", "terminal_id": "friend-terminal",
                                    "workspace_id": "w1", "tab_id": "w1:t1"}
        world.activate(fixture_runtime_id("friend-process"), "w1:p1", "codex")
        pid = world.foreground["w1:p1"][0]
        process = self.root / "proc" / str(pid)
        process.mkdir(parents=True)
        write_text_atomic(process / "stat", f"{pid} (fixture) S " + "0 " * 18 + "1\n")
        with patch.dict(os.environ, {"HERDR_PROC": str(self.root / "proc")}), patch.object(
                herdr, "_PLATFORM", "linux"), patch.object(
                world, "native_pane_process", side_effect=AssertionError("no native identity")):
            self.assertEqual(world.pane_agent_process("w1:p1", "friend-terminal"), herdr.AgentProcessIdentity(pid, 1))
        self.assertEqual(world.process_reads, ["w1:p1"])
        del world.foreground["w1:p1"]
        with self.assertRaisesRegex(herdr.HerdrFailure, "expected one foreground agent"):
            _ = world.pane_agent_process("w1:p1", "friend-terminal")

    def test_prompt_terminal_sends_only_to_the_selected_terminal(self) -> None:
        from scripts.herdr.test_worker_panes import PaneWorld

        world = PaneWorld()
        world.locations["w1:p1"] = {"pane_id": "w1:p1", "terminal_id": "friend-terminal",
                                    "workspace_id": "w1", "tab_id": "w1:t1"}
        with patch.object(world, "native_session", side_effect=AssertionError("no native identity")):
            world.prompt_terminal("w1:p1", "friend-terminal", "Explain the result.")
            world.locations["w1:p1"]["terminal_id"] = "replacement-terminal"
            with self.assertRaisesRegex(herdr.HerdrFailure, "terminal changed"):
                world.prompt_terminal("w1:p1", "friend-terminal", "Do not deliver this text.")
        self.assertEqual(len(world.commands), 1)
        self.assertEqual(world.commands[0][:3], ("agent", "prompt", "w1:p1"))
        self.assertIn("Explain the result.", world.commands[0][3])

    def test_codex_native_process_refuses_an_unheld_thread_from_environment(self) -> None:
        identity = fixture_runtime_id("unheld-thread")
        proc = self.root / "unheld-proc"
        process = proc / "998079"
        (process / "fd").mkdir(parents=True)
        for arguments in (["codex"], ["codex", "app-server", "--managed-daemon"],
                          ["codex", "exec", "resume", identity],
                          ["codex", "-c", "resume", identity]):
            _ = (process / "cmdline").write_bytes("\0".join(arguments).encode())
            with (self.subTest(arguments=arguments),
                  patch.dict(os.environ, {"HERDR_PROC": str(proc)}),
                  patch.object(herdr, "_PLATFORM", "linux"),
                  patch.object(sessions, "process_start", return_value="same-process"),
                  patch.object(herdr, "process_environment", return_value={"CODEX_THREAD_ID": identity})):
                with self.assertRaisesRegex(ValueError, "Codex launch identity is unavailable"):
                    _ = herdr.native_process(998079, "codex")

    def test_codex_native_process_accepts_only_the_thread_in_its_resume_arguments(self) -> None:
        identity = fixture_runtime_id("argv-thread")
        wrong = fixture_runtime_id("wrong-thread")
        proc = self.root / "resume-proc"
        process = proc / "998079"
        (process / "fd").mkdir(parents=True)
        for arguments in (["codex", "resume", identity],
                          ["node", "/opt/@openai/codex/bin/codex.js", "resume", identity],
                          ["codex", "-m", "model", "resume", identity]):
            _ = (process / "cmdline").write_bytes("\0".join(arguments).encode())
            with (self.subTest(arguments=arguments),
                  patch.dict(os.environ, {"HERDR_PROC": str(proc)}),
                  patch.object(herdr, "_PLATFORM", "linux"),
                  patch.object(sessions, "process_start", return_value="same-process"),
                  patch.object(herdr, "process_environment", return_value={"CODEX_THREAD_ID": wrong})):
                self.assertEqual(herdr.native_process(998079, "codex").identity, herdr.LaunchArgument(identity))

    def test_codex_daemon_client_uses_resume_id_without_rollout_or_exported_thread(self) -> None:
        identity = fixture_runtime_id("daemon-client-resume")
        arguments = ["resume", identity]
        with (codex_launch_evidence(self.root, ["codex", *arguments]) as info,
              patch.object(herdr, "_PLATFORM", "linux"),
              patch.object(herdr, "process_environment", return_value={}),
              patch.object(sessions, "process_start", return_value="same-process") as birth,
              patch.object(herdr, "run_program", return_value=subprocess.CompletedProcess[str](
                  [], 0, json.dumps({"result": info}), ""))):
            client = herdr.Herdr("agents")
            entry = client.codex_launch("w1:p1", arguments)
            self.assertEqual(entry["session_id"], identity)
            self.assertEqual(entry.get("proc_start"), "same-process")
            self.assertNotIn("CODEX_THREAD_ID", entry["environment"])
            birth.side_effect = ["same-process", "same-process", "same-process", "reused-pid"]
            with self.assertRaisesRegex(ValueError, "native process start changed"):
                _ = client.codex_launch("w1:p1", arguments)

    def test_bare_codex_launch_uses_the_tabs_current_staff_thread(self) -> None:
        owner = staff.StaffRow(staff.native_id(fixture_runtime_id("bare-client-owner")), "agent", "bare-client",
                              staff.StaffRoot(), current_session=staff.native_id(fixture_runtime_id("bare-client-thread")))
        client = herdr.Herdr("agents")
        with (codex_launch_evidence(self.root, ["codex"]) as info,
              patch.object(client, "panes", return_value=[
                  {"pane_id": "w1:p1", "session_id": None, "workspace_id": "w1"}]),
              patch.object(client, "pane_tab", return_value=("workspace", 1, {"name": owner.id})),
              patch.object(staff, "initial_snapshot", return_value=staff.StaffSnapshot((owner,))),
              patch.object(client, "result", return_value=info)):
            entry = client.codex_launch("w1:p1")
        self.assertEqual(entry["session_id"], owner.current_session)
        self.assertEqual(entry["cwd"], str(self.root))
        self.assertEqual(entry.get("pid"), 998070)

    def test_codex_launch_removes_retired_identity_before_retaining_the_launch(self) -> None:
        identity = fixture_runtime_id("clean-codex-launch")
        observation = herdr.NativeAgentProcess(herdr.RolloutThread(identity), "same-start", {"SHOWRUNNER_UNIT": "old"})
        with (codex_launch_evidence(self.root, ["codex", "resume", identity]) as info,
              patch.object(herdr, "native_process", return_value=observation),
              patch.object(herdr, "run_program", return_value=subprocess.CompletedProcess[str](
                  [], 0, json.dumps({"result": info}), ""))):
            entry = herdr.Herdr("agents").codex_launch("w1:p1", ["resume", identity])
        self.assertEqual(entry["environment"], {})

    def test_foreground_process_without_argv_does_not_hide_the_agent(self) -> None:
        identity = fixture_runtime_id("foreground-agent")
        observation = herdr.NativeAgentProcess(herdr.RolloutThread(identity), "same-start", {"CODEX_HOME": str(self.root / ".codex2")})
        info: dict[str, object] = {"shell_pid": 998070, "foreground_process_group_id": 998075,
                "foreground_processes": [{"pid": 998074},
                    {"pid": 998075, "name": "codex", "argv": ["codex"], "cwd": str(self.root)}]}
        completed = subprocess.CompletedProcess[str]([], 0, json.dumps({"result": {"process_info": info}}), "")
        client = herdr.Herdr("agents")
        with (patch.object(herdr, "run_program", return_value=completed),
              patch.object(herdr, "native_process", return_value=observation) as native,
              native_process_commands(998075),
              patch.object(client, "panes", return_value=[
                  {"pane_id": "w1:p1", "session_id": identity, "workspace_id": "w1"}]),
              patch.object(client, "result", return_value={"process_info": info})):
            self.assertEqual(client.native_pane_process("w1:p1", "codex", 1), observation)
            startup = client.startup_processes("w1:p1", "codex", 1)
            self.assertIsInstance(startup.activity, herdr.AgentProcessPresent)
            launch = client.codex_launch("w1:p1")
            self.assertEqual(launch["session_id"], identity)
        self.assertEqual(native.call_args_list, [call(998075, "codex")] * 2)

    def test_native_pane_process_returns_the_proved_account_and_launch_identity_for_the_exact_codex_pid(self) -> None:
        current = fixture_runtime_id("pane-current-thread")
        launch = fixture_runtime_id("pane-launch-thread")
        observation = herdr.NativeAgentProcess(herdr.RolloutThread(current), "same-start", {
            "CODEX_THREAD_ID": launch, "CODEX_HOME": str(self.root / ".codex2")})
        payload = {"result": {"process_info": {"foreground_processes": [
            {"pid": 998074, "argv": ["zsh"]}, {"pid": 998075, "argv": ["codex", "resume", launch]}]}}}
        completed = subprocess.CompletedProcess[str]([], 0, json.dumps(payload), "")
        with (patch.object(herdr, "run_program", return_value=completed),
              patch.object(herdr, "native_process", return_value=observation) as native):
            observed = herdr.Herdr("agents").native_pane_process("w1:p1", "codex", 1)
        self.assertEqual(observed, observation)
        native.assert_called_once_with(998075, "codex")

    def test_held_pane_row_proves_the_native_process_and_session_without_another_pane_read(self) -> None:
        identity = fixture_runtime_id("held-pane-report")
        row = {"pane_id": "w1:p1", "agent_session": {"value": identity}, "agent_type": "codex"}
        observation = herdr.NativeAgentProcess(herdr.PaneSessionReport(identity), "same-start",
                                               {"UNIT_RUN_DIR": str(self.root)})
        payload = {"result": {"process_info": {"foreground_processes": [
            {"pid": 998075, "argv": ["codex"]}]}}}
        result = subprocess.CompletedProcess[str]([], 0, json.dumps(payload), "")
        client = herdr.Herdr("agents")
        with (patch.object(herdr, "run_program", return_value=result),
              patch.object(herdr, "native_process", return_value=observation) as native,
              patch.object(client, "panes", side_effect=AssertionError("held pane was read again"))):
            process = client.native_pane_process("w1:p1", "codex", 1, pane_row=row)
            self.assertEqual(process.current_identity, identity)
            self.assertEqual(process.environment, observation.environment)
            self.assertEqual(client.native_session("w1:p1", "codex", 1, pane_row=row), identity)
        self.assertEqual(native.call_args_list, [call(998075, "codex", identity)] * 2)

    def test_codex_native_process_proves_the_open_thread_instead_of_the_launch_thread(self) -> None:
        launch = fixture_runtime_id("native-launch-thread")
        current = fixture_runtime_id("native-replaced-thread")
        proc = self.root / "native-proc"
        descriptors = proc / "998072" / "fd"
        descriptors.mkdir(parents=True)
        rollout = self.root / "rollout-current.jsonl"
        write_text_atomic(rollout, json.dumps({"type": "session_meta", "payload": {"id": current}}) + "\n")
        (descriptors / "7").symlink_to(rollout)
        environment = {"CODEX_THREAD_ID": launch, "CODEX_HOME": str(self.root / ".codex2")}
        with (patch.dict(os.environ, {"HERDR_PROC": str(proc)}),
              patch.object(herdr, "_PLATFORM", "linux"),
              patch.object(sessions, "process_start", return_value="same-process"),
              patch.object(herdr, "process_environment", return_value=environment)):
            observed = herdr.native_process(998072, "codex")
        self.assertEqual(observed, herdr.NativeAgentProcess(herdr.RolloutThread(current), "same-process", environment))
        self.assertEqual(observed.environment["CODEX_THREAD_ID"], launch)

    def assert_target_survives_closed_descriptor(self, stage: Literal[
            "listing", "read", "final listing", "new descriptor", "launch descriptor"]) -> None:
        identity = fixture_runtime_id("descriptor-current-thread")
        other = fixture_runtime_id("descriptor-other-thread")
        proc = self.root / "descriptor-proc"
        descriptors = proc / "998081" / "fd"
        descriptors.mkdir(parents=True)
        rollout = self.root / "rollout-descriptor.jsonl"
        write_text_atomic(rollout, json.dumps({"type": "session_meta", "payload": {"id": identity}}) + "\n")
        closing, stable = descriptors / "7", descriptors / "8"
        closing.symlink_to(rollout)
        stable.symlink_to(rollout)
        readlink = os.readlink
        open_file = cast(Callable[..., TextIO], Path.open)

        def read_descriptor(path: Path) -> str:
            if path == closing and stage == "listing":
                path.unlink()
            return readlink(path)

        def read_rollout(path: Path, *args: object, **kwargs: object) -> TextIO:
            if path == closing and stage == "read":
                path.unlink()
            return open_file(path, *args, **kwargs)

        stat = Path.stat
        reads = 0

        def read_metadata(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            nonlocal reads
            value = stat(path, follow_symlinks=follow_symlinks)
            if path == closing and stage in ("final listing", "new descriptor", "launch descriptor"):
                reads += 1
                if reads == 2:
                    if stage == "final listing":
                        path.unlink()
                    else:
                        (descriptors / "9").symlink_to(rollout)
            return value

        client = herdr.Herdr("fixture")
        pane: herdr.PaneIdentity = {"pane_id": "w1:p1", "workspace_id": "w1",
                                    "terminal_id": "private-terminal", "session_id": other}
        info = {"process_info": {"shell_pid": 998080, "foreground_process_group_id": 998081,
                "foreground_processes": [{"pid": 998081, "argv": ["codex"]}]}}
        with (patch.dict(os.environ, {"HERDR_PROC": str(proc)}),
              patch.object(herdr, "_PLATFORM", "linux"),
              patch.object(sessions, "process_start", return_value="same-process"),
              patch.object(herdr, "process_environment", return_value={}),
              patch.object(client, "result", return_value=info),
              patch.object(client, "composer", return_value=herdr.ComposerInput("")),
              patch.object(os, "readlink", side_effect=read_descriptor),
              patch.object(Path, "open", read_rollout),
              patch.object(Path, "stat", read_metadata)):
            proof = herdr.launch_rollout_proof() if stage == "launch descriptor" else nullcontext()
            with proof, redirect_stderr(io.StringIO()) as output:
                allowed = herdr.check_target(identity, action="keys", client=client,
                    pane_info=pane, place=herdr.HerdrPlace("fixture", "w1:p1"))
            if stage == "new descriptor":
                self.assertFalse(allowed)
                self.assertIn("current Codex rollout or process changed", output.getvalue())
            else:
                self.assertTrue(allowed)
            with self.assertRaisesRegex(herdr.HerdrFailure, f"does not run session {other}") as refused:
                self.assertTrue(herdr.check_target(other, action="keys", client=client,
                    pane_info=pane, place=herdr.HerdrPlace("fixture", "w1:p1")))
            self.assertEqual(refused.exception.code, "wrong_target")

    def test_target_proof_survives_a_descriptor_closing_during_listing(self) -> None:
        self.assert_target_survives_closed_descriptor("listing")

    def test_target_proof_survives_a_descriptor_closing_during_read(self) -> None:
        self.assert_target_survives_closed_descriptor("read")

    def test_target_proof_survives_a_descriptor_closing_before_the_final_listing(self) -> None:
        self.assert_target_survives_closed_descriptor("final listing")

    def test_target_proof_refuses_a_new_descriptor_before_the_final_listing(self) -> None:
        self.assert_target_survives_closed_descriptor("new descriptor")

    def test_launch_proof_accepts_a_rollout_descriptor_opening_during_the_read(self) -> None:
        self.assert_target_survives_closed_descriptor("launch descriptor")

    def test_launch_argument_cannot_prove_the_current_pane_session(self) -> None:
        launch = fixture_runtime_id("native-fallback-launch")
        proc = self.root / "fallback-proc"
        (proc / "998076" / "fd").mkdir(parents=True)
        _ = (proc / "998076" / "cmdline").write_bytes(f"codex\0resume\0{launch}\0".encode())
        environment = {"CODEX_THREAD_ID": launch, "CODEX_HOME": str(self.root / ".codex2")}
        payload = {"result": {"process_info": {"foreground_processes": [
            {"pid": 998076, "argv": ["codex", "resume", launch]}]}}}
        completed = subprocess.CompletedProcess[str]([], 0, json.dumps(payload), "")
        with (patch.dict(os.environ, {"HERDR_PROC": str(proc)}),
              patch.object(herdr, "_PLATFORM", "linux"),
              patch.object(sessions, "process_start", return_value="same-process"),
              patch.object(herdr, "process_environment", return_value=environment),
              patch.object(herdr, "run_program", return_value=completed)):
            observed = herdr.native_process(998076, "codex")
            with self.assertRaisesRegex(herdr.HerdrFailure, "launch argument does not prove"):
                _ = herdr.Herdr("agents").native_session("w1:p1", "codex", 1)
        self.assertEqual(observed.identity, herdr.LaunchArgument(launch))
        self.assertEqual(observed.started, "same-process")
        self.assertEqual(observed.environment, environment)
        with self.assertRaisesRegex(ValueError, "launch argument does not prove"):
            _ = observed.current_identity

    def test_native_pane_identity_uses_its_exact_claude_pid_registry_without_an_exported_id(self) -> None:
        identity = fixture_runtime_id("herdr-native-Claude")
        registry = Path(os.environ["NOTIFIER_SESSIONS_DIR"]) / f"{os.getpid()}.json"
        observation: dict[str, object] = {"pid": os.getpid(), "sessionId": identity,
            "procStart": NATIVE_PROCESS_START, "name": "same-name",
            "messagingSocketPath": str(self.root / "private.sock"), "updatedAt": 1}
        self.write_json(registry, observation)
        payload = {"result": {"process_info": {"foreground_processes": [
            {"pid": os.getpid(), "argv": ["claude", "-n", "same-name"]}]}}}
        result = subprocess.CompletedProcess[str]([], 0, json.dumps(payload), "")
        with (patch.object(herdr, "run_program", return_value=result),
              native_process_commands(os.getpid()),
              patch.object(herdr, "process_environment", return_value={}),
              patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "", "CODEX_THREAD_ID": ""})):
            self.assertEqual(herdr.Herdr("agents").native_session("w1:p1", "claude", 1), identity)
            observation["procStart"] = "reused-process-id"
            self.write_json(registry, observation)
            with self.assertRaisesRegex(herdr.HerdrFailure, "native registry identity is unavailable"):
                _ = herdr.Herdr("agents").native_session("w1:p1", "claude", 1)

    def test_codex_native_process_refuses_a_changed_process_after_proving_its_thread(self) -> None:
        launch = fixture_runtime_id("changed-process-launch")
        current = fixture_runtime_id("changed-process-thread")
        with (patch.object(herdr, "_PLATFORM", "linux"),
              patch.object(sessions, "process_start", side_effect=["first-process", "reused-pid"]),
              patch.object(herdr, "process_environment", return_value={"CODEX_THREAD_ID": launch}),
              patch.object(herdr, "codex_rollout_thread", return_value=current)):
            with self.assertRaisesRegex(ValueError, "native process start changed"):
                _ = herdr.native_process(998073, "codex")


    def test_codex_native_process_does_not_replace_failed_rollout_proof_with_the_launch_identity(self) -> None:
        launch = fixture_runtime_id("failed-rollout-launch")
        for reason in ("current Codex rollout changed", "current Codex rollout is ambiguous",
                       "current Codex rollout is unreadable"):
            with (self.subTest(reason=reason),
                  patch.object(herdr, "_PLATFORM", "linux"),
                  patch.object(sessions, "process_start", return_value="same-process"),
                  patch.object(herdr, "process_environment", return_value={"CODEX_THREAD_ID": launch}),
                  patch.object(herdr, "codex_rollout_thread", side_effect=ValueError(reason))):
                with self.assertRaisesRegex(ValueError, reason):
                    _ = herdr.native_process(998073, "codex")


class CodexAnomalyNameTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(patch.object(codex_daemon, "read_daemons", return_value=[]))

    def test_codex_managed_daemon_distinguishes_thread_holders(self) -> None:
        managed = ["/opt/bin/codex", "app-server", "--listen", "unix://",
                   "--analytics-default-enabled", "--managed-daemon"]
        wrapped = ["node", "/opt/@openai/codex/bin/codex.js", *managed[1:]]
        for arguments, expected in ((managed, True), (wrapped, True),
                (["nodejs", *wrapped[1:]], True), (managed[:-1], False),
                (["codex", "resume", "thread"], False), ([], False),
                (["other", "app-server", "--managed-daemon"], False)):
            with self.subTest(arguments=arguments):
                self.assertEqual(herdr.codex_managed_daemon(arguments), expected)

    def test_daemon_rollouts_prove_threads_and_quiet_clients_report_unknown(self) -> None:
        proc = self.root / "proc"
        process = proc / "42"
        (process / "fd").mkdir(parents=True)
        _ = (process / "cmdline").write_bytes(b"codex\0app-server\0")
        identities = {fixture_runtime_id("thread-one"), fixture_runtime_id("thread-two")}
        for index, identity in enumerate(sorted(identities)):
            rollout = self.root / f"rollout-{identity}.jsonl"
            _ = rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": identity}}) + "\n")
            (process / "fd" / str(index)).symlink_to(rollout)
        for pid, argv in enumerate((["codex"], ["codex", "resume", "thread"],
                ["codex", "-m", "model", "-C", "/tmp", "--profile", "work", "resume", "thread"],
                ["codex", "--model=model", "-C/tmp", "--profile=work", "resume", "thread"],
                ["codex", "-c", "key=value", "app-server", "--listen", "unix://"],
                ["codex", "app-server", "daemon", "pid-update-loop"]), 43):
            (proc / str(pid) / "fd").mkdir(parents=True)
            _ = (proc / str(pid) / "cmdline").write_bytes("\0".join(argv).encode())
        with (patch.dict(os.environ, {"HERDR_PROC": str(proc)}),
              patch.object(herdr, "_PLATFORM", "linux"),
              patch.object(sessions, "process_start", return_value="fixture-start"),
              redirect_stderr(io.StringIO()) as output):
            self.assertEqual(herdr.running_codex_threads(thread_host=lambda _arguments: False), identities)
            self.assertEqual(len(output.getvalue().splitlines()), 5)
            self.assertTrue(all("Codex thread unknown" in line for line in output.getvalue().splitlines()))
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                _ = herdr.codex_rollout_thread(42)
            _ = (process / "cmdline").write_bytes(b"codex\0app-server\0--managed-daemon\0")
            live = sorted(identities)[0]
            (proc / "43" / "fd" / "0").symlink_to(self.root / f"rollout-{live}.jsonl")
            self.assertEqual(herdr.running_codex_threads(thread_host=herdr.codex_managed_daemon), {live})
            (proc / "43" / "fd" / "0").unlink()
            self.assertEqual(herdr.running_codex_threads(thread_host=herdr.codex_managed_daemon), set())
            self.assertEqual(len(output.getvalue().splitlines()), 14)
            self.assertTrue(all("Codex thread unknown" in line for line in output.getvalue().splitlines()))

    def assert_unknown_codex_note(self, source: Literal["tab", "registry", "pid"]) -> None:
        pid = 998071
        proc = self.root / "unknown-codex-proc"
        process = proc / str(pid)
        (process / "fd").mkdir(parents=True)
        _ = (process / "cmdline").write_bytes(b"codex\0--no-daemon\0")
        _ = (process / "environ").write_bytes(b"")
        records: list[sessions.SessionRecord] = [{
            "pid": pid + 1, "sessionId": fixture_runtime_id("unrelated-registry-process"),
            "name": "unrelated", "messagingSocketPath": "", "updatedAt": 1, "tmux": "", "formerNames": [],
        }, {
            "pid": pid, "sessionId": fixture_runtime_id("named-registry-process"),
            "name": "registry-name", "messagingSocketPath": "", "updatedAt": 1, "tmux": "", "formerNames": [],
        }]
        output = io.StringIO()
        with (patch.dict(os.environ, {"HERDR_PROC": str(proc), "HERDR_BIN": "/nonexistent/herdr"}),
              patch.object(herdr, "_PLATFORM", "linux"),
              patch.object(sessions, "process_start", return_value="fixture-start"),
              patch.object(herdr, "herdr_place_of") as placement,
              patch.object(herdr.Herdr, "result") as result,
              patch.object(sessions, "running_sessions", return_value=records) as registry,
              redirect_stderr(output)):
            if source == "tab":
                placement.return_value = herdr.HerdrPlace("agents", "w1:p1")
                result.side_effect = [{"panes": [{"pane_id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1"}]},
                    {"tabs": [{"tab_id": "w1:t2", "label": "other-tab", "workspace_id": "w1"},
                              {"tab_id": "w1:t1", "label": "morty", "workspace_id": "w1"}]},
                    {"workspaces": [{"workspace_id": "w1", "label": "agents"}]}]
            else:
                placement.side_effect = herdr.HerdrFailure("placement is unavailable")
            if source == "pid":
                registry.side_effect = OSError("registry is unavailable")
            with self.assertRaisesRegex(ValueError, "Codex launch identity is unavailable"):
                _ = herdr.native_process(pid, "codex")
            self.assertEqual(herdr.running_codex_threads(thread_host=lambda _argv: False), set())
            name = {"tab": "morty", "registry": "registry-name", "pid": f"pid {pid}"}[source]
            self.assertEqual(output.getvalue(), f"anomaly: {name} (Codex thread unknown); not blocking\n")
            placement.assert_called_once_with(pid)
            if source == "tab":
                self.assertNotIn(str(pid), output.getvalue())
                self.assertEqual(result.call_args_list, [call("pane", "list"), call("tab", "list"), call("workspace", "list")])
                registry.assert_not_called()
            else:
                result.assert_not_called()
                registry.assert_called_once_with()

    def test_unknown_codex_note_uses_the_tab_label_before_the_registry_name(self) -> None:
        self.assert_unknown_codex_note("tab")

    def test_unknown_codex_note_uses_the_registry_name_when_placement_is_unavailable(self) -> None:
        self.assert_unknown_codex_note("registry")

    def test_unknown_codex_note_uses_the_pid_when_both_name_readers_fail(self) -> None:
        self.assert_unknown_codex_note("pid")


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


# The probe that each transient server start runs before it gives panes their own scopes.
SCOPE_PROBE = ["--user", "--scope", "--quiet", "--collect", "--slice=app-agents.slice", "--", "true"]
PROCESS_SESSIONS = herdr._process_sessions  # pyright: ignore[reportPrivateUsage]
STARTUP_PROCESSES = herdr._startup_processes  # pyright: ignore[reportPrivateUsage]
STARTUP_PROCESS_READER = herdr._startup_process_reader  # pyright: ignore[reportPrivateUsage]


@final
class PromptSubmissionTests(unittest.TestCase):
    """Real scratch transcripts, with every pane command and elapsed second faked."""

    root: Path = Path()
    transcript: herdr.SessionTranscript = herdr.SessionTranscript(Path(), "fixture")
    path: Path = Path()
    launched_at: datetime = datetime(2026, 10, 17, tzinfo=timezone.utc)
    elapsed: float = 0
    ready_at: float = 0
    typed: list[tuple[str, str, float]] = []
    enters: list[tuple[str, tuple[str, ...]]] = []
    stashed_drafts: list[tuple[str, str]] = []
    ready_panes: list[str] = []
    screen: str = ""
    typed_screen: str = ""
    drop_before: float = 0
    drop_all: bool = False
    working_until: float = 0
    turn_delay: float = 0
    events: list[tuple[float, Callable[[], None]]] = []
    submit_on_type: bool = False
    submit_on_enter: int = 0
    client: herdr.Herdr = cast(herdr.Herdr, Mock())
    start: Mock = Mock()
    read: Mock = Mock()
    stop_key_transport: Callable[[], None] = lambda: None

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HOME": str(self.root), "HERDR_BIN": "/nonexistent/herdr"}, clear=True)))
        self.transcript = herdr.SessionTranscript(self.root / ".claude3", "moved-session")
        self.path = self.transcript.config_dir / "projects/project/moved-session.jsonl"
        self.path.parent.mkdir(parents=True)
        self.launched_at = datetime.now(timezone.utc)
        self.elapsed = 0.0
        self.ready_at = 0.0
        self.typed = []
        self.enters = []
        self.stashed_drafts = []
        self.ready_panes = []
        self.screen = "────────\n❯ \n────────"
        self.typed_screen = ""
        self.drop_before = 0
        self.drop_all = False
        self.working_until = 0
        self.turn_delay = 0
        self.events = []
        self.submit_on_type = False
        self.submit_on_enter = 0
        self.client = herdr.Herdr("agents")
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        _ = self.enterContext(patch.object(herdr, "_tab_staff", return_value=staff.initial_snapshot()))
        _ = self.enterContext(patch.object(herdr, "Herdr", return_value=self.client))
        _ = self.enterContext(patch.object(self.client, "panes", return_value=[
            {"pane_id": "destination:p1", "terminal_id": "destination-terminal", "session_id": self.transcript.session_id}]))
        _ = self.enterContext(patch.object(self.client, "pane_activity", return_value=herdr.PaneRunning(("claude",))))
        _ = self.enterContext(patch.object(self.client, "native_session", return_value=self.transcript.session_id))
        _ = self.enterContext(patch.object(self.client, "result", return_value={"process_info": {
            "shell_pid": 23455, "foreground_process_group_id": 23456,
            "foreground_processes": [{"pid": 23456, "name": "claude", "argv": ["claude"]}]}}))
        _ = self.enterContext(patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
            herdr.RolloutThread(self.transcript.session_id), "fixture-prompt-recipient", {})))
        _ = self.enterContext(patch.object(herdr, "prompt_recipient_process",
            return_value=herdr.PromptRecipientProcess(23456, "fixture-prompt-recipient")))
        _ = self.enterContext(patch.object(time, "monotonic", side_effect=lambda: self.elapsed))
        _ = self.enterContext(patch.object(time, "sleep", side_effect=self.advance))
        self.start = cast(Mock, self.enterContext(patch.object(self.client, "start_command",
            return_value=subprocess.CompletedProcess(["fixture agent start"], 0, "", ""))))
        _ = self.enterContext(patch.object(self.client, "readiness", side_effect=self.readiness))
        self.read = cast(Mock, self.enterContext(patch.object(self.client, "run", side_effect=self.read_screen)))
        _ = self.enterContext(patch.object(self.client, "_prompt", side_effect=self.type_prompt))
        key_transport = patch.object(self.client, "_send_keys", side_effect=self.enter_prompt)
        self.stop_key_transport = key_transport.stop
        _ = key_transport.start()
        self.addCleanup(key_transport.stop)

    def advance(self, seconds: float) -> None:
        self.assertGreater(seconds, 0)
        self.elapsed += seconds
        due = [event for event in self.events if event[0] <= self.elapsed]
        self.events = [event for event in self.events if event[0] > self.elapsed]
        for _at, action in due:
            action()

    def readiness(self, pane: str) -> herdr.WorkerReadiness:
        self.ready_panes.append(pane)
        ready = pane == "sibling:p1" or self.elapsed >= self.ready_at
        status = herdr.AgentStatus.WORKING if self.elapsed < self.working_until else herdr.AgentStatus.IDLE
        return {"agent_status": status if ready else herdr.AgentStatus.STARTING, "interactive_ready": ready}

    def read_screen(self, *arguments: str) -> str:
        self.assertIn(arguments, {("pane", "read", "destination:p1", "--source", "visible"),
                                  ("pane", "read", "destination:p1", "--source", "visible", "--format", "ansi")})
        return self.screen

    def write_turn(self, text: str, *, kind: str = "user", meta: bool = False,
                   seconds_after_launch: float = 1, blocks: bool = False) -> None:
        text = script_prompt(text)
        content: object = [{"type": "text", "text": text}] if blocks else text
        row = {"type": kind, "isMeta": meta,
               "timestamp": (self.launched_at + timedelta(seconds=seconds_after_launch)).isoformat(),
               "message": {"role": kind, "content": content}}
        with self.path.open("a") as stream:
            _ = stream.write(json.dumps(row) + "\n")

    def type_prompt(self, pane: str, text: str) -> None:
        self.typed.append((pane, text, self.elapsed))
        if self.drop_all or self.elapsed < self.drop_before:
            return
        if self.submit_on_type:
            self.write_turn(text, seconds_after_launch=self.elapsed + 1)
            self.screen = "────────\n❯ \n────────"
        elif self.turn_delay:
            self.events.append((self.elapsed + self.turn_delay,
                lambda: self.write_turn(text, seconds_after_launch=self.elapsed + 1)))
            self.screen = "────────\n❯ \n────────"
        else:
            self.screen = self.typed_screen or f"────────\n❯ {text}\n────────"

    def enter_prompt(self, pane: str, *keys: str) -> None:
        if keys == ("ctrl+s",):
            self.stashed_drafts.append((pane, self.screen))
            self.screen = "────────\n❯ \n────────"
            return
        self.enters.append((pane, keys))
        if len(self.enters) == self.submit_on_enter:
            self.write_turn(self.typed[-1][1], seconds_after_launch=self.elapsed + 1)
            self.screen = "────────\n❯ \n────────"

    def submit(self, timeout_ms: int = 30_000, *, text: str = "resume the saved work") -> herdr.PromptSubmitOutcome:
        return self.client.submit_opening_prompt("destination:p1",
            herdr.OpeningPrompt(text, self.transcript), self.launched_at, timeout_ms)

    @contextmanager
    def unreadable_transcript(self, from_second: float, until_second: float) -> Generator[list[float]]:
        original_open = Path.open
        failures: list[float] = []

        @contextmanager
        def open_transcript(path: Path, mode: str) -> Generator[BinaryIO]:
            if path == self.path and mode == "rb" and from_second <= self.elapsed < until_second:
                failures.append(self.elapsed)
                raise PermissionError("scratch transcript temporarily unreadable")
            with original_open(path, mode) as stream:
                yield cast(BinaryIO, stream)

        with patch.object(Path, "open", open_transcript):
            yield failures

    def test_opening_prompt_stashes_a_draft_once_before_typing_and_submission(self) -> None:
        draft = "────────\n❯ keep this draft\n────────"
        self.screen = draft
        self.submit_on_enter = 1
        outcome = self.submit()
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(self.stashed_drafts, [("destination:p1", draft)])
        self.assertEqual([(pane, text) for pane, text, _at in self.typed],
                         [("destination:p1", script_prompt("resume the saved work"))])
        self.assertEqual(self.enters, [("destination:p1", ("Enter",))])

    def test_proved_submission_returns_without_waiting_for_the_turn_or_restoring_the_stash(self) -> None:
        text = "Resume the saved work.\nKeep the user's draft."
        for draft_text in ("keep this draft", "[Pasted text #9 +2 lines]"):
            draft = f"────────\n❯ {draft_text}\n────────"
            for composer_text in ("", draft_text, script_prompt(text), "[Pasted text #1 +4 lines]"):
                with self.subTest(draft=draft_text, composer=composer_text):
                    self.elapsed = 0
                    self.screen = draft
                    self.stashed_drafts.clear()
                    self.path.unlink(missing_ok=True)
                    self.submit_on_type = True
                    self.working_until = 60
                    submitted_screen = f"────────\n❯ {composer_text}\n────────"

                    def submit_with_composer(pane: str, prompt: str) -> None:
                        self.type_prompt(pane, prompt)
                        self.screen = submitted_screen

                    with patch.object(self.client, "_prompt", side_effect=submit_with_composer):
                        outcome = self.submit(text=text)
                    self.assertIsInstance(outcome, herdr.PromptSubmitted)
                    self.assertEqual(self.stashed_drafts, [("destination:p1", draft)])
                    self.assertEqual(self.enters, [])
                    self.assertEqual(self.elapsed, 0.1)
                    self.assertEqual(self.screen, submitted_screen)

    def test_a_second_draft_waits_until_the_deadline_without_a_second_stash(self) -> None:
        draft = "────────\n❯ keep this draft\n────────"
        second_draft = "────────\n❯ keep this second draft\n────────"
        self.screen = draft
        self.events.append((0.1, lambda: setattr(self, "screen", second_draft)))
        outcome = self.submit(timeout_ms=1000)
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        self.assertEqual(cast(herdr.PromptNotSubmitted, outcome).reason,
                         "destination never became ready for input")
        self.assertEqual(cast(herdr.PromptNotSubmitted, outcome).screen, second_draft)
        self.assertEqual(self.stashed_drafts, [("destination:p1", draft)])
        self.assertEqual(self.typed, [])
        self.assertEqual(self.enters, [])
        self.assertEqual(self.elapsed, 1)

    def test_shared_tab_lost_enter_is_retried_once_without_typing_the_prompt_twice(self) -> None:
        self.submit_on_enter = 1
        _ = self.enterContext(patch.object(self.client, "native_session", return_value=self.transcript.session_id))
        outcome = self.client.start_agent("moved-session", "destination:p1", "claude",
            ["--resume", "moved-session"], herdr.OpeningPrompt("resume the saved work", self.transcript), 30_000)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(cast(herdr.PromptSubmitted, outcome).process,
                         herdr.PromptRecipientProcess(23456, "fixture-prompt-recipient"))
        self.assertEqual([(pane, text) for pane, text, _at in self.typed],
                         [("destination:p1", script_prompt("resume the saved work"))])
        self.assertEqual(self.enters, [("destination:p1", ("Enter",))])
        cast(Mock, self.start).assert_called_once()
        self.assertEqual(set(self.ready_panes), {"destination:p1"})
        self.assertGreaterEqual(cast(Mock, self.read).call_count, 2)

    def test_slash_command_recorded_as_command_tags_is_submitted_once(self) -> None:
        text = "/showrunner:produce /x/doc.md resume"
        recorded = ("<command-message>showrunner:produce</command-message>\n"
                    "<command-name>/showrunner:produce</command-name>\n"
                    "<command-args>/x/doc.md resume</command-args>")
        self.drop_all = True
        self.events.append((8, lambda: self.write_turn(recorded, seconds_after_launch=8)))
        outcome = self.submit(timeout_ms=30_000, text=text)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(cast(herdr.PromptSubmitted, outcome).at, self.launched_at + timedelta(seconds=8))
        self.assertEqual(self.typed, [("destination:p1", text, 0)])
        self.assertEqual(self.enters, [])

    def test_slash_command_recorded_as_raw_text_is_submitted_once(self) -> None:
        text = "/showrunner:produce /x/doc.md resume"
        self.drop_all = True
        self.events.append((8, lambda: self.write_turn(text, seconds_after_launch=8)))
        outcome = self.submit(timeout_ms=30_000, text=text)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(cast(herdr.PromptSubmitted, outcome).at, self.launched_at + timedelta(seconds=8))
        self.assertEqual(self.typed, [("destination:p1", text, 0)])
        self.assertEqual(self.enters, [])

    def test_real_recorded_showrunner_resume_row_is_submitted_once(self) -> None:
        self.launched_at = datetime.fromisoformat("2026-10-18T04:38:39+00:00")
        text = "/showrunner:produce /home/natepiano/rust/cargo-liner-style/docs/cargo-liner-style-production.md resume"
        row = {"type": "user", "timestamp": "2026-10-18T04:38:44.088Z", "message": {"role": "user", "content": "<command-message>showrunner:produce</command-message>\n<command-name>/showrunner:produce</command-name>\n<command-args>/home/natepiano/rust/cargo-liner-style/docs/cargo-liner-style-production.md resume</command-args>"}}
        self.drop_all = True
        def record_turn() -> None:
            _ = self.path.write_text(json.dumps(row) + "\n")
        self.events.append((5.088, record_turn))
        outcome = self.submit(timeout_ms=30_000, text=text)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(cast(herdr.PromptSubmitted, outcome).at,
                         datetime.fromisoformat("2026-10-18T04:38:44.088+00:00"))
        self.assertEqual(self.typed, [("destination:p1", text, 0)])
        self.assertEqual(self.enters, [])

    PRODUCE = "/showrunner:produce /x/doc.md resume"

    def write_rejection(self, command: str) -> None:
        """Claude's answer to a command its list lacks, as session cbc14765 recorded it on 2026-10-21."""
        at = (self.launched_at + timedelta(seconds=self.elapsed + 1)).isoformat()
        with self.path.open("a") as stream:
            for content in (f"Unknown command: {command}", "Args from unknown skill: /x/doc.md resume"):
                _ = stream.write(json.dumps({"type": "system", "subtype": "informational", "content": content,
                                             "isMeta": False, "level": "warning", "timestamp": at}) + "\n")

    def submit_command(self, accept: Callable[[str, int], bool], timeout_ms: int = 90_000) -> herdr.PromptSubmitOutcome:
        """Type into a session that answers `Unknown command` unless `accept(text, typing)` takes the text."""
        def type_text(pane: str, text: str) -> None:
            self.typed.append((pane, text, self.elapsed))
            if accept(text, len(self.typed)):
                self.write_turn(text, seconds_after_launch=self.elapsed + 1)
            else:
                self.write_rejection(text.split()[0])
        with patch.object(self.client, "_prompt", side_effect=type_text):
            return self.submit(timeout_ms, text=self.PRODUCE)

    def command_file(self) -> Path:
        path = self.transcript.config_dir / "commands/showrunner/produce.md"
        path.parent.mkdir(parents=True)
        _ = path.write_text("# Produce\n")
        return path

    def test_slash_command_matching_requires_the_name_arguments_and_non_meta_user(self) -> None:
        for text, content, kind, meta, matches in (
            ("/check", "<command-name>/check</command-name>", "user", False, True),
            ("/check   /x/doc.md resume  ", "<command-name>/check</command-name>\n<command-args>/x/doc.md resume</command-args>", "user", False, True),
            ("/check /x/doc.md", "<command-name>/other</command-name>\n<command-args>/x/doc.md</command-args>", "user", False, False),
            ("/check /x/doc.md", "<command-name>/check</command-name>\n<command-args>/x/other.md</command-args>", "user", False, False),
            ("/check /x/doc.md", "<command-name>/check</command-name>", "user", False, False),
            ("/check", "<command-name>/check</command-name>", "user", True, False),
            ("/check", "<command-name>/check</command-name>", "assistant", False, False),
        ):
            with self.subTest(text=text, content=content, kind=kind, meta=meta):
                _ = self.path.write_text("")
                self.write_turn(content, kind=kind, meta=meta)
                outcome = herdr.submitted_prompt(self.transcript, text, self.launched_at)
                self.assertEqual(isinstance(outcome, herdr.PromptSubmitted), matches)

    def test_final_read_finds_a_turn_after_the_last_deadline_poll(self) -> None:
        self.drop_all = True
        def screen_after_poll(*arguments: str) -> str:
            if self.elapsed >= 5:
                self.write_turn("resume the saved work", seconds_after_launch=5)
            return self.read_screen(*arguments)
        with patch.object(self.client, "run", side_effect=screen_after_poll):
            outcome = self.submit(timeout_ms=5_000)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(cast(herdr.PromptSubmitted, outcome).at, self.launched_at + timedelta(seconds=5))
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])

    def test_final_read_also_precedes_a_non_deadline_give_up(self) -> None:
        self.drop_all = True
        def screen_after_poll(*arguments: str) -> str:
            if self.elapsed >= 5:
                self.write_turn("resume the saved work", seconds_after_launch=5)
                self.screen = "────────\n❯ another task\n────────"
            return self.read_screen(*arguments)
        with patch.object(self.client, "run", side_effect=screen_after_poll):
            outcome = self.submit()
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])

    def test_final_read_finds_a_turn_when_the_screen_read_fails_after_the_last_poll(self) -> None:
        self.drop_all = True
        def screen_after_poll(*arguments: str) -> str:
            if self.elapsed >= 5:
                self.write_turn("resume the saved work", seconds_after_launch=5)
                raise OSError("scratch pane read failed")
            return self.read_screen(*arguments)
        with patch.object(self.client, "run", side_effect=screen_after_poll):
            outcome = self.submit()
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])

    def test_lost_prompt_is_typed_again_after_loading_finishes(self) -> None:
        self.drop_before = 7
        self.submit_on_type = True
        outcome = self.submit(timeout_ms=90_000)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual([(pane, text) for pane, text, _at in self.typed],
                         [("destination:p1", script_prompt("resume the saved work"))] * 2)
        self.assertEqual(self.typed[0][2], 0)
        self.assertGreaterEqual(self.typed[1][2], 10)
        self.assertEqual(self.enters, [])

    def test_unreadable_accepted_turn_blocks_retyping_until_the_deadline(self) -> None:
        self.turn_delay = 8
        with self.unreadable_transcript(8, 20) as failures:
            outcome = self.submit(timeout_ms=16_000)
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        failure = cast(herdr.PromptNotSubmitted, outcome)
        self.assertIn("transcript unreadable", failure.reason)
        self.assertIn("prompt typed 1 times", failure.reason)
        self.assertEqual(failure.screen, self.screen)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])
        self.assertEqual(self.elapsed, 16)
        self.assertTrue(any(at >= 10 for at in failures))
        self.assertIsInstance(herdr.submitted_prompt(self.transcript, "resume the saved work", self.launched_at),
                              herdr.PromptSubmitted)

    def test_unreadable_accepted_turn_is_found_after_reads_recover_without_retyping(self) -> None:
        self.turn_delay = 8
        with self.unreadable_transcript(8, 13) as failures:
            outcome = self.submit(timeout_ms=30_000)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])
        self.assertTrue(failures)
        self.assertGreaterEqual(self.elapsed, 13)
        self.assertLess(self.elapsed, 30)

    def test_transcript_becoming_unreadable_at_the_final_check_blocks_retyping(self) -> None:
        self.drop_all = True
        self.write_turn("older context", kind="assistant", seconds_after_launch=-1)
        recipient = herdr.PromptRecipientProcess(23456, "same-recipient")

        def observe_recipient(_session: str) -> herdr.PromptRecipientProcess:
            if self.elapsed >= 10:
                self.write_turn("resume the saved work", seconds_after_launch=self.elapsed + 1)
            return recipient

        # Read errors start only after the recipient check at the retry boundary.
        original_open = Path.open
        failures: list[float] = []
        blocked = False

        def recipient_and_block_reads(session: str) -> herdr.PromptRecipientProcess:
            nonlocal blocked
            result = observe_recipient(session)
            blocked = self.elapsed >= 10
            return result

        @contextmanager
        def open_transcript(path: Path, mode: str) -> Generator[BinaryIO]:
            if path == self.path and mode == "rb" and blocked:
                failures.append(self.elapsed)
                raise PermissionError("scratch final transcript check unreadable")
            with original_open(path, mode) as stream:
                yield cast(BinaryIO, stream)

        with (patch.object(herdr, "prompt_recipient_process", side_effect=recipient_and_block_reads),
              patch.object(Path, "open", open_transcript)):
            outcome = self.submit(timeout_ms=16_000)
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        self.assertIn("transcript unreadable", cast(herdr.PromptNotSubmitted, outcome).reason)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])
        self.assertTrue(failures)
        self.assertEqual(failures[0], 10)
        self.assertEqual(self.elapsed, 16)

    def test_missing_composer_during_loading_recovers_and_retypes_the_lost_prompt(self) -> None:
        self.drop_before = 7
        self.submit_on_type = True
        self.events.append((1, lambda: setattr(self, "screen", "Restoring conversation")))
        self.events.append((7, lambda: setattr(self, "screen", "────────\n❯ \n────────")))
        outcome = self.submit(timeout_ms=90_000)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 2)
        self.assertEqual(self.typed[0][2], 0)
        self.assertGreaterEqual(self.typed[1][2], 10)
        self.assertEqual(self.enters, [])

    def test_accepted_prompt_with_a_turn_eight_seconds_late_is_never_typed_again(self) -> None:
        self.turn_delay = 8
        cursors: list[herdr.TranscriptCursor] = []
        original_read = herdr.TranscriptCursor.read_turns

        def read_turns(cursor: herdr.TranscriptCursor) -> list[tuple[datetime, dict[str, object]]]:
            cursors.append(cursor)
            return original_read(cursor)

        with patch.object(herdr.TranscriptCursor, "read_turns", autospec=True, side_effect=read_turns):
            outcome = self.submit(timeout_ms=90_000)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertGreaterEqual(self.elapsed, 8)
        self.assertEqual(self.enters, [])
        self.assertGreater(len(cursors), 2)
        self.assertTrue(all(cursor is cursors[0] for cursor in cursors))

    def test_accepted_enter_with_a_turn_eight_seconds_late_is_not_retyped(self) -> None:
        def queued_enter(pane: str, *keys: str) -> None:
            self.enters.append((pane, keys))
            self.screen = "────────\n❯ \n────────"
            self.events.append((self.elapsed + 8,
                lambda: self.write_turn("resume the saved work", seconds_after_launch=self.elapsed + 1)))

        with patch.object(self.client, "_send_keys", side_effect=queued_enter):
            self.assertIsInstance(self.submit(), herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [("destination:p1", ("Enter",))])
        self.assertGreaterEqual(self.elapsed, 13)

    def test_working_session_with_a_queued_prompt_waits_for_its_twenty_second_turn(self) -> None:
        self.turn_delay = 20
        self.working_until = 25
        outcome = self.submit(timeout_ms=90_000)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertGreaterEqual(self.elapsed, 20)
        self.assertEqual(self.enters, [])

    def test_visible_empty_composer_is_required_even_when_herdr_reports_ready(self) -> None:
        self.screen = "Loading the saved conversation"
        self.events.append((4, lambda: setattr(self, "screen", "────────\n❯ \n────────")))
        self.submit_on_type = True
        outcome = self.submit()
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertGreaterEqual(self.typed[0][2], 4)

    def test_other_text_in_the_composer_is_never_overwritten(self) -> None:
        self.screen = "────────\n❯ the user is editing this\n────────"
        with patch.object(self.client, "_send_keys", side_effect=herdr.HerdrFailure("draft stash unavailable")):
            self.assertIsInstance(self.submit(timeout_ms=1_000), herdr.PromptNotSubmitted)
        self.assertEqual((self.typed, self.enters), ([], []))
        self.assertEqual(self.screen, "────────\n❯ the user is editing this\n────────")

    def test_other_text_appearing_after_a_lost_prompt_blocks_retyping(self) -> None:
        self.drop_all = True
        self.events.append((3, lambda: setattr(self, "screen", "────────\n❯ another task\n────────")))
        outcome = self.submit(timeout_ms=30_000)
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])

    def test_changed_recipient_blocks_retyping_a_lost_prompt(self) -> None:
        self.drop_all = True
        original = herdr.PromptRecipientProcess(23456, "first-recipient")
        replacement = herdr.PromptRecipientProcess(34567, "replacement-process")
        def observe_recipient(_session: str) -> herdr.PromptRecipientProcess:
            return original if self.elapsed < 6 else replacement

        with patch.object(herdr, "prompt_recipient_process", side_effect=observe_recipient):
            outcome = self.submit(timeout_ms=90_000)
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        self.assertIn("recipient changed", cast(herdr.PromptNotSubmitted, outcome).reason)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])

    def test_idle_pane_without_interactive_ready_or_remote_control_receives_prompt(self) -> None:
        self.submit_on_type = True
        _ = self.path.write_text("")
        result = self.client.result

        def observe(*arguments: str) -> dict[str, object]:
            if arguments == ("agent", "get", "destination:p1"):
                return {"agent": {"agent_status": "idle"}}
            return result(*arguments)

        def readiness(pane: str) -> herdr.WorkerReadiness:
            return type(self.client).readiness(self.client, pane)

        with (patch.object(self.client, "result", side_effect=observe),
              patch.object(self.client, "readiness", side_effect=readiness)):
            self.assertIsInstance(self.submit(), herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])

    def test_ready_session_still_requires_a_visible_empty_composer(self) -> None:
        self.screen = "Restoring conversation"
        self.events.append((2, lambda: setattr(self, "screen", "────────\n❯ \n────────")))
        self.submit_on_type = True
        self.assertIsInstance(self.submit(), herdr.PromptSubmitted)
        self.assertGreaterEqual(self.typed[0][2], 2)

    def test_idle_pane_waits_for_a_visible_empty_composer(self) -> None:
        self.screen = "Restoring conversation"
        self.events.append((2, lambda: setattr(self, "screen", "────────\n❯ \n────────")))
        self.submit_on_type = True
        self.assertIsInstance(self.submit(), herdr.PromptSubmitted)
        self.assertGreaterEqual(self.typed[0][2], 2)

    def test_late_turn_during_the_final_recipient_check_prevents_retyping(self) -> None:
        self.drop_all = True
        recipient = herdr.PromptRecipientProcess(23456, "same-recipient")

        def observe_recipient(_session: str) -> herdr.PromptRecipientProcess:
            if self.elapsed >= 10:
                self.write_turn("resume the saved work", seconds_after_launch=self.elapsed + 1)
            return recipient

        with patch.object(herdr, "prompt_recipient_process", side_effect=observe_recipient):
            self.assertIsInstance(self.submit(), herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])

    def test_failure_carries_the_last_screen_without_ansi_and_only_its_last_forty_lines(self) -> None:
        self.drop_all = True
        failed_screen = "\n".join(f"\x1b[31mloading row {index}\x1b[0m" for index in range(45))
        self.events.append((1, lambda: setattr(self, "screen", failed_screen)))
        outcome = self.submit()
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        failure = cast(herdr.PromptNotSubmitted, outcome)
        self.assertEqual(failure.screen, "\n".join(f"loading row {index}" for index in range(5, 45)))
        self.assertIn("1", failure.reason)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(herdr.decode_prompt_outcome(herdr.encode_prompt_outcome(failure)), failure)

    def test_not_submitted_screen_round_trips_and_old_records_keep_the_not_read_state(self) -> None:
        failure = herdr.PromptNotSubmitted("lost after one typing", "────────\n❯ \n────────")
        encoded = herdr.encode_prompt_outcome(failure)
        self.assertEqual(json.loads(encoded), {"kind": "not submitted", "reason": failure.reason,
                                               "screen": failure.screen})
        self.assertEqual(herdr.decode_prompt_outcome(encoded), failure)
        legacy = herdr.PromptNotSubmitted("old failure")
        self.assertIsInstance(legacy.screen, herdr.PromptScreenNotRead)
        self.assertNotIn("screen", cast(dict[str, object], json.loads(herdr.encode_prompt_outcome(legacy))))
        self.assertEqual(herdr.decode_prompt_outcome('{"kind":"not submitted","reason":"old failure"}'), legacy)
        self.assertEqual(herdr.decode_prompt_outcome(herdr.encode_prompt_outcome(
            herdr.PromptNotSubmitted("empty visible screen", ""))),
            herdr.PromptNotSubmitted("empty visible screen", ""))

    def test_prelaunch_cursor_skips_complete_history_without_parsing_json(self) -> None:
        self.write_turn("old context", kind="assistant", seconds_after_launch=-1)
        cursor = herdr.TranscriptCursor(self.transcript, self.launched_at)
        with patch.object(json, "loads", side_effect=AssertionError("historical JSON read")):
            cursor.skip_existing_turns()
        self.assertEqual(cursor.offsets[self.path], self.path.stat().st_size)
        self.assertEqual(cursor.read_turns(), [])
        self.write_turn("resume the saved work")
        self.assertIsInstance(cursor.submitted_prompt("resume the saved work"), herdr.PromptSubmitted)

    def test_prelaunch_cursor_keeps_an_incomplete_final_row(self) -> None:
        text = "resume the saved work – saved context"
        row = (json.dumps({
            "type": "user",
            "timestamp": (self.launched_at + timedelta(seconds=1)).isoformat(),
            "message": {"role": "user", "content": script_prompt(text)},
        }, ensure_ascii=False) + "\n").encode()
        split = row.index("–".encode()) + 1
        _ = self.path.write_bytes(row[:split])
        cursor = herdr.TranscriptCursor(self.transcript, self.launched_at)
        cursor.skip_existing_turns()
        self.assertEqual(cursor.offsets[self.path], 0)
        with self.path.open("ab") as stream:
            _ = stream.write(row[split:])
        self.assertIsInstance(cursor.submitted_prompt(text), herdr.PromptSubmitted)

    def test_prompt_submission_keeps_the_prelaunch_cursor(self) -> None:
        self.write_turn("old context", kind="assistant", seconds_after_launch=-1)
        cursor = herdr.TranscriptCursor(self.transcript, self.launched_at)
        cursor.skip_existing_turns()
        before = cursor.offsets[self.path]
        self.submit_on_type = True
        result = self.client.submit_opening_prompt(
            "destination:p1", herdr.OpeningPrompt("resume the saved work", self.transcript, cursor=cursor),
            self.launched_at, 1000,
        )
        self.assertIsInstance(result, herdr.PromptSubmitted)
        self.assertGreater(cursor.offsets[self.path], before)

    def test_transcript_cursor_reads_only_appended_bytes_after_its_first_poll(self) -> None:
        self.write_turn("old context " * 1000, kind="assistant", seconds_after_launch=-1)
        first_size = self.path.stat().st_size
        cursor = herdr.TranscriptCursor(self.transcript, self.launched_at)
        positions: list[int] = []
        original_open = Path.open

        @contextmanager
        def traced_open(path: Path, mode: str) -> Generator[BinaryIO]:
            self.assertEqual(mode, "rb")
            with original_open(path, mode) as raw:
                stream = cast(BinaryIO, raw)
                tracked = Mock(wraps=stream)

                def readline() -> bytes:
                    positions.append(stream.tell())
                    return stream.readline()

                cast(Mock, tracked.readline).side_effect = readline
                yield cast(BinaryIO, tracked)

        with patch.object(Path, "open", traced_open):
            self.assertIsInstance(cursor.submitted_prompt("resume the saved work"), herdr.PromptNotSubmitted)
        self.assertEqual(positions[0], 0)
        self.write_turn("resume the saved work")
        positions.clear()
        with patch.object(Path, "open", traced_open):
            self.assertIsInstance(cursor.submitted_prompt("resume the saved work"), herdr.PromptSubmitted)
        self.assertEqual(positions[0], first_size)
        self.assertTrue(all(position >= first_size for position in positions))
        positions.clear()
        with patch.object(Path, "open", traced_open):
            self.assertIsInstance(cursor.submitted_prompt("resume the saved work"), herdr.PromptSubmitted)
        self.assertEqual(positions, [self.path.stat().st_size])

    def test_transcript_readability_requires_every_matching_file_and_recovers(self) -> None:
        cursor = herdr.TranscriptCursor(self.transcript, self.launched_at)
        self.assertEqual(cursor.read_turns(), [])
        self.assertIsInstance(cursor.readability, herdr.TranscriptReadable)
        self.write_turn("older context", kind="assistant", seconds_after_launch=-1)
        second = self.transcript.config_dir / "projects/other/moved-session.jsonl"
        second.parent.mkdir()
        _ = second.write_bytes(b"{}\n")
        with self.unreadable_transcript(0, 1) as failures:
            self.assertEqual(cursor.read_turns(), [])
            self.assertIsInstance(cursor.readability, herdr.TranscriptUnreadable)
        self.assertTrue(failures)
        self.assertIn(second, cursor.offsets)
        self.assertEqual(cursor.read_turns(), [])
        self.assertIsInstance(cursor.readability, herdr.TranscriptReadable)

    def test_transcript_cursor_waits_for_a_newline_and_a_complete_utf8_row(self) -> None:
        self.write_turn("resume the saved work – saved context")
        row = (json.dumps(json.loads(self.path.read_bytes()), ensure_ascii=False) + "\n").encode()
        split = row.index("–".encode()) + 1
        _ = self.path.write_bytes(row[:split])
        cursor = herdr.TranscriptCursor(self.transcript, self.launched_at)
        self.assertIsInstance(cursor.submitted_prompt("resume the saved work"), herdr.PromptNotSubmitted)
        with self.path.open("ab") as stream:
            _ = stream.write(row[split:-1])
        self.assertIsInstance(cursor.submitted_prompt("resume the saved work"), herdr.PromptNotSubmitted)
        with self.path.open("ab") as stream:
            _ = stream.write(b"\n")
        self.assertIsInstance(cursor.submitted_prompt("resume the saved work"), herdr.PromptSubmitted)
        self.assertEqual(len(cursor.read_turns()), 1)

    def test_transcript_cursor_restarts_after_shrink_and_keeps_offsets_per_project(self) -> None:
        self.write_turn("historical context " * 100, kind="assistant", seconds_after_launch=-1)
        cursor = herdr.TranscriptCursor(self.transcript, self.launched_at)
        self.assertIsInstance(cursor.submitted_prompt("resume the saved work"), herdr.PromptNotSubmitted)
        _ = self.path.write_bytes(b"")
        self.write_turn("resume the saved work")
        self.assertIsInstance(cursor.submitted_prompt("resume the saved work"), herdr.PromptSubmitted)
        # A replacement file cannot retain submit proof from rows it no longer contains.
        _ = self.path.write_bytes(b"{}\n")
        self.assertIsInstance(cursor.submitted_prompt("resume the saved work"), herdr.PromptNotSubmitted)
        second = self.transcript.config_dir / "projects/other/moved-session.jsonl"
        second.parent.mkdir()
        self.write_turn("resume the saved work")
        _ = second.write_bytes(self.path.read_bytes())
        _ = self.path.write_bytes(b"{}\n")
        self.assertIsInstance(cursor.submitted_prompt("resume the saved work"), herdr.PromptSubmitted)

    def test_empty_and_quoted_composers_share_the_readiness_parser(self) -> None:
        cases = (
            ("─── named composer ─\n❯ \xa0\n────────\n  footer", True, False),
            ("─── named composer ─\n❯ resume the saved work\n  ---\n  > quoted line\n────────\n  footer", False, True),
            ("────────\n  > resume the saved work\n────────", False, False),
            ("────────\n❯ resume the saved work\n────────\nAssistant output", False, False),
            # A new session's faint hint is not input, but coloured or bold typed text is.
            ("────────\r\n❯\xa0\x1b[0m\x1b[2mTry \"fix lint errors\"\x1b[0m\r\n────────\r\n  footer", True, False),
            ("────────\n❯ \x1b[38;2;200;100;50mresume the saved work\x1b[0m\n────────", False, True),
            ("────────\n❯ \x1b[1;2mhint\x1b[22mresume the saved work\x1b[0m\n────────", False, True))
        for screen, empty, contains in cases:
            with self.subTest(screen=screen):
                self.screen = screen
                self.assertEqual(self.client.prompt_input_empty("destination:p1"), empty)
                self.assertEqual(self.client.prompt_in_input("destination:p1", "resume the saved work"), contains)
                state = self.client.prompt_input_state("destination:p1")
                self.assertEqual(isinstance(state, herdr.PromptInputReady), empty)

    def test_prompt_is_typed_only_after_the_destination_becomes_ready_ten_seconds_later(self) -> None:
        self.ready_at = 10
        self.submit_on_type = True
        self.assertIsInstance(self.submit(), herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertGreaterEqual(self.typed[0][2], 10)
        self.assertEqual(self.enters, [])

    def test_meta_peer_turn_and_old_user_turn_cannot_prove_the_opening_prompt(self) -> None:
        self.write_turn("resume the saved work", meta=True)
        self.write_turn("resume the saved work", seconds_after_launch=-1)
        self.write_turn("resume the saved work", kind="assistant")
        self.write_turn("another task")
        self.drop_all = True
        self.assertIsInstance(self.submit(), herdr.PromptNotSubmitted)
        self.assertEqual(self.enters, [])

    def test_only_a_matching_new_user_turn_in_the_destination_transcript_proves_submit(self) -> None:
        sibling = self.path.with_name("sibling.jsonl")
        self.write_turn("resume the saved work", blocks=True)
        _ = self.path.rename(sibling)
        self.assertIsInstance(herdr.submitted_prompt(self.transcript, "resume the saved work", self.launched_at),
                              herdr.PromptNotSubmitted)
        self.write_turn("context before\nresume the saved work\ncontext after", blocks=True)
        proof = herdr.submitted_prompt(self.transcript, "resume the saved work", self.launched_at)
        self.assertEqual(proof, herdr.PromptSubmitted(self.launched_at + timedelta(seconds=1)))

    def test_a_ready_sibling_tab_never_makes_the_destination_ready(self) -> None:
        self.ready_at = 60
        outcome = self.submit(timeout_ms=1_000)
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        self.assertEqual(set(self.ready_panes), {"destination:p1"})
        self.assertEqual((self.typed, self.enters), ([], []))
        self.assertLessEqual(self.elapsed, 1)

    def test_prompt_only_in_scrollback_is_never_flushed_with_enter(self) -> None:
        self.drop_all = True
        self.screen = "❯ resume the saved work\nPrevious response\n────────\n❯ \n────────"
        self.assertIsInstance(self.submit(), herdr.PromptNotSubmitted)
        self.assertTrue(1 <= len(self.typed) <= 3)
        self.assertEqual(self.enters, [])

    def test_an_old_bordered_prompt_above_a_new_unbordered_input_is_scrollback(self) -> None:
        self.screen = "────────\n❯ resume the saved work\n────────\nPrevious response\n❯ "
        self.assertIsInstance(self.submit(), herdr.PromptNotSubmitted)
        self.assertEqual(self.typed, [])
        self.assertEqual(self.enters, [])

    def test_old_bordered_prompt_above_assistant_output_without_a_new_marker_gets_no_enter(self) -> None:
        self.screen = "────────\n❯ resume the saved work\n────────\nAssistant response is streaming"
        self.assertIsInstance(self.submit(), herdr.PromptNotSubmitted)
        self.assertEqual(self.typed, [])
        self.assertEqual(self.enters, [])

    def test_assistant_output_with_a_footer_prefix_does_not_make_scrollback_live_input(self) -> None:
        self.screen = ("────────\n❯ resume the saved work\n────────\n"
                       "ctrl+c cancels the operation described above")
        self.assertFalse(self.client.prompt_in_input("destination:p1", "resume the saved work"))
        self.assertIsInstance(self.submit(), herdr.PromptNotSubmitted)
        self.assertEqual(self.typed, [])
        self.assertEqual(self.enters, [])

    def test_bottom_composer_with_complete_footer_rows_still_gets_its_enter_retry(self) -> None:
        self.typed_screen = ("Assistant response above the composer\n────────\n❯ " + script_prompt("resume the saved work") + "\n────────\n"
                       + "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
                       + "  ? for shortcuts · ctrl+c to interrupt\n")
        self.submit_on_enter = 1
        self.assertIsInstance(self.submit(), herdr.PromptSubmitted)
        self.assertEqual(self.enters, [("destination:p1", ("Enter",))])

    def test_prompt_wrapped_inside_a_word_gets_one_enter_then_submission(self) -> None:
        self.typed_screen = '────────\n❯ <system-reminder source="script"> resume the sav\ned work </system-reminder>\n────────'
        self.submit_on_enter = 1
        self.assertIsInstance(self.submit(), herdr.PromptSubmitted)
        self.assertEqual([(pane, text) for pane, text, _at in self.typed],
                         [("destination:p1", script_prompt("resume the saved work"))])
        self.assertEqual(self.enters, [("destination:p1", ("Enter",))])

    @staticmethod
    def real_composer_screen(top_rule: str, *, marker_gap: str = " ", status_suffix: str = "",
                             mode: str = "⏵⏵ auto mode on · 1 shell",
                             input_text: str = "resume the saved work") -> str:
        input_text = script_prompt(input_text)
        # The test uses a generated composer and reads no live pane.
        return ("● Finished the previous task\n"
                "✻ Worked for 2m 10s\n"
                f"{top_rule}\n"
                f"❯{marker_gap}{input_text}\n"
                "────────────────────────────────────────────────────────────────\n"
                "  unit director for natedev | claude2 | claude-build-followups-two-accounts | "
                f"128,047 | Opus 5.5 high{status_suffix}\n"
                "  5-hour 100% left until 00:59 EDT | weekly 40% left until Thu 06:59 EDT\n"
                f"  {mode}\n"
                "\n"
                "  ● main\n"
                "  ◯ general-purpose  Check the regression   2m 10s · ↓ 127.6k tokens\n")

    def assert_real_composer_retries_once(self, text: str = "resume the saved work") -> None:
        self.submit_on_enter = 1
        outcome = self.submit(text=text)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual([(pane, text) for pane, text, _at in self.typed],
                         [("destination:p1", script_prompt(text))])
        self.assertEqual(self.enters, [("destination:p1", ("Enter",))])
        self.assertGreaterEqual(cast(Mock, self.read).call_count, 2)

    def test_named_composer_with_status_mode_and_agent_footer_gets_one_enter_retry(self) -> None:
        self.typed_screen = self.real_composer_screen("──────────────────────── two-accounts ─")
        self.assert_real_composer_retries_once()

    def test_unnamed_composer_with_status_and_auto_cycle_footer_gets_one_enter_retry(self) -> None:
        self.typed_screen = self.real_composer_screen("────────────────────────────────────────",
                                               mode="⏵⏵ auto mode on (shift+tab to cycle)")
        self.assert_real_composer_retries_once()

    def test_named_composer_with_remote_control_status_suffix_gets_one_enter_retry(self) -> None:
        self.typed_screen = self.real_composer_screen("──────────────────────── style ─", status_suffix="        /rc")
        self.assert_real_composer_retries_once()

    def test_named_composer_with_no_break_space_after_marker_gets_one_enter_retry(self) -> None:
        self.typed_screen = self.real_composer_screen("──────────────────────── ups ─", marker_gap="\xa0")
        self.assert_real_composer_retries_once()

    def test_multiline_prompt_with_rule_like_rows_gets_one_enter_retry(self) -> None:
        prompt = script_prompt("resume the saved work\n--- context ---\n──── saved context ─\ncontinue here")
        self.typed_screen = self.real_composer_screen("──────────────────────── two-accounts ─", input_text=prompt)
        self.assert_real_composer_retries_once(prompt)

    def test_indented_quote_after_rule_inside_live_prompt_gets_one_enter_retry(self) -> None:
        prompt = script_prompt("resume the saved work\n---\n> quoted line\ncontinue here")
        visible_input = "resume the saved work\n  ---\n  > quoted line\n  continue here"
        self.typed_screen = self.real_composer_screen("──────────────────────── two-accounts ─",
                                               input_text=visible_input)
        self.assert_real_composer_retries_once(prompt)

    def test_old_quoted_prompt_above_empty_live_composer_gets_no_enter(self) -> None:
        prompt = script_prompt("resume the saved work\n---\n> quoted line\ncontinue here")
        old_input = "resume the saved work\n  ---\n  > quoted line\n  continue here"
        self.drop_all = True
        self.screen = ("─── old session ─\n❯ " + old_input + "\n────────\n● Finished that task\n"
                       + self.real_composer_screen("──────────────────────── two-accounts ─", input_text=""))
        self.assertIsInstance(self.submit(text=prompt), herdr.PromptNotSubmitted)
        self.assertTrue(self.typed)
        self.assertTrue(all((pane, text) == ("destination:p1", script_prompt(prompt)) for pane, text, _at in self.typed))
        self.assertEqual(self.enters, [])

    def test_named_composer_with_rule_character_in_label_gets_one_enter_retry(self) -> None:
        self.typed_screen = self.real_composer_screen("─── two─accounts ─")
        self.assert_real_composer_retries_once()

    def test_old_rule_containing_prompt_above_empty_live_composer_gets_no_enter(self) -> None:
        prompt = script_prompt("resume the saved work\n--- context ---\n──── saved context ─\ncontinue here")
        self.drop_all = True
        self.screen = ("─── old session ─\n❯ " + prompt + "\n────────\n● Finished that task\n"
                       + self.real_composer_screen("──────────────────────── two-accounts ─", input_text=""))
        self.assertIsInstance(self.submit(text=prompt), herdr.PromptNotSubmitted)
        self.assertTrue(self.typed)
        self.assertTrue(all((pane, text) == ("destination:p1", script_prompt(prompt)) for pane, text, _at in self.typed))
        self.assertEqual(self.enters, [])

    def test_named_composer_does_not_retry_prompt_visible_only_above_its_empty_input(self) -> None:
        self.drop_all = True
        self.screen = ("❯ resume the saved work\n"
                       + self.real_composer_screen("──────────────────────── two-accounts ─", input_text=""))
        self.assertIsInstance(self.submit(), herdr.PromptNotSubmitted)
        self.assertTrue(1 <= len(self.typed) <= 3)
        self.assertEqual(self.enters, [])

    def test_named_bordered_prompt_above_column_zero_output_gets_no_enter(self) -> None:
        self.screen = (self.real_composer_screen("──────────────────────── two-accounts ─")
                       + "ctrl+c cancels the operation described above\n")
        self.assertIsInstance(self.submit(), herdr.PromptNotSubmitted)
        self.assertEqual(self.typed, [])
        self.assertEqual(self.enters, [])

    def test_real_composer_retry_still_rechecks_its_recipient_before_enter(self) -> None:
        self.typed_screen = self.real_composer_screen("──────────────────────── two-accounts ─")
        with patch.object(herdr, "prompt_recipient_process", side_effect=[
                herdr.PromptRecipientProcess(23456, "first-recipient"),
                herdr.PromptRecipientProcess(34567, "replacement-process")]):
            outcome = self.submit()
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        self.assertIn("recipient changed", cast(herdr.PromptNotSubmitted, outcome).reason)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])


    def test_done_recipient_can_retype_a_lost_prompt(self) -> None:
        self.drop_before = 7
        self.submit_on_type = True
        with patch.object(self.client, "readiness", return_value={"agent_status": herdr.AgentStatus.DONE, "interactive_ready": True}):
            self.assertIsInstance(self.submit(), herdr.PromptSubmitted)
        self.assertEqual(len(self.typed), 2)
        self.assertGreaterEqual(self.typed[1][2], herdr.PROMPT_SETTLE_SECONDS)

    def test_remote_control_turn_does_not_make_a_loading_destination_ready(self) -> None:
        self.ready_at = 60
        self.write_turn("/remote-control is active", kind="system")
        self.submit_on_type = True
        self.assertIsInstance(self.submit(timeout_ms=1_000), herdr.PromptNotSubmitted)
        self.assertEqual((self.typed, self.enters), ([], []))

    def test_promptless_codex_restart_needs_no_transcript_or_submit(self) -> None:
        with (codex_launch_evidence(self.root, ["codex"]) as process_info,
              patch.object(self.client, "result", return_value=process_info)):
            outcome = self.client.start_agent("codex-worker", "destination:p1", "codex", [],
                                              herdr.NoOpeningPrompt(), 1_000)
        self.assertIsInstance(outcome, herdr.PromptSubmissionNotRequired)
        self.assertEqual((self.typed, self.enters, self.ready_panes), ([], [], []))

    def test_no_user_turn_and_no_prompt_in_the_input_reports_not_submitted(self) -> None:
        self.drop_all = True
        outcome = self.submit(timeout_ms=90_000)
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        self.assertEqual(len(self.typed), 3)
        self.assertEqual(self.enters, [])
        self.assertLessEqual(self.elapsed, 90)
        self.assertTrue(all(at < 90 for _pane, _text, at in self.typed))
        self.assertGreaterEqual(self.typed[1][2] - self.typed[0][2], 10)
        self.assertGreaterEqual(self.typed[2][2] - self.typed[1][2], 10)
        self.assertIn("prompt typed 3 times", cast(herdr.PromptNotSubmitted, outcome).reason)
        self.assertEqual(cast(herdr.PromptNotSubmitted, outcome).screen, self.screen)


    def test_an_unknown_command_is_typed_once_more_after_its_list_settles(self) -> None:
        outcome = self.submit_command(lambda _text, typing: typing == 2)
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual([text for _pane, text, _at in self.typed], [self.PRODUCE] * 2)
        self.assertGreaterEqual(self.typed[1][2] - self.typed[0][2], herdr.PROMPT_SETTLE_SECONDS)
        self.assertEqual(self.enters, [])


    def test_a_command_unknown_twice_with_no_file_reports_the_refusal(self) -> None:
        outcome = self.submit_command(lambda _text, _typing: False)
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        reason = cast(herdr.PromptNotSubmitted, outcome).reason
        self.assertIn("Claude answered Unknown command: /showrunner:produce; prompt typed 2 times", reason)
        self.assertEqual([text for _pane, text, _at in self.typed], [self.PRODUCE] * 2)
        self.assertFalse(cast(herdr.PromptNotSubmitted, outcome).never_typed)


    def test_rejections_count_only_this_prompts_command(self) -> None:
        cursor = herdr.TranscriptCursor(self.transcript, self.launched_at)
        for command in ("/showrunner:produce-x", "/showrunner:producer", "/showrunner:produce", "/compat. Did you mean /compact?"):
            self.write_rejection(command)
        self.assertEqual(cursor.command_rejections(self.PRODUCE), 1)
        self.assertEqual(cursor.command_rejections("resume the saved work"), 0)


    def test_missing_composer_waits_for_the_deadline_and_carries_the_last_screen(self) -> None:
        self.drop_all = True
        self.events.append((1, lambda: setattr(self, "screen", "Restoring conversation")))
        self.events.append((11, lambda: setattr(self, "screen", "Still loading conversation")))
        outcome = self.submit(timeout_ms=16_000)
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        failure = cast(herdr.PromptNotSubmitted, outcome)
        self.assertIn("deadline", failure.reason)
        self.assertEqual(failure.screen, "Still loading conversation")
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])
        self.assertEqual(self.elapsed, 16)


    def test_lost_prompt_is_not_retyped_after_a_deadline_inside_the_settle_window(self) -> None:
        self.drop_all = True
        outcome = self.submit(timeout_ms=9_000)
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertLessEqual(self.elapsed, 9)
        self.assertEqual(self.enters, [])


    def test_a_replacement_process_cannot_inherit_the_turn_written_during_submission(self) -> None:
        self.submit_on_type = True
        observations = 0

        def recipient(_session: str) -> herdr.PromptRecipientProcess:
            nonlocal observations
            observations += 1
            return (herdr.PromptRecipientProcess(23456, "first-recipient") if observations == 1
                    else herdr.PromptRecipientProcess(34567, "replacement-process"))

        with patch.object(herdr, "prompt_recipient_process", side_effect=recipient):
            outcome = self.submit()
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [])


    def test_an_unsubmitted_input_gets_at_most_three_enter_retries(self) -> None:
        self.assertIsInstance(self.submit(), herdr.PromptNotSubmitted)
        self.assertEqual(len(self.typed), 1)
        self.assertEqual(self.enters, [("destination:p1", ("Enter",))] * 3)
        self.assertLessEqual(self.elapsed, 30)


    def test_enter_retry_uses_the_real_target_check_and_key_transport(self) -> None:
        self.submit_on_enter = 1
        commands: list[tuple[str, ...]] = []

        def transport(*arguments: str, deadline: float = math.inf) -> str:
            del deadline
            commands.append(arguments)
            if arguments[:2] == ("pane", "send-keys"):
                self.enter_prompt(arguments[2], *arguments[3:])
                return "{}"
            return self.read_screen(*arguments)

        self.stop_key_transport()
        with patch.object(self.client, "run", side_effect=transport):
            outcome = self.submit()
        self.assertIsInstance(outcome, herdr.PromptSubmitted)
        self.assertEqual([row for row in commands if row[:2] == ("pane", "send-keys")],
                         [("pane", "send-keys", "destination:p1", "Enter")])
        self.assertEqual(len(self.typed), 1)

    def test_replacement_session_never_receives_the_selected_sessions_prompt(self) -> None:
        replacement = fixture_runtime_id("replacement-prompt-session")
        self.submit_on_type = True
        self.ready_at = 2
        with (patch.object(self.client, "panes", return_value=[
                  {"pane_id": "destination:p1", "session_id": replacement}]),
              patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(
                  herdr.RolloutThread(replacement), "replacement-generation", {}))):
            outcome = self.submit()
        self.assertIsInstance(outcome, herdr.PromptNotSubmitted)
        self.assertEqual(self.typed, [])
        self.assertEqual(self.enters, [])


@final
class LaunchDestinationTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        proc = self.root / "proc"
        proc.mkdir()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HERDR_PROC": str(proc), "HERDR_BIN": "/nonexistent/herdr"})))
        _ = self.enterContext(patch.object(herdr, "_PLATFORM", "linux"))
        _ = self.enterContext(patch.object(herdr, "_startup_processes", return_value=herdr.PaneAtShell()))

    def test_pane_launch_waits_for_shell_cleanup_before_sending_the_command(self) -> None:
        client = herdr.Herdr("agents")
        clock = StartupClock()

        def advance(seconds: float) -> None:
            clock.elapsed += seconds

        def checked(*_args: object, **_kwargs: object) -> bool:
            self.assertFalse(run.called)
            if clock.elapsed < 0.2:
                raise herdr.HerdrFailure("pane runs other work: direnv, bash", "wrong_target")
            return clock.elapsed >= 0.4

        with (patch.object(herdr, "check_target", side_effect=checked) as check,
              patch.object(herdr.Herdr, "pane_row", return_value=herdr.NoPane("worker")),
              patch.object(time, "monotonic", lambda: clock.elapsed),
              patch.object(time, "sleep", side_effect=advance),
              patch.object(client, "run") as run):
            client.run_in_pane("worker", ["claude", "--resume", "thread"])
        self.assertGreaterEqual(check.call_count, 5)
        self.assertGreaterEqual(clock.elapsed, 0.4)
        run.assert_called_once_with("pane", "run", "worker", "claude --resume thread")

    def test_all_destinations_start_and_label_before_start(self) -> None:
        run_in_pane = herdr.Herdr.run_in_pane
        program = self.root / "bin" / "codex"
        write_text_atomic(program, f"#!{sys.executable}\nimport os\nprint(os.environ['CODEX_HOME'])\n",
                          new_file_mode=0o700)
        for destination in (herdr.NewTab("workers", "worker"),
                            herdr.SplitPane("parent", "right", 0.3, "worker"),
                            herdr.ExistingPane("worker-pane", "terminal-one")):
            with self.subTest(destination=destination):
                client = Mock(spec=herdr.Herdr)
                client.session = "agents"
                cast(Mock, client.result).return_value = {"pane": {"pane_id": "worker-pane"}}
                cast(Mock, client.panes).return_value = [{"pane_id": "worker-pane", "terminal_id": "terminal-one", "session_id": None}]
                cast(Mock, client.pane_activity).return_value = herdr.PaneAtShell()
                cast(Mock, client.startup_processes).return_value = herdr.PaneStartupObservation(herdr.PaneShell(123), herdr.PaneAtShell())
                events: list[str] = []
                def labelled(*_args: object) -> None:
                    events.append("label")
                cast(Mock, client.label_pane).side_effect = labelled
                def process_info(*arguments: str) -> dict[str, object]:
                    if arguments[:2] == ("pane", "process-info"):
                        events.append("process-info")
                        return {"process_info": {"shell_pid": 998044,
                            "foreground_process_group_id": 998044,
                            "foreground_processes": [{"pid": 998044, "name": "zsh", "argv": ["zsh"]}]}}
                    return {"pane": {"pane_id": "worker-pane"}}
                cast(Mock, client.result).side_effect = process_info
                def run_command(pane: str, command: list[str], *, terminal: str = "") -> None:
                    run_in_pane(cast(herdr.Herdr, client), pane, command, terminal=terminal)
                cast(Mock, client.run_in_pane).side_effect = run_command
                def start(*_args: object, **_kwargs: object) -> herdr.PromptSubmissionNotRequired:
                    events.append("start")
                    command = cast(str, cast(Mock, client.run).call_args.args[3])
                    alias = shlex.quote(f"CODEX_HOME=/wrong {shlex.quote(str(program))}")
                    shell = subprocess.run([shutil.which("zsh") or "/bin/zsh", "-c",
                                            f"alias codex={alias}\n{command}\ncodex\n"],
                                           env=prepared.env, capture_output=True, text=True, check=True)
                    self.assertEqual(shell.stdout.strip(), "/account")
                    return herdr.PromptSubmissionNotRequired()
                prepared = herdr.PreparedAgentEnvironment(Path("/tmp"),
                    {"CODEX_HOME": "/account", "PATH": f"{program.parent}{os.pathsep}{os.defpath}"})
                with (patch.object(herdr, "Herdr", return_value=client),
                      patch.object(herdr, "open_tab", return_value="worker-pane") as opened,
                      patch.object(herdr, "start_agent", side_effect=start) as started,
                      patch.object(client, "pane_row", return_value={"pane_id": "worker-pane",
                          "workspace_id": "workers", "terminal_id": "terminal-one", "session_id": None})):
                    result = herdr.launch_prepared_agent("agents", destination, prepared,
                        "codex", ["resume", "conversation"], herdr.NoOpeningPrompt(), 5000, agent="worker")
                self.assertEqual(result.pane, "worker-pane")
                started.assert_called_once_with("agents", "worker-pane", "codex", ["resume", "conversation"],
                    herdr.NoOpeningPrompt(), 5000, agent="worker")
                if isinstance(destination, herdr.NewTab):
                    opened.assert_called_once_with("agents", "workers", "worker", "/tmp", prepared.env)
                    self.assertEqual(events, ["process-info", "start"])
                else:
                    opened.assert_not_called()
                if isinstance(destination, herdr.SplitPane):
                    self.assertEqual(events, ["label", "process-info", "start"])
                    cast(Mock, client.label_pane).assert_called_once_with("worker-pane", "worker")
                    args = cast(tuple[str, ...], cast(Mock, client.result).call_args_list[0].args)
                    self.assertIn("--no-focus", args)
                    self.assertIn("CODEX_HOME=/account", args)
                if isinstance(destination, herdr.ExistingPane):
                    cast(Mock, client.panes).assert_called_once_with()
                    self.assertEqual(cast(Mock, client.result).call_args_list,
                                     [call("pane", "process-info", "--pane", "worker-pane")])
                    self.assertEqual(events, ["process-info", "start"])
                    command = cast(list[str], cast(Mock, client.run_in_pane).call_args.args[1])
                    self.assertIn("CODEX_HOME=/account", command[1])
                    self.assertIn("cd -- /tmp", command[1])

    def test_existing_pane_removes_legacy_labels_in_a_private_shell(self) -> None:
        client = Mock(spec=herdr.Herdr)
        cast(Mock, client.panes).return_value = [{"pane_id": "worker", "terminal_id": "terminal-one"}]
        prepared = herdr.PreparedAgentEnvironment(self.root, {
            "CODEX_THREAD_ID": "replacement-native-id", "KEPT_SETTING": "kept"})
        with (patch.object(herdr, "Herdr", return_value=client),
              patch.object(herdr, "start_agent", return_value=herdr.PromptSubmissionNotRequired())):
            _ = herdr.launch_prepared_agent("agents", herdr.ExistingPane("worker", "terminal-one"),
                prepared, "codex", [], herdr.NoOpeningPrompt(), 5000, agent="worker")
        command = cast(list[str], cast(Mock, client.run_in_pane).call_args.args[1])
        legacy = "SHOWRUNNER_" + "UNIT"
        unit_id = legacy + "_ID"
        shell_environment = {"PATH": os.environ["PATH"], legacy: "old production", unit_id: "old",
                             "SHOWRUNNER_EXTRA": "old value=with spaces", "CODEX_THREAD_ID": "old-native-id"}
        query = (f"; printf '%s\\n' \"${{{legacy}-unset}}\" \"${{{unit_id}-unset}}\" "
                 '"${SHOWRUNNER_EXTRA-unset}" "$CODEX_THREAD_ID" "$KEPT_SETTING"')
        result = subprocess.run([shutil.which("sh") or "/bin/sh", "-c", command[1] + query],
                                env=shell_environment, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["unset", "unset", "unset", "replacement-native-id", "kept"])

    def test_existing_pane_refuses_overwriting_proved_foreground_work(self) -> None:
        client = herdr.Herdr("agents")
        log = self.root / "occupied-pane.jsonl"

        def transport(*arguments: str, deadline: float = math.inf) -> str:
            del deadline
            with log.open("a") as output:
                _ = output.write(json.dumps(arguments) + "\n")
            if arguments == ("pane", "list"):
                return json.dumps({"result": {"panes": [{"pane_id": "worker", "workspace_id": "workers",
                    "terminal_id": "terminal-one", "agent_session": {"value": None}}]}})
            if arguments == ("pane", "process-info", "--pane", "worker"):
                return json.dumps({"result": {"process_info": {"shell_pid": 998044,
                    "foreground_process_group_id": 998045,
                    "foreground_processes": [{"pid": 998045, "name": "vim", "argv": ["vim"]}]}}})
            return "{}"

        with (patch.object(herdr, "Herdr", return_value=client),
              patch.object(client, "run", side_effect=transport),
              patch.object(herdr, "start_agent") as started,
              self.assertRaises(herdr.HerdrFailure)):
            _ = herdr.launch_prepared_agent("agents", herdr.ExistingPane("worker", "terminal-one"),
                herdr.PreparedAgentEnvironment(Path("/tmp"), {}), "codex", [],
                herdr.NoOpeningPrompt(), 5000, agent="worker")
        started.assert_not_called()
        commands = [cast(list[str], json.loads(line)) for line in log.read_text().splitlines()]
        self.assertFalse(any(command[:2] in (["pane", "run"], ["pane", "send-keys"], ["agent", "prompt"])
                             for command in commands))


    def test_existing_failed_agent_exits_without_closing_its_shell(self) -> None:
        client = Mock(spec=herdr.Herdr)
        cast(Mock, client.panes).return_value = [{"pane_id": "worker", "terminal_id": "terminal-one"}]
        cast(Mock, client.pane_activity).return_value = herdr.PaneAtShell()
        cast(Mock, client.startup_processes).return_value = herdr.PaneStartupObservation(
            herdr.PaneShell(123), herdr.PaneAtShell())
        with (patch.object(herdr, "Herdr", return_value=client),
              patch.object(herdr, "start_agent", side_effect=herdr.HerdrFailure("start failed"))):
            outcome = herdr.launch_prepared_agent("agents", herdr.ExistingPane("worker", "terminal-one"),
                herdr.PreparedAgentEnvironment(Path("/tmp"), {}), "codex", [],
                herdr.NoOpeningPrompt(), 5000, agent="worker")
        self.assertEqual(outcome, herdr.AgentLaunched("worker", herdr.PromptNotSubmitted("start failed")))
        cast(Mock, client.send_keys).assert_not_called()
        cast(Mock, client.close_pane).assert_not_called()


    def test_existing_shell_exports_account_environment_with_a_staff_row_lock(self) -> None:
        client = Mock(spec=herdr.Herdr)
        cast(Mock, client.panes).return_value = [{"pane_id": "worker", "terminal_id": "terminal-one"}]
        cast(Mock, client.pane_activity).return_value = herdr.PaneAtShell()
        cast(Mock, client.startup_processes).return_value = herdr.PaneStartupObservation(herdr.PaneShell(123), herdr.PaneAtShell())
        lock = "STAFF_ROW_FD_0e8d7a1b-61d3-464c-98df-92de4ec0114c"
        with (patch.dict(os.environ, {lock: "99"}), patch.object(herdr, "Herdr", return_value=client), patch.object(herdr.nix_env, "ensure", return_value=herdr.nix_env.NoEnvrc({"PATH": "/bin", "CLAUDE_CONFIG_DIR": "/account", lock: "99"})), patch.object(herdr, "start_agent", return_value=herdr.PromptSubmissionNotRequired())):
            prepared = herdr.prepare_agent_environment("/tmp", {}, frozenset({"EXCLUDED_TOKEN"}))
            self.assertIsInstance(prepared, herdr.PreparedAgentEnvironment)
            _ = herdr.launch_prepared_agent("agents", herdr.ExistingPane("worker", "terminal-one"),
                cast(herdr.PreparedAgentEnvironment, prepared), "claude", [], herdr.NoOpeningPrompt(), 5000, agent="worker")
        command = cast(list[str], cast(Mock, client.run_in_pane).call_args.args[1])
        removed = command[1].split("unset ", 1)[1].split(" && export", 1)[0].split()
        self.assertNotIn(lock, removed)
        self.assertNotIn(lock, herdr.child_environment({lock: "99"}))
        self.assertIn("EXCLUDED_TOKEN", removed)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", removed)
        check = "import os; print(os.environ.get('EXCLUDED_TOKEN', 'absent'), os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', 'absent'), os.environ.get('HERDR_PANE_ID', 'absent'), os.environ.get('CLAUDE_CONFIG_DIR', 'absent'))"
        probe = shlex.join([sys.executable, "-c", check])
        script = "claude() { " + probe + "; }\nalias claude=" + shlex.quote("CLAUDE_CONFIG_DIR=/wrong " + probe) + "\n" + command[1] + "\nclaude\n"
        shell = subprocess.run(["zsh", "-c", script],
            env={**os.environ, "EXCLUDED_TOKEN": "stale", "CLAUDE_CODE_OAUTH_TOKEN": "stale", "HERDR_PANE_ID": "worker"},
            capture_output=True, text=True, check=True)
        self.assertEqual(shell.stdout.strip(), "absent absent worker /account")

    def test_child_environment_omits_no_color(self) -> None:
        child = herdr.child_environment({"NO_COLOR": "1", "PATH": "/bin"})
        self.assertNotIn("NO_COLOR", child)
        self.assertEqual(child["PATH"], "/bin")

    def test_agent_exit_or_pid_reuse_before_cleanup_never_signals_the_shell(self) -> None:
        identity = herdr.AgentProcessIdentity(456, 789)
        for fresh in (FileNotFoundError(), herdr.AgentProcessIdentity(456, 999)):
            reader = Mock()
            checked = cast(Mock, reader.identity)
            checked.side_effect = fresh if isinstance(fresh, FileNotFoundError) else None
            checked.return_value = fresh
            with (self.subTest(fresh=fresh), patch.object(os, "kill") as signalled,
                  patch.object(herdr, "_startup_process_reader", return_value=reader)):
                if isinstance(fresh, FileNotFoundError):
                    self.assertFalse(herdr.check_target(identity, action="signal", shell=herdr.PaneShell(123)))
                else:
                    with self.assertRaisesRegex(herdr.HerdrFailure, "reused process"):
                        _ = herdr.check_target(identity, action="signal", shell=herdr.PaneShell(123))
            signalled.assert_not_called()
            checked.assert_called_once_with(identity.pid)

    def test_signal_target_check_preserves_the_shell_and_never_sends_a_signal(self) -> None:
        identity = herdr.AgentProcessIdentity(456, 789)
        for case in ("live", "exited", "reused", "shell", "weak"):
            reader = Mock()
            checked = cast(Mock, reader.identity)
            checked.return_value = herdr.AgentProcessIdentity(456, 999) if case == "reused" else identity
            if case == "exited":
                checked.side_effect = FileNotFoundError
            if case == "weak":
                checked.return_value = herdr.AgentProcessIdentity(456, herdr.ProcessGenerationUnavailable("unreadable"))
            with (self.subTest(case=case), patch.object(os, "kill") as signalled,
                  patch.object(herdr, "_startup_process_reader", return_value=reader)):
                agent = herdr.AgentProcessIdentity(123, 789) if case == "shell" else identity
                if case in ("shell", "reused"):
                    message = "agent identity matches the shell" if case == "shell" else "reused process"
                    with self.assertRaisesRegex(herdr.HerdrFailure, message):
                        _ = herdr.check_target(agent, action="signal", shell=herdr.PaneShell(123))
                else:
                    self.assertEqual(herdr.check_target(agent, action="signal", shell=herdr.PaneShell(123)), case == "live")
                if case == "shell":
                    checked.assert_not_called()
                else:
                    checked.assert_called_once_with(identity.pid)
                signalled.assert_not_called()


    def test_refused_environment_command_never_starts_an_agent(self) -> None:
        client = herdr.Herdr("agents")
        commands: list[tuple[str, ...]] = []

        def run(*arguments: str, deadline: float = math.inf) -> str:
            del deadline
            commands.append(arguments)
            if arguments == ("pane", "process-info", "--pane", "worker"):
                raise herdr.HerdrFailure("private process facts are unavailable")
            return "{}"

        with (patch.object(herdr, "Herdr", return_value=client),
              patch.object(client, "panes", return_value=[{
                  "pane_id": "worker", "terminal_id": "terminal-one", "session_id": None}]),
              patch.object(client, "run", side_effect=run),
              patch.object(herdr, "start_agent") as started):
            outcome = herdr.launch_prepared_agent("agents", herdr.ExistingPane("worker", "terminal-one"),
                herdr.PreparedAgentEnvironment(self.root, {}), "codex", [],
                herdr.NoOpeningPrompt(), 5000, agent="worker")
        started.assert_not_called()
        self.assertIsInstance(outcome.outcome, herdr.PromptNotSubmitted)
        self.assertFalse(any(row[:2] == ("pane", "run") for row in commands))


@final
class ConfirmedPaneCloseTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HERDR_BIN": str(Path(herdr.__file__).with_name("fake_herdr.py")),
        })))


    def test_finish_restore_keeps_each_record_when_its_close_is_unconfirmed(self) -> None:
        for verified in (False, True):
            with self.subTest(verified=verified), self.retained() as (client, path, original):
                del client
                replacement = pane_launch.read_strict(path)["w1:p2"]
                attempt = pane_launch.resume_attempt(path, "w1:p2")
                outcome = (pane_launch.VerifiedResume(replacement, frozenset({original["session_id"]}))
                           if verified else pane_launch.FailedResume())
                before = pane_launch.read_strict(path)
                closed: list[str] = []

                def unconfirmed_close(pane: str) -> bool:
                    closed.append(pane)
                    return False

                pane_launch.finish_restore(path, original, attempt, outcome, close=unconfirmed_close)

                self.assertEqual(closed, ["w1:p1" if verified else "w1:p2"])
                self.assertEqual(pane_launch.read_strict(path), before)

    @contextmanager
    def retained(self) -> Generator[tuple[herdr.Herdr, Path, pane_launch.PaneLaunch]]:
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"HERDR_PANE_RECORD_DIR": folder}):
            path = Path(folder) / "fixture.json"
            entry: pane_launch.PaneLaunch = {
                "pane_id": "w1:p1", "session_id": "conversation", "config_dir": "/account",
                "cwd": "/work", "name": "session-name", "model": "", "remote_control": False,
                "environment": {}, "flags_read": True, "written_at": "2026-11-02T12:00:00Z", "pid": 42,
            }
            pane_launch.save(path, {"w1:p1": entry, "w1:p2": {**entry, "pane_id": "w1:p2"}})
            client = herdr.Herdr("fixture")
            def activity_read(_shell: herdr.PaneShell, _kind: str,
                              _tracked: set[herdr.AgentProcessIdentity]) -> herdr.PaneAtShell | herdr.PaneRunning:
                return client.pane_activity("w1:p1")
            with (patch.object(herdr, "_startup_processes", side_effect=activity_read),
                  patch.object(client, "result", return_value=self.listing()),
                  patch.object(client, "pane_activity", return_value=herdr.PaneAtShell())):
                yield client, path, entry


    def rows(self) -> list[herdr.PaneIdentity]:
        return [{"pane_id": pane, "terminal_id": terminal, "workspace_id": "w1", "session_id": None}
                for pane, terminal in (("w1:p1", "term-one"), ("w1:p2", "term-two"))]


    def listing(self) -> dict[str, object]:
        return {"panes": [{**row, "tab_id": "w1:t1"} for row in self.rows()],
                "process_info": {"shell_pid": 998042, "foreground_process_group_id": 998042,
                                 "foreground_processes": [{"pid": 998042, "name": "zsh", "argv": ["zsh"]}]}}


    def test_close_confirms_only_the_target_and_retires_its_launch_once_on_rerun(self) -> None:
        with self.retained() as (client, path, entry):
            rows = self.rows()
            def close(pane: str) -> None:
                rows[:] = [row for row in rows if row["pane_id"] != pane]
            with (patch.object(client, "panes", side_effect=lambda: list(rows)),
                  patch.object(client, "pane_activity", return_value=herdr.PaneAtShell()) as activity,
                  patch.object(client, "_close_pane", side_effect=close) as closed,
                  patch.object(pane_launch, "retire_launch", wraps=pane_launch.retire_launch) as retired):
                _ = client.close_retained_pane("w1:p1", terminal="term-one")
                _ = client.close_retained_pane("w1:p1", terminal="term-one")
            closed.assert_called_once_with("w1:p1")
            activity.assert_called_once_with("w1:p1")
            retired.assert_called_once_with(path, "w1:p1", entry)
            self.assertEqual(list(pane_launch.read_strict(path)), ["w1:p2"])

    def test_close_refuses_a_terminal_that_changes_before_the_close(self) -> None:
        for terminal in ("term-replacement", ""):
            with self.subTest(terminal=terminal), self.retained() as (client, path, entry):
                fresh = self.rows()
                fresh[0]["terminal_id"] = terminal
                with (patch.object(client, "panes", return_value=fresh),
                      patch.object(client, "_close_pane") as closed,
                      redirect_stderr(io.StringIO()) as errors):
                    if terminal:
                        with self.assertRaisesRegex(herdr.HerdrFailure, "terminal identity changed"):
                            _ = client.close_retained_pane("w1:p1")
                    else:
                        _ = client.close_retained_pane("w1:p1")
                        self.assertEqual(errors.getvalue().splitlines(), [
                            "herdr conversation: terminal is unknown; target retained"])
                closed.assert_not_called()
                self.assertEqual(pane_launch.read_strict(path)["w1:p1"], entry)


    def test_close_retains_the_launch_when_its_terminal_moves_during_close(self) -> None:
        with self.retained() as (client, path, entry):
            moved = {**self.rows()[0], "pane_id": "w2:p1", "workspace_id": "w2"}
            errors = io.StringIO()
            with (patch.object(client, "panes", side_effect=[self.rows(), [moved, self.rows()[1]]]),
                  patch.object(client, "_close_pane"),
                  redirect_stderr(errors)):
                self.assertFalse(client.close_retained_pane("w1:p1"))
            self.assertEqual(errors.getvalue(), "herdr fixture:w1:p1: closure is pending; record retained\n")
            self.assertEqual(pane_launch.read_strict(path)["w1:p1"], entry)


    def test_close_refuses_proved_foreground_work_and_keeps_the_launch(self) -> None:
        with self.retained() as (client, path, entry):
            with (patch.object(client, "pane_activity", return_value=herdr.PaneRunning(("editor",))),
                  patch.object(client, "_close_pane") as closed,
                  self.assertRaisesRegex(herdr.HerdrFailure, "pane runs other work: editor")):
                _ = client.close_retained_pane("w1:p1")
            closed.assert_not_called()
            self.assertEqual(pane_launch.read_strict(path)["w1:p1"], entry)


    def test_unconfirmed_close_keeps_the_launch_for_a_rerun(self) -> None:
        with self.retained() as (client, path, entry):
            errors = io.StringIO()
            with (patch.object(client, "panes", return_value=self.rows()),
                  patch.object(client, "_close_pane") as closed,
                  redirect_stderr(errors)):
                self.assertFalse(client.close_retained_pane("w1:p1"))
            closed.assert_called_once_with("w1:p1")
            self.assertEqual(errors.getvalue(), "herdr fixture:w1:p1: closure is pending; record retained\n")
            self.assertEqual(pane_launch.read_strict(path)["w1:p1"], entry)


    def test_a_pane_that_stays_open_keeps_its_launch_record(self) -> None:
        with self.retained() as (client, path, entry):
            with (patch.object(client, "panes", return_value=self.rows()),
                  patch.object(client, "run", return_value="{}") as sent):
                _ = client.close_retained_pane("w1:p1", terminal="term-one")
            commands = [cast(tuple[str, ...], event.args) for event in sent.call_args_list]
            self.assertIn(("pane", "close", "w1:p1"), commands)
            self.assertEqual(pane_launch.read_strict(path)["w1:p1"], entry)

    def test_a_terminal_that_moves_during_close_keeps_its_launch_record(self) -> None:
        with self.retained() as (client, path, entry):
            moved = False
            def rows() -> list[herdr.PaneIdentity]:
                original = self.rows()
                if moved:
                    original[0] = {**original[0], "pane_id": "w2:p1", "workspace_id": "w2"}
                return original
            def transport(*arguments: str, deadline: float = math.inf) -> str:
                nonlocal moved
                del deadline
                if arguments == ("pane", "close", "w1:p1"):
                    moved = True
                return "{}"
            with (patch.object(client, "panes", side_effect=rows),
                  patch.object(client, "run", side_effect=transport) as sent):
                _ = client.close_retained_pane("w1:p1", terminal="term-one")
            commands = [cast(tuple[str, ...], event.args) for event in sent.call_args_list]
            self.assertIn(("pane", "close", "w1:p1"), commands)
            self.assertEqual(pane_launch.read_strict(path)["w1:p1"], entry)


@final
class PaneSelectionTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HERDR_BIN": str(Path(herdr.__file__).with_name("fake_herdr.py")),
        })))

    def test_stale_or_duplicate_labels_never_select_a_pane(self) -> None:
        row = unit_membership("production", "worker")
        client = herdr.Herdr("fixture")
        panes: list[herdr.PaneIdentity] = [
            {"pane_id": "w1:p1", "workspace_id": "w1", "session_id": fixture_runtime_id("another-session"), "label": "stale-label"},
            {"pane_id": "w1:p2", "workspace_id": "w1", "session_id": row.current_session, "label": "stale-label"}]
        with patch.object(client, "panes", return_value=panes):
            self.assertEqual(client.pane_id(row.id), "w1:p2")
            with self.assertRaisesRegex(herdr.HerdrFailure, "unavailable or ambiguous"):
                _ = client.pane_id("stale-label")
            panes[1]["session_id"] = fixture_runtime_id("replaced-session")
            with self.assertRaisesRegex(herdr.HerdrFailure, "unavailable or ambiguous"):
                _ = client.pane_id(row.id)


@final
class TargetCatastropheTests(StaffTestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.client: Mock = Mock(spec=herdr.Herdr)

    @property
    def row(self) -> staff.StaffRow:
        row = next(row for row in staff.initial_snapshot().rows if row.name == "worker")
        return row

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = unit_membership("production", "worker")
        cast(Mock, self.client.panes).return_value = [
            {"pane_id": "w1:p1", "terminal_id": "terminal-one", "session_id": self.row.current_session}]
        cast(Mock, self.client.pane_activity).return_value = herdr.PaneRunning(("claude",))
        cast(Mock, self.client.native_session).return_value = self.row.current_session
        cast(Mock, self.client.run).return_value = ""
        cast(Mock, self.client.result).return_value = {"process_info": {
            "shell_pid": 998042, "foreground_process_group_id": 998043,
            "foreground_processes": [{"pid": 998043, "name": "claude", "argv": ["claude"]}]}}
        composer = herdr.Herdr.composer
        def read_composer(pane: str) -> herdr.ComposerInput | herdr.NoComposer:
            return composer(cast(herdr.Herdr, self.client), pane)
        cast(Mock, self.client.composer).side_effect = read_composer
        def native(_pid: int, _kind: str) -> herdr.NativeAgentProcess:
            identity = cast(str, cast(Mock, self.client.native_session).return_value)
            return herdr.NativeAgentProcess(herdr.RolloutThread(identity), "private-generation", {})
        _ = self.enterContext(patch.object(herdr, "native_process", side_effect=native))
        _ = self.enterContext(patch.object(herdr, "Herdr", return_value=self.client))

    def test_target_check_keeps_the_selected_session_when_the_row_resumes(self) -> None:
        replacement = fixture_runtime_id("resumed-worker")
        _ = staff.update_current_session(self.row.current_session, staff.native_id(replacement))
        cast(Mock, self.client.native_session).return_value = replacement
        for action in ("keys", "close"):
            with self.subTest(action=action):
                with self.assertRaisesRegex(herdr.HerdrFailure, f"does not run session {self.row.id}") as refused:
                    _ = herdr.check_target(self.row.id, action=action,
                        place=herdr.HerdrPlace("fixture", "w1:p1"))
                self.assertEqual(refused.exception.code, "wrong_target")
                self.assertTrue(herdr.check_target(replacement, action=action,
                    place=herdr.HerdrPlace("fixture", "w1:p1")))

    def test_target_check_refuses_input_or_closure_of_another_conversation(self) -> None:
        cast(Mock, self.client.native_session).return_value = fixture_runtime_id("another-conversation")
        for action in ("keys", "close"):
            with self.subTest(action=action), self.assertRaisesRegex(herdr.HerdrFailure, "does not run session"):
                _ = herdr.check_target(self.row.id, action=action, place=herdr.HerdrPlace("fixture", "w1:p1"))
        cast(Mock, self.client.run).assert_not_called()

    def test_target_check_refuses_signalling_a_reused_pid(self) -> None:
        original = herdr.AgentProcessIdentity(42, 100)
        reader = Mock()
        identity = Mock(return_value=herdr.AgentProcessIdentity(42, 101))
        reader.configure_mock(identity=identity)
        with (patch.object(herdr, "_startup_process_reader", return_value=reader),
              self.assertRaisesRegex(herdr.HerdrFailure, "reused process")):
            _ = herdr.check_target(original, action="signal")

    def test_target_check_allows_signalling_the_same_process_generation(self) -> None:
        original = herdr.AgentProcessIdentity(42, 100)
        reader = Mock()
        identity = Mock(return_value=original)
        reader.configure_mock(identity=identity)
        with patch.object(herdr, "_startup_process_reader", return_value=reader):
            self.assertTrue(herdr.check_target(original, action="signal"))
        identity.assert_called_once_with(42)

    def test_target_check_refuses_signalling_the_shell(self) -> None:
        with self.assertRaisesRegex(herdr.HerdrFailure, "signal the shell"):
            _ = herdr.check_target(herdr.AgentProcessIdentity(42, 100), action="signal", shell=herdr.PaneShell(42))

    def test_target_check_refuses_closing_another_terminal(self) -> None:
        with self.assertRaisesRegex(herdr.HerdrFailure, "another terminal"):
            _ = herdr.check_target(self.row.id, action="close", terminal="replaced-terminal",
                               place=herdr.HerdrPlace("fixture", "w1:p1"))

    def test_target_check_refuses_overwriting_unsaved_input(self) -> None:
        cast(Mock, self.client.run).return_value = "──────────\n❯ keep my draft\n──────────"
        with self.assertRaisesRegex(herdr.HerdrFailure, "unsaved input"):
            _ = herdr.check_target(self.row.id, action="keys", text="replacement",
                               place=herdr.HerdrPlace("fixture", "w1:p1"))

    def test_target_check_refuses_closing_a_shell_with_other_live_work(self) -> None:
        cast(Mock, self.client.pane_activity).return_value = herdr.PaneAtShell()
        cast(Mock, self.client.result).return_value = {"process_info": {
            "shell_pid": 42, "foreground_process_group_id": 42,
            "foreground_processes": [{"pid": 42, "name": "bash", "argv": ["bash"]}]}}
        cast(Mock, self.client.startup_processes).return_value = herdr.PaneStartupObservation(
            herdr.PaneShell(42), herdr.PaneAtShell())
        with (patch.object(herdr, "_startup_processes", return_value=herdr.PaneRunning(("editor",))),
              self.assertRaisesRegex(herdr.HerdrFailure, "other live work")):
            _ = herdr.check_target(self.row.id, action="close", place=herdr.HerdrPlace("fixture", "w1:p1"))

class HerdrTests(unittest.TestCase):
    root: Path = Path()
    fake: Path = Path()
    log: Path = Path()
    responses_path: Path = Path()
    responses: dict[str, CommandResponse | list[CommandResponse]] = {}
    proc: Path = Path()
    process_pane_replies: dict[str, str] = {}
    @property
    def herdr(self) -> herdr.Herdr:
        return herdr.Herdr("fixture")

    @contextmanager
    def prompt_target(self, client: herdr.Herdr, pane: str, session: str) -> Generator[None]:
        result = client.result
        def process_info(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
            if arguments[:2] == ("pane", "process-info"):
                return {"process_info": {"shell_pid": 998040, "foreground_process_group_id": 998041,
                    "foreground_processes": [{"pid": 998041, "name": "claude", "argv": ["claude"]}]}}
            return result(*arguments, deadline=deadline)
        def native(pid: int, kind: str) -> herdr.NativeAgentProcess:
            if (pid, kind) != (998041, "claude"):
                raise herdr.HerdrFailure("unexpected private process")
            return herdr.NativeAgentProcess(herdr.RolloutThread(session), "fixture-start", {})
        def composer(_pane: str) -> herdr.ComposerInput:
            return herdr.ComposerInput("")
        with (patch.object(herdr.Herdr, "panes", return_value=[{"pane_id": pane, "terminal_id": "prompt-terminal", "session_id": session}]),
              patch.object(client, "result", side_effect=process_info),
              patch.object(client, "composer", side_effect=composer),
              patch.object(herdr, "native_process", side_effect=native)):
            yield

    def test_failed_source_record_prevents_prompt_submission(self) -> None:
        from scripts.lib import script_prompts

        client = self.herdr
        with (self.prompt_target(client, "w1:p1", "fixture-submission"),
              patch.object(script_prompts, "record", side_effect=OSError("record failed")),
              patch.object(herdr.Herdr, "run") as submit):
            with self.assertRaisesRegex(OSError, "record failed"):
                client.prompt("w1:p1", "/resume", target="fixture-submission")
        submit.assert_not_called()

    def test_timed_out_prose_keeps_its_source_in_the_delayed_hook(self) -> None:
        from scripts.hooks import conversation_pause
        from scripts.lib import script_prompts

        for error in (subprocess.TimeoutExpired("herdr", 10),
                      herdr.HerdrFailure("input can still arrive", "agent_prompt_stalled")):
            client = self.herdr
            with (self.subTest(error=type(error).__name__),
                  self.prompt_target(client, "w1:p1", "fixture-submission"),
                  patch.dict(os.environ, {"CONVERSATION_PAUSE_STATE_DIR": str(self.root / "evidence")}),
                  patch.object(herdr.Herdr, "run", side_effect=error) as submit):
                with self.assertRaises(type(error)):
                    client.prompt("w1:p1", "Continue", target="fixture-submission")
                text = cast(str, submit.call_args.args[-1])
                self.assertTrue(text.startswith('<system-reminder source="script">'))
                self.assertFalse(script_prompts.root().exists())
                with patch.object(script_prompts, "typed_here", side_effect=AssertionError("store read")):
                    source = conversation_pause.prompt_source(text, lambda: frozenset(), lambda: (),
                                                              typed_by_script=script_prompts.typed_here)
                self.assertIs(source, conversation_pause.PromptSource.SCHEDULED)

    def test_timed_out_slash_command_keeps_its_source_record(self) -> None:
        from scripts.lib import script_prompts

        for error in (subprocess.TimeoutExpired("herdr", 10),
                      herdr.HerdrFailure("input can still arrive", "agent_prompt_stalled")):
            client = self.herdr
            with (self.subTest(error=type(error).__name__),
                  self.prompt_target(client, "w1:p1", "fixture-submission"),
                  patch.dict(os.environ, {"CONVERSATION_PAUSE_STATE_DIR": str(self.root / "evidence")}),
                  patch.object(herdr.Herdr, "run", side_effect=error)):
                with self.assertRaises(type(error)):
                    client.prompt("w1:p1", "/resume", target="fixture-submission")
                self.assertTrue(script_prompts.typed_by_script("w1:p1", "/resume"))

    @override
    def setUp(self) -> None:
        _ = cast(object, self.enterContext(patch.object(herdr, "_PLATFORM", "linux")))
        # Server start mechanics do not depend on the name. The one-session rule has its own tests.
        # These fixtures specify process startup and argv; PromptSubmissionTests
        # exercises the real transcript/readiness path without this substitute.
        def submitted(client: herdr.Herdr, pane: str, opening: herdr.OpeningPrompt,
                      _launched_at: datetime, _timeout_ms: int) -> herdr.PromptSubmitOutcome:
            session = (opening.transcript.session_id if isinstance(opening.transcript, herdr.SessionTranscript)
                       else "fixture-submission")
            with self.prompt_target(client, pane, session):
                client.prompt(pane, opening.text, target=session)
            return herdr.PromptSubmitted(datetime.now(timezone.utc))
        _ = self.enterContext(patch.object(herdr.Herdr, "submit_opening_prompt", submitted))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.fake = self.root / "fake-herdr"
        source = Path(__file__).with_name("fake_herdr.py").read_text()
        _ = self.fake.write_text("#!" + sys.executable + "\n" + source.split("\n", 1)[1])
        self.fake.chmod(0o755)
        self.log = self.root / "log"
        self.responses_path = self.root / "responses.json"
        self.responses = {"default": {"stdout": "", "code": 0}}
        self.process_pane_replies = {}
        self.proc = self.root / "proc"
        self.proc.mkdir()
        (self.root / "home/.claude/sessions").mkdir(parents=True)
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HERDR_BIN": str(self.fake), "SYSTEMD_RUN": str(self.fake), "SYSTEMCTL": str(self.fake),
            "HERDR_PROC": str(self.proc), "FAKE_HERDR_LOG": str(self.log),
            "FAKE_HERDR_RESPONSES": str(self.responses_path), "PATH": str(self.root / "no-tools"),
            "HOME": str(self.root / "home"), "XDG_CONFIG_HOME": str(self.root / "config"),
        }, clear=True)))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        self.configure([], "")
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": []}}))

    def test_agent_status_decodes_all_server_states_and_future_states_as_unknown(self) -> None:
        for raw, state in [(value.value, value) for value in herdr.AgentStatus] + [("future-state", herdr.AgentStatus.UNKNOWN)]:
            with self.subTest(raw=raw):
                self.configure_herdr(["agent", "list"], json.dumps({"result": {"agents": [
                    {"pane_id": "worker", "agent": "codex", "agent_status": raw}]}}))
                self.configure_herdr(["agent", "get", "worker"], json.dumps({"result": {"agent": {
                    "agent_status": raw}}}))
                self.assertIs(self.herdr.agents()[0]["agent_status"], state)
                self.assertIs(self.herdr.readiness("worker")["agent_status"], state)
                self.configure_herdr(["pane", "read", "worker", "--source", "visible", "--format", "ansi"],
                                     "────────\n❯ \n────────")
                observation = self.herdr.prompt_input_state("worker")
                self.assertEqual(isinstance(observation, herdr.PromptInputReady),
                                 state in {herdr.AgentStatus.WORKING, herdr.AgentStatus.IDLE, herdr.AgentStatus.DONE})
                if isinstance(observation, herdr.PromptInputReady):
                    self.assertEqual(observation.idle, state in {herdr.AgentStatus.IDLE, herdr.AgentStatus.DONE})

    def test_agent_inventory_decodes_a_reported_session_id(self) -> None:
        identity = fixture_runtime_id("agent-inventory-report")
        self.configure_herdr(["agent", "list"], json.dumps({"result": {"agents": [{
            "pane_id": "worker", "agent": "codex", "agent_status": "idle",
            "agent_session": {"kind": "id", "value": identity},
        }]}}))
        self.assertEqual(self.herdr.agents()[0]["agent_session"], herdr.ReportedAgentSession(identity))

    def test_agent_inventory_decodes_no_reported_session(self) -> None:
        reports: list[dict[str, object]] = [{}, {"agent_session": {"kind": "pending"}}]
        for report in reports:
            with self.subTest(report=report):
                self.configure_herdr(["agent", "list"], json.dumps({"result": {"agents": [{
                    "pane_id": "worker", "agent": "codex", "agent_status": "idle", **report,
                }]}}))
                self.assertEqual(self.herdr.agents()[0]["agent_session"], herdr.NoReportedAgentSession())

    def test_agent_inventory_rejects_a_malformed_session_report(self) -> None:
        reports: list[object] = [None, "thread", [], {}, {"kind": 7}, {"kind": "id"},
                                 {"kind": "id", "value": 7}]
        for report in reports:
            with self.subTest(report=report):
                self.configure_herdr(["agent", "list"], json.dumps({"result": {"agents": [{
                    "pane_id": "worker", "agent": "codex", "agent_status": "idle", "agent_session": report,
                }]}}))
                with self.assertRaisesRegex(herdr.HerdrFailure, "herdr agent session"):
                    _ = self.herdr.agents()

    def test_label_pane_uses_a_persistent_pane_label(self) -> None:
        self.herdr.label_pane("worker", "dispatch-owner")
        self.assertEqual(self.commands()[-1], ["--session", "fixture", "pane", "rename", "worker", "dispatch-owner"])

    def test_one_codex_foreground_process_uses_its_session_report_without_a_rollout(self) -> None:
        current = fixture_runtime_id("reported-current")
        row = staff.StaffRow(staff.native_id(fixture_runtime_id("reported-fixed")), "agent", "probe",
                             staff.StaffRoot(), current_session=staff.native_id(current))
        membership = staff.StaffSnapshot((row,))
        pane = {"pane_id": "w1:p1", "workspace_id": "w1", "terminal_id": "report-terminal",
                "agent_session": {"value": current}}
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [pane]}}))
        self.configure_herdr(["pane", "process-info", "--pane", "w1:p1"], json.dumps({"result": {"process_info": {
            "shell_pid": 998070, "foreground_process_group_id": 998075,
            "foreground_processes": [{"pid": 998075, "argv": ["codex", "--no-daemon"]}]}}}))
        fake_unit_host.process(os.environ, 998075, 998070, ["codex", "--no-daemon"], {})
        (self.proc / "998075/fd").mkdir()
        with fake_unit_host.private_process_registry():
            parsed = self.herdr.panes()
            scan = self.herdr.scan_row_panes(membership, [dict(entry) for entry in parsed])
            self.assertEqual(scan.found[row.id][0]["pane_id"], "w1:p1")
            self.assertEqual(self.herdr.native_session("w1:p1", "codex", 1), current)
            self.herdr.prompt("w1:p1", "Read the next task.", target=current)
        self.assertIn("Read the next task.", self.commands()[-1][-1])

    def test_stale_report_cannot_claim_a_shell_a_claude_pane_or_two_foreground_processes(self) -> None:
        current = fixture_runtime_id("stale-pane-report")
        row = staff.StaffRow(staff.native_id(fixture_runtime_id("stale-fixed")), "agent", "probe",
                             staff.StaffRoot(), current_session=staff.native_id(current))
        membership = staff.StaffSnapshot((row,))
        pane: dict[str, object] = {"pane_id": "w1:p1", "workspace_id": "w1", "agent_session": {"value": current}}
        for arguments in ((["zsh"],), (["claude"],), (["codex"], ["zsh"])):
            with self.subTest(arguments=arguments):
                foreground: list[dict[str, object]] = []
                for offset, argv in enumerate(arguments):
                    pid = 998075 + offset
                    fake_unit_host.process(os.environ, pid, 998070, argv, {})
                    (self.proc / str(pid) / "fd").mkdir(exist_ok=True)
                    foreground.append({"pid": pid, "argv": argv})
                self.configure_herdr(["pane", "process-info", "--pane", "w1:p1"], json.dumps({"result": {
                    "process_info": {"shell_pid": 998070, "foreground_process_group_id": 998075,
                                     "foreground_processes": foreground}}}))
                with fake_unit_host.private_process_registry(), redirect_stderr(io.StringIO()):
                    scan = self.herdr.scan_row_panes(membership, [pane])
                self.assertEqual(scan.found, {})

    def test_rollout_wins_and_resume_accepts_an_empty_or_matching_pane_report(self) -> None:
        current = fixture_runtime_id("herdr-codex-launch")
        other = fixture_runtime_id("stale-report-loser")

        def configure(report: str, info: dict[str, object]) -> tuple[staff.StaffSnapshot, dict[str, object]]:
            rows = tuple(staff.StaffRow(staff.native_id(fixture_runtime_id("fixed-" + value)), "agent", name,
                                       staff.StaffRoot(), current_session=staff.native_id(value))
                         for name, value in (("native", current), ("reported", other)))
            pane: dict[str, object] = {"pane_id": "w1:p1", "workspace_id": "w1", "terminal_id": "terminal",
                                       "agent_session": {"value": report}}
            self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [pane]}}))
            process_info = herdr.json_object(info["process_info"], "fixture process info")
            process_info.update(shell_pid=998069, foreground_process_group_id=998070)
            self.configure_herdr(["pane", "process-info", "--pane", "w1:p1"], json.dumps({"result": info}))
            return staff.StaffSnapshot(rows), pane

        for source, report in (("rollout", other), ("resume", other), ("resume", ""), ("resume", current)):
            with self.subTest(source=source, report=report):
                with tempfile.TemporaryDirectory(dir=self.root) as folder:
                    argv = ["codex", "resume", current] if source == "resume" else ["codex", "--no-daemon"]
                    with codex_launch_evidence(Path(folder), argv) as info:
                        membership, pane = configure(report, info)
                        with redirect_stderr(io.StringIO()):
                            scan = self.herdr.scan_row_panes(membership, [pane])
                        if source == "rollout" or report in ("", current):
                            self.assertEqual(scan.found[membership.rows[0].id], [pane])
                        else:
                            self.assertEqual(scan.found, {})
                            with patch.object(self.herdr, "pane_row", return_value={"pane_id": "w1:p1",
                                              "workspace_id": "w1", "terminal_id": "terminal", "session_id": report}):
                                with self.assertRaises(herdr.HerdrFailure):
                                    self.herdr.prompt("w1:p1", "Do not send this task.", target=current)
                            self.assertEqual([command for command in self.commands()
                                              if command[2:4] == ["agent", "prompt"]], [])

    def test_unreadable_pane_session_reports_one_anomaly_and_keeps_the_pane(self) -> None:
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [{
            "pane_id": "w1:p1", "workspace_id": "w1", "terminal_id": "terminal", "agent_session": {"value": 7}}]}}))
        with redirect_stderr(io.StringIO()) as errors:
            panes = self.herdr.panes()
        self.assertEqual([(pane["pane_id"], pane["session_id"]) for pane in panes], [("w1:p1", None)])
        self.assertEqual(errors.getvalue().splitlines(), ["herdr: pane session is unreadable"])

    def test_unmarked_child_cannot_inherit_its_directors_staff_identity(self) -> None:
        with patch.dict(os.environ, {"STAFF_ID": "s123456789ab", ('' + 'SHOWRUNNER_' + 'UNIT'): "old",
                                     ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "alpha", "KEPT_SETTING": "kept"}):
            environment = herdr.unmarked_environment()
        self.assertNotIn("STAFF_ID", environment)
        self.assertNotIn(('' + 'SHOWRUNNER_' + 'UNIT'), environment)
        self.assertNotIn(('' + 'SHOWRUNNER_' + 'UNIT_ID'), environment)
        self.assertEqual(environment["KEPT_SETTING"], "kept")


    def configure(self, arguments: list[str], stdout: str = "", code: int = 0, stderr: str = "") -> None:
        self.responses[" ".join(arguments)] = {"stdout": stdout, "code": code, "stderr": stderr}
        _ = self.responses_path.write_text(json.dumps(self.responses))

    def commands(self) -> list[list[str]]:
        return [cast(list[str], json.loads(line)) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def server_creation(self, session: str = "fixture", *, scopes: bool = True) -> list[str]:
        """The transient server unit's systemd-run argv; with pane scopes, the server's SHELL is the wrapper."""
        shell = [f"--setenv=SHELL={self.root}/home/.local/state/herdr/pane-shell",
                 "--setenv=HERDR_PANE_SHELL=/bin/sh"] if scopes else []
        return ["--user", f"--unit=herdr-{session}.service", "--collect", "-p", "CPUWeight=1000", "-p", "MemoryLow=1G",
                "-p", "MemorySwapMax=0", *shell, str(self.fake), "--session", session, "server"]

    def start_commands(self) -> list[list[str]]:
        """Startup commands, excluding read-only lifecycle observations."""
        return [command for command in self.commands()
                if command[2:4] not in (["agent", "get"], ["pane", "process-info"])]

    def configure_herdr(self, arguments: list[str], stdout: str = "", code: int = 0, stderr: str = "") -> None:
        self.configure(["--session", "fixture", *arguments], stdout, code, stderr)

    def configure_startup_shell(self, pane: str) -> None:
        """Supply private shell facts for the shared target check."""
        if "--session fixture pane list" not in self.responses:
            self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
                {"pane_id": pane, "terminal_id": "terminal-one", "workspace_id": "w1"}]}}))
        if not (self.proc / "10").exists():
            self.startup_process(10, ["zsh"])
        self.configure_herdr(["pane", "process-info", "--pane", pane], self.startup_observation(["zsh"]))

    def configure_tab_start(self, pane: str, label: str, session_id: str) -> list[str]:
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": pane, "label": label}]}}))
        return ["agent", "start", label, "--kind", "claude", "--pane", pane,
                "--", "--session-id", session_id]

    def process(self, pid: int, parent: int, arguments: list[str], environment: dict[str, str]) -> Path:
        process = self.proc / str(pid)
        process.mkdir()
        _ = process.joinpath("stat").write_text(f"{pid} (fixture with ) parenthesis) S {parent} 0 0\n")
        _ = process.joinpath("cmdline").write_bytes("\0".join([*arguments, ""]).encode())
        _ = process.joinpath("environ").write_bytes("\0".join([*(f"{key}={value}" for key, value in environment.items()), ""]).encode())
        process.joinpath("cwd").symlink_to(self.root)
        for session in list(self.process_pane_replies):
            self.configure_process_panes(session)
        return process

    def configure_process_panes(self, session: str) -> None:
        """Give unchanged fake processes corresponding live panes, unless a test sets its own reply."""
        key = f"--session {session} pane list"
        response = self.responses.get(key)
        if response is not None and response != {"stdout": self.process_pane_replies.get(session), "code": 0, "stderr": ""}:
            return
        ids = {entry.split("=", 1)[1] for process in self.proc.iterdir() if process.name.isdecimal()
               for entry in (process / "environ").read_text().split("\0") if entry.startswith("HERDR_PANE_ID=")}
        text = json.dumps({"result": {"panes": [
            {"pane_id": pane, "workspace_id": pane.split(":")[0]} for pane in sorted(ids)]}})
        self.process_pane_replies[session] = text
        self.configure(["--session", session, "pane", "list"], text)

    def unix_socket(self, path: Path, inode: int, *, listening: bool = True) -> None:
        table = self.proc / "net" / "unix"
        table.parent.mkdir(exist_ok=True)
        flags = "00010000" if listening else "00000000"
        if not table.exists():
            _ = table.write_text("Num RefCount Protocol Flags Type St Inode Path\n")
        with table.open("a") as output:
            _ = output.write(f"0000000000000000: 00000002 00000000 {flags} 0001 01 {inode} {path}\n")
        if listening:
            session = "default" if path.parent.name == "herdr" else path.parent.name
            self.configure_process_panes(session)

    def socket_fd(self, process: Path, inode: int, *, fd: int = 3) -> None:
        directory = process / "fd"
        directory.mkdir(exist_ok=True)
        directory.joinpath(str(fd)).symlink_to(f"socket:[{inode}]")

    def darwin(self) -> None:
        _ = cast(object, self.enterContext(patch.object(herdr, "_PLATFORM", "darwin")))
        _ = cast(object, self.enterContext(patch.object(sys, "platform", "darwin")))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HERDR_PS": str(self.fake), "HERDR_LSOF": str(self.fake), "LAUNCHCTL": str(self.fake),
            "SHUTDOWN_OPEN": str(self.fake),
        })))

        self.configure(["managername"], "Aqua\n")

    def darwin_environment(self, pid: int, command: str, environment: str) -> None:
        self.configure(["-o", "command=", "-p", str(pid)], command + "\n")
        self.configure(["eww", "-o", "command=", "-p", str(pid)], command + " " + environment + "\n")

    def darwin_sockets(self, text: str) -> None:
        self.configure(["-n", "-P", "-a", "-U", "-u", str(os.getuid()), "-F", "pfn"], text)

    def darwin_process_table(self, text: str) -> None:
        executables: list[str] = []
        commands: list[str] = []
        for line in text.splitlines():
            pid, parent, uid, command = line.split(maxsplit=3)
            executables.append(f"{pid} {parent} {uid} {command.split()[0]}")
            commands.append(f"{pid} {command}")
        self.configure(["-U", str(os.getuid()), "-o", "pid=,ppid=,uid=,comm="], "\n".join(executables))
        self.configure(["-U", str(os.getuid()), "-o", "pid=,command="], "\n".join(commands))

    def darwin_startup_table(self, text: str, *, started: str = "Tue Nov  3 08:00:00 2026") -> None:
        """Supply process facts without an environment or a proc tree."""
        self.darwin_process_table(text)
        states: list[str] = []
        generations: list[herdr.AgentProcessIdentity] = []
        microseconds = int(datetime.strptime(started, "%a %b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc).timestamp()) * 1_000_000 + 123456
        for line in text.splitlines():
            pid, parent, _uid, _command = line.split(maxsplit=3)
            states.append(f"{pid} {parent} S {started}")
            generations.append(herdr.AgentProcessIdentity(int(pid), microseconds))
        _ = self.enterContext(native_process_commands(11, generations=tuple(generations)))
        self.configure(["-axo", "pid=,ppid=,state=,lstart="], "\n".join(states))
        self.configure(["-o", "tpgid=", "-p", "10"], "10\n")

    def darwin_startup_pane(self, arguments: list[str]) -> None:
        """Use the existing Mac command fake and reject unexpected process reads."""
        self.darwin()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"HERDR_PROC": str(self.root / "missing-proc")})))
        self.responses["default"] = {"stdout": "", "code": 1, "stderr": "unexpected process query"}
        self.configure_herdr(["pane", "process-info", "--pane", "pane-1"], self.startup_observation(arguments))

    def test_darwin_startup_at_shell_needs_no_proc_or_environment(self) -> None:
        self.darwin_startup_pane(["zsh"])
        self.darwin_startup_table(f"10 1 {os.getuid()} /bin/zsh\n")
        self.assertEqual(self.herdr.startup_processes("pane-1", "claude", 1),
                         herdr.PaneStartupObservation(herdr.PaneShell(10), herdr.PaneAtShell()))
        self.assertEqual(STARTUP_PROCESSES(herdr.PaneShell(10), "claude", set()), herdr.PaneAtShell())
        self.assertFalse(any("eww" in command for command in self.commands()))

    def test_darwin_startup_reports_foreground_work(self) -> None:
        self.darwin_startup_pane(["/usr/bin/vim", "plan.md"])
        self.darwin_startup_table(f"10 1 {os.getuid()} /bin/zsh\n11 10 {os.getuid()} /usr/bin/vim plan.md\n")
        self.configure(["-o", "tpgid=", "-p", "10"], "11\n")
        self.assertEqual(self.herdr.startup_processes("pane-1", "claude", 1).activity,
                         herdr.PaneRunning(("/usr/bin/vim",)))
        self.assertEqual(STARTUP_PROCESSES(herdr.PaneShell(10), "claude", set()),
                         herdr.PaneRunning(("/usr/bin/vim",)))

    def test_darwin_startup_identifies_native_and_node_agents_under_the_shell(self) -> None:
        self.darwin_startup_pane(["zsh"])
        for kind, arguments in (("claude", ["/opt/bin/claude", "--resume", "thread"]),
                                ("codex", ["/opt/bin/codex", "resume", "thread"]),
                                ("claude", ["/opt/bin/node", "/opt/lib/@anthropic-ai/claude-code/cli.js"]),
                                ("codex", ["/opt/bin/node", "/opt/lib/@openai/codex/bin/codex.js"])):
            with self.subTest(kind=kind, arguments=arguments):
                self.configure_herdr(["pane", "process-info", "--pane", "pane-1"], self.startup_observation(arguments))
                self.darwin_startup_table(f"10 1 {os.getuid()} /bin/zsh\n11 10 {os.getuid()} {shlex.join(arguments)}\n")
                observation = self.herdr.startup_processes("pane-1", kind, 1)
                self.assertIsInstance(observation.activity, herdr.AgentProcessPresent)
                assert isinstance(observation.activity, herdr.AgentProcessPresent)
                self.assertEqual(len(observation.activity.identities), 1)
                identity = observation.activity.identities[0]
                self.assertEqual(identity.pid, 11)
                self.assertIsInstance(identity.start_ticks, int)
                assert isinstance(identity.start_ticks, int)
                self.assertGreater(identity.start_ticks, 0)
                tracked: set[herdr.AgentProcessIdentity] = set()
                self.assertEqual(STARTUP_PROCESSES(observation.shell, kind, tracked), observation.activity)
                self.assertEqual(tracked, {identity})
        self.assertFalse(any("eww" in command for command in self.commands()))

    def test_darwin_startup_retains_exec_and_parent_changes_but_drops_reused_pids(self) -> None:
        self.darwin_startup_pane(["zsh"])
        shell = f"10 1 {os.getuid()} /bin/zsh\n"
        self.darwin_startup_table(shell + f"11 10 {os.getuid()} /opt/bin/claude\n")
        tracked: set[herdr.AgentProcessIdentity] = set()
        first = STARTUP_PROCESSES(herdr.PaneShell(10), "claude", tracked)
        self.assertIsInstance(first, herdr.AgentProcessPresent)
        assert isinstance(first, herdr.AgentProcessPresent)
        self.assertEqual({identity.pid for identity in first.identities}, {11})
        self.darwin_startup_table(shell + f"11 1 {os.getuid()} /opt/bin/runtime\n")
        self.assertEqual(STARTUP_PROCESSES(herdr.PaneShell(10), "claude", tracked), first)
        self.darwin_startup_table(shell + f"11 1 {os.getuid()} /opt/bin/runtime\n",
                                  started="Tue Nov  3 08:00:01 2026")
        self.assertEqual(STARTUP_PROCESSES(herdr.PaneShell(10), "claude", tracked), herdr.PaneAtShell())
        self.assertEqual(tracked, set())

    def test_darwin_startup_discards_a_process_that_exits_during_the_walk(self) -> None:
        self.darwin_startup_pane(["zsh"])
        shell = f"10 1 {os.getuid()} /bin/zsh\n"
        self.darwin_startup_table(shell + "11 10 0 /usr/bin/sudo\n")
        self.darwin_process_table(shell)
        self.configure(["-o", "command=", "-p", "11"], code=1)
        with patch.object(os, "kill", side_effect=ProcessLookupError):
            self.assertEqual(STARTUP_PROCESSES(herdr.PaneShell(10), "claude", set()), herdr.PaneAtShell())
        self.assertIn(["-o", "command=", "-p", "11"], self.commands())

    def test_darwin_startup_keeps_a_child_from_the_same_process_snapshot(self) -> None:
        self.darwin_startup_pane(["zsh"])
        self.darwin_startup_table(f"10 1 {os.getuid()} /bin/zsh\n11 10 {os.getuid()} /opt/bin/claude\n")
        # Earlier process tables cannot describe a child that starts later.
        self.configure(["-axo", "pid=,state=,lstart="], "10 S Tue Nov  3 08:00:00 2026\n")
        self.configure(["-axo", "pid=,ppid="], "10 1\n11 10\n")
        tracked: set[herdr.AgentProcessIdentity] = set()
        activity = STARTUP_PROCESSES(herdr.PaneShell(10), "claude", tracked)
        self.assertIsInstance(activity, herdr.AgentProcessPresent)
        assert isinstance(activity, herdr.AgentProcessPresent)
        self.assertEqual({identity.pid for identity in activity.identities}, {11})
        self.assertEqual(self.commands().count(["-axo", "pid=,ppid=,state=,lstart="]), 1)
        self.assertNotIn(["-axo", "pid=,ppid="], self.commands())

    def test_darwin_startup_reports_another_users_foreground_command(self) -> None:
        self.darwin_startup_pane(["zsh"])
        shell = f"10 1 {os.getuid()} /bin/zsh\n"
        self.darwin_startup_table(shell + "11 10 0 /usr/bin/sudo /usr/bin/vim plan.md\n")
        self.darwin_process_table(shell)
        self.configure(["-o", "command=", "-p", "11"], "/usr/bin/sudo /usr/bin/vim plan.md\n")
        self.configure(["-o", "tpgid=", "-p", "10"], "11\n")
        self.assertEqual(STARTUP_PROCESSES(herdr.PaneShell(10), "claude", set()),
                         herdr.PaneRunning(("/usr/bin/sudo",)))
        self.assertIn(["-o", "command=", "-p", "11"], self.commands())

    def test_darwin_startup_keeps_agent_presence_when_its_pid_is_gone_or_missing(self) -> None:
        self.darwin_startup_pane(["claude"])
        self.darwin_startup_table(f"10 1 {os.getuid()} /bin/zsh\n")
        self.assertEqual(self.herdr.startup_processes("pane-1", "claude", 1).activity,
                         herdr.AgentProcessPresent(()))
        reply = self.startup_observation(["claude"]).replace('"pid": 11, ', "")
        self.configure_herdr(["pane", "process-info", "--pane", "pane-1"], reply)
        self.assertEqual(self.herdr.startup_processes("pane-1", "claude", 1).activity,
                         herdr.AgentProcessPresent(()))

    def test_darwin_startup_passes_the_callers_timeout_to_each_process_read(self) -> None:
        self.darwin_startup_pane(["claude"])
        shell = f"10 1 {os.getuid()} /bin/zsh\n"
        self.darwin_startup_table(shell + f"11 10 {os.getuid()} /opt/bin/claude\n12 10 0 /usr/bin/sudo\n")
        self.darwin_process_table(shell + f"11 10 {os.getuid()} /opt/bin/claude\n")
        self.configure(["-o", "command=", "-p", "12"], "/usr/bin/sudo /usr/bin/vim plan.md\n")
        run_program = herdr.run_program
        timeouts: list[float] = []

        def read(argv: list[str], timeout: float = 10.0, *,
                 environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            if argv[1:3] != ["--session", "fixture"]:
                timeouts.append(timeout)
            return run_program(argv, timeout=timeout, environment=environment)

        with (patch.object(herdr, "time", SimpleNamespace(monotonic=lambda: 0.0)),
              patch.object(herdr, "run_program", read)):
            activity = self.herdr.startup_processes("pane-1", "claude", 0.75).activity
            self.assertIsInstance(activity, herdr.AgentProcessPresent)
            reader = STARTUP_PROCESS_READER(0.75)
            self.assertEqual(reader.arguments(11), ["/opt/bin/claude"])
            self.assertEqual(reader.arguments(12), ["/usr/bin/sudo", "/usr/bin/vim", "plan.md"])
            self.assertEqual(reader.foreground(10), 10)
        self.assertEqual(timeouts, [0.75] * 6)

    def test_darwin_startup_reads_no_process_snapshot_without_an_agent_pid(self) -> None:
        self.darwin_startup_pane(["zsh"])
        self.assertEqual(self.herdr.startup_processes("pane-1", "claude", 1).activity, herdr.PaneAtShell())
        reply = self.startup_observation(["claude"]).replace('"pid": 11, ', "")
        self.configure_herdr(["pane", "process-info", "--pane", "pane-1"], reply)
        self.assertEqual(self.herdr.startup_processes("pane-1", "claude", 1).activity,
                         herdr.AgentProcessPresent(()))
        self.assertEqual(self.commands(), [["--session", "fixture", "pane", "process-info", "--pane", "pane-1"]] * 2)

    def test_darwin_startup_reports_a_deadline_stopped_read_as_unreadable(self) -> None:
        self.darwin_startup_pane(["zsh"])
        self.darwin_startup_table(f"10 1 {os.getuid()} /bin/zsh\n11 10 {os.getuid()} /usr/bin/vim\n")
        clock = StartupClock()
        reads: list[tuple[float, float]] = []

        def read(argv: list[str], timeout: float = 10.0, *,
                 environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            reads.append((clock.elapsed, timeout))
            clock.elapsed += min(0.1, timeout)
            if timeout <= 0.1:
                raise subprocess.TimeoutExpired(argv, timeout)
            return self.run_fake(argv, timeout, environment=environment)

        with (patch.object(herdr, "time", SimpleNamespace(monotonic=lambda: clock.elapsed)),
              patch.object(herdr, "run_program", read)):
            activity = STARTUP_PROCESSES(herdr.PaneShell(10), "claude", set(), timeout=0.25)
        self.assertEqual(activity, herdr.PaneProcessesUnreadable())
        self.assertEqual(len(reads), 3)
        self.assertTrue(all(0 < timeout <= 0.25 - called_at for called_at, timeout in reads), reads)
        self.assertAlmostEqual(clock.elapsed, 0.25)


    def test_darwin_cleanup_does_not_signal_a_pid_reused_between_generation_reads(self) -> None:
        self.darwin_startup_pane(["claude"])
        self.darwin_startup_table(f"10 1 {os.getuid()} /bin/zsh\n11 10 {os.getuid()} /opt/bin/claude\n")
        original = herdr.AgentProcessIdentity(11, NATIVE_PROCESS_MICROSECONDS)
        replacement = herdr.AgentProcessIdentity(11, NATIVE_PROCESS_MICROSECONDS + 1)
        with (native_process_commands(11, generations=(original, replacement)),
              patch.object(os, "kill") as signalled):
            self.assertTrue(herdr.check_target(original, action="signal", shell=herdr.PaneShell(10)))
            with self.assertRaisesRegex(herdr.HerdrFailure, "reused process"):
                _ = herdr.check_target(original, action="signal", shell=herdr.PaneShell(10))
        signalled.assert_not_called()


    def test_darwin_startup_keeps_a_reparented_agent_after_weak_generation_recovers(self) -> None:
        self.darwin_startup_pane(["claude"])
        self.darwin_startup_table(f"10 1 {os.getuid()} /bin/zsh\n11 1 {os.getuid()} /opt/bin/claude\n")
        original = herdr.AgentProcessIdentity(11, NATIVE_PROCESS_MICROSECONDS)
        weak = herdr.AgentProcessIdentity(11, herdr.ProcessGenerationUnavailable("proc_pidinfo denied"))
        tracked = {original}
        with native_process_commands(11, generations=(weak, weak, original, original)):
            first = STARTUP_PROCESSES(herdr.PaneShell(10), "claude", tracked)
            first_tracked = set(tracked)
            if isinstance(first, herdr.AgentProcessPresent):
                tracked.update(first.identities)
            second = STARTUP_PROCESSES(herdr.PaneShell(10), "claude", tracked)
        assert not isinstance(second, herdr.PaneProcessesUnreadable)
        client = Mock(spec=herdr.Herdr)
        cast(Mock, client.result).return_value = {"pane": {"pane_id": "pane-1"}}
        cast(Mock, client.startup_processes).return_value = herdr.PaneStartupObservation(herdr.PaneShell(10), second)
        cast(Mock, client.pane_activity).return_value = herdr.PaneAtShell()
        with (native_process_commands(11, generations=(weak,)),
              patch.object(herdr, "Herdr", return_value=client),
              patch.object(os, "kill") as signalled,
              patch.object(herdr, "os", SimpleNamespace(environ=os.environ, getuid=os.getuid, kill=signalled)),
              patch.object(herdr, "start_agent", side_effect=herdr.HerdrFailure("start failed"))):
            _ = herdr.launch_prepared_agent("fixture", herdr.SplitPane("parent", "right", 0.5, "worker"),
                herdr.PreparedAgentEnvironment(self.root, {}), "claude", [],
                herdr.NoOpeningPrompt(), 1000, agent="worker")
        self.assertEqual(first_tracked, {original})
        self.assertIn(original, tracked)
        self.assertEqual(second, herdr.AgentProcessPresent((original,)))
        self.assertIsInstance(first, herdr.AgentProcessPresent)
        assert isinstance(first, herdr.AgentProcessPresent)
        self.assertEqual(len(first.identities), 1)
        self.assertIsInstance(first.identities[0].start_ticks, herdr.ProcessGenerationUnavailable)
        signalled.assert_not_called()
        cast(Mock, client.close_retained_pane).assert_not_called()
        cast(Mock, client.close_pane).assert_not_called()

    def test_linux_startup_keeps_children_read_before_a_missing_thread_list_but_not_after(self) -> None:
        self.startup_process(10, ["zsh"])
        self.startup_process(11, ["claude"])
        tasks = self.proc / "10" / "task"
        first = tasks / "1"
        _ = (tasks / "10").rename(first)
        second = tasks / "2"
        second.mkdir()
        (first / "children").unlink()
        iterdir = Path.iterdir

        def threads(path: Path) -> Generator[Path]:
            if path == tasks:
                yield from order
            else:
                yield from iterdir(path)

        for child_thread in (first, second):
            child_list = child_thread / "children"
            _ = child_list.write_text("11")
            expected = (herdr.AgentProcessPresent((herdr.AgentProcessIdentity(11, 1100),))
                        if child_thread == first else herdr.PaneProcessesUnreadable())
            for order in ([first, second], [second, first]):
                with self.subTest(child_thread=child_thread.name, threads=order), patch.object(Path, "iterdir", threads):
                    self.assertEqual(STARTUP_PROCESSES(herdr.PaneShell(10), "claude", set()), expected)
            child_list.unlink()

    def test_darwin_startup_reports_failed_ps_as_unreadable(self) -> None:
        self.darwin_startup_pane(["/usr/bin/vim", "plan.md"])
        for query in (["-axo", "pid=,ppid=,state=,lstart="],
                      ["-U", str(os.getuid()), "-o", "pid=,ppid=,uid=,comm="],
                      ["-U", str(os.getuid()), "-o", "pid=,command="],
                      ["-o", "tpgid=", "-p", "10"]):
            with self.subTest(query=query):
                self.darwin_startup_table(f"10 1 {os.getuid()} /bin/zsh\n11 10 {os.getuid()} /usr/bin/vim plan.md\n")
                self.configure(query, code=1, stderr="process table denied")
                self.assertEqual(STARTUP_PROCESSES(herdr.PaneShell(10), "claude", set()),
                                 herdr.PaneProcessesUnreadable())

    def run_fake(self, argv: list[str], timeout: float = 10.0, *,
                 environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        """Replay the command fake in process while Popen models the server child."""
        self.assertGreater(timeout, 0)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, environment or {}), redirect_stdout(stdout), redirect_stderr(stderr):
            code = fake_herdr.main(argv[1:])
        return subprocess.CompletedProcess(argv, code, stdout.getvalue(), stderr.getvalue())

    def detached_server(self) -> tuple[Mock, Mock]:
        self.darwin()
        _ = self.enterContext(patch.object(herdr, "security_session", return_value=herdr.DesktopSession()))
        _ = self.enterContext(patch.object(herdr, "run_program", side_effect=self.run_fake))
        poll = Mock(return_value=None)
        child = SimpleNamespace(pid=42, poll=poll, wait=Mock(return_value=0))
        _ = self.enterContext(patch.object(os, "killpg"))
        self.darwin_sockets(f"p42\nf3\nn{self.root}/config/herdr/sessions/fixture/herdr.sock\n")
        spawn = self.enterContext(patch.object(subprocess, "Popen", return_value=child))
        return spawn, poll

    def test_security_session_distinguishes_aqua_background_and_failed_queries(self) -> None:
        self.darwin()
        for answer, code, expected in (("Aqua\n", 0, herdr.DesktopSession),
                                       ("Background\n", 0, herdr.NonDesktopSession),
                                       ("", 1, herdr.SessionKindUnreadable)):
            with self.subTest(answer=answer, code=code):
                self.configure(["managername"], answer, code, "manager unavailable" if code else "")
                self.assertIsInstance(herdr.security_session(), expected)
        self.assertEqual(self.commands(), [["managername"]] * 3)

    def test_non_desktop_restore_opens_attach_before_waiting_for_its_listener(self) -> None:
        self.darwin()
        status = "--session fixture status server"
        sockets = f"-n -P -a -U -u {os.getuid()} -F pfn"
        self.responses[status] = [{"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: stopped\n", "code": 0},
                                  {"stdout": "status: running\n", "code": 0}]
        self.responses[sockets] = [{"stdout": "", "code": 0}]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        clock = StartupClock()
        def advance(seconds: float) -> None:
            clock.elapsed += seconds
        with patch.object(herdr, "time", SimpleNamespace(monotonic=lambda: clock.elapsed, sleep=advance)):
            owned = herdr.restore_server("fixture", 2000, session_kind=herdr.NonDesktopSession())
        self.assertEqual(owned, herdr.WindowServerCreated())
        calls = self.commands()
        opener = ["-na", "/Applications/Nix Apps/Ghostty.app", "--args", "-e", str(self.fake),
                  "session", "attach", "fixture"]
        self.assertEqual(calls[2], opener)
        self.assertEqual(calls[3:5], [["--session", "fixture", "status", "server"]] * 2)
        self.assertFalse(any(row[2:] == ["server"] or row[2:4] == ["workspace", "close"] for row in calls))


    def test_darwin_server_start_does_not_inherit_the_heal_proof(self) -> None:
        spawn, _ = self.detached_server()
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0}]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        handoff = {herdr.nix_env.HEAL_SESSION_ENV: "healed-session",
                   herdr.nix_env.HEAL_DIRECTORY_ENV: str(self.root),
                   "IN_NIX_SHELL": "impure", "NIX_LDFLAGS": "-Lprepared"}
        with patch.dict(os.environ, handoff):
            self.assertIsInstance(herdr.start_server("fixture", 90_000), herdr.ServerCreated)
            spawn.assert_called_once()
            environment = cast(dict[str, str], spawn.call_args.kwargs["env"])
            self.assertNotIn(herdr.nix_env.HEAL_SESSION_ENV, environment)
            self.assertNotIn(herdr.nix_env.HEAL_DIRECTORY_ENV, environment)
            self.assertEqual(environment["IN_NIX_SHELL"], "impure")
            self.assertEqual(environment["NIX_LDFLAGS"], "-Lprepared")
            self.assertEqual(os.environ[herdr.nix_env.HEAL_SESSION_ENV], "healed-session")
            self.assertEqual(os.environ[herdr.nix_env.HEAL_DIRECTORY_ENV], str(self.root))


    def test_child_commands_drop_heal_proof_from_inherited_and_explicit_environments(self) -> None:
        handoff = {herdr.nix_env.HEAL_SESSION_ENV: "healed-session",
                   herdr.nix_env.HEAL_DIRECTORY_ENV: str(self.root), "IN_NIX_SHELL": "impure"}
        with (patch.dict(os.environ, handoff),
              patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(["probe"], 0, "", "")) as run):
            for environment in (None, handoff):
                with self.subTest(environment=environment):
                    _ = herdr.run_program(["probe"], environment=environment)
                    child_environment = cast(dict[str, str], run.call_args.kwargs["env"])
                    self.assertNotIn(herdr.nix_env.HEAL_SESSION_ENV, child_environment)
                    self.assertNotIn(herdr.nix_env.HEAL_DIRECTORY_ENV, child_environment)
                    self.assertEqual(child_environment["IN_NIX_SHELL"], "impure")
            self.assertEqual(handoff[herdr.nix_env.HEAL_SESSION_ENV], "healed-session")

    def test_agent_start_cli_does_not_inherit_the_heal_proof(self) -> None:
        handoff = {herdr.nix_env.HEAL_SESSION_ENV: "healed-session",
                   herdr.nix_env.HEAL_DIRECTORY_ENV: str(self.root), "IN_NIX_SHELL": "impure"}
        process = WaitingAgentCLI(StartupClock(), 0, lambda: None)
        with (patch.dict(os.environ, handoff),
              patch.object(herdr.Herdr, "startup_processes", side_effect=herdr.HerdrFailure("fixture has no shell")),
              patch.object(subprocess, "Popen", return_value=process) as spawn):
            result = self.herdr.start_command(["agent-start"], "claude", "pane", 1.0)
        self.assertEqual(result.returncode, 0)
        spawn.assert_called_once()
        environment = cast(dict[str, str], spawn.call_args.kwargs["env"])
        self.assertNotIn(herdr.nix_env.HEAL_SESSION_ENV, environment)
        self.assertNotIn(herdr.nix_env.HEAL_DIRECTORY_ENV, environment)
        self.assertEqual(environment["IN_NIX_SHELL"], "impure")

    def test_linux_client_discovery_in_an_isolated_copy_does_not_need_darwin_launcher(self) -> None:
        _ = self.process(42, 1, ["herdr", "session", "attach", "fixture"], {})
        isolated = self.root / "isolated/scripts/herdr"
        isolated.mkdir(parents=True)
        _ = shutil.copy2(Path(herdr.__file__), isolated / "herdr.py")
        _ = shutil.copy2(Path(herdr.__file__).with_name("place.py"), isolated / "place.py")
        _ = shutil.copy2(Path(herdr.__file__).with_name("pane_launch.py"), isolated / "pane_launch.py")
        message = isolated.parent / "message"
        copy_session_modules(message)
        _ = shutil.copytree(Path(herdr.__file__).resolve().parents[1] / "lib", isolated.parent / "lib", dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "test_*"))
        for relative in ("agents/worker_lifecycle.py", "production/staff.py", "shutdown/remote.py"):
            destination = isolated.parent / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            _ = shutil.copy2(Path(herdr.__file__).resolve().parents[1] / relative, destination)
        probe = isolated.parent.parent / "probe.py"
        _ = probe.write_text(
            "from scripts.herdr import herdr; " +
            "herdr._PLATFORM='linux'; print(herdr.attached_clients('fixture'))"
        )
        result = subprocess.run([BASH_BINARY, str(isolated.parent / "lib/py"), "--isolated", str(probe)],
                                env={**os.environ, "CLAUDE_PYTHON": sys.executable},
                                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ClientsAttached(count=1)\n")
        self.assertEqual(result.stderr, "")

    def test_darwin_running_server_is_adopted_without_spawning_or_clearing(self) -> None:
        spawn, _ = self.detached_server()
        self.configure_herdr(["status", "server"], "status: running\n")
        self.assertIsInstance(herdr.start_server("fixture", 90_000), herdr.ServerAdopted)
        spawn.assert_not_called()
        self.assertEqual(self.commands(), [["--session", "fixture", "status", "server"]])

    def test_darwin_never_clears_a_server_answering_after_our_child_exited(self) -> None:
        _, poll = self.detached_server()
        poll.side_effect = [None, 1]
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0}]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        with self.assertRaisesRegex(herdr.HerdrFailure, "herdr server for fixture did not start"):
            _ = herdr.start_server("fixture", 90_000)
        self.assertEqual(self.commands(), [["--session", "fixture", "status", "server"]] * 2 + [
            ["-n", "-P", "-a", "-U", "-u", str(os.getuid()), "-F", "pfn"]])

    def test_darwin_competing_listener_is_adopted_while_our_child_is_alive_or_after_it_exits(self) -> None:
        _, poll = self.detached_server()
        self.darwin_startup_table(f"42 1 {os.getuid()} /fixture/herdr server\n")
        socket = self.root / "config/herdr/sessions/fixture/herdr.sock"
        self.darwin_sockets(f"p77\nf3\nn{socket}\np42\nf4\nn{socket.parent}/other.sock\n")
        for exited in (False, True):
            with self.subTest(exited=exited), patch.object(os, "kill") as stop:
                self.log.unlink(missing_ok=True)
                if exited:
                    poll.side_effect = [None, 1]
                self.responses["--session fixture status server"] = [
                    {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0}]
                _ = self.responses_path.write_text(json.dumps(self.responses))
                self.assertIsInstance(herdr.start_server("fixture", 90_000), herdr.ServerAdopted)
                if not exited:
                    stop.assert_called_once_with(42, signal.SIGTERM)
                self.assertFalse(any(command[2:3] == ["workspace"] for command in self.commands()))

    def test_darwin_status_answer_without_our_socket_never_clears_workspaces(self) -> None:
        _ = self.detached_server()
        self.darwin_sockets("")
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0}]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        clock = StartupClock()

        def advance(seconds: float) -> None:
            clock.elapsed += seconds

        with (patch.object(time, "monotonic", side_effect=lambda: clock.elapsed),
              patch.object(time, "sleep", side_effect=advance),
              self.assertRaisesRegex(herdr.HerdrFailure, "did not start")):
            _ = herdr.start_server("fixture", 250)
        self.assertFalse(any(command[2:3] == ["workspace"] for command in self.commands()))

    def test_darwin_readiness_probes_share_the_startup_deadline(self) -> None:
        _ = self.detached_server()
        clock = StartupClock()
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0}]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        budgets: list[float] = []

        def slow_probe(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            budgets.append(timeout)
            if argv[1] == "-n":
                clock.elapsed += timeout
                raise subprocess.TimeoutExpired(argv, timeout)
            clock.elapsed += 2
            return self.run_fake(argv, timeout)

        with (patch.object(time, "monotonic", side_effect=lambda: clock.elapsed),
              patch.object(herdr, "run_program", side_effect=slow_probe),
              self.assertRaisesRegex(herdr.HerdrFailure, "did not start")):
            _ = herdr.start_server("fixture", 90_000)
        self.assertEqual(budgets, [10, 8, 6])
        self.assertEqual(clock.elapsed, 10)

    def test_darwin_identity_preserves_registry_start_time_and_pins_locale_and_zone(self) -> None:
        self.darwin()
        started = "Thu Oct  1 12:00:00 2026"
        self.configure(["-o", "lstart=,tty=", "-p", "42"], f"  {started} ttys001\n")
        with patch.object(herdr, "run_program", wraps=herdr.run_program) as probe:
            identity = herdr.darwin_process_identity(42)
        self.assertEqual(identity.started, started)
        self.assertEqual(identity.terminal, 1)
        self.assertEqual(tuple(identity), (started, 1))
        self.assertEqual(probe.call_args.args[0], [str(self.fake), "-o", "lstart=,tty=", "-p", "42"])
        self.assertEqual(probe.call_args.kwargs["timeout"], 5)
        environment = cast(dict[str, str], probe.call_args.kwargs["environment"])
        self.assertEqual(environment, {**os.environ, "LC_ALL": "C", "TZ": "UTC"})

    def test_darwin_open_files_preserves_absolute_paths_and_distinguishes_errors_from_no_matches(self) -> None:
        self.darwin()
        rollout = self.root / "account with spaces/sessions/rollout.jsonl"
        self.configure(["-a", "-p", "42", "-Fn"], f"p42\nf8\nn{rollout}\nf9\nnrelative\nf10\nn/dev/ttys001\n")
        self.assertEqual(herdr.darwin_open_files(42), [rollout, Path("/dev/ttys001")])
        self.assertEqual(self.commands(), [["-a", "-p", "42", "-Fn"]])
        self.configure(["-a", "-p", "42", "-Fn"], code=1)
        self.assertEqual(herdr.darwin_open_files(42), [])
        self.configure(["-a", "-p", "42", "-Fn"], code=1, stderr="permission denied")
        with self.assertRaisesRegex(herdr.HerdrFailure, "process 42 open files.*permission denied"):
            _ = herdr.darwin_open_files(42)

    def test_darwin_environment_uses_only_the_suffix_after_unchanged_arguments(self) -> None:
        self.darwin()
        command = 'claude --prompt "HERDR_PANE_ID=forged CLAUDE_CONFIG_DIR=/wrong"'
        self.darwin_environment(42, command, "HERDR_PANE_ID=w1:p1 CLAUDE_CONFIG_DIR=/real account VALUE=a=b")
        self.assertEqual(herdr.darwin_process_environment(42), {
            "HERDR_PANE_ID": "w1:p1", "CLAUDE_CONFIG_DIR": "/real account", "VALUE": "a=b"})
        self.darwin_environment(43, command, "")
        self.assertEqual(herdr.darwin_process_environment(43), {})

    def test_darwin_process_columns_preserve_spaced_executables_and_argument_states(self) -> None:
        self.darwin()
        uid = os.getuid()
        ghostty = "/Applications/Nix Apps/Ghostty.app/Contents/MacOS/ghostty"
        drive = "/Applications/Google Drive.app/Contents/MacOS/Google Drive"
        herdr_path = "/Applications/Nix Apps/herdr"
        self.configure(["-U", str(uid), "-o", "pid=,ppid=,uid=,comm="],
            f"40 1 {uid} {ghostty}\n41 40 {uid} {drive}\n42 40 {uid} {herdr_path}\n" +
            f"43 40 {uid} {herdr_path}\n44 40 {uid} {herdr_path}\n45 40 {uid} {herdr_path}\n")
        self.configure(["-U", str(uid), "-o", "pid=,command="],
            f"40 {ghostty} --working-directory '/work folder'\n41 {drive} --auto_restart_count=1\n" +
            f"42 {herdr_path} --session writing\n43 (herdr)\n44 changed-executable --session writing\n" +
            f"45 {herdr_path} 'unterminated\n")
        rows = herdr._darwin_processes()  # pyright: ignore[reportPrivateUsage]
        self.assertEqual([row.executable for row in rows], [ghostty, drive, *([herdr_path] * 4)])
        self.assertEqual(rows[0].arguments, herdr.ReadProcessArguments((ghostty, "--working-directory", "/work folder")))
        self.assertEqual(rows[1].arguments, herdr.ReadProcessArguments((drive, "--auto_restart_count=1")))
        self.assertEqual(rows[2].arguments, herdr.ReadProcessArguments((herdr_path, "--session", "writing")))
        for row in rows[3:]:
            self.assertIsInstance(row.arguments, herdr.UnreadableProcessArguments)
        self.assertEqual(self.commands(), [
            ["-U", str(uid), "-o", "pid=,ppid=,uid=,comm="],
            ["-U", str(uid), "-o", "pid=,command="]])

    def test_darwin_process_listing_keeps_float_timeouts_and_skips_a_timed_out_fallback(self) -> None:
        self.darwin()
        uid = os.getuid()
        self.configure(["-U", str(uid), "-o", "pid=,ppid=,uid=,comm="],
                       f"42 1 {uid} /opt/bin/herdr\n43 1 {uid} /opt/bin/launcher\n")
        self.configure(["-U", str(uid), "-o", "pid=,command="], "42 /opt/bin/herdr --session writing\n")
        for fallback_times_out in (False, True):
            with self.subTest(fallback_times_out=fallback_times_out):
                clock = StartupClock()
                budgets: list[float] = []

                def read(argv: list[str], timeout: float = 10.0, *,
                         environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                    budgets.append(timeout)
                    clock.elapsed += 0.2
                    if argv[1:] == ["-o", "command=", "-p", "43"]:
                        if fallback_times_out:
                            raise subprocess.TimeoutExpired(argv, timeout)
                        return subprocess.CompletedProcess(argv, 0, "/opt/bin/launcher\n", "")
                    return self.run_fake(argv, timeout, environment=environment)

                with (patch.object(herdr, "time", SimpleNamespace(monotonic=lambda: clock.elapsed)),
                      patch.object(herdr, "run_program", read)):
                    rows = herdr._darwin_processes(timeout=0.75)  # pyright: ignore[reportPrivateUsage]
                self.assertEqual([row.pid for row in rows], [42] if fallback_times_out else [42, 43])
                self.assertEqual(rows[0].arguments, herdr.ReadProcessArguments(("/opt/bin/herdr", "--session", "writing")))
                self.assertEqual(budgets, [0.75, 0.75, 0.75])

    def test_darwin_unreadable_herdr_arguments_report_unreadable_clients(self) -> None:
        self.darwin()
        uid = os.getuid()
        self.configure(["-U", str(uid), "-o", "pid=,ppid=,uid=,comm="],
                       f"42 1 {uid} /Applications/Nix Apps/herdr\n")
        self.configure(["-U", str(uid), "-o", "pid=,command="], "42 (herdr)\n")
        self.assertIsInstance(herdr.attached_clients("writing"), herdr.ClientsUnreadable)

    def test_darwin_spaced_herdr_path_detects_attached_clients_and_unreadable_environment(self) -> None:
        self.darwin()
        uid = os.getuid()
        executable = "/Applications/Nix Apps/herdr"
        self.configure(["-U", str(uid), "-o", "pid=,ppid=,uid=,comm="], f"42 1 {uid} {executable}\n")
        self.configure(["-U", str(uid), "-o", "pid=,command="], f"42 {executable} session attach writing\n")
        self.assertEqual(herdr.attached_clients("writing"), herdr.ClientsAttached(1))
        self.configure(["-U", str(uid), "-o", "pid=,command="], f"42 {executable}\n")
        self.configure(["-o", "command=", "-p", "42"], code=1, stderr="permission denied")
        self.assertIsInstance(herdr.attached_clients("writing"), herdr.ClientsUnreadable)

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

    def test_darwin_user_tmux_is_never_attributed_to_its_outer_herdr_pane(self) -> None:
        self.darwin()
        uid = os.getuid()
        self.darwin_process_table(f"70 1 {uid} herdr server\n71 70 {uid} claude\n72 70 {uid} claude\n")
        self.darwin_sockets(f"p70\nf3\nn{self.root}/config/herdr/sessions/writing/herdr.sock\n")
        self.configure(["--session", "writing", "pane", "list"], json.dumps({"result": {"panes": [{'pane_id': 'w1:p1', 'workspace_id': 'w1'}, {'pane_id': 'w1:p2', 'workspace_id': 'w1'}]}}))
        self.darwin_environment(71, "claude", "HERDR_PANE_ID=w1:p1 TMUX_PANE=%1")
        self.darwin_environment(72, "claude", "HERDR_PANE_ID=w1:p2 TMUX_PANE=")
        for pid in (71, 72):
            self.configure(["-a", "-p", str(pid), "-d", "cwd", "-Fn"], f"p{pid}\nfcwd\nn{self.root}\n")
        self.assertEqual(herdr.herdr_place_of(71), herdr.NotInHerdr())
        self.assertEqual(herdr.herdr_place_of(72), herdr.HerdrPlace("writing", "w1:p2"))
        self.assertEqual([row.pid for row in herdr.herdr_processes()], [72])

    def test_darwin_process_discovery_uses_sockets_uid_cwd_and_real_environment(self) -> None:
        self.darwin()
        uid = os.getuid()
        self.darwin_process_table(
            f"70 1 {uid} herdr server\n71 70 {uid} zsh\n72 71 {uid} claude\n" +
            f"73 71 {uid + 1} claude\n74 71 {uid} claude HERDR_PANE_ID=forged\n" +
            f"75 71 {uid} codex\n76 71 {uid} claude\n77 1 {uid} unrelated\n")
        self.darwin_sockets(f"p70\nf3\nn{self.root}/config/herdr/sessions/writing/herdr.sock\n")
        self.configure(["--session", "writing", "pane", "list"], json.dumps({"result": {"panes": [{'pane_id': 'w1:p1', 'workspace_id': 'w1'}, {'pane_id': 'w1:p2', 'workspace_id': 'w1'}]}}))
        self.darwin_environment(71, "zsh", "HERDR_PANE_ID=w1:p1 CODEX_THREAD_ID=unit")
        self.darwin_environment(72, "claude", "HERDR_PANE_ID=w1:p1")
        self.darwin_environment(74, "claude HERDR_PANE_ID=forged", "")
        self.configure(["-o", "command=", "-p", "75"], code=1, stderr="vanished")
        self.darwin_environment(76, "claude", "HERDR_PANE_ID=w1:p2")
        self.configure(["-a", "-p", "71", "-d", "cwd", "-Fn"], "p71\nfcwd\nn/work folder\n")
        self.configure(["-a", "-p", "72", "-d", "cwd", "-Fn"], "p72\nfcwd\nn/work folder\n")
        self.configure(["-a", "-p", "76", "-d", "cwd", "-Fn"], code=1, stderr="gone")
        rows = herdr.herdr_processes()
        self.assertCountEqual(rows, [
            herdr.HerdrProcess(71, herdr.HerdrPlace("writing", "w1:p1"), {"CODEX_THREAD_ID": "unit"}, "zsh", "/work folder"),
            herdr.HerdrProcess(72, herdr.HerdrPlace("writing", "w1:p1"), {}, "claude", "/work folder"),
            herdr.HerdrProcess(76, herdr.HerdrPlace("writing", "w1:p2"), {}, "claude",
                               herdr.UnreadableCwd("process 76 cwd: stderr: gone"))])
        self.assertEqual(herdr.herdr_place_of(72), herdr.HerdrPlace("writing", "w1:p1"))
        self.assertEqual(herdr.herdr_place_of(74), herdr.NotInHerdr())
        for pid in (73, 77):
            self.assertNotIn(["eww", "-o", "command=", "-p", str(pid)], self.commands())

    def test_darwin_live_marked_process_remains_placed_when_cwd_is_unreadable(self) -> None:
        self.darwin()
        uid = os.getuid()
        self.darwin_process_table(
                       f"70 1 {uid} herdr server\n71 70 {uid} zsh\n72 71 {uid} claude\n")
        self.darwin_sockets(f"p70\nf3\nn{self.root}/config/herdr/sessions/writing/herdr.sock\n")
        self.configure(["--session", "writing", "pane", "list"], json.dumps({"result": {"panes": [{'pane_id': 'w1:p1', 'workspace_id': 'w1'}]}}))
        self.darwin_environment(71, "zsh", "HERDR_PANE_ID=w1:p1 CODEX_THREAD_ID=unit")
        self.darwin_environment(72, "claude", "HERDR_PANE_ID=w1:p1")
        for output, code, error in (("", 1, "permission denied"), ("p72\nfcwd\n", 0, ""),
                                    ("n/one\nn/two\n", 0, "")):
            with self.subTest(output=output, code=code):
                for pid in (71, 72):
                    self.configure(["-a", "-p", str(pid), "-d", "cwd", "-Fn"], output, code, error)
                processes = {process.pid: process for process in herdr.herdr_processes()}
                self.assertCountEqual(processes, [71, 72])
                self.assertEqual(processes[71].marks, {"CODEX_THREAD_ID": "unit"})
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
        self.darwin_process_table(table + f"99 1 {uid + 1} herdr session attach writing\n")
        self.darwin_environment(81, commands[1], "")
        self.darwin_environment(82, "herdr", "HERDR_SESSION=writing")
        self.assertEqual(herdr.attached_clients("writing"), herdr.ClientsAttached(3))
        self.assertEqual(herdr.attached_clients("missing"), herdr.NoClientAttached())
        for pid in (83, 84, 85, 86, 99):
            self.assertNotIn(["eww", "-o", "command=", "-p", str(pid)], self.commands())
        self.configure(["-U", str(uid), "-o", "pid=,ppid=,uid=,comm="], code=1, stderr="cannot read processes")
        self.assertIsInstance(herdr.attached_clients("writing"), herdr.ClientsUnreadable)

    def test_darwin_attached_clients_skip_clients_confirmed_gone_during_environment_reads(self) -> None:
        self.darwin()
        uid = os.getuid()
        for stage in ("command", "environment", "confirmation"):
            with self.subTest(stage=stage):
                self.log.unlink(missing_ok=True)
                self.darwin_process_table(f"81 1 {uid} herdr\n82 1 {uid} herdr session attach writing\n")
                self.darwin_environment(81, "herdr", "HERDR_SESSION=writing")
                if stage == "environment":
                    self.configure(["eww", "-o", "command=", "-p", "81"], code=1)
                elif stage == "confirmation":
                    self.responses["-o command= -p 81"] = [
                        {"stdout": "herdr\n", "code": 0}, {"stdout": "", "code": 1}]
                    _ = self.responses_path.write_text(json.dumps(self.responses))
                else:
                    self.configure(["-o", "command=", "-p", "81"], code=1)
                with patch.object(os, "kill", side_effect=ProcessLookupError("no such process")) as probe:
                    self.assertEqual(herdr.attached_clients("writing"), herdr.ClientsAttached(1))
                probe.assert_called_once_with(81, 0)
                self.assertIn(["-o", "command=", "-p", "81"], self.commands())

    def test_darwin_attached_clients_skip_clients_confirmed_gone_between_process_scans(self) -> None:
        self.darwin()
        uid = os.getuid()
        self.darwin_process_table(f"81 1 {uid} herdr\n82 1 {uid} herdr session attach writing\n")
        self.configure(["-U", str(uid), "-o", "pid=,command="], "82 herdr session attach writing\n")
        self.configure(["-o", "command=", "-p", "81"], code=1)
        with patch.object(os, "kill", side_effect=ProcessLookupError("no such process")) as probe:
            self.assertEqual(herdr.attached_clients("writing"), herdr.ClientsAttached(1))
        probe.assert_called_once_with(81, 0)
        self.assertEqual(self.commands(), [
            ["-U", str(uid), "-o", "pid=,ppid=,uid=,comm="],
            ["-U", str(uid), "-o", "pid=,command="],
            ["-o", "command=", "-p", "81"]])

    def test_darwin_attached_clients_preserve_unconfirmed_process_read_failures(self) -> None:
        self.darwin()
        uid = os.getuid()
        failures: list[tuple[str, CommandResponse, OSError | None]] = [
            ("pid still exists", {"stdout": "", "code": 1}, None),
            ("pid probe denied", {"stdout": "", "code": 1}, PermissionError("not permitted")),
            ("pid probe failed", {"stdout": "", "code": 1}, OSError("probe failed")),
            ("ps permission denied", {"stdout": "", "stderr": "permission denied", "code": 1},
             ProcessLookupError("no such process")),
            ("ps partial output", {"stdout": "herdr\n", "code": 1}, ProcessLookupError("no such process")),
            ("ps malformed output", {"stdout": "(herdr)\n", "code": 0}, ProcessLookupError("no such process")),
            ("ps empty success", {"stdout": "", "code": 0}, ProcessLookupError("no such process")),
        ]
        for stage in ("environment read", "argument scan"):
            for reason, response, probe_error in failures:
                with self.subTest(stage=stage, reason=reason):
                    self.log.unlink(missing_ok=True)
                    self.darwin_process_table(f"81 1 {uid} herdr\n82 1 {uid} herdr session attach writing\n")
                    self.darwin_environment(81, "herdr", "HERDR_SESSION=writing")
                    if stage == "argument scan":
                        self.configure(["-U", str(uid), "-o", "pid=,command="], "82 herdr session attach writing\n")
                    self.responses["-o command= -p 81"] = response
                    _ = self.responses_path.write_text(json.dumps(self.responses))
                    with patch.object(os, "kill", side_effect=probe_error, return_value=None) as probe:
                        self.assertIsInstance(herdr.attached_clients("writing"), herdr.ClientsUnreadable)
                    if reason in {"pid still exists", "pid probe denied", "pid probe failed"}:
                        probe.assert_called_once_with(81, 0)
                    else:
                        probe.assert_not_called()

    def test_darwin_handoff_finds_reparented_pane_shells_and_their_agents(self) -> None:
        self.darwin()
        uid = os.getuid()
        self.darwin_process_table(
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

    def test_session_of_keeps_the_terminal_but_clears_the_conversation_after_claude_exits(self) -> None:
        pane, session_id = "w1:p2", "71356b6b-bf7b-4996-b27e-49da9f90b719"
        start = self.configure_tab_start(pane, "alpha", session_id)
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [
            {"workspace_id": "w1", "label": "production"}]}}))
        marks = {"HERDR_SESSION": "fixture", "HERDR_PANE_ID": pane}
        _ = self.process(4241, 1, ["zsh"], marks)
        running = self.process(4242, 4241, ["claude"], marks)
        client = self.herdr
        self.assertEqual(client.session_of("alpha"), herdr.TabSession(pane, "alpha", "production", None))
        _ = client.run(*start)
        self.assertTrue(running.exists())
        expected = herdr.TabSession(pane, "alpha", "production", session_id)
        self.assertEqual(client.session_of("alpha"), expected)
        terminal = client.panes()[0].get("terminal_id", "")
        self.assertTrue(terminal)
        shutil.rmtree(running)
        self.assertFalse(running.exists())
        self.assertEqual(client.session_of("alpha"), herdr.TabSession(pane, "alpha", "production", None))
        raw = herdr.json_object(herdr.json_items(client.result("pane", "list")["panes"], "panes")[0], "pane")
        self.assertEqual(raw["workspace_id"], "w1")
        self.assertIsNone(raw["agent_session"])
        self.assertEqual(raw["terminal_id"], terminal)

    def test_moved_terminal_clears_its_conversation_when_the_agent_with_old_pane_marks_exits(self) -> None:
        pane, session_id = "w1:p2", "71356b6b-bf7b-4996-b27e-49da9f90b719"
        start = self.configure_tab_start(pane, "alpha", session_id)
        marks = {"HERDR_SESSION": "fixture", "HERDR_PANE_ID": pane}
        shell = self.process(4241, 1, ["zsh"], marks)
        agent = self.process(4242, 4241, ["claude"], marks)
        _ = self.herdr.run(*start)
        before = self.herdr.panes()[0]
        self.assertEqual(before["session_id"], session_id)
        terminal = before.get("terminal_id", "")
        self.assertTrue(terminal)
        self.configure_herdr(["pane", "move", pane, "--new-tab", "--workspace", "w2"], json.dumps({"result": {
            "move_result": {"changed": True, "created_tab": {"tab_id": "w2:t7"}, "pane": {"pane_id": "w2:p9"}}}}))
        moved = "w2:p9"
        _ = self.herdr.result("pane", "move", pane, "--new-tab", "--workspace", "w2")
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": moved, "label": "alpha"}]}}))
        after_move = self.herdr.panes()[0]
        self.assertEqual(after_move["session_id"], session_id)
        self.assertEqual(after_move.get("terminal_id"), terminal)
        inherited = (shell / "environ").read_bytes()
        shutil.rmtree(agent)
        after_exit = self.herdr.panes()[0]
        self.assertEqual(after_exit["pane_id"], moved)
        self.assertIsNone(after_exit["session_id"])
        self.assertEqual(after_exit.get("terminal_id"), terminal)
        self.assertEqual((shell / "environ").read_bytes(), inherited)

    def test_a_failed_agent_start_does_not_supply_a_pane_conversation(self) -> None:
        start = self.configure_tab_start("w1:p2", "alpha", "71356b6b-bf7b-4996-b27e-49da9f90b719")
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": "w1:p2", "label": "alpha", "terminal_id": "terminal-alpha"}]}}))
        self.configure_herdr(start, code=1, stderr="agent failed to start")
        with self.assertRaisesRegex(herdr.HerdrFailure, "agent failed to start"):
            _ = self.herdr.run(*start)
        self.assertEqual(self.herdr.panes(), [{"pane_id": "w1:p2", "label": "alpha",
                                               "workspace_id": "w1", "session_id": None,
                                               "terminal_id": "terminal-alpha"}])

    def test_an_agent_start_with_a_missing_required_path_does_not_supply_a_conversation(self) -> None:
        start = self.configure_tab_start("w1:p2", "alpha", "71356b6b-bf7b-4996-b27e-49da9f90b719")
        self.responses[" ".join(["--session", "fixture", *start])] = {
            "stdout": "{}", "code": 0, "require_paths": [str(self.root / "missing")]}
        self.configure([], "")
        result = self.run_fake(self.herdr.argv(*start))
        self.assertEqual(result.returncode, 98)
        self.assertIn("fixture required path missing", result.stderr)
        self.assertEqual(self.herdr.session_of("alpha"), herdr.TabSession("w1:p2", "alpha", "", None))

    def test_tab_sessions_reads_panes_once_and_joins_workspace_labels_once(self) -> None:
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": "w1:p2", "workspace_id": "w2", "label": "alpha",
             "agent_session": {"agent": "claude", "kind": "id", "source": "herdr:claude", "value": "sid-alpha"}},
            {"pane_id": "w2:p1", "workspace_id": "w1", "label": "beta", "agent_session": None}]}}))
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [
            {"workspace_id": "w1", "label": "first"}, {"workspace_id": "w2", "label": "second"}]}}))
        client = self.herdr
        with patch.object(client, "panes", wraps=client.panes) as read_panes:
            self.assertEqual(client.tab_sessions(), [herdr.TabSession("w1:p2", "alpha", "second", "sid-alpha"),
                                                     herdr.TabSession("w2:p1", "beta", "first", None)])
        read_panes.assert_called_once_with()
        self.assertEqual(self.commands(), [["--session", "fixture", "pane", "list"],
                                           ["--session", "fixture", "workspace", "list"]])

    def test_tab_sessions_omits_panes_without_a_label(self) -> None:
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": "w1:p1"}, {"pane_id": "w1:p2", "label": None},
            {"pane_id": "w1:p3", "label": "named"}]}}))
        self.assertEqual(self.herdr.tab_sessions(), [herdr.TabSession("w1:p3", "named", "", None)])

    def test_tab_sessions_keeps_panes_with_missing_or_unlabelled_workspaces(self) -> None:
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": "w1:p1", "label": "missing"}, {"pane_id": "w2:p1", "label": "unlabelled"}]}}))
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [
            {"workspace_id": "w2", "label": None}]}}))
        self.assertEqual(self.herdr.tab_sessions(), [herdr.TabSession("w1:p1", "missing", "", None),
                                                     herdr.TabSession("w2:p1", "unlabelled", "", None)])

    def test_session_of_keeps_exact_label_and_ambiguity_rules(self) -> None:
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": "w1:p1", "label": "alpha"}, {"pane_id": "w1:p2", "label": "alpha-extra"},
            {"pane_id": "w2:p1", "label": "duplicate"}, {"pane_id": "w3:p1", "label": "duplicate"},
            {"pane_id": "w1:p3", "label": ""}, {"pane_id": "w1:p4", "label": None}]}}))
        client = self.herdr
        cases = [("alpha", herdr.TabSession("w1:p1", "alpha", "", None)),
                 ("", herdr.TabSession("w1:p3", "", "", None)),
                 ("Alpha", herdr.NoPane("Alpha")),
                 ("alph", herdr.NoPane("alph")),
                 ("missing", herdr.NoPane("missing")),
                 ("duplicate", herdr.SeveralPanes("duplicate", 2))]
        for label, tab in cases:
            with self.subTest(label=label):
                self.assertEqual(client.session_of(label), tab)

    def test_panes_reads_session_values_and_allows_absent_conversations(self) -> None:
        sessions: tuple[object, ...] = (None, {}, {"value": None}, {"value": "sid-alpha"})
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": f"w1:p{index}", "workspace_id": "w2", "label": "alpha", "agent_session": session,
             "terminal_id": f"terminal-{index}"}
            for index, session in enumerate(sessions)]}}))
        self.assertEqual(self.herdr.panes(), [
            {"pane_id": f"w1:p{index}", "workspace_id": "w2", "label": "alpha", "session_id": session_id,
             "terminal_id": f"terminal-{index}"}
            for index, session_id in enumerate((None, None, None, "sid-alpha"))])

    def test_panes_preserves_tab_and_focus_when_the_transport_supplies_them(self) -> None:
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": "w1:p1", "terminal_id": "terminal-w1:p1", "workspace_id": "w1", "tab_id": "w1:t1", "focused": True},
            {"pane_id": "w1:p2", "terminal_id": "terminal-w1:p2", "workspace_id": "w1", "tab_id": "w1:t1", "focused": False},
            {"pane_id": "w2:p1", "terminal_id": "terminal-w2:p1", "workspace_id": "w2"}]}}))

        self.assertEqual(self.herdr.panes(), [
            {"pane_id": "w1:p1", "terminal_id": "terminal-w1:p1", "workspace_id": "w1",
             "label": None, "session_id": None, "tab_id": "w1:t1", "focused": True},
            {"pane_id": "w1:p2", "terminal_id": "terminal-w1:p2", "workspace_id": "w1",
             "label": None, "session_id": None, "tab_id": "w1:t1", "focused": False},
            {"pane_id": "w2:p1", "terminal_id": "terminal-w2:p1", "workspace_id": "w2",
             "label": None, "session_id": None}])

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

    def test_server_start_does_not_poll_after_a_unit_creation_failure(self) -> None:
        self.configure_herdr(["status", "server"], "status: stopped\n")
        creation = self.server_creation()
        self.configure(creation, code=1, stderr="fixture manager unavailable")
        with self.assertRaisesRegex(herdr.HerdrFailure, "fixture manager unavailable"):
            _ = herdr.start_server("fixture", 10_000)
        self.assertEqual(self.commands(), [["--session", "fixture", "status", "server"], SCOPE_PROBE, creation])

    def test_server_start_polls_after_launch(self) -> None:
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0},
        ]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        outcome = herdr.start_server("fixture", 10_000)
        self.assertIsInstance(outcome, herdr.ServerCreated)
        self.assertEqual(self.commands(), [
            ["--session", "fixture", "status", "server"],
            SCOPE_PROBE,
            self.server_creation(),
            ["--session", "fixture", "status", "server"],
            ["--user", "is-active", "herdr-fixture.service"],
        ])


    def test_the_shared_session_starts_its_boot_unit_and_keeps_its_restored_layout(self) -> None:
        shared = self.root / "config/herdr/shared-session"
        shared.parent.mkdir(parents=True)
        _ = shared.write_text("fixture\n")
        self.configure(["--user", "show", "-p", "FragmentPath", "--value", "herdr-fixture.service"],
                       "/etc/systemd/user/herdr-fixture.service\n")
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0},
            {"stdout": "status: running\n", "code": 0},
        ]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        started = [
            ["--session", "fixture", "status", "server"],
            ["--user", "show", "-p", "FragmentPath", "--value", "herdr-fixture.service"],
            ["--user", "start", "herdr-fixture.service"],
            ["--session", "fixture", "status", "server"],
            ["--user", "is-active", "herdr-fixture.service"],
        ]
        _ = herdr.start_server("fixture", 10_000)
        self.assertEqual(self.commands(), started)
        _ = shared.with_name("config.toml").write_text("[session]\nresume_agents_on_restore = false\n")
        self.log.unlink()
        _ = self.responses_path.write_text(json.dumps(self.responses | {"--session fixture status server": [
            {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0}]}))
        _ = herdr.start_server("fixture", 10_000)
        self.assertEqual(self.commands(), started)
        # The boot unit sets its own properties and shell: no probe, no wrapper.
        self.assertFalse((self.root / "home/.local/state/herdr").exists())

    def test_answering_server_with_inactive_spawned_unit_is_never_cleared(self) -> None:
        self.configure(["--user", "is-active", "herdr-fixture.service"], "inactive\n", code=3)
        self.configure_herdr(["workspace", "list"], json.dumps({
            "result": {"workspaces": [{"workspace_id": "w1"}]},
        }))
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0},
            {"stdout": "status: running\n", "code": 0},
        ]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        self.assertIsInstance(herdr.start_server("fixture", 10_000), herdr.ServerAdopted)
        self.assertEqual(self.commands(), [
            ["--session", "fixture", "status", "server"],
            SCOPE_PROBE,
            self.server_creation(),
            ["--session", "fixture", "status", "server"],
            ["--user", "is-active", "herdr-fixture.service"],
        ])

    def test_stop_stops_only_named_active_unit(self) -> None:
        herdr.stop_session("fixture")
        self.assertEqual(self.commands(), [
            ["--session", "fixture", "server", "stop"], ["--user", "is-active", "herdr-fixture.service"],
            ["--user", "stop", "herdr-fixture.service"],
        ])

    def share(self) -> None:
        folder = self.root / "config/herdr"
        folder.mkdir(parents=True, exist_ok=True)
        _ = (folder / "shared-session").write_text("fixture\n")

    def test_programs_and_servers_never_inherit_a_units_marks(self) -> None:
        with patch.dict(os.environ, {"SHOWRUNNER_" + "UNIT": "show/demo-unit", "SHOWRUNNER_" + "UNIT_ID": "id-demo", "KEPT": "yes"}), \
                patch.object(subprocess, "run") as run:
            _ = herdr.run_program(["true"])
            given = cast(dict[str, str], run.call_args.kwargs["env"])
            self.assertEqual(given["KEPT"], "yes")
            self.assertNotIn(('' + 'SHOWRUNNER_' + 'UNIT'), given)
            self.assertNotIn(('' + 'SHOWRUNNER_' + 'UNIT_ID'), given)
            _ = herdr.run_program(["true"], environment={('' + 'SHOWRUNNER_' + 'UNIT'): "explicit"})
            self.assertEqual(run.call_args.kwargs["env"], {})

    def test_shared_session_is_agents_until_named(self) -> None:
        self.assertEqual(herdr.shared_session(), "agents")
        self.share()
        self.assertEqual(herdr.shared_session(), "fixture")

    def test_attach_hint_names_the_shared_session_workspace_and_tab(self) -> None:
        self.assertEqual(herdr.attach_hint("build-followups", "alpha"),
                         "herdr session attach agents, workspace build-followups, tab alpha")
        self.share()
        self.assertEqual(herdr.attach_hint("solo", "solo"),
                         "herdr session attach fixture, workspace solo, tab solo")

    def test_open_tab_creates_the_production_workspace_once_then_adds_tabs(self) -> None:
        members = {name: fixture_member("showrunner", name=name) for name in ("boss", "alpha")}
        options = ["--label", "boss", "--cwd", "/repo", "--env",
                   "CLAUDE_CODE_SESSION_ID=" + members["boss"].current_session, "--env", "HERDR_AGENT_PANE=1", "--no-focus"]
        self.configure_herdr(["workspace", "create", *options], json.dumps({"result": {
            "root_pane": {"pane_id": "w1:p1"}, "tab": {"tab_id": "w1:t1"}}}))
        self.configure_herdr(["tab", "list"], json.dumps({"result": {"tabs": []}}))
        environment = {('' + 'SHOWRUNNER_' + 'UNIT'): "prod",
                       "CLAUDE_CODE_SESSION_ID": members["boss"].current_session, herdr.nix_env.HEAL_SESSION_ENV: "healed-session",
                       herdr.nix_env.HEAL_DIRECTORY_ENV: "/repo"}
        self.assertEqual(herdr.open_tab("fixture", "boss", "boss", "/repo", environment), "w1:p1")
        self.assertEqual(self.commands()[1:4], [["--session", "fixture", "workspace", "create", *options],
                                                ["--session", "fixture", "tab", "rename", "w1:t1", "boss"],
                                                ["--session", "fixture", "pane", "rename", "w1:p1", "boss"]])
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [
            {"workspace_id": "w1", "label": "boss"}]}}))
        tab = ["tab", "create", "--workspace", "w1", "--label", "alpha", "--cwd", "/wt", "--env", "HERDR_AGENT_PANE=1", "--no-focus"]
        self.configure_herdr(tab, json.dumps({"result": {"root_pane": {"pane_id": "w1:p2"}}}))
        tabs = [{"tab_id": "w1:t1", "label": "boss", "pane_count": 1, "workspace_id": "w1"},
                {"tab_id": "w1:t2", "label": "alpha", "pane_count": 1, "workspace_id": "w1"}]
        for arguments in (["tab", "list", "--workspace", "w1"], ["tab", "list"]):
            self.configure_herdr(arguments, json.dumps({"result": {"tabs": tabs}}))
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": f"w1:p{index}", "tab_id": f"w1:t{index}",
             "agent_session": {"value": members[name].current_session}}
            for index, name in enumerate(("boss", "alpha"), 1)]}}))
        self.enterContext(fake_unit_host.private_process_registry())
        for index, name in enumerate(("boss", "alpha"), 1):
            pid = 992000 + index
            fake_unit_host.process(os.environ, pid, 1, ["claude"], {})
            registry = sessions.sessions_dir() / f"{pid}.json"
            registry.parent.mkdir(parents=True, exist_ok=True)
            _ = registry.write_text(json.dumps({"pid": pid, "procStart": "1", "name": name,
                "sessionId": members[name].current_session, "updatedAt": 1, "messagingSocketPath": ""}))
            self.configure_herdr(["pane", "process-info", "--pane", f"w1:p{index}"], json.dumps({"result": {
                "process_info": {"shell_pid": 990000 + index, "foreground_process_group_id": pid,
                    "foreground_processes": [{"pid": pid, "name": "claude", "argv": ["claude"]}]}}}))
        self.assertEqual(herdr.open_tab("fixture", "boss", "alpha", "/wt", {}), "w1:p2")
        self.assertEqual([command[2:] for command in self.commands()[-11:]], [
            tab, ["pane", "rename", "w1:p2", "alpha"], ["tab", "list", "--workspace", "w1"], ["workspace", "list"],
            ["tab", "list", "--workspace", "w1"], ["pane", "list"], ["workspace", "list"], ["tab", "list"], ["pane", "list"],
            ["pane", "process-info", "--pane", "w1:p1"], ["pane", "process-info", "--pane", "w1:p2"]])
        saved = herdr.saved_layout("fixture")
        self.assertEqual([(space["name"], [tab["name"] for tab in space["tabs"]]) for space in saved["workspaces"]],
                         [("boss", layout_row_ids(["boss", "alpha"]))])

    def test_new_tab_and_split_keep_the_terminal_environment(self) -> None:
        environment = {"TERM": "dumb", "COLORTERM": "", "KEPT_SETTING": "kept"}
        tab = ["workspace", "create", "--label", "workers", "--cwd", str(self.root),
               "--env", "KEPT_SETTING=kept", "--env", "HERDR_AGENT_PANE=1", "--no-focus"]
        self.configure_herdr(tab, json.dumps({"result": {
            "root_pane": {"pane_id": "w1:p1"}, "tab": {"tab_id": "w1:t1"}}}))
        self.assertEqual(self.herdr.open_tab("workers", "parent", str(self.root), environment), "w1:p1")
        split = ["pane", "split", "w1:p1", "--direction", "right", "--ratio", "0.3",
                 "--cwd", str(self.root), "--no-focus", "--env", "KEPT_SETTING=kept",
                 "--env", "HERDR_AGENT_PANE=1"]
        self.configure_herdr(split, json.dumps({"result": {"pane": {"pane_id": "w1:p2"}}}))
        self.configure_startup_shell("w1:p2")
        launched = herdr.launch_prepared_agent("fixture", herdr.SplitPane("w1:p1", "right", 0.3, "worker"),
            herdr.PreparedAgentEnvironment(self.root, environment), "claude", [],
            herdr.NoOpeningPrompt(), 5000, agent="worker")
        self.assertEqual(launched.pane, "w1:p2")
        self.assertIsInstance(launched.outcome, herdr.PromptSubmissionNotRequired)
        creations = [command[2:] for command in self.commands()
                     if command[2:4] in (["workspace", "create"], ["pane", "split"])]
        self.assertEqual(creations, [tab, split])

    def test_existing_pane_launch_neither_exports_nor_unsets_terminal_names(self) -> None:
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": "w1:p1", "workspace_id": "w1", "terminal_id": "terminal-one"}]}}))
        self.configure_startup_shell("w1:p1")
        prepared = herdr.PreparedAgentEnvironment(self.root,
            {"TERM": "dumb", "COLORTERM": "", "KEPT_SETTING": "kept"},
            frozenset({"TERM", "COLORTERM", "REMOVED_SETTING"}))
        launched = herdr.launch_prepared_agent("fixture", herdr.ExistingPane("w1:p1", "terminal-one"),
            prepared, "claude", [], herdr.NoOpeningPrompt(), 5000, agent="worker")
        self.assertIsInstance(launched.outcome, herdr.PromptSubmissionNotRequired)
        command, = [command for command in self.commands() if command[2:4] == ["pane", "run"]]
        script = shlex.split(command[5])[1]
        exports = shlex.split(script.split(" && export ", 1)[1])
        removals = shlex.split(script.rsplit("unset ", 1)[1].split(" && export ", 1)[0])
        self.assertIn("KEPT_SETTING=kept", exports)
        self.assertIn("HERDR_AGENT_PANE=1", exports)
        self.assertIn("REMOVED_SETTING", removals)
        self.assertFalse(any(value.split("=", 1)[0] in {"TERM", "COLORTERM"} for value in exports))
        self.assertNotIn("TERM", removals)
        self.assertNotIn("COLORTERM", removals)

    def test_child_environment_keeps_term(self) -> None:
        child = herdr.child_environment({"TERM": "dumb", "COLORTERM": "", "KEPT_SETTING": "kept"})
        self.assertEqual(child, {"TERM": "dumb", "COLORTERM": "", "KEPT_SETTING": "kept"})

    def test_fake_refuses_workspace_labels_before_reading_responses(self) -> None:
        self.responses_path.unlink()
        for workspace in ("sdlc", "wlower", "w", "wD-label"):
            for command in (["tab", "list"], ["pane", "move", "w1:p2", "--new-tab"]):
                with self.subTest(workspace=workspace, command=command):
                    result = self.run_fake([str(self.fake), "--session", "fixture", *command, "--workspace", workspace])
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(herdr.decode_object(result.stderr, "workspace error"), {
                        "error": {"code": "workspace_not_found", "message": f"workspace {workspace} not found"},
                        "id": "cli:tab:list",
                    })

    def test_fake_replays_responses_for_workspace_ids(self) -> None:
        for workspace in ("wD", "wH", "w12A"):
            with self.subTest(workspace=workspace):
                command = ["tab", "list", "--workspace", workspace]
                self.configure_herdr(command, "workspace tabs")
                result = self.run_fake([str(self.fake), "--session", "fixture", *command])
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "workspace tabs", ""))


    def test_order_tabs_preserves_split_ids_and_moves_only_tabs_out_of_place(self) -> None:
        _ = unit_membership("boss", "zulu")
        cases = [(("boss", "delta", "alpha"), -1, False, ["boss", "alpha", "delta"], "alpha", 1),
                 (("boss", "zed", "alpha"), 0, False, ["boss", "boss", "alpha", "zed"], "alpha", 1),
                 (("boss", "zed", "alpha"), 2, False, ["boss", "alpha", "alpha", "zed"], "alpha", 1),
                 (("boss", "alpha", "zed"), -1, False, ["boss", "alpha", "zed"], "", 0),
                 *(( ("boss", "zulu", "zebra", "apple"), 1, reverse,
                    ["boss", "zulu", "zulu", "apple", "zebra"], "apple", 2) for reverse in (False, True))]
        for titles, split, reverse, expected, moved_title, position in cases:
            with self.subTest(titles=titles, split=split, reverse=reverse):
                client = LayoutHerdr()
                client.spaces = [("w1", "boss"), ("w2", "other")]
                for title in titles:
                    _ = client.add_tab("w1", title)
                by_title = {pane["label"]: pane for pane in client.tabs}
                if split >= 0:
                    client.tabs.insert(split if reverse else split + 1, {**client.tabs[split], "pane_id": "split-pane"})
                before = {pane["pane_id"]: pane["tab_id"] for pane in client.tabs}
                processes = [herdr.HerdrProcess(991004, herdr.HerdrPlace("fixture", by_title["zulu"]["pane_id"]),
                    {('' + 'SHOWRUNNER_' + 'UNIT'): "boss", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "zulu"}, "claude", "/repo")] if "zulu" in by_title else []
                with patch.object(herdr, "herdr_processes", return_value=processes):
                    moved = client.order_tabs("w1", herdr.SavedLayout(at=0.0, workspaces=[]))
                self.assertEqual(client.layout(), [("boss", expected), ("other", [])])
                self.assertEqual({pane["pane_id"]: pane["tab_id"] for pane in client.tabs}, before)
                self.assertEqual(moved, {pane: pane for pane, tab in before.items()
                                         if moved_title and tab == by_title[moved_title]["tab_id"]})
                self.assertEqual(client.commands, [("tab.move", by_title[moved_title]["tab_id"], str(position))] if moved_title else [])

    def test_retitle_names_the_pane_and_tab_then_orders_only_on_a_change(self) -> None:
        self.configure_herdr(["tab", "list"], json.dumps({"result": {"tabs": [
            {"tab_id": "w1:t1", "label": "boss"}, {"tab_id": "w1:t2", "label": "old-name"}]}}))
        for label, changed in (("old-name", True), ("new-name", False)):
            with self.subTest(label=label):
                self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
                    {"pane_id": "w1:p2", "tab_id": "w1:t2", "workspace_id": "w1", "label": label}]}}))
                if not changed:
                    self.configure_herdr(["tab", "list"], json.dumps({"result": {"tabs": [
                        {"tab_id": "w1:t2", "label": "new-name"}]}}))
                self.log.unlink(missing_ok=True)
                with patch.object(herdr.Herdr, "order_tabs", return_value={}) as ordered:
                    self.assertEqual(herdr.Herdr("fixture").retitle("w1:p2", "new-name", tab=True), changed)
                acted = [command[2:] for command in self.commands() if command[3:4] == ["rename"]]
                self.assertEqual(acted, [["pane", "rename", "w1:p2", "new-name"], ["tab", "rename", "w1:t2", "new-name"]]
                                 if changed else [])
                self.assertEqual(ordered.call_args_list, [call("w1", herdr.SavedLayout(at=0.0, workspaces=[]))] if changed else [])

    def test_focus_workspace_targets_the_named_session_and_reports_refusal(self) -> None:
        self.herdr.focus_workspace("w3")
        self.assertEqual(self.commands(), [["--session", "fixture", "workspace", "focus", "w3"]])
        self.configure_herdr(["workspace", "focus", "w3"], code=1, stderr="workspace no longer exists")
        with self.assertRaisesRegex(herdr.HerdrFailure, "workspace no longer exists"):
            self.herdr.focus_workspace("w3")

    def test_startup_cleanup_closes_only_the_untouched_home_shell_and_preserves_the_restored_workspace(self) -> None:
        shells = [startup_shell(name) for name in ("w1", "w2", "w3")]
        for name in ("w1", "w2", "w3"):
            self.configure_startup_shell(name + ":p1")
        workspaces = [workspace for workspace, _ in shells]
        panes = [pane for _, pane in shells]
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": workspaces}}))
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": panes}}))
        self.herdr.close_startup_workspaces("w2")
        self.assertEqual(self.commands(), [
            ["--session", "fixture", "workspace", "list"],
            ["--session", "fixture", "workspace", "list"],
            ["--session", "fixture", "pane", "list"],
            ["--session", "fixture", "pane", "process-info", "--pane", "w1:p1"],
            ["--session", "fixture", "workspace", "close", "w1"],
            ["--session", "fixture", "workspace", "list"],
            ["--session", "fixture", "pane", "list"],
            ["--session", "fixture", "pane", "process-info", "--pane", "w3:p1"],
            ["--session", "fixture", "workspace", "close", "w3"]])

    def test_startup_cleanup_closes_the_shell_from_the_captured_mac_list_entries(self) -> None:
        workspace = herdr.decode_object(STARTUP_WORKSPACE_JSON, "captured startup workspace")
        pane = herdr.decode_object(STARTUP_PANE_JSON, "captured startup pane")
        self.configure_startup_shell("wG:p1")
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [workspace]}}))
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [pane]}}))
        with (patch.object(herdr, "run_program", side_effect=self.run_fake),
              patch.object(Path, "home", return_value=Path("/Users/natemccoy"))):
            self.herdr.close_startup_workspaces("w2")
        self.assertEqual(self.commands()[-1], ["--session", "fixture", "workspace", "close", "wG"])

    def test_startup_cleanup_preserves_workspace_changed_while_another_workspace_closes(self) -> None:
        for change in ("typed input", "renamed workspace", "removed workspace"):
            with self.subTest(change=change):
                self.log.unlink(missing_ok=True)
                shells = [startup_shell(name) for name in ("w1", "w2", "w3")]
                for name in ("w1", "w2", "w3"):
                    self.configure_startup_shell(name + ":p1")
                workspaces = [workspace for workspace, _ in shells]
                panes = [pane for _, pane in shells]
                self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": workspaces}}))
                self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": panes}}))
                def close_then_change(argv: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
                    result = self.run_fake(argv, timeout)
                    if argv[1:] == ["--session", "fixture", "workspace", "close", "w1"]:
                        if change == "typed input":
                            self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
                                {**pane, "revision": 1} if pane["pane_id"] == "w3:p1" else pane for pane in panes]}}))
                        else:
                            remaining = [workspace for workspace in workspaces if workspace["workspace_id"] != "w1"
                                         and (change != "removed workspace" or workspace["workspace_id"] != "w3")]
                            self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [
                                {**workspace, "label": "my shell"} if workspace["workspace_id"] == "w3" else workspace
                                for workspace in remaining]}}))
                    return result
                with patch.object(herdr, "run_program", side_effect=close_then_change):
                    self.herdr.close_startup_workspaces("w2")
                self.assertEqual([command for command in self.commands() if command[2:4] == ["workspace", "close"]],
                                 [["--session", "fixture", "workspace", "close", "w1"]])

    def test_promptless_start_never_sends_input(self) -> None:
        _ = self.herdr.start_agent("worker", "pane-1", "claude", ["-n", "solo"], herdr.NoOpeningPrompt(), 90_000)
        self.assertEqual(self.start_commands(), [["--session", "fixture", "agent", "start", "worker", "--kind",
                                           "claude", "--pane", "pane-1", "--timeout", "90000", "--", "-n", "solo"]])

    def test_valid_name_starts_and_stops_its_exact_unit(self) -> None:
        self.configure(["--session", "a-b", "status", "server"], "status: stopped")
        _ = herdr.start_server("a-b", 0)
        herdr.stop_session("a-b")
        commands = self.commands()
        self.assertIn(self.server_creation("a-b"), commands)
        self.assertIn(["--user", "stop", "herdr-a-b.service"], commands)
        self.assertTrue(all("herdr-a b" not in part for command in commands for part in command))

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

    def test_attached_clients_skip_vanished_clients_but_report_permission_errors(self) -> None:
        disappearing = self.process(10, 1, ["herdr"], {"HERDR_SESSION": "writing"})
        _ = self.process(11, 1, ["herdr"], {"HERDR_SESSION": "writing"})
        original = Path.read_bytes
        for field in ("cmdline", "environ"):
            for failure in (FileNotFoundError, ProcessLookupError, PermissionError):
                with self.subTest(field=field, failure=failure.__name__):
                    def read(path: Path) -> bytes:
                        if path == disappearing / field:
                            raise failure("client process read")
                        return original(path)

                    with patch.object(Path, "read_bytes", read):
                        result = herdr.attached_clients("writing")
                    if failure is PermissionError:
                        self.assertEqual(result, herdr.ClientsUnreadable("client process read"))
                    else:
                        self.assertEqual(result, herdr.ClientsAttached(1))

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
        identity = fixture_runtime_id("handoff-director")
        server = self.process(30, 1, ["herdr", "server", "--handoff-import", "/fixture/import", "token"], {})
        self.unix_socket(self.root / "config/herdr/sessions/writing/herdr.sock", 100)
        self.socket_fd(server, 100)
        shell = self.process(11, 1, ["zsh"], {"HERDR_PANE_ID": "p1"})
        claude = self.process(12, 11, ["claude"], {"HERDR_PANE_ID": "p1", "HERDR_SESSION": "stale",
                              "CLAUDE_CODE_SESSION_ID": identity})
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
                                     {"CLAUDE_CODE_SESSION_ID": identity})
                    self.assertEqual(herdr.herdr_place_of(12), herdr.HerdrPlace("writing", "p1"))
                    self.assertEqual(herdr.herdr_places(), {row.pid: row.place for row in rows})

    def test_shared_scope_handoff_uses_preserved_shells_without_reading_unrelated_environments(self) -> None:
        identity = fixture_runtime_id("handoff-director")
        servers = [self.process(pid, 1, ["herdr", "server"], {}) for pid in (30, 40)]
        for server, name in zip(servers, ("writing", "other")):
            self.unix_socket(self.root / f"config/herdr/sessions/{name}/herdr.sock", int(server.name))
            self.socket_fd(server, int(server.name))
        shell = self.process(11, 1, ["zsh"], {"HERDR_PANE_ID": "w1:p1"})
        claude = self.process(12, 11, ["claude"], {"HERDR_PANE_ID": "w1:p1", "HERDR_SESSION": "stale",
                              "CLAUDE_CODE_SESSION_ID": identity})
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
                                     {"CLAUDE_CODE_SESSION_ID": identity})
                    self.assertEqual(herdr.herdr_place_of(12), herdr.HerdrPlace("writing", "w1:p1"))
                    self.assertEqual(herdr.herdr_places(), {row.pid: row.place for row in rows})

    def test_handoff_survivors_in_pane_scopes_keep_their_unit_server(self) -> None:
        server = self.process(30, 1, ["herdr", "server", "--handoff-import", "/fixture/import", "token"], {})
        self.unix_socket(self.root / "config/herdr/sessions/writing/herdr.sock", 100)
        self.socket_fd(server, 100)
        shell = self.process(11, 1, ["zsh"], {"HERDR_PANE_ID": "w1:p1"})
        claude = self.process(12, 11, ["claude"], {"HERDR_PANE_ID": "w1:p1", "HERDR_SESSION": "stale"})
        # A pane process whose parent exited: only its scope still names the pane.
        daemon = self.process(13, 1, ["daemon"], {"HERDR_PANE_ID": "w1:p1"})
        unrelated = self.process(20, 1, ["claude"], {"HERDR_PANE_ID": "w1:p1"})
        _ = self.process(5, 1, ["subreaper"], {})
        self.configure(["--session", "writing", "pane", "list"],
                       json.dumps({"result": {"panes": [{"pane_id": "w1:p1"}]}}))
        self.configure(["--session", "writing", "pane", "process-info", "--pane", "w1:p1"],
                       json.dumps({"result": {"process_info": {"shell_pid": 11}}}))
        slice_path = "/user.slice/user-1000.slice/user@1000.service/app.slice"
        expected = {pid: herdr.HerdrPlace("writing", "w1:p1") for pid in (11, 12, 13)}
        for membership in ("0::", "1:name=systemd:"):
            _ = (server / "cgroup").write_text(f"{membership}{slice_path}/herdr-writing.service\n")
            for process in (shell, claude, daemon):
                _ = (process / "cgroup").write_text(f"{membership}{slice_path}/{agents_slice()}/run-p11.scope\n")
            _ = (unrelated / "cgroup").write_text(f"{membership}{slice_path}/{agents_slice()}/run-p20.scope\n")
            for parent in (1, 5):
                _ = (shell / "stat").write_text(f"11 (zsh) S {parent} 0 0\n")
                with self.subTest(orphan_parent=parent, membership=membership):
                    rows = herdr.herdr_processes()
                    self.assertEqual({row.pid: row.place for row in rows}, expected)
                    self.assertEqual(herdr.herdr_place_of(13), herdr.HerdrPlace("writing", "w1:p1"))
                    self.assertEqual(herdr.herdr_places(), expected)
            # Without a handoff the parent walk explains the scope; one live read verifies its pane.
            _ = (shell / "stat").write_text("11 (zsh) S 30 0 0\n")
            _ = (unrelated / "cgroup").write_text(f"{membership}/user.slice/session-7.scope\n")
            self.log.unlink(missing_ok=True)
            with self.subTest(handoff=False, membership=membership):
                self.assertEqual(herdr.herdr_places(), expected)
                self.assertEqual([command for command in self.commands() if command[2:4] == ["pane", "list"]],
                                 [["--session", "writing", "pane", "list"]])

    def test_process_sessions_without_scope_reads_does_not_read_the_agent_slice(self) -> None:
        server = self.process(30, 1, ["herdr", "server"], {})
        shell = self.process(11, 30, ["zsh"], {})
        with patch.object(herdr, "agents_slice", side_effect=AssertionError("no scope read")) as setting:
            self.assertEqual(herdr._process_sessions([server, shell], {30: "writing"}),  # pyright: ignore[reportPrivateUsage]
                             {11: "writing"})
        setting.assert_not_called()

    def test_process_sessions_reads_the_agent_slice_once_for_multiple_pane_scopes(self) -> None:
        server = self.process(30, 1, ["herdr", "server"], {})
        shell = self.process(11, 30, ["zsh"], {})
        claude = self.process(12, 11, ["claude"], {})
        survivor = self.process(13, 1, ["daemon"], {})
        unrelated = self.process(20, 1, ["unrelated"], {})
        _ = (server / "cgroup").write_text("0::/app.slice/herdr-writing.service\n")
        for process in (shell, claude, survivor):
            _ = (process / "cgroup").write_text("0::/app.slice/app-agents.slice/run-p11.scope\n")
        _ = (unrelated / "cgroup").write_text("0::/app.slice/app-agents.slice/run-p20.scope\n")
        with (patch.object(herdr, "agents_slice", return_value="app-agents.slice") as setting,
              patch.object(herdr, "_pane_shell_pids", return_value=[11])):
            self.assertEqual(herdr._process_sessions([server, shell, claude, survivor, unrelated], {30: "writing"}),  # pyright: ignore[reportPrivateUsage]
                             {11: "writing", 12: "writing", 13: "writing"})
        setting.assert_called_once_with()

    def test_server_start_gives_each_pane_shell_a_scope_through_the_wrapper(self) -> None:
        real = self.root / "bin/zsh"
        real.parent.mkdir()
        _ = real.write_text("#!/bin/sh\n")
        real.chmod(0o755)
        for state in ("", str(self.root / "state")):
            with self.subTest(state=state), patch.dict(os.environ, {"SHELL": str(real), "XDG_STATE_HOME": state}):
                self.log.unlink(missing_ok=True)
                self.configure_herdr(["status", "server"], "status: stopped\n")
                _ = herdr.start_server("fixture", 0)
                wrapper = Path(state or self.root / "home/.local/state") / "herdr/pane-shell"
                creation = [*self.server_creation()[:9], f"--setenv=SHELL={wrapper}",
                            f"--setenv=HERDR_PANE_SHELL={real}", *self.server_creation()[11:]]
                self.assertEqual(self.commands()[-2:], [SCOPE_PROBE, creation])
                self.assertEqual(wrapper.stat().st_mode & 0o777, 0o755)
                self.assertEqual(wrapper.read_text(), "\n".join([
                    "#!/bin/sh",
                    "real=${HERDR_PANE_SHELL:-/bin/sh}",
                    "unset HERDR_PANE_SHELL",
                    "SHELL=$real; export SHELL",
                    f'exec {self.fake} --user --scope --quiet --collect --slice=app-agents.slice -- "$real" "$@"',
                    "",
                ]))
                self.assertEqual([path.name for path in wrapper.parent.iterdir()], ["pane-shell"])
                self.log.unlink()
                _ = subprocess.run([str(wrapper), "-l"], check=True, env={
                    "HERDR_PANE_SHELL": str(real), "FAKE_HERDR_LOG": str(self.log),
                    "FAKE_HERDR_RESPONSES": str(self.responses_path)})
                self.assertEqual(self.commands(), [["--user", "--scope", "--quiet", "--collect",
                                                    "--slice=app-agents.slice", "--", str(real), "-l"]])

    def test_agent_slice_changes_apply_to_the_next_wrapper_and_scope_read(self) -> None:
        config = self.root / "configuration"
        config.mkdir()
        _ = (config / "codex_daemon.conf").write_text("agents_slice=app-custom.slice\n")
        with patch.dict(os.environ, {"CONF_DIR": str(config)}):
            self.configure_herdr(["status", "server"], "status: stopped\n")
            _ = herdr.start_server("fixture", 0)
            wrapper = (self.root / "home/.local/state/herdr/pane-shell").read_text()
            self.assertIn("--slice=app-custom.slice", wrapper)
            scope = Path("/app.slice/app-custom.slice/run-p11.scope")
            self.assertEqual(herdr.pane_scopes([("0", scope)], agents_slice()), [("0", scope)])

    def test_pane_shell_falls_back_to_bin_sh(self) -> None:
        plain = self.root / "not-executable"
        _ = plain.write_text("")
        wrapper = self.root / "home/.local/state/herdr/pane-shell"
        for shell in (None, "zsh", str(plain), str(self.root / "missing"), str(wrapper)):
            with self.subTest(shell=shell):
                with patch.dict(os.environ, {} if shell is None else {"SHELL": shell}):
                    self.log.unlink(missing_ok=True)
                    self.configure_herdr(["status", "server"], "status: stopped\n")
                    _ = herdr.start_server("fixture", 0)
                self.assertEqual(self.commands()[-1], self.server_creation())

    def test_server_start_without_pane_scopes_runs_the_plain_shell_and_says_so_once(self) -> None:
        self.configure(SCOPE_PROBE, code=1, stderr="Failed to connect to bus\nsecond line\n")
        self.configure_herdr(["status", "server"], "status: stopped\n")
        with redirect_stderr(io.StringIO()) as errors:
            _ = herdr.start_server("fixture", 0)
        self.assertEqual(self.commands()[-2:], [SCOPE_PROBE, self.server_creation(scopes=False)])
        self.assertEqual(errors.getvalue().splitlines(), [
            "herdr fixture: pane scopes unavailable, panes stay in the server unit: Failed to connect to bus second line",
            "herdr fixture: server is pending after 0 ms"])
        self.assertFalse((self.root / "home/.local/state/herdr").exists())

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
        _ = self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.NoOpeningPrompt(), 90_000)
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
        _ = self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.NoOpeningPrompt(), 90_000)
        self.assertEqual(process.returncode, 0)
        self.assertFalse(process.killed)
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
                _ = self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.NoOpeningPrompt(), 90_000)
                self.assertFalse(process.killed)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(len(calls), 1)

    def test_start_uses_slow_herdr_observations_when_proc_is_unreadable(self) -> None:
        clock = StartupClock()

        def observation() -> str:
            return self.startup_observation(["claude"] if clock.elapsed >= 0.4 else ["zsh"])

        process, calls = self.startup_watch(clock, 4.3, lambda: None, observation)
        _ = self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.NoOpeningPrompt(), 90_000)
        self.assertFalse(process.killed)
        self.assertLessEqual(len(calls), 1 + int(clock.elapsed * 2))
        self.assertTrue(all(later[0] - earlier[0] >= 0.5 for earlier, later in zip(calls, calls[1:])))


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
            1,
        )
        _ = self.fake.write_text(source)
        self.configure_herdr(["pane", "process-info", "--pane", "pane-1"], code=1, stderr="unreadable")
        _ = self.herdr.start_agent("worker", "pane-1", "claude", [], herdr.OpeningPrompt("continue"), 90_000)
        self.assertTrue(any(command[2:4] == ["pane", "process-info"] for command in self.commands()))
        self.assertEqual(self.commands()[-1], ["--session", "fixture", "agent", "prompt", "pane-1", script_prompt("continue")])

    def test_start_sends_exactly_one_multiline_prompt(self) -> None:
        with (codex_launch_evidence(self.root, ["codex", "-m", "fixture-model"]) as process_info,
              patch.object(herdr.Herdr, "panes", return_value=[
                  {"pane_id": "pane-1", "session_id": "fixture-agent", "workspace_id": "w1"}])):
            self.configure_herdr(["pane", "process-info", "--pane", "pane-1"],
                                 json.dumps({"result": process_info}))
            _ = self.herdr.start_agent("worker", "pane-1", "codex", ["-m", "fixture-model"], herdr.OpeningPrompt("opening\nprompt"), 10_000)
        self.assertEqual(self.start_commands(), [
            ["--session", "fixture", "agent", "start", "worker", "--kind", "codex", "--pane", "pane-1", "--timeout", "10000", "--", "-m", "fixture-model"],
            ["--session", "fixture", "agent", "prompt", "pane-1", script_prompt("opening\nprompt")],
        ])

    def test_claude_start_passes_only_extra_arguments_after_separator(self) -> None:
        _ = self.herdr.start_agent(
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
        self.assertEqual(prompt, ["--session", "fixture", "agent", "prompt", "pane-1", script_prompt("continue")])

    def test_module_start_passes_extra_arguments_and_names_the_agent(self) -> None:
        _ = herdr.start_agent(
            "fixture", "pane-1", kind="claude", agent_arguments=["--resume", "conversation"],
            opening=herdr.OpeningPrompt("continue"), timeout_ms=10_000, agent="worker",
        )
        self.assertEqual(self.start_commands(), [
            ["--session", "fixture", "agent", "start", "worker", "--kind", "claude", "--pane", "pane-1",
             "--timeout", "10000", "--", "--resume", "conversation"],
            ["--session", "fixture", "agent", "prompt", "pane-1", script_prompt("continue")],
        ])

    def test_agent_names_are_valid_for_method_and_module_launches(self) -> None:
        for label, expected in (("Kept Runner", "kept-runner"), ("7runner", "a-7runner"),
                                ("X" * 40, "x" * 23 + "-bd913ff6"), ("", "a-7pane-1")):
            for module_launch in (False, True):
                with (self.subTest(label=label, module_launch=module_launch),
                      patch.object(herdr, "run_program", side_effect=self.run_fake)):
                    self.log.unlink(missing_ok=True)
                    if module_launch:
                        _ = herdr.start_agent("fixture", "7pane-1", "claude", ["--model", "opus"], herdr.OpeningPrompt("continue"), 10_000,
                                              agent=label or "7pane-1")
                    else:
                        _ = self.herdr.start_agent(label or "7pane-1", "7pane-1", "claude", ["--model", "opus"], herdr.OpeningPrompt("continue"), 10_000)
                    start, prompt = self.start_commands()[-2:]
                    self.assertEqual(start, [
                        "--session", "fixture", "agent", "start", expected, "--kind", "claude",
                        "--pane", "7pane-1", "--timeout", "10000", "--", "--model", "opus",
                    ])
                    self.assertEqual(prompt, ["--session", "fixture", "agent", "prompt", "7pane-1", script_prompt("continue")])

    def test_long_worker_names_start_distinct_agents(self) -> None:
        names: list[str] = []
        for label in ("claude-build-followups-verify-review4",
                      "claude-build-followups-verify-review5",
                      "claude-build-followups-verify-review6"):
            outcome = self.herdr.start_agent(label, "pane-1", "claude", [], herdr.NoOpeningPrompt(), 10_000)
            self.assertIsInstance(outcome, herdr.PromptSubmissionNotRequired)
            names.append(self.start_commands()[-1][4])
        self.assertEqual(len(set(names)), 3)
        self.assertTrue(all(len(name) <= 32 for name in names), names)

    def test_worker_server_names_use_the_shared_mapping_and_keep_the_pane_label(self) -> None:
        pane = "w1:p2"
        self.configure_startup_shell(pane)
        prepared = herdr.PreparedAgentEnvironment(self.root, {})
        self.configure_herdr(["pane", "split", "w1:p1", "--direction", "right", "--ratio", "0.3",
            "--cwd", str(self.root), "--no-focus", "--env", "HERDR_AGENT_PANE=1"],
            json.dumps({"result": {"pane": {"pane_id": pane}}}))
        for server_name, expected in (("unit-impl", "unit-impl"), ("second-unit-impl", "second-unit-impl"),
                                     ("claude-build-followups-verify-impl", "claude-build-followups--e05490da")):
            with self.subTest(server_name=server_name):
                self.log.unlink(missing_ok=True)
                launched = herdr.launch_prepared_agent("fixture", herdr.SplitPane("w1:p1", "right", 0.3, "impl"),
                    prepared, "claude", [], herdr.NoOpeningPrompt(), 10_000, agent="impl", agent_name=server_name)
                self.assertIsInstance(launched.outcome, herdr.PromptSubmissionNotRequired)
                self.assertIn(["--session", "fixture", "agent", "start", expected, "--kind", "claude",
                    "--pane", pane, "--timeout", "10000", "--"], self.commands())
                self.assertIn(["--session", "fixture", "pane", "rename", pane, "impl"], self.commands())
                self.assertLessEqual(len(expected), 32)

    def test_name_taken_closes_a_new_split_but_keeps_an_existing_pane(self) -> None:
        pane = "w1:p2"
        self.configure_startup_shell(pane)
        prepared = herdr.PreparedAgentEnvironment(self.root, {})
        for destination in (herdr.SplitPane("w1:p1", "right", 0.3, "worker"),
                            herdr.ExistingPane(pane, "terminal-one")):
            with self.subTest(destination=destination):
                self.log.unlink(missing_ok=True)
                self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
                    {"pane_id": pane, "workspace_id": "w1", "terminal_id": "terminal-one"}]}}))
                self.configure_herdr(["pane", "split", "w1:p1", "--direction", "right", "--ratio", "0.3",
                    "--cwd", str(self.root), "--no-focus", "--env", "HERDR_AGENT_PANE=1"],
                    json.dumps({"result": {"pane": {"pane_id": pane}}}))
                self.configure_herdr(["agent", "start", "worker", "--kind", "claude", "--pane", pane,
                    "--timeout", "10000", "--"], code=8,
                    stderr=json.dumps({"error": {"code": "agent_name_taken", "message": "agent name is taken"}}))
                closed_responses = {**self.responses, "--session fixture pane list": {
                    "stdout": json.dumps({"result": {"panes": []}}), "code": 0}}
                self.responses["--session fixture pane close " + pane] = {
                    "stdout": "", "code": 0,
                    "write_files": {str(self.responses_path): json.dumps(closed_responses)}}
                write_text_atomic(self.responses_path, json.dumps(self.responses))
                with redirect_stderr(io.StringIO()) as errors:
                    launched = herdr.launch_prepared_agent("fixture", destination, prepared, "claude", [],
                        herdr.NoOpeningPrompt(), 10_000, agent="worker")
                self.assertEqual(launched.pane, pane)
                assert isinstance(launched.outcome, herdr.AgentNotStarted)
                self.assertIsInstance(launched.outcome, herdr.PromptNotSubmitted)
                commands = self.commands()
                if isinstance(destination, herdr.SplitPane):
                    self.assertIn(["--session", "fixture", "pane", "rename", pane, "worker"], commands)
                    self.assertEqual(commands.count(["--session", "fixture", "pane", "close", pane]), 1)
                    self.assertEqual(self.herdr.panes(), [])
                    self.assertIn(f"herdr fixture:{pane}: agent start failed: {launched.outcome.reason}; pane closed\n",
                                  errors.getvalue())
                else:
                    self.assertFalse(any(command[2:4] in (["pane", "close"], ["tab", "close"], ["server", "stop"])
                                         for command in commands))
                    self.assertEqual([row["pane_id"] for row in self.herdr.panes()], [pane])
                    self.assertNotIn("pane closed", errors.getvalue())
                self.assertFalse(any(command[2:4] == ["agent", "prompt"] for command in commands))

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
        self.configure_herdr(["agent", "prompt", "pane-1", script_prompt("work")], code=1, stderr=json.dumps({"error": {"code": "agent_blocked", "message": "permission dialog"}}))
        client = self.herdr
        with (self.prompt_target(client, "pane-1", "fixture-submission"),
              self.assertRaises(herdr.AgentBlocked)):
            client.prompt("pane-1", "work", target="fixture-submission")
        self.assertEqual(len(self.commands()), 1)

    def test_untracked_idle_pane_with_an_empty_composer_is_ready_without_a_transcript_turn(self) -> None:
        self.configure_herdr(["agent", "get", "pane-1"], json.dumps({"result": {"agent": {"agent_status": "idle"}}}))
        self.configure_herdr(["pane", "read", "pane-1", "--source", "visible", "--format", "ansi"], "────────\n❯ \n────────")
        transcript = herdr.SessionTranscript(self.root / "home/.claude", "untracked-session")
        path = transcript.config_dir / "projects/project/untracked-session.jsonl"
        path.parent.mkdir(parents=True)
        _ = path.write_text("")
        with redirect_stderr(io.StringIO()) as output:
            state = self.herdr.prompt_input_state("pane-1")
        self.assertIsInstance(state, herdr.PromptInputReady)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(path.read_text(), "")

    def test_untracked_ready_pane_with_text_in_the_composer_holds_a_draft(self) -> None:
        self.configure_herdr(["agent", "get", "pane-1"], json.dumps({"result": {"agent": {"agent_status": "idle"}}}))
        self.configure_herdr(["pane", "read", "pane-1", "--source", "visible", "--format", "ansi"], "────────\n❯ keep this draft\n────────")
        state = self.herdr.prompt_input_state("pane-1")
        self.assertIsInstance(state, herdr.PromptInputHoldsDraft)
        self.assertEqual(cast(herdr.PromptInputHoldsDraft, state).screen, "────────\n❯ keep this draft\n────────")

    def test_untracked_blocked_pane_is_not_ready(self) -> None:
        self.configure_herdr(["agent", "get", "pane-1"], json.dumps({"result": {"agent": {"agent_status": "blocked"}}}))
        self.configure_herdr(["pane", "read", "pane-1", "--source", "visible", "--format", "ansi"], "────────\n❯ \n────────")
        state = self.herdr.prompt_input_state("pane-1")
        self.assertIsInstance(state, herdr.PromptInputNotReady)
        self.assertEqual(cast(herdr.PromptInputNotReady, state).reason, "destination session is still loading")

    def test_explicit_false_readiness_keeps_an_idle_pane_not_ready(self) -> None:
        self.configure_herdr(["agent", "get", "pane-1"], json.dumps({"result": {"agent": {
            "agent_status": "idle", "interactive_ready": False}}}))
        self.configure_herdr(["pane", "read", "pane-1", "--source", "visible", "--format", "ansi"], "────────\n❯ \n────────")
        state = self.herdr.prompt_input_state("pane-1")
        self.assertIsInstance(state, herdr.PromptInputNotReady)
        self.assertEqual(cast(herdr.PromptInputNotReady, state).reason, "destination session is still loading")

    def test_non_boolean_readiness_stays_unknown(self) -> None:
        for value in (None, "true", 1):
            with self.subTest(value=value):
                self.configure_herdr(["agent", "get", "pane-1"], json.dumps({"result": {"agent": {
                    "agent_status": "idle", "interactive_ready": value}}}))
                with redirect_stderr(io.StringIO()) as output:
                    self.assertFalse(self.herdr.readiness("pane-1")["interactive_ready"])
                self.assertEqual(output.getvalue(), "herdr fixture:pane-1: readiness is unknown\n")

    def test_readiness_and_wait_states(self) -> None:
        self.configure_herdr(["agent", "get", "pane-1"], json.dumps({"result": {"agent": {"agent_status": "idle"}}}))
        self.assertEqual(self.herdr.readiness("pane-1"), {"agent_status": "idle", "interactive_ready": True})
        arguments = ["agent", "wait", "pane-1", "--until", "done", "--until", "idle", "--timeout", "20"]
        self.configure_herdr(arguments, json.dumps({"result": {"agent": {"agent_status": "done"}}}))
        self.assertEqual(self.herdr.wait_turn_end("pane-1", 20), herdr.ObservedAgentState(herdr.AgentStatus.DONE))
        self.configure_herdr(arguments, code=1, stderr=json.dumps({"error": {"code": "timeout", "message": "deadline"}}))
        self.assertEqual(self.herdr.wait_turn_end("pane-1", 20), herdr.WaitTimedOut())
        with patch.object(herdr, "run_program", side_effect=subprocess.TimeoutExpired("fake", 1)):
            self.assertEqual(self.herdr.wait_turn_end("pane-1", 20), herdr.WaitTimedOut())
        self.configure_herdr(arguments, json.dumps({"result": {"agent": {"agent_status": "blocked"}}}))
        with self.assertRaises(herdr.AgentBlocked):
            _ = self.herdr.wait_turn_end("pane-1", 20)

    def test_wait_turn_end_requests_done_and_idle(self) -> None:
        arguments = ["agent", "wait", "pane-1", "--until", "done", "--until", "idle", "--timeout", "20"]
        self.configure_herdr(arguments, json.dumps({"result": {"agent": {"agent_status": "idle"}}}))
        self.assertEqual(self.herdr.wait_turn_end("pane-1", 20), herdr.ObservedAgentState(herdr.AgentStatus.IDLE))
        self.assertEqual(self.commands(), [["--session", "fixture", *arguments]])

    def test_pane_reads_share_the_absolute_deadline_and_keep_the_default_timeout(self) -> None:
        reply = subprocess.CompletedProcess(["fixture"], 0, '{"result":{"agents":[]}}', "")
        with (patch.object(time, "monotonic", return_value=100.0),
              patch.object(herdr, "run_program", return_value=reply) as program):
            for deadline, expected in ((100.25, 0.25), (200.0, 10.0)):
                with self.subTest(deadline=deadline):
                    _ = self.herdr.read("pane-1", 40, joined=True, ansi=False, deadline=deadline)
                    self.assertEqual(cast(float, program.call_args.kwargs["timeout"]), expected)
                    self.assertEqual(self.herdr.agents(deadline=deadline), [])
                    self.assertEqual(cast(float, program.call_args.kwargs["timeout"]), expected)
            program.reset_mock()
            with self.assertRaisesRegex(herdr.HerdrFailure, "deadline passed"):
                _ = self.herdr.read("pane-1", 40, joined=True, ansi=False, deadline=99.0)
            program.assert_not_called()
            _ = self.herdr.read("pane-1", 40, joined=False, ansi=False)
            self.assertEqual(cast(float, program.call_args.kwargs["timeout"]), 10.0)
            self.assertEqual(self.herdr.agents(), [])
            self.assertEqual(cast(float, program.call_args.kwargs["timeout"]), 10.0)

    def test_moved_pane_resolves_single_and_batched_process_places_from_retained_shell(self) -> None:
        server = self.process(10, 1, ["herdr", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        _ = self.process(11, 10, ["zsh"], {"HERDR_PANE_ID": "w1:p1"})
        _ = self.process(12, 11, ["claude"], {"HERDR_PANE_ID": "w1:p1", "STAFF_ID": "unit-id"})
        _ = self.process(13, 12, ["worker"], {"HERDR_PANE_ID": "w1:p1"})
        for pane in ("w2:p9", "w1:pA"):
            self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
                {"pane_id": pane, "workspace_id": pane.split(":")[0]}]}}))
            self.configure_herdr(["pane", "process-info", "--pane", pane],
                                 json.dumps({"result": {"process_info": {"shell_pid": 11}}}))
            expected = {pid: herdr.HerdrPlace("fixture", pane) for pid in (11, 12, 13)}
            with self.subTest(pane=pane):
                self.assertEqual(herdr.herdr_place_of(12), expected[12])
                self.log.unlink()
                self.assertEqual(herdr.herdr_places(), expected)
                self.assertEqual(self.commands().count(["--session", "fixture", "pane", "list"]), 1)
                self.assertEqual(self.commands().count(
                    ["--session", "fixture", "pane", "process-info", "--pane", pane]), 1)
                self.assertEqual({row.pid: row.place for row in herdr.herdr_processes()}, expected)
                self.assertEqual(next(row.marks for row in herdr.herdr_processes() if row.pid == 12), {})

    def test_missing_live_pane_never_returns_the_launch_environment_place(self) -> None:
        server = self.process(10, 1, ["herdr", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        _ = self.process(11, 10, ["claude"], {"HERDR_PANE_ID": "w1:p1"})
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": []}}))
        self.assertEqual(herdr.herdr_place_of(11), herdr.NotInHerdr())
        self.assertEqual(herdr.herdr_places(), {})
        self.assertEqual(herdr.herdr_processes(), [])

    def test_process_scan_keeps_launch_places_when_live_panes_are_unreadable(self) -> None:
        server = self.process(10, 1, ["herdr", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        expected: dict[int, herdr.HerdrPlace] = {}
        for pid in (11, 12, 13):
            pane = f"w1:p{pid}"
            _ = self.process(pid, 10, ["claude"], {"HERDR_PANE_ID": pane})
            expected[pid] = herdr.HerdrPlace("fixture", pane)
        for reply, code in (("", 0), ("not JSON", 0), ("", 1)):
            with self.subTest(reply=reply, code=code):
                self.configure_herdr(["pane", "list"], reply, code=code)
                self.log.unlink(missing_ok=True)
                self.assertEqual({row.pid: row.place for row in herdr.herdr_processes()}, expected)
                self.assertEqual(self.commands(), [["--session", "fixture", "pane", "list"]])
        with patch.dict(os.environ, {"HERDR_BIN": "/nonexistent/herdr"}):
            self.assertEqual({row.pid: row.place for row in herdr.herdr_processes()}, expected)

    def test_process_scan_reads_live_panes_once_per_session(self) -> None:
        expected: dict[int, herdr.HerdrPlace] = {}
        for session, server_pid in (("fixture", 10), ("writing", 20)):
            server = self.process(server_pid, 1, ["herdr", "server"], {})
            self.unix_socket(self.root / f"config/herdr/sessions/{session}/herdr.sock", server_pid)
            self.socket_fd(server, server_pid)
            for pid in (server_pid + 1, server_pid + 2):
                pane = f"w1:p{pid}"
                _ = self.process(pid, server_pid, ["claude"], {"HERDR_PANE_ID": pane})
                expected[pid] = herdr.HerdrPlace(session, pane)
        self.log.unlink(missing_ok=True)
        self.assertEqual({row.pid: row.place for row in herdr.herdr_processes()}, expected)
        self.assertCountEqual(self.commands(), [
            ["--session", "fixture", "pane", "list"],
            ["--session", "writing", "pane", "list"],
        ])

    def test_unreadable_live_panes_keep_the_launch_place(self) -> None:
        server = self.process(10, 1, ["herdr", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        _ = self.process(11, 10, ["claude"], {"HERDR_PANE_ID": "w1:p1"})
        place = herdr.HerdrPlace("fixture", "w1:p1")
        readers: tuple[tuple[Callable[[], object], object], ...] = (
            (lambda: herdr.herdr_place_of(11), place),
            (herdr.herdr_places, {11: place}),
            (lambda: [(row.pid, row.place) for row in herdr.herdr_processes()], [(11, place)]))
        for reply in ("not JSON", json.dumps({"result": {"panes": None}})):
            self.configure_herdr(["pane", "list"], reply)
            for read, expected in readers:
                with self.subTest(reply=reply, reader=read):
                    self.assertEqual(read(), expected)
        with patch.dict(os.environ, {"HERDR_BIN": "/nonexistent/herdr"}):
            for read, expected in readers:
                with self.subTest(reader=read):
                    self.assertEqual(read(), expected)

    def test_darwin_unreadable_live_panes_keep_the_launch_place(self) -> None:
        self.darwin()
        uid = os.getuid()
        self.darwin_process_table(f"70 1 {uid} herdr server\n71 70 {uid} claude\n")
        self.darwin_sockets(f"p70\nf3\nn{self.root}/config/herdr/sessions/writing/herdr.sock\n")
        self.darwin_environment(71, "claude", "HERDR_PANE_ID=w1:p1")
        self.configure(["--session", "writing", "pane", "list"], "not JSON")
        place = herdr.HerdrPlace("writing", "w1:p1")
        readers: tuple[tuple[Callable[[], object], object], ...] = (
            (lambda: herdr.herdr_place_of(71), place),
            (herdr.herdr_places, {71: place}),
            (lambda: [(row.pid, row.place) for row in herdr.herdr_processes()], [(71, place)]))
        for read, expected in readers:
            with self.subTest(reader=read):
                self.assertEqual(read(), expected)

    def test_darwin_moved_pane_resolves_processes_from_the_retained_shell(self) -> None:
        self.darwin()
        uid = os.getuid()
        self.darwin_process_table(f"70 1 {uid} herdr server\n71 1 {uid} zsh\n72 71 {uid} claude\n")
        self.darwin_sockets(f"p70\nf3\nn{self.root}/config/herdr/sessions/writing/herdr.sock\n")
        self.configure(["--session", "writing", "pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": "w2:p9", "workspace_id": "w2"}]}}))
        self.configure(["--session", "writing", "pane", "process-info", "--pane", "w2:p9"],
                       json.dumps({"result": {"process_info": {"shell_pid": 71}}}))
        for pid, command in ((71, "zsh"), (72, "claude")):
            self.darwin_environment(pid, command, "HERDR_PANE_ID=w1:p1")
            self.configure(["-a", "-p", str(pid), "-d", "cwd", "-Fn"], f"p{pid}\nfcwd\nn{self.root}\n")
        expected = {pid: herdr.HerdrPlace("writing", "w2:p9") for pid in (71, 72)}
        self.assertEqual(herdr.herdr_places(), expected)
        self.assertEqual(self.commands().count(["--session", "writing", "pane", "list"]), 1)
        self.assertEqual({row.pid: row.place for row in herdr.herdr_processes()}, expected)
        self.assertEqual(herdr.herdr_place_of(72), expected[72])

    def test_linux_user_tmux_is_never_attributed_to_its_outer_herdr_pane(self) -> None:
        server = self.process(10, 1, ["/fixture/herdr", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        _ = self.process(11, 10, ["/bin/tmux"], {"HERDR_PANE_ID": "outer"})
        _ = self.process(12, 11, ["/bin/claude"], {"HERDR_PANE_ID": "outer", "TMUX_PANE": "%1"})
        _ = self.process(13, 10, ["/bin/claude"], {"HERDR_PANE_ID": "native", "TMUX_PANE": ""})
        self.assertEqual(herdr.herdr_place_of(12), herdr.NotInHerdr())
        self.assertEqual(herdr.herdr_place_of(13), herdr.HerdrPlace("fixture", "native"))
        self.assertEqual([row.pid for row in herdr.herdr_processes() if row.command == "claude"], [13])

    def test_linux_deleted_cwd_preserves_the_process_marks_and_place(self) -> None:
        server = self.process(10, 1, ["/fixture/herdr", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        marks = {"CODEX_THREAD_ID": "unit"}
        process = self.process(12, 10, ["/bin/claude"], {**marks, "HERDR_PANE_ID": "pane-1"})
        (process / "cwd").unlink()
        (process / "cwd").symlink_to(self.root / "deleted-directory")
        rows = herdr.herdr_processes()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].pid, rows[0].command, rows[0].place, rows[0].marks),
                         (12, "claude", herdr.HerdrPlace("fixture", "pane-1"), marks))
        self.assertIsInstance(rows[0].working_directory, herdr.UnreadableCwd)

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

    def test_process_snapshot_serves_roster_units_inventory_and_codex_from_one_scan(self) -> None:
        from scripts.message import roster
        from scripts.shutdown import inventory
        from scripts.production.staff_test_support import isolated_staff_file, unit_membership

        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        unit = unit_membership("production", "unit")
        codex = fixture_member("agent", name="codex-pane")

        server = self.process(10, 1, ["/fixture/herdr", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        _ = self.process(12, 10, ["/bin/claude"], {
            "HERDR_PANE_ID": "pane",
            "CLAUDE_CODE_SESSION_ID": unit.current_session,
        })
        _ = self.process(13, 10, ["/bin/codex"], {"HERDR_PANE_ID": "pane-codex", "CODEX_THREAD_ID": codex.current_session})
        from scripts.production import unit_lookup as lookup
        live = lookup.LiveClaude("Unit", unit.id, "/fixture/socket", 12)
        with (patch.object(herdr, "live_sessions", return_value=[{"name": "fixture"}]),
              patch.object(herdr.Herdr, "agents", return_value=[{
                  "agent": "codex", "pane_id": "pane-codex", "agent_status": herdr.AgentStatus.IDLE,
                  "agent_session": herdr.ReportedAgentSession(str(codex.current_session)),
              }]),
              patch.object(lookup, "_claudes", return_value=(
                  {12: live}, lookup.UnreadAt({}, lookup.RecordsRead()))),
              patch.object(roster, "_process_account", return_value=roster.AccountLabel("codex2")),
              patch.object(herdr, "_process_sessions", wraps=PROCESS_SESSIONS) as scans):
            def read_callers() -> tuple[object, object, object]:
                return (roster._every_labeled_session(), inventory._unit_sessions((  # pyright: ignore[reportPrivateUsage]
                    inventory.ShowrunnerRegistration("runner", "production", str(self.root / "production.md")),
                )), roster._herdr_codex_panes("fixture"))  # pyright: ignore[reportPrivateUsage]

            expected = read_callers()
            self.assertEqual(scans.call_count, 3)
            scans.reset_mock()
            with herdr.process_snapshot():
                observed = read_callers()
            self.assertEqual(scans.call_count, 1)
        self.assertEqual(observed, expected)
        self.assertIn(unit.id, cast(dict[str, object], observed[1]))
        self.assertEqual(cast(roster.Roster, observed[2]).entries[0].account, roster.AccountLabel("codex2"))

    def test_process_reads_outside_scope_and_after_scope_exit_are_fresh(self) -> None:
        with patch.object(herdr, "_process_sessions", wraps=PROCESS_SESSIONS) as scans:
            self.assertEqual(herdr.herdr_processes(), [])
            self.assertEqual(herdr.herdr_processes(), [])
            self.assertEqual(scans.call_count, 2)
            with herdr.process_snapshot():
                self.assertEqual(herdr.herdr_processes(), [])
                with herdr.process_snapshot():
                    self.assertEqual(herdr.herdr_processes(), [])
            self.assertEqual(scans.call_count, 3)
            self.assertEqual(herdr.herdr_processes(), [])
            with herdr.process_snapshot():
                self.assertEqual(herdr.herdr_processes(), [])
            self.assertEqual(scans.call_count, 5)

    def test_process_snapshot_isolates_mutable_results_and_next_scope_sees_changes(self) -> None:
        server = self.process(10, 1, ["/fixture/herdr", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        process = self.process(12, 10, ["/bin/claude"], {"HERDR_PANE_ID": "pane", "CODEX_THREAD_ID": "first"})
        with herdr.process_snapshot():
            rows = herdr.herdr_processes()
            expected = herdr.herdr_processes()
            rows[0].marks["CODEX_THREAD_ID"] = "caller change"
            rows.clear()
            _ = process.joinpath("environ").write_bytes(b"HERDR_PANE_ID=pane\0CODEX_THREAD_ID=second\0")
            self.assertEqual(herdr.herdr_processes(), expected)
        with herdr.process_snapshot():
            self.assertEqual(herdr.herdr_processes()[0].marks["CODEX_THREAD_ID"], "second")

    def test_process_snapshot_does_not_change_scan_failure_or_retry(self) -> None:
        with (patch.object(herdr, "_process_sessions", side_effect=[herdr.HerdrFailure("fixture scan failed"), {}]) as scans,
              herdr.process_snapshot()):
            with self.assertRaisesRegex(herdr.HerdrFailure, "fixture scan failed"):
                _ = herdr.herdr_processes()
            self.assertEqual(herdr.herdr_processes(), [])
            self.assertEqual(herdr.herdr_processes(), [])
            self.assertEqual(scans.call_count, 2)
        with patch.object(herdr, "_proc_root", return_value=self.root / "missing") as roots:
            with herdr.process_snapshot():
                self.assertEqual(herdr.herdr_processes(), [])
                self.assertEqual(herdr.herdr_processes(), [])
            self.assertEqual(roots.call_count, 1)

    def test_action_reads_retire_report_snapshots_and_ignore_nested_report_scopes(self) -> None:
        with patch.object(herdr, "_process_sessions", wraps=PROCESS_SESSIONS) as scans:
            with herdr.process_snapshot():
                _ = herdr.herdr_processes()
                _ = herdr.herdr_processes()
                self.assertEqual(scans.call_count, 1)
                with herdr.process_action():
                    with herdr.process_snapshot():
                        _ = herdr.herdr_processes()
                        _ = herdr.herdr_processes()
                self.assertEqual(scans.call_count, 3)
                _ = herdr.herdr_processes()
                self.assertEqual(scans.call_count, 4)

    def test_action_retires_nested_snapshot_in_a_copied_context(self) -> None:
        from contextvars import copy_context

        with patch.object(herdr, "_process_sessions", wraps=PROCESS_SESSIONS) as scans:
            with herdr.process_snapshot():
                _ = herdr.herdr_processes()
                copied = copy_context()
                nested = herdr.process_snapshot()
                copied.run(nested.__enter__)
                try:
                    _ = copied.run(herdr.herdr_processes)
                    self.assertEqual(scans.call_count, 1)
                    with herdr.process_action():
                        _ = herdr.herdr_processes()
                    _ = copied.run(herdr.herdr_processes)
                    self.assertEqual(scans.call_count, 3)
                finally:
                    _ = copied.run(nested.__exit__, None, None, None)

    def test_copied_context_scans_fresh_after_snapshot_scope_closes(self) -> None:
        from contextvars import copy_context

        with patch.object(herdr, "_process_sessions", wraps=PROCESS_SESSIONS) as scans:
            with herdr.process_snapshot():
                self.assertEqual(herdr.herdr_processes(), [])
                copied = copy_context()
                self.assertEqual(copied.run(herdr.herdr_processes), [])
                self.assertEqual(scans.call_count, 1)
            self.assertEqual(copied.run(herdr.herdr_processes), [])
            self.assertEqual(scans.call_count, 2)

    def test_process_snapshot_is_released_on_error_and_not_shared_with_another_thread(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        with patch.object(herdr, "_process_sessions", wraps=PROCESS_SESSIONS) as scans:
            with self.assertRaisesRegex(ValueError, "fixture scope failed"):
                with herdr.process_snapshot():
                    _ = herdr.herdr_processes()
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        self.assertEqual(pool.submit(herdr.herdr_processes).result(), [])
                    _ = herdr.herdr_processes()
                    self.assertEqual(scans.call_count, 2)
                    raise ValueError("fixture scope failed")
            _ = herdr.herdr_processes()
            self.assertEqual(scans.call_count, 3)

    def test_processes_skip_unreadable_and_limit_marks(self) -> None:
        server = self.process(10, 1, ["/fixture/herdr", "--session", "fixture", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/fixture/herdr.sock", 100)
        self.socket_fd(server, 100)
        _ = self.process(12, 10, ["/bin/claude", "--model", "fixture"], {"HERDR_PANE_ID": "pane-1", "CODEX_THREAD_ID": "unit", "SECRET": "never returned"})
        unreadable = self.process(13, 10, ["/bin/sleep"], {"HERDR_PANE_ID": "pane-2"})
        unreadable.joinpath("environ").unlink()
        unreadable.joinpath("environ").mkdir()
        self.assertEqual(herdr.herdr_processes(), [herdr.HerdrProcess(12, herdr.HerdrPlace("fixture", "pane-1"), {"CODEX_THREAD_ID": "unit"}, "claude", str(self.root))])

    def test_ancestry_loop_is_skipped_and_vanished_cwd_keeps_placement(self) -> None:
        _ = self.process(10, 11, ["/bin/sh"], {"HERDR_PANE_ID": "pane-1"})
        _ = self.process(11, 10, ["/bin/sh"], {})
        self.assertEqual(herdr.herdr_place_of(10), herdr.NotInHerdr())
        server = self.process(20, 1, ["/fixture/herdr", "server"], {})
        self.unix_socket(self.root / "config/herdr/herdr.sock", 100)
        self.socket_fd(server, 100)
        process = self.process(21, 20, ["/bin/sleep"], {"HERDR_PANE_ID": "pane-2"})
        process.joinpath("cwd").unlink()
        rows = herdr.herdr_processes()
        self.assertEqual([(row.pid, row.place) for row in rows],
                         [(21, herdr.HerdrPlace("default", "pane-2"))])
        self.assertIsInstance(rows[0].working_directory, herdr.UnreadableCwd)

    def test_trust_ignores_inherited_git_repository_and_configuration(self) -> None:
        requested = self.root / "requested"
        foreign = self.root / "foreign"
        requested.mkdir()
        foreign.mkdir()
        clean_environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        clean_environment.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        for directory in (requested, foreign):
            result = subprocess.run([GIT_BINARY, "init", str(directory)], env=clean_environment,
                                    capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
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
            ["--session", "target", "agent", "prompt", "w2:p1", script_prompt("server")],
            ["--session", "target", "server", "stop"],
            ["--session=target", "server", "reload-config"],
            ["--session", "target", "status", "server"],
        ]
        for offset, arguments in enumerate(commands):
            pid = 20 + offset * 2
            _ = self.process(pid, 10, ["/fixture/herdr", *arguments], {"HERDR_PANE_ID": "pane-owner"})
            _ = self.process(pid + 1, pid, ["/bin/child"], {
                "HERDR_PANE_ID": "pane-owner", "CODEX_THREAD_ID": "unit",
            })
            with self.subTest(arguments=arguments):
                self.assertEqual(herdr.herdr_place_of(pid), herdr.HerdrPlace("owner", "pane-owner"))
                self.assertEqual(herdr.herdr_place_of(pid + 1), herdr.HerdrPlace("owner", "pane-owner"))
        processes = herdr.herdr_processes()
        self.assertEqual(len(processes), len(commands) * 2)
        self.assertTrue(all(process.place.session == "owner" for process in processes))
        self.assertEqual([process.marks for process in processes if process.command == "child"],
                         [{"CODEX_THREAD_ID": "unit"}] * len(commands))

    def test_socket_ownership_decides_place_despite_conflicting_argv(self) -> None:
        server = self.process(10, 1, ["/fixture/herdr", "--handoff", "--session=other", "server"], {})
        self.unix_socket(self.root / "config/herdr/sessions/owner/herdr.sock", 100)
        self.socket_fd(server, 100)
        _ = self.process(11, 10, ["/bin/sleep"], {"HERDR_PANE_ID": "pane-owner"})
        self.assertEqual(herdr.herdr_place_of(11), herdr.HerdrPlace("owner", "pane-owner"))

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

    def test_trust_copy_adds_only_the_source_grant_and_keeps_the_rest_of_the_target(self) -> None:
        folder = self.root / "worktree" / "nested"
        main = self.root / "main"
        self.trust_git(folder, folder.parent, main)
        source, target = self.root / "source.json", self.root / "target.json"
        _ = source.write_text(json.dumps({"projects": {str(main): {"hasTrustDialogAccepted": True},
                                                       str(self.root / "other"): {"hasTrustDialogAccepted": True}}}))
        _ = target.write_text(json.dumps({"theme": "dark", "projects": {str(main): {"history": ["kept"]}}}))
        self.assertEqual(herdr.copy_claude_trust(folder, source, target), herdr.Trusted(folder, target, main))
        self.assertEqual(json.loads(target.read_text()),
                         {"theme": "dark", "projects": {str(main): {"history": ["kept"], "hasTrustDialogAccepted": True}}})
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_trust_copy_creates_a_missing_target_with_private_mode(self) -> None:
        folder = self.root / "worktree"
        main = self.root / "main"
        self.trust_git(folder, folder, main)
        source, target = self.root / "source.json", self.root / "missing-target.json"
        _ = source.write_text(json.dumps({"projects": {
            str(main): {"hasTrustDialogAccepted": True},
            str(self.root / "other"): {"hasTrustDialogAccepted": True},
        }}))
        self.assertEqual(herdr.copy_claude_trust(folder, source, target), herdr.Trusted(folder, target, main))
        self.assertEqual(json.loads(target.read_text()),
                         {"projects": {str(main): {"hasTrustDialogAccepted": True}}})
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_failed_first_trust_publication_leaves_no_target_and_a_rerun_recovers(self) -> None:
        folder = self.root / "worktree"
        main = self.root / "main"
        self.trust_git(folder, folder, main)
        source, target = self.root / "source.json", self.root / "missing-target.json"
        _ = source.write_text(json.dumps({"projects": {str(main): {"hasTrustDialogAccepted": True}}}))
        with patch("scripts.lib.atomic_file.os.replace", side_effect=OSError("disk full")):
            self.assertIsInstance(herdr.copy_claude_trust(folder, source, target), herdr.UnreadableTrustFile)
        self.assertFalse(target.exists())
        self.assertEqual(herdr.copy_claude_trust(folder, source, target), herdr.Trusted(folder, target, main))
        self.assertEqual(json.loads(target.read_text()), {"projects": {str(main): {"hasTrustDialogAccepted": True}}})
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

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

    def test_agent_environment_removes_parent_identity_before_and_after_environment_loading(self) -> None:
        identities = {"STAFF_ID": "parent", "CLAUDE_CODE_SESSION_ID": "parent-Claude", "CODEX_THREAD_ID": "parent-Codex"}
        with patch.dict(os.environ, identities):
            environment = herdr.agent_base_environment({**identities, "KEPT_SETTING": "kept"}, frozenset())
        for key in identities:
            self.assertNotIn(key, environment)
        self.assertEqual(environment["KEPT_SETTING"], "kept")
        with patch.object(herdr.nix_env, "ensure", return_value=herdr.nix_env.NoEnvrc({**environment, **identities})):
            prepared = herdr.prepare_agent_environment(str(self.root), identities)
        assert isinstance(prepared, herdr.PreparedAgentEnvironment)
        for key in identities:
            self.assertNotIn(key, prepared.env)
        self.assertEqual(prepared.env["KEPT_SETTING"], "kept")


    def test_a_pane_of_the_heal_started_server_can_launch_another_session(self) -> None:
        self.configure_startup_shell("pane")
        spawn, _ = self.detached_server()
        self.darwin_startup_table(f"10 1 {os.getuid()} zsh")
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0}]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        envrc = self.root / ".envrc"
        _ = envrc.write_text("use flake\n")
        handoff = {herdr.nix_env.HEAL_SESSION_ENV: "healed-session",
                   herdr.nix_env.HEAL_DIRECTORY_ENV: str(self.root),
                   "IN_NIX_SHELL": "impure", "NIX_LDFLAGS": "-Lprepared"}
        with (patch.dict(os.environ, handoff),
              patch.object(herdr.nix_env, "ensure", side_effect=AssertionError("the heal proof must avoid another load")) as ensure,
              patch.object(herdr, "open_tab", return_value="pane") as opened,
              patch.object(herdr, "start_agent", return_value=herdr.PromptSubmissionNotRequired()) as started):
            _ = herdr.start_server("fixture", 90_000)
            restored = herdr.launch_agent("fixture", "space", "healed", str(self.root), {}, "claude",
                                          ["--resume", "healed-session"], herdr.NoOpeningPrompt(), 90_000, agent="healed")
            self.assertIsInstance(restored, herdr.AgentLaunched)
            ensure.assert_not_called()
            # A pane inherits the server environment, with its launch overlay on top.
            server_environment = cast(dict[str, str], spawn.call_args.kwargs["env"])
            pane_overlay = cast(dict[str, str], opened.call_args.args[4])
            pane_environment = {**server_environment, **pane_overlay}
            with (patch.dict(os.environ, pane_environment, clear=True),
                  patch.object(herdr.nix_env, "ensure", return_value=herdr.nix_env.Loaded(pane_environment, envrc)) as load):
                result = herdr.launch_agent("fixture", "space", "other", str(self.root), {}, "claude",
                                           ["--resume", "another-session"], herdr.NoOpeningPrompt(), 90_000, agent="other")
                self.assertIsInstance(result, herdr.AgentLaunched)
                load.assert_called_once()
                self.assertEqual(cast(Path, load.call_args.args[0]), self.root)
            self.assertEqual(started.call_count, 2)
            second_overlay = cast(dict[str, str], opened.call_args.args[4])
            self.assertNotIn(herdr.nix_env.HEAL_SESSION_ENV, second_overlay)
            self.assertNotIn(herdr.nix_env.HEAL_DIRECTORY_ENV, second_overlay)
            self.assertEqual(second_overlay["NIX_LDFLAGS"], "-Lprepared")


    def test_darwin_exited_child_fails_immediately_with_plain_message_and_existing_log(self) -> None:
        _, poll = self.detached_server()
        poll.return_value = 1
        self.configure_herdr(["status", "server"], "status: stopped\n")
        log = self.root / "config/herdr/sessions/fixture/herdr-server.log"
        log.parent.mkdir(parents=True)
        _ = log.write_text("server could not bind its socket\n")
        with patch.object(time, "sleep") as pause:
            with self.assertRaises(herdr.HerdrFailure) as raised:
                _ = herdr.start_server("fixture", 90_000)
            self.assertEqual(str(raised.exception), f"herdr server for fixture did not start; see {log}")
            pause.assert_not_called()
        self.assertEqual(self.commands(), [["--session", "fixture", "status", "server"]])


    def test_darwin_unreadable_startup_listener_fails_plainly_without_clearing(self) -> None:
        _ = self.detached_server()
        self.responses["--session fixture status server"] = [
            {"stdout": "status: stopped\n", "code": 0}, {"stdout": "status: running\n", "code": 0}]
        self.configure(["-n", "-P", "-a", "-U", "-u", str(os.getuid()), "-F", "pfn"],
                       code=1, stderr="inspection denied")
        with self.assertRaisesRegex(herdr.HerdrFailure, "herdr server for fixture did not start"):
            _ = herdr.start_server("fixture", 90_000)
        self.assertFalse(any(command[2:3] == ["workspace"] for command in self.commands()))


    def test_darwin_spawn_failure_has_plain_start_message(self) -> None:
        spawn, _ = self.detached_server()
        spawn.side_effect = OSError("cannot execute")
        self.configure_herdr(["status", "server"], "status: stopped\n")
        with self.assertRaises(herdr.HerdrFailure) as raised:
            _ = herdr.start_server("fixture", 90_000)
        self.assertEqual(str(raised.exception), "herdr server for fixture did not start")


    def test_darwin_stop_runs_only_named_server_stop_and_reports_refusal(self) -> None:
        self.darwin()
        herdr.stop_session("foo.service")
        self.configure(["--session", "foo.service", "server", "stop"], code=1, stderr="stop refused")
        with self.assertRaisesRegex(herdr.HerdrFailure, "stop refused"):
            herdr.stop_session("foo.service")
        self.assertEqual(self.commands(), [["--session", "foo.service", "server", "stop"]] * 2)


    def test_darwin_identity_distinguishes_no_terminal_and_refuses_missing_or_malformed_process(self) -> None:
        self.darwin()
        for terminal in ("??", "?", "-"):
            self.configure(["-o", "lstart=,tty=", "-p", "42"], f"Thu Oct 15 12:00:00 2026 {terminal}\n")
            self.assertEqual(herdr.darwin_process_identity(42).terminal, 0)
        for output in ("", "Thu Oct 15 12:00:00 2026", "malformed ttys001",
                       "Thu Oct 15 12:00:00 2026 ttys001\nThu Oct 15 12:00:00 2026 ??"):
            self.configure(["-o", "lstart=,tty=", "-p", "42"], output)
            with self.subTest(output=output), self.assertRaisesRegex(herdr.HerdrFailure, "process 42"):
                _ = herdr.darwin_process_identity(42)
        self.configure(["-o", "lstart=,tty=", "-p", "42"], code=1, stderr="process vanished")
        with self.assertRaisesRegex(herdr.HerdrFailure, "process vanished"):
            _ = herdr.darwin_process_identity(42)


    def test_mac_codex_rollout_proof_requires_one_unchanged_open_rollout(self) -> None:
        self.darwin()
        identity = fixture_runtime_id("Mac-current-rollout")
        other_identity = fixture_runtime_id("Mac-other-rollout")
        paths: list[Path] = []
        for native_id in (identity, other_identity):
            path = self.root / "account with spaces" / f"rollout-{native_id}.jsonl"
            path.parent.mkdir(exist_ok=True)
            _ = path.write_text(json.dumps({"type": "session_meta", "payload": {"id": native_id}}) + "\n")
            paths.append(path)
        arguments = ["-a", "-p", "42", "-Fn"]
        first = f"p42\nf7\nn{paths[0]}\n"
        second = f"p42\nf7\nn{paths[1]}\n"
        self.configure(["-o", "lstart=,tty=", "-p", "42"], "Mon Nov  2 10:00:00 2026 ttys001\n")
        with patch.object(sessions, "process_start", return_value="Mac-stable-start"):
            self.configure(arguments, first)
            self.assertEqual(herdr.codex_rollout_thread(42), identity)
            for evidence in ("missing", "several", "changed"):
                with self.subTest(evidence=evidence):
                    _ = self.log.write_text("")
                    self.configure(arguments, "" if evidence == "missing" else first + second)
                    if evidence == "changed":
                        self.responses[" ".join(arguments)] = [
                            {"stdout": first, "code": 0}, {"stdout": second, "code": 0}]
                        _ = self.responses_path.write_text(json.dumps(self.responses))
                    with self.assertRaises((ValueError, herdr.HerdrFailure)):
                        _ = herdr.codex_rollout_thread(42)


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


    def test_darwin_unreadable_command_cannot_be_used_to_read_an_environment(self) -> None:
        self.darwin()
        self.darwin_environment(42, "(herdr)", "HERDR_SESSION=writing")
        with self.assertRaisesRegex(herdr.HerdrFailure, "arguments are unreadable"):
            _ = herdr.darwin_process_environment(42)
        self.assertEqual(self.commands(), [["-o", "command=", "-p", "42"]])


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


    def test_darwin_unreadable_socket_listing_refuses_server_environment(self) -> None:
        self.darwin()
        self.configure(["-n", "-P", "-a", "-U", "-u", str(os.getuid()), "-F", "pfn"],
                       code=1, stderr="socket inspection denied")
        with self.assertRaisesRegex(herdr.HerdrFailure, "session fixture.*socket inspection denied"):
            _ = herdr.server_environment("fixture")
        self.assertEqual(herdr.herdr_processes(), [])


    def test_panes_rejects_invalid_workspace_ids(self) -> None:
        client = self.herdr
        workspaces: tuple[object, ...] = (None, 3, True, [], {})
        for workspace in workspaces:
            with self.subTest(workspace=workspace), patch.object(client, "result", return_value={"panes": [
                    {"pane_id": "w1:p1", "label": "alpha", "workspace_id": workspace}]}):
                with self.assertRaisesRegex(herdr.HerdrFailure, "herdr pane workspace id: expected text"):
                    _ = client.panes()


    def test_tab_sessions_rejects_invalid_workspace_labels(self) -> None:
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": []}}))
        self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [
            {"workspace_id": "w1", "label": 3}]}}))
        with self.assertRaisesRegex(herdr.HerdrFailure, "herdr workspace label: expected text"):
            _ = self.herdr.tab_sessions()


    def test_stop_checks_unit_after_server_refusal(self) -> None:
        self.configure_herdr(["server", "stop"], code=1, stderr="server failed")
        with self.assertRaises(herdr.HerdrFailure):
            herdr.stop_session("fixture")
        self.assertEqual(self.commands()[-1], ["--user", "stop", "herdr-fixture.service"])


    def test_focus_tab_of_shows_the_workspace_then_the_tab_and_refuses_a_missing_pane(self) -> None:
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1"},
            {"pane_id": "w2:p3", "workspace_id": "w2", "tab_id": "w2:t2"}]}}))
        self.herdr.focus_tab_of("w2:p3")
        self.assertEqual(self.commands(), [["--session", "fixture", "pane", "list"],
                                           ["--session", "fixture", "workspace", "focus", "w2"],
                                           ["--session", "fixture", "tab", "focus", "w2:t2"]])
        self.log.unlink()
        with self.assertRaisesRegex(herdr.HerdrFailure, "herdr pane w9:p9 is not open in session fixture"):
            self.herdr.focus_tab_of("w9:p9")
        self.assertEqual(self.commands(), [["--session", "fixture", "pane", "list"]])


    def test_shared_session_is_never_stopped_and_close_agent_closes_only_the_tab(self) -> None:
        self.share()
        self.configure_startup_shell("w1:p2")
        with self.assertRaisesRegex(herdr.HerdrFailure, "close one tab instead"):
            herdr.stop_session("fixture")
        self.responses["--session fixture pane list"] = [
            {"stdout": json.dumps({"result": {"panes": [
                {"pane_id": "w1:p1", "tab_id": "w1:t1"},
                {"pane_id": "w1:p2", "tab_id": "w1:t2"}]}}), "code": 0},
            {"stdout": json.dumps({"result": {"panes": [
                {"pane_id": "w1:p1", "tab_id": "w1:t1"}]}}), "code": 0},
        ]
        _ = self.responses_path.write_text(json.dumps(self.responses))
        herdr.close_agent("fixture", "w1:p2")
        self.assertEqual(self.commands(), [
            ["--session", "fixture", "pane", "list"],
            ["--session", "fixture", "pane", "process-info", "--pane", "w1:p2"],
            ["--session", "fixture", "tab", "close", "w1:t2"],
            ["--session", "fixture", "pane", "list"],
        ])
        self.assertNotIn(["--session", "fixture", "server", "stop"], self.commands())


    def test_close_agent_refuses_when_the_closed_pane_is_still_listed(self) -> None:
        self.share()
        self.configure_startup_shell("w1:p2")
        self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": [
            {"pane_id": "w1:p1", "tab_id": "w1:t1"}, {"pane_id": "w1:p2", "tab_id": "w1:t2"}]}}))
        with self.assertRaisesRegex(herdr.HerdrFailure, "tab closure is not confirmed"):
            herdr.close_agent("fixture", "w1:p2")
        self.assertEqual(self.commands(), [
            ["--session", "fixture", "pane", "list"],
            ["--session", "fixture", "pane", "process-info", "--pane", "w1:p2"],
            ["--session", "fixture", "tab", "close", "w1:t2"],
            ["--session", "fixture", "pane", "list"],
        ])
        self.assertNotIn(["--session", "fixture", "server", "stop"], self.commands())


    def test_startup_cleanup_preserves_workspaces_with_activity_or_incomplete_evidence(self) -> None:
        _ = self.enterContext(patch.object(herdr, "run_program", side_effect=self.run_fake))
        workspace, pane = startup_shell("w1")
        scroll = herdr.json_object(pane["scroll"], "captured startup scroll")
        changed: list[tuple[str, dict[str, object], list[dict[str, object]]]] = [
            ("scrollback", {**workspace}, [{**pane, "scroll": {**scroll, "max_offset_from_bottom": 1}}]),
            ("typed input", {**workspace}, [{**pane, "revision": 1}]),
            ("second pane", {**workspace}, [{**pane}, {**pane, "pane_id": "w1:p2"}]),
            ("missing pane", {**workspace}, []),
            ("pane belongs elsewhere", {**workspace}, [{**pane, "pane_id": "w3:p1"}]),
            ("second tab", {**workspace, "tab_count": 2}, [{**pane}]),
            ("agent", {**workspace}, [{**pane, "agent": "claude"}]),
            ("agent status", {**workspace, "agent_status": "running"}, [{**pane}]),
            ("pane agent status", {**workspace}, [{**pane, "agent_status": "running"}]),
            ("cwd", {**workspace}, [{**pane, "cwd": str(self.root / "work")}]),
            ("foreground cwd", {**workspace}, [{**pane, "foreground_cwd": str(self.root / "work")}]),
            ("renamed", {**workspace, "label": "my shell"}, [{**pane}]),
            ("non-object scroll", {**workspace}, [{**pane, "scroll": []}]),
            ("null scroll", {**workspace}, [{**pane, "scroll": None}]),
            ("missing scroll offset", {**workspace}, [{**pane, "scroll": {
                key: value for key, value in scroll.items() if key != "max_offset_from_bottom"}}]),
            ("text scroll offset", {**workspace}, [{**pane, "scroll": {**scroll, "max_offset_from_bottom": "0"}}]),
            ("boolean revision", {**workspace}, [{**pane, "revision": False}]),
            ("boolean scrollback", {**workspace}, [{**pane, "scroll": {**scroll, "max_offset_from_bottom": False}}]),
            ("boolean tab count", {**workspace, "tab_count": True}, [{**pane}]),
            ("boolean pane count", {**workspace, "pane_count": True}, [{**pane}]),
        ]
        changed.extend((f"missing workspace {field}", {key: value for key, value in workspace.items() if key != field},
                        [{**pane}]) for field in ("label", "tab_count", "pane_count", "agent_status"))
        changed.extend((f"missing pane {field}", {**workspace},
                        [{key: value for key, value in pane.items() if key != field}])
                       for field in ("agent_status", "cwd", "foreground_cwd", "revision", "scroll"))
        for reason, candidate, members in changed:
            with self.subTest(reason=reason):
                self.log.unlink(missing_ok=True)
                self.configure_herdr(["workspace", "list"], json.dumps({"result": {"workspaces": [candidate]}}))
                self.configure_herdr(["pane", "list"], json.dumps({"result": {"panes": members}}))
                self.herdr.close_startup_workspaces("w2")
                self.assertEqual(self.commands(), [
                    ["--session", "fixture", "workspace", "list"],
                    ["--session", "fixture", "workspace", "list"],
                    ["--session", "fixture", "pane", "list"]])


    def test_generated_name_fits_suffix_and_launches_under_the_contract(self) -> None:
        for text, suffix in (("space é" * 20, "123"), (".", ""), ("..", ""), ("", "")):
            name = herdr.generated_session_name(text, suffix)
            self.configure(["--session", name, "status", "server"], "status: running")
            _ = herdr.start_server(name, 10_000)
            self.assertLessEqual(len(name.encode("ascii")), 64)
            if suffix:
                self.assertTrue(name.endswith("-123"))
            self.assertEqual(self.commands()[-1], ["--session", name, "status", "server"])


    def test_start_bounds_slow_darwin_process_reads_by_the_launch_deadline(self) -> None:
        self.darwin_startup_pane(["zsh"])
        self.darwin_startup_table(f"10 1 {os.getuid()} /bin/zsh\n11 10 {os.getuid()} /usr/bin/vim\n")
        clock = StartupClock()
        process = WaitingAgentCLI(clock, 90, lambda: None)
        reads: list[tuple[float, float]] = []

        def read(argv: list[str], timeout: float = 10.0, *,
                 environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            if argv[1:3] != ["--session", "fixture"]:
                reads.append((clock.elapsed, timeout))
                clock.elapsed += min(0.12, timeout)
                if timeout <= 0.12:
                    raise subprocess.TimeoutExpired(argv, timeout)
            return self.run_fake(argv, timeout, environment=environment)

        with (patch.object(herdr, "time", SimpleNamespace(monotonic=lambda: clock.elapsed)),
              patch.object(herdr, "run_program", read),
              patch.object(subprocess, "Popen", return_value=process),
              self.assertRaises(subprocess.TimeoutExpired)):
            _ = self.herdr.start_command(["fake-herdr", "agent", "start"], "claude", "pane-1", 0.4)
        self.assertGreaterEqual(len(reads), 3)
        self.assertTrue(all(0 < timeout <= 0.4 - called_at for called_at, timeout in reads), reads)
        self.assertAlmostEqual(clock.elapsed, 0.4)
        self.assertTrue(process.killed)


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
            _ = self.herdr.start_agent("worker", "pane-1", "claude", ["--resume", "conversation"], herdr.OpeningPrompt("continue\nworking"), 10_000)
        self.assertEqual(self.start_commands(), [arguments, arguments, arguments,
                                          ["--session", "fixture", "agent", "prompt", "pane-1", script_prompt("continue\nworking")]])
        self.assertEqual([call.args for call in pause.call_args_list], [(0.1,), (0.1,)])


    def test_read_sources_formats_keys_and_close(self) -> None:
        client = self.herdr
        with (patch.object(client, "panes", return_value=[{
                  "pane_id": "pane-1", "workspace_id": "w1", "session_id": None, "terminal_id": "terminal-1"}]),
              patch.object(client, "result", side_effect=[{"process_info": {
                  "shell_pid": 998042, "foreground_process_group_id": 998043,
                  "foreground_processes": [{"pid": 998043, "name": "claude", "argv": ["claude"]}]}}, {"process_info": {
                  "shell_pid": 998042, "foreground_process_group_id": 998042,
                  "foreground_processes": [{"pid": 998042, "name": "bash", "argv": ["bash"]}]}}]),
              patch.object(herdr, "native_process", return_value=herdr.NativeAgentProcess(herdr.RolloutThread("keys-session"), "fixture-start", {})),
              patch.object(client, "composer", return_value=herdr.ComposerInput("")),
              patch.object(herdr, "_startup_processes", return_value=herdr.PaneAtShell())):
            _ = client.read("pane-1", 40, joined=False, ansi=False)
            _ = client.read("pane-1", 80, joined=True, ansi=True)
            _ = client.send_keys("pane-1", "ctrl-c", target="keys-session")
            _ = client.close_pane("pane-1", target="")
        self.assertEqual(self.commands(), [
            ["--session", "fixture", "pane", "read", "pane-1", "--source", "recent", "--lines", "40"],
            ["--session", "fixture", "pane", "read", "pane-1", "--source", "recent-unwrapped", "--lines", "80", "--format", "ansi"],
            ["--session", "fixture", "pane", "send-keys", "pane-1", "ctrl-c"],
            ["--session", "fixture", "pane", "close", "pane-1"],
        ])


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


    def test_plain_calls_and_session_listing_translate_timeouts(self) -> None:
        with patch.object(herdr, "run_program", side_effect=subprocess.TimeoutExpired("fake", 10)):
            with self.assertRaisesRegex(herdr.HerdrFailure, "herdr pane list: no answer after 10 s"):
                _ = self.herdr.run("pane", "list")
            with self.assertRaisesRegex(herdr.HerdrFailure, "herdr pane list: no answer after 10 s"):
                _ = self.herdr.result("pane", "list")
            with self.assertRaisesRegex(herdr.HerdrFailure, "herdr session list: no answer after 10 s"):
                _ = herdr.live_sessions()


    def test_trust_copy_never_adds_trust_the_source_lacks(self) -> None:
        folder = self.root / "worktree"
        main = self.root / "main"
        self.trust_git(folder, folder, main)
        source, target = self.root / "source.json", self.root / "target.json"
        _ = source.write_text(json.dumps({"projects": {}}))
        _ = target.write_text(json.dumps({"projects": {}}))
        self.assertIsInstance(herdr.copy_claude_trust(folder, source, target), herdr.Untrusted)
        self.assertEqual(json.loads(target.read_text()), {"projects": {}})


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


def layout_row_ids(names: list[str]) -> list[str]:
    rows = staff.initial_snapshot().rows
    return [next(row.id for row in rows if row.name == name) for name in names]


class LayoutHerdr(herdr.Herdr):
    """An in-memory herdr session: workspaces and tabs in order, one pane per tab."""

    def __init__(self, session: str = "fixture") -> None:
        with patch.dict(os.environ, {"HERDR_BIN": str(Path(herdr.__file__).with_name("fake_herdr.py"))}):
            super().__init__(session)
        self.spaces: list[tuple[str, str]] = []
        self.tabs: list[dict[str, str]] = []
        self.count: int = 0
        self.commands: list[tuple[str, ...]] = []
        self.focused_pane: str = ""
        self.focused_workspace: str = ""

    @override
    def socket_request(self, method: str, **params: object) -> dict[str, object]:
        self.commands.append((method, *(str(value) for value in params.values())))
        if method != "tab.move":
            raise AssertionError(f"unexpected socket request {method}: {params}")
        tab_id, index = str(params["tab_id"]), params["insert_index"]
        assert isinstance(index, int)
        workspace = next(pane["workspace_id"] for pane in self.tabs if pane["tab_id"] == tab_id)
        siblings = list(dict.fromkeys(pane["tab_id"] for pane in self.tabs if pane["workspace_id"] == workspace))
        siblings.insert(index, siblings.pop(siblings.index(tab_id)))
        ordered = iter(pane for tab in siblings for pane in self.tabs if pane["tab_id"] == tab)
        self.tabs = [next(ordered) if pane["workspace_id"] == workspace else pane for pane in self.tabs]
        return {}

    @override
    def _close_placeholders(self, workspace: str) -> bool:
        with patch.object(herdr, "Herdr", return_value=self):
            return super()._close_placeholders(workspace)

    @override
    def startup_processes(self, pane: str, kind: str, timeout: float) -> herdr.PaneStartupObservation:
        return herdr.PaneStartupObservation(herdr.PaneShell(42), herdr.PaneAtShell())

    def require_time(self, deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise herdr.HerdrFailure("fixture command deadline exhausted")

    def new_id(self, kind: str) -> str:
        self.count += 1
        return f"{kind}{self.count}"

    def assign_session(self, tab: dict[str, str], name: str) -> None:
        rows = [row for row in staff.initial_snapshot().rows if row.name == name]
        row = rows[0] if rows else fixture_member("showrunner", name=name)
        if row.current_session == row.id:
            row = staff.update_current_session(row.current_session, staff.native_id(fixture_runtime_id("layout-runtime:" + name)))
        tab["session_id"] = row.current_session

    def add_tab(self, workspace: str, label: str) -> dict[str, object]:
        tab = {"tab_id": self.new_id("t"), "pane_id": self.new_id("p"), "workspace_id": workspace, "label": label}
        tab["terminal_id"] = "terminal-" + tab["pane_id"]
        if label != herdr.LAYOUT_PLACEHOLDER:
            self.assign_session(tab, label)
        self.tabs.append(tab)
        return {"tab": {"tab_id": tab["tab_id"]}, "root_pane": {"pane_id": tab["pane_id"]}}

    def layout(self) -> list[tuple[str, list[str]]]:
        return [(label, [tab["label"] for tab in self.tabs if tab["workspace_id"] == ident]) for ident, label in self.spaces]

    @override
    def result(self, *arguments: str, deadline: float = math.inf) -> dict[str, object]:
        self.require_time(deadline)
        match arguments[:2]:
            case ["agent", "list"]:
                return {"agents": []}
            case ["pane", "process-info"]:
                pane = next(tab for tab in self.tabs if tab["pane_id"] == arguments[-1])
                if "session_id" in pane and "HERDR_PROC" in os.environ:
                    pid = 992000 + self.tabs.index(pane)
                    fake_unit_host.process(os.environ, pid, 1, ["claude"], {})
                    registry = sessions.sessions_dir() / f"{pid}.json"
                    registry.parent.mkdir(parents=True, exist_ok=True)
                    _ = registry.write_text(json.dumps({"pid": pid, "procStart": "1", "name": pane["label"],
                        "sessionId": pane["session_id"], "updatedAt": 1, "messagingSocketPath": ""}))
                    return {"process_info": {"shell_pid": 42, "foreground_process_group_id": pid,
                        "foreground_processes": [{"pid": pid, "name": "claude", "argv": ["claude"]}]}}
                activity = self.pane_activity(arguments[-1])
                command = activity.commands[0] if isinstance(activity, herdr.PaneRunning) else "zsh"
                foreground = 43 if isinstance(activity, herdr.PaneRunning) else 42
                return {"process_info": {"shell_pid": 42, "foreground_process_group_id": foreground,
                    "foreground_processes": [{"pid": foreground, "name": command, "argv": [command]}]}}
            case ["workspace", "list"]:
                focused = next((pane for pane in self.tabs if pane["pane_id"] == self.focused_pane), dict[str, str]())
                return {"workspaces": [{"workspace_id": ident, "label": label,
                    "focused": ident == (self.focused_workspace or focused.get("workspace_id")), "active_tab_id": focused.get("tab_id", "")}
                    for ident, label in self.spaces]}
            case ["workspace", "create"]:
                ident = self.new_id("w")
                self.spaces.append((ident, arguments[arguments.index("--label") + 1]))
                result = self.add_tab(ident, herdr.LAYOUT_PLACEHOLDER)
                return {**result, "workspace": {"workspace_id": ident}}
            case ["tab", "create"]:
                return self.add_tab(arguments[arguments.index("--workspace") + 1], arguments[arguments.index("--label") + 1])
            case ["tab", "list"]:
                shown = [tab for tab in self.tabs if "--workspace" not in arguments or tab["workspace_id"] == arguments[-1]]
                return {"tabs": [{**group[0], "pane_count": len(group)}
                    for tab_id in dict.fromkeys(p["tab_id"] for p in shown)
                    if (group := [p for p in shown if p["tab_id"] == tab_id])]}
            case ["pane", "list"]:
                return {"panes": [{**tab, "label": tab.get("pane_label", tab["label"]),
                    "focused": tab["pane_id"] == self.focused_pane,
                    "agent_session": {"value": tab.get("mark", tab.get("session_id", ""))}} for tab in self.tabs]}
            case ["pane", "move"]:
                self.commands.append(arguments)
                tab = next(tab for tab in self.tabs if tab["pane_id"] == arguments[2])
                self.tabs.remove(tab)
                moved = {**tab, "tab_id": self.new_id("t"), "workspace_id": arguments[arguments.index("--workspace") + 1]}
                if moved["workspace_id"] != tab["workspace_id"]:
                    moved["pane_id"] = self.new_id("p")
                self.tabs.append(moved)
                if "--no-focus" not in arguments:
                    self.focused_pane = moved["pane_id"]
                return {"move_result": {"changed": True, "created_tab": {"tab_id": moved["tab_id"]}, "pane": {"pane_id": moved["pane_id"]}}}
            case _:
                raise AssertionError(f"unexpected herdr call {arguments}")

    @override
    def pane_activity(self, pane: str) -> herdr.PaneAtShell | herdr.PaneRunning:
        return herdr.PaneAtShell()

    @override
    def run(self, *arguments: str, deadline: float = math.inf) -> str:
        self.require_time(deadline)
        self.commands.append(arguments)
        kind, action, ident, *values = arguments
        if action == "rename":
            for pane in self.tabs:
                if pane[kind + "_id"] == ident:
                    if kind == "tab" and "session_id" not in pane and values[0] != herdr.LAYOUT_PLACEHOLDER:
                        self.assign_session(pane, values[0])
                    pane["pane_label" if kind == "pane" else "label"] = values[0]
        elif action == "close":
            self.tabs = [pane for pane in self.tabs if pane[kind + "_id"] != ident]
            if kind == "workspace":
                self.spaces = [space for space in self.spaces if space[0] != ident]
        elif action == "focus":
            if kind == "pane":
                self.focused_pane = ident
            elif kind == "workspace":
                self.focused_workspace = ident
        else:
            raise AssertionError(f"unexpected herdr call {arguments}")
        return "{}"


TitledLayoutHerdr = LayoutHerdr


def split_tree(direction: str, ratio: float, first: object, second: object) -> dict[str, object]:
    def node(value: object) -> object:
        return {"type": "pane", "pane_id": value} if isinstance(value, str) else value
    return {"type": "split", "direction": direction, "ratio": ratio, "first": node(first), "second": node(second)}


class RowTabsHerdr(LayoutHerdr):
    """Model native sessions, split trees, and new public IDs at the transport boundary."""

    def __init__(self) -> None:
        super().__init__()
        self.unknown: set[str] = set()
        self.missing_argv: set[str] = set()
        self.zoomed: set[str] = set()
        self.focused_pane: str = "p1"
        self.trees: dict[str, dict[str, object]] = {"t0": split_tree("right", 0.3, "p0", "p1")}
        self.count: int = 100
        self.scans: list[herdr.RowPaneScan] = []

    @override
    def scan_row_panes(self, record: staff.StaffSnapshot,
                       panes: list[dict[str, object]]) -> herdr.RowPaneScan:
        scan = super().scan_row_panes(record, panes)
        self.scans.append(scan)
        return scan

    @override
    def run(self, *arguments: str, deadline: float = math.inf) -> str:
        if arguments[:2] == ("pane", "focus"):
            raise herdr.HerdrFailure("usage: herdr pane focus --direction left|right|up|down [--pane ID|--current]")
        result = super().run(*arguments, deadline=deadline)
        if arguments[:2] == ("tab", "focus"):
            members = [pane for pane in self.tabs if pane["tab_id"] == arguments[2]]
            if not any(pane["pane_id"] == self.focused_pane for pane in members):
                self.focused_pane = members[0]["pane_id"]
        return result

    @override
    def socket_request(self, method: str, **params: object) -> dict[str, object]:
        if method == "layout.export":
            self.commands.append((method, *(str(value) for value in params.values())))
            tab = str(params["tab_id"])
            members = [pane for pane in self.tabs if pane["tab_id"] == tab]
            leaf: dict[str, object] = {"type": "pane", "pane_id": members[0]["pane_id"]}
            return {"layout": {"zoomed": tab in self.zoomed,
                "root": leaf if len(members) == 1 else self.trees[tab]}}
        return super().socket_request(method, **params)

    def native_process(self, pid: int, kind: str) -> herdr.NativeAgentProcess:
        if kind != "claude":
            raise ValueError("private native session is unavailable")
        return herdr.NativeAgentProcess(herdr.RolloutThread(self.tabs[pid - 991000]["session_id"]), "private-generation", {})

    def replace_leaf(self, tree: dict[str, object], pane: str, replacement: dict[str, object]) -> dict[str, object]:
        if tree.get("type") == "pane":
            return replacement if tree.get("pane_id") == pane else tree
        return {**tree, **{side: self.replace_leaf(herdr.json_object(tree[side], "fixture tree"), pane, replacement)
                          for side in ("first", "second")}}

    @override
    def result(self, *arguments: str, deadline: float = math.inf) -> dict[str, object]:
        if arguments[:2] == ("pane", "process-info"):
            pane = next(row for row in self.tabs if row["pane_id"] == arguments[-1])
            if pane["pane_id"] in self.unknown:
                raise herdr.HerdrFailure("private process facts are unavailable")
            if "session_id" not in pane:
                return {"process_info": {"shell_pid": 990042, "foreground_process_group_id": 990042,
                    "foreground_processes": [{"pid": 990042, "argv": ["zsh"], "name": "zsh"}]}}
            pid = 991000 + self.tabs.index(pane)
            process: dict[str, object] = {"pid": pid, "name": "claude"}
            if pane["pane_id"] not in self.missing_argv:
                process["argv"] = ["claude"]
            return {"process_info": {"shell_pid": 990000 + self.tabs.index(pane),
                "foreground_process_group_id": pid, "foreground_processes": [process]}}
        if arguments[:2] == ("pane", "move") and "--tab" in arguments:
            self.commands.append(arguments)
            pane = next(pane for pane in self.tabs if pane["pane_id"] == arguments[2])
            tab = arguments[arguments.index("--tab") + 1]
            target = arguments[arguments.index("--target-pane") + 1]
            workspace = next(item["workspace_id"] for item in self.tabs if item["tab_id"] == tab)
            self.tabs.remove(pane)
            pane.update(tab_id=tab, workspace_id=workspace, pane_id=self.new_id("p"))
            self.tabs.append(pane)
            root = self.trees.get(tab, {"type": "pane", "pane_id": target})
            self.trees[tab] = self.replace_leaf(root, target, split_tree(
                arguments[arguments.index("--split") + 1], float(arguments[arguments.index("--ratio") + 1]), target, pane["pane_id"]))
            return {"move_result": {"changed": True, "pane": {"pane_id": pane["pane_id"]}}}
        return super().result(*arguments, deadline=deadline)


class RowTabTests(StaffTestCase):
    rows: dict[str, staff.StaffRow] = {}
    client: RowTabsHerdr = RowTabsHerdr()

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})))
        self.rows = {}
        self.rows["parent"] = fixture_member("showrunner", name="new-production")
        self.rows["unit"] = fixture_member("unit", name="director", parent=self.rows["parent"].id)
        for key in ("split", "worker", "stopped"):
            self.rows[key] = fixture_member("worker", name=key, parent=self.rows["unit"].id)
        for key, row in self.rows.items():
            self.rows[key] = staff.update_current_session(row.current_session,
                staff.native_id(fixture_runtime_id("row-tab-runtime:" + key)))
        self.client = RowTabsHerdr()
        self.client.spaces = [("w1", "old-production"), ("w2", self.rows["parent"].name)]
        for index, row in enumerate((self.rows["unit"], self.rows["split"], self.rows["worker"])):
            self.client.tabs.append({"pane_id": f"p{index}", "tab_id": "t0" if index < 2 else "t1",
                "workspace_id": "w1", "label": "duplicate-old-title", "pane_label": row.name, "session_id": row.current_session, "mark": "stale",
                "terminal_id": f"terminal-{index}"})
        proc = {"HERDR_PROC": str(self.root / "proc"), "HERDR_PLATFORM": "linux"}
        fake_unit_host.process(proc, 990042, 1, ["zsh"], {})
        _ = cast(object, self.enterContext(patch.dict(os.environ, proc)))
        _ = self.enterContext(patch.object(herdr, "native_process", side_effect=self.client.native_process))

    def bindings(self) -> list[tuple[str, str]]:
        return [(space["name"], tab["name"]) for space in herdr.saved_layout("fixture")["workspaces"]
                for tab in space["tabs"]]

    @contextmanager
    def refused_command(self, method: Literal["run", "result"], prefix: tuple[str, ...], reason: str,
                        *, option: str = "") -> Generator[io.StringIO]:
        original = self.client.run if method == "run" else self.client.result
        def refuse(*arguments: str, deadline: float = math.inf) -> str | dict[str, object]:
            if arguments[:len(prefix)] == prefix and (not option or option in arguments):
                raise herdr.HerdrFailure(reason)
            return original(*arguments, deadline=deadline)
        with patch.object(self.client, method, side_effect=refuse), redirect_stderr(io.StringIO()) as errors:
            yield errors

    def assert_complete_rerun(self) -> None:
        self.client.commands.clear()
        self.assertFalse(self.client.move_row_tabs(self.rows["unit"].id))
        self.assertEqual(self.client.commands, [])

    def test_a_staff_root_has_no_parent_workspace(self) -> None:
        before = self.staff_file.read_bytes()
        with self.assertRaisesRegex(herdr.HerdrFailure, "has no parent workspace"):
            _ = self.client.move_row_tabs(self.rows["parent"].id)
        self.assertEqual(self.client.commands, [])
        self.assertEqual(self.staff_file.read_bytes(), before)

    def test_layout_scan_retains_each_unknown_location_and_uses_the_supplied_snapshot(self) -> None:
        self.client.unknown.update(("p1", "p2"))
        self.client.tabs.append({**self.client.tabs[0], "pane_id": "foreign",
            "terminal_id": "foreign-terminal", "tab_id": "foreign-tab",
            "session_id": self.rows["parent"].current_session})
        record = staff.StaffSnapshot(tuple(row for row in staff.snapshot().rows
                                          if row.id != self.rows["parent"].id))

        with redirect_stderr(io.StringIO()) as errors:
            self.client.record_layout(record=record)

        self.assertEqual(len(self.client.scans), 1)
        scan = self.client.scans[0]
        self.assertEqual({row_id: [pane["pane_id"] for pane in panes]
                          for row_id, panes in scan.found.items()}, {self.rows["unit"].id: ["p0"]})
        self.assertEqual([(item.pane, item.tab, item.reason) for item in scan.unknown], [
            ("p1", "t0", "private process facts are unavailable"),
            ("p2", "t1", "private process facts are unavailable")])
        notes = errors.getvalue().splitlines()
        self.assertEqual(len(notes), 2)
        for pane, tab in (("p1", "t0"), ("p2", "t1")):
            self.assertEqual(sum(pane in line and tab in line for line in notes), 1)
        self.assertEqual(self.bindings(), [("old-production", self.rows["unit"].id)])

    def test_retitle_uses_proved_rows_and_reports_an_unknown_pane_once(self) -> None:
        self.client.unknown.add("p2")
        self.client.tabs[1]["pane_label"] = "old-worker-label"

        with redirect_stderr(io.StringIO()) as errors:
            self.assertTrue(self.client.retitle_row(self.rows["split"].id))

        self.assertEqual(self.client.tabs[1]["pane_label"], self.rows["split"].name)
        self.assertEqual(self.client.tabs[2]["pane_label"], self.rows["worker"].name)
        self.assertEqual(len(self.client.scans), 1)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        for text in ("p2", "t1", "private process facts are unavailable"):
            self.assertIn(text, errors.getvalue())

    def test_transfer_continues_for_a_proved_tab_and_keeps_an_unknown_tab(self) -> None:
        unknown = dict(self.client.tabs[2])
        self.client.unknown.add(unknown["pane_id"])

        with redirect_stderr(io.StringIO()) as errors:
            self.assertTrue(self.client.move_row_tabs(self.rows["unit"].id))

        moved = [pane for pane in self.client.tabs if pane["terminal_id"] != unknown["terminal_id"]]
        self.assertEqual({pane["workspace_id"] for pane in moved}, {"w2"})
        self.assertEqual(next(pane for pane in self.client.tabs
                              if pane["terminal_id"] == unknown["terminal_id"]), unknown)
        self.assertEqual(len(self.client.scans), 1)
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        for text in (unknown["pane_id"], unknown["tab_id"], "private process facts are unavailable"):
            self.assertIn(text, errors.getvalue())

    def test_moved_unit_takes_its_alphabetical_place_with_a_saved_tab_order(self) -> None:
        parent = self.rows["parent"]
        alpha = fixture_member("unit", name="alpha", parent=parent.id)
        zulu = fixture_member("unit", name="zulu", parent=parent.id)
        other = fixture_member("worker", name="another-tab", parent=parent.id)
        source = dict(self.client.tabs[0])
        for saved_order in (False, True):
            with self.subTest(saved_order=saved_order):
                self.client.tabs = [dict(source)]
                self.assert_moved_unit_order((parent, alpha, zulu, other),
                    [parent, alpha, self.rows["unit"], zulu, other], saved_order=saved_order)

    def test_moved_unit_takes_its_place_past_a_saved_worker_tab(self) -> None:
        parent = self.rows["parent"]
        alpha = fixture_member("unit", name="alpha", parent=parent.id)
        bravo = fixture_member("unit", name="bravo", parent=parent.id)
        zulu = fixture_member("unit", name="zulu", parent=parent.id)
        worker = fixture_member("worker", name="review", parent=bravo.id)
        self.assert_moved_unit_order((parent, alpha, worker, bravo, zulu),
            [parent, alpha, worker, bravo, self.rows["unit"], zulu], saved_order=True)

    def assert_moved_unit_order(self, members: tuple[staff.StaffRow, ...], expected: list[staff.StaffRow],
                                *, saved_order: bool) -> None:
        parent = self.rows["parent"]
        initial = [dict(self.client.tabs[0])]
        for index, member in enumerate(members, 1):
            initial.append({"pane_id": f"destination-p{index}", "tab_id": f"destination-t{index}",
                "workspace_id": "w2", "label": member.name, "session_id": member.current_session,
                "terminal_id": f"destination-terminal-{index}"})
        self.client.tabs = [dict(pane) for pane in initial]
        self.client.focused_pane = "p0"
        saved = herdr.SavedLayout(at=time.time(), workspaces=[])
        if saved_order:
            saved["workspaces"] = [
                herdr.SavedSpace(name="old-production", seen=time.time(), tabs=[
                    herdr.SavedName(name=self.rows["unit"].id, seen=time.time())]),
                herdr.SavedSpace(name=parent.name, seen=time.time(), tabs=[
                    herdr.SavedName(name=member.id, seen=time.time())
                    for member in members])]
        path = herdr.layout_path("fixture")
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps(saved))

        self.assertTrue(self.client.move_row_tabs(self.rows["unit"].id))

        self.assertEqual([tab["label"] for tab in self.client.listed("tab", "--workspace", "w2")],
                         [member.name for member in expected])
        destination = next(space for space in herdr.saved_layout("fixture")["workspaces"]
                           if space["name"] == parent.name)
        if saved_order:
            self.assertEqual([tab["name"] for tab in destination["tabs"]],
                             [member.id for member in expected]
                             + [self.rows[key].id for key in ("split", "stopped", "worker")])
        self.assert_complete_rerun()

    def test_record_layout_binds_the_native_row_instead_of_a_stale_mark(self) -> None:
        self.client.tabs = self.client.tabs[:1]
        self.client.tabs[0]["mark"] = self.rows["stopped"].current_session

        self.client.record_layout()

        self.assertEqual(self.bindings(), [("old-production", self.rows["unit"].id)])

    def test_record_layout_does_not_bind_a_mark_without_a_native_process(self) -> None:
        self.client.tabs = self.client.tabs[:1]
        self.client.tabs[0]["mark"] = self.rows["unit"].current_session
        del self.client.tabs[0]["session_id"]

        self.client.record_layout()

        self.assertEqual(self.bindings(), [])

    def test_record_layout_keeps_the_saved_place_when_pane_evidence_is_unknown(self) -> None:
        self.client.tabs = self.client.tabs[:2]
        self.client.tabs[0]["mark"] = self.rows["unit"].current_session
        self.client.tabs[1]["mark"] = self.rows["stopped"].current_session
        self.client.unknown.add(self.client.tabs[1]["pane_id"])
        saved = herdr.SavedSpace(name="remembered", seen=time.time(), tabs=[
            herdr.SavedName(name=self.rows["stopped"].id, seen=time.time(), terminal_id="retained-terminal")])
        path = herdr.layout_path("fixture")
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps(herdr.SavedLayout(at=time.time(), workspaces=[saved])))
        record = staff.StaffSnapshot(tuple(row for row in staff.snapshot().rows if row.id != self.rows["stopped"].id))

        with redirect_stderr(io.StringIO()) as errors:
            self.client.record_layout(record=record)

        self.assertEqual(self.bindings(), [("remembered", self.rows["stopped"].id), ("old-production", self.rows["unit"].id)])
        self.assertEqual(herdr.saved_layout("fixture")["workspaces"][0], saved)
        self.assertIn("pane evidence is unknown", errors.getvalue())

    def test_unknown_pane_evidence_keeps_old_workspace_and_tab_bindings(self) -> None:
        self.client.tabs = self.client.tabs[:2]
        self.client.tabs[1]["session_id"] = self.rows["stopped"].current_session
        self.client.unknown.add(self.client.tabs[1]["pane_id"])
        now = time.time()
        old = now - herdr.LAYOUT_KEEP_SECONDS - 1
        remembered = herdr.SavedSpace(name="remembered", seen=old, tabs=[
            herdr.SavedName(name=self.rows["worker"].id, seen=old, terminal_id="old-workspace-terminal")])
        stopped = herdr.SavedName(name=self.rows["stopped"].id, seen=old, terminal_id="old-tab-terminal")
        saved = herdr.SavedLayout(at=old, workspaces=[remembered,
            herdr.SavedSpace(name="old-production", seen=old, tabs=[
                herdr.SavedName(name=self.rows["unit"].id, seen=old), stopped])])
        path = herdr.layout_path("fixture")
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps(saved))

        with redirect_stderr(io.StringIO()) as errors:
            self.client.record_layout()

        result = herdr.saved_layout("fixture")
        self.assertEqual(self.bindings(), [("remembered", self.rows["worker"].id),
            ("old-production", self.rows["unit"].id), ("old-production", self.rows["stopped"].id)])
        self.assertEqual(result["workspaces"][0], remembered)
        self.assertEqual(result["workspaces"][1]["tabs"][1], stopped)
        self.assertGreaterEqual(result["workspaces"][1]["tabs"][0]["seen"], now)
        self.assertIn("pane evidence is unknown", errors.getvalue())
        self.assertIn("row placement retained", errors.getvalue())

    def test_known_pane_evidence_expires_old_workspace_and_tab_bindings(self) -> None:
        self.client.tabs = self.client.tabs[:2]
        del self.client.tabs[1]["session_id"]
        now = time.time()
        old = now - herdr.LAYOUT_KEEP_SECONDS - 1
        saved = herdr.SavedLayout(at=old, workspaces=[
            herdr.SavedSpace(name="remembered", seen=old, tabs=[
                herdr.SavedName(name=self.rows["worker"].id, seen=old, terminal_id="old-workspace-terminal")]),
            herdr.SavedSpace(name="old-production", seen=old, tabs=[
                herdr.SavedName(name=self.rows["unit"].id, seen=old),
                herdr.SavedName(name=self.rows["stopped"].id, seen=old, terminal_id="old-tab-terminal")])])
        path = herdr.layout_path("fixture")
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps(saved))

        with redirect_stderr(io.StringIO()) as errors:
            self.client.record_layout()

        self.assertEqual(self.bindings(), [("old-production", self.rows["unit"].id)])
        tabs = herdr.saved_layout("fixture")["workspaces"][0]["tabs"]
        for tab in tabs:
            self.assertGreaterEqual(tab["seen"], now)
        self.assertEqual(errors.getvalue(), "")

    def test_transfer_preserves_shared_tabs_separate_tabs_and_nested_grid_splits(self) -> None:
        initial = [dict(pane) for pane in self.client.tabs]
        for grid in (False, True):
            with self.subTest(grid=grid):
                self.client.tabs = [dict(pane) for pane in initial]
                self.client.focused_pane = "p1"
                if grid:
                    self.client.tabs.append({**initial[0], "pane_id": "p3", "terminal_id": "terminal-3",
                                             "session_id": self.rows["stopped"].current_session})
                    self.client.tabs[2]["tab_id"] = "t0"
                    self.client.trees["t0"] = split_tree("right", 0.4, split_tree("down", 0.3, "p0", "p1"),
                                                      split_tree("down", 0.7, "p2", "p3"))
                terminals = {pane["terminal_id"] for pane in self.client.tabs}
                before = self.staff_file.read_bytes()
                self.assertTrue(self.client.move_row_tabs(self.rows["unit"].id))
                self.assertEqual({pane["workspace_id"] for pane in self.client.tabs}, {"w2"})
                panes = {pane["terminal_id"]: pane for pane in self.client.tabs}
                self.assertEqual(panes["terminal-0"]["tab_id"], panes["terminal-1"]["tab_id"])
                self.assertEqual(panes["terminal-0"]["tab_id"] == panes["terminal-2"]["tab_id"], grid)
                ids = [panes[f"terminal-{index}"]["pane_id"] for index in range(4 if grid else 3)]
                expected = (split_tree("right", 0.4, split_tree("down", 0.3, ids[0], ids[1]),
                                       split_tree("down", 0.7, ids[2], ids[3])) if grid else split_tree("right", 0.3, ids[0], ids[1]))
                self.assertEqual(self.client.trees[panes["terminal-0"]["tab_id"]], expected)
                if grid:
                    self.assertEqual(len({pane["tab_id"] for pane in self.client.tabs}), 1)
                focused = next(pane for pane in self.client.tabs if pane["pane_id"] == self.client.focused_pane)
                self.assertEqual(focused["tab_id"], panes["terminal-1"]["tab_id"])
                self.assertEqual({pane["terminal_id"] for pane in self.client.tabs}, terminals)
                self.assertFalse({"p0", "p1", "p2", "p3"} & {pane["pane_id"] for pane in self.client.tabs})
                self.assertCountEqual(self.bindings(), [(self.rows["parent"].name, row.id)
                                      for row in self.rows.values() if row.kind != "showrunner"])
                self.assertEqual(self.staff_file.read_bytes(), before)
                self.assertEqual([tab["label"] for tab in self.client.listed("tab", "--workspace", "w2")],
                                 [self.rows["unit"].name] if grid else [self.rows["unit"].name, self.rows["worker"].name])
                self.assertFalse(any(command[:2] in (("pane", "close"), ("agent", "start"), ("tab", "close"))
                                     for command in self.client.commands))
                self.assert_complete_rerun()

    def test_transfer_restores_the_focused_tab_with_the_supported_command(self) -> None:
        self.client.tabs = self.client.tabs[:2]
        self.assertTrue(self.client.move_row_tabs(self.rows["unit"].id))
        moved = next(pane for pane in self.client.tabs if pane["terminal_id"] == "terminal-1")
        self.assertEqual(moved["workspace_id"], "w2")
        self.assertIn(("tab", "focus", moved["tab_id"]), self.client.commands)
        focused = next(pane for pane in self.client.tabs if pane["pane_id"] == self.client.focused_pane)
        self.assertEqual(focused["tab_id"], moved["tab_id"])

    def test_focus_restore_failure_keeps_the_tab_move_result(self) -> None:
        self.client.tabs = self.client.tabs[:2]
        with self.refused_command("run", ("tab", "focus"), "private focus failure\nsecond line") as errors:
            self.assertTrue(self.client.move_row_tabs(self.rows["unit"].id))
        self.assertEqual(self.client.tabs[0]["workspace_id"], "w2")
        self.assertIn((self.rows["parent"].name, self.rows["unit"].id), self.bindings())
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn("focus restore failed: private focus failure second line", errors.getvalue())

    def test_unknown_native_evidence_writes_bindings_and_never_reports_unchanged(self) -> None:
        self.client.tabs = self.client.tabs[:1]
        for missing_argv, foreign in ((False, False), (True, False), (False, True)):
            with self.subTest(missing_argv=missing_argv, foreign=foreign):
                self.client.tabs[0]["session_id"] = self.rows["parent" if foreign else "unit"].current_session
                self.client.unknown = set() if missing_argv else {"p0"}
                self.client.missing_argv = {"p0"} if missing_argv else set()
                for _attempt in range(2):
                    with redirect_stderr(io.StringIO()) as errors:
                        self.assertTrue(self.client.move_row_tabs(self.rows["unit"].id))
                    self.assertEqual(self.client.tabs[0]["workspace_id"], "w1")
                    self.assertEqual(len(errors.getvalue().splitlines()), 1)
                    self.assertIn("argv" if missing_argv else "private process facts are unavailable", errors.getvalue())
                    self.assertCountEqual(self.bindings(), [(self.rows["parent"].name, row.id)
                                          for row in self.rows.values() if row.kind != "showrunner"])

    def test_stopped_rows_restart_in_the_new_production_with_only_pane_replies(self) -> None:
        self.client.tabs.clear()

        def result(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
            self.client.require_time(deadline)
            if arguments == ("pane", "list"):
                return {"panes": []}
            raise herdr.HerdrFailure(f"unconfigured command: {arguments}")

        with patch.object(self.client, "result", side_effect=result):
            self.assertTrue(self.client.move_row_tabs(self.rows["unit"].id))
            self.assertCountEqual(self.bindings(), [(self.rows["parent"].name, row.id)
                                  for row in self.rows.values() if row.kind != "showrunner"])
            saved = herdr.layout_path("fixture").read_bytes()
            self.assertFalse(self.client.move_row_tabs(self.rows["unit"].id))
            self.assertEqual(herdr.layout_path("fixture").read_bytes(), saved)
        self.assertEqual(self.client.saved_row_tab(self.rows["stopped"].id, self.rows["stopped"].name, "fallback"),
                         herdr.NewTab(self.rows["parent"].name, self.rows["stopped"].name))

    def test_retitle_row_uses_native_sessions_despite_stale_marks_and_duplicate_titles(self) -> None:
        for pane in self.client.tabs[:2]:
            pane["pane_label"] = "old-pane"
        self.client.tabs[1]["session_id"] = self.rows["unit"].current_session
        self.client.tabs[2]["mark"] = self.rows["unit"].current_session
        self.assertTrue(self.client.retitle_row(self.rows["unit"].id))
        renames = [command for command in self.client.commands if command[:2] == ("pane", "rename")]
        self.assertCountEqual(renames, [("pane", "rename", "p0", self.rows["unit"].name),
                                      ("pane", "rename", "p1", self.rows["unit"].name)])
        self.assertEqual(self.client.tabs[2]["label"], "duplicate-old-title")
        self.client.commands.clear()
        self.assertFalse(self.client.retitle_row(self.rows["unit"].id))
        self.assertEqual(self.client.commands, [])

    def test_sort_preserves_worker_columns(self) -> None:
        before = [(pane["pane_id"], pane["tab_id"], pane["terminal_id"]) for pane in self.client.tabs]
        _ = self.client.sort_tabs()
        self.assertCountEqual(before, [(pane["pane_id"], pane["tab_id"], pane["terminal_id"])
                                      for pane in self.client.tabs])
        self.client.commands.clear()
        _ = self.client.sort_tabs()
        self.assertFalse(any(command[0] == "tab.move" for command in self.client.commands))

    def test_ordering_retry_keeps_split_tabs_and_closes_only_the_placeholder(self) -> None:
        self.assertTrue(self.client.move_row_tabs(self.rows["unit"].id))
        original = {pane["terminal_id"]: (pane["pane_id"], pane["tab_id"]) for pane in self.client.tabs}
        self.client.tabs.reverse()
        _ = self.client.add_tab("w2", herdr.LAYOUT_PLACEHOLDER)
        selected = self.client.focused_pane
        self.client.spaces.append(("w3", "unrelated"))
        _ = self.client.add_tab("w3", "unrelated")
        foreign = self.client.tabs[-1]["pane_id"]
        request = self.client.socket_request

        def refuse(method: str, **params: object) -> dict[str, object]:
            if method == "tab.move":
                self.client.focused_pane, self.client.focused_workspace = foreign, "w3"
                raise herdr.HerdrFailure("private order failure")
            return request(method, **params)

        with patch.object(self.client, "socket_request", side_effect=refuse), self.assertRaisesRegex(herdr.HerdrFailure, "private order failure"):
            _ = self.client.move_row_tabs(self.rows["unit"].id)
        focused = next(pane for pane in self.client.tabs if pane["pane_id"] == self.client.focused_pane)
        selected_tab = next(pane["tab_id"] for pane in self.client.tabs if pane["pane_id"] == selected)
        self.assertEqual(focused["tab_id"], selected_tab)
        self.assertTrue(next(space for space in self.client.listed("workspace") if space["workspace_id"] == "w2")["focused"])
        self.assertTrue(self.client.move_row_tabs(self.rows["unit"].id))
        self.assertEqual([tab["label"] for tab in self.client.listed("tab", "--workspace", "w2")],
                         [self.rows["unit"].name, self.rows["worker"].name])
        self.assertEqual({pane["terminal_id"]: (pane["pane_id"], pane["tab_id"]) for pane in self.client.tabs if pane["workspace_id"] == "w2"}, original)
        self.assertFalse(any(pane["label"] == herdr.LAYOUT_PLACEHOLDER for pane in self.client.tabs))
        self.assert_complete_rerun()

    def test_an_unrelated_focused_tab_survives_destination_ordering(self) -> None:
        for index, name in enumerate(("zulu", "alpha"), 3):
            row = fixture_member("showrunner", name=name)
            self.client.tabs.append({"pane_id": f"p{index}", "tab_id": f"t{index}", "workspace_id": "w2",
                "label": name, "terminal_id": f"terminal-{index}", "session_id": row.current_session})
        self.client.tabs.append({**self.client.tabs[-2], "pane_id": "p5", "terminal_id": "terminal-5"})
        herdr._write_layout("fixture", herdr.SavedLayout(at=time.time(), workspaces=[herdr.SavedSpace(  # pyright: ignore[reportPrivateUsage]
            name=self.rows["parent"].name, seen=time.time(), tabs=[herdr.SavedName(name=ident, seen=time.time())
                for ident in [self.rows["unit"].id, self.rows["worker"].id, *layout_row_ids(["alpha", "zulu"])]] )]))
        self.client.focused_pane = "p5"
        self.assertTrue(self.client.move_row_tabs(self.rows["unit"].id))
        focused = next(pane for pane in self.client.tabs if pane["pane_id"] == self.client.focused_pane)
        self.assertEqual(focused["tab_id"], "t3")
        self.assertEqual(next(pane["tab_id"] for pane in self.client.tabs if pane["pane_id"] == "p5"), "t3")
        self.assertEqual([tab["label"] for tab in self.client.listed("tab", "--workspace", "w2")],
                         [self.rows["unit"].name, self.rows["worker"].name, "alpha", "zulu"])
        self.assertTrue(any(command[0] == "tab.move" for command in self.client.commands))

    def test_a_title_failure_is_repaired_on_rerun_without_moving_the_pane_again(self) -> None:
        self.client.tabs = self.client.tabs[:1]
        with self.refused_command("run", ("tab", "rename"), "private rename failure") as errors:
            self.assertTrue(self.client.move_row_tabs(self.rows["unit"].id))
        self.assertIn("private rename failure", errors.getvalue())
        pane_id = self.client.tabs[0]["pane_id"]
        self.client.commands.clear()
        self.assertTrue(self.client.move_row_tabs(self.rows["unit"].id))
        self.assertEqual(self.client.tabs[0]["label"], self.rows["unit"].name)
        self.assertEqual(self.client.tabs[0]["pane_id"], pane_id)
        self.assertFalse(any(command[:2] == ("pane", "move") for command in self.client.commands))
        self.assert_complete_rerun()

    def test_a_missing_destination_is_created_and_the_moved_tabs_are_ordered(self) -> None:
        _ = self.client.spaces.pop()
        self.assertTrue(self.client.move_row_tabs(self.rows["unit"].id))
        workspace = next(ident for ident, name in self.client.spaces if name == self.rows["parent"].name)
        self.assertEqual({pane["workspace_id"] for pane in self.client.tabs}, {workspace})
        self.assertEqual([tab["label"] for tab in self.client.listed("tab", "--workspace", workspace)],
                         [self.rows["unit"].name, self.rows["worker"].name])
        self.assertFalse(any(pane["label"] == herdr.LAYOUT_PLACEHOLDER for pane in self.client.tabs))

    def test_worker_retitle_keeps_the_director_tab_title(self) -> None:
        self.client.tabs[0]["label"] = self.rows["unit"].name
        self.client.tabs[1]["pane_label"] = "old-pane"
        self.assertTrue(self.client.retitle_row(self.rows["split"].id))
        self.assertEqual(self.client.tabs[1]["pane_label"], self.rows["split"].name)
        self.assertEqual(self.client.tabs[0]["label"], self.rows["unit"].name)
        self.assertFalse(any(command[:2] == ("tab", "rename") for command in self.client.commands))
        self.assertFalse(self.client.retitle_row(self.rows["split"].id))

    def test_rerun_repairs_a_worker_tab_title_and_keeps_the_shared_director_title(self) -> None:
        self.assertTrue(self.client.move_row_tabs(self.rows["unit"].id))
        worker = next(pane for pane in self.client.tabs if pane["terminal_id"] == "terminal-2")
        self.client.tabs.append({**worker, "pane_id": "shell", "terminal_id": "shell-terminal"})
        del self.client.tabs[-1]["session_id"]
        for pane in self.client.tabs:
            pane["label"] = "stale title"
        self.assertTrue(self.client.move_row_tabs(self.rows["unit"].id))
        self.assertEqual([tab["label"] for tab in self.client.listed("tab", "--workspace", "w2")],
                         [self.rows["unit"].name, self.rows["worker"].name])
        self.assertFalse(self.client.move_row_tabs(self.rows["unit"].id))

    def test_an_interrupted_rebuild_names_the_tab_and_panes_left_in_the_source(self) -> None:
        self.client.tabs = self.client.tabs[:2]
        with self.refused_command("result", ("pane", "move"), "private split failure", option="--tab") as errors:
            self.assertTrue(self.client.move_row_tabs(self.rows["unit"].id))
        self.assertEqual({pane["workspace_id"] for pane in self.client.tabs}, {"w1", "w2"})
        for text in ("t0", "p1", "split", "private split failure"):
            self.assertIn(text, errors.getvalue())
        self.assertFalse(any(command[:2] in (("pane", "close"), ("agent", "start")) for command in self.client.commands))

    def test_zoom_and_unchanged_moves_keep_bindings_without_claiming_success(self) -> None:
        self.client.tabs = self.client.tabs[:1]
        result = self.client.result
        def unchanged(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
            if arguments[:2] == ("pane", "move"):
                return {"move_result": {"changed": False, "reason": "private refusal"}}
            return result(*arguments, deadline=deadline)
        for zoomed, message in ((True, "tab is zoomed; unzoom it and run again"), (False, "private refusal")):
            with self.subTest(zoomed=zoomed):
                self.client.zoomed = {"t0"} if zoomed else set()
                with patch.object(self.client, "result", side_effect=unchanged), redirect_stderr(io.StringIO()) as errors:
                    self.assertTrue(self.client.move_row_tabs(self.rows["unit"].id))
                self.assertIn(message, errors.getvalue())
                self.assertEqual(self.client.tabs[0]["workspace_id"], "w1")
                self.assertIn((self.rows["parent"].name, self.rows["unit"].id), self.bindings())
                self.assertFalse(any(command[:2] == ("pane", "move") for command in self.client.commands))


@final
class SavedLayoutTests(unittest.TestCase):
    """A restart puts every session back in its saved workspace and tab, in the saved order (user, 2026-10-21)."""

    BEFORE: list[tuple[str, list[str]]] = [
        ("mail", ["postmaster", "ups"]),
        ("cargo-liner", ["cargo-liner", "berth", "tile"]),
        ("hana", ["hana", "assembly", "widget"]),
        ("fps", ["performance", "benchmark", "frame-cost", "metrics-gathering"]),
        ("natedev", ["natedev", "accounts", "herdr", "unit-board"]),
        ("sdlc", ["sdlc", "roles"]),
        ("cargo-liner-style", ["cargo-liner-style", "names"]),
    ]

    def __init__(self, method: str = "runTest") -> None:
        super().__init__(method)
        self.root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        _ = self.enterContext(patch.object(herdr, "herdr_processes", return_value=[]))
        proc = self.root / "proc"
        task = proc / "42/task/42"
        task.mkdir(parents=True)
        _ = (task / "children").write_text("")
        _ = (proc / "42/stat").write_text("42 (shell) S 1 42 42 1 42 " + "0 " * 13 + "4200\n")
        _ = self.enterContext(patch.object(herdr, "_PLATFORM", "linux"))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state"),
            "HOME": str(self.root), "HERDR_BIN": "/nonexistent/herdr", "HERDR_PROC": str(proc),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions")})))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        self.enterContext(fake_unit_host.private_process_registry())

    def running(self) -> LayoutHerdr:
        client = LayoutHerdr()
        for space, titles in self.BEFORE:
            for title in titles:
                _ = client.open_tab(space, title, "/repo", {})
        return client

    def test_saved_row_tab_uses_the_supplied_fallback_workspace(self) -> None:
        client = LayoutHerdr()
        self.assertEqual(client.saved_row_tab("missing-row", "unit", "production"),
                         herdr.NewTab("production", "unit"))

    def test_remember_row_tab_moves_its_saved_binding_to_the_new_workspace(self) -> None:
        client = LayoutHerdr()
        client.spaces = [("w-a", "workspace-a"), ("w-b", "workspace-b")]
        _ = client.add_tab("w-a", "moving-row")
        row_id = layout_row_ids(["moving-row"])[0]
        client.remember_row_tab(row_id, client.tabs[0]["pane_id"])
        self.assertEqual([(space["name"], tab["name"])
                          for space in herdr.saved_layout("fixture")["workspaces"] for tab in space["tabs"]],
                         [("workspace-a", row_id)])

        client.tabs[0].update(workspace_id="w-b", tab_id="tab-b", pane_id="pane-b")
        client.remember_row_tab(row_id, "pane-b")

        self.assertEqual([(space["name"], tab["name"])
                          for space in herdr.saved_layout("fixture")["workspaces"] for tab in space["tabs"]],
                         [("workspace-b", row_id)])
        self.assertEqual(client.saved_row_tab(row_id, "moving-row", "fallback"),
                         herdr.ExistingPane("pane-b", "terminal-p2"))

    def test_remember_row_tab_keeps_a_pending_row_before_the_restarted_row(self) -> None:
        client = LayoutHerdr()
        client.spaces = [("w1", "workspace")]
        for name in ("pending", "restarted", "other"):
            _ = client.add_tab("w1", name)
        client.record_layout()
        expected = layout_row_ids(["pending", "restarted", "other"])
        del client.tabs[0]

        client.remember_row_tab(expected[1], client.tabs[0]["pane_id"])

        self.assertEqual([tab["name"] for tab in herdr.saved_layout("fixture")["workspaces"][0]["tabs"]],
                         expected)

    def test_retained_removed_units_and_wrapped_parents_do_not_classify_tabs_as_current_staff(self) -> None:
        retired = unit_membership("current", "retired")
        old_child = unit_membership("wrapped", "child")
        _ = staff.set_worktree(retired.id, staff.RemovedWorktree("natedev", self.root / "retired"), expected=retired.worktree)
        assert isinstance(old_child.parent, staff.ReportsTo)
        _ = staff.set_doc(old_child.parent.id, staff.WrappedDoc("natedev", self.root / "archive.md"))
        processes = [herdr.HerdrProcess(1, herdr.HerdrPlace("fixture", "w1:p1"),
                                       {"STAFF_ID": retired.id}, "claude", "/missing")]
        classifier = herdr.UnitTabClassifier("fixture", processes, staff.snapshot())
        self.assertIsInstance(classifier.classify("current", "retired", ["w1:p1"]), herdr.OtherTab)
        self.assertIsInstance(classifier.classify("wrapped", "child", []), herdr.OtherTab)
        self.assertFalse(classifier.has_unit_title("retired"))
        self.assertFalse(classifier.has_unit_title("child"))

    def test_session_names_restore_saved_tab_order_and_resolve_each_pane(self) -> None:
        old = LayoutHerdr()
        for key in ("sdlc", "zulu", "alpha"):
            _ = old.open_tab("sdlc", key, "/repo", {})
        order = [tab["pane_label"] for tab in old.tabs]
        self.assertEqual([tab["name"] for tab in herdr.saved_layout("fixture")["workspaces"][0]["tabs"]], layout_row_ids(order))

        fresh = LayoutHerdr()
        for key in reversed(order):
            _ = fresh.open_tab("sdlc", key, "/repo", {})
        self.assertEqual([tab["pane_label"] for tab in fresh.tabs], order)
        for tab in fresh.tabs:
            key = tab["pane_label"]
            self.assertEqual(tab["label"], key)
            self.assertTrue(herdr.attach_hint("sdlc", key).endswith("tab " + key))
        self.assertEqual([tab["name"] for tab in herdr.saved_layout("fixture")["workspaces"][0]["tabs"]], layout_row_ids(order))

    def test_showrunner_unit_director_and_plain_tabs_and_panes_take_the_session_name(self) -> None:
        client = LayoutHerdr()
        cases: tuple[tuple[str, str, dict[str, str]], ...] = (
            ("control", "control", {}),
            ("control", "roles", {('' + 'SHOWRUNNER_' + 'UNIT'): "sdlc", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "roles"}),
            ("scratch", "scratch", {}))
        for space, name, environment in cases:
            with self.subTest(session=name):
                pane = client.open_tab(space, name, "/repo", environment)
                tab = next(tab for tab in client.tabs if tab["pane_id"] == pane)
                self.assertEqual(tab["label"], name)
                self.assertEqual(tab["pane_label"], name)
                for added in (" · ", "sdlc", "showrunner", "unit director"):
                    self.assertNotIn(added, tab["label"])

    def test_a_reboot_restart_in_any_order_brings_back_every_workspace_and_tab_in_place(self) -> None:
        self.assertEqual(self.running().layout(), self.BEFORE)
        launches = [(space, title) for space, titles in self.BEFORE for title in titles]
        for seed in range(5):
            with self.subTest(seed=seed):
                import random
                random.Random(seed).shuffle(launches)
                fresh = LayoutHerdr()
                fresh.prepare_restore_workspaces(frozenset(space for space, _ in self.BEFORE))
                for space, title in launches:
                    _ = fresh.open_tab(space, title, "/repo", {})
                self.assertEqual(fresh.layout(), self.BEFORE)

    def test_placeholder_with_a_running_program_survives_cleanup(self) -> None:
        _ = self.running()
        fresh = LayoutHerdr()
        fresh.prepare_restore_workspaces(frozenset({"hana"}))
        with patch.object(fresh, "pane_activity", return_value=herdr.PaneRunning(("editor",))):
            fresh.close_restore_placeholders()
        self.assertEqual(fresh.layout(), [("hana", [herdr.LAYOUT_PLACEHOLDER])])

    def test_a_new_tab_takes_its_a_to_z_place_and_never_passes_the_first_tab(self) -> None:
        client = self.running()
        _ = client.open_tab("natedev", "builds", "/repo", {})
        _ = client.open_tab("fps", "alpha", "/repo", {})
        _ = client.open_tab("natedev", "zebra", "/repo", {})
        shown = dict(client.layout())
        self.assertEqual(shown["natedev"], ["natedev", "accounts", "builds", "herdr", "unit-board", "zebra"])
        self.assertEqual(shown["fps"], ["performance", "alpha", "benchmark", "frame-cost", "metrics-gathering"])


    def test_the_snapshot_keeps_the_row_id_when_its_name_changes(self) -> None:
        client = self.running()
        record = staff.initial_snapshot()
        roles = next((row for row in record.rows if row.name == "roles"), None)
        assert roles is not None
        saved_before = herdr.saved_layout("fixture")
        renamed = staff.rename(roles.id, "design")
        tab = next(tab for tab in client.tabs if tab["label"] == "roles")
        _ = client.run("tab", "rename", tab["tab_id"], renamed.name)
        client.label_pane(tab["pane_id"], renamed.name)
        with patch.object(herdr, "shared_session", return_value="fixture"), patch.object(herdr, "Herdr", return_value=client):
            herdr.save_shared_layout({"other"})
            self.assertEqual(herdr.saved_layout("fixture"), saved_before)
            herdr.save_shared_layout({"fixture", "other"})
        saved = {space["name"]: [tab["name"] for tab in space["tabs"]]
                 for space in herdr.saved_layout("fixture")["workspaces"]}
        self.assertEqual(saved["sdlc"], layout_row_ids(["sdlc", "design"]))
        self.assertEqual(saved["sdlc"][-1], roles.id)
        self.assertIn("design", dict(client.layout())["sdlc"])

    def test_without_a_saved_layout_the_showrunner_comes_first_then_a_to_z(self) -> None:
        self.assertEqual(herdr.tab_order("boss", ["delta", "boss", "alpha"], [], set()), [1, 2, 0])

    def test_unit_directors_sort_before_other_tabs_from_their_panes_marks(self) -> None:
        for name in ("mike", "zulu"):
            _ = unit_membership("boss", name)
        client = LayoutHerdr()
        client.spaces.append(("w1", "boss"))
        for title in ("apple", "zulu", "boss", "beta", "mike", "able", "aardvark"):
            _ = client.add_tab("w1", title)
        marks = {('' + 'SHOWRUNNER_' + 'UNIT'): "prod", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "one"}
        processes = [herdr.HerdrProcess(index, herdr.HerdrPlace(session, tab["pane_id"]), environment, "claude", "/repo")
                     for index, (tab, session, environment) in enumerate((
                         (client.tabs[1], "fixture", marks), (client.tabs[4], "fixture", marks),
                         (client.tabs[5], "fixture", {('' + 'SHOWRUNNER_' + 'UNIT'): "prod"}),
                         (client.tabs[6], "other", marks)))]
        with patch.object(herdr, "herdr_processes", return_value=processes):
            _ = client.order_tabs("w1", herdr.SavedLayout(at=0.0, workspaces=[]))
        self.assertEqual(client.layout(), [("boss", ["boss", "mike", "zulu", "aardvark", "able", "apple", "beta"])])

    def test_new_unit_environment_sorts_before_terminals_before_its_process_starts(self) -> None:
        _ = unit_membership("boss", "zulu")
        client = LayoutHerdr()
        _ = client.open_tab("boss", "boss", "/repo", {})
        _ = client.open_tab("boss", "apple", "/repo", {})
        pane = client.open_tab("boss", "zulu", "/repo", {('' + 'SHOWRUNNER_' + 'UNIT'): "prod", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "one"})
        self.assertEqual(client.layout(), [("boss", ["boss", "zulu", "apple"])])
        self.assertEqual(next(tab["label"] for tab in client.tabs if tab["pane_id"] == pane), "zulu")
        processes = [herdr.HerdrProcess(1, herdr.HerdrPlace("fixture", pane),
                                       {('' + 'SHOWRUNNER_' + 'UNIT'): "prod", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "one"}, "claude", "/repo")]
        with patch.object(herdr, "herdr_processes", return_value=processes):
            _ = client.open_tab("boss", "banana", "/repo", {})
            _ = client.open_tab("boss", "aardvark", "/repo", {('' + 'SHOWRUNNER_' + 'UNIT'): "prod"})
        self.assertEqual(client.layout(), [("boss", ["boss", "zulu", "aardvark", "apple", "banana"])])

    def test_sort_tabs_uses_staff_identity_and_membership_after_hand_resume_or_transfer(self) -> None:
        for name in ("devtools", "roles", "style", "shutdown", "herdr", "pinned"):
            _ = unit_membership("sdlc", name)
        client = LayoutHerdr()
        client.spaces.extend([("w1", "sdlc"), ("w2", "natedev")])
        for title in ("sdlc", "devtools", "roles", "style", "shutdown", "herdr", "a-terminal", "renamed-tab"):
            _ = client.add_tab("w1", title)
        _ = client.add_tab("w2", "natedev")
        pinned = next(row for row in staff.snapshot().find(kind="unit") if row.name == "pinned")
        client.tabs[7]["session_id"] = pinned.current_session
        # Hand resumes have no marks; a transferred process can retain the old production mark.
        processes = [
            herdr.HerdrProcess(1, herdr.HerdrPlace("fixture", client.tabs[7]["pane_id"]),
                               {"CLAUDE_CODE_SESSION_ID": pinned.id, ('' + 'SHOWRUNNER_' + 'UNIT'): "natedev"}, "claude", "/repo"),
            herdr.HerdrProcess(2, herdr.HerdrPlace("fixture", client.tabs[6]["pane_id"]),
                               {('' + 'SHOWRUNNER_' + 'UNIT'): "sdlc", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "gone"}, "claude", "/repo"),
        ]
        with patch.object(herdr, "herdr_processes", return_value=processes), \
                patch.object(staff, "snapshot", wraps=staff.snapshot) as reads:
            _ = client.sort_tabs()
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(client.layout(), [
            ("sdlc", ["sdlc", "devtools", "herdr", "renamed-tab", "roles", "shutdown", "style", "a-terminal"]),
            ("natedev", ["natedev"]),
        ])
        self.assertEqual([tab["name"] for tab in herdr.saved_layout("fixture")["workspaces"][0]["tabs"]],
                         layout_row_ids(["sdlc", "devtools", "herdr", "pinned", "roles", "shutdown", "style", "a-terminal"]))

    def test_saved_tab_order_wins_over_unit_groups_and_alphabetical_order(self) -> None:
        titles = ["boss", "apple", "zulu", "mike"]
        saved = [herdr.SavedName(name=title, seen=100.0) for title in ("boss", "zulu", "apple", "mike")]
        order = herdr.tab_order("boss", titles, saved, {"mike", "zulu"})
        self.assertEqual([titles[index] for index in order], ["boss", "zulu", "apple", "mike"])

    def test_a_new_unit_takes_its_place_past_a_saved_worker_tab(self) -> None:
        titles = ["boss", "a-unit", "worker", "m-unit", "z-unit"]
        saved = [herdr.SavedName(name=title, seen=100.0) for title in titles[:-1]]
        order = herdr.tab_order("boss", titles, saved, {"a-unit", "m-unit", "z-unit"})
        self.assertEqual([titles[index] for index in order], titles)

    def test_new_units_and_terminals_take_their_places_after_the_first_saved_tab(self) -> None:
        titles = ["performance", "zulu", "apple", "mike", "banana"]
        saved = [herdr.SavedName(name=title, seen=100.0) for title in titles[:3]]
        order = herdr.tab_order("fps", titles, saved, {"mike", "zulu"})
        self.assertEqual([titles[index] for index in order], ["performance", "mike", "zulu", "apple", "banana"])

    def test_sort_without_saved_order_keeps_a_first_tab_whose_title_differs_from_its_workspace(self) -> None:
        titles = ["performance", "apple", "zulu", "mike"]
        order = herdr.tab_order("fps", titles, [], {"mike", "zulu"})
        self.assertEqual([titles[index] for index in order], ["performance", "mike", "zulu", "apple"])

    def test_a_gone_entry_keeps_its_place_until_it_is_old(self) -> None:
        saved = [herdr.SavedName(name="a", seen=100.0), herdr.SavedName(name="b", seen=100.0),
                 herdr.SavedName(name="c", seen=100.0)]
        self.assertEqual([entry["name"] for entry in herdr.merged_order(saved, ["c", "d"], 200.0)], ["a", "b", "c", "d"])
        late = 100.0 + herdr.LAYOUT_KEEP_SECONDS + 1
        self.assertEqual([entry["name"] for entry in herdr.merged_order(saved, ["c"], late)], ["c"])

    def test_an_unreadable_layout_file_is_an_empty_layout(self) -> None:
        path = herdr.layout_path("fixture")
        path.parent.mkdir(parents=True)
        _ = path.write_text("{not json")
        self.assertEqual(herdr.saved_layout("fixture"), herdr.SavedLayout(at=0.0, workspaces=[]))

    def test_a_partial_restart_keeps_the_place_of_the_sessions_still_to_come(self) -> None:
        _ = self.running()
        fresh = LayoutHerdr()
        fresh.prepare_restore_workspaces(frozenset({"natedev", "hana"}))
        for space, title in (("natedev", "herdr"), ("hana", "widget"), ("natedev", "natedev")):
            _ = fresh.open_tab(space, title, "/repo", {})
        self.assertEqual([space for space, _ in fresh.layout()], ["hana", "natedev"])
        again = LayoutHerdr()
        again.prepare_restore_workspaces(frozenset(space for space, _ in self.BEFORE))
        for space, titles in reversed(self.BEFORE):
            for title in reversed(titles):
                _ = again.open_tab(space, title, "/repo", {})
        self.assertEqual(again.layout(), self.BEFORE)


    def test_failed_restore_removes_only_unfilled_shell_placeholders(self) -> None:
        _ = self.running()
        fresh = LayoutHerdr()
        fresh.prepare_restore_workspaces(frozenset({"hana", "natedev"}))
        _ = fresh.open_tab("natedev", "herdr", "/repo", {})
        fresh.close_restore_placeholders()
        self.assertEqual(fresh.layout(), [("natedev", ["herdr"])])


    def test_late_session_renaming_a_placeholder_survives_cleanup(self) -> None:
        _ = self.running()
        fresh = LayoutHerdr()
        fresh.prepare_restore_workspaces(frozenset({"hana"}))
        def landed(_pane: str) -> herdr.PaneAtShell:
            fresh.tabs[0]["label"] = "late-session"
            return herdr.PaneAtShell()
        with patch.object(fresh, "pane_activity", side_effect=landed):
            fresh.close_restore_placeholders()
        self.assertEqual(fresh.layout(), [("hana", ["late-session"])])


class RunLoadedThreadsTests(unittest.TestCase):
    def test_current_and_retired_servers_are_read_with_all_pages(self) -> None:
        from scripts.agents.codex_mesh_test_support import StubAppServer
        from scripts.lib.unit_runs import SERVER_FILE

        def respond(request: dict[str, object]) -> list[dict[str, object]]:
            params = request.get("params", {})
            result: dict[str, object] = {}
            if request.get("method") == "thread/loaded/list":
                result = ({"data": ["second"]} if params else {"data": ["first"], "nextCursor": "page2"})
            return [{"id": request["id"], "result": result}]

        with tempfile.TemporaryDirectory() as folder:
            current, retired = StubAppServer(respond), StubAppServer(respond)
            self.addCleanup(current.close)
            self.addCleanup(retired.close)
            path = Path(folder) / SERVER_FILE
            _ = path.write_text(json.dumps({"pid": os.getpid(), "port": current.port,
                "previous_servers": [{"pid": os.getpid(), "port": retired.port}]}))
            self.assertEqual(herdr.loaded_threads(Path(folder), time.monotonic() + TEST_HANG_GUARD_SECS),
                             {"first", "second"})
            for server in (retired, current):
                self.assertEqual([request["params"] for request in server.requests
                                  if request["method"] == "thread/loaded/list"], [{}, {"cursor": "page2"}])

    def test_a_failed_page_gives_no_partial_result_and_expiry_prevents_a_connection(self) -> None:
        from scripts.agents.codex_mesh_test_support import StubAppServer
        from scripts.lib.unit_runs import SERVER_FILE

        def respond(request: dict[str, object]) -> list[dict[str, object]]:
            if request.get("method") == "thread/loaded/list":
                return [{"id": request["id"], "error": {"message": "probe failed"}}]
            return [{"id": request["id"], "result": {}}]

        with tempfile.TemporaryDirectory() as folder:
            server = StubAppServer(respond)
            self.addCleanup(server.close)
            path = Path(folder) / SERVER_FILE
            _ = path.write_text(json.dumps({"pid": os.getpid(), "port": server.port}))
            with self.assertRaisesRegex(ValueError, "probe failed"):
                _ = herdr.loaded_threads(Path(folder), time.monotonic() + TEST_HANG_GUARD_SECS)
            requests = list(server.requests)
            with self.assertRaisesRegex(ValueError, "timed out"):
                _ = herdr.loaded_threads(Path(folder), time.monotonic())
            self.assertEqual(server.requests, requests)


if __name__ == "__main__":
    _ = unittest.main()
