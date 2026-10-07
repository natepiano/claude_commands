"""Tests for the Codex wind-down: which processes count, whose they are, and the two messages."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import override

import codex_winddown
from codex_winddown import Process

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
        self.assertTrue(codex_winddown.is_codex_agent(Process(1, 0, "python3", f"{MESH} start --name a")))
        self.assertTrue(codex_winddown.is_codex_agent(Process(1, 0, "python3", f"{MESH} follow --to a")))
        self.assertTrue(codex_winddown.is_codex_agent(Process(1, 0, "codex", "codex exec --json")))

    def test_servers_wrappers_and_claude_seats_are_not_agents(self) -> None:
        for command, arguments in (("codex", "codex app-server --listen ws://127.0.0.1:1"),
                                   ("zsh", f"zsh -c {MESH} start --name a"),
                                   ("python3", f"{MESH} can-follow --session-dir d"),
                                   ("bash", "bash /home/u/.claude/scripts/agents/agent_bg.sh a")):
            self.assertFalse(codex_winddown.is_codex_agent(Process(1, 0, command, arguments)), arguments)

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
        _ = (self.root / "showrunners.json").write_text(json.dumps({
            "threshold_percent": 2, "repeat_minutes": 30, "stall_minutes": 5, "faults_to": "hana",
            "always": [], "showrunners": [{"session": "hana", "zone": "America/Los_Angeles", "units": ["trunk"]},
                                          {"session": "gone", "zone": "America/Los_Angeles", "units": []}],
        }), encoding="utf-8")
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        listener.bind(str(self.root / "hana.sock"))
        _ = (self.root / "sessions/1.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": "abc", "name": "hana", "updatedAt": 1,
            "messagingSocketPath": str(self.root / "hana.sock"),
        }), encoding="utf-8")
        _ = (self.root / "notifier.sh").write_text(NOTIFIER_STUB, encoding="utf-8")
        _ = (self.root / "send.py").write_text(SEND_STUB, encoding="utf-8")
        self.environment = {
            **os.environ, "SHOWRUNNERS_CONFIG": str(self.root / "showrunners.json"),
            "NOTIFIER_STATE_DIR": str(self.root / "notifier"), "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "CODEX_WINDDOWN_NOTIFIER": str(self.root / "notifier.sh"), "CODEX_WINDDOWN_SEND": str(self.root / "send.py"),
            "CODEX_WINDDOWN_STATE": str(self.root / "prompts"), "LOG": str(self.root / "log"),
        }

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env=self.environment, capture_output=True,
                              text=True, check=False)

    def test_start_tells_each_live_showrunner_and_starts_its_count(self) -> None:
        done = self.run_script("start", "--from", "natedev")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines(), ["hana: told to wind down", "gone: no live session, skipped",
                                                    "hana: counting every 2 minutes"])
        log = (self.root / "log").read_text(encoding="utf-8")
        self.assertIn("send --to hana --from natedev --text Codex wind-down, from the user", log)
        self.assertIn("notifier new codex-count-hana --every 2 --to session:abc", log)
        prompt = (self.root / "prompts/codex-count-hana.txt").read_text(encoding="utf-8")
        self.assertIn(f"{SCRIPT} count hana`", prompt)

    def test_clear_stops_every_count_and_sends_the_all_clear(self) -> None:
        _ = self.run_script("start", "--from", "natedev")
        done = self.run_script("clear", "--from", "natedev")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines(), ["hana: count stopped", "hana: told the all clear",
                                                    "gone: no live session, skipped"])
        self.assertEqual(list((self.root / "notifier").iterdir()), [])
        self.assertIn("--text All clear, from the user", (self.root / "log").read_text(encoding="utf-8"))

    def test_triage_needs_a_wind_down_and_carries_the_users_words(self) -> None:
        self.assertEqual(self.run_script("triage", "--from", "natedev").returncode, 1)
        _ = self.run_script("start", "--from", "natedev")
        done = self.run_script("triage", "--from", "natedev")
        self.assertEqual(done.stdout.splitlines(), ["hana: told to sort its agents", "gone: no live session, skipped"])
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

    def test_status_says_whether_a_wind_down_is_on(self) -> None:
        self.assertIn("Wind-down: off", self.run_script("status").stdout)
        _ = self.run_script("start", "--from", "natedev")
        self.assertIn("Wind-down: on for hana", self.run_script("status").stdout)


if __name__ == "__main__":
    _ = unittest.main()
