"""rca.py cases, owner messages and pushes, with a temp state dir and a stub send.py."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, NamedTuple, cast, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "notify"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "production"))
import push_texts  # noqa: E402
import rca  # noqa: E402
import showrunners  # noqa: E402

OPENED = datetime(2026, 10, 23, 13, 30, tzinfo=timezone.utc)
SHOWRUNNER = "uds:/tmp/cargo-liner-show.sock"
STUB = """import json, os, sys
with open(os.environ['RCA_TEST_LOG'], 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
print(os.environ.get('RCA_TEST_OUTPUT', 'SENT: to the user'))
raise SystemExit(int(os.environ.get('RCA_TEST_EXIT', '0')))
"""
# The stub of subprocess.Popen keeps the real one: subprocess.run calls Popen too.
REAL_POPEN = subprocess.Popen
Stream = int | IO[str] | None
HANDLER = rca.Report("handler-unit", "natedev", "prompt", ("cargo build -p cargo-handler",), "/tmp/t.jsonl",
                     "cargo-liner")


def same_slug(slug: str) -> str:
    return slug


def showrunner_address(slug: str) -> str:
    """Only cargo-liner's showrunner runs."""
    return SHOWRUNNER if slug == "cargo-liner" else ""


def flags(argv: list[str]) -> dict[str, str]:
    """Each flag of a send.py call and its value; the --value pairs go under their own keys."""
    found: dict[str, str] = {}
    for flag, value in zip(argv, argv[1:]):
        if flag == "--value":
            key, _, item = value.partition("=")
            found["value:" + key] = item
        elif flag.startswith("--"):
            found[flag] = value
    return found


class Detached(NamedTuple):
    """How a detached owner message started."""
    stdin: Stream
    log: str
    start_new_session: bool


class RcaTests(unittest.TestCase):
    root: Path = Path()
    log: Path = Path()
    detached: list[Detached] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.log = self.root / "sends.jsonl"
        stub = self.root / "send.py"
        _ = stub.write_text(STUB)
        _ = self.enterContext(mock.patch.object(rca, "STATE", self.root / "rca"))
        _ = self.enterContext(mock.patch.object(rca, "SEND", stub))
        removed = (rca.PRODUCTION_MARK, "CLAUDE_CODE_SESSION_ID", "RCA_TEST_EXIT")
        environment = {key: value for key, value in os.environ.items() if key not in removed}
        self.enterContext(mock.patch.dict(os.environ, environment | {"RCA_TEST_LOG": str(self.log),
                                                                     "USER_ZONE": "America/Puerto_Rico"}, clear=True))
        _ = self.enterContext(mock.patch.object(showrunners, "canonical_slug", side_effect=same_slug))
        _ = self.enterContext(mock.patch.object(showrunners, "current_address", side_effect=showrunner_address))
        self.detached = []
        _ = self.enterContext(mock.patch.object(subprocess, "Popen", self.popen))

    def popen(self, argv: list[str], *, stdin: Stream = None, stdout: Stream = None, stderr: Stream = None,
              text: bool = False, start_new_session: bool = False) -> subprocess.Popen[str]:
        """Record how a detached owner message starts, then run it to its end, so a test reads what it sent.
        A push's subprocess.run goes to the real Popen."""
        if not start_new_session:
            return REAL_POPEN(argv, stdin=stdin, stdout=stdout, stderr=stderr, text=text)
        self.detached.append(Detached(stdin, "" if stdout is None or isinstance(stdout, int) else stdout.name, True))
        process = REAL_POPEN(argv, stdin=stdin, stdout=stdout, stderr=stderr, text=True)
        _ = process.wait()
        return process

    def sends(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [cast(list[str], json.loads(line)) for line in self.log.read_text().splitlines()]

    def case(self, case_id: str) -> rca.Case:
        found = rca.read_case(rca.case_path(case_id))
        assert found is not None
        return found

    def quiet(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = rca.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_a_new_case_sends_its_showrunner_the_steps_once(self) -> None:
        self.assertEqual(rca.open_case(HANDLER, OPENED), "rca: opened 261023-1")
        case = self.case("261023-1")
        self.assertEqual((case["session"], case["production"], case["status"], case["commands"], case["alerts"],
                          case["reminded"], case["opened"]),
                         ("handler-unit", "cargo-liner", "open", ["cargo build -p cargo-handler"], 1, False,
                          OPENED.isoformat()))
        [owner] = self.sends()
        sent = flags(owner)
        self.assertEqual((sent["--to"], sent["--from"], sent["--key"]), (SHOWRUNNER, "rca", "rca-261023-1"))
        for part in ("rca 261023-1: handler-unit on natedev stopped at a permission prompt.",
                     "Commands: cargo build -p cargo-handler", "Transcript: /tmp/t.jsonl",
                     f"1. Run `{rca.CALL} start 261023-1 --owner <unit you route the fix to>`.",
                     "2. Find where the command came from: a work order, a helper prompt, a skill or a script.",
                     "(an allow rule only when the command is safe and expected)",
                     f"4. After the fix merges, run `{rca.CALL} fixed 261023-1 --cause"):
            self.assertIn(part, sent["--text"])
        self.assertEqual(rca.CALL, "~/.claude/scripts/lib/py ~/.claude/scripts/message/rca.py")
        # The message starts detached: the caller does not wait for its relay, and its output goes to the log.
        self.assertEqual(self.detached, [Detached(subprocess.DEVNULL, str(self.root / "rca" / rca.OWNER_LOG), True)])
        logged = (self.root / "rca" / rca.OWNER_LOG).read_text().splitlines()
        self.assertTrue(logged[0].endswith(" rca-261023-1"))
        self.assertEqual(logged[1:], ["SENT: to the user"])

    def test_the_same_session_and_commands_join_the_case_until_it_is_fixed(self) -> None:
        two = rca.Report("modules", "natedev", "auto_mode", ("implement.sh --to modules-impl",
                                                             "review.sh  --to modules-test"), production="cargo-liner")
        self.assertEqual(rca.open_case(two, OPENED), "rca: opened 261023-1")
        swapped = two._replace(commands=("review.sh --to modules-test", "implement.sh --to modules-impl"))
        self.assertEqual(rca.open_case(swapped, OPENED), "rca: joined 261023-1")
        self.assertEqual(self.case("261023-1")["alerts"], 2)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(rca.open_case(two._replace(commands=("git push",)), OPENED), "rca: opened 261023-2")
        self.assertEqual(self.quiet(["fixed", "261023-1", "--cause", "c", "--fix", "f", "--commit", "abc"])[0], 0)
        self.assertEqual(rca.open_case(two, OPENED), "rca: opened 261023-3")

    def test_the_owner_is_natedev_without_a_running_showrunner_and_the_mark_names_the_production(self) -> None:
        _ = rca.open_case(HANDLER._replace(production=""), OPENED)
        _ = rca.open_case(HANDLER._replace(session="other", production="stopped-production"), OPENED)
        with mock.patch.dict(os.environ, {rca.PRODUCTION_MARK: "cargo-liner"}):
            _ = rca.open_case(HANDLER._replace(session="marked", production=""), OPENED)
        self.assertEqual([flags(argv)["--to"] for argv in self.sends()], ["natedev", "natedev", SHOWRUNNER])
        self.assertEqual(self.case("261023-3")["production"], "cargo-liner")

    def test_the_mark_is_the_one_unit_lookup_reads(self) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "production"))
        import unit_lookup
        self.assertEqual(rca.PRODUCTION_MARK, unit_lookup.PRODUCTION_MARK)

    def test_the_transcript_is_the_sessions_own_when_none_is_given(self) -> None:
        transcript = self.root / "config/projects/-home-x/sid-1.jsonl"
        transcript.parent.mkdir(parents=True)
        _ = transcript.write_text("{}\n")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sid-1", "CLAUDE_CONFIG_DIR": str(self.root / "config")}):
            _ = rca.open_case(HANDLER._replace(transcript=""), OPENED)
        self.assertEqual(self.case("261023-1")["transcript"], str(transcript))

    def test_open_never_fails_its_caller(self) -> None:
        blocked = self.root / "a-file"
        _ = blocked.write_text("")
        with mock.patch.object(rca, "STATE", blocked / "rca"):
            code, out, err = self.quiet(["open", "--session", "s", "--machine", "natedev", "--kind", "prompt"])
        self.assertEqual(code, 0)
        self.assertIn("rca: no case opened: ", out)
        self.assertIn("rca: no case opened: ", err)
        # An owner message that does not start still opens the case; the reminder sends it later.
        with mock.patch.object(subprocess, "Popen", side_effect=OSError("no python")):
            code, out, err = self.quiet(["open", "--session", "s", "--machine", "natedev", "--kind", "prompt"])
        self.assertEqual((code, out), (0, "rca: opened 261023-1\n"))
        self.assertIn("the owner message did not start, a reminder follows: no python", err)

    def test_start_sends_the_registered_started_push_with_the_case_key(self) -> None:
        for commands, shown in ((("a",), "a"), (("a", "b"), "a and b"), (("a", "b", "c"), "a, b and c"),
                                (("x" * 100,), "x" * 89 + "…"), ((), "permission")):
            with self.subTest(shown=shown):
                self.log.unlink(missing_ok=True)
                case_id = rca.open_case(HANDLER._replace(session=shown, commands=commands), OPENED).split()[-1]
                code, out, _ = self.quiet(["start", case_id, "--owner", "builds-unit"])
                self.assertEqual((code, out), (0, "SENT: to the user\n"))
                sent = flags(self.sends()[-1])
                self.assertEqual((sent["--to"], sent["--text-id"], sent["--from"], sent["--key"]),
                                 ("user", "permission.rca_started", "cargo-liner", "rca-" + case_id))
                self.assertEqual(sent["value:commands"], shown)
                self.assertEqual((self.case(case_id)["status"], self.case(case_id)["owner"]), ("started", "builds-unit"))
        values = {key.removeprefix("value:"): value for key, value in flags(self.sends()[-1]).items() if key.startswith("value:")}
        rendered = push_texts.render("permission.rca_started", **values | {"commands": "cargo build -p cargo-handler",
                                                                             "session": "handler-unit"})
        self.assertEqual(rendered.title, "cargo-liner: fixing why handler stops")
        self.assertEqual(rendered.body, "The builds unit finds why handler asked for cargo build -p cargo-handler and fixes "
                         + "the source. A second push comes when it is fixed.")

    def test_fixed_closes_the_case_and_its_push_has_one_period_each(self) -> None:
        _ = rca.open_case(HANDLER._replace(production=""), OPENED)
        code, _, _ = self.quiet(["fixed", "261023-1", "--cause", "a helper ran a raw cargo build.",
                                 "--fix", "the builds unit added\na verify.sh build verb.",
                                 "--commit", "5e2b9c41d07a"])
        self.assertEqual(code, 0)
        sent = flags(self.sends()[-1])
        self.assertEqual((sent["--text-id"], sent["--from"], sent["--key"]), ("permission.rca_fixed", "messaging", "rca-261023-1"))
        values = {key.removeprefix("value:"): value for key, value in sent.items() if key.startswith("value:")}
        rendered = push_texts.render("permission.rca_fixed", **values)
        self.assertEqual(rendered.title, "messaging: fixed why handler stopped")
        self.assertEqual(rendered.body, "Cause: a helper ran a raw cargo build.\n"
                         + "Fix: the builds unit added a verify.sh build verb (5e2b9c41).")
        case = self.case("261023-1")
        self.assertEqual((case["status"], case["commit"]), ("fixed", "5e2b9c41d07a"))
        # A fixed case takes no start; an unknown case is refused.
        self.assertEqual(self.quiet(["start", "261023-1", "--owner", "builds"])[0], 2)
        code, _, err = self.quiet(["start", "261023-9", "--owner", "builds"])
        self.assertEqual(code, 2)
        self.assertIn("no case 261023-9 on this machine", err)

    def test_a_failed_push_exits_1_and_keeps_the_new_status(self) -> None:
        _ = rca.open_case(HANDLER, OPENED)
        with mock.patch.dict(os.environ, {"RCA_TEST_EXIT": "4", "RCA_TEST_OUTPUT": "HELD: not sent"}):
            self.assertEqual(self.quiet(["start", "261023-1", "--owner", "builds"])[:2], (1, "HELD: not sent\n"))
        self.assertEqual(self.case("261023-1")["status"], "started")

    def test_remind_tells_the_owner_once_of_a_case_open_30_minutes(self) -> None:
        _ = rca.open_case(HANDLER, OPENED)
        _ = rca.open_case(HANDLER._replace(session="started"), OPENED)
        _ = self.quiet(["start", "261023-2", "--owner", "builds"])
        sends = len(self.sends())
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rca.remind(OPENED + timedelta(minutes=29))
            self.assertEqual(len(self.sends()), sends)
            with mock.patch.object(subprocess, "Popen", side_effect=OSError("no python")):
                rca.remind(OPENED + timedelta(minutes=30))
            self.assertFalse(self.case("261023-1")["reminded"])
            rca.remind(OPENED + timedelta(minutes=30))
            rca.remind(OPENED + timedelta(minutes=90))
        reminders = self.sends()[sends:]
        self.assertEqual([flags(argv)["--key"] for argv in reminders], ["rca-261023-1-remind"])
        self.assertIn("rca 261023-1 is still open after 30 minutes.", flags(reminders[0])["--text"])
        self.assertTrue(self.case("261023-1")["reminded"])

    def test_list_shows_the_open_and_started_cases(self) -> None:
        self.assertEqual(self.quiet(["list"])[1], "no open cases\n")
        _ = rca.open_case(HANDLER, OPENED)
        _ = rca.open_case(HANDLER._replace(session="started"), OPENED)
        _ = rca.open_case(HANDLER._replace(session="done"), OPENED)
        _ = self.quiet(["start", "261023-2", "--owner", "builds"])
        _ = self.quiet(["fixed", "261023-3", "--cause", "c", "--fix", "f", "--commit", "abc"])
        lines = self.quiet(["list"])[1].splitlines()
        self.assertEqual(lines, [
            "261023-1  open  handler-unit on natedev  10-23 09:30 AST  alerts 1  cargo build -p cargo-handler",
            "261023-2  started owner builds  started on natedev  10-23 09:30 AST  alerts 1  cargo build -p cargo-handler"])


if __name__ == "__main__":
    _ = unittest.main()
