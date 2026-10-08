"""Tests for the Codex wind-down: which processes count, whose they are, and the two messages."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import override

import broadcast
import codex_winddown
from broadcast import Process

SCRIPT = Path(__file__).with_name("codex_winddown.py")
MESH = "/run/current-system/sw/bin/python3 /home/u/.claude/scripts/delegate/../agents/codex_mesh.py"
NOTIFIER_STUB = """print -r -- "notifier $*" >> "$LOG"
case $1 in new) mkdir -p "$NOTIFIER_STATE_DIR/$2" ;; remove) rm -rf "$NOTIFIER_STATE_DIR/$2" ;; esac
"""
SEND_STUB = """import os, sys
with open(os.environ["LOG"], "a") as log:
    _ = log.write("send " + " ".join(sys.argv[1:]) + "\\n")
"""


class CountTests(unittest.TestCase):
    def test_a_mesh_turn_and_a_plain_exec_are_agents(self) -> None:
        self.assertTrue(broadcast.is_codex_agent(Process(1, 0, "python3", f"{MESH} start --name a")))
        self.assertTrue(broadcast.is_codex_agent(Process(1, 0, "python3", f"{MESH} follow --to a")))
        self.assertTrue(broadcast.is_codex_agent(Process(1, 0, "codex", "codex exec --json")))

    def test_servers_wrappers_and_claude_seats_are_not_agents(self) -> None:
        for command, arguments in (("codex", "codex app-server --listen ws://127.0.0.1:1"),
                                   ("zsh", f"zsh -c {MESH} start --name a"),
                                   ("python3", f"{MESH} can-follow --session-dir d"),
                                   ("bash", "bash /home/u/.claude/scripts/agents/agent_bg.sh a")):
            self.assertFalse(broadcast.is_codex_agent(Process(1, 0, command, arguments)), arguments)

    def test_an_agent_belongs_to_its_nearest_named_session(self) -> None:
        table = {10: Process(10, 1, "claude", "claude"), 20: Process(20, 10, "claude", "claude"),
                 30: Process(30, 20, "bash", "bash implement.sh"), 40: Process(40, 30, "python3", f"{MESH} start"),
                 50: Process(50, 1, "codex", "codex exec")}
        counts = codex_winddown.agent_counts(table, {10: "showrunner", 20: "trunk"})
        self.assertEqual(counts, {"trunk": 1, codex_winddown.NO_SESSION: 1})

    def test_the_list_is_every_unit_in_alphabetical_order(self) -> None:
        rows = codex_winddown.listed("hana", ["trunk", "Organon", "startup"], {"trunk": 1, "Organon": 2})
        held: dict[str, codex_winddown.Projection] = {"Organon": 1300.0}
        self.assertEqual(codex_winddown.render("Codex agents, 13:31 PDT", rows, held, 1000.0),
                         "Codex agents, 13:31 PDT\n\n```\nOrganon - 2 - likely finishes in 5 minutes\n"
                         + "startup - 0\ntrunk - 1 - ETA Unmeasured\n```")

    def test_a_projection_reads_owed_unmeasured_or_minutes(self) -> None:
        held: dict[str, codex_winddown.Projection] = {"owed": None, "blind": "unmeasured", "soon": 1001.0,
                                                      "late": 999.0}
        self.assertEqual([codex_winddown.projected(unit, held, 1000.0) for unit in ("owed", "blind", "soon", "late")],
                         ["asked, answer owed", "ETA Unmeasured", "likely finishes in 1 minute", "ETA Unmeasured"])

    def test_a_unit_is_asked_once_and_again_when_its_time_has_passed(self) -> None:
        held: dict[str, codex_winddown.Projection] = {"owed": None, "blind": "unmeasured", "soon": 1001.0,
                                                      "late": 999.0}
        self.assertEqual([unit for unit in ("new", "owed", "blind", "soon", "late")
                          if codex_winddown.needs_asking(unit, held, 1000.0)], ["new", "late"])

    def test_a_showrunner_lists_itself_only_with_agents_or_without_units(self) -> None:
        self.assertEqual(codex_winddown.listed("hana", ["trunk"], {"hana": 1}), {"trunk": 0, "hana": 1})
        self.assertEqual(codex_winddown.listed("solo", [], {}), {"solo": 0})


class MessageTests(unittest.TestCase):
    root: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ("notifier", "sessions"):
            (self.root / name).mkdir()
        # hana's one unit: a row of its production doc, and a tmux session marked as that unit.
        doc = self.root / "show-production.md"
        _ = doc.write_text("\n".join(("## Units", "| Unit | Plan | Worktree | Branch | Port | Owns |",
                                      "| --- | --- | --- | --- | --- | --- |",
                                      "| trunk-unit | docs/plan.md | /tmp/no-worktree-of-trunk | trunk | — | — |")),
                           encoding="utf-8")
        _ = (self.root / "tmux.json").write_text(json.dumps({"$1": {
            "label": "any-label", "panes": ["%1"],
            "env": {"SHOWRUNNER_UNIT": "show", "SHOWRUNNER_UNIT_ID": "trunk-unit"}}}), encoding="utf-8")
        self.enterContext(mock.patch.dict(os.environ, {
            "UNIT_LOOKUP_TMUX": str(Path(__file__).with_name("fake_tmux.py")),
            "FAKE_TMUX_STATE": str(self.root / "tmux.json")}))
        _ = (self.root / "showrunners.json").write_text(json.dumps({
            "threshold_percent": 2, "repeat_minutes": 30, "stall_minutes": 5, "faults_to": "hana",
            "always": [], "showrunners": [{"session": "hana", "zone": "America/Los_Angeles", "doc": str(doc)},
                                          {"session": "gone", "zone": "America/Los_Angeles", "doc": ""}],
        }), encoding="utf-8")
        for name, pid in (("hana", os.getpid()), ("trunk", os.getppid())):
            listener = socket.socket(socket.AF_UNIX)
            self.addCleanup(listener.close)
            listener.bind(str(self.root / f"{name}.sock"))
            _ = (self.root / f"sessions/{pid}.json").write_text(json.dumps({
                "pid": pid, "sessionId": f"id-{name}", "name": name, "updatedAt": 1,
                "tmux": "name-at-start:@1.%1" if name == "trunk" else "",
                "messagingSocketPath": str(self.root / f"{name}.sock"),
            }), encoding="utf-8")
        _ = (self.root / "notifier.sh").write_text(NOTIFIER_STUB, encoding="utf-8")
        _ = (self.root / "send.py").write_text(SEND_STUB, encoding="utf-8")
        self.environment = {
            **os.environ, "SHOWRUNNERS_CONFIG": str(self.root / "showrunners.json"),
            "NOTIFIER_STATE_DIR": str(self.root / "notifier"), "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "CODEX_WINDDOWN_NOTIFIER": str(self.root / "notifier.sh"), "BROADCAST_SEND": str(self.root / "send.py"),
            "CODEX_WINDDOWN_STATE": str(self.root / "prompts"), "LOG": str(self.root / "log"),
            "BROADCAST_PS": f"cat {self.root / 'ps'}",
        }
        self.set_processes()

    def set_processes(self, *lines: str) -> None:
        _ = (self.root / "ps").write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env=self.environment, capture_output=True,
                              text=True, check=False)

    def test_start_tells_each_role_its_version_and_starts_each_showrunners_count(self) -> None:
        done = self.run_script("start", "--from", "natedev")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines(), ["hana - showrunner - sent", "gone - showrunner - no live session",
                                                    "trunk - unit director - sent", "hana: counting every 2 minutes"])
        log = (self.root / "log").read_text(encoding="utf-8")
        for session in ("hana", "trunk"):
            self.assertIn(f"send --to {session} --from natedev --text Codex wind-down, from the user", log)
        self.assertEqual(log.count('a "Codex count" message arrives'), 1)
        self.assertEqual(log.count("You are encouraged to continue work on your own"), 1)
        self.assertEqual(log.count("was sent this directly, and no other agent."), 2)
        self.assertIn("notifier new codex-count-hana --every 2 --to session:id-hana", log)
        prompt = (self.root / "prompts/codex-count-hana.txt").read_text(encoding="utf-8")
        self.assertIn(f"{SCRIPT} count hana`", prompt)

    def test_clear_stops_every_count_and_sends_the_all_clear(self) -> None:
        _ = self.run_script("start", "--from", "natedev")
        done = self.run_script("clear", "--from", "natedev")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines(), ["hana: count stopped", "hana - showrunner - sent",
                                                    "gone - showrunner - no live session",
                                                    "trunk - unit director - sent"])
        self.assertEqual(list((self.root / "notifier").iterdir()), [])
        log = (self.root / "log").read_text(encoding="utf-8")
        self.assertIn("send --to hana --from natedev --text All clear, from the user", log)
        self.assertIn("Start using Codex again", log)

    def test_triage_needs_a_wind_down_and_carries_the_users_words(self) -> None:
        self.assertEqual(self.run_script("triage", "--from", "natedev").returncode, 1)
        _ = self.run_script("start", "--from", "natedev")
        done = self.run_script("triage", "--from", "natedev")
        self.assertEqual(done.stdout.splitlines(), ["hana - showrunner - sent", "gone - showrunner - no live session",
                                                    "trunk - unit director - sent"])
        self.assertIn("which ones can be stopped now and added to a resume list",
                      (self.root / "log").read_text(encoding="utf-8"))

    def test_a_unit_records_its_answer_and_start_forgets_it(self) -> None:
        self.assertEqual(self.run_script("eta", "trunk", "5").stdout, "trunk: recorded\n")
        self.assertEqual(self.run_script("eta", "organon", "unmeasured").returncode, 0)
        self.assertEqual(self.run_script("eta", "trunk", "soon").returncode, 2)
        saved = (self.root / "prompts/projections.json").read_text(encoding="utf-8")
        self.assertIn('"organon": "unmeasured"', saved)
        self.assertIn('"trunk": ', saved)
        _ = self.run_script("start", "--from", "natedev")
        self.assertFalse((self.root / "prompts/projections.json").exists())

    def test_a_unit_that_reaches_no_agents_is_told_at_once_and_only_once(self) -> None:
        (self.root / "prompts").mkdir()
        _ = (self.root / "prompts/projections.json").write_text('{"trunk": null}', encoding="utf-8")
        for _ in range(2):
            self.assertIn("trunk - 0\n", self.run_script("count", "hana").stdout)
        log = (self.root / "log").read_text(encoding="utf-8")
        self.assertEqual(log.count("send --to trunk --from hana --text Codex wind-down, from hana: your Codex agents"), 1)
        self.assertIn("launch Claude agents for short time frames", log)

    def test_the_user_is_told_once_when_a_wind_down_reaches_no_agents(self) -> None:
        agent = (f"{os.getpid()} 1 claude claude", f"40 {os.getpid()} python3 {MESH} start --name a")
        self.set_processes(*agent)
        _ = self.run_script("start", "--from", "natedev")
        self.assertIn("hana - 1 - ETA Unmeasured", self.run_script("count", "hana").stdout)
        self.assertNotIn("--to user", (self.root / "log").read_text(encoding="utf-8"))
        self.set_processes()
        for _ in range(2):
            _ = self.run_script("count", "hana")
        log = (self.root / "log").read_text(encoding="utf-8")
        self.assertEqual(log.count("send --to user --from codex-winddown --summary Codex wind-down --need decision"
                                   + " --text No Codex agent is running on"), 1)
        self.set_processes(*agent)
        _ = self.run_script("count", "hana")
        self.set_processes()
        _ = self.run_script("count", "hana")
        self.assertEqual((self.root / "log").read_text(encoding="utf-8").count("--to user"), 2)

    def test_status_says_whether_a_wind_down_is_on(self) -> None:
        self.assertIn("Wind-down: off", self.run_script("status").stdout)
        _ = self.run_script("start", "--from", "natedev")
        self.assertIn("Wind-down: on for hana", self.run_script("status").stdout)


if __name__ == "__main__":
    _ = unittest.main()
