#!/usr/bin/env python3
"""add_showrunner.py through its CLI, with stand-ins for tmux, systemd-run, the session records and the
message sender: nothing real starts and nothing real is sent."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override

SCRIPT = Path(__file__).with_name("add_showrunner.py")
PARENT_SESSION = "11111111-2222-3333-4444-555555555555"
STUB = '''#!__PYTHON__
import json
import os
import subprocess
import sys
from pathlib import Path

state = Path(os.environ["STUB_STATE"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with (state / "events.jsonl").open("a") as events:
    events.write(json.dumps({"command": name, "args": args,
                             "claude_env": sorted(key for key in os.environ if key.startswith("CLAUDE_"))}) + "\\n")
if name == "tmux":
    # A session is a `live-<name>` file.
    target = args[args.index("-t") + 1] if "-t" in args else ""
    if args[:1] == ["has-session"] and not (state / f"live-{target.removeprefix('=')}").exists():
        raise SystemExit(1)
    if args[:1] == ["new-session"]:
        (state / f"live-{args[args.index('-s') + 1]}").touch()
        # What the started session does once it is up: STUB_ON_START maps each file it writes to its text.
        for path, text in json.loads(os.environ.get("STUB_ON_START", "{}")).items():
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(text)
elif name == "systemd-run":
    raise SystemExit(subprocess.run(args[3:], check=False).returncode)
'''
SESSIONS = '''import json, os, pathlib, sys
command, target = sys.argv[1:]
for path in pathlib.Path(os.environ["NOTIFIER_SESSIONS_DIR"]).glob("*.json"):
    record = json.loads(path.read_text())
    if command == "socket" and target == "session:" + record["sessionId"]:
        print(record["messagingSocketPath"])
        raise SystemExit(0)
raise SystemExit(1)
'''
SEND = '''import json, os, sys
with open(os.environ["TEST_SENT"], "a") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
'''


class AddShowrunnerTests(unittest.TestCase):
    root: Path = Path()
    state: Path = Path()
    env: dict[str, str] = {}
    on_start: dict[str, str] = {}
    processes: list[subprocess.Popen[bytes]] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.root / "stub"
        self.state.mkdir()
        bin_path = self.root / "bin"
        bin_path.mkdir()
        for name in ("tmux", "systemd-run"):
            stub = bin_path / name
            _ = stub.write_text(STUB.replace("__PYTHON__", sys.executable), encoding="utf-8")
            stub.chmod(0o755)
        for name in ("home", "sessions"):
            (self.root / name).mkdir()
        _ = (self.root / "sessions.py").write_text(SESSIONS, encoding="utf-8")
        _ = (self.root / "send.py").write_text(SEND, encoding="utf-8")
        self.on_start = {}
        self.processes = []
        self.addCleanup(self.close_processes)
        self.env = {**{key: value for key, value in os.environ.items() if not key.startswith("CLAUDE_")},
                    "HOME": str(self.root / "home"), "STUB_STATE": str(self.state),
                    "PATH": str(bin_path) + os.pathsep + os.environ.get("PATH", ""),
                    "UNIT_LOOKUP_TMUX": str(bin_path / "tmux"),
                    "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
                    "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
                    "SHOWRUNNER_STATE_DIR": str(self.root / "showrunner"),
                    "SHOWRUNNERS_SESSIONS": str(self.root / "sessions.py"),
                    "SHOWRUNNERS_SEND": str(self.root / "send.py"), "TEST_SENT": str(self.root / "sent.jsonl"),
                    # The parent runs the command, so its own session and account are in the caller's environment.
                    "CLAUDE_CODE_SESSION_ID": PARENT_SESSION, "CLAUDE_CONFIG_DIR": str(self.root / "unit-account"),
                    "ADD_SHOWRUNNER_WAIT_S": "0"}
        self.timer("top", PARENT_SESSION)
        self.live(PARENT_SESSION)

    def doc(self, slug: str) -> Path:
        path = self.root / f"{slug}-production.md"
        if not path.exists():
            (self.root / f"{slug}-checkout").mkdir()
            _ = path.write_text("\n".join([
                f"# {slug}", "", f"- **Merge branch:** `{slug}`",
                f"- **Showrunner checkout:** `{self.root / f'{slug}-checkout'}`",
                f"- **Log:** `docs/plans/{slug}-log.md`", "- **User zone:** America/Los_Angeles",
                "- **Updates:** every 15 minutes", "", "## Units", "", "| Unit | Plan |", "| --- | --- |", ""]),
                encoding="utf-8")
        return path

    def timer_conf(self, slug: str) -> Path:
        return self.root / "notifier" / f"showrunner-{slug}" / "conf"

    def timer_text(self, slug: str, session: str) -> str:
        return f"TARGET=session:{session}\nCHECK=zsh /x/production_check.sh {self.doc(slug)}\n"

    def timer(self, slug: str, session: str) -> None:
        self.timer_conf(slug).parent.mkdir(parents=True)
        _ = self.timer_conf(slug).write_text(self.timer_text(slug, session), encoding="utf-8")

    def close_processes(self) -> None:
        for process in self.processes:
            process.terminate()
            _ = process.wait(timeout=2)

    def record(self, session: str) -> tuple[str, str]:
        """A live session's record: where it is kept and what it holds. Its name is read from a live process's."""
        process = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        return (str(self.root / "sessions" / f"{process.pid}.json"),
                json.dumps({"pid": process.pid, "name": f"{session}-name", "sessionId": session,
                            "messagingSocketPath": str(self.root / f"{session}.sock")}))

    def live(self, session: str) -> None:
        path, text = self.record(session)
        _ = Path(path).write_text(text, encoding="utf-8")

    def starts(self, session: str, *, registers: str = "") -> None:
        """The started session comes up as `session`, and registers `registers`'s update timer when named."""
        path, text = self.record(session)
        self.on_start[path] = text
        if registers:
            self.on_start[str(self.timer_conf(registers))] = self.timer_text(registers, session)
        self.env["STUB_ON_START"] = json.dumps(self.on_start)

    def sent_to(self) -> list[str]:
        path = self.root / "sent.jsonl"
        if not path.exists():
            return []
        sent = [cast(list[str], json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines()]
        return [arguments[arguments.index("--to") + 1] for arguments in sent]

    def run_command(self, slug: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), "--production", str(self.doc(slug)),
                               "--model", "opus", "--effort", "high", *extra],
                              env=self.env, capture_output=True, text=True, check=False)

    def events(self, command: str) -> list[dict[str, object]]:
        path = self.state / "events.jsonl"
        if not path.exists():
            return []
        records = [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()]
        return [record for record in records if record["command"] == command]

    def started(self) -> dict[str, object]:
        return next(record for record in self.events("tmux")
                    if cast(list[str], record["args"])[:1] == ["new-session"])

    def parent_of(self, slug: str) -> str:
        path = self.root / "showrunner" / slug / "parent"
        return path.read_text(encoding="utf-8").strip() if path.exists() else ""

    def refused(self, result: subprocess.CompletedProcess[str], cause: str) -> None:
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f"add_showrunner: {cause}", result.stderr)
        self.assertEqual(self.events("systemd-run"), [])

    def test_it_starts_the_session_on_the_showrunner_account_and_attaches_it(self) -> None:
        self.starts("kid-session", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines()[:2],
                         ["started kid in tmux session kid", "kid now reports to top"])
        self.assertEqual(self.parent_of("kid"), "top")
        # The parent ran the command and reads its output; only the child is told.
        self.assertEqual(self.sent_to(), [f"uds:{self.root / 'kid-session.sock'}"])
        launch = cast(list[str], self.events("systemd-run")[0]["args"])
        self.assertEqual(launch[:2], ["--user", "--scope"])
        self.assertRegex(launch[2], r"^--unit=kid-\d+$")
        args = cast(list[str], self.started()["args"])
        self.assertEqual(args[:8], ["new-session", "-d", "-s", "kid", "-c", str(self.root / "kid-checkout"),
                                    "zsh", "-ic"])
        # No unit mark: a marked session would be relayed to as a unit of the parent.
        self.assertNotIn("-e", args)
        # No account prefix and no CLAUDE_* variable: it takes the main login, whatever the caller's units use.
        self.assertEqual(args[8], "ENABLE_TOOL_SEARCH=true command claude --model opus --effort high "
                         + "--remote-control kid -n kid --settings '{\"disableAgentView\": true}' "
                         + f"'/showrunner:produce {self.doc('kid')}'; exec zsh")
        self.assertEqual(self.started()["claude_env"], [])

    def test_resume_starts_in_the_given_directory_with_the_resume_prompt(self) -> None:
        self.starts("abc-123", registers="kid")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        result = self.run_command("kid", "--resume", "abc-123", "--cwd", str(elsewhere))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = cast(list[str], self.started()["args"])
        self.assertEqual(args[5], str(elsewhere))
        self.assertIn(" --resume abc-123 ", args[8])
        self.assertIn(f"'/showrunner:produce {self.doc('kid')} resume'; exec zsh", args[8])
        alone = self.run_command("other", "--resume", "abc-123")
        self.assertEqual(alone.returncode, 2)
        self.assertIn("--resume and --cwd go together", alone.stderr)

    def test_a_timer_left_by_an_ended_session_does_not_stand_in_the_way(self) -> None:
        # The child ran before under this parent; its session ended, and its timer and its edge are still stored.
        self.timer("kid", "abc-123")
        edge = self.root / "showrunner" / "kid" / "parent"
        edge.parent.mkdir(parents=True)
        _ = edge.write_text("top\n", encoding="utf-8")
        self.starts("abc-123")
        result = self.run_command("kid", "--resume", "abc-123", "--cwd", str(self.root))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["started kid in tmux session kid", "kid already reports to top"])
        self.assertEqual(self.sent_to(), [])

    def test_a_stored_timer_alone_is_not_a_showrunner_that_appeared(self) -> None:
        self.timer("kid", "ended-session")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("kid has no running showrunner after 0 s", result.stdout)

    def test_no_showrunner_in_time_leaves_the_session_running_and_unattached(self) -> None:
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("kid has no running showrunner after 0 s, so it is not attached. Its session is still running",
                      result.stdout)
        self.assertIn("showrunners.py attach kid top", result.stdout)
        self.assertTrue((self.state / "live-kid").exists())
        self.assertEqual(self.parent_of("kid"), "")
        self.assertEqual(self.sent_to(), [])

    def test_session_records_that_cannot_be_read_refuse_the_start(self) -> None:
        _ = (self.root / "sessions.py").write_text(
            "import sys\nprint('registry gone', file=sys.stderr)\nraise SystemExit(3)\n", encoding="utf-8")
        self.timer("kid", "some-session")
        self.refused(self.run_command("kid"), "cannot tell whether session:some-session is running: registry gone")

    def test_check_runs_the_refusals_and_starts_nothing(self) -> None:
        result = self.run_command("kid", "--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(),
                         f"check: ok — would start kid in {self.root / 'kid-checkout'} and attach it to top")
        self.assertEqual(self.events("systemd-run"), [])
        (self.state / "live-kid").touch()
        self.refused(self.run_command("kid", "--check"), "a tmux session named kid is already live")

    def test_each_refusal_names_its_cause_and_starts_nothing(self) -> None:
        notes = self.root / "notes.md"
        _ = notes.write_text("# notes\n", encoding="utf-8")
        not_a_doc = subprocess.run([sys.executable, str(SCRIPT), "--production", str(notes), "--model", "opus",
                                    "--effort", "high"], env=self.env, capture_output=True, text=True, check=False)
        self.assertEqual(not_a_doc.returncode, 2, not_a_doc.stdout + not_a_doc.stderr)
        self.assertIn("add_showrunner: ", not_a_doc.stderr)
        self.refused(self.run_command("top"), "top is this session's own production")
        self.timer("running", "running-session")
        self.live("running-session")
        self.refused(self.run_command("running"), "running already has a running showrunner")
        (self.state / "live-kid").touch()
        self.refused(self.run_command("kid"), "a tmux session named kid is already live")

    def test_a_production_above_the_caller_is_refused(self) -> None:
        self.timer("middle", "middle-session")
        self.timer("grand", "grand-session")
        for child, parent in (("top", "middle"), ("middle", "grand")):
            edge = self.root / "showrunner" / child / "parent"
            edge.parent.mkdir(parents=True)
            _ = edge.write_text(parent + "\n", encoding="utf-8")
        self.refused(self.run_command("grand"), "top already reports to grand, directly or through others")

    def test_a_session_that_runs_no_production_is_refused(self) -> None:
        self.env["CLAUDE_CODE_SESSION_ID"] = "no-such-session"
        self.refused(self.run_command("kid"), "this session runs no production, so it has nothing to attach kid to")


if __name__ == "__main__":
    _ = unittest.main()
