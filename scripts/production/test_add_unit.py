"""Exercise add_unit through its command line with isolated git and launcher state."""

from __future__ import annotations

import argparse
import fcntl
import io
import json
import os
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar, TypedDict, cast, final, override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import add_unit
from cli_fixture import ImmutableGitSeed, invoke_cli
import conversation_pause
import launch_permission
import unit_lookup
import session_pane
from herdr import HerdrPlace
from add_unit import cell_value, live_unit_table, plan_cell_is_retired, retired_units
from scripts.accounts import accounts
import usage_store
from test_support import ShutdownTestCase


# Script completion deadlines only bound hangs; startup can be slow under load.
SCRIPT = Path(__file__).with_name("add_unit.py")
REAL_WORKTREE_IS_LINKED = add_unit.worktree_is_linked
MARKED_TMUX_PLACE = unit_lookup.TmuxPlace("%7")
OLD_HEADER = ["## Units", "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
STUB = r'''#!/usr/bin/env python3
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

state = Path(os.environ["STUB_STATE"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with (state / "events.jsonl").open("a") as events:
    events.write(json.dumps({"command": name, "args": args,
                             "claude_env": sorted(key for key in os.environ if key.startswith("CLAUDE_"))}) + "\n")
if name == "tmux":
    # A session is a `live-<name>` file; its marks are in `env-<name>.json`, absent when it has none.
    live = sorted(path.name[5:] for path in state.glob("live-*"))

    def named(target):
        for index, session in enumerate(live, 1):
            if target in (session, f"${index}", f"%{index}", f"={session}"):
                return session
        return None

    target = args[args.index("-t") + 1] if "-t" in args else ""
    # Option commands take a pane target, where only `=name:` names a session.
    if args[:1] in (["set-option"], ["show-options"]):
        if target.startswith("=") and not target.endswith(":"):
            print(f"no such session: {target}", file=sys.stderr)
            raise SystemExit(1)
        target = target.removesuffix(":")
    if args[:1] in (["has-session"], ["show-environment"], ["capture-pane"]) and named(target) is None:
        raise SystemExit(1)
    if args[:1] == ["list-panes"]:
        if not live:
            print("no server running on /tmp/stub", file=sys.stderr)
            raise SystemExit(1)
        for index, session in enumerate(live, 1):
            print(f"${index}\t%{index}\t{session}")
    if args[:1] == ["show-environment"]:
        marks = state / f"env-{named(target)}.json"
        for key, value in (json.loads(marks.read_text()) if marks.exists() else {}).items():
            print(f"{key}={value}")
    if args[:1] == ["capture-pane"]:
        print("/remote-control is active" if (state / "ready").exists() else "Waiting for remote control")
    if args[:1] == ["show-options"]:
        options = state / f"options-{named(target)}.json"
        value = (json.loads(options.read_text()) if options.exists() else {}).get(args[-1], "")
        print(value)
    if args[:1] == ["set-option"]:
        options = state / f"options-{named(target)}.json"
        values = json.loads(options.read_text()) if options.exists() else {}
        values[args[-2]] = args[-1]
        options.write_text(json.dumps(values))
    if args[:1] == ["kill-session"]:
        session = named(target)
        for prefix in ("live-", "env-", "options-"):
            suffix = "" if prefix == "live-" else ".json"
            (state / f"{prefix}{session}{suffix}").unlink(missing_ok=True)
    if args[:1] == ["new-session"]:
        session = args[args.index("-s") + 1]
        (state / f"live-{session}").touch()
        marks = dict(args[index + 1].split("=", 1) for index, argument in enumerate(args) if argument == "-e")
        (state / f"env-{session}.json").write_text(json.dumps(marks))
elif name == "systemd-run":
    raise SystemExit(subprocess.run(args[3:], check=False).returncode)
elif name == "nix":
    print(os.environ["STUB_TMUX"])
'''


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.enterModuleContext(patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


@final
class AddUnitTests(ShutdownTestCase):
    seed: ClassVar[ImmutableGitSeed]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        root = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix="AddUnitTests-seed-")))
        cls.seed = ImmutableGitSeed.create(root, 'build-followups', 'Unit Test', 'unit@example.invalid', {})

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.checkout = Path()
        self.origin = Path()
        self.doc = Path()
        self.plan = Path()
        self.log = Path()
        self.config = Path()
        self.prompt = Path()
        self.state = Path()
        self.bin = Path()
        self.agent_config = Path()
        self.pause_state = Path()
        self.env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        # Rows read in this process name worktrees that exist nowhere: answer for them here, so no
        # test asks the real disk, and a row with a worktree never asks tmux. RetiredRowTests covers the rule.
        _ = self.enterContext(patch.object(add_unit, "worktree_is_linked", return_value=True))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        home_context = cast(object, self.enterContext(
            patch.dict(os.environ, {"HOME": str(self.root / "home")})
        ))
        del home_context
        self.checkout = self.root / "project-trunk"
        self.origin = self.root / "origin.git"
        self.seed.copy_to(self.checkout, self.origin)
        self.doc = self.checkout / "docs/plans/build-followups-production.md"
        _ = self.doc.parent.mkdir(parents=True)
        _ = self.doc.write_text(self.production_doc(), encoding="utf-8")
        self.plan = self.checkout / "docs/plans/given.md"
        _ = self.plan.write_text("# Given\n\n> **Production: build-followups** — unit `alpha-unit`\n", encoding="utf-8")
        _ = (self.checkout / ".gitignore").write_text("docs/plans/build-followups-log.md\n", encoding="utf-8")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "initial")
        _ = self.git("push", "-u", "origin", "build-followups")
        self.log = self.checkout / "docs/plans/build-followups-log.md"
        self.config = self.root / "home/.claude/config/showrunners.json"
        self.prompt = self.root / "home/.local/state/showrunner/build-followups/prompt.txt"
        home = self.root / "home"
        (home / ".claude").mkdir(parents=True)
        _ = (home / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "owner@example.com"},
                        "projects": self.trusted_projects()}),
            encoding="utf-8",
        )
        notes = self.root / "notes"
        notes.mkdir()
        _ = (notes / "claude 2.md").write_text(
            "---\nlogin: owner@example.com\nstate: active\n---\n",
            encoding="utf-8",
        )
        self.state = self.root / "stub-state"
        _ = self.state.mkdir()
        _ = (self.state / "ready").touch()
        self.bin = self.root / "bin"
        _ = self.bin.mkdir()
        for name in ("tmux", "systemd-run", "nix", "claude"):
            command = self.bin / name
            _ = command.write_text(STUB, encoding="utf-8")
            _ = command.chmod(0o755)
        self.agent_config = self.root / "agents.conf"
        self.pause_state = self.root / "conversation-pause"
        codex_config = self.root / "codex.toml"
        codex_cache = self.root / "models.json"
        sync_state = self.root / "catalog-sync-success"
        self.write_agent_config()
        _ = sync_state.touch()
        self.env = {**os.environ, "HOME": str(self.root / "home"),
                    "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                    "STUB_STATE": str(self.state), "STUB_TMUX": str(self.bin / "tmux"),
                    "UNIT_LOOKUP_TMUX": str(self.bin / "tmux"), "TMUX_PANE": "",
                    "SHOWRUNNERS_CONFIG": str(self.config), "CLAUDE_TEST_SECRET": "must-not-leak",
                    "CONVERSATION_PAUSE_STATE_DIR": str(self.pause_state),
                    "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
                    "AGENT_NOTES_DIR": str(notes),
                    "AGENTS_CONFIG_FILE": str(self.agent_config),
                    "CODEX_CONFIG_FILE": str(codex_config),
                    "CODEX_MODELS_CACHE_FILE": str(codex_cache),
                    "CODEX_CATALOG_SYNC_STATE_FILE": str(sync_state)}
        _ = self.env.pop("CLAUDE_CONFIG_DIR", None)
        self.quota_reading("owner@example.com", 80, 7)
        _ = self.select_unit_account("default")

    def quota_reading(self, login: str, left: float, reset_days: float) -> None:
        file = Path(self.env["HOME"]) / ".local/state/agent-notes/usage.json"
        saved = cast(dict[str, usage_store.StoredUsage], json.loads(file.read_text())) if file.exists() else {}
        now = datetime.now(timezone.utc)
        saved[login] = {"weekly": {"left": left, "at": now.isoformat(),
                                  "resets": (now + timedelta(days=reset_days)).isoformat()}}
        file.parent.mkdir(parents=True, exist_ok=True)
        _ = file.write_text(json.dumps(saved))

    def select_unit_account(self, label: str) -> Path:
        with patch.dict(os.environ, self.env):
            path = accounts.groups_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text(json.dumps({"units": label, "showrunners": "default"}))
            return accounts.main_config_dir() if label == "default" else accounts.account_dir(label)

    def trusted_projects(self) -> dict[str, dict[str, bool]]:
        return {str(folder): {"hasTrustDialogAccepted": True}
                for folder in (self.checkout, self.root / "prior-session", self.root / "prior session")}

    def test_untrusted_new_worktree_refuses_before_stub_row_or_worktree(self) -> None:
        config = Path(self.env["HOME"]) / ".claude.json"
        _ = config.write_text(json.dumps({"oauthAccount": {"emailAddress": "owner@example.com"},
                                         "projects": {str(self.root): {"hasTrustDialogAccepted": True}}}))
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        for mode in ([], ["--check"]):
            with self.subTest(mode=mode):
                result = self.cli("alpha", "--brief", "Add a feature", *mode)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                folder = self.root / "project-alpha"
                self.assertIn(f"alpha-unit: Claude is not trusted in {folder} on default account. Run: "
                              + f"cd {shlex.quote(str(self.checkout))} && "
                              + "env -u CLAUDE_CONFIG_DIR -u CLAUDE_CODE_REMOTE_MEMORY_DIR claude", result.stderr)
                self.assert_no_launch_change(document, head)
                self.assertFalse((self.checkout / "docs/plans/build-followups-alpha.md").exists())
                self.assertNotIn("build-followups-alpha", self.git("branch", "--list"))

    def test_trust_uses_launch_account_instead_of_callers_account(self) -> None:
        directory = self.prepare_unit_account("claude 3")
        _ = (directory / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "claude-3@example.invalid"}, "projects": {}}))
        self.env["CLAUDE_CONFIG_DIR"] = str(Path(self.env["HOME"]) / ".claude")
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--brief", "Add a feature")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("on claude 3. Run:", result.stderr)
        self.assertIn(f"CLAUDE_CONFIG_DIR={shlex.quote(str(directory))}", result.stderr)
        self.assert_no_launch_change(document, head)

    def test_unreadable_trust_file_names_the_unit_and_refuses_before_any_write(self) -> None:
        with patch.dict(os.environ, self.env):
            selected = accounts.role_account("unit")
        config = Path(self.env["HOME"]) / ".claude.json"
        _ = config.write_text('{"oauthAccount": "SECRET-EMAIL", ')
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        output = io.StringIO()
        errors = io.StringIO()
        with (patch.dict(os.environ, self.env), redirect_stdout(output), redirect_stderr(errors),
              patch.object(accounts, "role_account", return_value=selected),
              patch.object(launch_permission, "launch_permission",
                           return_value=launch_permission.AllowedByShutdownState())):
            code = add_unit.main(["--production", str(self.doc), "alpha", "--brief", "Add a feature"])
        self.assertEqual(code, 2, (output.getvalue(), errors.getvalue()))
        self.assertEqual(errors.getvalue(), f"add_unit: alpha-unit: cannot read {config} "
                         + "to check Claude trust (JSONDecodeError)\n")
        self.assertNotIn("SECRET", output.getvalue() + errors.getvalue())
        self.assert_no_launch_change(document, head)
        self.assertFalse((self.checkout / "docs/plans/build-followups-alpha.md").exists())

    def test_resume_checks_session_cwd_instead_of_worktree(self) -> None:
        prior = self.root / "untrusted-session"
        prior.mkdir()
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123",
                          "--cwd", str(prior), "--account", "default")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn(f"Claude is not trusted in {prior}", result.stderr)
        self.assert_no_launch_change(document, head)

    def prepare_unit_account(self, label: str) -> Path:
        directory = self.select_unit_account(label)
        login = f"{label.replace(' ', '-')}@example.invalid"
        with patch.dict(os.environ, self.env):
            self.assertEqual(accounts.setup(label), [])
            _ = (directory / ".claude.json").write_text(
                json.dumps({"oauthAccount": {"emailAddress": login},
                            "projects": self.trusted_projects()}),
                encoding="utf-8",
            )
            _ = (Path(self.env["AGENT_NOTES_DIR"]) / f"{label}.md").write_text(
                f"---\nlogin: {login}\nstate: active\n---\n", encoding="utf-8"
            )
        self.quota_reading(login, 1, 2)
        return directory

    def persist_unit_account(self, directory: Path, *, session: str = "recorded-tmux", model: str = "opus") -> None:
        _ = (self.state / f"live-{session}").touch()
        _ = (self.state / f"env-{session}.json").write_text(json.dumps({
            unit_lookup.PRODUCTION_MARK: "build-followups", unit_lookup.UNIT_MARK: "alpha-unit"}), encoding="utf-8")
        _ = (self.state / f"options-{session}.json").write_text(json.dumps({
            "@claude_config_dir": str(directory), "@claude_model": model}), encoding="utf-8")
        (Path(self.env["HOME"]) / ".claude/sessions").mkdir(exist_ok=True)

    def recorded_unit_options(self, session: str) -> dict[str, str]:
        return cast(dict[str, str], json.loads((self.state / f"options-{session}.json").read_text(encoding="utf-8")))

    def named_prefix(self, directory: Path) -> str:
        """The assignments a unit on a named account's directory starts with."""
        memory_base = Path(self.env["HOME"]) / ".claude"
        return (
            "unset CLAUDE_CONFIG_DIR CLAUDE_CODE_REMOTE_MEMORY_DIR CLAUDE_CODE_OAUTH_TOKEN; "
            + f"CLAUDE_CONFIG_DIR={shlex.quote(str(directory))} "
            + f"CLAUDE_CODE_REMOTE_MEMORY_DIR={shlex.quote(str(memory_base))} "
        )

    def write_agent_config(self, *, family: str = "claude", director: str = "opus:xhigh",
                           claude_set: bool = True) -> None:
        claude_section = f"[production.claude]\ndirector={director}\n\n" if claude_set else ""
        codex_section = "[production.codex]\ndirector=gpt-test:high\n\n" if family == "codex" else ""
        content = (f"[assignments]\nproduction={family}\n\n"
                   f"{claude_section}{codex_section}"
                   "[codex.agents]\ngpt-test=low,medium,high\n\n"
                   "[claude.agents]\nopus=low,medium,high,xhigh\nsonnet=low,medium,high,xhigh\n")
        _ = self.agent_config.write_text(content, encoding="utf-8")

    def production_doc(self, merge_branch: str = "build-followups", *, session_column: bool = False) -> str:
        columns = ["Unit", "Plan", "Worktree", "Branch", *(["Session"] if session_column else []), "Port", "Owns"]
        return ("# Production\n\n"
                f"- **Merge branch:** `{merge_branch}` — unit checkpoints merge here\n"
                f"- **Showrunner checkout:** `{self.checkout}`\n"
                "- **Showrunner session:** director\n"
                "- **Log:** `docs/plans/build-followups-log.md` — git-excluded\n"
                "- **User zone:** America/Los_Angeles — reports use PDT\n\n"
                "## Units\n\n"
                f"| {' | '.join(columns)} |\n"
                f"| {' | '.join('---' for _ in columns)} |\n\n"
                "## Gates\n")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", *args], cwd=cwd or self.checkout,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, (args, result.stdout, result.stderr))
        return result.stdout.strip()

    def cli(self, *args: str, timeout: float = 300, subprocess_cli: bool = False) -> subprocess.CompletedProcess[str]:
        arguments = ["--production", str(self.doc), *args]
        if subprocess_cli:
            return subprocess.run([sys.executable, str(SCRIPT), *arguments], cwd=self.checkout,
                                  env=self.env, capture_output=True, text=True, check=False, timeout=timeout)
        # The row-reader fixture patch never reached the old CLI child. Preserve
        # the real worktree check on this command boundary too.
        with patch.object(add_unit, "worktree_is_linked", REAL_WORKTREE_IS_LINKED):
            return invoke_cli(add_unit, lambda: add_unit.main(arguments), arguments,
                              self.env, self.checkout, production_state=True)

    def successful(self, *args: str, subprocess_cli: bool = False) -> subprocess.CompletedProcess[str]:
        result = self.cli(*args, subprocess_cli=subprocess_cli)
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        return result

    def events(self, command: str) -> list[dict[str, object]]:
        path = self.state / "events.jsonl"
        if not path.exists():
            return []
        records = [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()]
        return [record for record in records if record["command"] == command]

    def launch_command(self) -> str:
        launches = self.events("systemd-run")
        self.assertEqual(len(launches), 1)
        return cast(list[str], launches[0]["args"])[-1]

    def recorded_prompts(self, session_id: str) -> tuple[str, ...]:
        with patch.dict(os.environ, self.env):
            return conversation_pause.read_scheduled_prompts(session_id)

    def shutdown_record(
        self,
        state: str,
        *,
        session_id: str = "session-123",
        progress: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "login": "owner@example.com",
            "label": "claude 2",
            "machine": "natedev",
            "state": state,
            "requested_at": "2026-10-09T21:49:10+00:00",
            "requested_by": {"kind": "terminal"},
            "scope": {"kind": "all account sessions"},
            "conductor": {"kind": "not started"},
            "force": "wait for ready",
            "entries": [
                {
                    "session": {
                        "kind": "unit",
                        "session_id": session_id,
                        "pid": 12345,
                        "proc_start": "process-start",
                        "name": "alpha",
                        "cwd": "/tmp/alpha",
                        "status": "idle",
                        "model": {"kind": "no reply yet"},
                        "checkout": {"kind": "not a checkout"},
                        "run_dirs": [],
                        "codex_servers": [],
                        "timers": [],
                        "host": {
                            "kind": "unit",
                            "production": "build-followups",
                            "unit": "alpha-unit",
                            "doc": str(self.doc),
                            "tmux_session": "alpha",
                            "plan": {
                                "kind": "plan",
                                "path": str(self.plan),
                            },
                        },
                    },
                    "timers": [],
                    "settle_message": {"kind": "not sent"},
                    "where": {"kind": "not said"},
                    "progress": progress or {"kind": "waiting"},
                    "stop_issues": [],
                }
            ],
            "stop_issues": [],
        }

    def write_shutdown_record(self, value: object, *, login: str = "owner@example.com") -> None:
        account = Path(self.env["SHUTDOWN_STATE_DIR"]) / login
        account.mkdir(parents=True)
        _ = (account / "record.json").write_text(
            json.dumps(value), encoding="utf-8"
        )

    def assert_director_flags(self, model: str, effort: str | None) -> None:
        command = self.launch_command()
        flags = f"--model {model}"
        if effort is not None:
            flags += f" --effort {effort}"
        self.assertIn(flags + " ", command)
        if effort is None:
            self.assertNotIn("--effort", command)

    def test_retired_marker_only_applies_at_start_of_plan_cell(self) -> None:
        cases = (
            ("retired by the user", True),
            ("(retired by the user)", True),
            ("docs/plan.md; retired by the user", False),
            ("(run done; retired by the user)", False),
            ("retiredness", False),
            ("Retired by the user", False),
        )
        for plan_cell, expected in cases:
            with self.subTest(plan_cell=plan_cell):
                self.assertEqual(plan_cell_is_retired(plan_cell), expected)

    def test_retired_readers_use_marker_on_production_rows(self) -> None:
        lines = [
            "## Units",
            "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            "| stalls-unit | (retired by the user 2026-10-07; run done, worktree removed) follow-up: `/unit:report` shows the same sections every time, the last recorded tables when no window is open (the user, 2026-10-06 22:5x); earlier follow-up: showrunner replies end with the agent usage lines, checked by a hook (the user, 2026-10-06 08:1x), then `docs/as-built/build-followups-fn-length-hook.md (run done; as-built 4ab5e4c)` (follow-up `commands/unit/eta_breakdown.md` merged as a9f35af) (earlier runs' as-builts `docs/as-built/screen-record.md`, merged as b3c069f, `docs/as-built/build-memory-admission.md`, merged as 35151e5, and `docs/as-built/buildlog-memory-stalls.md`) | `/home/natepiano/worktrees/claude-build-followups-stalls` | `build-followups-stalls` | `hook` (resumed in `/etc/nixos`; renamed from `nightly-config` 2026-10-04, then `stalls` → `footer` → `hook` by the user 2026-10-06, tmux session too; its own tmux session `stalls` since 22:13 PDT, after the nightly review's launcher closed the `nightly-config` session it ran in) | — | memory stall recording and its report section; then memory admission, which edited `scripts/buildlog/`, `scripts/lint/` (`invoke.sh`, `memory_gate.sh`, `sweep.py`), `scripts/delegate/verify.sh` and `board.sh`, `scripts/build_hold/build_hold.py`, `scripts/hooks/pre-tool-use-brp-launch-gate.sh`, `scripts/agents/codex_mesh.py`, `scripts/message/` (`send.py`, `top_level.py`), `scripts/notify/pushover.py`, `scripts/whoami/agent_notes.py`, `scripts/production/dailies_render.py`, `settings.json`, `config/lint.conf`, `pyrightconfig.json`, and `commands/` (`build_hold.md`, `lint_config.md`, `notify_top_level.md`, `showrunner/dailies.md`, `showrunner/produce.md`); promoted by the user 2026-10-04; then the screen recording skill: `commands/screen_record.md`, `scripts/screen_record/`; and the `/unit:report` timer fixes: `scripts/production/unit_status.sh`, `scripts/delegate/progress_history.py`; then the Codex `systemError` fix: `scripts/agents/codex_mesh.py`, `scripts/agents/test_codex_mesh.py`; then the kill order: `scripts/lint/invoke.sh`, `scripts/lint/test_invoke_scope.py`, `docs/as-built/build-memory-admission.md`; then `/unit:eta_breakdown`: `commands/unit/eta_breakdown.md`; then the function-length hook: `scripts/hooks/fn_length_lib.py`, `scripts/hooks/post-tool-use-fn-length.py`, `scripts/hooks/test_fn_length.py`, `scripts/hooks/codex_hooks.py`, `scripts/hooks/test_codex_hooks.py`, `settings.json`; then the footer: `scripts/production/dailies_render.py`, a showrunner-only Stop hook under `scripts/hooks/`, `settings.json`, `commands/showrunner/produce.md`; `commands/unit/report.md` (the report follow-up) |",
            "| enh-showrunner-unit | `docs/plans/build-followups-enh-showrunner-dailies.md` (follow-up: retired units drop out of the status and a simple dailies groups idle units; the user, 2026-10-07; the earlier run's as-built is `docs/as-built/showrunner-automation.md`) (Phases 7 and 8 of stalls-unit's plan, moved by the user 2026-10-06 14:2x PDT) | `/home/natepiano/worktrees/claude-build-followups-enh-showrunner` | `build-followups-enh-showrunner` | `enh-showrunner` | — | `docs/as-built/showrunner-automation.md`; `scripts/production/add_unit.py`, `test_add_unit.py`, `merge_checkpoint.py`, `test_merge_checkpoint.py`; `commands/showrunner/add_unit.md`; after stalls-unit Phase 6 merges: `commands/showrunner/produce.md`, `commands/showrunner/promote_unit.md`, `scripts/production/showrunners.py`, `scripts/production/stall_watch.py` and their tests (hub rows); from its Phase 5 (the user, 2026-10-06 15:25 PDT, the phase-end split): `commands/unit/delegate.md`, `commands/unit/checkpoint.md`, `docs/delegate/run_phase_review.md`, `docs/production_format.md`, `commands/unit/add_ons.md`, `commands/plan/shrink.md`, `commands/plan/phase_review.md`, `docs/delegate/phase_end.md`, `docs/delegate_plan_format.md`, `commands/unit/eta_breakdown.md`, `docs/delegate/final_gate_commit.md` |",
            "| model-study-unit | `docs/as-built/director-model-study.md` (run done; as-built 16e5ac6) | `/home/natepiano/worktrees/claude-build-followups-model-study` | `build-followups-model-study` | `model-study` | — | `docs/plans/build-followups-model-study.md`; `scripts/model_study/`; `docs/as-built/director-model-study-results.md` |",
        ]
        self.assertEqual(retired_units(lines, "build-followups"), {"stalls-unit"})
        self.assertEqual([cell_value(cells["Unit"]) for cells in live_unit_table(lines, "build-followups")],
                         ["enh-showrunner-unit", "model-study-unit"])

    def test_retired_units_include_retired_name_and_exclude_live_name(self) -> None:
        lines = [
            "## Units",
            "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            "| `old-unit` | retired after completion | /tmp/old | old | old | — | — |",
            "| `live-unit` | docs/live.md | /tmp/live | live | live | — | — |",
        ]
        self.assertEqual(retired_units(lines, "show"), {"old-unit"})

    def unit_row(self, name: str = "alpha", *, branch: str = "build-followups-alpha",
                 worktree: Path | None = None, plan: str = "docs/plans/given.md",
                 port: str = "—", owns: str = "—") -> str:
        target = worktree or self.root / "project-alpha"
        return f"| {name}-unit | {plan} | {target} | {branch} | {port} | {owns} |"

    def prepared_row(self, *, name: str = "alpha", plan: str = "docs/plans/given.md",
                     worktree: Path | None = None, branch: str = "build-followups-alpha") -> str:
        target = worktree or self.root / "project-alpha"
        return (f"| `{name}-unit` | `{plan}` (ready to launch) | `{target}` | "
                f"`{branch}` | 8123 | `src/alpha` — assigned files |")

    def commit_prepared_row(self, row: str) -> None:
        _ = self.doc.write_text(self.production_doc().replace("## Gates", row + "\n\n## Gates"),
                                encoding="utf-8")
        _ = self.git("add", "docs/plans/build-followups-production.md")
        _ = self.git("commit", "-m", "prepare unit rows")
        _ = self.git("push", "origin", "build-followups")

    def assert_no_launch_change(self, document: str, head: str) -> None:
        self.assertEqual(self.doc.read_text(), document)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.log.exists())
        self.assertFalse((self.root / "project-alpha").exists())
        for event in self.events("tmux"):
            arguments = cast(list[str], event["args"])
            self.assertIn(arguments[0], {"list-panes", "show-environment", "show-options", "has-session"})
        self.assertEqual(self.events("systemd-run"), [])

    def marks(self, session: str) -> dict[str, str]:
        return cast(dict[str, str], json.loads((self.state / f"env-{session}.json").read_text()))

    def new_sessions(self) -> list[list[str]]:
        return [cast(list[str], record["args"]) for record in self.events("tmux")
                if cast(list[str], record["args"])[:1] == ["new-session"]]

    def restore(self, name: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")
        return self.cli(name, "--plan", "docs/plans/given.md", "--resume", "session-123", "--cwd", str(cwd),
                        "--restart-note", str(note), "--session-name", "recorded-name",
                        "--host-session", "recorded-tmux", "--config-dir", str(Path(self.env["HOME"]) / ".claude"))

    def repository_state(self) -> tuple[str, str, str]:
        """The production doc, every branch with its commit, and every worktree with its branch."""
        return (self.doc.read_text(), self.git("for-each-ref", "refs/heads"),
                self.git("worktree", "list", "--porcelain"))

    def commit_row_off_the_naming_pattern(self) -> Path:
        """Unit `trunk` as seen live: neither its worktree nor its branch is what the naming pattern gives."""
        worktree = self.root / "tool-based-ui-trunk"
        _ = self.git("worktree", "add", str(worktree), "-b", "init/catalyst-trunk")
        self.commit_prepared_row(self.unit_row("trunk", branch="init/catalyst-trunk", worktree=worktree))
        return worktree

    def run_with_marked_unit(
        self,
        launch_kind: add_unit.NewWorkLaunch | add_unit.UnitRestoreLaunch,
        claude: unit_lookup.Claude,
        *,
        already_marked: bool = True,
        place: unit_lookup.Place = MARKED_TMUX_PLACE,
    ) -> tuple[int, str, list[list[str]], int, int]:
        request = add_unit.UnitLaunch(
            production=add_unit.read_production(self.doc),
            identity=add_unit.UnitIdentity("alpha-unit", "alpha"),
            branch="build-followups-alpha",
            worktree=self.root / "project-alpha",
            plan=add_unit.PlanGiven(Path("docs/plans/given.md")),
            port=add_unit.OmittedCell(),
            owns=add_unit.OmittedCell(),
            session=(add_unit.ResumedSession("session-123", self.root / "prior-session")
                     if isinstance(launch_kind, add_unit.UnitRestoreLaunch) else add_unit.NewSession()),
            launch_kind=launch_kind,
            timeout=1.0,
        )
        label = place.session if isinstance(place, HerdrPlace) else "marked-tmux"
        marked = unit_lookup.MarkedUnit("alpha-unit", label, claude, place)
        ready = add_unit.ReadyToLaunch(
            request,
            add_unit.NoUnitRow(),
            add_unit.DirectorAgent("opus", add_unit.DefaultEffort()),
        )
        commands: list[list[str]] = []
        launches: list[str] = []

        if isinstance(launch_kind, add_unit.UnitRestoreLaunch):
            _ = launch_kind.note.write_text("Resume this unit.", encoding="utf-8")

        def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if "show-options" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            if "launch-blocked" in command:
                live = (
                    Path(self.env["SHUTDOWN_STATE_DIR"])
                    / "owner@example.com"
                    / "record.json"
                ).exists()
                return subprocess.CompletedProcess(
                    command,
                    0 if live else 1,
                    (
                        "claude 2 is held by a shutdown "
                        + "(since 2026-10-09 14:49 PDT)\n"
                        if live
                        else ""
                    ),
                    "",
                )
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        def launch(
            _request: add_unit.UnitLaunch,
            _tmux: str,
            _director: add_unit.DirectorAgent,
            prompt: str,
            _selected: accounts.ClaudeLaunchAccount,
            *, target: str = "", model: str = "",
        ) -> None:
            self.assertEqual(target, "default" if isinstance(_request.session, add_unit.ResumedSession) else "")
            self.assertEqual(model, "")
            if isinstance(launch_kind, add_unit.UnitRestoreLaunch):
                recorded = self.recorded_prompts(launch_kind.session_id)
                self.assertEqual(recorded[-1], prompt)
                self.assertIs(
                    conversation_pause.prompt_source(
                        prompt, frozenset, lambda: recorded
                    ),
                    conversation_pause.PromptSource.SCHEDULED,
                )
            launches.append(prompt)

        error = io.StringIO()
        with (
            patch.object(add_unit, "launch_request", return_value=request),
            patch.object(add_unit, "preflight", return_value=ready),
            patch.object(unit_lookup, "tmux_binary", return_value="tmux"),
            patch.object(
                add_unit,
                "launched_unit",
                return_value=(add_unit.MarkedUnitFound(marked) if already_marked else add_unit.NoMarkedUnit()),
            ),
            patch.object(add_unit, "tmux_live", return_value=False),
            patch.object(add_unit, "write_stub") as write_stub,
            patch.object(add_unit, "append_row"),
            patch.object(add_unit, "commit_unit"),
            patch.object(add_unit, "ensure_worktree"),
            patch.object(add_unit, "launch_session", side_effect=launch),
            patch.object(add_unit, "wait_for_remote_control", return_value=marked) as wait,
            patch.object(add_unit, "record"),
            patch.object(subprocess, "run", side_effect=run),
            patch.dict(os.environ, self.env),
            redirect_stderr(error),
        ):
            arguments = ["--production", "unused", "alpha", "--plan", "unused"]
            if isinstance(launch_kind, add_unit.UnitRestoreLaunch):
                arguments.extend(("--config-dir", str(Path(self.env["HOME"]) / ".claude")))
            result = add_unit.main(arguments)
        if result == 0:
            if isinstance(launch_kind, add_unit.UnitRestoreLaunch):
                wait.assert_not_called()
            else:
                wait.assert_called_once_with(request, "tmux")
        return result, error.getvalue(), commands, len(launches), write_stub.call_count

    def test_restore_returns_zero_without_waiting_for_remote_control(self) -> None:
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123", self.root / "restart-note", "recorded-name", "recorded-tmux"
        )
        output = io.StringIO()
        with redirect_stdout(output):
            result, error, commands, launches, _writes = self.run_with_marked_unit(
                launch_kind, unit_lookup.ClaudeNotRunning(), already_marked=False
            )
        self.assertEqual(result, 0, error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 1)
        self.assertEqual(output.getvalue(), "alpha-unit restore launched: tmux attach -t recorded-tmux\n")

    def test_restore_already_running_returns_zero_without_waiting_for_remote_control(self) -> None:
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123", self.root / "restart-note", "recorded-name", "recorded-tmux"
        )
        output = io.StringIO()
        with redirect_stdout(output):
            result, error, commands, launches, _writes = self.run_with_marked_unit(
                launch_kind, unit_lookup.LiveClaude("recorded-name", "session-123", "/socket", 123)
            )
        self.assertEqual(result, 0, error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 0)
        self.assertEqual(output.getvalue(), "alpha-unit restore launched: tmux attach -t marked-tmux\n")

    def test_new_work_launch_still_waits_for_remote_control(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result, error, commands, launches, _writes = self.run_with_marked_unit(
                add_unit.NewWorkLaunch(), unit_lookup.ClaudeNotRunning(), already_marked=False
            )
        self.assertEqual(result, 0, error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 1)
        self.assertEqual(output.getvalue(), "alpha-unit started: tmux attach -t marked-tmux\n")

    def test_restore_replaces_marked_tmux_without_running_claude(self) -> None:
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123", self.root / "restart-note", "recorded-name", "recorded-tmux"
        )

        result, error, commands, launches, writes = self.run_with_marked_unit(
            launch_kind, unit_lookup.ClaudeNotRunning()
        )

        self.assertEqual(result, 0, error)
        self.assertEqual(commands, [["tmux", "kill-session", "-t", "%7"]])
        self.assertEqual(launches, 1)
        self.assertEqual(writes, 1)

    def test_restore_replaces_stopped_herdr_unit_by_name_without_tmux_commands(self) -> None:
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123", self.root / "restart-note", "recorded-name", "recorded-tmux"
        )
        with patch("herdr.stop_session") as stopped:
            result, error, commands, launches, writes = self.run_with_marked_unit(
                launch_kind, unit_lookup.ClaudeNotRunning(), place=HerdrPlace("native-unit", "w1:p1")
            )
        self.assertEqual(result, 0, error)
        stopped.assert_called_once_with("native-unit")
        self.assertEqual(commands, [])
        self.assertEqual((launches, writes), (1, 1))

    def test_new_work_is_refused_while_account_is_down(self) -> None:
        self.write_shutdown_record(self.shutdown_record("down"))
        document = self.doc.read_text(encoding="utf-8")
        head = self.git("rev-parse", "HEAD")

        result = self.cli("alpha", "--plan", "docs/plans/given.md")

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(
            result.stderr,
            "add_unit: claude 2 is held by a shutdown "
            + "(since 2026-10-09 14:49 PDT); /shutdown restart first\n",
        )
        self.assert_no_launch_change(document, head)

    def test_restart_restore_is_allowed_for_its_restarting_unit(self) -> None:
        self.commit_prepared_row(self.unit_row())
        self.write_shutdown_record(self.shutdown_record("restarting"))
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")

        result = self.successful(
            "alpha",
            "--plan",
            "docs/plans/given.md",
            "--resume",
            "session-123",
            "--cwd",
            str(prior),
            "--restart-note",
            str(note),
            "--session-name",
            "recorded-name",
            "--host-session",
            "recorded-tmux",
        )

        self.assertIn("alpha-unit restore launched", result.stdout)
        self.assertEqual(len(self.events("systemd-run")), 1)
        self.assertEqual(len(self.recorded_prompts("session-123")), 1)

    def test_restart_restore_adopts_a_row_added_from_a_brief_whose_plan_is_now_its_as_built(self) -> None:
        checkout = self.doc.parents[2]
        (checkout / "docs/as-built").mkdir(parents=True, exist_ok=True)
        _ = (checkout / "docs/as-built/alpha.md").write_text("# Alpha\n", encoding="utf-8")
        row = self.unit_row(plan="docs/as-built/alpha.md")
        _ = self.doc.write_text(self.production_doc().replace("## Gates", row + "\n\n## Gates"), encoding="utf-8")
        _ = self.git("add", "docs/plans/build-followups-production.md", "docs/as-built/alpha.md")
        _ = self.git("commit", "-m", "production(build-followups): add unit alpha-unit (brief)")
        _ = self.git("push", "origin", "build-followups")
        self.write_shutdown_record(self.shutdown_record("restarting"))
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")

        result = self.successful(
            "alpha",
            "--plan",
            "docs/as-built/alpha.md",
            "--resume",
            "session-123",
            "--cwd",
            str(prior),
            "--restart-note",
            str(note),
            "--session-name",
            "recorded-name",
            "--host-session",
            "recorded-tmux",
        )

        self.assertIn("alpha-unit restore launched", result.stdout)
        self.assertEqual(len(self.recorded_prompts("session-123")), 1)

    def test_restore_without_a_units_row_is_refused_and_adds_nothing(self) -> None:
        self.write_shutdown_record(self.shutdown_record("restarting"))
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")

        result = self.cli(
            "alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(prior),
            "--restart-note", str(note),
            "--session-name", "recorded-name", "--host-session", "recorded-tmux",
         "--config-dir", str(Path(self.env["HOME"]) / ".claude"))

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(
            result.stderr, "add_unit: unit alpha-unit has no Units row; a restore never adds one\n"
        )
        self.assert_no_launch_change(document, head)

    def test_restore_adopts_a_row_whose_worktree_and_branch_are_off_the_naming_pattern(self) -> None:
        recorded = self.commit_row_off_the_naming_pattern()
        before = self.repository_state()

        result = self.restore("trunk", recorded)

        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(result.stderr, "")
        tmux = self.new_sessions()[0]
        self.assertEqual(tmux[tmux.index("-c") + 1], str(recorded))
        self.assertEqual(self.repository_state(), before)

    def test_new_work_and_a_promote_still_refuse_a_row_off_the_naming_pattern(self) -> None:
        recorded = self.commit_row_off_the_naming_pattern()
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        promote = ("--resume", "session-123", "--cwd", str(recorded),
                   "--config-dir", str(Path(self.env["HOME"]) / ".claude"))
        for launch, flags in (("new work", ()), ("promote", promote)):
            with self.subTest(launch=launch):
                result = self.cli("trunk", "--plan", "docs/plans/given.md", *flags)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(
                    result.stderr,
                    f"add_unit: unit trunk-unit Worktree is {str(recorded)!r}; expected {str(self.checkout)!r}\n",
                )
                self.assert_no_launch_change(document, head)

    def test_restore_in_a_cwd_other_than_the_row_worktree_launches_there(self) -> None:
        self.commit_prepared_row(self.unit_row())
        worktree = self.root / "project-alpha"
        recorded = self.root / "prior-session"
        _ = self.git("worktree", "add", str(recorded), "-b", "build-followups-alpha")
        before = self.repository_state()

        result = self.restore("alpha", recorded)

        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(result.stderr, "")
        tmux = self.new_sessions()[0]
        self.assertEqual(tmux[tmux.index("-c") + 1], str(recorded))
        self.assertEqual(self.repository_state(), before)
        self.assertFalse(worktree.exists())

    def test_restore_in_the_row_worktree_on_another_branch_launches(self) -> None:
        self.commit_prepared_row(self.unit_row())
        worktree = self.root / "project-alpha"
        _ = self.git("worktree", "add", str(worktree), "-b", "other-branch")
        before = self.repository_state()

        result = self.restore("alpha", worktree)

        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.repository_state(), before)

    def test_restart_restore_is_refused_when_record_is_down(self) -> None:
        self.write_shutdown_record(self.shutdown_record("down"))
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")

        result = self.cli(
            "alpha",
            "--plan",
            "docs/plans/given.md",
            "--resume",
            "session-123",
            "--cwd",
            str(prior),
            "--restart-note",
            str(note),
            "--session-name",
            "recorded-name",
            "--host-session",
            "recorded-tmux",
         "--config-dir", str(Path(self.env["HOME"]) / ".claude"))

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("/shutdown restart first", result.stderr)
        self.assertEqual(self.events("systemd-run"), [])
        self.assertEqual(self.recorded_prompts("session-123"), ())

    def test_restart_restore_is_refused_when_record_does_not_hold_it(self) -> None:
        self.write_shutdown_record(
            self.shutdown_record("restarting", session_id="other-session")
        )
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")

        result = self.cli(
            "alpha",
            "--plan",
            "docs/plans/given.md",
            "--resume",
            "session-123",
            "--cwd",
            str(prior),
            "--restart-note",
            str(note),
            "--session-name",
            "recorded-name",
            "--host-session",
            "recorded-tmux",
         "--config-dir", str(Path(self.env["HOME"]) / ".claude"))

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("/shutdown restart first", result.stderr)
        self.assertEqual(self.events("systemd-run"), [])
        self.assertEqual(self.recorded_prompts("session-123"), ())

    def test_unreadable_shutdown_state_refuses_a_restore_with_one_prefix(self) -> None:
        account = Path(self.env["SHUTDOWN_STATE_DIR"]) / "owner@example.com"
        account.mkdir(parents=True)
        _ = (account / "record.json").write_text("{", encoding="utf-8")
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")

        result = self.cli(
            "alpha",
            "--plan",
            "docs/plans/given.md",
            "--resume",
            "session-123",
            "--cwd",
            str(prior),
            "--restart-note",
            str(note),
            "--session-name",
            "recorded-name",
            "--host-session",
            "recorded-tmux",
        )

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertTrue(
            result.stderr.startswith("add_unit: shutdown state unreadable: "),
            result.stderr,
        )
        self.assertEqual(result.stderr.count("shutdown state unreadable: "), 1)
        self.assertEqual(self.events("systemd-run"), [])
        self.assertEqual(self.recorded_prompts("session-123"), ())

    def test_refused_marked_restore_does_not_kill_tmux_or_record_prompt(self) -> None:
        self.write_shutdown_record(self.shutdown_record("down"))
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123",
            self.root / "restart-note",
            "recorded-name",
            "recorded-tmux",
        )

        result, error, commands, launches, writes = self.run_with_marked_unit(
            launch_kind, unit_lookup.ClaudeNotRunning()
        )

        self.assertEqual(result, 2)
        self.assertIn("/shutdown restart first", error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 0)
        self.assertEqual(writes, 0)
        self.assertEqual(self.recorded_prompts("session-123"), ())

    def test_launch_barrier_covers_preflight_through_spawn(self) -> None:
        note = self.root / "restart-note"
        _ = note.write_text("Resume this unit.", encoding="utf-8")
        request = add_unit.UnitLaunch(
            production=add_unit.read_production(self.doc),
            identity=add_unit.UnitIdentity("alpha-unit", "alpha"),
            branch="build-followups-alpha",
            worktree=self.root / "project-alpha",
            plan=add_unit.PlanGiven(Path("docs/plans/given.md")),
            port=add_unit.OmittedCell(),
            owns=add_unit.OmittedCell(),
            session=add_unit.ResumedSession(
                "session-123", self.root / "prior-session"
            ),
            launch_kind=add_unit.UnitRestoreLaunch(
                "session-123", note, "recorded-name", "recorded-tmux"
            ),
            timeout=1.0,
        )
        ready = add_unit.ReadyToLaunch(
            request,
            add_unit.NoUnitRow(),
            add_unit.DirectorAgent("opus", add_unit.DefaultEffort()),
        )
        old = self.shutdown_record("down")
        owner = cast(list[dict[str, object]], old["entries"])[0]
        session_shutdown: dict[str, object] = {
            "login": old["login"],
            "label": old["label"],
            "machine": old["machine"],
            "requested_at": old["requested_at"],
            "requested_by": old["requested_by"],
            "request_id": "launch-barrier-request",
            "hold": "account",
            "then": "stop",
            "force": old["force"],
            "alerts": "user",
            "seats": [],
            **owner,
        }
        creator: subprocess.Popen[str] | None = None

        def start_shutdown(_request: add_unit.UnitLaunch) -> None:
            nonlocal creator
            creator = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import os, record; assert record.create_session(record.parse_session_records(os.environ['TEST_RECORD'])[0])",
                ],
                cwd=Path(__file__).parents[1] / "shutdown",
                env={
                    **self.env,
                    "TEST_RECORD": json.dumps([session_shutdown]),
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        def launch(
            _request: add_unit.UnitLaunch,
            _tmux: str,
            _director: add_unit.DirectorAgent,
            _prompt: str,
            _selected: accounts.ClaudeLaunchAccount,
            *, target: str = "", model: str = "",
        ) -> None:
            self.assertEqual(target, "default" if isinstance(_request.session, add_unit.ResumedSession) else "")
            self.assertEqual(model, "")
            self.assertIsNotNone(creator)
            if creator is None:
                self.fail("shutdown creator did not start")
            self.assertIsNone(creator.poll(), "shutdown record appeared before spawn")
            lock_path = Path(self.env["SHUTDOWN_STATE_DIR"]) / "launch.lock"
            with lock_path.open("a+", encoding="utf-8") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(
                        contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )

        def finish_shutdown() -> None:
            self.assertIsNotNone(creator)
            if creator is None:
                self.fail("shutdown creator did not start")
            stdout, stderr = creator.communicate(timeout=300)
            self.assertEqual(creator.returncode, 0, (stdout, stderr))

        error = io.StringIO()
        with (
            patch.object(add_unit, "launch_request", return_value=request),
            patch.object(add_unit, "preflight", return_value=ready),
            patch.object(unit_lookup, "tmux_binary", return_value="tmux"),
            patch.object(
                add_unit,
                "launched_unit",
                return_value=add_unit.NoMarkedUnit(),
            ),
            patch.object(add_unit, "tmux_live", return_value=False),
            patch.object(add_unit, "write_stub", side_effect=start_shutdown),
            patch.object(add_unit, "append_row"),
            patch.object(add_unit, "commit_unit"),
            patch.object(add_unit, "ensure_worktree"),
            patch.object(add_unit, "record_scheduled_restore_prompt"),
            patch.object(add_unit, "launch_session", side_effect=launch),
            patch.object(add_unit, "wait_for_remote_control") as wait,
            patch.object(add_unit, "record"),
            patch.dict(os.environ, self.env),
            redirect_stderr(error),
        ):
            result = add_unit.main(
                ["--production", "unused", "alpha", "--plan", "unused",
                 "--config-dir", str(Path(self.env["HOME"]) / ".claude")]
            )
            finish_shutdown()

        self.assertEqual(result, 0, error.getvalue())
        wait.assert_not_called()
        account = Path(self.env["SHUTDOWN_STATE_DIR"]) / "owner@example.com"
        self.assertTrue((account / "sessions" / "session-123.json").is_file())

    def test_restore_recording_failure_uses_one_stderr_line_and_does_not_launch(self) -> None:
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123", self.root / "restart-note", "recorded-name", "recorded-tmux"
        )

        with patch.object(
            conversation_pause,
            "record_scheduled_prompts",
            side_effect=OSError("first line\nsecond line"),
        ):
            result, error, _commands, launches, _writes = self.run_with_marked_unit(
                launch_kind, unit_lookup.ClaudeNotRunning()
            )

        self.assertEqual(result, 1)
        self.assertEqual(
            error,
            "add_unit: restart prompt not recorded: first line\n",
        )
        self.assertEqual(launches, 0)

    def test_restore_keeps_marked_tmux_with_live_claude(self) -> None:
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123", self.root / "restart-note", "recorded-name", "recorded-tmux"
        )
        live = unit_lookup.LiveClaude("recorded-name", "session-123", "/socket", 123)

        result, error, commands, launches, writes = self.run_with_marked_unit(launch_kind, live)

        self.assertEqual(result, 0, error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 0)
        self.assertEqual(writes, 1)

    def test_restore_refuses_marked_tmux_when_claude_state_is_unknown(self) -> None:
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123", self.root / "restart-note", "recorded-name", "recorded-tmux"
        )

        result, error, commands, launches, writes = self.run_with_marked_unit(
            launch_kind, unit_lookup.ClaudeUnknown("session registry unreadable")
        )

        self.assertEqual(result, 2)
        self.assertIn(
            "cannot tell whether Claude runs in session marked-tmux: session registry unreadable",
            error,
        )
        self.assertEqual(commands, [])
        self.assertEqual(launches, 0)
        self.assertEqual(writes, 0)

    def test_new_work_keeps_marked_tmux_without_running_claude(self) -> None:
        result, error, commands, launches, writes = self.run_with_marked_unit(
            add_unit.NewWorkLaunch(), unit_lookup.ClaudeNotRunning()
        )

        self.assertEqual(result, 0, error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 0)
        self.assertEqual(writes, 1)

    def test_restart_restore_of_a_standby_unit_moved_to_another_account(self) -> None:
        directory = self.prepare_unit_account("claude 3")
        self.commit_prepared_row(self.unit_row(plan="standby"))
        self.write_shutdown_record(self.shutdown_record("restarting"))
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")

        result = self.successful(
            "alpha", "--standby", "--resume", "session-123", "--cwd", str(prior),
            "--restart-note", str(note), "--session-name", "recorded-name",
            "--host-session", "recorded-tmux", "--account", "claude 3",
        )

        self.assertIn("alpha-unit restore launched", result.stdout)
        self.assertEqual(len(self.events("systemd-run")), 1)
        self.assertEqual(self.recorded_unit_options("recorded-tmux")["@claude_config_dir"], str(directory))
        arguments = shlex.split(self.launch_command().removesuffix("; exec zsh"))
        self.assertEqual(arguments[arguments.index("--resume") + 1], "session-123")

    def test_resume_from_a_brief_is_refused(self) -> None:
        result = self.cli("alpha", "--brief", "Do it.", "--resume", "session-123", "--cwd", str(self.root))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--resume requires --plan, --standby, or --permanent", result.stdout + result.stderr)

    def test_standby_launch_records_state_without_writing_a_plan(self) -> None:
        _ = self.successful("alpha", "--standby", "--port", "8123", "--owns", "src/alpha", subprocess_cli=True)
        worktree = self.root / "project-alpha"
        self.assertIn(self.unit_row(plan="standby", port="8123", owns="src/alpha"),
                      self.doc.read_text())
        self.assertEqual(self.git("log", "-1", "--format=%s"),
                         "production(build-followups): add unit alpha-unit (standby)")
        self.assertEqual(self.git("show", "--pretty=format:", "--name-only", "HEAD"),
                         "docs/plans/build-followups-production.md")
        self.assertFalse((self.checkout / "docs/plans/build-followups-alpha.md").exists())
        self.assertFalse((worktree / "docs/plans/build-followups-alpha.md").exists())
        # Adding a unit registers nothing: the showrunner's update timer already names the doc.
        self.assertFalse(self.config.exists())
        self.assertEqual(self.marks("alpha"), {"SHOWRUNNER_UNIT": "build-followups", "SHOWRUNNER_UNIT_ID": "alpha-unit"})
        tmux = next(record for record in self.events("tmux")
                    if cast(list[str], record["args"])[:1] == ["new-session"])
        command = cast(list[str], tmux["args"])[-1]
        prompt = (f"You are alpha-unit in production build-followups (doc {self.doc}), "
                  "under the showrunner, whose `SendMessage` address `~/.claude/scripts/lib/py "
                  "~/.claude/scripts/production/showrunners.py address build-followups` prints (look it up "
                  "before each message: it changes when the showrunner restarts), on standby. "
                  f"Work only in your worktree {worktree}, "
                  "branch build-followups-alpha. Do nothing until the showrunner sends you work.")
        self.assertIn(prompt, command)
        self.assert_director_flags("opus", "xhigh")
        self.assertRegex(self.log.read_text(),
                         r"^- \d\d:\d\d PDT: added alpha-unit \(standby\), worktree ")

    def test_permanent_launch_writes_a_permanent_row_and_says_wrap_keeps_it(self) -> None:
        _ = self.successful("alpha", "--permanent")
        self.assertIn(self.unit_row(plan="permanent"), self.doc.read_text())
        self.assertEqual(self.git("log", "-1", "--format=%s"),
                         "production(build-followups): add unit alpha-unit (permanent)")
        tmux = next(record for record in self.events("tmux")
                    if cast(list[str], record["args"])[:1] == ["new-session"])
        self.assertIn("on standby. You are permanent: wrap never retires you. Work only in your worktree",
                      cast(list[str], tmux["args"])[-1])

    def test_the_nightly_review_unit_runs_from_rust_under_the_review_settings(self) -> None:
        profile = add_unit.launch_profile("nightly-review-unit")
        assert isinstance(profile, add_unit.ProfiledLaunch)
        self.assertEqual(profile.cwd, Path.home() / "rust")
        settings = profile.flags[profile.flags.index("--settings") + 1]
        self.assertEqual(Path(settings), Path(add_unit.__file__).resolve().parents[1] / "nightly_review/settings.json")
        self.assertIn("/etc/nixos", profile.flags)
        self.assertIsInstance(add_unit.launch_profile("alpha-unit"), add_unit.StandardLaunch)

    def test_adopt_refuses_a_plan_or_a_resume(self) -> None:
        for args in (("--plan", "docs/plans/given.md", "--adopt", "nightly-review"),
                     ("--standby", "--adopt", "nightly-review", "--resume", "session-123", "--cwd", str(self.root))):
            with self.subTest(args=args):
                result = self.cli("alpha", *args)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertIn("--adopt takes --standby or --permanent", result.stderr)

    def test_standby_rejects_other_modes_before_any_change(self) -> None:
        original = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        for args in (("--standby", "--plan", "docs/plans/given.md"),
                     ("--standby", "--brief", "Write a plan")):
            with self.subTest(args=args):
                result = self.cli("alpha", *args)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(len((result.stdout + result.stderr).strip().splitlines()), 1)
                self.assertEqual(self.doc.read_text(), original)
                self.assertEqual(self.git("rev-parse", "HEAD"), head)
                self.assertFalse(self.config.exists())
                self.assertFalse(self.log.exists())
                self.assertFalse((self.root / "project-alpha").exists())
                self.assertEqual(self.events("systemd-run"), [])

    def test_plan_adds_row_commits_worktree_launches_and_records(self) -> None:
        pushed = self.git("--git-dir", str(self.origin), "rev-parse", "refs/heads/build-followups")
        result = self.successful("alpha", "--plan", "docs/plans/given.md", "--port", "8123",
                                 "--owns", "src/alpha")
        worktree = self.root / "project-alpha"
        row = self.unit_row(port="8123", owns="src/alpha")
        self.assertIn(row, self.doc.read_text())
        self.assertEqual(self.git("show", "--pretty=format:", "--name-only", "HEAD"),
                         "docs/plans/build-followups-production.md")
        self.assertIn("production(build-followups): add unit alpha-unit (plan)",
                      self.git("log", "-1", "--format=%s"))
        # An add pushes nothing: the unit's commit waits for the next merge push, and the unit branch
        # for its first checkpoint push, which sets the upstream.
        self.assertEqual(self.git("--git-dir", str(self.origin), "rev-parse", "refs/heads/build-followups"), pushed)
        self.assertEqual(self.git("--git-dir", str(self.origin), "for-each-ref", "--format=%(refname)", "refs/heads"),
                         "refs/heads/build-followups")
        self.assertEqual(self.git("for-each-ref", "--format=%(refname) upstream=%(upstream)",
                                  "refs/heads/build-followups-alpha"),
                         "refs/heads/build-followups-alpha upstream=")
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree),
                         "build-followups-alpha")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=worktree),
                         self.git("rev-parse", "build-followups"))
        launch = self.events("systemd-run")
        self.assertEqual(len(launch), 1)
        systemd_args = cast(list[str], launch[0]["args"])
        self.assertEqual(systemd_args[:2], ["--user", "--scope"])
        self.assertRegex(systemd_args[2], r"^--unit=alpha-[0-9]+$")
        self.assertEqual(launch[0]["claude_env"], [])
        tmux = next(record for record in self.events("tmux")
                    if cast(list[str], record["args"])[:1] == ["new-session"])
        args = cast(list[str], tmux["args"])
        self.assertEqual(args[:11], ["new-session", "-d", "-s", "alpha", "-c", str(worktree),
                                     "-e", "SHOWRUNNER_UNIT=build-followups", "-e", "SHOWRUNNER_UNIT_ID=alpha-unit",
                                     "zsh"])
        self.assertIn("claude --model opus --effort xhigh --remote-control alpha -n alpha", args[-1])
        self.assert_director_flags("opus", "xhigh")
        self.assertIn("'/unit:direct docs/plans/given.md'", args[-1])
        self.assertEqual(
            args[-1],
            "unset CLAUDE_CONFIG_DIR CLAUDE_CODE_REMOTE_MEMORY_DIR CLAUDE_CODE_OAUTH_TOKEN; ENABLE_TOOL_SEARCH=true command " + shlex.join([
                "claude", "--model", "opus", "--effort", "xhigh",
                "--remote-control", "alpha", "-n", "alpha",
                "--settings", '{"disableAgentView": true}',
                "/unit:direct docs/plans/given.md",
            ]) + "; exec zsh",
        )
        # Adding a unit registers nothing: the showrunner's update timer already names the doc.
        self.assertFalse(self.config.exists())
        self.assertRegex(self.log.read_text(),
                         r"^- \d\d:\d\d PDT: added alpha-unit \(plan\), worktree ")
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertIn("alpha-unit started: tmux attach -t alpha", result.stdout)

    def test_review_unit_uses_an_ordinary_row_and_worktree_with_review_direct_opening(self) -> None:
        _ = self.doc.write_text(self.doc.read_text().replace("## Units", "- **Review pool:** code\n\n## Units"))
        _ = self.successful("review", "--plan", "docs/plans/given.md")
        worktree = self.root / "project-review"
        self.assertTrue(worktree.is_dir())
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree),
                         "build-followups-review")
        row = next(line for line in self.doc.read_text().splitlines() if line.startswith("| review-unit |"))
        self.assertEqual([cell.strip() for cell in row.strip("|").split("|")],
                         ["review-unit", "docs/plans/given.md", str(worktree),
                          "build-followups-review", "—", "—"])
        self.assertTrue(row.endswith("| — | — |"), row)
        self.assertEqual(self.marks("review"),
                         {"SHOWRUNNER_UNIT": "build-followups", "SHOWRUNNER_UNIT_ID": "review-unit"})
        self.assertIn(shlex.quote(f"/review:direct {self.doc}"), self.launch_command())
        self.assertNotIn("/unit:direct", self.launch_command())

    def test_promoted_review_unit_uses_review_direct_with_its_identity(self) -> None:
        _ = self.doc.write_text(self.doc.read_text().replace("## Units", "- **Review pool:** code\n\n## Units"))
        _ = self.successful("review-unit", "--plan", "docs/plans/given.md",
                            "--resume", "session-123", "--cwd", str(self.checkout), "--config-dir", str(Path(self.env["HOME"]) / ".claude"))
        command = self.launch_command()
        self.assertIn("You are now review-unit in production build-followups", command)
        self.assertIn(f"Run /review:direct {self.doc}.", command)
        self.assertNotIn("Run /unit:direct", command)

    def test_review_named_unit_without_a_pool_keeps_unit_direct_opening(self) -> None:
        _ = self.successful("review", "--plan", "docs/plans/given.md")
        self.assertIn(shlex.quote("/unit:direct docs/plans/given.md"), self.launch_command())
        self.assertNotIn("/review:direct", self.launch_command())

    def test_named_unit_account_is_quoted_inside_tmux_command(self) -> None:
        home = self.root / "home with 'quotes' $()"
        _ = Path(self.env["HOME"]).rename(home)
        self.env["HOME"] = str(home)
        self.env["CLAUDE_CONFIG_DIR"] = str(self.root / "caller account")
        directory = self.prepare_unit_account("claude 3")

        _ = self.successful("alpha", "--plan", "docs/plans/given.md")

        self.assertEqual(
            self.launch_command(),
            self.named_prefix(directory)
            + "ENABLE_TOOL_SEARCH=true command " + shlex.join([
                "claude", "--model", "opus", "--effort", "xhigh",
                "--remote-control", "alpha", "-n", "alpha",
                "--settings", '{"disableAgentView": true}',
                "/unit:direct docs/plans/given.md",
            ]) + "; exec zsh",
        )
        launches = [
            *self.events("systemd-run"),
            *(event for event in self.events("tmux")
              if cast(list[str], event["args"])[:1] == ["new-session"]),
        ]
        for event in launches:
            self.assertEqual(event["claude_env"], [])

    def test_held_named_unit_account_refuses_new_work_before_changes(self) -> None:
        """F001: the launch guard checks the selected unit account's shutdown."""
        _ = self.prepare_unit_account("claude 3")
        login = "claude-3@example.invalid"
        held = self.shutdown_record("down")
        held["login"] = login
        held["label"] = "claude 3"
        self.write_shutdown_record(held, login=login)
        document = self.doc.read_text(encoding="utf-8")
        head = self.git("rev-parse", "HEAD")

        result = self.cli("alpha", "--plan", "docs/plans/given.md")

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(
            result.stderr,
            "add_unit: claude 3 is held by a shutdown "
            + "(since 2026-10-09 14:49 PDT); /shutdown restart first\n",
        )
        self.assert_no_launch_change(document, head)

    def test_default_account_shutdown_does_not_block_a_named_unit(self) -> None:
        """F001: another account's shutdown does not block the selected unit account."""
        directory = self.prepare_unit_account("claude 3")
        self.write_shutdown_record(self.shutdown_record("down"))

        _ = self.successful("alpha", "--plan", "docs/plans/given.md")

        self.assertTrue(self.launch_command().startswith(
            self.named_prefix(directory)
        ))

    def test_default_unit_guard_environment_is_unchanged(self) -> None:
        """F001: default selection preserves the original sanitized guard environment."""
        self.env["CLAUDE_CONFIG_DIR"] = str(self.root / "caller account")
        with (
            patch.dict(os.environ, self.env),
            patch.object(
                launch_permission, "launch_permission",
                return_value=launch_permission.AllowedByShutdownState(),
            ) as permission,
        ):
            expected = {
                key: value for key, value in os.environ.items()
                if not key.startswith("CLAUDE_")
            }
            result = add_unit.main([
                "--production", str(self.doc), "alpha",
                "--plan", "docs/plans/given.md", "--check",
            ])

        self.assertEqual(result, 0)
        permission.assert_called_once_with(
            expected, launch_permission.NewWorkLaunchPurpose()
        )

    def test_better_candidate_after_readiness_keeps_the_stored_group_account(self) -> None:
        """Better readings neither replace the stored group nor the checked launch account."""
        directory = self.prepare_unit_account("claude 3")
        ensure_worktree = add_unit.ensure_worktree

        def change_role_after_worktree(request: add_unit.UnitLaunch) -> None:
            ensure_worktree(request)
            _ = self.prepare_unit_account("claude 1")
            self.quota_reading("claude-1@example.invalid", 1, 1)
            _ = self.select_unit_account("claude 3")

        with (
            patch.dict(os.environ, self.env),
            patch.object(accounts, "role_account", wraps=accounts.role_account) as selection,
            patch.object(accounts, "check", wraps=accounts.check) as readiness,
            patch.object(add_unit, "ensure_worktree", side_effect=change_role_after_worktree),
        ):
            result = add_unit.main([
                "--production", str(self.doc), "alpha", "--plan", "docs/plans/given.md",
            ])

        self.assertEqual(result, 0)
        selection.assert_called_once_with("unit")
        self.assertIn("claude 3", [call.args[0] for call in readiness.call_args_list])
        self.assertTrue(self.launch_command().startswith(
            self.named_prefix(directory)
        ))
        self.assertTrue((directory.parent / ".claude1").exists())
        with patch.dict(os.environ, self.env):
            self.assertEqual(
                accounts.role_account("unit"),
                accounts.NamedAccount("claude 3", directory),
            )

    def test_new_unit_launch_records_default_account_for_a_later_resume(self) -> None:
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.recorded_unit_options("alpha")["@claude_config_dir"], str(Path(self.env["HOME"]) / ".claude"))

    def test_new_unit_launch_records_named_account_for_a_later_resume(self) -> None:
        directory = self.prepare_unit_account("claude 3")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.recorded_unit_options("alpha")["@claude_config_dir"], str(directory))

    def test_explicit_unit_account_move_records_target_account_for_a_later_resume(self) -> None:
        directory = self.prepare_unit_account("claude 3")
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(prior), "--account", "claude 3")
        self.assertEqual(self.recorded_unit_options("alpha")["@claude_config_dir"], str(directory))

    def test_manual_unit_resume_keeps_its_recorded_model_despite_changed_director_configuration(self) -> None:
        self.write_agent_config(director="sonnet:high")
        directory = self.prepare_unit_account("claude 3")
        self.persist_unit_account(directory, session="alpha", model="opus")
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(prior))
        arguments = shlex.split(self.launch_command().removesuffix("; exec zsh"))
        self.assertEqual(arguments[arguments.index("--model") + 1], "opus")
        self.assertEqual(self.recorded_unit_options("alpha")["@claude_model"], "opus")

    def test_unit_resume_uses_configured_account_instead_of_recorded_account(self) -> None:
        """F004: the configured units account decides resumes too."""
        old = self.prepare_unit_account("claude 2")
        caller = self.prepare_unit_account("claude 3")
        directory = self.prepare_unit_account("claude 1")
        self.env["CLAUDE_CONFIG_DIR"] = str(caller)
        self.persist_unit_account(old, session="alpha")
        prior = self.root / "prior-session"
        prior.mkdir()
        with (patch.dict(os.environ, self.env),
              patch.object(accounts, "prepared_account", wraps=accounts.prepared_account) as prepared):
            code = add_unit.main(["--production", str(self.doc), "alpha", "--plan", "docs/plans/given.md",
                "--resume", "session-123", "--cwd", str(prior)])
        self.assertEqual(code, 0)
        prepared.assert_any_call("claude 1", resume=True)
        self.assertEqual(self.recorded_unit_options("alpha")["@claude_config_dir"], str(directory))
        self.assertIn("--resume session-123", self.launch_command())

    def test_unit_resume_uses_configured_account_instead_of_old_named_account(self) -> None:
        self.commit_prepared_row(self.unit_row())
        directory = self.prepare_unit_account("claude 1")
        other_directory = self.prepare_unit_account("claude 3")
        self.env["CLAUDE_CONFIG_DIR"] = str(other_directory)
        self.persist_unit_account(directory)
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")

        _ = self.successful(
            "alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(prior),
            "--restart-note", str(note),
            "--session-name", "recorded-name", "--host-session", "recorded-tmux",
        )

        self.assertEqual(self.recorded_unit_options("recorded-tmux")["@claude_config_dir"], str(other_directory))
        self.assertIn("exec 'claude 3'", self.launch_command())
        self.assertIn("--resume session-123", self.launch_command())
        self.assertNotIn(str(directory), self.launch_command())

    def test_unit_resume_without_recorded_account_uses_configured_units(self) -> None:
        """F004: no retained account is needed once the group is configured."""
        directory = self.prepare_unit_account("claude 1")
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md",
            "--resume", "unrecorded-session", "--cwd", str(prior))
        self.assertEqual(self.recorded_unit_options("alpha")["@claude_config_dir"], str(directory))

    def test_unit_resume_refuses_config_directory_outside_configured_account(self) -> None:
        """F004: an explicit directory cannot replace the units account."""
        directory = self.root / "external account with spaces"
        directory.mkdir()
        _ = self.prepare_unit_account("claude 1")
        before = self.repository_state()
        result = self.cli("alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(self.checkout), "--config-dir", str(directory))
        self.assertEqual(result.returncode, 2)
        self.assertIn("claude 1", result.stderr)
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.events("systemd-run"), [])

    def test_unit_resume_refuses_missing_directory_that_differs_from_configured_default(self) -> None:
        """F004: an override mismatch names configuration, not a missing former account."""
        directory = self.root / "gone-account"
        prior = self.root / "prior-session"
        prior.mkdir()
        before = self.repository_state()
        result = self.cli("alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(prior), "--config-dir", str(directory))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("units account is default", result.stderr)
        self.assertIn("--account and --config-dir must name default", result.stderr)
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.events("systemd-run"), [])

    def test_resumed_unit_uses_its_recorded_model_despite_a_different_director_configuration(self) -> None:
        self.write_agent_config(director="sonnet:high")
        directory = self.prepare_unit_account("claude 3")
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(prior), "--account", "claude 3", "--model", "opus")
        arguments = shlex.split(self.launch_command().removesuffix("; exec zsh"))
        self.assertEqual(arguments[arguments.index("--model") + 1], "opus")
        self.assertNotIn(str(directory), self.launch_command())

    def test_unit_resume_refuses_account_that_differs_from_configured_units(self) -> None:
        """F004: --account must agree with the configured group."""
        _ = self.prepare_unit_account("claude 2")
        _ = self.prepare_unit_account("claude 1")
        before = self.repository_state()
        result = self.cli("alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(self.checkout), "--account", "claude 2")
        self.assertEqual(result.returncode, 2)
        self.assertIn("claude 1", result.stderr)
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.events("systemd-run"), [])

    def test_unconfigured_units_refuse_new_standby_and_resume_without_writes(self) -> None:
        """F004: every unit launch mode requires explicit configuration."""
        with patch.dict(os.environ, self.env):
            accounts.groups_file().unlink()
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        for mode in (("--plan", "docs/plans/given.md"), ("--standby",),
                     ("--plan", "docs/plans/given.md", "--resume", "session-123", "--cwd", str(self.checkout))):
            with self.subTest(mode=mode):
                result = self.cli("alpha", *mode)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("accounts.py move units <claude N>", result.stderr)
                self.assert_no_launch_change(document, head)
                self.assertFalse((Path(self.env["HOME"]) / ".local/state/accounts/groups.json").exists())

    def test_new_unit_refuses_unfit_configured_account_without_default_fallback(self) -> None:
        """F004: a configured account cannot silently fall back to default."""
        directory = self.select_unit_account("claude 3")
        directory.mkdir()
        before = self.repository_state()
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2)
        self.assertIn("claude3", result.stderr)
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.events("systemd-run"), [])

    def test_new_unit_and_standby_use_configured_units_despite_showrunner_caller(self) -> None:
        """F004: caller identity does not decide either new unit launch mode."""
        caller = self.prepare_unit_account("claude 3")
        directory = self.prepare_unit_account("claude 1")
        self.env["CLAUDE_CONFIG_DIR"] = str(caller)
        path = Path(self.env["HOME"]) / ".local/state/accounts/groups.json"
        _ = path.write_text(json.dumps({"units": "claude 1", "showrunners": "claude 3"}))
        for name, mode in (("alpha", ("--plan", "docs/plans/given.md")), ("beta", ("--standby",))):
            with self.subTest(mode=mode):
                _ = self.successful(name, *mode)
                self.assertEqual(self.recorded_unit_options(name)["@claude_config_dir"], str(directory))

    def test_unit_accepts_explicit_account_matching_configured_units(self) -> None:
        """F004: matching --account selects no alternative account."""
        directory = self.prepare_unit_account("claude 1")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--account", "claude 1")
        self.assertEqual(self.recorded_unit_options("alpha")["@claude_config_dir"], str(directory))

    def test_units_and_showrunners_may_share_the_launch_account(self) -> None:
        """F004: sharing one account is valid configuration."""
        directory = self.prepare_unit_account("claude 1")
        path = Path(self.env["HOME"]) / ".local/state/accounts/groups.json"
        _ = path.write_text(json.dumps({"units": "claude 1", "showrunners": "claude 1"}))
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.recorded_unit_options("alpha")["@claude_config_dir"], str(directory))

    def test_ready_unit_account_passes_check_without_launch_changes(self) -> None:
        _ = self.prepare_unit_account("claude 3")
        document = self.doc.read_text(encoding="utf-8")
        head = self.git("rev-parse", "HEAD")

        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--check")

        self.assert_no_launch_change(document, head)

    def test_a_doc_that_still_has_the_session_column_gets_a_dash_there(self) -> None:
        _ = self.doc.write_text(self.production_doc(session_column=True), encoding="utf-8")
        _ = self.git("commit", "-am", "an older production doc")
        _ = self.git("push", "origin", "build-followups")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--port", "8123")
        self.assertIn(f"| alpha-unit | docs/plans/given.md | {self.root / 'project-alpha'} | build-followups-alpha "
                      + "| — | 8123 | — |", self.doc.read_text())

    def test_a_rerun_finds_the_unit_by_its_mark_after_its_session_was_renamed(self) -> None:
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        for kind, suffix in (("live", ""), ("env", ".json")):
            _ = (self.state / f"{kind}-alpha{suffix}").rename(self.state / f"{kind}-renamed{suffix}")
        result = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(len(self.new_sessions()), 1)
        self.assertIn("alpha-unit started: tmux attach -t renamed", result.stdout)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_sonnet_director_row_changes_launch_model_and_effort(self) -> None:
        self.write_agent_config(director="sonnet:xhigh")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assert_director_flags("sonnet", "xhigh")

    def test_director_row_without_effort_uses_claude_default(self) -> None:
        self.write_agent_config(director="opus")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assert_director_flags("opus", None)

    def test_codex_director_refused_before_any_change_with_or_without_check(self) -> None:
        self.write_agent_config(family="codex")
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        for check in (False, True):
            with self.subTest(check=check):
                arguments = ("alpha", "--plan", "docs/plans/given.md")
                result = self.cli(*arguments, *(("--check",) if check else ()))
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr.strip().splitlines(), [
                    "add_unit: unit directors launch only on claude; production.director resolves to codex (gpt-test)"])
                self.assert_no_launch_change(document, head)

    def test_missing_claude_director_set_returns_resolver_error_before_change(self) -> None:
        self.write_agent_config(claude_set=False)
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.strip().splitlines(), [
            f"add_unit: ERROR: [production.director] missing set section [production.claude] in {self.agent_config}."])
        self.assert_no_launch_change(document, head)

    def test_prepared_row_launches_without_rewriting_or_committing_it(self) -> None:
        self.commit_prepared_row(self.prepared_row())
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.doc.read_text(), document)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertNotIn("add unit", self.git("log", "-1", "--format=%s"))
        self.assertEqual(self.git("branch", "--show-current", cwd=self.root / "project-alpha"),
                         "build-followups-alpha")
        self.assertEqual(len(self.events("systemd-run")), 1)
        # Adding a unit registers nothing: the showrunner's update timer already names the doc.
        self.assertFalse(self.config.exists())
        self.assertIn("added alpha-unit (plan)", self.log.read_text())

    def test_prepared_row_refuses_mismatched_identity_cells_before_launch(self) -> None:
        cases = (("Plan", self.prepared_row(plan="docs/plans/other.md"),
                  "docs/plans/other.md", "docs/plans/given.md"),
                 ("Branch", self.prepared_row(branch="other-branch"),
                  "other-branch", "build-followups-alpha"),
                 ("Worktree", self.prepared_row(worktree=self.root / "other-worktree"),
                  str(self.root / "other-worktree"), str(self.root / "project-alpha")))
        for cell, row, actual, expected in cases:
            with self.subTest(cell=cell):
                self.commit_prepared_row(row)
                document = self.doc.read_text()
                head = self.git("rev-parse", "HEAD")
                result = self.cli("alpha", "--plan", "docs/plans/given.md")
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(len(result.stderr.strip().splitlines()), 1)
                self.assertIn(cell, result.stderr)
                self.assertIn(f"is {actual!r}; expected {expected!r}", result.stderr)
                self.assert_no_launch_change(document, head)

    def test_prepared_row_refuses_different_supplied_port_before_launch(self) -> None:
        self.commit_prepared_row(self.prepared_row())
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md", "--port", "9000")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertIn("Port is '8123'; expected '9000'", result.stderr)
        self.assert_no_launch_change(document, head)

    def test_prepared_row_refuses_different_supplied_owns_before_launch(self) -> None:
        self.commit_prepared_row(self.prepared_row())
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md", "--owns", "src/beta")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertIn("Owns is 'src/alpha'; expected 'src/beta'", result.stderr)
        self.assert_no_launch_change(document, head)

    def test_prepared_standby_row_uses_standby_mode(self) -> None:
        self.commit_prepared_row(self.prepared_row(plan="standby"))
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        _ = self.successful("alpha", "--standby")
        self.assertEqual(self.doc.read_text(), document)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertIn("added alpha-unit (standby)", self.log.read_text())

    def test_other_prepared_row_reserves_backticked_branch(self) -> None:
        self.commit_prepared_row(self.prepared_row(name="beta"))
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertIn("branch or worktree", result.stderr)
        self.assert_no_launch_change(document, head)

    def test_check_resume_preflights_without_any_launch_side_effect(self) -> None:
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123",
                          "--cwd", str(self.root / "prior-session"), "--check", "--config-dir", str(Path(self.env["HOME"]) / ".claude"))
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assert_no_launch_change(document, head)

    def test_check_refuses_invalid_session_name_without_any_change(self) -> None:
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("new director", "--plan", "docs/plans/given.md", "--resume", "session-123",
                          "--cwd", str(self.root / "prior-session"), "--check", "--config-dir", str(Path(self.env["HOME"]) / ".claude"))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertIn("name must contain only letters", result.stderr)
        self.assert_no_launch_change(document, head)

    def test_berth_target_is_set_only_when_berth_config_exists(self) -> None:
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertNotIn("cargoBerthTarget", self.git("config", "--local", "--list"))

    def test_berth_target_is_set_when_config_exists(self) -> None:
        berth = self.checkout / ".claude/config/berth.toml"
        _ = berth.parent.mkdir(parents=True)
        _ = berth.write_text("", encoding="utf-8")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.git("config", "branch.build-followups-alpha.cargoBerthTarget"),
                         "build-followups")

    def test_brief_commits_stub_with_verbatim_words_and_approval_prompt(self) -> None:
        words = "Investigate the slow build.\nKeep the user's exact wording."
        result = self.successful("alpha", "--brief", words)
        stub = self.checkout / "docs/plans/build-followups-alpha.md"
        self.assertTrue(stub.exists())
        content = stub.read_text()
        self.assertIn("# alpha", content)
        self.assertIn("> **Production: build-followups** — unit `alpha-unit`; production doc", content)
        self.assertIn("## Source", content)
        self.assertIn(words, content)
        self.assertRegex(content, r"\d{4}-\d\d-\d\d.*PDT")
        self.assertIn(self.unit_row(plan="docs/plans/build-followups-alpha.md"), self.doc.read_text())
        self.assertEqual(set(self.git("show", "--pretty=format:", "--name-only", "HEAD").splitlines()),
                         {"docs/plans/build-followups-production.md", "docs/plans/build-followups-alpha.md"})
        tmux = next(record for record in self.events("tmux")
                    if cast(list[str], record["args"])[:1] == ["new-session"])
        prompt = cast(list[str], tmux["args"])[-1]
        self.assertIn("Write the full phased plan there", prompt)
        self.assertIn("wait for its approval before you run /unit:direct docs/plans/build-followups-alpha.md", prompt)
        self.assertIn("alpha-unit started: tmux attach -t alpha", result.stdout)
        self.assert_director_flags("opus", "xhigh")

    def test_branch_takes_the_slug_and_the_stub_sits_beside_the_production_doc(self) -> None:
        _ = self.git("checkout", "-b", "init/catalyst")
        self.doc = self.checkout / "docs/hana/tool-based-ui-production.md"
        _ = self.doc.parent.mkdir(parents=True)
        _ = self.doc.write_text(self.production_doc("init/catalyst"), encoding="utf-8")
        _ = self.git("add", "docs/hana/tool-based-ui-production.md")
        _ = self.git("commit", "-m", "production under another merge branch")
        _ = self.successful("lfo", "--brief", "Add the LFO tool")
        worktree = self.root / "project-lfo"
        self.assertIn(self.unit_row("lfo", branch="tool-based-ui-lfo", worktree=worktree,
                                    plan="docs/hana/tool-based-ui-lfo.md"), self.doc.read_text())
        self.assertEqual(self.git("branch", "--show-current", cwd=worktree), "tool-based-ui-lfo")
        self.assertEqual(set(self.git("show", "--pretty=format:", "--name-only", "HEAD").splitlines()),
                         {"docs/hana/tool-based-ui-production.md", "docs/hana/tool-based-ui-lfo.md"})
        self.assertFalse((self.checkout / "docs/plans/init").exists())
        self.assertEqual(self.git("branch", "--list", "init/catalyst-lfo"), "")

    def test_timeout_keeps_session_then_rerun_finishes_without_duplicate_steps(self) -> None:
        _ = (self.state / "ready").unlink()
        first = self.cli("alpha", "--plan", "docs/plans/given.md", "--timeout", "0.2")
        self.assertEqual(first.returncode, 1, (first.stdout, first.stderr))
        self.assertIn("alpha", first.stdout + first.stderr)
        self.assertIn("Waiting for remote control", first.stdout + first.stderr)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.log.exists())
        commit = self.git("rev-parse", "HEAD")
        _ = (self.state / "ready").touch()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.git("rev-parse", "HEAD"), commit)
        self.assertEqual(self.doc.read_text().count("| alpha-unit |"), 1)
        self.assertEqual(len([event for event in self.events("tmux")
                              if cast(list[str], event["args"])[:1] == ["new-session"]]), 1)
        # Adding a unit registers nothing: the showrunner's update timer already names the doc.
        self.assertFalse(self.config.exists())
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_refusals_change_no_repository_or_registry_state(self) -> None:
        cases: tuple[tuple[str, tuple[str, ...]], ...] = (
            ("no mode", ("alpha",)),
            ("two modes", ("alpha", "--plan", "docs/plans/given.md", "--brief", "words")),
            ("missing plan", ("alpha", "--plan", "docs/plans/missing.md")),
        )
        initial = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        for label, arguments in cases:
            with self.subTest(label=label):
                result = self.cli(*arguments)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(len((result.stdout + result.stderr).strip().splitlines()), 1)
                self.assertEqual(self.doc.read_text(), initial)
                self.assertEqual(self.git("rev-parse", "HEAD"), head)
                self.assertFalse(self.config.exists())

    def test_refuses_plan_without_production_header(self) -> None:
        _ = self.plan.write_text("# No production header\n", encoding="utf-8")
        _ = self.git("add", "docs/plans/given.md")
        _ = self.git("commit", "-m", "remove production header")
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertNotIn("| alpha-unit |", self.doc.read_text())

    def test_refuses_missing_production_field(self) -> None:
        original = self.doc.read_text()
        _ = self.doc.write_text(original.replace("- **Log:** `docs/plans/build-followups-log.md` — git-excluded\n", ""))
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertNotIn("| alpha-unit |", self.doc.read_text())

    def test_a_doc_without_a_user_zone_line_still_adds_a_unit(self) -> None:
        # The zone is the user's setting; a production doc no longer has to name one.
        original = self.doc.read_text()
        _ = self.doc.write_text(original.replace("- **User zone:** America/Los_Angeles — reports use PDT\n", ""))
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertIn("| alpha-unit |", self.doc.read_text())

    def test_refuses_checkout_off_merge_branch(self) -> None:
        _ = self.git("checkout", "-b", "wrong-branch")
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertNotIn("| alpha-unit |", self.doc.read_text())

    def test_refuses_taken_row_and_unowned_live_tmux_session(self) -> None:
        original = self.doc.read_text()
        _ = self.doc.write_text(original.replace("## Gates", self.unit_row(branch="different") + "\n\n## Gates"))
        taken = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(taken.returncode, 2, (taken.stdout, taken.stderr))
        _ = self.doc.write_text(original)
        _ = (self.state / "live-alpha").touch()
        live = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(live.returncode, 2, (live.stdout, live.stderr))
        self.assertIn("tmux session alpha is already live and is not marked as alpha-unit", live.stderr)
        self.assertEqual(self.doc.read_text(), original)
        self.assertEqual(len(self.events("systemd-run")), 0)

    def test_resume_uses_session_id_cwd_and_promote_prompt(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123",
                            "--cwd", str(prior), "--config-dir", str(Path(self.env["HOME"]) / ".claude"))
        tmux = next(record for record in self.events("tmux")
                    if cast(list[str], record["args"])[:1] == ["new-session"])
        args = cast(list[str], tmux["args"])
        self.assertEqual(args[args.index("-c") + 1], str(prior))
        command = args[-1]
        self.assertTrue(command.startswith("unset CLAUDE_CONFIG_DIR CLAUDE_CODE_REMOTE_MEMORY_DIR CLAUDE_CODE_OAUTH_TOKEN; ENABLE_TOOL_SEARCH=true command "))
        self.assertIn("accounts.py exec default", command)
        self.assertNotIn("CLAUDE_CONFIG_DIR=", command)
        self.assertIn("--model opus --effort xhigh --resume session-123 --remote-control alpha -n alpha", command)
        self.assert_director_flags("opus", "xhigh")
        self.assertIn("You are now alpha-unit in production build-followups", command)
        promoted_plan = self.root / "project-alpha/docs/plans/given.md"
        self.assertTrue(promoted_plan.exists())
        self.assertIn(f"Run /unit:direct {promoted_plan}", command)

    def test_all_restore_flags_parse_as_a_unit_restore_and_other_launches_do_not(self) -> None:
        note = self.root / "restart note.txt"
        _ = note.write_text("resume here", encoding="utf-8")
        common = {
            "production": str(self.doc),
            "name": "alpha",
            "plan": "docs/plans/given.md",
            "brief": None,
            "standby": False,
            "permanent": False,
            "adopt": None,
            "port": None,
            "owns": None,
            "timeout": 90.0,
            "check": False,
        }
        restored = add_unit.launch_request(argparse.Namespace(
            **common,
            resume="session-123",
            cwd=str(self.root / "prior-session"),
            restart_note=str(note),
            session_name="recorded name",
            host_session="recorded tmux",
        ))
        ordinary_resume = add_unit.launch_request(argparse.Namespace(
            **common,
            resume="session-123",
            cwd=str(self.root / "prior-session"),
            restart_note=None,
            session_name=None,
            host_session=None,
        ))
        new_work = add_unit.launch_request(argparse.Namespace(
            **common,
            resume=None,
            cwd=None,
            restart_note=None,
            session_name=None,
            host_session=None,
        ))

        self.assertEqual(
            restored.launch_kind,
            add_unit.UnitRestoreLaunch(
                "session-123", note, "recorded name", "recorded tmux"
            ),
        )
        self.assertIsInstance(ordinary_resume.launch_kind, add_unit.NewWorkLaunch)
        self.assertIsInstance(new_work.launch_kind, add_unit.NewWorkLaunch)

    def test_a_name_ending_in_unit_launches_as_the_name_without_it(self) -> None:
        def request(name: str) -> tuple[str, str, Path, str]:
            launched = add_unit.launch_request(argparse.Namespace(
                production=str(self.doc), name=name, plan="docs/plans/given.md", brief=None,
                standby=False, permanent=False, adopt=None, port=None, owns=None, timeout=90.0, check=False,
                resume=None, cwd=None, restart_note=None, session_name=None, host_session=None,
            ))
            return launched.unit, launched.branch, launched.worktree, launched.requested_name

        expected = request("feature-log")
        self.assertEqual(expected[0], "feature-log-unit")
        self.assertEqual(request("feature-log-unit"), expected)
        self.assertEqual(request("feature_log_unit"), request("feature_log"))
        self.assertEqual(request("unit")[0], "unit-unit")

    def test_restore_flags_are_atomic_and_require_resume(self) -> None:
        note = self.root / "restart-note.txt"
        _ = note.write_text("resume here", encoding="utf-8")
        for flags in (
            ("--restart-note", str(note)),
            ("--session-name", "recorded-name"),
            ("--host-session", "recorded-tmux"),
            (
                "--restart-note", str(note),
                "--session-name", "recorded-name",
                "--host-session", "recorded-tmux",
            ),
        ):
            with self.subTest(flags=flags):
                result = self.cli("alpha", "--plan", "docs/plans/given.md", *flags)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(self.events("systemd-run"), [])

    def test_restore_note_recorded_names_and_unique_scope_drive_the_launch(self) -> None:
        self.commit_prepared_row(self.unit_row())
        prior = self.root / "prior session"
        prior.mkdir()
        note = self.root / "restart note.txt"
        restart_note = "Restarted with spaces, 'quotes', and $() unchanged."
        _ = note.write_text(restart_note, encoding="utf-8")
        recorded_session = "restored 'session' $()"
        recorded_tmux = "restore $()"
        existing_prompt = "Keep this previously scheduled prompt."
        with patch.dict(os.environ, self.env):
            conversation_pause.record_scheduled_prompts(
                "session-123", (existing_prompt,)
            )

        _ = self.successful(
            "alpha",
            "--plan", "docs/plans/given.md",
            "--resume", "session-123",
            "--cwd", str(prior),
            "--restart-note", str(note),
            "--session-name", recorded_session,
            "--host-session", recorded_tmux,
         "--config-dir", str(Path(self.env["HOME"]) / ".claude"))

        tmux = self.new_sessions()[0]
        self.assertEqual(tmux[tmux.index("-s") + 1], recorded_tmux)
        self.assertEqual(tmux[tmux.index("-c") + 1], str(prior))
        has_targets = [cast(list[str], event["args"])[-1] for event in self.events("tmux")
                       if cast(list[str], event["args"])[:1] == ["has-session"]]
        self.assertTrue(has_targets)
        self.assertEqual(set(has_targets), {f"={recorded_tmux}"})

        command = tmux[-1]
        self.assertTrue(command.startswith("unset CLAUDE_CONFIG_DIR CLAUDE_CODE_REMOTE_MEMORY_DIR CLAUDE_CODE_OAUTH_TOKEN; ENABLE_TOOL_SEARCH=true command "))
        self.assertIn("accounts.py exec default", command)
        self.assertNotIn("CLAUDE_CONFIG_DIR=", command)
        encoded = command.split(" command ", 1)[1].rsplit("; exec zsh", 1)[0]
        claude = shlex.split(encoded)
        self.assertEqual(claude[claude.index("--remote-control") + 1], recorded_session)
        self.assertEqual(claude[claude.index("-n") + 1], recorded_session)
        self.assertIn("You are now alpha-unit in production build-followups", claude[-1])
        self.assertTrue(claude[-1].endswith(restart_note), claude[-1])
        self.assertNotIn("Run /unit:direct", claude[-1])
        recorded = self.recorded_prompts("session-123")
        self.assertEqual(recorded, (existing_prompt, claude[-1]))
        self.assertIs(
            conversation_pause.prompt_source(
                claude[-1], frozenset, lambda: recorded
            ),
            conversation_pause.PromptSource.SCHEDULED,
        )

        launches = self.events("systemd-run")
        self.assertEqual(len(launches), 1)
        systemd = cast(list[str], launches[0]["args"])
        scope = next(argument for argument in systemd if argument.startswith("--unit="))
        name, epoch = scope.removeprefix("--unit=").rsplit("-", 1)
        self.assertEqual(name, "restore----")
        self.assertGreater(int(epoch), 0)

    def test_restore_refuses_launch_when_prompt_cannot_be_recorded(self) -> None:
        self.commit_prepared_row(self.unit_row())
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")
        _ = self.pause_state.write_text("not a directory", encoding="utf-8")

        result = self.cli(
            "alpha",
            "--plan", "docs/plans/given.md",
            "--resume", "session-123",
            "--cwd", str(prior),
            "--restart-note", str(note),
            "--session-name", "recorded-name",
            "--host-session", "recorded-tmux",
         "--config-dir", str(Path(self.env["HOME"]) / ".claude"))

        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("add_unit: restart prompt not recorded:", result.stderr)
        self.assertEqual(self.events("systemd-run"), [])

    def test_new_work_records_no_scheduled_prompt(self) -> None:
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")

        self.assertFalse(self.pause_state.exists())

    def test_brief_row_cannot_be_retried_as_plan_on_its_stub(self) -> None:
        _ = self.successful("alpha", "--brief", "Write a full plan")
        original = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        launches = len(self.events("systemd-run"))
        result = self.cli("alpha", "--plan", "docs/plans/build-followups-alpha.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(self.doc.read_text(), original)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(len(self.events("systemd-run")), launches)

    def test_kebab_alias_refuses_existing_branch_and_worktree_row(self) -> None:
        _ = self.successful("mul_add", "--plan", "docs/plans/given.md")
        original = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("mul-add", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(self.doc.read_text(), original)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertNotIn("| mul-add-unit |", self.doc.read_text())

    def test_existing_local_or_origin_branch_refuses_before_any_write(self) -> None:
        for location in ("local", "origin"):
            with self.subTest(location=location):
                branch = "build-followups-alpha"
                if location == "local":
                    _ = self.git("branch", branch)
                else:
                    _ = self.git("branch", "-D", branch)
                    _ = self.git("push", "origin", f"HEAD:refs/heads/{branch}")
                original = self.doc.read_text()
                head = self.git("rev-parse", "HEAD")
                result = self.cli("alpha", "--brief", "Write a plan")
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(self.doc.read_text(), original)
                self.assertEqual(self.git("rev-parse", "HEAD"), head)
                self.assertFalse((self.checkout / "docs/plans/build-followups-alpha.md").exists())

    def test_occupied_worktree_refuses_before_row_commit_or_stub(self) -> None:
        occupied = self.root / "project-alpha"
        occupied.mkdir()
        _ = (occupied / "marker").write_text("keep", encoding="utf-8")
        original = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--brief", "Write a plan")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(self.doc.read_text(), original)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual((occupied / "marker").read_text(), "keep")
        self.assertFalse((self.checkout / "docs/plans/build-followups-alpha.md").exists())

    def test_unrelated_repository_on_expected_branch_refuses_before_commit(self) -> None:
        occupied = self.root / "project-alpha"
        occupied.mkdir()
        _ = self.git("init", "-b", "build-followups-alpha", cwd=occupied)
        _ = self.doc.write_text(self.doc.read_text().replace(
            "## Gates", self.unit_row() + "\n\n## Gates"), encoding="utf-8")
        original = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(self.doc.read_text(), original)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_the_name_check_is_exact_and_the_pane_is_read_by_its_id(self) -> None:
        _ = (self.state / "live-alpha2").touch()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(len(self.events("systemd-run")), 1)
        has_targets = [cast(list[str], event["args"])[-1] for event in self.events("tmux")
                       if cast(list[str], event["args"])[:1] == ["has-session"]]
        pane_targets = [cast(list[str], event["args"])[-1] for event in self.events("tmux")
                        if cast(list[str], event["args"])[:1] == ["capture-pane"]]
        self.assertTrue(has_targets)
        self.assertTrue(pane_targets)
        self.assertEqual(set(has_targets), {"=alpha"})
        self.assertEqual(set(pane_targets), {"%1"})

    def test_remote_control_polling_reads_each_host_through_its_place(self) -> None:
        request = add_unit.UnitLaunch(
            production=add_unit.read_production(self.doc), identity=add_unit.UnitIdentity("alpha-unit", "alpha"),
            branch="build-followups-alpha", worktree=self.root / "project-alpha", plan=add_unit.Standby(),
            port=add_unit.OmittedCell(), owns=add_unit.OmittedCell(), session=add_unit.NewSession(),
            launch_kind=add_unit.NewWorkLaunch(), timeout=0,
        )
        for place in (unit_lookup.TmuxPlace("%1"), HerdrPlace("native", "w1:p1")):
            with self.subTest(place=place):
                marked = unit_lookup.MarkedUnit("alpha-unit", "label", unit_lookup.ClaudeNotRunning(), place)
                with patch.object(add_unit, "launched_unit", return_value=add_unit.MarkedUnitFound(marked)), \
                        patch.object(session_pane, "read_place", return_value="/remote-control is active") as read:
                    self.assertEqual(add_unit.wait_for_remote_control(request, "unused-tmux"), marked)
                read.assert_called_once_with(place)
                with patch.object(add_unit, "launched_unit", return_value=add_unit.MarkedUnitFound(marked)), \
                        patch.object(session_pane, "read_place", side_effect=session_pane.TmuxFailure("unreadable")):
                    with self.assertRaisesRegex(RuntimeError, "did not become active\\nunreadable"):
                        _ = add_unit.wait_for_remote_control(request, "unused-tmux")

    def test_underscore_name_and_checkout_without_trunk_suffix(self) -> None:
        renamed = self.root / "project"
        _ = self.checkout.rename(renamed)
        self.checkout = renamed
        self.doc = renamed / "docs/plans/build-followups-production.md"
        self.plan = renamed / "docs/plans/given.md"
        _ = (Path(self.env["HOME"]) / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "owner@example.com"},
            "projects": self.trusted_projects()}), encoding="utf-8")
        _ = self.doc.write_text(self.production_doc(), encoding="utf-8")
        _ = self.git("add", "docs/plans/build-followups-production.md")
        _ = self.git("commit", "-m", "move checkout")
        _ = self.git("push", "origin", "build-followups")
        _ = self.successful("mul_add", "--plan", "docs/plans/given.md")
        self.assertIn(self.unit_row("mul_add", branch="build-followups-mul-add",
                                    worktree=self.root / "project-mul-add"), self.doc.read_text())
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD",
                                  cwd=self.root / "project-mul-add"), "build-followups-mul-add")


class PythonExecutionEnvironment(TypedDict):
    root: str
    extraPaths: list[str]


class PythonImportConfiguration(TypedDict):
    executionEnvironments: list[PythonExecutionEnvironment]


@final
class UnitLaunchProtocolTests(ShutdownTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        home = self.enterContext(tempfile.TemporaryDirectory())
        home_context = cast(object, self.enterContext(
            patch.dict(os.environ, {"HOME": home})
        ))
        del home_context

    def resume_instructions(self) -> str:
        command = SCRIPT.parents[2] / "commands/showrunner/produce.md"
        return command.read_text(encoding="utf-8").split("**Resume.**", 1)[1].split(
            "</LaunchUnits>", 1
        )[0]

    def test_manual_unit_resume_uses_add_unit_without_selecting_the_current_unit_role(self) -> None:
        instructions = self.resume_instructions()
        self.assertIn("add_unit.py", instructions)
        self.assertIn("--resume <session-id>", instructions)
        self.assertNotIn("env unit", instructions)
        self.assertNotIn("claude --resume", instructions)

    def test_manual_unit_resume_uses_configured_units_account_and_names_missing_configuration_command(self) -> None:
        instructions = self.resume_instructions()
        self.assertIn("starts it on the configured units account", instructions)
        self.assertIn("units account is not configured", instructions)
        self.assertIn("accounts.py move units", instructions)

    def test_production_import_paths_resolve_the_accounts_package(self) -> None:
        """F005: production's configured import search can find scripts.accounts."""
        root = SCRIPT.parents[2]
        configuration = cast(
            PythonImportConfiguration,
            cast(object, json.loads((root / "pyrightconfig.json").read_text(encoding="utf-8"))),
        )
        environment = next(
            candidate for candidate in configuration["executionEnvironments"]
            if candidate["root"] == "scripts/production"
        )

        self.assertTrue(any(
            (root / search_path / "scripts/accounts/accounts.py").is_file()
            for search_path in [environment["root"], *environment["extraPaths"]]
        ))


class RetiredRowTests(ShutdownTestCase):
    """A unit is retired by its Plan cell, or when its worktree and its tmux session are both gone."""

    root: Path = Path()
    linked: Path = Path()
    main: Path = Path()
    missing: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        home_context = cast(object, self.enterContext(
            patch.dict(os.environ, {"HOME": str(self.root / "home")})
        ))
        del home_context
        self.linked = self.root / "linked"
        self.linked.mkdir()
        _ = (self.linked / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        self.main = self.root / "main"
        (self.main / ".git").mkdir(parents=True)
        self.missing = self.root / "missing"

    def row(self, unit: str, worktree: Path, plan: str = "`docs/plan.md`") -> str:
        return f"| `{unit}-unit` | {plan} | `{worktree}` | `branch` | `{unit}` (resumed elsewhere) | — | — |"

    def session(self, unit: str) -> unit_lookup.MarkedUnit:
        return unit_lookup.MarkedUnit(unit, "any-label", unit_lookup.ClaudeNotRunning(), unit_lookup.TmuxPlace("%1"))

    def retired(self, worktree: Path, *, gone: bool, plan: str = "`docs/plan.md`") -> tuple[bool, int]:
        """Whether the row is retired, and how many times tmux was asked."""
        found = {} if gone else {"some-unit": self.session("some-unit")}
        cells = add_unit.unit_table([*OLD_HEADER, self.row("some", worktree, plan)])[0]
        with patch.object(unit_lookup, "marked_units", return_value=found) as asked:
            return add_unit.unit_is_retired(cells, add_unit.SessionMarks("show")), asked.call_count

    def test_a_unit_is_gone_only_without_both_its_worktree_and_its_session(self) -> None:
        self.assertEqual(self.retired(self.missing, gone=True), (True, 1))
        self.assertEqual(self.retired(self.missing, gone=False), (False, 1))
        # A session that is only restarting keeps its row: its worktree is there, and tmux is not asked.
        self.assertEqual(self.retired(self.linked, gone=True), (False, 0))
        # A main checkout is shared, so it holds no unit once the session is gone.
        self.assertEqual(self.retired(self.main, gone=True), (True, 1))
        self.assertEqual(self.retired(self.main, gone=False), (False, 1))
        self.assertEqual(self.retired(self.linked, gone=False, plan="retired after completion"), (True, 0))

    def test_tmux_must_say_the_session_is_gone_and_is_asked_once_for_a_whole_table(self) -> None:
        lines = [*OLD_HEADER, self.row("first", self.missing), self.row("second", self.missing)]
        with patch.object(unit_lookup, "marked_units", side_effect=OSError("tmux could not list panes")) as asked:
            self.assertEqual(retired_units(lines, "show"), set())
        self.assertEqual(asked.call_count, 1)
        with patch.object(unit_lookup, "marked_units", return_value={"second-unit": self.session("second-unit")}) as asked:
            self.assertEqual(retired_units(lines, "show"), {"first-unit"})
        self.assertEqual(asked.call_count, 1)

    def test_every_list_drops_a_unit_that_is_gone(self) -> None:
        lines = [
            *OLD_HEADER,
            self.row("marked", self.linked, "retired after completion"),
            self.row("gone", self.missing),
            self.row("parked", self.linked),
        ]
        with patch.object(unit_lookup, "marked_units", return_value={}):
            self.assertEqual(retired_units(lines, "show"), {"marked-unit", "gone-unit"})
            self.assertEqual([cell_value(cells["Unit"]) for cells in live_unit_table(lines, "show")], ["parked-unit"])


class AdoptAccountTests(unittest.TestCase):
    """An adopted session keeps the account it runs on, named by its configured label."""

    def adopted(self, folder: Path, named: list[accounts.NamedAccount]) -> add_unit.Adoptable | add_unit.AlreadyAdopted:
        claude = unit_lookup.LiveClaude("nightly-review", "sid", "/sock", 4242)
        with patch.object(add_unit, "launched_unit", return_value=None), \
             patch.object(unit_lookup, "every_marked_unit", return_value=unit_lookup.MarkedProductions({}, {})), \
             patch.object(unit_lookup, "tmux_claude", return_value=claude), \
             patch("scripts.whoami.account.process_config_dir", return_value=folder), \
             patch.object(accounts, "main_config_dir", return_value=Path("/home/someone/.claude")), \
             patch.object(accounts, "checked_named_accounts", return_value=named):
            return add_unit.adoptable(cast(add_unit.RequestedUnitLaunch, cast(object, None)), "nightly-review")

    def test_a_session_on_a_named_account_is_adopted_on_that_account(self) -> None:
        three = accounts.NamedAccount("claude 3", Path("/home/someone/.claude3"))
        adopted = self.adopted(three.dir, [accounts.NamedAccount("claude 1", Path("/home/someone/.claude1")), three])
        self.assertEqual(cast(add_unit.Adoptable, adopted).account, three)

    def test_a_session_on_the_main_folder_is_adopted_on_the_default_account(self) -> None:
        adopted = self.adopted(Path("/home/someone/.claude"), [])
        self.assertEqual(cast(add_unit.Adoptable, adopted).account, accounts.DefaultAccount())

    def test_a_session_on_no_fit_account_is_refused(self) -> None:
        with self.assertRaises(add_unit.Refusal) as refused:
            _ = self.adopted(Path("/home/someone/.claude9"), [])
        self.assertIn("runs on /home/someone/.claude9, which is no account fit to launch", str(refused.exception))


if __name__ == "__main__":
    _ = unittest.main()
