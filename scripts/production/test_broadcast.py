"""Prove group membership, sender identity, and one concurrent delivery per address."""
from __future__ import annotations

import io
import json
import os
import sys
import shlex
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import AbstractContextManager, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

import broadcast
import codex_daemon
import process_table
import roster

NO_SHOWRUNNER = roster.NoShowrunner()
NO_DIRECTOR = roster.NoDirector()
NO_CODEX_THREAD = roster.NoCodexThread()

# Every send must start before any can finish, proving deliveries run together.
SEND_STUB = """import hashlib, json, os, sys, time, uuid
from pathlib import Path
root = Path(os.environ["LOG"])
args = sys.argv[1:]
to = args[args.index("--to") + 1]
(root / ("started-" + hashlib.sha256(to.encode()).hexdigest())).touch()
deadline = time.monotonic() + 3
while len(list(root.glob("started-*"))) < int(os.environ["TOGETHER"]) and time.monotonic() < deadline:
    time.sleep(0.01)
(root / ("send-" + uuid.uuid4().hex + ".json")).write_text(json.dumps(args))
if to == os.environ.get("REFUSED") or len(list(root.glob("started-*"))) < int(os.environ["TOGETHER"]):
    print("QUEUED: relay timed out after 40 s")
    raise SystemExit(1)
"""


def entry(
    name: str, address: str, *, role: roster.Role = "freestanding", kind: str = "claude",
    showrunner: roster.ShowrunnerRelationship = NO_SHOWRUNNER,
    director: roster.DirectorRelationship = NO_DIRECTOR,
    thread: roster.CodexThread | roster.NoCodexThread = NO_CODEX_THREAD,
) -> roster.RosterEntry:
    return roster.RosterEntry(
        machine="fixture", kind=kind, role=role, name=name, status="working",
        reach=roster.Messageable(address), cwd="/fixture", showrunner=showrunner,
        director=director, assignment=(roster.WorkerAssignment("impl", "/fixture/run")
                                      if role == "worker" else roster.NotAWorker()),
        thread=thread,
        session=(roster.ClaudeSession(address.removeprefix("session:"))
                 if kind == "claude" and address.startswith("session:") else roster.NoClaudeSession()),
    )


class BroadcastTests(unittest.TestCase):
    root: Path = Path()
    rows: list[roster.RosterEntry] = []
    environment: dict[str, str] = {}
    problems: list[str] = []
    peer_rows: list[roster.RosterEntry] = []
    peer_problems: list[str] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = (self.root / "send.py").write_text(SEND_STUB, encoding="utf-8")
        _ = (self.root / "ps").write_text("", encoding="utf-8")
        binaries = self.root / "bin"
        binaries.mkdir()
        (self.root / "peer-log").mkdir()
        peer_driver = binaries / "ssh"
        _ = peer_driver.write_text(
            f"#!{sys.executable}\n"
            + "import json, os, shlex, sys\n"
            + "from pathlib import Path\n"
            + "with open(os.environ['SSH_RECORD'], 'a') as log: log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            + "if os.environ.get('SSH_FAIL'): print('ssh: connection refused', file=sys.stderr); raise SystemExit(255)\n"
            + "if 'roster.py' in sys.argv[-1]: print(os.environ['SSH_SNAPSHOT']); raise SystemExit(0)\n"
            + "arguments = shlex.split(sys.argv[-1])\n"
            + "index = next(i for i, item in enumerate(arguments) if item.endswith('/broadcast.py'))\n"
            + "for item in arguments[:index]:\n"
            + "    if item.startswith(('BROADCAST_SEND=', 'BROADCAST_CLOSING_MACHINES=')): key, value = item.split('=', 1); os.environ[key] = value\n"
            + "os.environ.pop('CODEX_THREAD_ID', None); os.environ.pop('CLAUDE_CODE_SESSION_ID', None)\n"
            + "os.environ['LOG'] = os.environ['PEER_LOG']; os.environ['TOGETHER'] = '1'\n"
            + f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
            + "import broadcast, roster\n"
            + "broadcast.SEND = Path(os.environ.get('BROADCAST_SEND') or os.environ['SAFE_PEER_SEND'])\n"
            + "roster.machine_name = lambda: 'mac'\n"
            + "roster.roster = lambda environment, scope: roster.RosterSnapshot.decode(os.environ['SSH_SNAPSHOT']).result\n"
            + "raise SystemExit(broadcast.main(arguments[index + 1:]))\n",
            encoding="utf-8",
        )
        peer_driver.chmod(0o755)
        self.environment = {
            "HOME": str(self.root), "PATH": f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}",
            "SSH_RECORD": str(self.root / "ssh.jsonl"),
            "SSH_SNAPSHOT": roster.RosterSnapshot(roster.Roster([], [])).encode(),
            "PEER_LOG": str(self.root / "peer-log"), "SAFE_PEER_SEND": str(self.root / "send.py"),
            "BROADCAST_SEND": str(self.root / "send.py"), "LOG": str(self.root),
            "TOGETHER": "1", "CLAUDE_CODE_SESSION_ID": "id-natedev",
            "PROCESS_TABLE_PS": f"cat {self.root / 'ps'}",
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
            "ROSTER_RUN_ROOT": str(self.root / "runs"),
            "ROSTER_CODEX_SOCKET": str(self.root / "no-daemon.sock"),
        }
        self.rows = [
            entry("hana", "session:id-hana", role="showrunner",
                  showrunner=roster.RunsProduction("show")),
            entry("natedev", "session:id-natedev", role="showrunner",
                  showrunner=roster.RunsProduction("other")),
            entry("trunk", "session:id-trunk", role="unit director",
                  showrunner=roster.UnderShowrunner("show", "hana", "session:id-hana"),
                  director=roster.DirectsUnit("trunk-unit")),
            entry("ups", "session:id-ups"),
            entry("fps-impl", "worker:run-a/fps-impl", role="worker", kind="codex",
                  showrunner=roster.UnderShowrunner("show", "hana", "session:id-hana"),
                  director=roster.UnderDirector("trunk-unit", "trunk", "session:id-trunk"),
                  thread=roster.CodexThread("thread-worker")),
            entry("Codex window", "codex:thread-window", kind="codex",
                  thread=roster.CodexThread("thread-window")),
        ]
        self.problems, self.peer_problems, self.peer_rows = [], [], []
        _ = self.enterContext(cast(
            AbstractContextManager[object], patch.dict(os.environ, self.environment, clear=True),
        ))
        _ = self.enterContext(patch.object(broadcast, "SEND", self.root / "send.py"))
        _ = self.enterContext(patch.object(roster, "machine_name", return_value="fixture"))
        _ = self.enterContext(patch.object(roster.remote, "other_machine", return_value="mac"))
        _ = self.enterContext(patch.object(process_table, "PS", ["cat", str(self.root / "ps")]))
        _ = self.enterContext(patch.object(
            process_table, "snapshot", side_effect=AssertionError("use the fake roster"),
        ))
        _ = self.enterContext(patch.object(
            codex_daemon, "loaded_sessions", side_effect=AssertionError("use the fake roster"),
        ))

    def fixture_roster(self, environment: Mapping[str, str], scope: roster.RosterScope) -> roster.Roster:
        local = roster.Roster(self.rows, self.problems)
        if isinstance(scope, roster.AllMachines):
            peer = roster.peer_roster(environment)
            return roster.Roster([*local.entries, *peer.entries], [*local.problems, *peer.problems])
        self.assertEqual(scope, roster.ThisMachine())
        return local

    def run_main(self, *arguments: str, here: bool = True, **environment: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        os.environ["SSH_SNAPSHOT"] = roster.RosterSnapshot(roster.Roster(self.peer_rows, self.peer_problems)).encode()
        with (patch.dict(os.environ, environment),
              patch.object(roster, "roster", side_effect=self.fixture_roster) as read_roster,
              patch.object(roster, "sender_identity", wraps=roster.sender_identity) as read_identity,
              redirect_stdout(output), redirect_stderr(errors)):
            result = broadcast.main([*(["--here"] if here else []), "--from", "natedev", *arguments])
        if result != 2:
            read_roster.assert_called_once()
            read_identity.assert_called_once()
        return result, output.getvalue(), errors.getvalue()

    def sends(self) -> list[list[str]]:
        commands = [cast(list[str], json.loads(path.read_text(encoding="utf-8")))
                    for path in self.root.glob("send-*.json")]
        for command in commands:
            self.assertEqual(command[:1], ["--to"])
            self.assertEqual(command[2:4], ["--from", "natedev"])
            self.assertEqual(command[4:6], ["--here", "--text"])
            self.assertEqual(len(command), 7)
        return commands

    def sent_texts(self) -> dict[str, str]:
        commands = self.sends()
        addresses = [command[1] for command in commands]
        self.assertEqual(len(addresses), len(set(addresses)), "an address was sent twice")
        return {command[1]: command[6] for command in commands}

    def test_all_reaches_every_role_at_once_and_never_the_sender(self) -> None:
        result, output, errors = self.run_main("--all", "From the user: stop builds.", TOGETHER="5")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(set(self.sent_texts()), {
            "session:id-hana", "session:id-trunk", "session:id-ups",
            "worker:run-a/fps-impl", "codex:thread-window",
        })
        self.assertRegex(output.splitlines()[-2], r"^5 sent in \d+ s$")
        for text in self.sent_texts().values():
            self.assertTrue(text.startswith("From the user: stop builds.\n\n"))
            self.assertIn("showrunner", text)
            self.assertIn("unit director", text)
            self.assertIn("agent", text)

    def test_report_shows_each_job_and_address_once(self) -> None:
        result, output, errors = self.run_main("--all", "stop")
        self.assertEqual((result, errors), (0, ""), output)
        lines = output.splitlines()[:-2]
        self.assertIn("session:id-ups - sent", lines)
        self.assertIn("codex:thread-window - sent", lines)
        self.assertIn("show · trunk · unit director [session:id-trunk] - sent", lines)
        self.assertIn("show · showrunner [session:id-hana] - sent", lines)
        for line in lines:
            self.assertNotIn("[session:id-ups]", line)
            self.assertNotIn("[codex:thread-window]", line)
            self.assertLessEqual(line.count("unit director"), 1)
            self.assertLessEqual(line.count("showrunner"), 1)

    def test_audiences_use_organizational_role_even_when_names_and_jobs_disagree(self) -> None:
        base = self.rows[0]
        rows = [
            replace(base, name="misleading-impl", role="showrunner"),
            replace(base, name="code6", role="unit director",
                    showrunner=roster.UnderShowrunner("show", "runner", "session:runner")),
            replace(base, name="showrunner", role="worker", assignment=roster.WorkerAssignment(
                "review", "/run", "review", "code6"),
                    director=roster.UnderDirector("roles-unit", "director", "session:director")),
            replace(base, name="unit-director", role="freestanding"),
            replace(base, name="held-designer", kind="codex", role="astra-designer",
                    assignment=roster.HeldDesignRole("designer", "/held")),
            replace(base, name="held-reviewer", kind="codex", role="astra-reviewer",
                    assignment=roster.HeldDesignRole("reviewer", "/held")),
        ]
        for group, expected in ((broadcast.Showrunners(), {"misleading-impl"}),
                                (broadcast.UnitDirectors(), {"code6"}),
                                (broadcast.Workers(), {"showrunner"}),
                                (broadcast.WorkersOfUnit("session:director", "roles-unit", "fixture"), {"showrunner"}),
                                (broadcast.Agents(), {"showrunner", "unit-director"}),
                                (broadcast.TopLevel(), {"misleading-impl", "unit-director"}),
                                (broadcast.AllRoles(), {row.name for row in rows})):
            with self.subTest(group=group):
                self.assertEqual({row.name for row in rows if broadcast.selects(group, row)}, expected)

    def test_delivery_prints_shown_name_beside_unchanged_address(self) -> None:
        worker = replace(self.rows[4], assignment=roster.WorkerAssignment("test", "/fixture/run", "test", "test"))
        self.rows = [worker]
        result, output, errors = self.run_main("--workers", "finish")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertIn("show · trunk · worker test", output)
        self.assertIn("worker:run-a/fps-impl", output)
        self.assertEqual(set(self.sent_texts()), {"worker:run-a/fps-impl"})

    def test_a_registry_file_that_cannot_be_read_is_skipped(self) -> None:
        self.problems = ["sessions/broken.json: invalid JSON"]
        result, output, errors = self.run_main("--all", "stop")
        self.assertEqual(result, 0, output + errors)
        self.assertEqual(len(self.sends()), 5)
        self.assertEqual(errors.splitlines(), ["broadcast: roster: sessions/broken.json: invalid JSON"])

    def test_showrunners_with_no_members_say_none_and_send_nothing(self) -> None:
        self.rows = [row for row in self.rows if row.role != "showrunner"]
        result, output, errors = self.run_main("--showrunners", "stop")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(self.sends(), [])
        self.assertEqual(output.splitlines()[:-2], ["showrunners - none on this machine"])
        self.assertRegex(output.splitlines()[-2], r"^0 sent in \d+ s$")

    def test_empty_showrunners_are_reported_while_unit_directors_are_sent(self) -> None:
        self.rows = [row for row in self.rows if row.role != "showrunner"]
        result, output, errors = self.run_main("--units", "A", "--showrunners", "B")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(set(self.sent_texts()), {"session:id-trunk"})
        self.assertTrue(self.sent_texts()["session:id-trunk"].startswith("A\n\n"))
        self.assertEqual(output.splitlines()[:-2], [
            "show · trunk · unit director [session:id-trunk] - sent", "showrunners - none on this machine",
        ])
        self.assertRegex(output.splitlines()[-2], r"^1 sent in \d+ s$")

    def test_a_group_with_only_the_sender_says_none(self) -> None:
        self.rows = [entry("natedev", "session:id-natedev", role="showrunner",
                           showrunner=roster.RunsProduction("other"))]
        result, output, errors = self.run_main("--showrunners", "stop")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(self.sends(), [])
        self.assertEqual(output.splitlines()[:-2], ["showrunners - none on this machine"])
        self.assertRegex(output.splitlines()[-2], r"^0 sent in \d+ s$")

    def test_a_unit_with_no_workers_says_none_for_its_scoped_group(self) -> None:
        self.rows = [row for row in self.rows if row.role != "worker"]
        result, output, errors = self.run_main("--of", "trunk-unit", "--workers", "finish")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(self.sends(), [])
        self.assertEqual(output.splitlines()[:-2], ["workers of trunk-unit - none on this machine"])
        self.assertRegex(output.splitlines()[-2], r"^0 sent in \d+ s$")

    def test_empty_groups_are_named_once_in_command_line_order(self) -> None:
        self.rows = []
        result, output, errors = self.run_main(
            "--top-level", "top", "--agents", "agents", "--all", "all",
            "--workers", "workers", "--units", "units", "--showrunners", "runners",
        )
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(self.sends(), [])
        self.assertEqual(output.splitlines()[:-2], [
            "top level - none on this machine", "agents - none on this machine",
            "all roles - none on this machine", "workers - none on this machine",
            "unit directors - none on this machine", "showrunners - none on this machine",
        ])
        self.assertRegex(output.splitlines()[-2], r"^0 sent in \d+ s$")

    def test_a_group_overridden_by_a_later_flag_is_still_nonempty(self) -> None:
        self.rows = [entry("hana", "session:id-hana", role="showrunner",
                           showrunner=roster.RunsProduction("show"))]
        result, output, errors = self.run_main("--all", "all", "--showrunners", "runners")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(set(self.sent_texts()), {"session:id-hana"})
        self.assertTrue(self.sent_texts()["session:id-hana"].startswith("runners\n\n"))
        self.assertEqual(output.splitlines()[:-2], ["show · showrunner [session:id-hana] - sent"])

    def test_each_role_gets_its_own_version(self) -> None:
        result, output, errors = self.run_main("--showrunners", "count your agents", "--units", "finish your agents")
        self.assertEqual((result, errors), (0, ""), output)
        texts = self.sent_texts()
        self.assertEqual(set(texts), {"session:id-hana", "session:id-trunk"})
        self.assertTrue(texts["session:id-hana"].startswith("count your agents\n\n"))
        self.assertTrue(texts["session:id-trunk"].startswith("finish your agents\n\n"))
        for text in texts.values():
            self.assertIn("showrunner", text)
            self.assertIn("unit director", text)

    def test_a_role_flag_after_all_gives_that_role_its_own_version(self) -> None:
        result, output, errors = self.run_main("--all", "Codex is back", "--agents", "carry on")
        self.assertEqual((result, errors), (0, ""), output)
        texts = self.sent_texts()
        for address in ("session:id-hana", "session:id-trunk"):
            self.assertTrue(texts[address].startswith("Codex is back\n\n"))
        for address in ("session:id-ups", "worker:run-a/fps-impl", "codex:thread-window"):
            self.assertTrue(texts[address].startswith("carry on\n\n"))

    def test_one_role_alone_is_told_the_other_was_not_sent_it(self) -> None:
        for flag, address, inside, outside in (
            ("--showrunners", "session:id-hana", "showrunner", "unit director"),
            ("--units", "session:id-trunk", "unit director", "showrunner"),
        ):
            with self.subTest(flag=flag):
                result, output, errors = self.run_main(flag, "merge freeze")
                self.assertEqual((result, errors), (0, ""), output)
                latest = next(command[6] for command in self.sends() if command[1] == address)
                closing = latest.split("\n\n")[-1]
                self.assertIn(inside, closing)
                self.assertNotIn(outside, closing)
        self.assertEqual(len(self.sends()), 2)

    def test_a_delivery_that_fails_is_named_with_its_reason(self) -> None:
        result, output, _errors = self.run_main(
            "--showrunners", "stop", "--units", "stop", REFUSED="session:id-trunk",
        )
        self.assertEqual(result, 1)
        self.assertIn("show · trunk · unit director [session:id-trunk] - NOT sent: QUEUED: relay timed out after 40 s", output)
        self.assertIn("show · showrunner [session:id-hana] - sent", output)

    def test_any_other_arguments_print_the_usage(self) -> None:
        for arguments in ([], ["--everyone", "a"], ["--all"], ["--all", "a", "--units"],
                          ["--workers", "a", "--of", "trunk"]):
            with self.subTest(arguments=arguments):
                result, _output, errors = self.run_main(*arguments)
                self.assertEqual(result, 2)
                self.assertIn("usage: broadcast.py", errors)
        self.assertEqual(self.sends(), [])

    def test_codex_worker_identity_wins_over_its_inherited_director_session(self) -> None:
        result, output, errors = self.run_main(
            "--all", "stop", CODEX_THREAD_ID="thread-worker", CLAUDE_CODE_SESSION_ID="id-trunk",
        )
        self.assertEqual((result, errors), (0, ""), output)
        texts = self.sent_texts()
        self.assertNotIn("worker:run-a/fps-impl", texts)
        self.assertIn("session:id-trunk", texts)
        self.assertEqual(len(texts), 5)

    def test_daemon_codex_sender_is_left_out(self) -> None:
        result, output, errors = self.run_main("--agents", "stop", CODEX_THREAD_ID="thread-window")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(set(self.sent_texts()), {"session:id-ups", "worker:run-a/fps-impl"})

    def test_plain_shell_leaves_nobody_out_even_when_from_matches_a_name(self) -> None:
        result, output, errors = self.run_main("--all", "stop", CLAUDE_CODE_SESSION_ID="", CODEX_THREAD_ID="")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(len(self.sent_texts()), 6)
        self.assertIn("session:id-natedev", self.sent_texts())

    def test_a_session_sharing_the_senders_name_is_still_sent(self) -> None:
        self.rows.append(entry("natedev", "session:other-natedev"))
        result, output, errors = self.run_main("--all", "stop")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertIn("session:other-natedev", self.sent_texts())
        self.assertNotIn("session:id-natedev", self.sent_texts())

    def test_all_workers_include_claude_codex_and_orphans_but_no_directors(self) -> None:
        self.rows.extend([
            entry("claude-worker", "session:worker", role="worker",
                  director=roster.UnderDirector("trunk-unit", "trunk", "session:id-trunk")),
            entry("orphan", "worker:orphan/impl", role="worker", kind="codex"),
        ])
        result, output, errors = self.run_main("--workers", "finish")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(set(self.sent_texts()), {"session:worker", "worker:orphan/impl", "worker:run-a/fps-impl"})
        self.assertTrue(all("worker" in text.split("\n\n")[-1] for text in self.sent_texts().values()))

    def test_same_named_workers_are_selected_by_their_director_address(self) -> None:
        self.rows = [
            entry("director", "session:director-a", role="unit director", director=roster.DirectsUnit("unit-a")),
            entry("director", "session:director-b", role="unit director", director=roster.DirectsUnit("unit-b")),
            entry("impl", "worker:run-a/impl", role="worker", kind="codex",
                  director=roster.UnderDirector("unit-a", "director", "session:director-a")),
            entry("impl", "worker:run-b/impl", role="worker", kind="codex",
                  director=roster.UnderDirector("unit-b", "director", "session:director-b")),
            entry("orphan", "worker:orphan/impl", role="worker", kind="codex"),
            entry("runner", "session:runner", role="showrunner", showrunner=roster.RunsProduction("production")),
            entry("unhosted director", "session:unhosted", role="unit director",
                  showrunner=roster.ProductionWithoutRunningShowrunner("gone"),
                  director=roster.DirectsUnit("unhosted")),
        ]
        result, output, errors = self.run_main("--of", "unit-a", "--workers", "finish A",
                                               "--of", "session:director-b", "--workers", "finish B")
        self.assertEqual((result, errors), (0, ""), output)
        texts = self.sent_texts()
        self.assertEqual(set(texts), {"worker:run-a/impl", "worker:run-b/impl"})
        self.assertTrue(texts["worker:run-a/impl"].startswith("finish A\n\n"))
        self.assertTrue(texts["worker:run-b/impl"].startswith("finish B\n\n"))
        self.assertIn("workers of unit-a", texts["worker:run-a/impl"])

    def test_of_accepts_a_unique_director_name(self) -> None:
        result, output, errors = self.run_main("--of", "trunk", "--workers", "finish")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(set(self.sent_texts()), {"worker:run-a/fps-impl"})

    def test_socketless_directors_workers_are_selected_by_session_identity(self) -> None:
        self.rows = [replace(row, reach=roster.CannotReceiveMessages("no messaging socket"))
                     if row.role == "unit director" else row for row in self.rows]
        result, output, errors = self.run_main("--of", "session:id-trunk", "--workers", "finish")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(set(self.sent_texts()), {"worker:run-a/fps-impl"})
        self.assertTrue(self.sent_texts()["worker:run-a/fps-impl"].startswith("finish\n\n"))
        self.assertIn("workers of trunk-unit", self.sent_texts()["worker:run-a/fps-impl"])

    def test_socketless_directors_workers_are_selected_by_name_and_unit(self) -> None:
        self.rows = [replace(row, reach=roster.CannotReceiveMessages("no messaging socket"))
                     if row.role == "unit director" else row for row in self.rows]
        for target in ("trunk", "trunk-unit"):
            with self.subTest(target=target):
                result, output, errors = self.run_main("--of", target, "--workers", target)
                self.assertEqual((result, errors), (0, ""), output)
                commands = self.sends()
                self.assertTrue(all(command[1] == "worker:run-a/fps-impl" for command in commands))
                self.assertEqual(sum(command[6].startswith(target + "\n\n") for command in commands), 1)
        self.assertEqual(len(self.sends()), 2)

    def test_socketless_directors_keep_their_session_addresses_in_usage_errors(self) -> None:
        self.rows = [replace(row, reach=roster.CannotReceiveMessages("no messaging socket")) for row in (
            entry("director", "session:director-a", role="unit director", director=roster.DirectsUnit("unit")),
            entry("director", "session:director-b", role="unit director", director=roster.DirectsUnit("unit")),
        )]
        for target in ("director", "unit", "missing"):
            with self.subTest(target=target):
                result, _output, errors = self.run_main("--of", target, "--workers", "finish")
                self.assertEqual(result, 2)
                self.assertIn("session:director-a", errors)
                self.assertIn("session:director-b", errors)
                self.assertIn("matches more than one unit director" if target != "missing" else "known units", errors)
        self.assertEqual(self.sends(), [])

    def test_of_matching_two_directors_is_a_usage_error_with_both_addresses(self) -> None:
        self.rows.extend([
            entry("trunk", "session:second-trunk", role="unit director",
                  director=roster.DirectsUnit("trunk-unit"),
                  showrunner=roster.UnderShowrunner("other", "natedev", "session:id-natedev")),
        ])
        for target in ("trunk", "trunk-unit"):
            with self.subTest(target=target):
                result, _output, errors = self.run_main("--of", target, "--workers", "finish")
                self.assertEqual(result, 2)
                self.assertIn("session:id-trunk", errors)
                self.assertIn("session:second-trunk", errors)
        self.assertEqual(self.sends(), [])

    def test_unknown_unit_names_the_known_units(self) -> None:
        result, _output, errors = self.run_main("--of", "unknown-unit", "--workers", "finish")
        self.assertEqual(result, 2)
        self.assertIn("unknown-unit", errors)
        self.assertIn("trunk-unit", errors)
        self.assertEqual(self.sends(), [])

    def test_unmessageable_reviewers_report_both_director_states_without_failure(self) -> None:
        for name, relationship in (
            ("reviewer", roster.UnderDirector("trunk-unit", "trunk", "session:id-trunk")),
            ("orphan reviewer", roster.NoDirector()),
        ):
            self.rows.append(replace(
                entry(name, "", role="worker", kind="codex", director=relationship),
                reach=roster.CannotReceiveMessages("blind reviewer"),
            ))
        result, output, errors = self.run_main("--workers", "finish")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(set(self.sent_texts()), {"worker:run-a/fps-impl"})
        self.assertIn("reviewer cannot take messages (blind reviewer); contact its unit director trunk or wait", output)
        self.assertIn("orphan reviewer cannot take messages (blind reviewer)", output)
        self.assertRegex(output.splitlines()[-2], r"^1 sent in \d+ s$")

    def test_between_turns_worker_is_reported_without_a_send_or_failure(self) -> None:
        self.rows.append(replace(
            entry("waiting", "", role="worker", kind="codex",
                  director=roster.UnderDirector("trunk-unit", "trunk", "session:id-trunk")),
            reach=roster.CannotReceiveMessages("between turns; its unit director resumes it"),
        ))
        result, output, errors = self.run_main("--workers", "finish")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(len(self.sends()), 1)
        self.assertIn("between turns; its unit director resumes it", output)
        self.assertIn("contact its unit director trunk or wait", output)

    def test_top_level_includes_independent_directors_but_excludes_workers_and_codex(self) -> None:
        self.rows.extend([
            entry("solo", "session:solo", role="unit director", director=roster.DirectsUnit("solo-unit")),
            entry("unhosted", "session:unhosted", role="unit director", director=roster.DirectsUnit("unhosted"),
                  showrunner=roster.ProductionWithoutRunningShowrunner("gone")),
            entry("claude worker", "session:worker", role="worker"),
        ])
        result, output, errors = self.run_main("--top-level", "heads up")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(set(self.sent_texts()), {"session:id-hana", "session:id-ups", "session:solo", "session:unhosted"})
        self.assertTrue(all("top level" in text for text in self.sent_texts().values()))

    def test_all_top_level_showrunners_deliver_once_using_last_selecting_text(self) -> None:
        result, output, errors = self.run_main(
            "--all", "everyone", "--top-level", "top", "--showrunners", "runners",
        )
        self.assertEqual((result, errors), (0, ""), output)
        texts = self.sent_texts()
        self.assertEqual(len(texts), 5)
        self.assertTrue(texts["session:id-hana"].startswith("runners\n\n"))
        self.assertTrue(texts["session:id-ups"].startswith("top\n\n"))
        for address in ("session:id-trunk", "worker:run-a/fps-impl", "codex:thread-window"):
            self.assertTrue(texts[address].startswith("everyone\n\n"))


    def ssh_calls(self) -> list[list[str]]:
        log = self.root / "ssh.jsonl"
        return [cast(list[str], json.loads(line)) for line in log.read_text().splitlines()] if log.exists() else []

    def peer_sends(self) -> list[list[str]]:
        return [cast(list[str], json.loads(path.read_text())) for path in (self.root / "peer-log").glob("send-*.json")]

    def peer_worker_groups(self) -> tuple[broadcast.WorkersOfUnit, broadcast.WorkersOfUnit]:
        self.peer_rows = [
            replace(entry("director A", "session:director-a", role="unit director",
                          director=roster.DirectsUnit("unit-a")), machine="mac"),
            replace(entry("director B", "session:director-b", role="unit director",
                          director=roster.DirectsUnit("unit-b")), machine="mac"),
            replace(entry("impl", "worker:run-a/impl", role="worker", kind="codex",
                          director=roster.UnderDirector("unit-a", "director A", "session:director-a")), machine="mac"),
            replace(entry("impl", "worker:run-b/impl", role="worker", kind="codex",
                          director=roster.UnderDirector("unit-b", "director B", "session:director-b")), machine="mac"),
            replace(entry("orphan", "worker:orphan/impl", role="worker", kind="codex"), machine="mac"),
        ]
        return (broadcast.WorkersOfUnit("session:director-a", "unit-a", "mac"),
                broadcast.WorkersOfUnit("session:director-b", "unit-b", "mac"))

    def assert_peer_worker_texts(
        self, texts: Mapping[broadcast.BroadcastGroup, str], expected: Mapping[str, str],
    ) -> None:
        self.rows = []
        os.environ["SSH_SNAPSHOT"] = roster.RosterSnapshot(roster.Roster(self.peer_rows, [])).encode()
        with patch.object(roster, "roster", side_effect=self.fixture_roster):
            deliveries = broadcast.broadcast("natedev", texts)
        self.assertTrue(all(delivery.outcome != "NOT sent" for delivery in deliveries), deliveries)
        self.assertEqual(self.sends(), [])
        calls = self.ssh_calls()
        self.assertEqual(len(calls), 1, "the mapping must replay through one fake SSH call")
        self.assertIn("broadcast.py", calls[0][-1])
        self.assertIn("--here", shlex.split(calls[0][-1]))
        commands = self.peer_sends()
        self.assertCountEqual([command[1] for command in commands], expected)
        actual: dict[str, str] = {}
        for command in commands:
            self.assertIn("--here", command)
            text = command[command.index("--text") + 1]
            actual[command[1]] = text.partition("\n\n")[0]
        self.assertEqual(actual, expected)

    def test_peer_all_workers_after_a_unit_scope_reaches_every_worker_once(self) -> None:
        unit_a, _unit_b = self.peer_worker_groups()
        self.assert_peer_worker_texts(
            {unit_a: "unit text", broadcast.Workers(): "all workers text"},
            {"worker:run-a/impl": "all workers text", "worker:run-b/impl": "all workers text",
             "worker:orphan/impl": "all workers text"},
        )

    def test_peer_unit_scope_after_all_workers_overrides_only_its_own_workers(self) -> None:
        unit_a, _unit_b = self.peer_worker_groups()
        self.assert_peer_worker_texts(
            {broadcast.Workers(): "all workers text", unit_a: "unit text"},
            {"worker:run-a/impl": "unit text", "worker:run-b/impl": "all workers text",
             "worker:orphan/impl": "all workers text"},
        )

    def test_peer_two_unit_scopes_keep_each_units_workers_and_text_separate(self) -> None:
        unit_a, unit_b = self.peer_worker_groups()
        self.assert_peer_worker_texts(
            {unit_a: "A text", unit_b: "B text"},
            {"worker:run-a/impl": "A text", "worker:run-b/impl": "B text"},
        )

    def test_here_never_runs_ssh_and_every_recipient_send_stays_here(self) -> None:
        result, output, errors = self.run_main("--all", "stop")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(self.ssh_calls(), [])
        self.assertTrue(self.sends())
        self.assertTrue(all("--here" in command for command in self.sends()))

    def test_both_machines_deliver_each_selected_recipient_once_and_name_both_in_the_closing_line(self) -> None:
        self.peer_rows = [replace(entry("Mac Claude", "session:mac-claude"), machine="mac"),
                          replace(entry("Mac Codex", "codex:mac-thread", kind="codex"), machine="mac")]
        result, output, errors = self.run_main("--all", "all", "--agents", "agents", here=False)
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(len(self.sends()), 5)
        peer = self.peer_sends()
        self.assertCountEqual([command[1] for command in peer], ["session:mac-claude", "codex:mac-thread"])
        self.assertTrue(all("--here" in command for command in peer))
        self.assertTrue(all(command[command.index("--text") + 1].startswith("agents\n\n") for command in peer))
        self.assertIn("mac: session:mac-claude - sent", output)
        self.assertIn("fixture and mac", output.splitlines()[-1])
        self.assertEqual(len(self.ssh_calls()), 1)

    def test_remote_run_clears_sender_identities_and_leaves_every_peer_row_in(self) -> None:
        self.peer_rows = [replace(entry("same id", "session:id-natedev"), machine="mac"),
                          replace(entry("same thread", "codex:thread-worker", kind="codex",
                                        thread=roster.CodexThread("thread-worker")), machine="mac")]
        result, output, errors = self.run_main("--all", "all", here=False,
                                              CODEX_THREAD_ID="thread-worker", CLAUDE_CODE_SESSION_ID="id-natedev")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(len(self.peer_sends()), 2)
        command = self.ssh_calls()[0][-1]
        self.assertIn("-u CODEX_THREAD_ID", command)
        self.assertIn("-u CLAUDE_CODE_SESSION_ID", command)

    def test_unreachable_peer_prints_one_failed_machine_line_while_local_sends_continue(self) -> None:
        result, output, errors = self.run_main("--all", "all", here=False, SSH_FAIL="1")
        self.assertEqual(result, 1, errors)
        self.assertEqual(len(self.sends()), 5)
        self.assertEqual(len([line for line in output.splitlines() if "mac:" in line and "NOT sent" in line]), 1)

    def test_recording_sender_is_forwarded_only_when_selected_in_the_environment(self) -> None:
        for selected in (True, False):
            with self.subTest(selected=selected):
                if not selected:
                    _ = os.environ.pop("BROADCAST_SEND")
                result, output, errors = self.run_main("--all", "all", here=False)
                self.assertEqual((result, errors), (0, ""), output)
                command = self.ssh_calls()[-1][-1]
                self.assertEqual("BROADCAST_SEND=" in command, selected)
                if selected:
                    self.assertIn(str(self.root / "send.py"), command)
        self.assertEqual(len(self.ssh_calls()), 2)

    def test_each_machine_names_its_own_empty_groups(self) -> None:
        self.rows = []
        self.peer_rows = []
        result, output, errors = self.run_main("--showrunners", "all", here=False)
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual([line for line in output.splitlines() if "none on this machine" in line], [
            "showrunners - none on this machine", "mac: showrunners - none on this machine",
        ])
        self.assertEqual(self.sends() + self.peer_sends(), [])

    def test_of_a_local_director_is_resolved_before_delivery_and_never_replayed_on_peer(self) -> None:
        result, output, errors = self.run_main("--of", "trunk", "--workers", "finish", here=False)
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(set(self.sent_texts()), {"worker:run-a/fps-impl"})
        self.assertEqual(self.peer_sends(), [])
        self.assertTrue(self.ssh_calls())
        self.assertTrue(all("roster.py" in call[-1] for call in self.ssh_calls()))

    def test_of_a_peer_director_uses_its_resolved_identity_and_only_its_machine(self) -> None:
        director = replace(entry("peer director", "session:peer-director", role="unit director",
                                 director=roster.DirectsUnit("peer-unit")), machine="mac",
                           reach=roster.CannotReceiveMessages("no messaging socket"))
        worker = replace(entry("impl", "worker:peer/impl", kind="codex", role="worker",
                               director=roster.UnderDirector("peer-unit", "peer director", "session:peer-director")), machine="mac")
        self.peer_rows = [director, worker]
        result, output, errors = self.run_main("--of", "peer director", "--workers", "finish", here=False)
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(self.sends(), [])
        self.assertEqual([command[1] for command in self.peer_sends()], ["worker:peer/impl"])
        forwarded = next(shlex.split(call[-1]) for call in self.ssh_calls() if "broadcast.py" in call[-1])
        self.assertEqual(forwarded[forwarded.index("--of") + 1], "session:peer-director")
        self.assertIn("--here", forwarded)

    def test_of_a_name_shared_across_machines_refuses_before_any_delivery(self) -> None:
        self.peer_rows = [replace(entry("trunk", "session:peer-trunk", role="unit director",
                                       director=roster.DirectsUnit("peer-unit")), machine="mac")]
        result, output, errors = self.run_main("--all", "all", "--of", "trunk", "--workers", "finish", here=False)
        self.assertEqual(result, 2, output + errors)
        self.assertIn("session:id-trunk", errors)
        self.assertIn("session:peer-trunk", errors)
        self.assertEqual(self.sends() + self.peer_sends(), [])
        self.assertTrue(all("roster.py" in call[-1] for call in self.ssh_calls()))

    def test_of_refuses_before_delivery_when_the_peer_cannot_prove_director_uniqueness(self) -> None:
        result, output, errors = self.run_main(
            "--of", "trunk", "--workers", "finish", here=False, SSH_FAIL="1",
        )
        self.assertEqual(result, 1, output + errors)
        self.assertEqual(self.sends() + self.peer_sends(), [])
        self.assertEqual(len([line for line in output.splitlines() if "NOT sent" in line]), 1)
        self.assertTrue(all("roster.py" in call[-1] for call in self.ssh_calls()))

    def test_refusal_advice_reports_worker_and_socketless_states_without_failure(self) -> None:
        self.rows = [
            replace(entry("owned", "", role="worker", kind="codex",
                          director=roster.UnderDirector("unit", "director", "session:director")),
                    reach=roster.CannotReceiveMessages("blind reviewer")),
            replace(entry("orphan", "", role="worker", kind="codex"),
                    reach=roster.CannotReceiveMessages("between turns; no unit director is running")),
            replace(entry("Claude", "session:socketless"), reach=roster.CannotReceiveMessages("no messaging socket")),
        ]
        result, output, errors = self.run_main("--all", "all")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertIn("owned cannot take messages (blind reviewer); contact its unit director director or wait\n", output)
        self.assertIn("orphan cannot take messages (between turns; no unit director is running)\n", output)
        self.assertIn("Claude cannot take messages (no messaging socket); wait for it to open its messaging socket, then read the roster again and send again\n", output)
        self.assertNotIn("NOT sent", output)
        self.assertEqual(self.sends(), [])

    def test_a_recipient_disappearing_after_roster_read_is_never_looked_up_on_peer(self) -> None:
        result, output, errors = self.run_main("--units", "finish", REFUSED="session:id-trunk")
        self.assertEqual(result, 1, errors)
        self.assertIn("show · trunk · unit director [session:id-trunk] - NOT sent", output)
        self.assertEqual(self.ssh_calls(), [])
        self.assertEqual(len(self.sends()), 1)
        self.assertIn("--here", self.sends()[0])


if __name__ == "__main__":
    _ = unittest.main()
