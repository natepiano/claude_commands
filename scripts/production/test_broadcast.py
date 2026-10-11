"""Prove group membership, sender identity, and one concurrent delivery per address."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
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
    name: str, address: str, *, role: str = "freestanding", kind: str = "claude",
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

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = (self.root / "send.py").write_text(SEND_STUB, encoding="utf-8")
        _ = (self.root / "ps").write_text("", encoding="utf-8")
        self.environment = {
            "HOME": str(self.root), "PATH": os.environ.get("PATH", ""),
            "BROADCAST_SEND": str(self.root / "send.py"), "LOG": str(self.root),
            "TOGETHER": "1", "CLAUDE_CODE_SESSION_ID": "id-natedev",
            "PROCESS_TABLE_PS": f"cat {self.root / 'ps'}",
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
            "ROSTER_DELEGATE_ROOT": str(self.root / "delegate"),
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
            entry("fps-impl", "seat:run-a/fps-impl", role="worker", kind="codex",
                  showrunner=roster.UnderShowrunner("show", "hana", "session:id-hana"),
                  director=roster.UnderDirector("trunk-unit", "trunk", "session:id-trunk"),
                  thread=roster.CodexThread("thread-seat")),
            entry("Codex window", "codex:thread-window", kind="codex",
                  thread=roster.CodexThread("thread-window")),
        ]
        self.problems = []
        _ = self.enterContext(cast(
            AbstractContextManager[object], patch.dict(os.environ, self.environment, clear=True),
        ))
        _ = self.enterContext(patch.object(broadcast, "SEND", self.root / "send.py"))
        _ = self.enterContext(patch.object(process_table, "PS", ["cat", str(self.root / "ps")]))
        _ = self.enterContext(patch.object(
            process_table, "snapshot", side_effect=AssertionError("use the fake roster"),
        ))
        _ = self.enterContext(patch.object(
            codex_daemon, "loaded_sessions", side_effect=AssertionError("use the fake roster"),
        ))

    def run_main(self, *arguments: str, **environment: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        fixture = roster.Roster(self.rows, self.problems)
        with (patch.dict(os.environ, environment),
              patch.object(roster, "roster", return_value=fixture) as read_roster,
              patch.object(roster, "sender_identity", wraps=roster.sender_identity) as read_identity,
              redirect_stdout(output), redirect_stderr(errors)):
            result = broadcast.main(["--from", "natedev", *arguments])
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
            self.assertEqual(command[4:5], ["--text"])
            self.assertEqual(len(command), 6)
        return commands

    def sent_texts(self) -> dict[str, str]:
        commands = self.sends()
        addresses = [command[1] for command in commands]
        self.assertEqual(len(addresses), len(set(addresses)), "an address was sent twice")
        return {command[1]: command[5] for command in commands}

    def test_all_reaches_every_role_at_once_and_never_the_sender(self) -> None:
        result, output, errors = self.run_main("--all", "From the user: stop builds.", TOGETHER="5")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(set(self.sent_texts()), {
            "session:id-hana", "session:id-trunk", "session:id-ups",
            "seat:run-a/fps-impl", "codex:thread-window",
        })
        self.assertRegex(output.splitlines()[-1], r"^5 sent in \d+ s$")
        for text in self.sent_texts().values():
            self.assertTrue(text.startswith("From the user: stop builds.\n\n"))
            self.assertIn("showrunner", text)
            self.assertIn("unit director", text)
            self.assertIn("agent", text)

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
        self.assertEqual(output.splitlines()[:-1], ["showrunners - none on this machine"])
        self.assertRegex(output.splitlines()[-1], r"^0 sent in \d+ s$")

    def test_empty_showrunners_are_reported_while_unit_directors_are_sent(self) -> None:
        self.rows = [row for row in self.rows if row.role != "showrunner"]
        result, output, errors = self.run_main("--units", "A", "--showrunners", "B")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(set(self.sent_texts()), {"session:id-trunk"})
        self.assertTrue(self.sent_texts()["session:id-trunk"].startswith("A\n\n"))
        self.assertEqual(output.splitlines()[:-1], [
            "trunk - unit director - sent", "showrunners - none on this machine",
        ])
        self.assertRegex(output.splitlines()[-1], r"^1 sent in \d+ s$")

    def test_a_group_with_only_the_sender_says_none(self) -> None:
        self.rows = [entry("natedev", "session:id-natedev", role="showrunner",
                           showrunner=roster.RunsProduction("other"))]
        result, output, errors = self.run_main("--showrunners", "stop")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(self.sends(), [])
        self.assertEqual(output.splitlines()[:-1], ["showrunners - none on this machine"])
        self.assertRegex(output.splitlines()[-1], r"^0 sent in \d+ s$")

    def test_a_unit_with_no_workers_says_none_for_its_scoped_group(self) -> None:
        self.rows = [row for row in self.rows if row.role != "worker"]
        result, output, errors = self.run_main("--of", "trunk-unit", "--workers", "finish")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(self.sends(), [])
        self.assertEqual(output.splitlines()[:-1], ["workers of trunk-unit - none on this machine"])
        self.assertRegex(output.splitlines()[-1], r"^0 sent in \d+ s$")

    def test_empty_groups_are_named_once_in_command_line_order(self) -> None:
        self.rows = []
        result, output, errors = self.run_main(
            "--top-level", "top", "--agents", "agents", "--all", "all",
            "--workers", "workers", "--units", "units", "--showrunners", "runners",
        )
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(self.sends(), [])
        self.assertEqual(output.splitlines()[:-1], [
            "top level - none on this machine", "agents - none on this machine",
            "all roles - none on this machine", "workers - none on this machine",
            "unit directors - none on this machine", "showrunners - none on this machine",
        ])
        self.assertRegex(output.splitlines()[-1], r"^0 sent in \d+ s$")

    def test_a_group_overridden_by_a_later_flag_is_still_nonempty(self) -> None:
        self.rows = [entry("hana", "session:id-hana", role="showrunner",
                           showrunner=roster.RunsProduction("show"))]
        result, output, errors = self.run_main("--all", "all", "--showrunners", "runners")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(set(self.sent_texts()), {"session:id-hana"})
        self.assertTrue(self.sent_texts()["session:id-hana"].startswith("runners\n\n"))
        self.assertEqual(output.splitlines()[:-1], ["hana - showrunner - sent"])

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
        for address in ("session:id-ups", "seat:run-a/fps-impl", "codex:thread-window"):
            self.assertTrue(texts[address].startswith("carry on\n\n"))

    def test_one_role_alone_is_told_the_other_was_not_sent_it(self) -> None:
        for flag, address, inside, outside in (
            ("--showrunners", "session:id-hana", "showrunner", "unit director"),
            ("--units", "session:id-trunk", "unit director", "showrunner"),
        ):
            with self.subTest(flag=flag):
                result, output, errors = self.run_main(flag, "merge freeze")
                self.assertEqual((result, errors), (0, ""), output)
                latest = next(command[5] for command in self.sends() if command[1] == address)
                closing = latest.split("\n\n")[-1]
                self.assertIn(inside, closing)
                self.assertNotIn(outside, closing)
        self.assertEqual(len(self.sends()), 2)

    def test_a_delivery_that_fails_is_named_with_its_reason(self) -> None:
        result, output, _errors = self.run_main(
            "--showrunners", "stop", "--units", "stop", REFUSED="session:id-trunk",
        )
        self.assertEqual(result, 1)
        self.assertIn("trunk - unit director - NOT sent: QUEUED: relay timed out after 40 s", output)
        self.assertIn("hana - showrunner - sent", output)

    def test_any_other_arguments_print_the_usage(self) -> None:
        for arguments in ([], ["--everyone", "a"], ["--all"], ["--all", "a", "--units"],
                          ["--workers", "a", "--of", "trunk"]):
            with self.subTest(arguments=arguments):
                result, _output, errors = self.run_main(*arguments)
                self.assertEqual(result, 2)
                self.assertIn("usage: broadcast.py", errors)
        self.assertEqual(self.sends(), [])

    def test_codex_seat_identity_wins_over_its_inherited_director_session(self) -> None:
        result, output, errors = self.run_main(
            "--all", "stop", CODEX_THREAD_ID="thread-seat", CLAUDE_CODE_SESSION_ID="id-trunk",
        )
        self.assertEqual((result, errors), (0, ""), output)
        texts = self.sent_texts()
        self.assertNotIn("seat:run-a/fps-impl", texts)
        self.assertIn("session:id-trunk", texts)
        self.assertEqual(len(texts), 5)

    def test_daemon_codex_sender_is_left_out(self) -> None:
        result, output, errors = self.run_main("--agents", "stop", CODEX_THREAD_ID="thread-window")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(set(self.sent_texts()), {"session:id-ups", "seat:run-a/fps-impl"})

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
            entry("orphan", "seat:orphan/impl", role="worker", kind="codex"),
        ])
        result, output, errors = self.run_main("--workers", "finish")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(set(self.sent_texts()), {"session:worker", "seat:orphan/impl", "seat:run-a/fps-impl"})
        self.assertTrue(all("worker" in text.split("\n\n")[-1] for text in self.sent_texts().values()))

    def test_same_named_workers_are_selected_by_their_director_address(self) -> None:
        self.rows = [
            entry("director", "session:director-a", role="unit director", director=roster.DirectsUnit("unit-a")),
            entry("director", "session:director-b", role="unit director", director=roster.DirectsUnit("unit-b")),
            entry("impl", "seat:run-a/impl", role="worker", kind="codex",
                  director=roster.UnderDirector("unit-a", "director", "session:director-a")),
            entry("impl", "seat:run-b/impl", role="worker", kind="codex",
                  director=roster.UnderDirector("unit-b", "director", "session:director-b")),
            entry("orphan", "seat:orphan/impl", role="worker", kind="codex"),
            entry("runner", "session:runner", role="showrunner", showrunner=roster.RunsProduction("production")),
            entry("unhosted director", "session:unhosted", role="unit director",
                  showrunner=roster.ProductionWithoutRunningShowrunner("gone"),
                  director=roster.DirectsUnit("unhosted")),
        ]
        result, output, errors = self.run_main("--of", "unit-a", "--workers", "finish A",
                                               "--of", "session:director-b", "--workers", "finish B")
        self.assertEqual((result, errors), (0, ""), output)
        texts = self.sent_texts()
        self.assertEqual(set(texts), {"seat:run-a/impl", "seat:run-b/impl"})
        self.assertTrue(texts["seat:run-a/impl"].startswith("finish A\n\n"))
        self.assertTrue(texts["seat:run-b/impl"].startswith("finish B\n\n"))
        self.assertIn("workers of unit-a", texts["seat:run-a/impl"])

    def test_of_accepts_a_unique_director_name(self) -> None:
        result, output, errors = self.run_main("--of", "trunk", "--workers", "finish")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(set(self.sent_texts()), {"seat:run-a/fps-impl"})

    def test_socketless_directors_workers_are_selected_by_session_identity(self) -> None:
        self.rows = [replace(row, reach=roster.CannotReceiveMessages("no messaging socket"))
                     if row.role == "unit director" else row for row in self.rows]
        result, output, errors = self.run_main("--of", "session:id-trunk", "--workers", "finish")
        self.assertEqual((result, errors), (0, ""), output)
        self.assertEqual(set(self.sent_texts()), {"seat:run-a/fps-impl"})
        self.assertTrue(self.sent_texts()["seat:run-a/fps-impl"].startswith("finish\n\n"))
        self.assertIn("workers of trunk-unit", self.sent_texts()["seat:run-a/fps-impl"])

    def test_socketless_directors_workers_are_selected_by_name_and_unit(self) -> None:
        self.rows = [replace(row, reach=roster.CannotReceiveMessages("no messaging socket"))
                     if row.role == "unit director" else row for row in self.rows]
        for target in ("trunk", "trunk-unit"):
            with self.subTest(target=target):
                result, output, errors = self.run_main("--of", target, "--workers", target)
                self.assertEqual((result, errors), (0, ""), output)
                commands = self.sends()
                self.assertTrue(all(command[1] == "seat:run-a/fps-impl" for command in commands))
                self.assertEqual(sum(command[5].startswith(target + "\n\n") for command in commands), 1)
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
        self.assertEqual(set(self.sent_texts()), {"seat:run-a/fps-impl"})
        self.assertIn("reviewer cannot take messages (blind reviewer); contact its unit director trunk or wait", output)
        self.assertIn("orphan reviewer cannot take messages (blind reviewer); no unit director is running for it", output)
        self.assertRegex(output.splitlines()[-1], r"^1 sent in \d+ s$")

    def test_between_turns_seat_is_reported_without_a_send_or_failure(self) -> None:
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
        for address in ("session:id-trunk", "seat:run-a/fps-impl", "codex:thread-window"):
            self.assertTrue(texts[address].startswith("everyone\n\n"))


if __name__ == "__main__":
    _ = unittest.main()
