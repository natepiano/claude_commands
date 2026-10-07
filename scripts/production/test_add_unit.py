"""Exercise add_unit through its command line with isolated git and launcher state."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, final, override

from add_unit import live_unit_rows, plan_cell_is_retired, retired_sessions, retired_units


SCRIPT = Path(__file__).with_name("add_unit.py")
STUB = r'''#!/usr/bin/env python3
import json
import os
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
    if args[:1] == ["has-session"]:
        target = args[args.index("-t") + 1]
        raise SystemExit(0 if target.startswith("=") and (state / f"live-{target[1:]}").exists() else 1)
    if args[:1] == ["capture-pane"]:
        target = args[args.index("-t") + 1]
        if not target.startswith("=") or not (state / f"live-{target[1:-1]}").exists():
            raise SystemExit(1)
        print("/remote-control is active" if (state / "ready").exists() else "Waiting for remote control")
    if args[:1] == ["new-session"]:
        (state / f"live-{args[args.index('-s') + 1]}").touch()
elif name == "systemd-run":
    raise SystemExit(subprocess.run(args[3:], check=False).returncode)
elif name == "nix":
    print(os.environ["STUB_TMUX"])
'''


@final
class AddUnitTests(unittest.TestCase):
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
        self.env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.checkout = self.root / "project-trunk"
        self.checkout.mkdir()
        self.origin = self.root / "origin.git"
        _ = self.git("init", "--bare", str(self.origin), cwd=self.root)
        _ = self.git("init", "-b", "build-followups")
        _ = self.git("config", "user.name", "Unit Test")
        _ = self.git("config", "user.email", "unit@example.invalid")
        _ = self.git("remote", "add", "origin", str(self.origin))
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
        codex_config = self.root / "codex.toml"
        codex_cache = self.root / "models.json"
        sync_state = self.root / "catalog-sync-success"
        self.write_agent_config()
        _ = sync_state.touch()
        self.env = {**os.environ, "HOME": str(self.root / "home"),
                    "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                    "STUB_STATE": str(self.state), "STUB_TMUX": str(self.bin / "tmux"),
                    "SHOWRUNNERS_CONFIG": str(self.config), "CLAUDE_TEST_SECRET": "must-not-leak",
                    "AGENTS_CONFIG_FILE": str(self.agent_config),
                    "CODEX_CONFIG_FILE": str(codex_config),
                    "CODEX_MODELS_CACHE_FILE": str(codex_cache),
                    "CODEX_CATALOG_SYNC_STATE_FILE": str(sync_state)}

    def write_agent_config(self, *, family: str = "claude", director: str = "opus:xhigh",
                           claude_set: bool = True) -> None:
        claude_section = f"[production.claude]\ndirector={director}\n\n" if claude_set else ""
        codex_section = "[production.codex]\ndirector=gpt-test:high\n\n" if family == "codex" else ""
        content = (f"[assignments]\nproduction={family}\n\n"
                   f"{claude_section}{codex_section}"
                   "[codex.agents]\ngpt-test=low,medium,high\n\n"
                   "[claude.agents]\nopus=low,medium,high,xhigh\nsonnet=low,medium,high,xhigh\n")
        _ = self.agent_config.write_text(content, encoding="utf-8")

    def production_doc(self) -> str:
        return ("# Production\n\n"
                "- **Merge branch:** `build-followups` — unit checkpoints merge here\n"
                f"- **Showrunner checkout:** `{self.checkout}`\n"
                "- **Showrunner session:** director\n"
                "- **Log:** `docs/plans/build-followups-log.md` — git-excluded\n"
                "- **User zone:** America/Los_Angeles — reports use PDT\n\n"
                "## Units\n\n"
                "| Unit | Plan | Worktree | Branch | Session | Port | Owns |\n"
                "| --- | --- | --- | --- | --- | --- | --- |\n\n"
                "## Gates\n")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", *args], cwd=cwd or self.checkout,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, (args, result.stdout, result.stderr))
        return result.stdout.strip()

    def cli(self, *args: str, timeout: float = 15) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), "--production", str(self.doc), *args],
                              cwd=self.checkout, env=self.env, capture_output=True, text=True,
                              check=False, timeout=timeout)

    def successful(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = self.cli(*args)
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

    def assert_director_flags(self, model: str, effort: str | None) -> None:
        command = self.launch_command()
        flags = f"claude --model {model}"
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
        self.assertEqual(retired_units(lines), {"stalls-unit"})
        self.assertEqual(retired_sessions(lines), {"hook"})
        self.assertEqual(live_unit_rows(lines), lines[-2:])

    def test_retired_sessions_read_backticked_session_with_commentary(self) -> None:
        lines = [
            "## Units",
            "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            "| old-unit | retired after completion | /tmp/old | old | `old-session` (resumed elsewhere) | — | — |",
        ]
        self.assertEqual(retired_sessions(lines), {"old-session"})

    def test_retired_units_include_retired_name_and_exclude_live_name(self) -> None:
        lines = [
            "## Units",
            "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            "| `old-unit` | retired after completion | /tmp/old | old | old | — | — |",
            "| `live-unit` | docs/live.md | /tmp/live | live | live | — | — |",
        ]
        self.assertEqual(retired_units(lines), {"old-unit"})

    def unit_row(self, name: str = "alpha", *, branch: str = "build-followups-alpha",
                 worktree: Path | None = None, plan: str = "docs/plans/given.md",
                 port: str = "—", owns: str = "—") -> str:
        target = worktree or self.root / "project-alpha"
        return f"| {name}-unit | {plan} | {target} | {branch} | {name} | {port} | {owns} |"

    def prepared_row(self, *, name: str = "alpha", plan: str = "docs/plans/given.md",
                     worktree: Path | None = None, branch: str = "build-followups-alpha",
                     session: str = "alpha") -> str:
        target = worktree or self.root / "project-alpha"
        return (f"| `{name}-unit` | `{plan}` (ready to launch) | `{target}` | "
                f"`{branch}` | `{session}` | 8123 | `src/alpha` — assigned files |")

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
        self.assertEqual(self.events("tmux"), [])
        self.assertEqual(self.events("systemd-run"), [])

    def registry_units(self) -> list[str]:
        data = cast(dict[str, object], json.loads(self.config.read_text()))
        runners = cast(list[dict[str, object]], data["showrunners"])
        runner = next(item for item in runners if item["session"] == "director")
        self.assertEqual(runner["zone"], "America/Los_Angeles")
        return cast(list[str], runner["units"])

    def test_standby_launch_records_state_without_writing_a_plan(self) -> None:
        _ = self.successful("alpha", "--standby", "--port", "8123", "--owns", "src/alpha")
        worktree = self.root / "project-alpha"
        self.assertIn(self.unit_row(plan="standby", port="8123", owns="src/alpha"),
                      self.doc.read_text())
        self.assertEqual(self.git("log", "-1", "--format=%s"),
                         "production(build-followups): add unit alpha-unit (standby)")
        self.assertEqual(self.git("show", "--pretty=format:", "--name-only", "HEAD"),
                         "docs/plans/build-followups-production.md")
        self.assertFalse((self.checkout / "docs/plans/build-followups-alpha.md").exists())
        self.assertFalse((worktree / "docs/plans/build-followups-alpha.md").exists())
        self.assertEqual(self.registry_units(), ["alpha"])
        data = cast(dict[str, object], json.loads(self.config.read_text()))
        runner = cast(list[dict[str, object]], data["showrunners"])[0]
        self.assertEqual(runner["standby"], ["alpha"])
        tmux = next(record for record in self.events("tmux")
                    if cast(list[str], record["args"])[:1] == ["new-session"])
        command = cast(list[str], tmux["args"])[-1]
        prompt = (f"You are alpha-unit in production build-followups (doc {self.doc}), "
                  f"under the showrunner director, on standby. Work only in your worktree {worktree}, "
                  "branch build-followups-alpha. Do nothing until the showrunner sends you work.")
        self.assertIn(prompt, command)
        self.assert_director_flags("opus", "xhigh")
        self.assertRegex(self.log.read_text(),
                         r"^- \d\d:\d\d PDT: added alpha-unit \(standby\), tmux alpha, worktree ")

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

    def test_plan_adds_row_commits_pushes_worktree_launches_and_records(self) -> None:
        result = self.successful("alpha", "--plan", "docs/plans/given.md", "--port", "8123",
                                 "--owns", "src/alpha")
        worktree = self.root / "project-alpha"
        row = self.unit_row(port="8123", owns="src/alpha")
        self.assertIn(row, self.doc.read_text())
        self.assertEqual(self.git("show", "--pretty=format:", "--name-only", "HEAD"),
                         "docs/plans/build-followups-production.md")
        self.assertIn("production(build-followups): add unit alpha-unit (plan)",
                      self.git("log", "-1", "--format=%s"))
        for branch in ("build-followups", "build-followups-alpha"):
            _ = self.git("--git-dir", str(self.origin), "rev-parse", f"refs/heads/{branch}")
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree),
                         "build-followups-alpha")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=worktree),
                         self.git("rev-parse", "build-followups"))
        launch = self.events("systemd-run")
        self.assertEqual(len(launch), 1)
        self.assertEqual(cast(list[str], launch[0]["args"])[:3],
                         ["--user", "--scope", "--unit=alpha"])
        self.assertEqual(launch[0]["claude_env"], [])
        tmux = next(record for record in self.events("tmux")
                    if cast(list[str], record["args"])[:1] == ["new-session"])
        args = cast(list[str], tmux["args"])
        self.assertEqual(args[:9], ["new-session", "-d", "-s", "alpha", "-c", str(worktree),
                                    "-e", "SHOWRUNNER_UNIT=build-followups", "zsh"])
        self.assertIn("claude --model opus --effort xhigh --remote-control alpha -n alpha", args[-1])
        self.assert_director_flags("opus", "xhigh")
        self.assertIn("'/unit:delegate docs/plans/given.md'", args[-1])
        self.assertEqual(self.registry_units(), ["alpha"])
        self.assertRegex(self.log.read_text(),
                         r"^- \d\d:\d\d PDT: added alpha-unit \(plan\), tmux alpha, worktree ")
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertIn("alpha-unit started: tmux attach -t alpha", result.stdout)

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
        self.assertEqual(self.registry_units(), ["alpha"])
        self.assertIn("added alpha-unit (plan)", self.log.read_text())

    def test_prepared_row_refuses_mismatched_identity_cells_before_launch(self) -> None:
        cases = (("Plan", self.prepared_row(plan="docs/plans/other.md"),
                  "docs/plans/other.md", "docs/plans/given.md"),
                 ("Branch", self.prepared_row(branch="other-branch"),
                  "other-branch", "build-followups-alpha"),
                 ("Worktree", self.prepared_row(worktree=self.root / "other-worktree"),
                  str(self.root / "other-worktree"), str(self.root / "project-alpha")),
                 ("Session", self.prepared_row(session="other-session"),
                  "other-session", "alpha"))
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
                          "--cwd", str(self.root / "prior-session"), "--check")
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assert_no_launch_change(document, head)

    def test_check_refuses_invalid_session_name_without_any_change(self) -> None:
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("new director", "--plan", "docs/plans/given.md", "--resume", "session-123",
                          "--cwd", str(self.root / "prior-session"), "--check")
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
        self.assertIn("wait for its approval before you run /unit:delegate docs/plans/build-followups-alpha.md", prompt)
        self.assertIn("alpha-unit started: tmux attach -t alpha", result.stdout)
        self.assert_director_flags("opus", "xhigh")

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
        self.assertEqual(self.registry_units(), ["alpha"])
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
        _ = self.doc.write_text(original.replace("- **User zone:** America/Los_Angeles — reports use PDT\n", ""))
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertNotIn("| alpha-unit |", self.doc.read_text())

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
        self.assertEqual(self.doc.read_text(), original)
        self.assertEqual(len(self.events("systemd-run")), 0)

    def test_resume_uses_session_id_cwd_and_promote_prompt(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123",
                            "--cwd", str(prior))
        tmux = next(record for record in self.events("tmux")
                    if cast(list[str], record["args"])[:1] == ["new-session"])
        args = cast(list[str], tmux["args"])
        self.assertEqual(args[args.index("-c") + 1], str(prior))
        command = args[-1]
        self.assertIn("claude --model opus --effort xhigh --resume session-123 --remote-control alpha -n alpha", command)
        self.assert_director_flags("opus", "xhigh")
        self.assertIn("You are now alpha-unit in production build-followups", command)
        promoted_plan = self.root / "project-alpha/docs/plans/given.md"
        self.assertTrue(promoted_plan.exists())
        self.assertIn(f"Run /unit:delegate {promoted_plan}", command)

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

    def test_tmux_targets_are_exact_when_only_prefix_session_is_live(self) -> None:
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
        self.assertEqual(set(pane_targets), {"=alpha:"})

    def test_underscore_name_and_checkout_without_trunk_suffix(self) -> None:
        renamed = self.root / "project"
        _ = self.checkout.rename(renamed)
        self.checkout = renamed
        self.doc = renamed / "docs/plans/build-followups-production.md"
        self.plan = renamed / "docs/plans/given.md"
        _ = self.doc.write_text(self.production_doc(), encoding="utf-8")
        _ = self.git("add", "docs/plans/build-followups-production.md")
        _ = self.git("commit", "-m", "move checkout")
        _ = self.git("push", "origin", "build-followups")
        _ = self.successful("mul_add", "--plan", "docs/plans/given.md")
        self.assertIn(self.unit_row("mul_add", branch="build-followups-mul-add",
                                    worktree=self.root / "project-mul-add"), self.doc.read_text())
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD",
                                  cwd=self.root / "project-mul-add"), "build-followups-mul-add")

    def test_old_prompt_gains_unit_and_showrunner_form_stays_intact(self) -> None:
        _ = self.prompt.parent.mkdir(parents=True)
        old = "Run `zsh ~/.claude/scripts/production/unit_status.sh /tmp/scratch America/Los_Angeles existing | cut -c1-400`.\n"
        _ = self.prompt.write_text(old, encoding="utf-8")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertIn("America/Los_Angeles existing alpha |", self.prompt.read_text())
        _ = self.prompt.write_text("Run `unit_status.sh --showrunner director`.\n", encoding="utf-8")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.prompt.read_text(), "Run `unit_status.sh --showrunner director`.\n")


if __name__ == "__main__":
    _ = unittest.main()
