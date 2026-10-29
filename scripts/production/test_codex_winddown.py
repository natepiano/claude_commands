"""Tests for the Codex wind-down: which processes count, whose they are, and the two messages."""
from __future__ import annotations

import json
import os
import io
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast, override

import broadcast
import codex_winddown
import fake_unit_host
import fake_showrunner
import showrunners
import roster
from process_table import Process
from sessions import SessionRecord
from showrunners import Showrunner

SCRIPT = Path(__file__).with_name("codex_winddown.py")
MESH = "/run/current-system/sw/bin/python3 /home/u/.claude/scripts/direct/../agents/codex_mesh.py"
NOTIFIER_STUB = """print -r -- "notifier $*" >> "$LOG"
case $1 in new) mkdir -p "$NOTIFIER_STATE_DIR/$2" ;; remove) rm -rf "$NOTIFIER_STATE_DIR/$2" ;; esac
"""
SEND_STUB = """import os, sys
with open(os.environ["LOG"], "a") as log:
    _ = log.write("send " + " ".join(sys.argv[1:]) + "\\n")
"""


class CountTests(unittest.TestCase):
    def test_reset_ready_notification_retries_failed_delivery_and_sends_once_after_success(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with (mock.patch.object(codex_winddown, "STATE", directory),
              mock.patch.object(codex_winddown, "instances", return_value=["production"]),
              mock.patch.object(broadcast, "SEND", directory / "fake-send.py"),
              mock.patch.object(socket, "gethostname", return_value="natedev"),
              mock.patch.object(subprocess, "run", side_effect=[
                  subprocess.CompletedProcess([], 3, "FAILED: unavailable", ""),
                  subprocess.CompletedProcess([], 0, "SENT", "")]) as run):
            codex_winddown.announce_quiet(0)
            self.assertFalse((directory / codex_winddown.QUIET).exists())
            codex_winddown.announce_quiet(0)
            codex_winddown.announce_quiet(0)
        self.assertEqual(run.call_count, 2)
        command = cast(list[str], run.call_args.args[0])
        self.assertEqual(command[1], str(directory / "fake-send.py"))
        self.assertEqual(command[command.index("--text-id") + 1], "codex.reset_ready")

    def test_announcement_uses_showrunner_and_unit_director_groups(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with (
            mock.patch.dict(os.environ, {"HOME": str(root)}, clear=True),
            mock.patch.object(showrunners, "registered_showrunners", return_value=[]),
            mock.patch.object(codex_winddown, "clock", return_value="fixture time"),
            mock.patch.object(broadcast, "broadcast", return_value=[]) as send_groups,
            mock.patch.object(broadcast, "report", return_value=True) as report,
        ):
            self.assertTrue(codex_winddown.announce("fixture sender", "runner from {sender}", "unit from {sender}"))
        send_groups.assert_called_once_with("fixture sender", {
            broadcast.Showrunners(): "runner from fixture sender",
            broadcast.UnitDirectors(): "unit from fixture sender",
        })
        report.assert_called_once_with([])

    def test_announcement_reaches_showrunners_and_directors_on_both_machines_through_fake_ssh(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        commands: list[list[str]] = []
        holders: tuple[tuple[str, roster.Role], ...] = (("runner", "showrunner"), ("director", "unit director"))
        rows = [roster.RosterEntry(
            "fixture", "claude", role, name, "working", roster.Messageable(f"session:{name}"),
            "/fixture", roster.NoShowrunner(), roster.NoDirector(), roster.NotAWorker(),
            session=roster.ClaudeSession(name),
        ) for name, role in holders]

        def run(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            if command[0] == "ssh":
                return subprocess.CompletedProcess(command, 0,
                    "peer runner - showrunner - sent\npeer director - unit director - sent\n", "")
            self.assertEqual(Path(command[1]), root / "record-send.py")
            self.assertIn("--here", command)
            return subprocess.CompletedProcess(command, 0, "", "")

        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"HOME": str(root), "BROADCAST_SEND": str(root / "record-send.py"),
                                          "CLAUDE_CODE_SESSION_ID": "unrelated", "CODEX_THREAD_ID": "sender"}, clear=True),
            mock.patch.object(roster, "roster", return_value=roster.Roster(rows, [])) as read,
            mock.patch.object(roster, "machine_name", return_value="fixture"),
            mock.patch.object(roster.remote, "other_machine", return_value="mac"),
            mock.patch.object(broadcast, "SEND", root / "record-send.py"),
            mock.patch.object(showrunners, "registered_showrunners", return_value=[]),
            mock.patch.object(codex_winddown, "clock", return_value="fixture time"),
            mock.patch.object(subprocess, "run", new=run), redirect_stdout(output),
        ):
            self.assertTrue(codex_winddown.announce("sender", "runner from {sender}", "unit from {sender}"))
        read.assert_called_once()
        self.assertEqual(read.call_args.args[1], roster.ThisMachine())
        local = [command for command in commands if command[0] != "ssh"]
        self.assertCountEqual([command[command.index("--to") + 1] for command in local], ["session:runner", "session:director"])
        ssh = [command for command in commands if command[0] == "ssh"]
        self.assertEqual(len(ssh), 1)
        remote_arguments = shlex.split(ssh[0][-1])
        self.assertIn("--here", remote_arguments)
        self.assertEqual(remote_arguments[remote_arguments.index("--showrunners") + 1], "runner from sender")
        self.assertEqual(remote_arguments[remote_arguments.index("--units") + 1], "unit from sender")
        self.assertIn("mac: peer runner - showrunner - sent", output.getvalue())
        self.assertIn("mac: peer director - unit director - sent", output.getvalue())

    def test_start_counts_the_newest_named_session_without_broadcast_session_lookup(self) -> None:
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        older = SessionRecord(
            pid=10,
            sessionId="older",
            name="hana",
            messagingSocketPath="/tmp/older.sock",
            updatedAt=1,
            tmux="",
            formerNames=[],
        )
        newer = SessionRecord(
            pid=11,
            sessionId="newer",
            name="hana",
            messagingSocketPath="/tmp/newer.sock",
            updatedAt=2,
            tmux="",
            formerNames=[],
        )
        unnamed = SessionRecord(
            pid=12,
            sessionId="unnamed",
            name="",
            messagingSocketPath="/tmp/unnamed.sock",
            updatedAt=3,
            tmux="",
            formerNames=[],
        )
        runner = Showrunner(session="hana", socket="/tmp/newer.sock", slug="show", zone="UTC", doc="doc.md")

        with (
            mock.patch.object(codex_winddown, "STATE", state),
            mock.patch.object(codex_winddown, "announce", return_value=True),
            mock.patch.object(codex_winddown, "live_sessions", return_value=[newer, older, unnamed]),
            mock.patch.object(showrunners, "registered_showrunners", return_value=[runner]),
            mock.patch.object(codex_winddown, "start_count", return_value=True) as start_count,
            mock.patch.object(
                broadcast,
                "live_sessions",
                side_effect=AssertionError("start must not read broadcast.live_sessions"),
                create=True,
            ),
        ):
            result = codex_winddown.start("natedev")

        self.assertEqual(result, 0)
        start_count.assert_called_once_with(newer)

    def test_an_agent_belongs_to_its_nearest_named_session(self) -> None:
        table = {10: Process(10, 1, "claude", "claude"), 20: Process(20, 10, "claude", "claude"),
                 30: Process(30, 20, "bash", "bash implement.sh"), 40: Process(40, 30, "python3", f"{MESH} start"),
                 50: Process(50, 1, "codex", "codex exec")}
        counts = codex_winddown.agent_counts(table, {10: "showrunner", 20: "trunk"})
        self.assertEqual(counts, {"trunk": 1, codex_winddown.NO_SESSION: 1})

    def test_the_list_is_every_unit_in_alphabetical_order(self) -> None:
        rows = codex_winddown.listed("hana", ["trunk", "Organon", "startup"], {"trunk": 1, "Organon": 2})
        held: dict[str, codex_winddown.Projection] = {
            "Organon": codex_winddown.CompletionExpectedAt(1300.0),
        }
        self.assertEqual(codex_winddown.render("Codex agents, 13:31 PDT", rows, held, 1000.0),
                         "Codex agents, 13:31 PDT\n\n```\nOrganon - 2 - likely finishes in 5 minutes\n"
                         + "startup - 0\ntrunk - 1 - ETA Unmeasured\n```")

    def test_a_projection_reads_owed_unmeasured_or_minutes(self) -> None:
        held: dict[str, codex_winddown.Projection] = {
            "owed": codex_winddown.ProjectionAnswerOwed(),
            "blind": codex_winddown.CompletionUnmeasured(),
            "soon": codex_winddown.CompletionExpectedAt(1001.0),
            "late": codex_winddown.CompletionExpectedAt(999.0),
        }
        self.assertEqual([codex_winddown.projected(unit, held, 1000.0) for unit in ("owed", "blind", "soon", "late")],
                         ["asked, answer owed", "ETA Unmeasured", "likely finishes in 1 minute", "ETA Unmeasured"])

    def test_a_unit_is_asked_once_and_again_when_its_time_has_passed(self) -> None:
        held: dict[str, codex_winddown.Projection] = {
            "owed": codex_winddown.ProjectionAnswerOwed(),
            "blind": codex_winddown.CompletionUnmeasured(),
            "soon": codex_winddown.CompletionExpectedAt(1001.0),
            "late": codex_winddown.CompletionExpectedAt(999.0),
        }
        self.assertEqual([unit for unit in ("new", "owed", "blind", "soon", "late")
                          if codex_winddown.needs_asking(unit, held, 1000.0)], ["new", "late"])

    def test_every_projection_state_saves_and_reloads_in_its_json_shape(self) -> None:
        state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        expected: dict[str, codex_winddown.Projection] = {
            "timed": codex_winddown.CompletionExpectedAt(1300.0),
            "blind": codex_winddown.CompletionUnmeasured(),
            "owed": codex_winddown.ProjectionAnswerOwed(),
        }
        with mock.patch.object(codex_winddown, "STATE", state):
            with codex_winddown.projections() as saved:
                saved.update(expected)
            serialized = cast(
                object,
                json.loads((state / codex_winddown.PROJECTIONS).read_text(encoding="utf-8")),
            )
            reloaded = codex_winddown.read_projections()

        self.assertEqual(serialized, {"timed": 1300.0, "blind": "unmeasured", "owed": None})
        self.assertEqual(reloaded, expected)

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
        # hana's one unit: a row of its production doc, and a herdr pane marked as that unit.
        doc = self.root / "show-production.md"
        _ = doc.write_text("\n".join(("## Units", "| Unit | Plan | Worktree | Branch | Port | Owns |",
                                      "| --- | --- | --- | --- | --- | --- |",
                                      "| trunk-unit | docs/plan.md | /tmp/no-worktree-of-trunk | trunk | — | — |")),
                           encoding="utf-8")
        _ = (self.root / "showrunners.json").write_text(json.dumps({
            "threshold_percent": 2, "repeat_minutes": 30, "stall_minutes": 5, "faults_to": "hana",
            "always": [],
        }), encoding="utf-8")
        # A second showrunner whose production has no units.
        other = self.root / "other-production.md"
        _ = other.write_text("\n".join(("## Units", "| Unit | Plan | Worktree | Branch | Port | Owns |",
                                        "| --- | --- | --- | --- | --- | --- |")), encoding="utf-8")
        # A showrunner is recorded by its update timer; the one of `zz-gone` has no running session.
        _ = fake_showrunner.write_timer(self.root / "notifier", "show", "id-hana", "America/Los_Angeles", doc)
        _ = fake_showrunner.write_timer(self.root / "notifier", "zz-gone", "id-gone", "America/Los_Angeles", other)
        for name, pid in (("hana", os.getpid()), ("trunk", os.getppid())):
            listener = socket.socket(socket.AF_UNIX)
            self.addCleanup(listener.close)
            listener.bind(str(self.root / f"{name}.sock"))
            _ = (self.root / f"sessions/{pid}.json").write_text(json.dumps({
                "pid": pid, "sessionId": f"id-{name}", "name": name, "updatedAt": 1,
                "tmux": "",
                "messagingSocketPath": str(self.root / f"{name}.sock"),
            }), encoding="utf-8")
        _ = (self.root / "notifier.sh").write_text(NOTIFIER_STUB, encoding="utf-8")
        _ = (self.root / "send.py").write_text(SEND_STUB, encoding="utf-8")
        binaries = self.root / "bin"
        binaries.mkdir()
        ssh = binaries / "ssh"
        _ = ssh.write_text(f"#!{sys.executable}\n"
            + "import json, os, sys\n"
            + "with open(os.environ['SSH_RECORD'], 'a') as log: log.write(json.dumps(sys.argv[1:]) + '\\n')\n",
            encoding="utf-8")
        ssh.chmod(0o755)
        self.environment = {
            "HOME": str(self.root), "PATH": f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}",
            "SSH_RECORD": str(self.root / "ssh.jsonl"), "BROADCAST_CLOSING_MACHINES": "fixture and mac",
            "CLAUDE_CODE_SESSION_ID": "", "CODEX_THREAD_ID": "",
            "SHOWRUNNERS_CONFIG": str(self.root / "showrunners.json"),
            "NOTIFIER_STATE_DIR": str(self.root / "notifier"), "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "CODEX_WINDDOWN_NOTIFIER": str(self.root / "notifier.sh"), "BROADCAST_SEND": str(self.root / "send.py"),
            "CODEX_WINDDOWN_STATE": str(self.root / "prompts"), "LOG": str(self.root / "log"),
            "PROCESS_TABLE_PS": f"cat {self.root / 'ps'}",
            "ROSTER_CODEX_SOCKET": str(self.root / "no-daemon.sock"),
            "ROSTER_RUN_ROOT": str(self.root / "runs"),
            "UNIT_HISTORY_DIR": str(self.root / "history"),
        }
        self.environment.update(fake_unit_host.environment(self.root))
        fake_unit_host.write({**os.environ, **self.environment}, [
            fake_unit_host.HostedUnit("trunk", "w1:p1", {"SHOWRUNNER_UNIT": "show", "SHOWRUNNER_UNIT_ID": "trunk-unit"})])
        fake_unit_host.place_process(self.environment, os.getppid(), "w1:p1")
        self.set_processes()

    def set_processes(self, *lines: str) -> None:
        _ = (self.root / "ps").write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env=self.environment, capture_output=True,
                              text=True, check=False)

    def test_start_tells_each_role_its_version_and_starts_each_showrunners_count(self) -> None:
        done = self.run_script("start", "--from", "natedev")
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = done.stdout.splitlines()
        self.assertEqual(lines[-1], "hana: counting every 2 minutes")
        self.assertCountEqual(lines[:-1], [
            "show · showrunner [session:id-hana] - sent",
            "show · trunk · unit director [session:id-trunk] - sent",
        ])
        log = (self.root / "log").read_text(encoding="utf-8")
        for session in ("hana", "trunk"):
            self.assertIn(f"send --to session:id-{session} --from natedev --here --text Codex wind-down, from the user", log)
        self.assertEqual(log.count('a "Codex count" message arrives'), 1)
        self.assertEqual(log.count("You are encouraged to continue work on your own"), 1)
        self.assertEqual(log.count("Sent directly to showrunners and unit directors on fixture and mac; do not pass it on."), 2)
        self.assertIn("notifier new codex-count-hana --every 2 --to session:id-hana", log)
        prompt = (self.root / "prompts/codex-count-hana.txt").read_text(encoding="utf-8")
        self.assertIn(f"{SCRIPT} count hana`", prompt)

    def test_clear_stops_every_count_and_sends_the_all_clear(self) -> None:
        _ = self.run_script("start", "--from", "natedev")
        done = self.run_script("clear", "--from", "natedev")
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = done.stdout.splitlines()
        self.assertEqual(lines[0], "hana: count stopped")
        self.assertCountEqual(lines[1:], [
            "show · showrunner [session:id-hana] - sent",
            "show · trunk · unit director [session:id-trunk] - sent",
        ])
        # Only the showrunners' own update timers are left: every count is gone.
        self.assertEqual(sorted(path.name for path in (self.root / "notifier").iterdir()),
                         ["showrunner-show", "showrunner-zz-gone"])
        log = (self.root / "log").read_text(encoding="utf-8")
        self.assertIn("send --to session:id-hana --from natedev --here --text All clear, from the user", log)
        self.assertIn("Start using Codex again", log)

    def test_triage_needs_a_wind_down_and_carries_the_users_words(self) -> None:
        self.assertEqual(self.run_script("triage", "--from", "natedev").returncode, 1)
        _ = self.run_script("start", "--from", "natedev")
        done = self.run_script("triage", "--from", "natedev")
        self.assertCountEqual(done.stdout.splitlines(), [
            "show · showrunner [session:id-hana] - sent",
            "show · trunk · unit director [session:id-trunk] - sent",
        ])
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
        self.assertEqual(log.count("send --to user --from codex-winddown --need decision --text-id codex.reset_ready"), 1)
        self.assertIn("--value machine=", log)
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
