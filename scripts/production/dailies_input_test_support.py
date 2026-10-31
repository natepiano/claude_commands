"""Build dailies input from disposable production state and status output."""

from __future__ import annotations

import json
import runpy
import shutil
from collections.abc import Sequence
from types import ModuleType
import dailies_input
import fake_unit_host
import fake_herdr
import dailies_render
from cli_fixture import ImmutableGitSeed, invoke_cli, production_paths
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar, IO, cast, override
from unittest import mock

from dailies_input import (Activity, Block, ClaudeNotRunning, Decision, FormWaiting, Running, SessionGone,
                           StillWaiting, TicksFailing, UnitRow, status_blocks)
from unit_lookup import UnitState
from scripts.production import staff, unit_names
from scripts.production.staff_test_support import (
    fixture_showrunner_name, isolated_staff_file, showrunner_launch, showrunner_relationship,
    unit_membership,
)


# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300

SCRIPT = Path(__file__).with_name("dailies_input.py")
RENDERER = SCRIPT.with_name("dailies_render.py")
CI_POINTS = SCRIPT.with_name("ci_points.py")
WAITING = SCRIPT.with_name("waiting.py")
STAMP = "at 10:00 PDT / 17:00 UTC"
AT = "2026-10-06T17:00"
ALPHA = "alpha-unit"
BETA = "beta-unit"
NOTIFIER = '''#!__PYTHON__
import json
import os
from pathlib import Path
import sys

record = Path(os.environ["DAILIES_TEST_EVENTS"])
with record.open("a", encoding="utf-8") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
if Path(os.environ["DAILIES_TEST_FAIL"]).exists():
    raise SystemExit(1)
print(os.environ.get("DAILIES_TEST_NEXT_DUE", "next_due=1791334500 (2026-10-06 17:55 PDT)"))
'''
WATCH_STUB = '''#!__PYTHON__
import json
import os
from pathlib import Path
import sys

events = Path(os.environ["DAILIES_WATCH_EVENTS"])
with events.open("a", encoding="utf-8") as output:
    output.write(json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]]) + "\\n")
if Path(sys.argv[0]).name == "send.py":
    raise SystemExit(0)
if sys.argv[1:2] == ["watch"]:
    print("12 of 12 phases: report ready, waiting for your acknowledgment")
    raise SystemExit(3)
if sys.argv[1:2] == ["report"]:
    print("| phases merged | 12 |")
    raise SystemExit(0)
raise SystemExit(99)
'''
RACE_WATCH_STUB = '''#!__PYTHON__
import os
import subprocess
import sys

if sys.argv[1:2] != ["watch"]:
    raise SystemExit(99)
request = subprocess.run([
    sys.executable,
    os.environ["DAILIES_RACE_WAITING"],
    "eta-request",
    os.environ["DAILIES_RACE_UNIT"],
    "--phase",
    os.environ["DAILIES_RACE_PHASE"],
    "--production",
    os.environ["DAILIES_RACE_PRODUCTION"],
    "--state-dir",
    os.environ["DAILIES_RACE_STATE"],
], capture_output=True, text=True, check=False)
if request.returncode:
    print(request.stdout + request.stderr, file=sys.stderr)
raise SystemExit(request.returncode)
print("review threshold not reached")
'''
PHASE_TABLE_STUB = '''#!__PYTHON__
import json
import os
from pathlib import Path
import sys

events = Path(os.environ["DAILIES_PHASE_TABLE_EVENTS"])
with events.open("a", encoding="utf-8") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
command = sys.argv[1] if len(sys.argv) > 1 else ""
failure = os.environ.get("DAILIES_PHASE_TABLE_FAIL", "")
if command == failure:
    print(f"{command} could not read records", file=sys.stderr)
    raise SystemExit(1)
if command == "show":
    print(Path(os.environ["DAILIES_PHASE_TABLE_JSON"]).read_text(encoding="utf-8"))
    raise SystemExit(0)
raise SystemExit(2)
'''
BEVY_UPGRADE_STUB = '''
import json
import os
from pathlib import Path
import sys

Path(os.environ["DAILIES_BEVY_EVENTS"]).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
print(os.environ.get("DAILIES_BEVY_STDERR", ""), file=sys.stderr, flush=True)
print(os.environ.get("DAILIES_BEVY_OUTPUT", ""))
raise SystemExit(int(os.environ.get("DAILIES_BEVY_EXIT", "0")))
'''


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


class DailiesInputFixture(unittest.TestCase):
    seed: ClassVar[ImmutableGitSeed]

    fixture_root: ClassVar[Path]
    fixture_snapshot: ClassVar[Path]
    fixture_attributes: ClassVar[dict[str, Path]]
    fixture_env: ClassVar[dict[str, str]]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        folder = cls.enterClassContext(tempfile.TemporaryDirectory(prefix="dailies-input-test-"))
        directory = Path(folder).resolve()
        cls.seed = ImmutableGitSeed.create(directory / "git-seed", 'production', 'Dailies Test',
                                           'dailies@example.invalid', {'README.md': 'base\n'})
        cls.fixture_root = directory / "case"
        cls.fixture_snapshot = directory / "snapshot"
        builder = cls()
        builder.root = cls.fixture_root
        builder.root.mkdir()
        try:
            builder.build_fixture()
        finally:
            builder.doCleanups()
        cls.fixture_attributes = {name: cast(Path, getattr(builder, name)) for name in (
            "root", "home", "origin", "checkout", "doc", "status", "judgment", "state", "holders",
            "output", "notifier", "events", "notifier_failure", "phase_table", "phase_table_json",
            "phase_table_events", "showrunner_state",
        )}
        cls.fixture_env = builder.env.copy()
        _ = shutil.copytree(cls.fixture_root, cls.fixture_snapshot, symlinks=True)
        shutil.rmtree(cls.fixture_root)

    root: Path = Path()
    home: Path = Path()
    origin: Path = Path()
    checkout: Path = Path()
    doc: Path = Path()
    status: Path = Path()
    judgment: Path = Path()
    state: Path = Path()
    holders: Path = Path()
    output: Path = Path()
    notifier: Path = Path()
    events: Path = Path()
    notifier_failure: Path = Path()
    phase_table: Path = Path()
    phase_table_json: Path = Path()
    phase_table_events: Path = Path()
    showrunner_state: Path = Path()
    env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        # Production docs, Git remotes and fake tools embed absolute paths.
        # Copy files and Git metadata privately, preserving those paths per test.
        self.root = self.fixture_root
        _ = shutil.copytree(self.fixture_snapshot, self.root, symlinks=True)
        self.addCleanup(shutil.rmtree, self.root)
        for name, value in self.fixture_attributes.items():
            setattr(self, name, value)
        self.env = self.fixture_env.copy()
        _ = self.enterContext(isolated_staff_file(self.root / "staff/staff.json", self.env))
        _ = self.enterContext(production_paths(self.env))
        self.patch_commands()

    def build_fixture(self) -> None:
        self.home = self.root / "home"
        self.home.mkdir()
        self.origin = self.root / "origin.git"
        self.checkout = self.root / "merge"
        self.seed.copy_to(self.checkout, self.origin)
        self.doc = self.root / "example-production.md"
        self.status = self.root / "status.txt"
        self.judgment = self.root / "judgment.json"
        self.state = self.root / "state"
        self.state.mkdir()
        self.holders = self.root / "holders"
        self.holders.mkdir()
        self.output = self.root / "dailies_input.json"
        self.notifier = self.root / "notifier"
        self.events = self.root / "notifier-events.jsonl"
        self.notifier_failure = self.root / "notifier-fail"
        self.phase_table = self.root / "phase-table.py"
        self.phase_table_json = self.root / "phase-table.json"
        self.phase_table_events = self.root / "phase-table-events.jsonl"
        _ = self.notifier.write_text(NOTIFIER.replace("__PYTHON__", sys.executable), encoding="utf-8")
        self.notifier.chmod(0o755)
        _ = self.phase_table.write_text(PHASE_TABLE_STUB.replace("__PYTHON__", sys.executable), encoding="utf-8")
        self.phase_table.chmod(0o755)
        _ = self.phase_table_json.write_text("{}\n", encoding="utf-8")
        self.showrunner_state = self.root / "showrunner-state"
        upgrade = self.root / "bevy upgrade stub.py"
        _ = upgrade.write_text(BEVY_UPGRADE_STUB, encoding="utf-8")
        self.env = {**os.environ, "HOME": str(self.home), "BUILD_HOLD_DIR": str(self.holders),
                    "DAILIES_BEVY_UPGRADE": str(upgrade),
                    "DAILIES_BEVY_EVENTS": str(self.root / "bevy-events.json"),
                    "DAILIES_BEVY_OUTPUT": "", "DAILIES_BEVY_STDERR": "",
                    "DAILIES_BEVY_EXIT": "0",
                    "MAC_TEST_STATE_DIR": str(self.root / "mac-test"),
                    "DAILIES_TEST_EVENTS": str(self.events), "DAILIES_TEST_FAIL": str(self.notifier_failure),
                    "DAILIES_PHASE_TABLE": str(self.phase_table),
                    "DAILIES_PHASE_TABLE_JSON": str(self.phase_table_json),
                    "DAILIES_PHASE_TABLE_EVENTS": str(self.phase_table_events),
                    "PHASE_TABLE_VAULT": str(self.root / "vault"),
                    "UNIT_HISTORY_DIR": str(self.root / "history"),
                    "NOTIFIER_STATE_DIR": str(self.root / "notifier-state"),
                    "SHOWRUNNER_STATE_DIR": str(self.showrunner_state),
                    "STYLE_LEDGER_STATE_DIR": str(self.root / "style-state"),
                    "STYLE_HISTORY_NATE_STYLE_DIR": str(self.root / "nate_style"),
                    **fake_unit_host.environment(self.root),
                    "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
                    "HERDR_PANE_RECORD_DIR": str(self.root / "pane-records"), "HERDR_SESSION": "agents"}
        _ = self.enterContext(isolated_staff_file(self.root / "staff/staff.json", self.env))
        (self.root / "sessions").mkdir()
        self.production_doc()
        _ = showrunner_relationship(fixture_showrunner_name("example", self.doc))
        self.status_lines(f"== {ALPHA}", "● Checking panel labels", "ETA 23:40")
        self.judgment_file()

    def patch_commands(self) -> None:
        real_run = subprocess.run

        def run(argv: Sequence[str], *, capture_output: bool = False, text: bool = False, check: bool = False,
                timeout: float | None = None, env: dict[str, str] | None = None,
                cwd: str | Path | None = None, input: bytes | str | None = None,
                stdin: int | IO[bytes] | None = None,
                errors: str | None = None, stdout: int | IO[bytes] | None = None,
                stderr: int | IO[bytes] | None = None,
                ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
            environment = dict(os.environ) if env is None else env
            directory = Path.cwd() if cwd is None else Path(cwd)
            if argv[0] == self.env["HERDR_BIN"]:
                self.assertTrue(capture_output and text)
                self.assertFalse(check)
                self.assertIsNotNone(env, "herdr must supply its sanitized child environment")
                return invoke_cli(fake_herdr, lambda: fake_herdr.main(list(argv[1:])),
                                  list(argv[1:]), environment, directory)
            script = Path(argv[1] if argv[0] == sys.executable and len(argv) > 1 else argv[0])
            arguments = list(argv[2:] if argv[0] == sys.executable else argv[1:])
            if script == RENDERER:
                return invoke_cli(dailies_render, lambda: dailies_render.main(arguments),
                                  arguments, environment, directory, production_state=True)
            # Execute the actual fixture scripts; their logs, mutations, status
            # and nested commands remain the evidence consumed by the tests.
            fixtures = {self.notifier, self.phase_table, self.root / "bevy upgrade stub.py",
                        self.root / "bin/review_regime.py", self.root / "race-review.py",
                        self.home / ".claude/scripts/message/send.py"}
            production_commands = {CI_POINTS, WAITING, SCRIPT.with_name("review_regime.py"),
                                   SCRIPT.parent.parent / "direct/phase_table.py",
                                   SCRIPT.parent.parent / "style/style_ledger.py"}
            if script.exists() and (script in fixtures or script in production_commands):
                module = ModuleType("dailies_fixture_command")
                module.__file__ = str(script)

                def main() -> int:
                    with mock.patch.object(sys, "path", [str(script.parent), *sys.path]):
                        _ = runpy.run_path(str(script), run_name="__main__")
                    return 0

                return invoke_cli(module, main, arguments, environment, directory)
            if text:
                return real_run(argv, capture_output=capture_output, text=True, check=check,
                                timeout=timeout, env=env, cwd=cwd, input=cast(str | None, input), errors=errors,
                                stdin=stdin, stdout=stdout, stderr=stderr)
            return real_run(argv, capture_output=capture_output, check=check,
                            timeout=timeout, env=env, cwd=cwd, input=cast(bytes | None, input),
                            stdin=stdin, stdout=stdout, stderr=stderr)

        _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=run))

    def git(self, *arguments: str) -> str:
        result = subprocess.run(["git", "-C", str(self.checkout), *arguments],
                                check=False, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def production_doc(self, *, beta: bool = False) -> None:
        for name in ("alpha", "beta"):
            # A linked worktree, where `.git` is a file: its unit is never taken for a removed one.
            (self.root / name).mkdir(exist_ok=True)
            _ = (self.root / name / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        rows = [
            "# Production — example", "", "## Production Context", "",
            "- **Merge branch:** `production`",
            f"- **Showrunner checkout:** `{self.checkout}`",
            "- **Showrunner session:** test-showrunner",
            "- **Log:** `production.log`",
            "- **User zone:** America/Los_Angeles",
            "- **Updates:** every 15 minutes", "",
            "## Units", "",
            "| Unit | Plan | Worktree | Branch | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- |",
            f"| `{ALPHA}` | `docs/alpha.md` | `{self.root / 'alpha'}` | `alpha` | — | — |",
        ]
        if beta:
            rows.append(f"| `{BETA}` | `docs/beta.md` | `{self.root / 'beta'}` | `beta` | — | — |")
        rows.extend(["", "## Gates", ""])
        _ = self.doc.write_text("\n".join(rows), encoding="utf-8")
        self.timer("example")
        for unit in (ALPHA, BETA) if beta else (ALPHA,):
            _ = unit_membership(fixture_showrunner_name("example", self.doc), unit)

    def status_lines(self, *lines: str) -> None:
        _ = self.status.write_text("\n".join((STAMP, *lines)) + "\n", encoding="utf-8")

    def judgment_file(self, *, beta: bool = False, alpha: dict[str, object] | None = None) -> None:
        base: dict[str, object] = {
            "unit": ALPHA,
            "project": "panels that stay readable",
            "phase": "Phase 2 of 3: panel labels stay clear",
            "started": "2026-10-06T09:00",
            "held": None,
            "update": "checking the panel labels",
            "eta": {"percent": 60},
            "then": ["Phase 3: panel edges align"],
        }
        if alpha:
            base.update(alpha)
        units: list[dict[str, object]] = [base]
        if beta:
            units.append({
                "unit": BETA,
                "project": "buttons that stay readable",
                "phase": "Phase 1 of 2: button labels stay clear",
                "started": "2026-10-06T09:00",
                "held": None,
                "update": "checking the button labels",
                "then": ["Phase 2: button edges align"],
            })
        _ = self.judgment.write_text(json.dumps({"units": units, "topics": []}) + "\n", encoding="utf-8")

    def run_builder(self, *extra: str, include_render_state: bool = True, subprocess_cli: bool = False) -> subprocess.CompletedProcess[str]:
        state_args = (
            ("--render-state", str(self.root / "render-state.json"))
            if include_render_state and "--render-state" not in extra else ()
        )
        arguments = ["--production", str(self.doc),
             "--status", str(self.status), "--judgment", str(self.judgment),
             "--state-dir", str(self.state), "--out", str(self.output),
             "--holders", str(self.holders), "--notifier", str(self.notifier),
             "--at", AT, *state_args, *extra]
        if subprocess_cli:
            return subprocess.run([sys.executable, str(SCRIPT), *arguments], cwd=self.checkout,
                                  env=self.env, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        return invoke_cli(dailies_input, dailies_input.main, arguments, self.env, self.checkout, production_state=True)

    def report(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.output.read_text(encoding="utf-8")))

    def run_renderer(self, at: str) -> subprocess.CompletedProcess[str]:
        arguments = [str(self.output), "--at", at, "--state", str(self.root / "render-state.json"),
                     "--log", str(self.root / "dailies.log")]
        return invoke_cli(dailies_render, lambda: dailies_render.main(arguments), arguments,
                          self.env, self.checkout, production_state=True)

    def unit(self) -> dict[str, object]:
        return cast(dict[str, object], cast(list[object], self.report()["units"])[0])

    def notifier_events(self) -> list[list[str]]:
        if not self.events.exists():
            return []
        return [cast(list[str], json.loads(line)) for line in self.events.read_text(encoding="utf-8").splitlines()]

    def phase_table_calls(self) -> list[list[str]]:
        if not self.phase_table_events.exists():
            return []
        return [cast(list[str], json.loads(line))
                for line in self.phase_table_events.read_text(encoding="utf-8").splitlines()]

    def open_phase_record(
        self,
        *,
        phase: str = "2",
        started: str = "2026-10-06T15:30:00+00:00",
        percent: int | None = 60,
        eta: dict[str, object] | None = None,
        has_eta: bool = True,
        first: str | None = "2026-10-07T00:30:00+00:00",
        repair_rounds: int | None = 0,
        status: str = "running",
    ) -> None:
        current_eta: dict[str, object] | None = eta if eta is not None else {
            "time": "2026-10-07T00:40:00+00:00",
            "earliest": "2026-10-07T00:35:00+00:00",
            "latest": "2026-10-07T00:55:00+00:00",
            "source": "stated",
            "stated_at": "2026-10-06T23:45:00+00:00",
            "basis": "the checks are nearly done",
            "as_of": None,
        }
        if not has_eta:
            current_eta = None
        record: dict[str, object] = {
            "plan": str(self.root / "alpha/docs/alpha.md"),
            "updated": "2026-10-06T23:45:00+00:00",
            "plan_finish": "2026-10-07T01:00:00+00:00",
            "current": {
                "phase": phase,
                "of": 3,
                "title": "panel labels stay clear",
                "started": started,
                "percent": percent,
                "eta": current_eta,
                "first_stated_eta_target": first,
                "repair_rounds_since_first_stated_eta": (
                    repair_rounds if first is not None else None
                ),
            },
            "phases": [{
                "phase": phase,
                "title": "panel labels stay clear",
                "status": status,
                "start": started,
                "finish": None,
                "seconds": None,
            }],
        }
        _ = self.phase_table_json.write_text(json.dumps({ALPHA: record}) + "\n", encoding="utf-8")

    def watch_stubs(self) -> Path:
        bin_path = self.root / "bin"
        bin_path.mkdir()
        events = self.root / "watch-events.jsonl"
        for name in ("review_regime.py",):
            stub = bin_path / name
            _ = stub.write_text(WATCH_STUB.replace("__PYTHON__", sys.executable), encoding="utf-8")
            stub.chmod(0o755)
        send = self.home / ".claude/scripts/message/send.py"
        send.parent.mkdir(parents=True, exist_ok=True)
        _ = send.write_text(WATCH_STUB.replace("__PYTHON__", sys.executable), encoding="utf-8")
        send.chmod(0o755)
        self.env["DAILIES_WATCH_EVENTS"] = str(events)
        self.env["DAILIES_REVIEW_REGIME"] = str(bin_path / "review_regime.py")
        self.env["CI_POINTS_REVIEW_REGIME"] = str(bin_path / "review_regime.py")
        self.env["PATH"] = str(bin_path) + os.pathsep + os.environ.get("PATH", "")
        return events

    def watch_events(self, events: Path) -> list[list[str]]:
        return ([cast(list[str], json.loads(line)) for line in events.read_text().splitlines()]
                if events.exists() else [])

    def run_merge_watch(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(CI_POINTS), "watch", "--production", str(self.doc),
                               "--state-dir", str(self.state)], cwd=self.checkout, env=self.env,
                              capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

    def run_eta_request(self, phase: str = "Phase 2 of 3: panel labels stay clear") -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(WAITING), "eta-request", ALPHA,
                               "--phase", phase, "--production", str(self.doc),
                               "--state-dir", str(self.state)], cwd=self.checkout, env=self.env,
                              capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

    def rename_unit(self, name: str) -> None:
        row = unit_membership(fixture_showrunner_name("example", self.doc), ALPHA)
        _ = staff.rename(row.id, unit_names.name(name))
        for path in (self.doc, self.status, self.judgment):
            _ = path.write_text(path.read_text(encoding="utf-8").replace(ALPHA, name), encoding="utf-8")

    def parsed_block(self) -> tuple[Running | SessionGone | ClaudeNotRunning,
                                    tuple[FormWaiting | Decision | StillWaiting | Block | TicksFailing, ...],
                                    tuple[Activity, ...]]:
        block = status_blocks(self.status, (UnitRow(ALPHA, ALPHA, UnitState.RUNNING),))[0]
        return block.state, block.flags, block.activity

    def assert_refused_without_output(self, *extra: str, mention: str) -> str:
        _ = self.output.write_text("sentinel\n", encoding="utf-8")
        result = self.run_builder(*extra)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(mention, result.stdout + result.stderr)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "sentinel\n")
        return result.stdout + result.stderr

    def finished_run(self, worktree: Path) -> None:
        """Record a /unit:direct run in `worktree` that has finished, as the recorder would."""
        runs = self.root / "history/runs"
        runs.mkdir(parents=True, exist_ok=True)
        events: list[dict[str, object]] = [
            {"event_type": "run_started", "working_dir": str(worktree.resolve()), "run_started_at": 1.0},
            {"event_type": "run_finished"}]
        _ = (runs / f"{worktree.name}.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        self.env["UNIT_HISTORY_DIR"] = str(self.root / "history")

    def assert_bevy_unavailable(self, result: subprocess.CompletedProcess[str], reason: str) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        update = f"the record could not be read: {reason}"
        self.assertIn(f"bevy upgrade: unavailable — {update}\n", result.stdout)
        self.assertNotIn("bevy upgrade: failed", result.stdout)
        self.assertIn("input: ok", result.stdout)
        topics = cast(list[dict[str, object]], self.report()["topics"])
        self.assertEqual([item for item in topics if item["title"] == "Bevy upgrade"], [
            {"title": "Bevy upgrade", "update": update, "eta": "no ETA stated yet", "needs_user": False}])

    def timer(self, slug: str) -> None:
        """A production with a complete update timer, so the tree counts it."""
        instance = self.root / "notifier-state" / f"showrunner-{slug}"
        instance.mkdir(parents=True, exist_ok=True)
        _ = (instance / "conf").write_text(
            f"TARGET=session:{slug}-session\nCHECK=zsh /x/production_check.sh {self.doc}\n", encoding="utf-8")
        row = showrunner_relationship(fixture_showrunner_name(slug, self.doc))
        showrunner_launch(row, f"{slug}-session", self.env)

    def attach(self, child: str, parent: str) -> None:
        for slug in (child, parent):
            if not (self.root / "notifier-state" / f"showrunner-{slug}").exists():
                self.timer(slug)
        _ = showrunner_relationship(fixture_showrunner_name(child, self.doc), fixture_showrunner_name(parent, self.doc))

    def saved_report(self, slug: str) -> Path:
        return self.showrunner_state / slug / "dailies_last.json"

    def write_saved_report(self, slug: str, **changed: object) -> None:
        fields: dict[str, object] = {
            "written": "2026-10-06T16:50", "zone": "America/Los_Angeles", "interval": 15, "units": 3, "held": 1,
            "rows": [{"name": "fps", "started": "2026-10-06T15:10", "eta": "2026-10-06T19:20",
                      "earliest": "2026-10-06T19:20", "latest": "2026-10-06T19:20"}, {"name": "docs"}],
            **changed}
        self.saved_report(slug).parent.mkdir(parents=True, exist_ok=True)
        _ = self.saved_report(slug).write_text(json.dumps(fields), encoding="utf-8")

    def write_open_for(self, slug: str, content: str) -> None:
        path = self.showrunner_state / "outstanding" / f"{slug}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content, encoding="utf-8")

    def children(self) -> list[dict[str, object]]:
        return cast(list[dict[str, object]], self.report()["children"])

    def style_ledger_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        ledger = SCRIPT.parent.parent / "style/style_ledger.py"
        result = subprocess.run([sys.executable, str(ledger), *arguments, "--repo", str(self.checkout)],
                                env=self.env, capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def prepare_style_ledger(self) -> dict[str, object]:
        rules = Path(self.env["STYLE_HISTORY_NATE_STYLE_DIR"]) / "rust"
        rules.mkdir(parents=True)
        _ = (rules / "names.md").write_text(
            "---\ntier: C\nfamily: names\ninput_scope: file\n---\n# Names\n", encoding="utf-8")
        _ = (self.checkout / "Cargo.toml").write_text(
            '[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n', encoding="utf-8")
        (self.checkout / "src").mkdir()
        _ = (self.checkout / "src/lib.rs").write_text("pub fn value() {}\n", encoding="utf-8")
        _ = self.git("add", "Cargo.toml", "src/lib.rs")
        _ = self.git("-c", "commit.gpgsign=false", "commit", "-m", "Add sample crate")
        _ = self.style_ledger_cli("set-tip", "--tip", self.git("rev-parse", "HEAD"))
        return cast(dict[str, object], json.loads(self.style_ledger_cli("summary", "--json").stdout))

    def mark_style_production(self) -> None:
        content = self.doc.read_text(encoding="utf-8")
        _ = self.doc.write_text(content.replace("## Production Context", "## Production Context\n\n- **Kind:** style"),
                                encoding="utf-8")
