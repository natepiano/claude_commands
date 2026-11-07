"""Consultations retain their pane and leave staff membership unchanged."""
from __future__ import annotations

import io
import math
import os
import signal
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Literal, override
from unittest.mock import patch

from scripts.herdr import herdr, pane_launch, side_pane, worker_panes
from scripts.herdr.test_worker_panes import PaneWorld
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id
from scripts.direct import assign
from scripts.production.test_worker import PaneCommandRouter


class SidePaneTests(StaffTestCase):
    world: PaneWorld = PaneWorld()
    openings: list[herdr.OpeningPrompt | herdr.NoOpeningPrompt] = []
    caller_id: str = ""
    launch_interrupt: str = ""
    signals: list[tuple[int, int]] = []
    launch_limits: list[int] = []

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.router: PaneCommandRouter = PaneCommandRouter()

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.write_document({"schema": 2, "rows": []})
        self.world = PaneWorld()
        self.openings = []
        self.signals = self.world.signals
        self.launch_limits = []
        self.launch_interrupt = ""
        self.caller_id = fixture_runtime_id("ad-hoc-caller")
        self.world.setup(self)
        self.router.setup(self, self.root)
        _ = self.enterContext(patch.object(staff, "runtime_id", side_effect=lambda: self.caller_id))
        _ = self.enterContext(patch.object(herdr, "launch_prepared_agent", side_effect=self.launch))
        _ = self.enterContext(patch.object(os, "kill", side_effect=self.world.signal))
        self.caller("claude")

    def caller(self, family: Literal["claude", "codex"], *, member: bool = False) -> None:
        os.environ["HERDR_PANE_ID"] = "w1:p0"
        os.environ["CLAUDE_CODE_SESSION_ID"] = self.caller_id
        if family == "codex":
            os.environ["CODEX_THREAD_ID"] = self.caller_id
        else:
            _ = os.environ.pop("CODEX_THREAD_ID", None)
        if member:
            row = fixture_member("showrunner", name="holder")
            self.caller_id = str(row.current_session)
            self.world.add(row, "w1:p0", family=family)
        else:
            self.world.locations["w1:p0"] = {"pane_id": "w1:p0", "terminal_id": "caller-terminal",
                "tab_id": "w1:t1", "workspace_id": "w1", "label": "caller"}
            self.world.layouts["w1:t1"] = {"type": "pane", "pane_id": "w1:p0"}
            self.world.activate(self.caller_id, "w1:p0", family)

    def launch(self, name: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
               prepared: herdr.PreparedAgentEnvironment, kind: str, agent_arguments: list[str],
               opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt, timeout_ms: int, *, agent: str,
               reported_unknown: str = "") -> herdr.AgentLaunched:
        self.openings.append(opening)
        self.launch_limits.append(timeout_ms)
        result = self.world.launch(name, destination, prepared, kind, agent_arguments, opening, timeout_ms,
                                   agent=agent, reported_unknown=reported_unknown)
        if self.launch_interrupt == "agent":
            self.launch_interrupt = ""
            raise OSError("lost launch acknowledgement")
        return result

    def command(self, *arguments: str) -> tuple[int, str, str]:
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            code = side_pane.main(list(arguments))
        return code, output.getvalue(), errors.getvalue()

    def start(self, label: str = "friend") -> side_pane.OpenSidePane:
        result = side_pane.open(label, assign.pane_policy("friend"), cwd=self.root)
        self.assertIsInstance(result, side_pane.OpenSidePane)
        assert isinstance(result, side_pane.OpenSidePane)
        return result

    def test_codex_side_pane_keeps_the_native_compaction_limit_after_a_restart(self) -> None:
        self.caller("codex")
        first = self.start()
        arguments = self.world.requests[-1][1]
        self.assertFalse(any("model_auto_compact_token_limit" in argument for argument in arguments))
        config = self.root / "conf/direct.conf"
        write_text_atomic(config, config.read_text().replace("UNIT_COMPACT_ABOVE_TOKENS=43000",
                                                             "UNIT_COMPACT_ABOVE_TOKENS=71000"))
        del self.world.foreground[first.pane]
        self.assertEqual(self.start(), first)
        arguments = self.world.requests[-1][1]
        self.assertFalse(any("model_auto_compact_token_limit" in argument for argument in arguments))
        self.assertEqual(len(self.world.launches), 2)

    def test_a_friend_launches_with_the_worker_permissions_for_both_families(self) -> None:
        for family in ("claude", "codex"):
            with self.subTest(family=family):
                self.caller(family)
                opened = side_pane.open("worker-policy-" + family, assign.pane_policy("friend"), cwd=self.root)
                self.assertIsInstance(opened, side_pane.OpenSidePane)
                self.assertEqual(self.world.requests[-1][0], family)
                arguments = self.world.requests[-1][1]
                if family == "codex":
                    self.assertEqual(arguments[arguments.index("--sandbox") + 1], "danger-full-access")
                    self.assertEqual(arguments[arguments.index("--ask-for-approval") + 1], "never")
                else:
                    self.assertEqual(arguments[arguments.index("--permission-mode") + 1], "bypassPermissions")
                self.assertNotIn("read-only", arguments)
                self.assertNotIn("plan", arguments)

    def test_claude_side_pane_ignores_the_codex_compaction_limit(self) -> None:
        config = self.root / "conf/direct.conf"
        write_text_atomic(config, config.read_text().replace("UNIT_COMPACT_ABOVE_TOKENS=43000",
                                                             "UNIT_COMPACT_ABOVE_TOKENS=invalid"))
        _ = self.start()
        family, arguments, _ = self.world.requests[-1]
        self.assertEqual(family, "claude")
        self.assertFalse(any("model_auto_compact_token_limit" in argument for argument in arguments))

    def test_codex_side_pane_ignores_the_between_turn_compaction_limit(self) -> None:
        self.caller("codex")
        config = self.root / "conf/direct.conf"
        original = config.read_text()
        before = staff.snapshot().rows
        for index, value in enumerate(("", "invalid", "0", "-1", "1.5")):
            with self.subTest(value=value):
                write_text_atomic(config, original.replace("UNIT_COMPACT_ABOVE_TOKENS=43000",
                                                           "UNIT_COMPACT_ABOVE_TOKENS=" + value))
                code, output, errors = self.command("open", f"friend-{index}", "--cwd", str(self.root))
                self.assertEqual(code, 0, errors)
                self.assertEqual(output, "done\n")
                self.assertEqual(errors, "")
                family, arguments, _ = self.world.requests[-1]
                self.assertEqual(family, "codex")
                self.assertFalse(any("model_auto_compact_token_limit" in argument for argument in arguments))
                self.assertEqual(len(self.world.launches), index + 1)
                self.assertEqual(staff.snapshot().rows, before)

    def test_friend_question_and_followup_keep_the_caller_family_without_staff(self) -> None:
        before = staff.snapshot().rows
        for family in ("claude", "codex"):
            with self.subTest(family=family):
                self.caller(family)
                label = "friend-" + family
                question = self.root / "question.md"
                message = self.root / "followup.md"
                write_text_atomic(question, "Review this design.")
                write_text_atomic(message, "Explain the tradeoff.")
                code, output, errors = self.command("open", label, "--cwd", str(self.root), "--question-file", str(question))
                self.assertEqual(code, 0, errors)
                address = ("session:" if family == "claude" else "codex:") + self.caller_id
                opening = self.openings[-1]
                self.assertIsInstance(opening, herdr.NoOpeningPrompt)
                prompts = [command for command in self.world.commands if command[:2] == ("agent", "prompt")]
                self.assertIn(question.read_text(), prompts[-1][3])
                self.assertIn("send.py --to " + address, prompts[-1][3])
                self.assertEqual(output, "done\n")
                pane = next(pane for pane, row in self.world.locations.items() if row["label"] == label)
                self.assertEqual(self.world.requests[-1][0], family)
                code, _, errors = self.command("send", label, "--message-file", str(message))
                self.assertEqual(code, 0, errors)
                prompts = [command for command in self.world.commands if command[:2] == ("agent", "prompt")]
                self.assertEqual(prompts[-1][2], pane)
                self.assertIn(message.read_text(), prompts[-1][3])
                self.assertIn("~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to " + address, prompts[-1][3])
                self.assertIn('--text "<answer>"', prompts[-1][3])
                self.assertEqual(question.read_text(), "Review this design.")
                self.assertEqual(self.command("close", label)[0], 0)
        self.assertEqual(staff.snapshot().rows, before)
        self.assertEqual(self.router.worker_commands, [])
        for command in self.router.commands:
            self.assertEqual(command[command.index("--target") + 1], "consultation-pane")
            self.assertEqual(command[command.index("--policy") + 1], "friend")
        self.assertIn("w1:p0", self.world.locations)

    def test_open_sends_the_whole_question_after_start_without_reading_the_composer(self) -> None:
        question = "Review the design.\nExplain the result."
        with patch.object(self.world, "composer", side_effect=AssertionError("composer must not be read")):
            result = side_pane.open("friend", assign.pane_policy("friend"), question=question, cwd=self.root)
        self.assertIsInstance(result, side_pane.OpenSidePane)
        assert isinstance(result, side_pane.OpenSidePane)
        self.assertIsInstance(self.openings[-1], herdr.NoOpeningPrompt)
        self.assertEqual(self.world.locations[result.pane]["terminal_id"], result.terminal)
        self.assertEqual(self.world.commands[-1], ("agent", "prompt", result.pane,
            f'<system-reminder source="script">\n{question}\nReply with ~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to session:{self.caller_id} --text "<answer>".\n</system-reminder>'))

    def test_failed_open_question_send_retains_the_pane_and_returns_uncertain(self) -> None:
        question = self.root / "question.md"
        write_text_atomic(question, "Review the design.")
        run = self.world.run

        def fail_send(*arguments: str, deadline: float = math.inf) -> str:
            if arguments[:2] == ("agent", "prompt"):
                raise herdr.HerdrFailure("question delivery failed")
            return run(*arguments, deadline=deadline)

        with patch.object(self.world, "run", side_effect=fail_send):
            code, _, errors = self.command("open", "friend", "--cwd", str(self.root), "--question-file", str(question))
        self.assertEqual(code, 8)
        self.assertIn("question delivery failed", errors)
        retained = self.start()
        self.assertEqual(len(self.world.launches), 1)
        self.assertIn(retained.pane, self.world.locations)
        self.assertEqual(self.world.closed, [])

    def test_repeated_open_with_a_question_sends_nothing(self) -> None:
        first = side_pane.open("friend", assign.pane_policy("friend"), question="First question.", cwd=self.root)
        commands = self.world.commands.copy()
        second = side_pane.open("friend", assign.pane_policy("friend"), question="Second question.", cwd=self.root)
        self.assertEqual(second, first)
        self.assertEqual(self.world.commands, commands)
        self.assertEqual(len(self.world.launches), 1)

    def test_showrunner_consultation_has_no_worker_row(self) -> None:
        self.caller("claude", member=True)
        before = staff.snapshot().rows
        result = self.start()
        self.assertNotEqual(result.pane, "w1:p0")
        self.assertEqual(staff.snapshot().rows, before)
        self.assertEqual(self.command("close", "friend")[0], 0)
        self.assertEqual(staff.snapshot().rows, before)

    def test_open_uses_the_caller_pane_without_reading_other_processes(self) -> None:
        self.caller("codex")
        self.world.locations["w2:p9"] = {"pane_id": "w2:p9", "terminal_id": "foreign-terminal",
            "tab_id": "w2:t2", "workspace_id": "w2", "label": "other"}
        activity = self.world.pane_activity
        native = self.world.native_pane_process
        reads: list[str] = []

        def read_activity(pane: str) -> herdr.PaneAtShell | herdr.PaneRunning:
            reads.append(pane)
            if pane in ("w1:p0", "w2:p9"):
                raise herdr.HerdrFailure("process read is unavailable")
            return activity(pane)

        def read_native(pane: str, kind: str, timeout: float) -> herdr.NativeAgentProcess:
            reads.append(pane)
            if pane in ("w1:p0", "w2:p9"):
                raise herdr.HerdrFailure("native process read is unavailable")
            return native(pane, kind, timeout)

        with patch.object(self.world, "pane_activity", side_effect=read_activity), patch.object(
                self.world, "native_pane_process", side_effect=read_native):
            result = self.start()
        self.assertEqual(self.world.requests[-1][0], "codex")
        self.assertEqual(set(reads), {result.pane})
        self.assertEqual(len(self.world.launches), 1)
        self.assertEqual(staff.snapshot().rows, ())

    def test_friend_open_rerun_after_split(self) -> None:
        self.world.interrupt = "split"
        self.assertEqual(self.command("open", "friend", "--cwd", str(self.root))[0], 8)
        shell = next(value for value in self.world.locations.values() if value["label"] == "friend")
        result = self.start()
        self.assertEqual((result.pane, result.terminal), (shell["pane_id"], shell["terminal_id"]))
        self.assertIsInstance(self.world.launches[-1], herdr.ExistingPane)
        self.assertEqual(sum(isinstance(value, herdr.SplitPane) for value in self.world.launches), 1)
        self.assertEqual(self.world.closed, [])
        self.assertEqual(self.start(), result)
        self.assertEqual(len(self.world.launches), 2)

    def test_open_rerun_after_agent_start_keeps_the_terminal(self) -> None:
        self.launch_interrupt = "agent"
        self.assertEqual(self.command("open", "friend", "--cwd", str(self.root))[0], 8)
        result = self.start()
        self.assertEqual(len(self.world.launches), 1)
        self.assertEqual(self.start(), result)
        self.assertEqual(self.world.closed, [])

    def test_friend_end_rerun_after_close(self) -> None:
        result = self.start()
        pid = self.world.foreground[result.pane][0]
        self.assertEqual(self.command("close", "friend"), (0, "done\n", ""))
        self.assertEqual(self.command("close", "friend"), (0, "done\n", ""))
        self.assertEqual(self.world.closed, [(result.pane, result.terminal)])
        self.assertEqual(self.signals, [(pid, signal.SIGTERM)])
        self.assertEqual(list(self.world.locations), ["w1:p0"])

    def test_duplicate_labels_and_missing_terminals_preserve_all_panes(self) -> None:
        result = self.start()
        self.world.locations[result.pane]["terminal_id"] = ""
        code, _, errors = self.command("close", "friend")
        self.assertEqual(code, 2)
        self.assertIn("terminal identity", errors)
        self.world.locations[result.pane]["terminal_id"] = result.terminal
        self.world.locations["w1:p8"] = {**self.world.locations[result.pane], "pane_id": "w1:p8", "terminal_id": "other"}
        code, _, errors = self.command("close", "friend")
        self.assertEqual(code, 2)
        self.assertIn("duplicate", errors)
        self.assertEqual(self.world.closed, [])
        self.assertEqual(self.world.stops, [])

    def test_unknown_process_preserves_its_pane_and_other_consultations_continue(self) -> None:
        first = self.start("unknown")
        self.world.unknown[first.pane] = "process reader unavailable"
        code, _, errors = self.command("close", "unknown")
        self.assertEqual(code, 8)
        self.assertEqual(len(errors.splitlines()), 1, errors)
        second = self.start("known")
        self.assertEqual(self.command("close", "known")[0], 0)
        self.assertIn(first.pane, self.world.locations)
        self.assertEqual(self.world.closed, [(second.pane, second.terminal)])

    def test_close_retains_a_terminal_that_moves_after_the_stop(self) -> None:
        result = self.start()
        send_signal = self.world.signal

        def move(pid: int, number: int) -> None:
            send_signal(pid, number)
            self.world.locations["w2:p9"] = {**self.world.locations.pop(result.pane), "pane_id": "w2:p9"}

        with patch.object(os, "kill", side_effect=move):
            code, _, errors = self.command("close", "friend")
        self.assertEqual(code, 8)
        self.assertIn("terminal moved", errors)
        self.assertEqual(self.world.closed, [])
        self.assertIn("w1:p0", self.world.locations)

    def test_send_uses_the_same_terminal_without_a_native_identity(self) -> None:
        result = self.start()
        with patch.object(self.world, "native_session", side_effect=AssertionError("no native identity")), patch.object(
                self.world, "native_pane_process", side_effect=AssertionError("no native process")):
            current = side_pane.send("friend", "Use the same terminal.")
        self.assertEqual(current, result)
        self.assertIn("Use the same terminal.", self.world.commands[-1][3])
        self.assertEqual(self.world.stops, [])

    def test_friend_prompt_refuses_a_terminal_that_changes_before_submission(self) -> None:
        result = self.start()
        message = self.root / "message.md"
        write_text_atomic(message, "Review the second question.")
        submit = self.world.prompt_terminal
        before = self.world.commands.copy()

        def changed_terminal(pane: str, terminal: str, text: str) -> None:
            self.world.locations[pane]["terminal_id"] = "replacement-terminal"
            submit(pane, terminal, text)

        with patch.object(self.world, "prompt_terminal", side_effect=changed_terminal):
            code, output, errors = self.command("send", "friend", "--message-file", str(message))
        self.assertEqual(code, worker_panes.ExitCode.UNCERTAIN)
        self.assertEqual(output, "")
        self.assertEqual(len(errors.splitlines()), 1, errors)
        self.assertIn("terminal", errors)
        self.assertEqual(self.world.commands, before)
        self.assertIn(result.pane, self.world.locations)
        self.assertEqual(self.world.closed, [])

    def test_close_rerun_after_agent_stop_keeps_the_same_terminal(self) -> None:
        result = self.start()
        send_signal = self.world.signal

        pid = self.world.foreground[result.pane][0]

        def interrupted(agent_pid: int, number: int) -> None:
            send_signal(agent_pid, number)
            raise OSError("interrupted after agent stop")

        with patch.object(os, "kill", side_effect=interrupted):
            self.assertEqual(self.command("close", "friend")[0], 8)
        self.assertEqual(self.world.closed, [])
        self.assertEqual(self.command("close", "friend")[0], 0)
        self.assertEqual(self.world.closed, [(result.pane, result.terminal)])
        self.assertEqual(self.signals, [(pid, signal.SIGTERM)])

    def test_a_busy_terminal_after_the_stop_keeps_its_pane(self) -> None:
        result = self.start()
        send_signal = self.world.signal

        pid = self.world.foreground[result.pane][0]

        def busy(agent_pid: int, number: int) -> None:
            send_signal(agent_pid, number)
            self.world.activate(fixture_runtime_id("other-work"), result.pane, "claude")

        write_text_atomic(self.root / "conf/shutdown.conf", "# range: 0..300\nSTOP_GRACE_SECONDS=0\n")
        with patch.object(os, "kill", side_effect=busy):
            code, _, errors = self.command("close", "friend")
        self.assertEqual(code, 8)
        self.assertIn("agent has not ended", errors)
        self.assertEqual(self.world.closed, [])
        self.assertEqual(self.signals, [(pid, signal.SIGTERM)])

    def test_close_preserves_a_same_label_in_another_tab(self) -> None:
        selected = self.start()
        pid = self.world.foreground[selected.pane][0]
        self.world.locations["w2:p9"] = {"pane_id": "w2:p9", "terminal_id": "foreign-terminal",
            "tab_id": "w2:t2", "workspace_id": "w2", "label": "friend"}
        self.world.layouts["w2:t2"] = {"type": "pane", "pane_id": "w2:p9"}
        self.world.activate(fixture_runtime_id("foreign"), "w2:p9", "claude")
        self.assertEqual(self.command("close", "friend")[0], 0)
        self.assertEqual(self.world.closed, [(selected.pane, selected.terminal)])
        self.assertEqual(self.signals, [(pid, signal.SIGTERM)])
        self.assertIn("w2:p9", self.world.locations)
        self.assertIn("w1:p0", self.world.locations)

    def test_private_configuration_sets_the_split_and_a_dev_shell_environment(self) -> None:
        write_text_atomic(self.root / "conf/direct.conf", "# range: 0.1..0.9\nWORKER_SPLIT_RATIO=0.4\n# range: 1..600\nPANE_AGENT_START_SECONDS=125\n")
        environment = {"PRIVATE_DEV_SHELL": "loaded"}
        from scripts.lib import nix_env

        def loaded(directory: Path, base_env: dict[str, str]) -> nix_env.Loaded:
            return nix_env.Loaded({**base_env, **environment}, directory / ".envrc")

        with patch.object(nix_env, "ensure", side_effect=loaded):
            result = self.start()
        destination = self.world.launches[-1]
        assert isinstance(destination, herdr.SplitPane)
        self.assertEqual((destination.of_pane, destination.ratio), ("w1:p0", 0.4))
        self.assertEqual(self.world.requests[-1][2]["PRIVATE_DEV_SHELL"], "loaded")
        self.assertEqual(self.launch_limits[-1], 125000)
        self.assertNotIn("STAFF_ID", self.world.requests[-1][2])
        self.assertEqual(self.start(), result)

    def test_codex_record_retires_only_after_a_confirmed_close(self) -> None:
        self.caller("codex")
        result = self.start()
        path = pane_launch.record_directory() / "agents.json"
        entry: pane_launch.PaneLaunch = {"kind": "codex", "pane_id": result.pane,
            "session_id": self.world.processes[self.world.foreground[result.pane][0]].identity.id, "config_dir": str(self.root / "codex"),
            "cwd": str(self.root), "name": "friend", "model": "fixture", "environment": {},
            "remote_control": False, "flags_read": True, "written_at": "fixture-generation"}
        pane_launch.write(path, entry)
        run = self.world.run

        def fail_close(*arguments: str, deadline: float = math.inf) -> str:
            if arguments[:2] == ("pane", "close"):
                raise herdr.HerdrFailure("closure failed")
            return run(*arguments, deadline=deadline)

        with patch.object(self.world, "run", side_effect=fail_close):
            closed = side_pane.close("friend")
            self.assertIsInstance(closed, side_pane.SidePaneCloseUnavailable)
            assert isinstance(closed, side_pane.SidePaneCloseUnavailable)
            self.assertIn("close", closed.reason)
            self.assertEqual(self.command("close", "friend")[0], 8)
        self.assertEqual(pane_launch.read_valid_entries(path)[result.pane], entry)
        self.assertIn(result.pane, self.world.locations)
        self.assertEqual(self.command("close", "friend")[0], 0)
        self.assertEqual(pane_launch.read_valid_entries(path), {})
        self.assertEqual(self.world.closed, [(result.pane, result.terminal)])

    def test_open_needs_no_native_identity_for_either_friend_family(self) -> None:
        for family in ("claude", "codex"):
            with self.subTest(family=family):
                self.caller(family)
                with patch.object(self.world, "native_session", side_effect=AssertionError("no native identity")), patch.object(
                        self.world, "native_pane_process", side_effect=AssertionError("no native process")):
                    code, output, errors = self.command("open", family, "--cwd", str(self.root))
                self.assertEqual(code, 0, errors)
                self.assertEqual(output, "done\n")
                result = self.start(family)
                self.assertEqual(result.pane, next(
                    pane for pane, value in self.world.locations.items() if value["label"] == family))
                self.assertEqual(result.terminal, "terminal-" + result.pane)
                self.assertEqual(self.world.locations[result.pane]["label"], family)
                self.assertEqual(self.launch_limits[-1], 90000)

    def test_close_signals_only_its_pane_agent_without_a_native_identity(self) -> None:
        result = self.start()
        pid = self.world.foreground[result.pane][0]
        caller_process = self.world.foreground["w1:p0"]
        with patch.object(self.world, "native_session", side_effect=AssertionError("no native identity")), patch.object(
                self.world, "native_pane_process", side_effect=AssertionError("no native process")):
            self.assertEqual(self.command("close", "friend"), (0, "done\n", ""))
        self.assertEqual(self.signals, [(pid, signal.SIGTERM)])
        self.assertEqual(self.world.closed, [(result.pane, result.terminal)])
        self.assertEqual(self.world.foreground["w1:p0"], caller_process)

    def test_close_rerun_signals_an_agent_that_has_not_ended_again(self) -> None:
        result = self.start()
        pid = self.world.foreground[result.pane][0]

        def retained(agent_pid: int, number: int) -> None:
            self.signals.append((agent_pid, number))

        write_text_atomic(self.root / "conf/shutdown.conf", "# range: 0..300\nSTOP_GRACE_SECONDS=0\n")
        with patch.object(os, "kill", side_effect=retained):
            code, _, errors = self.command("close", "friend")
        self.assertEqual(code, 8)
        self.assertIn("agent has not ended", errors)
        self.assertEqual(self.world.closed, [])
        self.assertEqual(self.command("close", "friend"), (0, "done\n", ""))
        self.assertEqual(self.signals, [(pid, signal.SIGTERM), (pid, signal.SIGTERM)])
        self.assertEqual(self.world.closed, [(result.pane, result.terminal)])

    def test_a_caller_label_never_selects_the_caller_as_the_friend(self) -> None:
        self.world.locations["w1:p0"]["label"] = "friend"
        caller_process = self.world.foreground["w1:p0"]
        self.assertEqual(self.command("close", "friend"), (0, "done\n", ""))
        self.assertEqual(self.signals, [])
        friend = self.start()
        self.assertNotEqual(friend.pane, "w1:p0")
        self.assertEqual(self.start(), friend)
        self.assertEqual(self.command("close", "friend"), (0, "done\n", ""))
        self.assertEqual(self.world.foreground["w1:p0"], caller_process)
        self.assertEqual(self.world.closed, [(friend.pane, friend.terminal)])

    def test_close_retains_a_replaced_terminal_and_a_reused_agent_process(self) -> None:
        friend = self.start()
        pid = self.world.foreground[friend.pane][0]
        read = self.world.result

        def replace_terminal(*arguments: str) -> dict[str, object]:
            result = read(*arguments)
            if arguments[:2] == ("pane", "process-info"):
                self.world.locations[friend.pane]["terminal_id"] = "replacement-terminal"
            return result

        with patch.object(self.world, "result", side_effect=replace_terminal):
            code, _, errors = self.command("close", "friend")
        self.assertEqual(code, 8)
        self.assertIn("terminal changed", errors)
        self.assertEqual(self.signals, [])
        self.world.locations[friend.pane]["terminal_id"] = friend.terminal
        check = herdr.check_target

        def reused(target: str | herdr.AgentProcessIdentity, *, action: Literal["signal", "keys", "close"]) -> bool:
            write_text_atomic(self.root / "proc" / str(pid) / "stat", f"{pid} (fixture) S " + "0 " * 18 + "2\n")
            return check(target, action=action)

        with patch.object(herdr, "check_target", side_effect=reused):
            code, _, errors = self.command("close", "friend")
        self.assertEqual(code, 8)
        self.assertIn("reused process", errors)
        self.assertEqual(self.signals, [])
        self.assertEqual(self.world.closed, [])
        self.assertIn(friend.pane, self.world.locations)

    def test_an_empty_codex_thread_uses_claude_without_reading_staff(self) -> None:
        os.environ["CODEX_THREAD_ID"] = ""
        with patch.object(staff, "snapshot", side_effect=AssertionError("staff must not be read")):
            friend = self.start()
            self.assertEqual(self.world.requests[-1][0], "claude")
            delivered = side_pane.send("friend", "Explain the result.")
            self.assertEqual(delivered, friend)
            self.assertIn("--to session:" + self.caller_id, self.world.commands[-1][3])
            self.assertEqual(self.command("close", "friend"), (0, "done\n", ""))
        self.assertEqual(self.world.closed, [(friend.pane, friend.terminal)])

    def test_open_uses_the_configured_ratio_range_and_reads_configuration_once(self) -> None:
        write_text_atomic(self.root / "conf/direct.conf",
            "# range: 0..1\nWORKER_SPLIT_RATIO=0.95\n# range: 1..600\nPANE_AGENT_START_SECONDS=90\n")
        read = conf.read
        with patch.object(conf, "read", wraps=read) as readings:
            friend = self.start()
        self.assertEqual([entry.args for entry in readings.call_args_list], [("direct",)])
        destination = self.world.launches[-1]
        assert isinstance(destination, herdr.SplitPane)
        self.assertEqual(destination.ratio, 0.95)
        self.assertEqual(self.world.locations[friend.pane]["terminal_id"], friend.terminal)

    def test_close_retires_a_claude_launch_only_after_confirmed_closure(self) -> None:
        friend = self.start()
        path = pane_launch.record_directory() / "agents.json"
        entry: pane_launch.PaneLaunch = {"kind": "claude", "pane_id": friend.pane,
            "session_id": self.world.processes[self.world.foreground[friend.pane][0]].current_identity,
            "config_dir": str(self.root / "claude"), "cwd": str(self.root), "name": "friend",
            "model": "fixture", "environment": {}, "remote_control": False,
            "flags_read": True, "written_at": "fixture-generation"}
        pane_launch.write(path, entry)
        self.assertEqual(self.command("close", "friend"), (0, "done\n", ""))
        self.assertEqual(pane_launch.read_valid_entries(path), {})
        self.assertEqual(self.world.closed, [(friend.pane, friend.terminal)])
