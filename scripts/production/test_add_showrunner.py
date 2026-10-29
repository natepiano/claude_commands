#!/usr/bin/env python3
"""add_showrunner.py through its CLI, with fake herdr, the session records and the
message sender: nothing real starts and nothing real is sent."""
from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast, override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.accounts import accounts
from scripts.production import add_showrunner
import add_unit
import showrunners
import usage_store
import herdr as runtime
from scripts.herdr import herdr
from scripts.herdr.fake_herdr import CommandResponse

# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

SCRIPT = Path(__file__).with_name("add_showrunner.py")
SHARED = herdr.SHARED_SESSION
PARENT_SESSION = "11111111-2222-3333-4444-555555555555"
STUB = '''#!__PYTHON__
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

state = Path(os.environ["STUB_STATE"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with (state / "events.jsonl").open("a") as events:
    events.write(json.dumps({"command": name, "args": args,
                             "claude_env": sorted(key for key in os.environ if key.startswith("CLAUDE_"))}) + "\\n")
if name == "systemd-run":
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
    herdr_overrides: dict[str, CommandResponse] = {}
    processes: list[subprocess.Popen[bytes]] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.state = self.root / "stub"
        self.state.mkdir()
        bin_path = self.root / "bin"
        bin_path.mkdir()
        for name in ("systemd-run",):
            stub = bin_path / name
            _ = stub.write_text(STUB.replace("__PYTHON__", sys.executable), encoding="utf-8")
            stub.chmod(0o755)
        for name in ("home", "sessions"):
            (self.root / name).mkdir()
        _ = (self.root / "sessions.py").write_text(SESSIONS, encoding="utf-8")
        _ = (self.root / "send.py").write_text(SEND, encoding="utf-8")
        fake = bin_path / "submitting-herdr"
        source = (SCRIPT.parents[1] / "herdr/fake_herdr.py").read_text()
        source = source.replace('responses.get("default", {"stdout": "", "code": 0})',
            'responses.get(" ".join(argv[2:4]), responses.get("default", {"stdout": "", "code": 0}))')
        source = source.replace('raise SystemExit(main(sys.argv[1:]))', 'raise SystemExit(submitting_main(sys.argv[1:]))')
        hook = r'''def submitting_main(argv):
    result = main(argv)
    if result == 0 and argv[2:4] == ["agent", "prompt"]:
        from datetime import datetime, timezone
        calls = [json.loads(line) for line in Path(os.environ["FAKE_HERDR_LOG"]).read_text().splitlines()]
        start = next(row for row in reversed(calls) if row[:2] == argv[:2] and row[2:4] == ["agent", "start"])
        flag = "--resume" if "--resume" in start else "--session-id"
        session_id = start[start.index(flag) + 1]
        config = next(value.split("=", 1)[1] for row in reversed(calls) for value in row
                      if row[:2] == argv[:2] and value.startswith("CLAUDE_CONFIG_DIR="))
        path = Path(config) / "projects/fixture" / (session_id + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "user", "timestamp": datetime.now(timezone.utc).isoformat(),
                                   "message": {"role": "user", "content": argv[-1]}}) + "\n")
    return result

'''
        source = source.replace('if __name__ == "__main__":', hook + 'if __name__ == "__main__":')
        _ = fake.write_text(source)
        fake.chmod(0o755)
        self.on_start = {}
        self.herdr_overrides = {}
        self.processes = []
        self.addCleanup(self.close_processes)
        self.env = {**{key: value for key, value in os.environ.items() if not key.startswith("CLAUDE_")},
                    "HERDR_PLATFORM": "linux",
                    "HOME": str(self.root / "home"), "STUB_STATE": str(self.state),
                    "PATH": str(bin_path) + os.pathsep + os.environ.get("PATH", ""),
                    "HERDR_BIN": str(fake),
                    "SYSTEMD_RUN": str(fake),
                    "SYSTEMCTL": str(fake),
                    "HERDR_PROC": str(self.root / "proc"),
                    "FAKE_HERDR_LOG": str(self.state / "herdr.jsonl"),
                    "FAKE_HERDR_RESPONSES": str(self.state / "herdr-responses.json"),
                    "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
                    "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
                    "SHOWRUNNER_STATE_DIR": str(self.root / "showrunner"),
                    "SHOWRUNNERS_SESSIONS": str(self.root / "sessions.py"),
                    "SHOWRUNNERS_SEND": str(self.root / "send.py"), "TEST_SENT": str(self.root / "sent.jsonl"),
                    # The parent runs the command, so its own session and account are in the caller's environment.
                    "CLAUDE_CODE_SESSION_ID": PARENT_SESSION, "CLAUDE_CONFIG_DIR": str(self.root / "unit-account"),
                    "ADD_SHOWRUNNER_WAIT_S": "0", "ADD_SHOWRUNNER_POLL_S": "0.01",
                    "ADD_SHOWRUNNER_CLAUDE_JSON": str(self.root / "claude.json")}
        # The folders these tests start sessions in are trusted; the trust tests change that.
        self.trust(self.root, self.root / "kid-checkout", self.root / "elsewhere")
        home = Path(self.env["HOME"])
        (home / ".claude").mkdir()
        _ = (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "main@example.invalid"}}))
        notes = home / "notes"
        notes.mkdir()
        self.env["AGENT_NOTES_DIR"] = str(notes)
        _ = (notes / "claude2.md").write_text("---\nlogin: main@example.invalid\n---\n")
        now = datetime.now(timezone.utc)
        file = home / ".local/state/agent-notes/usage.json"
        file.parent.mkdir(parents=True)
        _ = file.write_text(json.dumps({"main@example.invalid": {"weekly": {"left": 1,
            "at": now.isoformat(), "resets": (now + timedelta(days=2)).isoformat()}}}))
        with patch.dict(os.environ, self.env):
            self.assertEqual(accounts.setup("claude2"), [])
            _ = (accounts.account_dir("claude2") / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "main@example.invalid"}}))
        _ = self.configure_groups()
        self.timer("top", PARENT_SESSION)
        self.live(PARENT_SESSION, "top")

    def configure_groups(self, *, units: str = "claude2", showrunners: str = "claude2") -> Path:
        path = Path(self.env["HOME"]) / ".local/state/accounts/groups.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"units": units, "showrunners": showrunners}))
        return path

    def trust(self, *folders: Path, extra: dict[str, object] | None = None) -> None:
        """The showrunner account's config, trusting exactly `folders`, with `extra` fields beside them."""
        projects = {str(folder.resolve()): {"hasTrustDialogAccepted": True, "allowedTools": ["SECRET-TOOL"]}
                    for folder in folders}
        _ = (self.root / "claude.json").write_text(json.dumps({**(extra or {}), "projects": projects}), encoding="utf-8")

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
            _ = process.wait(timeout=TEST_HANG_GUARD_SECS)

    def record(self, session: str, name: str = "") -> tuple[str, str]:
        """A live session's record: where it is kept and what it holds. Its name is read from a live process's;
        a showrunner's name is its production's slug."""
        process = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        return (str(self.root / "sessions" / f"{process.pid}.json"),
                json.dumps({"pid": process.pid, "name": name or f"{session}-name", "sessionId": session,
                            "messagingSocketPath": str(self.root / f"{session}.sock")}))

    def live(self, session: str, name: str = "") -> None:
        path, text = self.record(session, name)
        _ = Path(path).write_text(text, encoding="utf-8")

    def starts(self, session: str, *, registers: str = "") -> None:
        """The started session comes up as `session`, and registers `registers`'s update timer when named."""
        path, text = self.record(session, registers)
        self.on_start[path] = text
        if registers:
            self.on_start[str(self.timer_conf(registers))] = self.timer_text(registers, session)
        for destination in self.on_start:
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
        if registers and not self.timer_conf(registers).exists():
            # Fake herdr writes files into existing folders. Before launch this
            # timer belongs to an ended session, so it does not claim the child.
            _ = self.timer_conf(registers).write_text(self.timer_text(registers, "not-started"))

    def sent_to(self) -> list[str]:
        path = self.root / "sent.jsonl"
        if not path.exists():
            return []
        sent = [cast(list[str], json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines()]
        return [arguments[arguments.index("--to") + 1] for arguments in sent]

    def run_command(self, slug: str, *extra: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        self.prepare_herdr(slug, *extra)
        return subprocess.run([sys.executable, str(SCRIPT), "--production", str(self.doc(slug)),
                               "--model", "opus", "--effort", "high", *extra],
                              env=self.env, capture_output=True, text=True, check=False, cwd=cwd)

    def prepare_herdr(self, slug: str, *extra: str) -> None:
        """Fake herdr answers a launch of `slug`'s showrunner as a tab of the shared session, which has
        a boot unit; the agent prompt brings the session up. While the shared session is live, it holds
        a tab named `slug`."""
        sessions = [herdr.SessionEntry(name=path.name.removeprefix("live-"), running=True, default=False,
                    session_dir="/fixture", socket_path="/fixture/socket") for path in self.state.glob("live-*")]
        responses: dict[str, CommandResponse | list[CommandResponse]] = {
            "--session default session list --json": CommandResponse(code=0, stdout=json.dumps(
                herdr.SessionListResult(sessions=sessions))),
            f"--session {SHARED} status server": [CommandResponse(code=0, stdout="status: stopped\n"),
                                                 CommandResponse(code=0, stdout="status: running\n")],
            f"--user show -p FragmentPath --value herdr-{SHARED}.service": CommandResponse(
                code=0, stdout=f"{self.env['HOME']}/.config/systemd/user/herdr-{SHARED}.service\n"),
            f"--session {SHARED} workspace list": CommandResponse(
                code=0, stdout=json.dumps({"result": {"workspaces": []}})),
            f"--session {SHARED} pane list": CommandResponse(code=0, stdout=json.dumps({
                "result": {"panes": [{"pane_id": "w1:p1", "tab_id": "w1:t1", "label": slug}]}})),
            "default": CommandResponse(code=0, stdout=json.dumps({"result": {
                "root_pane": {"pane_id": "w1:p1"}, "tab": {"tab_id": "w1:t1"}, "tabs": []}})),
        }
        responses[f"--session {SHARED} agent get w1:p1"] = CommandResponse(code=0, stdout=json.dumps({
            "result": {"agent": {"agent_status": "idle", "interactive_ready": True}}}))
        responses[f"--session {SHARED} pane read w1:p1 --source visible --format ansi"] = CommandResponse(
            code=0, stdout="────────\n❯ \n────────")
        prompt = f"/showrunner:produce {self.doc(slug)}" + (" resume" if "--resume" in extra else "")
        responses[f"--session {SHARED} agent prompt w1:p1 {prompt}"] = CommandResponse(
            code=0, stdout="", write_files={**self.on_start, str(self.state / f"live-{SHARED}"): ""})
        responses.update(self.herdr_overrides)
        _ = Path(self.env["FAKE_HERDR_RESPONSES"]).write_text(json.dumps(responses))

    def events(self, command: str) -> list[dict[str, object]]:
        path = self.state / "events.jsonl"
        if not path.exists():
            return []
        records = [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()]
        return [record for record in records if record["command"] == command]

    def herdr_calls(self) -> list[list[str]]:
        path = Path(self.env["FAKE_HERDR_LOG"])
        return [cast(list[str], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def started(self) -> list[str]:
        return next(row for row in self.herdr_calls() if row[2:4] == ["agent", "start"])

    def opened(self) -> list[str]:
        return next(row for row in self.herdr_calls() if row[2:4] == ["workspace", "create"])

    def parent_of(self, slug: str) -> str:
        path = self.root / "showrunner" / slug / "parent"
        return path.read_text(encoding="utf-8").strip() if path.exists() else ""

    def refused(self, result: subprocess.CompletedProcess[str], cause: str) -> None:
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("add_showrunner:", result.stderr)
        self.assertIn(cause, result.stderr)
        self.assertFalse(any("workspace" in row or "server" in row or "agent" in row
                             for row in self.herdr_calls()))

    def test_new_showrunner_launches_on_picked_default_account_and_attaches_it(self) -> None:
        self.starts("kid-session", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines()[:2],
                         ["started kid: herdr session attach agents, workspace kid, tab kid", "kid now reports to top"])
        self.assertEqual(self.parent_of("kid"), "top")
        # The parent ran the command and reads its output; only the child is told.
        self.assertEqual(self.sent_to(), [f"uds:{self.root / 'kid-session.sock'}"])
        calls = self.herdr_calls()
        self.assertIn(["--user", "start", "herdr-agents.service"], calls)
        opened = self.opened()
        self.assertEqual(opened[:8], ["--session", "agents", "workspace", "create", "--label", "kid",
                                     "--cwd", str(self.root / "kid-checkout")])
        environment = dict(value.split("=", 1) for flag, value in zip(opened[8:], opened[9:]) if flag == "--env")
        self.assertEqual({key: value for key, value in environment.items()
                          if key.startswith(("CLAUDE_", "SHOWRUNNER_", "HERDR_")) or key == "ENABLE_TOOL_SEARCH"},
                         {"ENABLE_TOOL_SEARCH": "true", "HERDR_AGENT_PANE": "1",
                          "CLAUDE_CONFIG_DIR": str(Path(self.env["HOME"]) / ".claude2"),
                          "CLAUDE_CODE_REMOTE_MEMORY_DIR": str(Path(self.env["HOME"]) / ".claude")})
        self.assertEqual(opened[-1], "--no-focus")
        started = self.started()
        self.assertEqual(started[started.index("--") + 1:], ["--model", "opus", "--effort", "high",
            "--session-id", started[started.index("--session-id") + 1], "--remote-control", "kid", "-n", "kid", "--settings", '{"disableAgentView": true}'])
        self.assertEqual([row for row in calls if row[2:4] == ["agent", "prompt"]], [[
            "--session", "agents", "agent", "prompt", "w1:p1", f"/showrunner:produce {self.doc('kid')}"]])

    def test_resume_starts_in_the_given_directory_with_the_resume_prompt(self) -> None:
        self.starts("abc-123", registers="kid")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        result = self.run_command("kid", "--resume", "abc-123", "--cwd", str(elsewhere), "--account", "claude2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.opened()[7], str(elsewhere))
        args = self.started()
        self.assertEqual(args[args.index("--resume") + 1], "abc-123")
        self.assertIn(["--session", "agents", "agent", "prompt", "w1:p1",
                       f"/showrunner:produce {self.doc('kid')} resume"], self.herdr_calls())
        alone = self.run_command("other", "--resume", "abc-123")
        self.assertEqual(alone.returncode, 2)
        self.assertIn("--resume and --cwd go together", alone.stderr)

    def named_account(self) -> Path:
        with patch.dict(os.environ, self.env, clear=True):
            notes = Path(self.env["AGENT_NOTES_DIR"])
            _ = (notes / "claude3.md").write_text("---\nlogin: target@example.invalid\n---\n")
            self.assertEqual(accounts.setup("claude3"), [])
            directory = accounts.account_dir("claude3")
            _ = (directory / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "target@example.invalid"}}))
            # The trust checks use a separate fixture, so selection does not expose any live home.
            _ = (directory / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "target@example.invalid"},
                "projects": {str(self.root): {"hasTrustDialogAccepted": True},
                             str(self.root / "kid-checkout"): {"hasTrustDialogAccepted": True}}}))
        _ = self.configure_groups(showrunners="claude3")
        return directory

    def test_showrunner_shared_tab_retries_enter_with_the_selected_session_transcript(self) -> None:
        child = add_unit.read_production(self.doc("kid"))
        selected = accounts.NamedAccount("claude3", self.root / "selected")
        transcript = selected.dir / "projects/fixture/resumed-id.jsonl"
        transcript.parent.mkdir(parents=True)
        text = f"/showrunner:produce {child.doc} resume"
        elapsed = 0.0
        screen = "────────\n❯ \n────────"

        def advance(seconds: float) -> None:
            nonlocal elapsed
            elapsed += seconds

        def visible(*_args: str) -> str:
            return screen

        def turn(*, meta: bool) -> None:
            with transcript.open("a") as output:
                _ = output.write(json.dumps({"type": "user", "isMeta": meta,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "message": {"role": "user", "content": text}}) + "\n")

        def typed(_client: herdr.Herdr, pane: str, prompt: str) -> None:
            nonlocal screen
            self.assertEqual((pane, prompt), ("destination:p1", text))
            screen = f"────────\n❯ {text}\n────────"
            turn(meta=True)

        def enter(_client: herdr.Herdr, pane: str, *keys: str) -> None:
            nonlocal screen
            self.assertEqual((pane, keys), ("destination:p1", ("Enter",)))
            screen = "────────\n❯ \n────────"
            turn(meta=False)

        with (patch.object(runtime, "shared_session", return_value="agents"),
              patch.object(runtime, "start_server"),
              patch.object(runtime, "open_tab", return_value="destination:p1") as tab,
              patch.object(runtime, "time", SimpleNamespace(monotonic=lambda: elapsed, sleep=advance)),
              patch.object(runtime.Herdr, "panes", return_value=[{"pane_id": "destination:p1", "label": "kid"}]),
              patch.object(runtime.Herdr, "start_command", return_value=subprocess.CompletedProcess([], 0, "", "")),
              patch.object(runtime.Herdr, "readiness", return_value={"agent_status": "idle", "interactive_ready": True}) as readiness,
              patch.object(runtime.Herdr, "prompt", autospec=True, side_effect=typed) as prompt,
              patch.object(runtime.Herdr, "run", side_effect=visible),
              patch.object(runtime.Herdr, "send_keys", autospec=True, side_effect=enter) as keys):
            outcome = add_showrunner.launch(child, "opus", "high", "resumed-id", self.root, selected)
        self.assertIsInstance(outcome, runtime.PromptSubmitted)
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(keys.call_count, 1)
        readiness.assert_called_once_with("destination:p1")
        self.assertEqual(tab.call_args.args[:3], ("agents", "kid", "kid"))
        self.assertGreaterEqual(elapsed, 5)

    def test_unsubmitted_showrunner_is_named_and_never_attached_even_if_live(self) -> None:
        _ = self.doc("kid")
        for outcome in (runtime.PromptNotSubmitted("fixture lost input"), runtime.PromptSubmissionNotRequired()):
            with (self.subTest(outcome=outcome), patch.dict(os.environ, self.env, clear=True),
                  patch.object(showrunners, "own_slug", return_value="top"),
                  patch.object(add_showrunner, "checked"),
                  patch.object(runtime, "shared_session", return_value="agents"),
                  patch.object(runtime, "start_server"), patch.object(runtime, "open_tab", return_value="destination:p1"),
                  patch.object(runtime, "start_agent", return_value=outcome) as start,
                  patch.object(add_showrunner, "showrunner_appeared", return_value=True) as appeared,
                  redirect_stderr(io.StringIO()) as error):
                code = add_showrunner.main(["--production", str(self.doc("kid")), "--model", "opus", "--effort", "high"])
            self.assertEqual(code, add_showrunner.SESSION_NOT_READY)
            self.assertIn("kid: opening prompt not submitted", error.getvalue())
            appeared.assert_not_called()
            opening = cast(runtime.OpeningPrompt, start.call_args.args[4])
            assert isinstance(opening.transcript, runtime.SessionTranscript)
            self.assertEqual(opening.transcript.config_dir, Path(self.env["HOME"]) / ".claude2")
            arguments = cast(list[str], start.call_args.args[3])
            self.assertEqual(opening.transcript.session_id, arguments[arguments.index("--session-id") + 1])
        self.assertEqual(self.parent_of("kid"), "")

    def test_new_showrunner_reads_stored_showrunners_account_despite_better_unit_reading(self) -> None:
        directory = self.named_account()
        _ = self.env.pop("ADD_SHOWRUNNER_CLAUDE_JSON")
        home = Path(self.env["HOME"])
        path = home / ".local/state/accounts/groups.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({"units": "claude1", "showrunners": "claude3"}))
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(accounts.setup("claude1"), [])
            caller = accounts.account_dir("claude1")
            _ = (caller / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "unit@example.invalid"}}))
        self.env["CLAUDE_CONFIG_DIR"] = str(caller)
        now = datetime.now(timezone.utc)
        saved = {"main@example.invalid": {"weekly": {"left": 99, "at": now.isoformat(),
                  "resets": (now + timedelta(hours=1)).isoformat()}},
                 "target@example.invalid": {"weekly": {"left": 0, "at": now.isoformat(),
                  "resets": (now + timedelta(days=4)).isoformat()}}}
        _ = (home / ".local/state/agent-notes/usage.json").write_text(json.dumps(saved))
        self.starts("kid-session", registers="kid")
        before = path.read_bytes()
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(directory), self.opened())
        self.assertIn("CLAUDE_CODE_REMOTE_MEMORY_DIR=" + str(Path(self.env["HOME"]) / ".claude"), self.opened())
        self.assertEqual(path.read_bytes(), before)

    def test_unconfigured_showrunners_refuse_new_and_resume_without_group_write(self) -> None:
        """F004: live parent identity and readings cannot supply missing configuration."""
        path = self.configure_groups()
        path.unlink()
        for options in ((), ("--resume", "abc-123", "--cwd", str(self.root))):
            with self.subTest(options=options):
                result = self.run_command("kid", *options)
                self.refused(result, "accounts.py move showrunners <claudeN>")
                self.assertFalse(path.exists())
                self.assertEqual(self.events("systemd-run"), [])

    def test_showrunner_launch_accepts_one_account_shared_with_units(self) -> None:
        """F004: both launch groups may use the same configured account."""
        directory = self.named_account()
        _ = self.configure_groups(units="claude3", showrunners="claude3")
        self.starts("kid-session", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(directory), self.opened())

    def test_new_showrunner_refuses_configured_account_with_wrong_login(self) -> None:
        """F006: a trusted directory cannot bypass the configured account's readiness."""
        directory = self.named_account()
        _ = (directory / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "other@example.invalid"},
            "projects": {str(self.root / "kid-checkout"): {"hasTrustDialogAccepted": True}}}))
        path = Path(self.env["HOME"]) / ".local/state/accounts/groups.json"
        before = path.read_bytes()
        result = self.run_command("kid")
        self.refused(result, "logged in as other@example.invalid, not claude3")
        self.assertEqual(path.read_bytes(), before)

    def test_new_showrunner_repairs_a_wrong_shared_link_before_launching(self) -> None:
        directory = self.named_account()
        linked = directory / "projects"
        linked.unlink()
        linked.symlink_to(Path(self.env["HOME"]) / "missing-projects")
        self.starts("kid-session", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(linked.is_symlink())
        self.assertEqual(linked.resolve(), Path(self.env["HOME"]) / ".claude/projects")
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(directory), self.opened())
        self.assertEqual(self.parent_of("kid"), "top")

    def test_new_showrunner_refuses_a_real_file_against_an_existing_shared_folder(self) -> None:
        directory = self.named_account()
        local = directory / "projects"
        local.unlink()
        _ = local.write_text("local projects data")
        result = self.run_command("kid")
        self.refused(result, f"{local} is a real file; move it aside or merge it into ~/.claude")
        self.assertFalse(local.is_symlink())
        self.assertEqual(local.read_text(), "local projects data")

    def test_picked_named_account_refuses_untrusted_folder_despite_default_trust(self) -> None:
        directory = self.named_account()
        _ = self.env.pop("ADD_SHOWRUNNER_CLAUDE_JSON")
        home = Path(self.env["HOME"])
        _ = (home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "main@example.invalid"},
            "projects": {str(self.root / "kid-checkout"): {"hasTrustDialogAccepted": True}}}))
        _ = (directory / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "target@example.invalid"}, "projects": {}}))
        file = home / ".local/state/agent-notes/usage.json"
        saved = cast(dict[str, usage_store.StoredUsage], json.loads(file.read_text()))
        now = datetime.now(timezone.utc)
        saved["target@example.invalid"] = {"weekly": {"left": 1, "at": now.isoformat(),
            "resets": (now + timedelta(days=1)).isoformat()}}
        _ = file.write_text(json.dumps(saved))
        result = self.run_command("kid")
        self.refused(result, f"kid: Claude is not trusted in {self.root / 'kid-checkout'} on claude3. Run:")
        self.assertIn(f"CLAUDE_CONFIG_DIR={shlex.quote(str(directory))}", result.stderr)

    def test_showrunner_resume_refuses_unconfigured_directory_before_reading_its_trust(self) -> None:
        """F004: a retained directory cannot override group configuration."""
        directory = Path(self.env["HOME"]) / "retained account"
        directory.mkdir()
        _ = (directory / ".claude.json").write_text("SECRET malformed account contents")
        result = self.run_command("kid", "--resume", "abc-123", "--cwd", str(self.root),
                                  "--config-dir", str(directory))
        self.refused(result, "claude2")
        self.assertNotIn("SECRET", result.stderr)

    def test_resume_refuses_an_unrelated_tab_of_the_same_name_without_closing_it(self) -> None:
        (self.state / "live-agents").touch()
        self.live("unrelated-conversation")
        result = self.run_command("kid", "--resume", "ended-conversation", "--cwd", str(self.root),
                                  "--account", "claude2")
        self.refused(result, "a tab named kid is already open in herdr session agents")
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertFalse(any(row[2:4] in (["server", "stop"], ["tab", "close"]) for row in self.herdr_calls()))
        self.assertTrue((self.state / "live-agents").exists())

    def test_showrunner_resume_refuses_directory_outside_configured_account(self) -> None:
        """F004: a valid retained directory is still not the configured account."""
        directory = Path(self.env["HOME"]) / "retained account"
        directory.mkdir()
        _ = (directory / ".claude.json").write_text(json.dumps({
            "projects": {str(self.root): {"hasTrustDialogAccepted": True}}}))
        _ = self.named_account()
        result = self.run_command("kid", "--resume", "abc-123", "--cwd", str(self.root),
                                  "--config-dir", str(directory))
        self.refused(result, "claude3")
        self.assertEqual(self.events("systemd-run"), [])

    def test_showrunner_resume_accepts_relative_directory_of_configured_account(self) -> None:
        """F004: explicit matching directories are accepted after path resolution."""
        home = Path(self.env["HOME"])
        directory = self.named_account()
        self.starts("abc-123", registers="kid")
        result = self.run_command("kid", "--resume", "abc-123", "--cwd", str(self.root),
                                  "--config-dir", ".claude3", cwd=home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(directory), self.opened())

    def test_showrunner_config_directory_requires_resume_and_excludes_account(self) -> None:
        directory = Path(self.env["HOME"]) / "retained"
        directory.mkdir()
        for options in (("--config-dir", str(directory)),
                        ("--resume", "abc-123", "--cwd", str(self.root), "--config-dir", str(directory),
                         "--account", "claude2")):
            with self.subTest(options=options):
                result = self.run_command("kid", *options)
                self.assertEqual(result.returncode, 2)
                self.assertIn("--config-dir", result.stderr)
                self.assertEqual(self.events("systemd-run"), [])

    def test_showrunner_resume_uses_configured_account_instead_of_recorded_default(self) -> None:
        """F004: resume selection ignores the previous account."""
        directory = self.named_account()
        _ = (self.state / "kid@claude_config_dir").write_text(str(Path(self.env["HOME"]) / ".claude"))
        self.starts("abc-123", registers="kid")
        result = self.run_command("kid", "--resume", "abc-123", "--cwd", str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(directory), self.opened())

    def resume_record(self) -> None:
        _ = (self.root / "sessions/resume.json").write_text(json.dumps({
            "pid": 991001,
            "sessionId": "abc-123",
            "name": "kid",
            "messagingSocketPath": "",
            "updatedAt": 1,
            "tmux": "",
        }))

    def test_resume_uses_the_configured_showrunners_account(self) -> None:
        directory = self.named_account()
        child = add_unit.read_production(self.doc("kid"))
        self.resume_record()
        with patch.dict(os.environ, self.env, clear=True), \
                patch.object(accounts, "role_account", wraps=accounts.role_account) as role:
            selected = add_showrunner.account_for_launch(child, "abc-123")
        self.assertEqual(selected, accounts.NamedAccount("claude3", directory))
        role.assert_called_once_with("showrunner")

    def test_check_validates_the_destination_without_returning_a_binary(self) -> None:
        child = add_unit.read_production(self.doc("kid"))
        with patch.dict(os.environ, self.env, clear=True), \
                patch.object(add_showrunner, "running", return_value=False), \
                patch.object(showrunners, "descendants_of", return_value=[]), \
                patch.object(add_showrunner, "herdr_tab_open", return_value=False) as destination:
            result = add_showrunner.checked(child, "top", config_dir=Path(self.env["HOME"]) / ".claude2")
        self.assertIsNone(result)
        destination.assert_called_once_with("kid", add_showrunner.NoEndingTab())

    def test_recorded_herdr_resume_without_an_account_passes_check_using_configured_group(self) -> None:
        self.resume_record()
        for pid, parent, argv, environment in (
            (991000, 0, ["herdr", "--session", "kid", "server"], {}),
            (991001, 991000, ["claude"], {"HERDR_PANE_ID": "w1:p1"}),
        ):
            process = Path(self.env["HERDR_PROC"]) / str(pid)
            process.mkdir(parents=True)
            _ = (process / "stat").write_text(f"{pid} (fixture) S {parent} 0 0")
            _ = (process / "cmdline").write_bytes(("\0".join(argv) + "\0").encode())
            _ = (process / "environ").write_bytes(
                ("\0".join(f"{key}={value}" for key, value in environment.items()) + "\0").encode()
            )
        result = self.run_command("kid", "--resume", "abc-123", "--cwd", str(self.root), "--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("check: ok", result.stdout)
        self.assertEqual(self.herdr_calls(), [["--session", "default", "session", "list", "--json"]])

    def test_showrunner_refuses_explicit_account_different_from_configured_group(self) -> None:
        """F004: an explicit account must agree with the showrunners account."""
        _ = self.named_account()
        for options in (("--account", "claude2"),
                        ("--resume", "abc-123", "--cwd", str(self.root), "--account", "claude2")):
            with self.subTest(options=options):
                result = self.run_command("kid", *options)
                self.refused(result, "claude3")
                self.assertEqual(self.events("systemd-run"), [])

    def test_showrunner_resume_without_recorded_account_uses_configured_group(self) -> None:
        """F004: configuration is sufficient for a resume with no retained account."""
        directory = self.named_account()
        self.starts("abc-123", registers="kid")
        result = self.run_command("kid", "--resume", "abc-123", "--cwd", str(self.root), "--account", "claude3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLAUDE_CONFIG_DIR=" + str(directory), self.opened())

    def test_showrunner_launch_carries_no_marks_or_caller_account_variables(self) -> None:
        self.env["CLAUDE_CODE_OAUTH_TOKEN"] = "caller-token"
        self.env["SHOWRUNNER_UNIT"] = "parent"
        self.env["SHOWRUNNER_UNIT_ID"] = "parent-unit"
        self.starts("kid-session", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stderr)
        environment = dict(value.split("=", 1) for flag, value in zip(self.opened()[8:], self.opened()[9:]) if flag == "--env")
        self.assertEqual({key: value for key, value in environment.items()
                          if key.startswith(("CLAUDE_", "SHOWRUNNER_", "HERDR_")) or key == "ENABLE_TOOL_SEARCH"},
                         {"ENABLE_TOOL_SEARCH": "true", "HERDR_AGENT_PANE": "1",
                          "CLAUDE_CONFIG_DIR": str(Path(self.env["HOME"]) / ".claude2"),
                          "CLAUDE_CODE_REMOTE_MEMORY_DIR": str(Path(self.env["HOME"]) / ".claude")})
        self.assertEqual(self.opened()[-1], "--no-focus")
        self.assertNotIn("caller-token", json.dumps(self.herdr_calls()))

    def test_showrunner_start_not_ready_returns_a_distinct_status_and_sends_no_prompt_or_key(self) -> None:
        self.herdr_overrides["agent start"] = CommandResponse(code=1, stdout=json.dumps({
            "error": {"code": "agent_not_ready", "message": "permission dialog"}}))
        screen = "Do you trust this folder?\n1. Yes\n2. No"
        self.herdr_overrides["--session agents pane read w1:p1 --source visible"] = CommandResponse(
            code=0, stdout=screen)
        result = self.run_command("kid")
        self.assertEqual(result.returncode, add_showrunner.SESSION_NOT_READY, result.stdout + result.stderr)
        self.assertIn(screen, result.stderr)
        self.assertFalse(any(row[2:4] in (["agent", "prompt"], ["pane", "send-keys"],
                                        ["pane", "send-text"]) for row in self.herdr_calls()))
        self.assertEqual(self.sent_to(), [])
        self.assertEqual(self.parent_of("kid"), "")

    def test_showrunner_start_failure_is_reported_without_attaching(self) -> None:
        self.herdr_overrides["workspace create"] = CommandResponse(
                code=1, stdout="", stderr="fixture could not open workspace")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("fixture could not open workspace", result.stderr)
        self.assertFalse(any(row[2:4] == ["agent", "start"] for row in self.herdr_calls()))
        self.assertEqual(self.parent_of("kid"), "")

    def test_unreadable_herdr_inventory_refuses_before_starting_the_child(self) -> None:
        self.herdr_overrides["--session default session list --json"] = CommandResponse(
            code=1, stdout="", stderr="fixture cannot list herdr sessions")
        result = self.run_command("kid")
        self.refused(result, "herdr session list: fixture cannot list herdr sessions")
        self.assertEqual(self.herdr_calls(), [["--session", "default", "session", "list", "--json"]])


    def test_child_opens_its_tab_in_a_shared_server_that_already_runs(self) -> None:
        self.herdr_overrides["--session agents status server"] = CommandResponse(
            code=0, stdout="status: running\n")
        self.starts("kid-session", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.herdr_calls()
        self.assertFalse(any(row[:1] == ["--user"] for row in calls))
        self.assertEqual(self.opened()[:6], ["--session", "agents", "workspace", "create", "--label", "kid"])
        self.assertEqual(self.parent_of("kid"), "top")

    def test_new_showrunner_zero_reading_without_refusal_can_launch(self) -> None:
        file = Path(self.env["HOME"]) / ".local/state/agent-notes/usage.json"
        saved = cast(dict[str, usage_store.StoredUsage], json.loads(file.read_text()))
        held = saved["main@example.invalid"]
        assert "weekly" in held
        held["weekly"]["left"] = 0
        _ = file.write_text(json.dumps(saved))
        self.starts("kid-session", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"CLAUDE_CONFIG_DIR={Path(self.env['HOME']) / '.claude2'}", self.opened())

    def test_a_timer_left_by_an_ended_session_does_not_stand_in_the_way(self) -> None:
        # The child ran before under this parent; its session ended, and its timer and its edge are still stored.
        self.timer("kid", "abc-123")
        edge = self.root / "showrunner" / "kid" / "parent"
        edge.parent.mkdir(parents=True)
        _ = edge.write_text("top\n", encoding="utf-8")
        self.starts("abc-123")
        result = self.run_command("kid", "--resume", "abc-123", "--cwd", str(self.root), "--account", "claude2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["started kid: herdr session attach agents, workspace kid, tab kid", "kid already reports to top"])
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
        self.assertIn("herdr session attach agents, workspace kid, tab kid", result.stdout)
        self.assertTrue((self.state / "live-agents").exists())
        self.assertEqual(self.parent_of("kid"), "")
        self.assertEqual(self.sent_to(), [])

    def test_session_records_that_cannot_be_read_refuse_the_start(self) -> None:
        _ = (self.root / "sessions.py").write_text(
            "import sys\nprint('registry gone', file=sys.stderr)\nraise SystemExit(3)\n", encoding="utf-8")
        self.timer("kid", "some-session")
        self.refused(self.run_command("kid"), "cannot tell whether session:some-session is running: registry gone")

    def flaky_registry(self, failures: int) -> None:
        """Once the started session registers its timer, the registry cannot be read for `failures` looks."""
        count = self.root / "registry-failures"
        _ = (self.root / "flaky_sessions.py").write_text(
            "import os, pathlib, runpy, sys\n"
            + f"count, started = pathlib.Path({str(count)!r}), pathlib.Path({str(self.state / 'live-agents')!r})\n"
            + "seen = int(count.read_text()) if count.exists() else 0\n"
            + f"if started.exists() and seen < {failures}:\n"
            + "    count.write_text(str(seen + 1))\n"
            + "    print('one or more registry files could not be read', file=sys.stderr)\n"
            + "    raise SystemExit(3)\n"
            + f"runpy.run_path({str(self.root / 'sessions.py')!r}, run_name='__main__')\n", encoding="utf-8")
        self.env["SHOWRUNNERS_SESSIONS"] = str(self.root / "flaky_sessions.py")

    def test_a_registry_unreadable_for_a_moment_during_the_wait_is_tried_again(self) -> None:
        self.flaky_registry(1)
        self.env["ADD_SHOWRUNNER_WAIT_S"] = "30"
        self.starts("kid-session", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.parent_of("kid"), "top")

    def test_showrunner_wait_retries_with_the_configured_interval(self) -> None:
        self.flaky_registry(1)
        self.env["ADD_SHOWRUNNER_WAIT_S"] = "300"
        self.starts("kid-session", registers="kid")
        self.prepare_herdr("kid")
        polls = self.root / "polls.jsonl"
        program = f'''import json, sys, time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, {str(SCRIPT.parent)!r})
import add_showrunner
polls = Path({str(polls)!r})
def wait(seconds):
    with polls.open("a") as output:
        output.write(json.dumps(seconds) + "\\n")
add_showrunner.time = SimpleNamespace(monotonic=time.monotonic, time=time.time, sleep=wait)
raise SystemExit(add_showrunner.main(sys.argv[2:]))
'''
        result = subprocess.run(
            [sys.executable, "-c", program, str(SCRIPT), "--production", str(self.doc("kid")),
             "--model", "opus", "--effort", "high"],
            env=self.env, capture_output=True, text=True, check=False, timeout=300,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.parent_of("kid"), "top")
        self.assertEqual([json.loads(line) for line in polls.read_text().splitlines()], [0.01])

    def test_showrunner_poll_default_remains_two_seconds(self) -> None:
        environment = {key: value for key, value in self.env.items() if key != "ADD_SHOWRUNNER_POLL_S"}
        program = f"import sys; sys.path.insert(0, {str(SCRIPT.parent)!r}); import add_showrunner; print(add_showrunner.POLL_S)"
        result = subprocess.run([sys.executable, "-c", program], env=environment,
                                capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual((result.returncode, result.stdout), (0, "2.0\n"), result.stderr)

    def test_a_registry_still_unreadable_at_the_deadline_leaves_the_child_unattached(self) -> None:
        self.flaky_registry(1000)
        self.starts("kid-session", registers="kid")
        result = self.run_command("kid")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("kid is not attached: cannot tell whether session:kid-session is running: "
                      + "one or more registry files could not be read", result.stdout)
        self.assertEqual(self.parent_of("kid"), "")

    def test_check_runs_the_refusals_and_starts_nothing(self) -> None:
        result = self.run_command("kid", "--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(),
                         f"check: ok — would start kid in {self.root / 'kid-checkout'} and attach it to top")
        self.assertEqual(self.herdr_calls(), [["--session", "default", "session", "list", "--json"]])

    def test_launch_check_with_no_ending_tab_refuses_a_same_named_shared_tab(self) -> None:
        (self.state / "live-agents").touch()
        self.refused(self.run_command("kid", "--check"), "a tab named kid is already open in herdr session agents")
        self.assertIn(["--session", "agents", "pane", "list"], self.herdr_calls())

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
        (self.state / "live-agents").touch()
        self.refused(self.run_command("kid"), "a tab named kid is already open in herdr session agents")

    def test_a_production_above_the_caller_is_refused(self) -> None:
        self.timer("middle", "middle-session")
        self.timer("grand", "grand-session")
        for child, parent in (("top", "middle"), ("middle", "grand")):
            edge = self.root / "showrunner" / child / "parent"
            edge.parent.mkdir(parents=True)
            _ = edge.write_text(parent + "\n", encoding="utf-8")
        self.refused(self.run_command("grand"), "top already reports to grand, directly or through others")

    def test_an_untrusted_start_folder_is_refused_naming_it_and_nothing_from_the_config(self) -> None:
        checkout = self.root / "kid-checkout"
        _ = self.doc("kid")
        self.trust(self.root, extra={"oauthAccount": {"emailAddress": "SECRET-EMAIL"}, "userID": "SECRET-ID"})
        result = self.run_command("kid", "--check")
        self.refused(result, f"kid: Claude is not trusted in {checkout.resolve()} on claude2. "
                     + f"Run: cd {shlex.quote(str(checkout.resolve()))} && "
                     + f"CLAUDE_CONFIG_DIR={shlex.quote(str(Path(self.env['HOME']) / '.claude2'))} "
                     + f"CLAUDE_CODE_REMOTE_MEMORY_DIR={shlex.quote(str(Path(self.env['HOME']) / '.claude'))} claude")
        self.assertNotIn("SECRET", result.stdout + result.stderr)
        self.assertEqual(self.herdr_calls(), [])

    def test_trust_comes_from_the_repository_root_or_a_worktrees_main_checkout(self) -> None:
        main = self.root / "main"
        main.mkdir()
        for command in (["init", "-q", "-b", "main"], ["-c", "user.name=t", "-c", "user.email=t@x.invalid",
                                                       "commit", "-q", "--allow-empty", "-m", "seed"],
                        ["worktree", "add", "-q", "-b", "side", str(self.root / "side")]):
            _ = subprocess.run(["git", "-C", str(main), *command], check=True, capture_output=True)
        (main / "sub").mkdir()
        self.trust(main)
        for folder in (main / "sub", self.root / "side"):
            with self.subTest(folder=folder.name):
                result = self.run_command("kid", "--resume", "abc-123", "--cwd", str(folder), "--account", "claude2", "--check")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_trusted_parent_folder_does_not_cover_a_repository_below_it(self) -> None:
        _ = self.doc("kid")
        _ = subprocess.run(["git", "init", "-q", str(self.root / "kid-checkout")], check=True, capture_output=True)
        self.trust(self.root)
        self.refused(self.run_command("kid", "--check"), "kid: Claude is not trusted in")

    def test_an_unreadable_config_refuses_without_its_contents(self) -> None:
        _ = (self.root / "claude.json").write_text('{"oauthAccount": "SECRET-EMAIL", ', encoding="utf-8")
        result = self.run_command("kid", "--check")
        self.refused(result, f"kid: cannot read {self.root / 'claude.json'} to check Claude trust (JSONDecodeError)")
        self.assertNotIn("SECRET", result.stderr)
        self.assertEqual(self.herdr_calls(), [])

    def test_a_session_that_runs_no_production_is_refused(self) -> None:
        self.env["CLAUDE_CODE_SESSION_ID"] = "no-such-session"
        self.refused(self.run_command("kid"), "this session runs no production, so it has nothing to attach kid to")


if __name__ == "__main__":
    _ = unittest.main()
