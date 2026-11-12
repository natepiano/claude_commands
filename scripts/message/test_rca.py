"""rca.py cases, owner messages and pushes, with a temp state dir and a stub send.py."""
from __future__ import annotations

import io
import fcntl
import json
import math
import os
import socket
import signal
import subprocess
import tempfile
import unittest
from threading import Thread
from time import monotonic
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path

from typing import IO, cast, override
from unittest import mock

from scripts.lib.socket_test_support import short_socket_directory
from scripts.message.process_test_support import OwnedTestProcesses
from scripts.notify import push_texts  # noqa: E402
from scripts.message import rca  # noqa: E402
from scripts.notify import review  # noqa: E402
from scripts.message import send  # noqa: E402
from scripts.message import sessions  # noqa: E402
from scripts.production import session_staff  # noqa: E402
from scripts.production import showrunners, staff, fake_showrunner  # noqa: E402
from scripts.production.staff_test_support import isolated_staff_file, recorded_showrunner, fixture_member  # noqa: E402
from scripts.production.test_showrunners import stalled_session_reader  # noqa: E402
from scripts.notify import silence  # noqa: E402
from scripts.notify.user_action import NoActionRequired  # noqa: E402

OPENED = datetime(2026, 10, 23, 13, 30, tzinfo=timezone.utc)
SHOWRUNNER = "uds:/tmp/cargo-liner-show.sock"
STUB = """import json, os, socket, sys
if os.environ.get('RCA_TEST_BARRIER_SOCKET'):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(5)
        client.connect(os.environ['RCA_TEST_BARRIER_SOCKET'])
        client.sendall((sys.argv[sys.argv.index('--key') + 1] + '\\n').encode())
        assert client.recv(16) == b'delivered'
with open(os.environ['RCA_TEST_LOG'], 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
print(os.environ.get('RCA_TEST_OUTPUT', 'SENT: to the user'))
raise SystemExit(int(os.environ.get('RCA_TEST_EXIT', '0')))
"""
# The stub of subprocess.Popen keeps the real one: subprocess.run calls Popen too.
REAL_POPEN = subprocess.Popen
REAL_SOCKET_FOR = showrunners.socket_for
Stream = int | IO[str] | None
HANDLER = rca.Report("handler", "natedev", "prompt", ("cargo build -p cargo-handler",), "/tmp/t.jsonl",
                     "cargo-liner")
# The handler case as stall_watch.py opens it: its alert's sender and key, and what waits on the session.
ALERTED = HANDLER._replace(waits="The cargo-handler release waits for it", source="stall-watch",
                           key="stall-watch:prompt:cargo-liner:handler")
CAUSE = "a helper ran a build command that needs your permission"
FIX = "helpers use the build command that is always allowed"
# The reviewer's refusal of a fixed text: what failed, a question, and its own rewrite.
REWRITE = review.Draft("cargo-liner", "0", "cargo-liner: fixed why handler hit a check", NoActionRequired(),
                       "In the handler session, the session asked you to allow a build.\n"
                       + "Why: a helper ran a build that needs your permission.\nNow: helpers use the allowed build.", False)
REFUSED = review.Failed(REWRITE, ["plain-words"], failures=(review.RuleFailure("plain-words", "verify.sh"),),
                        questions=("What does the fix change for the user?",))


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


class RcaTests(unittest.TestCase):
    def test_owner_send_uses_the_current_configured_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)
            with mock.patch.dict(os.environ, {"CONF_DIR": str(config)}):
                for seconds in (12, 18):
                    _ = (config / "rca.conf").write_text(f"# range: 0.001..\nsend_seconds={seconds}\n")
                    result = subprocess.CompletedProcess([], 0, "SENT: fixture", "")
                    with mock.patch.object(subprocess, "run", return_value=result) as run:
                        self.assertEqual(rca.send(["--text", "body"]), (True, "SENT: fixture"))
                    self.assertEqual(run.call_args.kwargs["timeout"], seconds)

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.owned: OwnedTestProcesses = OwnedTestProcesses()

    root: Path = Path()
    log: Path = Path()
    owner_processes: list[bool] = []
    # What the fake push review gives, by the draft it reads; None approves the draft.
    verdict: review.ReviewOutcome | None = None
    reviewed: list[review.Draft] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.log = self.root / "sends.jsonl"
        stub = self.root / "send.py"
        _ = stub.write_text(STUB)
        _ = self.enterContext(mock.patch.object(rca, "STATE", self.root / "rca"))
        _ = self.enterContext(mock.patch.object(rca, "SEND", stub))
        _ = self.enterContext(mock.patch.object(rca, "PROJECTS", self.root / "projects"))
        # A fixed clock: a case id carries the day, so a call with no time of its own opens 261023-<n> on any day.
        _ = self.enterContext(mock.patch.object(rca, "now", return_value=OPENED))
        (self.root / "sessions").mkdir()
        removed = ("CLAUDE_CODE_SESSION_ID", "RCA_TEST_EXIT")
        environment = {key: value for key, value in os.environ.items() if key not in removed and not key.startswith("SHOWRUNNER_")}
        self.enterContext(mock.patch.dict(os.environ, environment | {
            "RCA_TEST_LOG": str(self.log), "USER_ZONE": "America/Puerto_Rico",
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"), "NOTIFY_STATE_DIR": str(self.root / "notify")},
            clear=True))
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        _ = (self.root / "staff.json").write_text(json.dumps({"schema": 2, "rows": []}))
        # No test runs the real push review (claude -p).
        self.verdict, self.reviewed = None, []
        _ = self.enterContext(mock.patch.object(review, "review", side_effect=self.review))
        timers = self.root / "notifiers"
        _ = self.enterContext(mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", timers))
        document = self.root / "cargo-production.md"
        _ = document.write_text("# Cargo production\n")
        owner = recorded_showrunner("cargo-liner", document)
        _ = fake_showrunner.write_timer(timers, "cargo-liner", owner.current_session, "UTC", document)

        def live_socket(target: str, *, deadline: float = math.inf) -> str | None:
            del deadline
            return SHOWRUNNER.removeprefix("uds:") if target.removeprefix("session:") == owner.current_session else None

        _ = self.enterContext(mock.patch.object(showrunners, "socket_for", side_effect=live_socket))
        self.owned = OwnedTestProcesses()
        self.addCleanup(self.owned.finish)
        self.owner_processes = []
        _ = self.enterContext(mock.patch.object(subprocess, "Popen", self.popen))

    def popen(self, argv: list[str], *, stdin: Stream = None, stdout: Stream = None, stderr: Stream = None,
              text: bool = False, start_new_session: bool = False) -> subprocess.Popen[str]:
        """Record whether send.py was detached, while running the real isolated sender."""
        self.owner_processes.append(start_new_session)
        child = REAL_POPEN(argv, stdin=stdin, stdout=stdout, stderr=stderr, text=text,
                           start_new_session=start_new_session, env=self.owned.environment())
        self.owned.track(child, group=start_new_session)
        return child

    def review(self, draft: review.Draft, revision: bool, *, limit: float = 0) -> review.ReviewOutcome:
        del revision, limit
        self.reviewed.append(draft)
        return self.verdict or review.Approved(draft)

    def live_session(self, name: str, session_id: str) -> None:
        """A session record for this test's process, with a bound socket, so the record counts as live."""
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        socket_path = Path(self.enterContext(short_socket_directory())) / f"{name}.sock"
        listener.bind(str(socket_path))
        record = {"pid": os.getpid(), "name": name, "sessionId": session_id, "messagingSocketPath": str(socket_path),
                  "updatedAt": 1, "procStart": sessions.process_start(os.getpid()) or ""}
        _ = (self.root / "sessions" / f"{name}.json").write_text(json.dumps(record))
        membership = staff.snapshot()
        if membership.by_current_session(session_id) is None:
            member = staff.StaffRow(staff.StaffId(session_id), "unit", name, staff.StaffRoot(),
                                    current_session=staff.StaffId(session_id))
            _ = self.enterContext(mock.patch.object(staff, "snapshot",
                                                   return_value=replace(membership, rows=(*membership.rows, member))))

    def transcript(self, folder: str, session_id: str, mtime: float) -> Path:
        path = self.root / "projects" / folder / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("{}\n")
        os.utime(path, (mtime, mtime))
        return path

    def caller(self) -> dict[str, str]:
        """The environment of the session that runs rca.py open: a showrunner with a transcript of its own."""
        _ = self.transcript("-home-showrunner", "sid-caller", 3000)
        self.live_session("showrunner", "sid-caller")
        return {"CLAUDE_CODE_SESSION_ID": "sid-caller", "CLAUDE_CONFIG_DIR": str(self.root)}

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
                         ("handler", "cargo-liner", "open", ["cargo build -p cargo-handler"], 1, False,
                          OPENED.isoformat()))
        [owner] = self.sends()
        sent = flags(owner)
        self.assertEqual((sent["--to"], sent["--from"], sent["--key"]), (SHOWRUNNER, "rca", "rca-261023-1"))
        for part in ("rca 261023-1: handler on natedev stopped at a permission prompt.",
                     "Commands: cargo build -p cargo-handler", "Transcript: /tmp/t.jsonl",
                     f"1. Run `{rca.CALL} start 261023-1 --owner <unit you route the fix to>`.",
                     "2. Find where the command came from: a work order, a helper prompt, a skill or a script.",
                     "(an allow rule only when the command is safe and expected)",
                     f"4. After the fix merges, run `{rca.CALL} fixed 261023-1 --cause"):
            self.assertIn(part, sent["--text"])
        self.assertEqual(rca.CALL, "~/.claude/scripts/lib/py ~/.claude/scripts/message/rca.py")
        # The owner send completes before the caller returns, with its outcome in the log.
        self.assertEqual(self.owner_processes, [False])
        logged = (self.root / "rca" / rca.OWNER_LOG).read_text().splitlines()
        self.assertTrue(logged[0].endswith(" rca-261023-1"))
        self.assertEqual(logged[1:], ["SENT: to the user"])

    def test_owner_deliveries_finish_before_the_one_shot_process_group_ends(self) -> None:
        _ = rca.open_case(HANDLER, OPENED)
        socket_root = Path(self.enterContext(short_socket_directory()))
        sender = self.root / "socket-send.py"
        _ = sender.write_text("""import json, os, pathlib, socket, sys
with pathlib.Path(os.environ['OWNER_TEST_PIDS']).open('a') as marker:
    marker.write(str(os.getpid()) + '\\n')
with socket.socket(socket.AF_UNIX) as client:
    client.settimeout(10)
    client.connect(os.environ['OWNER_TEST_SOCKET'])
    client.sendall((json.dumps(sys.argv[1:]) + '\\n').encode())
    assert client.recv(16) == b'delivered'
pathlib.Path(os.environ['OWNER_TEST_RECEIPT']).write_text('delivered')
print('SENT: fake owner socket')
""")
        descendants = self.root / "owner-pids"
        caller = """import json, math, os, pathlib, subprocess, sys
from scripts.production import stall_watch
rca = stall_watch.rca
rca.STATE = pathlib.Path(os.environ['OWNER_TEST_CASE_DIR'])
from scripts.production import showrunners
showrunners.NOTIFIER_STATE_DIR = pathlib.Path(os.environ['OWNER_TEST_CASE_DIR']).parent / 'notifiers'
def live_socket(target: str, *, deadline: float = math.inf) -> str:
    del target, deadline
    return '/private/owner.sock'
showrunners.socket_for = live_socket
if sys.argv[1] == 'rca':
    case = rca.loaded('261023-1')
    failure = rca.tell_owner(case)
else:
    path = stall_watch.STATE_DIR / 'owner.prompt.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    stall_watch.save_prompt(path, stall_watch.PromptWait(session_id='owner', created=0, key='owner-key', since=0, alerted=False, tool='tool', retiring=False))
    stall_watch.send_all([stall_watch.Delivery(path, 'prompt-owner',
        [sys.executable, str(stall_watch.SEND), '--to', 'fake-owner', '--text', 'permission request'])], 60)
    failure = ''
pathlib.Path(os.environ['OWNER_TEST_RETURN']).write_text(json.dumps({
    'failure': failure, 'delivered': pathlib.Path(os.environ['OWNER_TEST_RECEIPT']).exists()}))
# A separate service manager can end the test's caller group without signalling its own group.
subprocess.run([sys.executable, '-c',
    "import signal, sys; "
    "from scripts.lib import process_group; "
    "process_group.send_process_group(int(sys.argv[1]), signal.SIGTERM)",
    str(os.getpgrp())], start_new_session=True, check=True)
"""
        for mode in ("rca", "stall-watch"):
            with self.subTest(sender=mode), socket.socket(socket.AF_UNIX) as receiver:
                address = socket_root / f"{mode}.sock"
                receiver.bind(str(address))
                receiver.listen()
                receiver.settimeout(10)
                receipt = self.root / f"{mode}.receipt"
                returned = self.root / f"{mode}.returned"
                environment = {**os.environ, "HOME": str(self.root),
                    "XDG_STATE_HOME": str(self.root / "state"), "RCA_SEND": str(sender),
                    "STALL_WATCH_SEND": str(sender), "STALL_WATCH_STATE_DIR": str(self.root / "stall"),
                    "OWNER_TEST_SOCKET": str(address), "OWNER_TEST_RECEIPT": str(receipt),
                    "OWNER_TEST_RETURN": str(returned), "OWNER_TEST_CASE_DIR": str(rca.STATE),
                    "OWNER_TEST_PIDS": str(descendants)}
                self.owned.markers.append(descendants)
                with REAL_POPEN([str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", caller, mode], cwd=Path(__file__).resolve().parents[2],
                                env=self.owned.environment(environment), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True) as process:
                    self.owned.track(process, group=True)
                    try:
                        with receiver.accept()[0] as connection:
                            connection.settimeout(10)
                            message = connection.recv(65536)
                            self.assertIn(b"--text", message)
                            connection.sendall(b"delivered")
                        out, err = process.communicate(timeout=10)
                    finally:
                        self.owned.finish()
                self.assertEqual(process.returncode, -signal.SIGTERM, out + err)
                self.assertEqual(json.loads(returned.read_text()), {"failure": "", "delivered": True})
                self.assertEqual(receipt.read_text(), "delivered")

    def test_the_same_session_and_commands_join_the_case_until_it_is_fixed(self) -> None:
        two = rca.Report("modules", "natedev", "auto_mode", ("assign.sh --role implement --to modules-impl",
                                                             "review.sh  --to modules-test"), production="cargo-liner")
        self.assertEqual(rca.open_case(two, OPENED), "rca: opened 261023-1")
        swapped = two._replace(commands=("review.sh --to modules-test", "assign.sh --role implement --to modules-impl"))
        self.assertEqual(rca.open_case(swapped, OPENED), "rca: joined 261023-1")
        self.assertEqual(self.case("261023-1")["alerts"], 2)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(rca.open_case(two._replace(commands=("git push",)), OPENED), "rca: opened 261023-2")
        self.assertEqual(self.quiet(["fixed", "261023-1", "--cause", "c", "--fix", "f", "--commit", "abc"])[0], 0)
        self.assertEqual(rca.open_case(two, OPENED), "rca: opened 261023-3")

    def test_the_owner_is_natedev_without_a_running_showrunner_and_the_mark_names_the_production(self) -> None:
        _ = rca.open_case(HANDLER._replace(production=""), OPENED)
        stopped_doc = self.root / "stopped-production.md"
        _ = stopped_doc.write_text("# Stopped production\n")
        stopped = recorded_showrunner("stopped-production", stopped_doc)
        _ = fake_showrunner.write_timer(self.root / "notifiers", "stopped-production",
                                         stopped.current_session, "UTC", stopped_doc)
        _ = rca.open_case(HANDLER._replace(session="other", production="stopped-production"), OPENED)
        doc = self.root / "cargo-production.md"
        _ = doc.write_text("# Cargo production\n")
        owner = recorded_showrunner("cargo-liner", doc)
        member = fixture_member("unit", name="marked", parent=owner.id,
                                 worktree=staff.PresentWorktree("natedev", self.root / "marked"))
        self.live_session("marked", member.current_session)
        timers = self.root / "notifiers"
        _ = fake_showrunner.write_timer(timers, "cargo-liner", owner.current_session, "UTC", doc)
        with (mock.patch.dict(os.environ, {"SHOWRUNNER_" + "UNIT": "cargo-liner"}),
              mock.patch.object(showrunners, "NOTIFIER_STATE_DIR", timers)):
            _ = rca.open_case(HANDLER._replace(session="marked", production=""), OPENED)
        self.assertEqual([flags(argv)["--to"] for argv in self.sends()], ["natedev", "natedev", SHOWRUNNER])
        self.assertEqual(self.case("261023-3")["production"], "cargo-liner")

    def test_native_identity_uses_the_shared_member_resolver(self) -> None:
        from scripts.production import unit_lookup
        self.assertIs(session_staff.member_sources, unit_lookup.member_sources)

    def test_a_by_hand_open_takes_the_newest_transcript_of_the_session_it_names(self) -> None:
        _ = self.transcript("-home-handler-old", "sid-handler", 1000)
        newest = self.transcript("-home-handler", "sid-handler", 2000)
        self.live_session("handler", "sid-handler")
        with mock.patch.dict(os.environ, self.caller()):
            code, out, _ = self.quiet(["open", "--session", "handler", "--machine", "natedev", "--kind", "prompt",
                                       "--command", "cargo build", "--production", "cargo-liner"])
        self.assertEqual((code, out), (0, "rca: opened 261023-1\n"))
        self.assertEqual(self.case("261023-1")["transcript"], str(newest))
        self.assertIn(f"Transcript: {newest}\n", flags(self.sends()[0])["--text"])

    def test_an_unknown_session_gets_no_transcript_and_never_the_callers(self) -> None:
        with mock.patch.dict(os.environ, self.caller()):
            _ = rca.open_case(HANDLER._replace(session="gone", transcript=""), OPENED)
        self.assertEqual(self.case("261023-1")["transcript"], "")
        text = flags(self.sends()[0])["--text"]
        self.assertIn("Commands: cargo build -p cargo-handler\nFind why this happened", text)
        self.assertNotIn("Transcript", text)

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

    def values(self, argv: list[str]) -> dict[str, str]:
        return {key.removeprefix("value:"): value for key, value in flags(argv).items() if key.startswith("value:")}

    def test_the_kind_of_action_is_named_by_the_first_word_of_each_command(self) -> None:
        for commands, action in ((("cargo build -p cargo-handler",), "a shell command"),
                                 (("write phases/34h/state.json (34h phase record)",), "a file change"),
                                 (("Edit",), "a file change"), (("WebFetch",), "an action"),
                                 (("mcp__brp__brp_launch",), "an action"), (("cargo",), "a shell command"),
                                 (("assign.sh --role implement --to modules-impl", "review.sh --to modules-test"), "2 shell commands"),
                                 (("write a.json", "edit b.py"), "2 file changes"),
                                 (("write a.json", "cargo build"), "2 actions"), ((), "an action"), ((" ",), "an action")):
            with self.subTest(commands=commands):
                self.assertEqual(rca.action_of(commands), action)

    def test_auto_mode_owner_message_and_story_describe_a_refused_command_without_a_session_stop(self) -> None:
        case_id = rca.open_case(HANDLER._replace(kind="auto_mode", commands=("write progress.json",)), OPENED).split()[-1]
        case = self.case(case_id)
        delivered = flags(self.sends()[0])["--text"]
        self.assertIn("handler on natedev had a command refused by auto mode.", delivered)
        self.assertIn("Find why this happened and fix the source.", delivered)
        for text in (delivered, rca.owner_message(case, reminder=True), *rca.story(case).values()):
            with self.subTest(text=text):
                self.assertNotIn("stopped", text)
        self.assertEqual(rca.story(case)["stop"], "auto mode refused a file change")

    def test_start_sends_the_started_push_in_plain_words_with_the_case_key(self) -> None:
        stopped = OPENED - timedelta(minutes=2)
        for report, stop, waits in (
                (ALERTED._replace(stopped=stopped.isoformat()), "the session asked you to allow a shell command",
                 " The cargo-handler release waits for it."),
                (HANDLER._replace(kind="auto_mode", commands=("write a.json", "edit b.py")),
                 "auto mode refused 2 file changes", "")):
            with self.subTest(stop=stop):
                self.log.unlink(missing_ok=True)
                case_id = rca.open_case(report, OPENED).split()[-1]
                code, out, _ = self.quiet(["start", case_id, "--owner", "builds"])
                self.assertEqual((code, out), (0, "SENT: to the user\n"))
                sent = flags(self.sends()[-1])
                self.assertEqual((sent["--to"], sent["--text-id"], sent["--from"], sent["--key"]),
                                 ("user", "permission.rca_started", "cargo-liner", "rca-" + case_id))
                time = silence.shown(stopped if report.stopped else OPENED)
                self.assertTrue(time.endswith(("09:28 AST", "09:30 AST")))
                self.assertEqual(self.values(self.sends()[-1]), {"sender": "cargo-liner", "session": "handler",
                                                                  "time": time, "stop": stop, "waits_line": waits})
                rendered = push_texts.render("permission.rca_started", **self.values(self.sends()[-1]))
                self.assertEqual(rendered.title, "cargo-liner: finding why handler hit a check")
                self.assertEqual(rendered.body, f"At {time} in the handler session, {stop}.{waits} A session"
                                 + " now finds the cause and fixes it, so this does not happen again."
                                 + " A second push comes when the fix is in.")
                # The owner, who the user does not need, stays in the case.
                self.assertEqual((self.case(case_id)["status"], self.case(case_id)["owner"]), ("started", "builds"))
                self.assertNotIn("builds", rendered.body)

    def test_fixed_closes_the_case_and_its_push_has_one_period_each(self) -> None:
        _ = rca.open_case(HANDLER._replace(production="", waits="The cargo-handler release waits for it"), OPENED)
        code, _, _ = self.quiet(["fixed", "261023-1", "--cause", CAUSE + ".",
                                 "--fix", "helpers use the build\ncommand that is always allowed.",
                                 "--commit", "5e2b9c41d07a"])
        self.assertEqual(code, 0)
        # The case had no alert key: no retire runs, only the owner message and the push.
        [_, fixed_push] = self.sends()
        sent = flags(fixed_push)
        self.assertEqual((sent["--text-id"], sent["--from"], sent["--key"]), ("permission.rca_fixed", "messaging", "rca-261023-1"))
        rendered = push_texts.render("permission.rca_fixed", **self.values(fixed_push))
        self.assertEqual(rendered.title, "messaging: fixed why handler hit a check")
        # The waits sentence is in the present tense: the fixed push leaves it out.
        self.assertEqual(rendered.body, f"At {silence.shown(OPENED)} in the handler session, the session asked you to"
                         + f" allow a shell command.\nWhy: {CAUSE}.\nNow: {FIX}.")
        # The push review read the whole fixed text once and approved it.
        self.assertEqual([(draft.title, draft.body) for draft in self.reviewed], [(rendered.title, rendered.body)])
        case = self.case("261023-1")
        self.assertEqual((case["status"], case["commit"], case["refusals"]), ("fixed", "5e2b9c41d07a", 0))
        # A fixed case takes no start; an unknown case is refused.
        self.assertEqual(self.quiet(["start", "261023-1", "--owner", "builds"])[0], 2)
        code, _, err = self.quiet(["start", "261023-9", "--owner", "builds"])
        self.assertEqual(code, 2)
        self.assertIn("no case 261023-9 on this machine", err)

    def test_fixed_retires_the_alert_of_the_case_before_its_push(self) -> None:
        _ = rca.open_case(ALERTED, OPENED)
        code, out, _ = self.quiet(["fixed", "261023-1", "--cause", CAUSE, "--fix", FIX, "--commit", "abc"])
        self.assertEqual((code, out), (0, "SENT: to the user\nSENT: to the user\n"))
        [_, retire, fixed_push] = self.sends()
        self.assertEqual(retire, ["retire", "--to", "user", "--from", "stall-watch",
                                  "--key", "stall-watch:prompt:cargo-liner:handler"])
        self.assertEqual(flags(fixed_push)["--text-id"], "permission.rca_fixed")

    def test_the_first_refusal_of_the_fixed_text_sends_nothing_and_exits_1(self) -> None:
        _ = rca.open_case(ALERTED, OPENED)
        self.verdict = REFUSED
        code, out, _ = self.quiet(["fixed", "261023-1", "--cause", "verify.sh ran cargo build", "--fix", FIX,
                                   "--commit", "abc"])
        self.assertEqual(code, 1)
        for part in ("rca: not sent: the push review refused this text:", "failed: plain-words: verify.sh",
                     "question: What does the fix change for the user?", "The reviewer's rewrite:", REWRITE.body,
                     "Write --cause and --fix again"):
            self.assertIn(part, out)
        # Nothing went out: no retire and no push, only the owner message of the open.
        self.assertEqual(len(self.sends()), 1)
        case = self.case("261023-1")
        self.assertEqual((case["status"], case["refusals"], case["cause"]), ("open", 1, ""))

    def test_a_second_refusal_sends_the_reviewers_rewrite_or_else_the_callers_text(self) -> None:
        for rewrite in (True, False):
            with self.subTest(rewrite=rewrite):
                self.verdict = REFUSED
                case_id = rca.open_case(ALERTED, OPENED).split()[-1]
                self.assertEqual(self.quiet(["fixed", case_id, "--cause", CAUSE, "--fix", FIX, "--commit", "abc"])[0], 1)
                if not rewrite:
                    # The reviewer gave no rewrite: its best version is the caller's own text.
                    self.verdict = review.Failed(self.reviewed[-1], ["plain-words"])
                code, _, _ = self.quiet(["fixed", case_id, "--cause", CAUSE, "--fix", FIX, "--commit", "abc"])
                self.assertEqual(code, 0)
                retire, pushed = self.sends()[-2:]
                self.assertEqual(retire[0], "retire")
                values = self.values(pushed)
                if rewrite:
                    self.assertEqual(flags(pushed)["--text-id"], "permission.rca_fixed_rewritten")
                    self.assertEqual(values, {"sender": "cargo-liner", "title": REWRITE.title, "body": REWRITE.body})
                else:
                    self.assertEqual(flags(pushed)["--text-id"], "permission.rca_fixed")
                    self.assertEqual((values["cause"], values["fix"]), (CAUSE, FIX))
                self.assertEqual(flags(pushed)["--key"], "rca-" + case_id)
                case = self.case(case_id)
                self.assertEqual((case["status"], case["refusals"]), ("fixed", 2))

    def test_a_case_saved_before_the_new_fields_still_loads(self) -> None:
        _ = rca.open_case(HANDLER, OPENED)
        path = rca.case_path("261023-1")
        old = cast(dict[str, object], json.loads(path.read_text()))
        for field in ("stopped", "waits", "alert_source", "alert_key", "refusals"):
            del old[field]
        _ = path.write_text(json.dumps(old))
        case = self.case("261023-1")
        self.assertEqual((case["stopped"], case["waits"], case["alert_source"], case["alert_key"], case["refusals"]),
                         (OPENED.isoformat(), "", "", "", 0))
        self.assertEqual(self.quiet(["start", "261023-1", "--owner", "builds"])[0], 0)
        self.assertEqual(self.quiet(["fixed", "261023-1", "--cause", CAUSE, "--fix", FIX, "--commit", "abc"])[0], 0)
        self.assertNotEqual(self.sends()[-2][0], "retire")

    def test_a_fixed_case_under_silence_leaves_only_its_fixed_push_for_silence_end(self) -> None:
        """The alert and the started push are kept under silence; at its end only the fixed push goes, once."""
        with redirect_stdout(io.StringIO()):
            self.assertEqual(silence.on(None, lambda _argv: 0), 0)
        sender = "cargo-liner"

        def kept(argv: list[str]) -> silence.KeptPush:
            given = flags(argv)
            values = self.values(argv)
            rendered = push_texts.render(given["--text-id"], **values)
            return {"source": given["--from"], "priority": "0", "title": rendered.title, "message": rendered.body,
                    "detail": "", "key": given.get("--key", ""), "text_id": given["--text-id"]}

        def deliver(argv: list[str]) -> tuple[bool, str]:
            """send.py as rca.py runs it: a retire runs the real one; a push is kept, as pushover.py keeps it."""
            if argv[0] == "retire":
                output = io.StringIO()
                with (mock.patch.object(send, "STATE", self.root / "message"),
                      mock.patch.object(send, "run", return_value=(0, "pushover: nothing to cancel\n", "")),
                      redirect_stdout(output)):
                    return send.main(argv) == 0, output.getvalue().strip()
            return isinstance(silence.keep_here(kept(argv)), silence.Kept), "KEPT"

        alert = push_texts.render("stall_watch.permission_prompt", unit="handler", machine="natedev", since="09:28 AST",
                                  request="run cargo build -p cargo-handler")
        alert_argv = push_texts.user_send_argv(alert, sender="stall-watch", key=ALERTED.key)[2:]
        self.assertIsInstance(silence.keep_here(kept(alert_argv)), silence.Kept)
        _ = rca.open_case(ALERTED, OPENED)
        with mock.patch.object(rca, "send", side_effect=deliver):
            self.assertEqual(self.quiet(["start", "261023-1", "--owner", "builds"])[0], 0)
            self.assertEqual(len(silence.read_kept()), 2)
            code, out, _ = self.quiet(["fixed", "261023-1", "--cause", CAUSE, "--fix", FIX, "--commit", "abc"])
        self.assertEqual(code, 0)
        self.assertIn("dropped 1 kept push\n", out)
        delivered: list[silence.KeptPush] = []

        def post(push: silence.KeptPush) -> int:
            delivered.append(push)
            return 0

        claimed = silence.claim()
        assert claimed is not None
        with redirect_stdout(io.StringIO()):
            self.assertEqual(silence.release(*claimed, lambda _argv: 0, post, "silence off"), 0)
        self.assertEqual([(push["source"], push["key"], push["title"]) for push in delivered],
                         [(sender, "rca-261023-1", "cargo-liner: fixed why handler hit a check")])
        self.assertTrue(delivered[0]["message"].endswith(f"Now: {FIX}."))

    def test_a_failed_push_exits_1_and_keeps_the_new_status(self) -> None:
        _ = rca.open_case(HANDLER, OPENED)
        with mock.patch.dict(os.environ, {"RCA_TEST_EXIT": "4", "RCA_TEST_OUTPUT": "HELD: not sent"}):
            self.assertEqual(self.quiet(["start", "261023-1", "--owner", "builds"])[:2], (1, "HELD: not sent\n"))
        self.assertEqual(self.case("261023-1")["status"], "started")

    def test_pending_owner_notices_and_reminders_share_one_concurrent_delivery_budget(self) -> None:
        for number in range(4):
            _ = rca.open_case(HANDLER._replace(session=f"unit-{number}"), OPENED, defer_owner=True)
            if number < 2:
                case = self.case(f"261023-{number + 1}")
                case["owner_told"] = True
                rca.save(case)
        root = Path(self.enterContext(short_socket_directory()))
        address = root / "owners.sock"
        failures: list[Exception] = []
        received: list[str] = []
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(address))
            listener.listen(4)
            listener.settimeout(5)

            def acknowledge_batch() -> None:
                connections: list[socket.socket] = []
                try:
                    while len(connections) < 4:
                        connection = listener.accept()[0]
                        connections.append(connection)
                        connection.settimeout(5)
                        received.append(connection.recv(4096).decode().strip())
                    # No child can finish before every child has reached the receiver.
                    for connection in connections:
                        connection.sendall(b"delivered")
                except Exception as error:
                    failures.append(error)
                finally:
                    for connection in connections:
                        connection.close()

            receiver = Thread(target=acknowledge_batch)
            receiver.start()
            try:
                with mock.patch.dict(os.environ, {"RCA_TEST_BARRIER_SOCKET": str(address)}):
                    rca.remind(OPENED + timedelta(minutes=30), deadline=monotonic() + 5)
            finally:
                receiver.join(timeout=6)
            self.assertFalse(receiver.is_alive())
        self.assertEqual(failures, [])
        self.assertCountEqual(received, ["rca-261023-1-remind", "rca-261023-2-remind",
                                         "rca-261023-3", "rca-261023-4"])
        self.assertTrue(self.case("261023-1")["reminded"])
        self.assertTrue(self.case("261023-2")["reminded"])
        self.assertTrue(self.case("261023-3").get("owner_told"))
        self.assertTrue(self.case("261023-4").get("owner_told"))
        _ = rca.open_case(HANDLER._replace(session="retry"), OPENED, defer_owner=True)
        with redirect_stderr(io.StringIO()):
            rca.remind(OPENED, deadline=monotonic())
        self.assertFalse(self.case("261023-5").get("owner_told"))
        rca.remind(OPENED, deadline=monotonic() + 5)
        self.assertTrue(self.case("261023-5").get("owner_told"))
        self.assertEqual(sum(flags(argv)["--key"] == "rca-261023-5" for argv in self.sends()), 1)

    def test_a_contended_case_lock_stops_at_the_deadline_and_keeps_the_notice_pending(self) -> None:
        _ = rca.open_case(HANDLER, OPENED, defer_owner=True)
        errors = io.StringIO()
        with (rca.STATE / ".lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            began = monotonic()
            # The lock deadline must cover lock contention, without interpreter startup.
            with (redirect_stderr(errors),
                  mock.patch.object(rca, "send", return_value=(True, "SENT: fixture"))):
                rca.remind(OPENED, deadline=began + 0.2)
            self.assertLess(monotonic() - began, 1.5)
        self.assertFalse(self.case("261023-1").get("owner_told", True))
        self.assertEqual(errors.getvalue().count("owner delivery retained for retry"), 1)
        self.assertIn("case lock deadline exhausted", errors.getvalue())
        rca.remind(OPENED, deadline=monotonic() + 5)
        self.assertTrue(self.case("261023-1").get("owner_told"))

    def test_stalled_owner_lookup_ends_at_the_deadline_and_keeps_the_notice_pending(self) -> None:
        _ = rca.open_case(HANDLER, OPENED, defer_owner=True)
        reader = stalled_session_reader(self.root)
        errors = io.StringIO()
        began = monotonic()
        with (mock.patch.object(showrunners, "socket_for", wraps=REAL_SOCKET_FOR),
              mock.patch.object(showrunners, "SESSIONS", reader), redirect_stderr(errors)):
            rca.remind(OPENED, deadline=began + 0.15)
        self.assertLess(monotonic() - began, 1.5)
        self.assertEqual(errors.getvalue().count("owner delivery retained for retry"), 1)
        self.assertIn("lookup timed out", errors.getvalue())
        self.assertFalse(self.case("261023-1").get("owner_told", True))
        self.assertEqual(self.sends(), [])

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
            "261023-1  open  handler on natedev  10-23 09:30 AST  alerts 1  cargo build -p cargo-handler",
            "261023-2  started owner builds  started on natedev  10-23 09:30 AST  alerts 1  cargo build -p cargo-handler"])


if __name__ == "__main__":
    _ = unittest.main()
