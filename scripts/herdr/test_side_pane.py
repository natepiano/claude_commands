"""Consultations retain their pane and leave staff membership unchanged."""
from __future__ import annotations

import io
import json
import math
import os
import runpy
import signal
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import Callable, Literal, cast, override
from unittest.mock import patch

from scripts.herdr import herdr, pane_launch, side_pane, worker_panes
from scripts.herdr import herdr_types
from scripts.herdr.test_worker_panes import PaneWorld
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id
from scripts.direct import assign
from scripts.agents import pane_record


class SidePaneTests(StaffTestCase):
    world: PaneWorld = PaneWorld()
    openings: list[herdr.OpeningPrompt | herdr.NoOpeningPrompt] = []
    caller_id: str = ""
    launch_interrupt: str = ""
    signals: list[tuple[int, int]] = []
    launch_limits: list[int] = []

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.write_document({"schema": 2, "rows": []})
        self.world = PaneWorld()
        self.world.publish_claude_start = False
        self.openings = []
        self.signals = self.world.signals
        self.launch_limits = []
        self.launch_interrupt = ""
        self.caller_id = fixture_runtime_id("ad-hoc-caller")
        self.world.setup(self)
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
        if kind == "claude":
            pid = self.world.foreground[result.pane][0]
            process = self.world.processes[pid]
            path = pane_launch.record_directory() / f"{name}.json"
            entry: pane_launch.PaneLaunch = {"kind": "claude", "pane_id": result.pane,
                "session_id": process.current_identity, "pid": pid, "proc_start": process.started,
                "config_dir": prepared.env.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude"),
                "cwd": str(prepared.directory),
                "name": agent, "model": "fixture", "remote_control": False,
                "environment": {}, "flags_read": True, "written_at": process.started}
            pane_launch.save(path, {**pane_launch.read(path), result.pane: entry}, record=staff.StaffSnapshot(()))
        if self.launch_interrupt == "agent":
            self.launch_interrupt = ""
            raise OSError("lost launch acknowledgement")
        return result

    def command(self, *arguments: str) -> tuple[int, str, str]:
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            code = side_pane.main(list(arguments))
        return code, output.getvalue(), errors.getvalue()

    def policy(self) -> worker_panes.LaunchPolicy:
        settings = assign.pane_policy("astra-reviewer").settings
        family: Literal["claude", "codex"] = "codex" if os.environ.get("CODEX_THREAD_ID") else "claude"
        settings = settings._replace(kind=family, model="opus" if family == "claude" else settings.model)
        return worker_panes.worker_policy(settings)

    def start(self, label: str = "consultation") -> side_pane.OpenSidePane:
        result = side_pane.open(label, self.policy(), cwd=self.root)
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

    def test_a_consultation_launches_with_the_worker_permissions_for_both_families(self) -> None:
        with self.subTest(publication="Claude SessionStart arguments"):
            case = SidePaneTests()
            case.setUp()
            try:
                namespace = runpy.run_path(str(Path(__file__).parents[1] / "hooks/session-start-pane-record.py"))
                record_start = cast(Callable[[], None], namespace["main"])

                def launch(name: str, destination: herdr.NewTab | herdr.SplitPane | herdr.ExistingPane,
                           prepared: herdr.PreparedAgentEnvironment, kind: str, arguments: list[str],
                           opening: herdr.OpeningPrompt | herdr.NoOpeningPrompt, timeout_ms: int, *, agent: str,
                           reported_unknown: str = "") -> herdr.AgentLaunched:
                    result = case.world.launch(name, destination, prepared, kind, arguments, opening, timeout_ms,
                                               agent=agent, reported_unknown=reported_unknown)
                    pid = case.world.foreground[result.pane][0]
                    identity = case.world.processes[pid].current_identity
                    with (patch.dict(os.environ, {"HERDR_ENV": "1", "HERDR_SESSION": name,
                              "HERDR_PANE_ID": result.pane}),
                          patch.object(sys, "stdin", io.StringIO(json.dumps({
                              "source": "startup", "session_id": identity, "cwd": str(prepared.directory)}))),
                          patch.object(pane_record, "RECORD_DIR", pane_launch.record_directory()),
                          patch.object(pane_record, "claude_arguments", return_value=["claude", *arguments]),
                          patch.object(os, "getppid", return_value=pid),
                          patch.object(staff, "runtime_id", return_value=identity)):
                        record_start()
                    return result

                with patch.object(herdr, "launch_prepared_agent", side_effect=launch):
                    result = side_pane.open("native-argument-name", case.policy(), cwd=case.root)
                entry = next(iter(pane_launch.read(pane_launch.record_directory() / "agents.json").values()))
                self.assertEqual(entry.get("name"), "native-argument-name")
                self.assertIsInstance(result, side_pane.OpenSidePane)
            finally:
                case.doCleanups()

        for shown in (None, "old-helper-title"):
            with self.subTest(helper_title=shown):
                case = SidePaneTests()
                case.setUp()
                try:
                    opened = case.start("native-helper")
                    case.world.locations[opened.pane]["label"] = shown
                    foreign = fixture_member("showrunner", name="other-helper")
                    case.world.add(foreign, "w1:p8", label="native-helper")
                    before = len(case.world.commands)
                    self.assertEqual(side_pane.send("native-helper", "Read the native helper."), opened)
                    self.assertEqual(case.world.commands[before:][0][2], opened.pane)
                    self.assertEqual(case.world.closed, [])
                finally:
                    case.doCleanups()

        for family in ("claude", "codex"):
            with self.subTest(family=family):
                self.caller(family)
                opened = side_pane.open("worker-policy-" + family, self.policy(), cwd=self.root)
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

        for family in ("claude", "codex"):
            for conflict in ("launch session", "staffed native owner", "staffed shell"):
                with self.subTest(family=family, conflict=conflict):
                    case = SidePaneTests()
                    case.setUp()
                    try:
                        case.caller(family)
                        label = "worker-policy-" + family
                        case.world.locations["w1:p8"] = {"pane_id": "w1:p8", "terminal_id": "foreign-terminal",
                            "tab_id": "w1:t1", "workspace_id": "w1", "label": label}
                        case.world.layouts["w1:t1"] = {"type": "split", "direction": "right", "ratio": 0.5,
                            "first": {"type": "pane", "pane_id": "w1:p0"},
                            "second": {"type": "pane", "pane_id": "w1:p8"}}
                        foreign = fixture_runtime_id("foreign-consultation")
                        case.world.activate(foreign, "w1:p8", family)
                        if conflict == "staffed native owner":
                            _ = fixture_member("showrunner", name="other", runtime_id=foreign)
                        elif conflict == "staffed shell":
                            owner = fixture_member("showrunner", name="other", runtime_id=foreign)
                            entry = pane_launch.launch("w1:p8", owner.id,
                                str(case.root), {}, [family], kind=family)
                            pane_launch.write(pane_launch.record_directory() / "agents.json", entry)
                            del case.world.foreground["w1:p8"]
                        else:
                            entry = pane_launch.launch("w1:p8", fixture_runtime_id("old-consultation"),
                                str(case.root), {}, [family], kind=family)
                            pane_launch.write(pane_launch.record_directory() / "agents.json", entry)
                        before = len(case.world.commands)
                        self.assertIsInstance(side_pane.send(label, "Do not send to the other process."),
                                              side_pane.AbsentSidePane)
                        self.assertEqual(case.world.commands[before:], [])
                        self.assertIsInstance(side_pane.close(label), side_pane.ClosedSidePane)
                        self.assertEqual(case.signals, [])
                        self.assertIn("w1:p8", case.world.locations)
                        opened = side_pane.open(label, case.policy(), cwd=case.root)
                        self.assertIsInstance(opened, side_pane.OpenSidePane)
                        assert isinstance(opened, side_pane.OpenSidePane)
                        self.assertNotEqual(opened.pane, "w1:p8")
                        self.assertEqual(case.world.requests[-1][0], family)
                    finally:
                        case.doCleanups()

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
                _ = self.start(f"consultation-{index}")
                family, arguments, _ = self.world.requests[-1]
                self.assertEqual(family, "codex")
                self.assertFalse(any("model_auto_compact_token_limit" in argument for argument in arguments))
                self.assertEqual(len(self.world.launches), index + 1)
                self.assertEqual(staff.snapshot().rows, before)

    def test_consultation_question_and_followup_keep_the_caller_family_without_staff(self) -> None:
        before = staff.snapshot().rows
        for family in ("claude", "codex"):
            with self.subTest(family=family):
                self.caller(family)
                label = "consultation-" + family
                question = self.root / "question.md"
                message = self.root / "followup.md"
                write_text_atomic(question, "Review this design.")
                write_text_atomic(message, "Explain the tradeoff.")
                opened = side_pane.open(label, self.policy(), question=question.read_text(), cwd=self.root)
                self.assertIsInstance(opened, side_pane.OpenSidePane)
                address = ("session:" if family == "claude" else "codex:") + self.caller_id
                opening = self.openings[-1]
                self.assertIsInstance(opening, herdr.NoOpeningPrompt)
                prompts = [command for command in self.world.commands if command[:2] == ("agent", "prompt")]
                self.assertIn(question.read_text(), prompts[-1][3])
                self.assertIn("send.py --to " + address, prompts[-1][3])
                pane = next(pane for pane, row in self.world.locations.items() if row["label"] == label)
                if family == "codex":
                    with self.subTest(transition="first prompt proves the thread"):
                        saved = pane_launch.read(pane_launch.record_directory() / "agents.json").get(pane)
                        self.assertEqual(saved.get("name") if saved is not None else None, label)
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
        self.assertIn("w1:p0", self.world.locations)

    def test_codex_question_keeps_a_delayed_thread_and_replaces_a_stopped_launch(self) -> None:
        self.caller("codex")
        self.world.report_reads_before_publish = 2
        first = side_pane.open("delayed-consultation", self.policy(), question="Review.", cwd=self.root)
        self.assertIsInstance(first, side_pane.OpenSidePane)
        assert isinstance(first, side_pane.OpenSidePane)
        saved = pane_launch.read(pane_launch.record_directory() / "agents.json").get(first.pane)
        self.assertIsNotNone(saved)
        self.assertEqual(side_pane.send("delayed-consultation", "Continue."), first)
        self.world.end_agent(first.pane)
        _ = self.world.locations[first.pane].pop("agent_session", None)
        self.world.report_reads_before_publish = 2
        activate = self.world.activate
        def restarted(identity: str, pane: str, family: Literal["claude", "codex"]) -> None:
            del identity
            activate(fixture_runtime_id("restarted-delayed-consultation"), pane, family)
        with patch.object(self.world, "activate", side_effect=restarted):
            second = side_pane.open("delayed-consultation", self.policy(), question="Review again.", cwd=self.root)
        self.assertEqual(second, first)
        replacement = pane_launch.read(pane_launch.record_directory() / "agents.json")[first.pane]
        self.assertEqual(replacement.get("session_id"), fixture_runtime_id("restarted-delayed-consultation"))
        self.assertEqual(side_pane.send("delayed-consultation", "Continue again."), first)
        prompts = [command[3] for command in self.world.commands if command[:3] == ("agent", "prompt", first.pane)]
        self.assertTrue(any("Review again." in text for text in prompts))
        self.assertEqual(len(self.world.launches), 2)

    def test_open_sends_the_whole_question_after_start_without_reading_the_composer(self) -> None:
        question = "Review the design.\nExplain the result."
        with patch.object(self.world, "composer", side_effect=AssertionError("composer must not be read")):
            result = side_pane.open("consultation", self.policy(), question=question, cwd=self.root)
        self.assertIsInstance(result, side_pane.OpenSidePane)
        assert isinstance(result, side_pane.OpenSidePane)
        self.assertIsInstance(self.openings[-1], herdr.NoOpeningPrompt)
        self.assertEqual(self.world.locations[result.pane]["terminal_id"], result.terminal)
        self.assertEqual(self.world.commands[-1], ("agent", "prompt", result.pane,
            f'<system-reminder source="script">\n{question}\nReply with ~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to session:{self.caller_id} --text "<answer>".\n</system-reminder>'))

    def test_failed_open_question_send_retains_the_pane_and_raises_delivery_failure(self) -> None:
        question = self.root / "question.md"
        write_text_atomic(question, "Review the design.")
        run = self.world.run

        def fail_send(*arguments: str, deadline: float = math.inf) -> str:
            if arguments[:2] == ("agent", "prompt"):
                raise herdr_types.HerdrFailure("question delivery failed")
            return run(*arguments, deadline=deadline)

        with patch.object(self.world, "run", side_effect=fail_send):
            with self.assertRaisesRegex(herdr_types.HerdrFailure, "question delivery failed"):
                _ = side_pane.open("consultation", self.policy(), question=question.read_text(), cwd=self.root)
        retained = self.start()
        self.assertEqual(len(self.world.launches), 1)
        self.assertIn(retained.pane, self.world.locations)
        self.assertEqual(self.world.closed, [])

    def test_repeated_open_with_a_question_sends_nothing(self) -> None:
        first = side_pane.open("consultation", self.policy(), question="First question.", cwd=self.root)
        commands = self.world.commands.copy()
        second = side_pane.open("consultation", self.policy(), question="Second question.", cwd=self.root)
        self.assertEqual(second, first)
        self.assertEqual(self.world.commands, commands)
        self.assertEqual(len(self.world.launches), 1)

    def test_showrunner_consultation_has_no_worker_row(self) -> None:
        self.caller("claude", member=True)
        before = staff.snapshot().rows
        result = self.start()
        self.assertNotEqual(result.pane, "w1:p0")
        self.assertEqual(staff.snapshot().rows, before)
        self.assertEqual(self.command("close", "consultation")[0], 0)
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
                raise herdr_types.HerdrFailure("process read is unavailable")
            return activity(pane)

        def read_native(pane: str, kind: str, timeout: float) -> herdr.NativeAgentProcess:
            reads.append(pane)
            if pane in ("w1:p0", "w2:p9"):
                raise herdr_types.HerdrFailure("native process read is unavailable")
            return native(pane, kind, timeout)

        with patch.object(self.world, "pane_activity", side_effect=read_activity), patch.object(
                self.world, "native_pane_process", side_effect=read_native):
            result = self.start()
        self.assertEqual(self.world.requests[-1][0], "codex")
        self.assertEqual(set(reads), {result.pane})
        self.assertEqual(len(self.world.launches), 1)
        self.assertEqual(staff.snapshot().rows, ())

    def test_consultation_open_rerun_after_split(self) -> None:
        self.world.interrupt = "split"
        with self.assertRaises(OSError):
            _ = self.start()
        shell = next(value for value in self.world.locations.values() if value["label"] == "consultation")
        result = self.start()
        self.assertEqual((result.pane, result.terminal), (shell["pane_id"], shell["terminal_id"]))
        self.assertIsInstance(self.world.launches[-1], herdr.ExistingPane)
        self.assertEqual(sum(isinstance(value, herdr.SplitPane) for value in self.world.launches), 1)
        self.assertEqual(self.world.closed, [])
        self.assertEqual(self.start(), result)
        self.assertEqual(len(self.world.launches), 2)

    def test_open_rerun_after_agent_start_keeps_the_terminal(self) -> None:
        self.launch_interrupt = "agent"
        with self.assertRaises(OSError):
            _ = self.start()
        result = self.start()
        self.assertEqual(len(self.world.launches), 1)
        self.assertEqual(self.start(), result)
        self.assertEqual(self.world.closed, [])

    def test_consultation_end_rerun_after_close(self) -> None:
        result = self.start()
        pid = self.world.foreground[result.pane][0]
        self.assertEqual(self.command("close", "consultation"), (0, "done\n", ""))
        self.assertEqual(self.command("close", "consultation"), (0, "done\n", ""))
        self.assertEqual(self.world.closed, [(result.pane, result.terminal)])
        self.assertEqual(self.signals, [(pid, signal.SIGTERM)])
        self.assertEqual(list(self.world.locations), ["w1:p0"])

    def test_duplicate_labels_and_missing_terminals_preserve_all_panes(self) -> None:
        result = self.start()
        self.world.locations[result.pane]["terminal_id"] = ""
        code, _, errors = self.command("close", "consultation")
        self.assertEqual(code, 2)
        self.assertIn("terminal identity", errors)
        self.world.locations[result.pane]["terminal_id"] = result.terminal
        self.world.locations["w1:p8"] = {**self.world.locations[result.pane], "pane_id": "w1:p8", "terminal_id": "other"}
        code, _, errors = self.command("close", "consultation")
        self.assertEqual(code, 2)
        self.assertIn("several panes match", errors)
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

        with self.subTest(evidence="two Claude processes without a launch record"):
            case = SidePaneTests()
            case.setUp()
            try:
                first = case.start("unknown")
                path = pane_launch.record_directory() / "agents.json"
                pane_launch.save(path, {}, record=staff.StaffSnapshot(()))
                processes = [case.world.foreground[first.pane]]
                case.world.activate(fixture_runtime_id("other-consultation"), first.pane, "claude")
                processes.append(case.world.foreground[first.pane])
                read = case.world.result

                def ambiguous(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
                    result = read(*arguments, deadline=deadline)
                    if arguments == ("pane", "process-info", "--pane", first.pane):
                        info = herdr.json_object(result["process_info"], "fixture processes")
                        return {"process_info": {**info, "foreground_processes": [
                            {"pid": pid, "name": name, "argv": argv} for pid, name, argv in processes]}}
                    return result

                before = case.world.commands.copy()
                with (patch.object(case.world, "result", side_effect=ambiguous),
                      redirect_stderr(io.StringIO()) as errors):
                    self.assertIsInstance(side_pane.send("unknown", "Keep this prompt private."),
                                          side_pane.UnknownSidePane)
                self.assertEqual(case.world.commands, before)
                self.assertEqual(errors.getvalue().splitlines(), [
                    f"herdr agents:{first.pane}: expected one foreground claude process, found 2; pane retained"])
                self.assertIn(first.pane, case.world.locations)
                second = case.start("known")
                self.assertEqual(case.command("close", "known")[0], 0)
                self.assertEqual(case.world.closed, [(second.pane, second.terminal)])
            finally:
                case.doCleanups()

    def test_close_retains_a_terminal_that_moves_after_the_stop(self) -> None:
        result = self.start()
        send_signal = self.world.signal

        def move(pid: int, number: int) -> None:
            send_signal(pid, number)
            self.world.locations["w2:p9"] = {**self.world.locations.pop(result.pane), "pane_id": "w2:p9"}

        with patch.object(os, "kill", side_effect=move):
            code, _, errors = self.command("close", "consultation")
        self.assertEqual(code, 8)
        self.assertIn("terminal moved", errors)
        self.assertEqual(self.world.closed, [])
        self.assertIn("w1:p0", self.world.locations)

    def test_send_uses_the_same_terminal_without_a_native_identity(self) -> None:
        result = self.start()
        with patch.object(self.world, "native_session", side_effect=AssertionError("no native identity")), patch.object(
                self.world, "native_pane_process", side_effect=AssertionError("no native process")):
            current = side_pane.send("consultation", "Use the same terminal.")
        self.assertEqual(current, result)
        self.assertIn("Use the same terminal.", self.world.commands[-1][3])
        self.assertEqual(self.world.stops, [])

    def test_consultation_prompt_refuses_a_terminal_that_changes_before_submission(self) -> None:
        result = self.start()
        message = self.root / "message.md"
        write_text_atomic(message, "Review the second question.")
        submit = self.world.prompt_terminal
        before = self.world.commands.copy()

        def changed_terminal(pane: str, terminal: str, text: str) -> None:
            self.world.locations[pane]["terminal_id"] = "replacement-terminal"
            submit(pane, terminal, text)

        with patch.object(self.world, "prompt_terminal", side_effect=changed_terminal):
            code, output, errors = self.command("send", "consultation", "--message-file", str(message))
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
            self.assertEqual(self.command("close", "consultation")[0], 8)
        self.assertEqual(self.world.closed, [])
        self.assertEqual(self.command("close", "consultation")[0], 0)
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
            code, _, errors = self.command("close", "consultation")
        self.assertEqual(code, 8)
        self.assertIn("agent has not ended", errors)
        self.assertEqual(self.world.closed, [])
        self.assertEqual(self.signals, [(pid, signal.SIGTERM)])

    def test_close_preserves_a_same_label_in_another_tab(self) -> None:
        selected = self.start()
        pid = self.world.foreground[selected.pane][0]
        self.world.locations["w2:p9"] = {"pane_id": "w2:p9", "terminal_id": "foreign-terminal",
            "tab_id": "w2:t2", "workspace_id": "w2", "label": "consultation"}
        self.world.layouts["w2:t2"] = {"type": "pane", "pane_id": "w2:p9"}
        self.world.activate(fixture_runtime_id("foreign"), "w2:p9", "claude")
        self.assertEqual(self.command("close", "consultation")[0], 0)
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
        pid = self.world.foreground[result.pane][0]
        process = self.world.processes[pid]
        self.world.processes[pid] = replace(process, identity=herdr.PaneSessionReport(process.identity.id))
        path = pane_launch.record_directory() / "agents.json"
        entry: pane_launch.PaneLaunch = {"kind": "codex", "pane_id": result.pane,
            "session_id": self.world.processes[self.world.foreground[result.pane][0]].identity.id, "config_dir": str(self.root / "codex"),
            "cwd": str(self.root), "name": "consultation", "model": "fixture", "environment": {},
            "remote_control": False, "flags_read": True, "written_at": "fixture-generation"}
        pane_launch.write(path, entry)
        run = self.world.run

        def fail_close(*arguments: str, deadline: float = math.inf) -> str:
            if arguments[:2] == ("pane", "close"):
                raise herdr_types.HerdrFailure("closure failed")
            return run(*arguments, deadline=deadline)

        with patch.object(self.world, "run", side_effect=fail_close):
            closed = side_pane.close("consultation")
            self.assertIsInstance(closed, side_pane.SidePaneCloseUnavailable)
            assert isinstance(closed, side_pane.SidePaneCloseUnavailable)
            self.assertIn("close", closed.reason)
            self.assertEqual(self.command("close", "consultation")[0], 8)
        self.assertEqual(pane_launch.read_valid_entries(path)[result.pane], entry)
        self.assertIn(result.pane, self.world.locations)
        self.assertEqual(self.command("close", "consultation")[0], 0)
        self.assertEqual(pane_launch.read_valid_entries(path), {})
        self.assertEqual(self.world.closed, [(result.pane, result.terminal)])

    def test_open_needs_no_native_identity_for_either_consultation_family(self) -> None:
        for family in ("claude", "codex"):
            with self.subTest(family=family):
                self.caller(family)
                with patch.object(self.world, "native_session", side_effect=AssertionError("no native identity")), patch.object(
                        self.world, "native_pane_process", side_effect=AssertionError("no native process")):
                    opened = self.start(family)
                result = self.start(family)
                self.assertEqual(result, opened)
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
            self.assertEqual(self.command("close", "consultation"), (0, "done\n", ""))
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
            code, _, errors = self.command("close", "consultation")
        self.assertEqual(code, 8)
        self.assertIn("agent has not ended", errors)
        self.assertEqual(self.world.closed, [])
        self.assertEqual(self.command("close", "consultation"), (0, "done\n", ""))
        self.assertEqual(self.signals, [(pid, signal.SIGTERM), (pid, signal.SIGTERM)])
        self.assertEqual(self.world.closed, [(result.pane, result.terminal)])

    def test_a_caller_label_never_selects_the_caller_as_the_consultation(self) -> None:
        self.world.locations["w1:p0"]["label"] = "consultation"
        caller_process = self.world.foreground["w1:p0"]
        self.assertEqual(self.command("close", "consultation"), (0, "done\n", ""))
        self.assertEqual(self.signals, [])
        consultation = self.start()
        self.assertNotEqual(consultation.pane, "w1:p0")
        self.assertEqual(self.start(), consultation)
        self.assertEqual(self.command("close", "consultation"), (0, "done\n", ""))
        self.assertEqual(self.world.foreground["w1:p0"], caller_process)
        self.assertEqual(self.world.closed, [(consultation.pane, consultation.terminal)])

    def test_close_retains_a_replaced_terminal_and_a_reused_agent_process(self) -> None:
        consultation = self.start()
        pid = self.world.foreground[consultation.pane][0]
        read = self.world.result

        def replace_terminal(*arguments: str) -> dict[str, object]:
            result = read(*arguments)
            if arguments[:2] == ("pane", "process-info"):
                self.world.locations[consultation.pane]["terminal_id"] = "replacement-terminal"
            return result

        with patch.object(self.world, "result", side_effect=replace_terminal):
            code, _, errors = self.command("close", "consultation")
        self.assertEqual(code, 8)
        self.assertIn("terminal changed", errors)
        self.assertEqual(self.signals, [])
        self.world.locations[consultation.pane]["terminal_id"] = consultation.terminal
        check = herdr.check_target

        def reused(target: str | herdr.AgentProcessIdentity, *, action: Literal["signal", "keys", "close"]) -> bool:
            write_text_atomic(self.root / "proc" / str(pid) / "stat", f"{pid} (fixture) S " + "0 " * 18 + "2\n")
            return check(target, action=action)

        with patch.object(herdr, "check_target", side_effect=reused):
            code, _, errors = self.command("close", "consultation")
        self.assertEqual(code, 8)
        self.assertIn("reused process", errors)
        self.assertEqual(self.signals, [])
        self.assertEqual(self.world.closed, [])
        self.assertIn(consultation.pane, self.world.locations)

    def test_an_empty_codex_thread_uses_claude_without_reading_staff(self) -> None:
        os.environ["CODEX_THREAD_ID"] = ""
        with patch.object(staff, "snapshot", side_effect=AssertionError("staff must not be read")):
            consultation = self.start()
            self.assertEqual(self.world.requests[-1][0], "claude")
            delivered = side_pane.send("consultation", "Explain the result.")
            self.assertEqual(delivered, consultation)
            self.assertIn("--to session:" + self.caller_id, self.world.commands[-1][3])
            self.assertEqual(self.command("close", "consultation"), (0, "done\n", ""))
        self.assertEqual(self.world.closed, [(consultation.pane, consultation.terminal)])

    def test_open_uses_the_configured_ratio_range_and_reads_configuration_once(self) -> None:
        write_text_atomic(self.root / "conf/direct.conf",
            "# range: 0..1\nWORKER_SPLIT_RATIO=0.95\n# range: 1..600\nPANE_AGENT_START_SECONDS=90\n")
        read = conf.read
        with patch.object(conf, "read", wraps=read) as readings:
            consultation = self.start()
        self.assertEqual([entry.args for entry in readings.call_args_list], [("direct",)])
        destination = self.world.launches[-1]
        assert isinstance(destination, herdr.SplitPane)
        self.assertEqual(destination.ratio, 0.95)
        self.assertEqual(self.world.locations[consultation.pane]["terminal_id"], consultation.terminal)

    def test_close_retires_a_claude_launch_only_after_confirmed_closure(self) -> None:
        consultation = self.start()
        path = pane_launch.record_directory() / "agents.json"
        entry: pane_launch.PaneLaunch = {"kind": "claude", "pane_id": consultation.pane,
            "session_id": self.world.processes[self.world.foreground[consultation.pane][0]].current_identity,
            "config_dir": str(self.root / "claude"), "cwd": str(self.root), "name": "consultation",
            "model": "fixture", "environment": {}, "remote_control": False,
            "flags_read": True, "written_at": "fixture-generation"}
        pane_launch.write(path, entry)
        self.assertEqual(self.command("close", "consultation"), (0, "done\n", ""))
        self.assertEqual(pane_launch.read_valid_entries(path), {})
        self.assertEqual(self.world.closed, [(consultation.pane, consultation.terminal)])
