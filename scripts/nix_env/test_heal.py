"""Healing decisions with isolated journals and injected process observations."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import TYPE_CHECKING, cast, override
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import nix_env
from scripts.lib.nix_env_test_support import NixFixture
from scripts.nix_env import heal
if TYPE_CHECKING:
    from scripts.notify import push_texts
else:
    import push_texts
from scripts.message import sessions
from scripts.whoami import account


class HealTests(NixFixture):
    session: heal.HealSession = heal.HealSession("fixture", 811, "start-811", "worker", Path(), "idle", Path())

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.envrc(self.work)
        self.session = heal.HealSession("fixture", 811, "start-811", "worker", self.work, "idle", self.home / ".claude3")
        _ = self.enterContext(patch.object(heal, "STATE_DIR", self.root / "journal"))

    def records(self) -> list[dict[str, object]]:
        path = heal.STATE_DIR / "log.jsonl"
        return [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def test_later_envrc_approval_does_not_repair_the_live_process(self) -> None:
        # The session began before approval. Only a restart changes its own env.
        _ = nix_env.ensure(self.work, self.env)
        real_stat = Path.stat
        real_read = Path.read_bytes
        process = Path(f"/proc/{self.session.pid}")

        def stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            return real_stat(self.work if path == process else path, follow_symlinks=follow_symlinks)

        def read(path: Path) -> bytes:
            return b"DIRENV_DIR=/fixture\0PATH=/bin\0" if path == process / "environ" else real_read(path)

        with (patch.object(sys, "platform", "linux"),
              patch.object(sessions, "process_start", return_value=self.session.proc_start),
              patch.object(Path, "stat", stat), patch.object(Path, "read_bytes", read)):
            self.assertTrue(heal.broken(self.session))
            with patch.object(Path, "read_bytes", return_value=b"IN_NIX_SHELL=impure\0"):
                self.assertFalse(heal.broken(self.session))

    def test_reused_pid_never_counts_as_broken(self) -> None:
        with (patch.object(sessions, "process_start", return_value="different-start"),
              patch.object(Path, "read_bytes", side_effect=AssertionError("reused pid must not be inspected"))):
            self.assertFalse(heal.broken(self.session))
            self.assertIsInstance(heal.inspect(self.session), heal.Gone)

    def test_no_envrc_is_healthy_without_process_or_direnv_probe(self) -> None:
        (self.work / ".envrc").unlink()
        with patch.object(sessions, "process_start", side_effect=AssertionError("no probe needed")):
            self.assertFalse(heal.broken(self.session))
        self.assertEqual(self.direnv_calls(), [])

    def test_registry_reads_all_config_dirs_and_ignores_corrupt_or_reused_entries(self) -> None:
        for number, name in enumerate((".claude", ".claude3"), 1):
            directory = self.home / name / "sessions"
            directory.mkdir(parents=True)
            record = {"pid": 800 + number, "procStart": "fixture-start", "sessionId": f"sid-{number}",
                      "name": f"worker-{number}", "messagingSocketPath": "", "updatedAt": 1,
                      "cwd": str(self.work), "status": "idle", "kind": "interactive", "entrypoint": "cli"}
            _ = (directory / f"{800 + number}.json").write_text(json.dumps(record))
            _ = (directory / "corrupt.json").write_text("{half written")
        with patch.object(sessions, "process_start", return_value="fixture-start"):
            found = heal.list_sessions()
        self.assertEqual({item.session_id for item in found}, {"sid-1", "sid-2"})
        self.assertEqual({item.config_dir.name for item in found}, {".claude", ".claude3"})
        with patch.object(sessions, "process_start", return_value="reused"):
            self.assertEqual(heal.list_sessions(), [])
        self.assertNotIn("NOTIFIER_SESSIONS_DIR", os.environ)

    def test_mac_process_arguments_cannot_pose_as_nix_environment(self) -> None:
        arguments = int(3).to_bytes(4, sys.byteorder, signed=True) + b"/bin/claude\0\0claude\0-p\0IN_NIX_SHELL=spoofed prompt\0"
        with (patch.object(sys, "platform", "darwin"),
              patch.object(sessions, "process_start", return_value=self.session.proc_start),
              patch.object(account, "kern_procargs2", return_value=arguments + b"HOME=/fixture\0") as kernel):
            self.assertTrue(heal.broken(self.session))
            kernel.return_value = arguments + b"IN_NIX_SHELL=impure\0"
            self.assertFalse(heal.broken(self.session))

    def install_sweep(self, selected: list[heal.HealSession]) -> tuple[Mock, Mock, Mock]:
        registry = list(selected)
        healthy: set[tuple[str, int]] = set()

        def observation(session: heal.HealSession) -> heal.Healthy | heal.Broken:
            return heal.Healthy() if (session.session_id, session.pid) in healthy else heal.Broken()

        _ = self.enterContext(patch.object(heal, "list_sessions", side_effect=lambda: list(registry)))
        _ = self.enterContext(patch.object(heal, "inspect", side_effect=observation))
        held = cast(Mock, self.enterContext(patch.object(heal, "hold_reason", return_value="")))
        ensure = cast(Mock, self.enterContext(patch.object(nix_env, "ensure", return_value=nix_env.Loaded({"IN_NIX_SHELL": "impure"}, self.work / ".envrc"))))
        _ = self.enterContext(patch.object(account, "claude_account", return_value=account.Account("claude", "test-account", "claude3")))
        _ = self.enterContext(patch.object(heal, "stop_completion", return_value=heal.StopComplete()))

        def command_result(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            del kwargs
            if command[2] == "restart":
                session_id = command[command.index("--only") + 1]
                original = next(item for item in selected if item.session_id == session_id)
                replacement = replace(original, pid=original.pid + 10000, proc_start="restarted-" + original.proc_start)
                registry[:] = [item for item in registry if item.session_id != session_id] + [replacement]
                healthy.add((replacement.session_id, replacement.pid))
            return subprocess.CompletedProcess(command, 0, "SENT: fixture", "")

        run = cast(Mock, self.enterContext(patch.object(subprocess, "run", side_effect=command_result)))
        return held, ensure, run

    def test_idle_session_restarts_once_and_logs_the_restart(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual(ensure.call_args_list, [call(self.work), call(self.work)])
        self.assertGreaterEqual(held.call_count, 2)
        command = cast(list[str], run.call_args.args[0])
        self.assertEqual(command[2:], ["restart", "test-account", "--only", self.session.session_id, "--here"])
        stop = cast(list[str], run.call_args_list[0].args[0])
        self.assertEqual(stop[2:], ["down", "test-account", "--here", "--only", self.session.session_id])
        self.assertTrue(command[1].endswith("/shutdown/shutdown.py"))
        entries = self.records()
        self.assertEqual([entry["action"] for entry in entries], ["restart"])
        self.assertEqual(entries[0]["session_id"], self.session.session_id)
        self.assertEqual(entries[0]["cwd"], str(self.work))
        self.assertTrue(all(key in entries[0] for key in ("time", "machine", "name", "reason")))
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual(run.call_count, 2)

    def test_hold_and_busy_sessions_do_not_load_or_restart_and_log_reason_changes(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        held.return_value = "build hold: fixture"
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual(heal.sweep(), 0)
        ensure.assert_not_called()
        run.assert_not_called()
        self.assertEqual(len(self.records()), 1)
        held.return_value = ""
        with patch.object(heal, "list_sessions", return_value=[replace(self.session, status="busy")]):
            self.assertEqual(heal.sweep(), 0)
        ensure.assert_not_called()
        run.assert_not_called()
        self.assertEqual([entry["reason"] for entry in self.records()], ["build hold: fixture", "session is not idle"])

    def test_refused_environment_sends_one_blocked_push_and_no_restart(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        held.return_value = ""
        ensure.return_value = nix_env.Refused("nix_env: fixture: no nix shell")
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual(run.call_count, 1)
        command = cast(list[str], run.call_args.args[0])
        self.assertTrue(command[1].endswith("/message/send.py"))
        self.assertEqual(command[command.index("--need") + 1], "blocked")
        self.assertIn("nix-heal-fixture", command)
        self.assertIn("nix_heal.failed", command)
        self.assertIn("--only fixture --check", command[command.index("--clears-when") + 1])
        self.assertEqual([entry["action"] for entry in self.records()], ["refused", "refused"])

    def test_broken_replacement_hits_hour_guard_and_pushes_once(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        held.return_value = ""
        # The command returns but never publishes a healthy replacement.
        def unconfirmed(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            del kwargs
            return subprocess.CompletedProcess(command, 0, "SENT: fixture", "")

        run.side_effect = unconfirmed
        with patch.object(time, "time", return_value=10000):
            self.assertEqual(heal.sweep(), 0)
        replacement = replace(self.session, pid=812, proc_start="replacement")
        with (patch.object(heal, "list_sessions", return_value=[replacement]),
              patch.object(time, "time", return_value=10030)):
            self.assertEqual(heal.sweep(), 0)
            self.assertEqual(heal.sweep(), 0)
        self.assertEqual(ensure.call_args_list, [call(self.work), call(self.work)])
        self.assertEqual(run.call_count, 3)
        commands = [cast(list[str], call.args[0]) for call in run.call_args_list]
        self.assertEqual(sum(command[2] == "restart" for command in commands), 1)
        self.assertEqual(sum("--need" in command for command in commands), 1)
        self.assertFalse(any(entry["action"] == "restart" for entry in self.records()))
        self.assertEqual(heal.read_history()[self.session.session_id]["pending"]["kind"], "launching")
        push = commands[-1]
        values = dict(push[index + 1].split("=", 1) for index, word in enumerate(push) if word == "--value")
        self.assertIn("An automatic restart did not fix it.",
                      push_texts.render("nix_heal.failed", **values).message)
        raw = "session still has no nix shell after its heal restart"
        self.assertEqual(self.records()[-1]["reason"], raw)
        self.assertNotIn(raw, " ".join(push))

    def test_recovery_failure_phone_causes_keep_raw_diagnostics_only_in_the_log(self) -> None:
        cases = (
            ("refused", f"nix_env: {self.work}: .envrc loaded but no nix shell (IN_NIX_SHELL unset)",
             "The automatic restart could not fix it. The .envrc file loaded, but no nix shell started."),
            ("still_broken", "session still has no nix shell after its heal restart",
             "An automatic restart did not fix it."),
            ("other_refusal", "nix_env: direnv: fixture stderr\nraw diagnostic with internal details",
             "The automatic restart could not fix it. The nix shell setup refused to load."),
        )
        for outcome, raw, cause in cases:
            with self.subTest(outcome=outcome), patch.object(heal, "STATE_DIR", self.root / outcome):
                held, ensure, run = self.install_sweep([self.session])
                held.return_value = ""
                if outcome == "still_broken":
                    heal.STATE_DIR.mkdir(parents=True)
                    history = heal.new_history()
                    history["restart_at"] = time.time()
                    history["restart_pid"] = self.session.pid - 1
                    history["restart_proc_start"] = "before-restart"
                    heal.save_history({self.session.session_id: history})
                else:
                    ensure.return_value = nix_env.Refused(raw)
                self.assertEqual(heal.sweep(), 0)
                run.assert_called_once()
                command = cast(list[str], run.call_args.args[0])
                values = dict(command[index + 1].split("=", 1)
                              for index, word in enumerate(command) if word == "--value")
                push = push_texts.render("nix_heal.failed", **values)
                self.assertEqual(push.body,
                    f"The nix shell for {self.session.name}'s worktree did not load, so its builds will fail. "
                    + f"{cause}\n\n"
                    + "The session's worktree changes stay on disk. This alert clears when the session has a nix shell.")
                self.assertEqual(push.title,
                                 f"{self.session.name} builds will fail: no nix shell")
                self.assertIn(
                    f"On {socket.gethostname()}, fix the nix shell in {self.work}, then restart {self.session.name}",
                    push.message)
                self.assertNotIn(raw, " ".join(command))
                self.assertEqual([entry["reason"] for entry in self.records()], [raw])

    def test_hold_acquired_during_environment_load_prevents_restart(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        held.side_effect = ["", "build hold: acquired"]
        self.assertEqual(heal.sweep(), 0)
        ensure.assert_called_once()
        run.assert_not_called()
        self.assertEqual(self.records()[0]["reason"], "build hold: acquired")

    def test_hold_between_stop_and_launch_persists_pending_restart_without_live_registry(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        complete = cast(Callable[..., subprocess.CompletedProcess[str]], run.side_effect)

        def stop_or_launch(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if "down" in command:
                held.return_value = "build hold: acquired after stop"
            return complete(command, **kwargs)

        run.side_effect = stop_or_launch
        self.assertEqual(heal.sweep(), 0)
        commands = [cast(list[str], call.args[0]) for call in run.call_args_list]
        self.assertEqual(len(commands), 1)
        self.assertNotIn("--restart", commands[0])
        self.assertEqual(self.records()[-1]["action"], "pending")
        # The stopped process has left the registry; recovery must use the journal.
        held.return_value = ""
        with patch.object(heal, "list_sessions", return_value=[]) as registry:
            self.assertEqual(heal.sweep(), 0)
            registry.return_value = [replace(self.session, pid=10811, proc_start="restarted-start-811")]
            self.assertEqual(heal.sweep(), 0)
        commands = [cast(list[str], call.args[0]) for call in run.call_args_list]
        self.assertEqual([command[2] for command in commands], ["down", "restart"])
        self.assertEqual(commands[1][3:], ["test-account", "--only", self.session.session_id, "--here"])
        self.assertEqual(self.records()[-1]["action"], "restart")
        self.assertEqual(ensure.call_count, 2)

    def test_async_stop_completion_is_required_before_any_relaunch(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        held.return_value = ""
        with patch.object(heal, "stop_completion", return_value=heal.StopPending()) as completion:
            self.assertEqual(heal.sweep(), 0)
            self.assertEqual(heal.sweep(), 0)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(self.records()[-1]["reason"], "waiting for shutdown stop completion")
            completion.return_value = heal.StopComplete()
            self.assertEqual(heal.sweep(), 0)
        self.assertEqual([cast(list[str], item.args[0])[2] for item in run.call_args_list], ["down", "restart"])
        self.assertEqual(ensure.call_count, 2)

    def test_pending_restart_refusal_alerts_once_and_never_relaunches(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        held.return_value = ""
        ensure.side_effect = [nix_env.NoEnvrc({}), nix_env.Refused("nix_env: changed while stopped"),
                              nix_env.Refused("nix_env: changed while stopped")]
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual(heal.sweep(), 0)
        commands = [cast(list[str], item.args[0]) for item in run.call_args_list]
        self.assertEqual(sum(command[2] == "down" for command in commands), 1)
        self.assertEqual(sum(command[2] == "restart" for command in commands), 0)
        self.assertEqual(sum("--need" in command for command in commands), 1)

    def test_hold_during_pending_environment_load_prevents_the_relaunch(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        loaded = nix_env.Loaded({"IN_NIX_SHELL": "impure", "NIX_LDFLAGS": "-Lfixture"}, self.work / ".envrc")

        def load(directory: Path) -> nix_env.Loaded:
            self.assertEqual(directory, self.work)
            if ensure.call_count == 2:
                held.return_value = "build hold: acquired during final load"
            return loaded

        ensure.side_effect = load
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual(ensure.call_count, 2)
        commands = [cast(list[str], item.args[0]) for item in run.call_args_list]
        self.assertEqual([command[2] for command in commands], ["down"])
        self.assertEqual(heal.read_history()[self.session.session_id]["pending"]["kind"], "stopping")
        held.return_value = ""
        self.assertEqual(heal.sweep(), 0)
        environment = cast(dict[str, str], run.call_args.kwargs["env"])
        self.assertEqual(environment["NIX_LDFLAGS"], "-Lfixture")
        self.assertEqual(environment[nix_env.HEAL_SESSION_ENV], self.session.session_id)
        self.assertEqual(environment[nix_env.HEAL_DIRECTORY_ENV], str(self.work.resolve()))
        self.assertEqual(heal.read_history()[self.session.session_id]["pending"]["kind"], "none")

    def test_relaunch_failure_keeps_pending_and_pushes_once_without_a_duplicate_launch(self) -> None:
        for failure in ("timeout", "nonzero", "oserror"):
            with self.subTest(failure=failure), patch.object(heal, "STATE_DIR", self.root / failure):
                held, ensure, run = self.install_sweep([self.session])
                held.return_value = ""

                def fail_launch(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                    if command[2] == "restart":
                        self.assertEqual(command[3], "test-account")
                        timeout = cast(int, kwargs["timeout"])
                        self.assertGreaterEqual(timeout, nix_env.TIMEOUT_SECONDS + 90)
                        self.assertEqual(heal.read_history()[self.session.session_id]["pending"]["kind"], "launching")
                        if failure == "timeout":
                            raise subprocess.TimeoutExpired(command, timeout)
                        if failure == "oserror":
                            raise OSError("fixture cannot start restart")
                        return subprocess.CompletedProcess(command, 1, "", "fixture launch refused")
                    return subprocess.CompletedProcess(command, 0, "SENT: fixture", "")

                run.side_effect = fail_launch
                self.assertEqual(heal.sweep(), 0)
                self.assertEqual(heal.read_history()[self.session.session_id]["pending"]["kind"], "launching")
                self.assertEqual(heal.sweep(), 0)
                commands = [cast(list[str], item.args[0]) for item in run.call_args_list]
                self.assertEqual(sum(command[2] == "restart" for command in commands), 1)
                self.assertEqual(sum("--need" in command for command in commands), 1)
                self.assertEqual(ensure.call_count, 2)
                # Delayed success after a timeout must clear the reservation,
                # rather than start another copy of the session.
                replacement = replace(self.session, pid=912, proc_start="late-replacement")
                with (patch.object(heal, "list_sessions", return_value=[replacement]),
                      patch.object(heal, "inspect", return_value=heal.Healthy())):
                    self.assertEqual(heal.sweep(), 0)
                self.assertEqual(heal.read_history()[self.session.session_id]["pending"]["kind"], "none")
                self.assertEqual(sum(cast(list[str], item.args[0])[2] == "restart" for item in run.call_args_list), 1)

    def test_successful_restart_command_retains_pending_until_the_replacement_is_healthy(self) -> None:
        held, ensure, run = self.install_sweep([self.session])
        held.return_value = ""

        def success_without_registry(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            del kwargs
            return subprocess.CompletedProcess(command, 0, "", "")

        run.side_effect = success_without_registry
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual(heal.read_history()[self.session.session_id]["pending"]["kind"], "launching")
        replacement = replace(self.session, pid=913, proc_start="confirmed-replacement")
        with (patch.object(heal, "list_sessions", return_value=[replace(replacement, config_dir=self.home / ".claude-wrong")]) as registry,
              patch.object(heal, "inspect", return_value=heal.Healthy())):
            self.assertEqual(heal.sweep(), 0)
            self.assertEqual(heal.read_history()[self.session.session_id]["pending"]["kind"], "launching")
            registry.return_value = [replacement]
            self.assertEqual(heal.sweep(), 0)
        self.assertEqual(heal.read_history()[self.session.session_id]["pending"]["kind"], "none")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(ensure.call_count, 2)
        self.assertEqual(self.records()[-1]["action"], "restart")

    def test_check_requires_a_live_healthy_session_to_clear_alert(self) -> None:
        with patch.object(heal, "list_sessions", return_value=[]):
            self.assertEqual(heal.sweep("missing", check=True), 1)
        with (patch.object(heal, "list_sessions", return_value=[self.session]),
              patch.object(heal, "inspect", return_value=heal.Unavailable("metadata unreadable"))):
            self.assertEqual(heal.sweep("fixture", check=True), 1)
        with (patch.object(heal, "list_sessions", return_value=[self.session]),
              patch.object(heal, "inspect", return_value=heal.Healthy())):
            self.assertEqual(heal.sweep("fixture", check=True), 0)

    def test_one_failed_session_does_not_prevent_next_restart(self) -> None:
        other = replace(self.session, session_id="other", pid=813)
        held, ensure, run = self.install_sweep([self.session, other])
        held.return_value = ""
        ensure.side_effect = [OSError("fixture unreadable"), nix_env.NoEnvrc({}), nix_env.NoEnvrc({})]
        self.assertEqual(heal.sweep(), 0)
        self.assertEqual([entry["action"] for entry in self.records()], ["refused", "restart"])
        self.assertEqual(run.call_count, 2)
        self.assertIn("other", cast(list[str], run.call_args.args[0]))
