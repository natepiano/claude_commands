"""Specification tests isolated from herdr, systemd and real account files."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NotRequired, TypedDict, cast, override
from unittest.mock import patch

# The work order requires a bare sibling import for unittest discovery.
import trial  # pyright: ignore[reportImplicitRelativeImport]


class CommandResponse(TypedDict):
    stdout: str
    code: int
    stderr: NotRequired[str]


class PaneDetails(TypedDict):
    pane_id: str
    label: str | None
    workspace_id: NotRequired[str]


class NewRootPane(TypedDict):
    pane_id: str
    label: NotRequired[str | None]


class CreatedWorkspace(TypedDict):
    workspace_id: str
    label: str


class CreatedTab(TypedDict):
    tab_id: str


class WorkspaceCreationResult(TypedDict):
    workspace: CreatedWorkspace
    tab: CreatedTab
    root_pane: NewRootPane


class WorkspaceCreationResponse(TypedDict):
    ok: bool
    result: WorkspaceCreationResult


class PaneCloseResult(TypedDict):
    type: str


class PaneCloseResponse(TypedDict):
    ok: bool
    result: PaneCloseResult


class PaneListResult(TypedDict):
    panes: list[PaneDetails]


class PaneListResponse(TypedDict):
    ok: bool
    result: PaneListResult


class AgentDetails(TypedDict):
    pane_id: str
    agent: str
    agent_status: str


class AgentListResult(TypedDict):
    agents: list[AgentDetails]


class AgentListResponse(TypedDict):
    ok: bool
    result: AgentListResult


class SettledAgentResult(TypedDict):
    agent: AgentDetails


class SettledAgentResponse(TypedDict):
    ok: bool
    result: SettledAgentResult


class AgentErrorDetail(TypedDict):
    code: str
    message: str


class AgentErrorResponse(TypedDict):
    ok: bool
    error: AgentErrorDetail


class StartedUnits(TypedDict):
    systemd_units: list[str]


class IssueRecord(TypedDict):
    time: str
    session: str
    expected: str
    happened: str


class ClaudeProjectTrust(TypedDict):
    hasTrustDialogAccepted: bool


class ClaudeTrustConfiguration(TypedDict):
    projects: dict[str, ClaudeProjectTrust]


class RecordedWorkOrder(TypedDict):
    file: str


WorkerAssignments = TypedDict("WorkerAssignments", {
    "slug-unit-impl": str,
    "slug-unit-review": str,
    "clamp-unit-impl": str,
    "clamp-unit-review": str,
    "count-unit-impl": str,
    "count-unit-review": str,
}, total=False)


@dataclass(frozen=True)
class InvocationResult:
    code: int
    stdout: str
    stderr: str


def herdr_args(*arguments: str) -> list[str]:
    """Use targeting confirmed by the impl slot against herdr 0.9.0."""
    return ["--session", "herdr-trial", *arguments]


def installed_git() -> str:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("scaffold fixture tests require Git")
    return executable


GIT_BINARY = installed_git()


class TrialTests(unittest.TestCase):
    root: Path = Path()
    scratch: Path = Path()
    log: Path = Path()
    response_file: Path = Path()
    fake: Path = Path()
    claude: Path = Path()
    codex: Path = Path()
    project: Path = Path()
    responses: dict[str, CommandResponse | list[CommandResponse]] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.scratch = self.root / "scratch"
        self.log = self.root / "argv.jsonl"
        self.response_file = self.root / "responses.json"
        self.fake = self.root / "fake-herdr"
        source = Path(__file__).with_name("fake_herdr.py").read_text()
        _ = self.fake.write_text("#!" + sys.executable + "\n" + source.split("\n", 1)[1])
        self.fake.chmod(0o755)
        self.claude = self.root / "claude-account"
        self.codex = self.root / "codex-account"
        self.claude.mkdir()
        self.codex.mkdir()
        self.project = self.root / "project" / "child"
        self.project.mkdir(parents=True)
        self.responses = {"default": {"stdout": "", "code": 0}}
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            "HERDR_BIN": str(self.fake),
            "HERDR_TRIAL_SYSTEMD_RUN": str(self.fake),
            "HERDR_TRIAL_SYSTEMCTL": str(self.fake),
            "HERDR_TRIAL_GIT": GIT_BINARY,
            "HERDR_TRIAL_ROOT": str(self.scratch),
            "HERDR_TRIAL_START_TIMEOUT": "0.5",
            "FAKE_HERDR_LOG": str(self.log),
            "FAKE_HERDR_RESPONSES": str(self.response_file),
            "CLAUDE_CONFIG_DIR": str(self.claude),
            "CODEX_HOME": str(self.codex),
            "HOME": str(self.root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Trial fixture",
            "GIT_AUTHOR_EMAIL": "trial@example.invalid",
            "GIT_COMMITTER_NAME": "Trial fixture",
            "GIT_COMMITTER_EMAIL": "trial@example.invalid",
            # An omitted tool override must fail instead of reaching a real tool.
            "PATH": str(self.root / "no-external-programs"),
        }, clear=True)))

    def configure(self, arguments: list[str], stdout: str = "", code: int = 0,
                  stderr: str = "") -> None:
        self.responses[" ".join(arguments)] = {
            "stdout": stdout, "code": code, "stderr": stderr,
        }

    def invoke(self, *arguments: str) -> InvocationResult:
        _ = self.response_file.write_text(json.dumps(self.responses))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = trial.main(list(arguments))
        return InvocationResult(code, stdout.getvalue(), stderr.getvalue())

    def invoke_cli(self, *arguments: str) -> InvocationResult:
        _ = self.response_file.write_text(json.dumps(self.responses))
        result = subprocess.run([
            sys.executable, str(Path(__file__).with_name("trial.py").resolve()), *arguments,
        ], cwd=self.project, capture_output=True, text=True, check=False, timeout=10)
        return InvocationResult(result.returncode, result.stdout, result.stderr)

    def fixture_git(self, directory: Path, *arguments: str) -> str:
        result = subprocess.run([GIT_BINARY, "-C", str(directory), *arguments],
                                capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def production_parser(self, document: Path, expression: str) -> InvocationResult:
        program = (
            "import sys\nfrom pathlib import Path\n" +
            "sys.path.insert(0, '/home/natepiano/.claude/scripts/production')\n" +
            "from add_unit import cell_value\n" +
            "from merge_checkpoint import optional_field, FieldPresent\n" +
            "from production_lifecycle import production_name\n" +
            "lines = Path(sys.argv[1]).read_text().splitlines()\n" + expression
        )
        result = subprocess.run([sys.executable, "-B", "-c", program, str(document)],
                                capture_output=True, text=True, check=False, timeout=10)
        return InvocationResult(result.returncode, result.stdout, result.stderr)

    def assignment(self, label: str, work_order: Path) -> None:
        directory = self.scratch / "work"
        directory.mkdir(parents=True, exist_ok=True)
        order: RecordedWorkOrder = {"file": str(work_order)}
        _ = (directory / f"{label}.json").write_text(json.dumps(order))

    def assigned_files(self) -> WorkerAssignments:
        files: dict[str, str] = {}
        for path in (self.scratch / "work").glob("*.json"):
            order = cast(RecordedWorkOrder, json.loads(path.read_text()))
            files[path.stem] = order["file"]
        return cast(WorkerAssignments, cast(object, files))

    def settled_response(self, pane_id: str, state: str, timeout: int = 1800000) -> None:
        response: SettledAgentResponse = {"ok": True, "result": {"agent": {
            "pane_id": pane_id, "agent": "codex", "agent_status": state,
        }}}
        self.configure(herdr_args("agent", "wait", pane_id, "--timeout", str(timeout)),
                       json.dumps(response))

    def assert_blocked_issue(self, result: InvocationResult, label: str) -> None:
        self.assertEqual(result.code, 2, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("blocked", result.stderr.casefold())
        lines = (self.scratch / "issues.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        issue = cast(IssueRecord, json.loads(lines[0]))
        self.assertEqual(set(issue), {"time", "session", "expected", "happened"})
        self.assertEqual(issue["session"], label)
        self.assertTrue(issue["expected"])
        self.assertIn("blocked", issue["happened"].casefold())
        self.assertEqual(datetime.fromisoformat(issue["time"]).utcoffset(), timedelta(0))

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [cast(list[str], json.loads(line))
                for line in self.log.read_text().splitlines()]

    def started_units(self) -> StartedUnits:
        value = cast(StartedUnits, json.loads((self.scratch / "started.json").read_text()))
        self.assertEqual(set(value), {"systemd_units"})
        return value

    def assert_success(self, result: InvocationResult) -> None:
        self.assertEqual(result.code, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def assert_refusal(self, result: InvocationResult, *names: str) -> None:
        self.assertEqual(result.code, 1, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        for name in names:
            self.assertIn(name, result.stderr)

    def trust_configs(self, claude_projects: dict[str, bool],
                      codex_projects: dict[str, str]) -> None:
        configuration: ClaudeTrustConfiguration = {
            "projects": {path: {"hasTrustDialogAccepted": accepted}
                         for path, accepted in claude_projects.items()},
        }
        _ = (self.claude / ".claude.json").write_text(json.dumps(configuration))
        _ = (self.codex / "config.toml").write_text("\n".join(
            f"[projects.{json.dumps(path)}]\ntrust_level = {json.dumps(level)}\n"
            for path, level in codex_projects.items()
        ))

    def pane_list(self, panes: list[PaneDetails]) -> None:
        response: PaneListResponse = {"ok": True, "result": {"panes": panes}}
        self.configure(herdr_args("pane", "list"), json.dumps(response))

    def test_scaffold_builds_bare_origin_committed_branches_and_three_worktrees(self) -> None:
        self.scratch.mkdir()
        started = self.scratch / "started.json"
        issues = self.scratch / "issues.jsonl"
        _ = started.write_text('{"systemd_units": ["herdr-trial-server"]}\n')
        _ = issues.write_text('existing phase one evidence\n')
        before = (started.read_bytes(), issues.read_bytes())
        self.assert_success(self.invoke("scaffold"))
        repo = self.scratch / "repo"
        origin = self.scratch / "origin.git"
        self.assertEqual(self.fixture_git(origin, "rev-parse", "--is-bare-repository"), "true")
        initial = self.fixture_git(repo, "rev-parse", "main")
        for reference in ("origin/main", "herdr-fake", "origin/herdr-fake"):
            self.assertEqual(self.fixture_git(repo, "rev-parse", reference), initial)
        self.assertEqual(self.fixture_git(repo, "status", "--porcelain"), "")
        self.assertTrue((repo / "README.md").is_file())
        self.assertTrue((repo / "textkit" / "__init__.py").is_file())
        self.assertTrue((repo / "textkit" / "test_textkit.py").is_file())
        for name in ("slug", "clamp", "count"):
            checkout = self.scratch / "worktrees" / f"{name}-unit"
            self.assertEqual(self.fixture_git(checkout, "branch", "--show-current"),
                             f"herdr-fake-{name}")
            self.assertEqual(self.fixture_git(checkout, "rev-parse", "HEAD"), initial)
            self.assertEqual(self.fixture_git(checkout, "status", "--porcelain"), "")
            self.assertTrue((checkout / "textkit" / "__init__.py").is_file())
        with patch.object(subprocess, "run", side_effect=AssertionError("second scaffold starts nothing")):
            self.assert_refusal(self.invoke("scaffold"), str(repo))
        self.assertEqual(self.fixture_git(repo, "rev-parse", "main"), initial)
        baseline = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "textkit"],
                                  cwd=repo, capture_output=True, text=True, check=False, timeout=10)
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        self.assertIn("Ran 1 test", baseline.stderr)
        self.assertEqual((started.read_bytes(), issues.read_bytes()), before)
        self.assertEqual(self.calls(), [])

    def test_scaffold_writes_production_rules_and_delegate_ready_unit_plans(self) -> None:
        self.assert_success(self.invoke("scaffold"))
        repo = self.scratch / "repo"
        plans = repo / "docs" / "plans"
        production = (plans / "herdr-fake-production.md").read_text()
        for text in (
            "> **Status: PRODUCTION — running.**", "## Production Context", "## Units",
            "**Repository:**", str(repo), "**Merge branch:**", "herdr-fake",
            "**Showrunner checkout:**", "**Showrunner session:**", "natedev",
            "**Log:**", "docs/plans/herdr-fake-log.md", "**User zone:**", "America/New_York",
            "**Updates:**", "on demand", "**Merge tests:**", "python3 -m unittest discover -s textkit",
            "**Push:**", "git", "**Promote:**", "none", "herdr session herdr-trial",
            "/home/natepiano/worktrees/claude-build-followups-herdr-trial/scripts/herdr/trial.py",
            "tmux", "add_unit.py", "trial.py issue", "never promote or wrap",
            "never answer a trust or permission prompt",
        ):
            self.assertIn(text, production)
        functions = {"slug": "slugify", "clamp": "clamp", "count": "word_count"}
        for name, function in functions.items():
            unit = f"{name}-unit"
            filename = f"herdr-fake-{name}.md"
            self.assertIn(filename, production)
            self.assertIn(unit, production)
            self.assertIn(str(self.scratch / "worktrees" / unit), production)
            self.assertIn(f"herdr-fake-{name}", production)
            plan = (plans / filename).read_text()
            for text in ("Delegation Context", "Phase 1", "Work Order", function,
                         "textkit/__init__.py", "test", f"{unit}-impl", f"{unit}-review",
                         f"trial.py work {unit}", f"trial.py wait {unit}"):
                self.assertIn(text, plan)
            self.assertNotIn("Phase 2", plan)
            self.assertIn(filename, self.fixture_git(repo, "ls-tree", "-r", "--name-only", "HEAD"))
        self.trust_configs({str(self.root): True}, {str(self.root): "trusted"})
        for checkout in (repo, *(self.scratch / "worktrees" / f"{name}-unit"
                                  for name in functions)):
            self.assert_success(self.invoke("trust-check", str(checkout)))

    def test_scaffold_refuses_existing_repo_before_running_git_or_replacing_files(self) -> None:
        repo = self.scratch / "repo"
        repo.mkdir(parents=True)
        marker = repo / "keep.txt"
        _ = marker.write_text("do not replace\n")
        with patch.object(subprocess, "run", side_effect=AssertionError("refusal must start nothing")):
            self.assert_refusal(self.invoke("scaffold"), str(repo))
        self.assertEqual(marker.read_text(), "do not replace\n")
        self.assertFalse((self.scratch / "origin.git").exists())
        self.assertEqual(self.calls(), [])

    def test_rendered_production_push_parses_as_git_with_the_real_merge_parser(self) -> None:
        self.assert_success(self.invoke("scaffold"))
        document = self.scratch / "repo" / "docs" / "plans" / "herdr-fake-production.md"
        result = self.production_parser(document,
                                        "field = optional_field(lines, 'Push')\n" +
                                        "assert isinstance(field, FieldPresent)\n" +
                                        "print(cell_value(field.text))\n")
        self.assert_success(result)
        self.assertEqual(result.stdout, "git\n")

    def test_rendered_production_name_is_accepted_by_the_real_lifecycle_parser(self) -> None:
        self.assert_success(self.invoke("scaffold"))
        document = self.scratch / "repo" / "docs" / "plans" / "herdr-fake-production.md"
        result = self.production_parser(document, "print(production_name(lines))\n")
        self.assert_success(result)
        self.assertEqual(result.stdout, "herdr-fake\n")

    def test_concurrent_worker_dispatches_preserve_both_assignments_for_separate_waits(self) -> None:
        units = {"slug-unit": "w38:p71", "clamp-unit": "w38:p81"}
        self.pane_list([{"pane_id": pane, "label": f"{unit}-impl"}
                        for unit, pane in units.items()])
        orders: dict[str, Path] = {}
        for unit in units:
            order = self.root / f"{unit}.md"
            _ = order.write_text("tiny simultaneous work order\n")
            orders[unit] = order
        _ = self.response_file.write_text(json.dumps(self.responses))
        launcher = [sys.executable, "-B", str(Path(__file__).with_name("trial.py").resolve()), "work"]
        with subprocess.Popen([*launcher, "slug-unit", "impl", str(orders["slug-unit"])],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as first:
            with subprocess.Popen([*launcher, "clamp-unit", "impl", str(orders["clamp-unit"])],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as second:
                try:
                    first_stdout, first_stderr = first.communicate(timeout=10)
                    second_stdout, second_stderr = second.communicate(timeout=10)
                    self.assert_success(InvocationResult(first.returncode, first_stdout, first_stderr))
                    self.assert_success(InvocationResult(second.returncode, second_stdout, second_stderr))
                finally:
                    for process in (first, second):
                        if process.poll() is None:
                            process.kill()
        for unit, pane in units.items():
            with self.subTest(unit=unit):
                self.assertTrue((self.scratch / "work" / f"{unit}-impl.json").is_file())
                report = Path(str(orders[unit]) + ".done")
                _ = report.write_text(f"{unit} completed\n")
                self.settled_response(pane, "done")
                result = self.invoke("wait", unit, "impl")
                self.assert_success(result)
                self.assertEqual(result.stdout, str(report) + "\n")
        self.assertFalse((self.scratch / "work.json").exists())
        prompts = [call for call in self.calls() if call[2:4] == ["agent", "prompt"]]
        self.assertEqual({call[4] for call in prompts}, set(units.values()))
        self.assertEqual(len(prompts), 2)

    def test_work_records_file_removes_stale_report_and_submits_literal_prompt_once(self) -> None:
        label = "slug-unit-impl"
        work_order = self.root / "work order ' café.md"
        report = Path(str(work_order) + ".done")
        _ = work_order.write_text("tiny work order\n")
        _ = report.write_text("stale report\n")
        other_file = self.root / "other.md"
        self.assignment("count-unit-review", other_file)
        self.pane_list([{"pane_id": "w38:p71", "label": label}])
        prompt = f"Read and carry out {work_order}. When done, write your report to {report}."
        expected = herdr_args("agent", "prompt", "w38:p71", prompt)
        original_run = subprocess.run

        def check_dispatch_order(argv: list[str], *, capture_output: bool, text: bool,
                                 check: bool, timeout: float) -> subprocess.CompletedProcess[str]:
            if argv == [str(self.fake), *expected]:
                self.assertFalse(report.exists())
                self.assertEqual(self.assigned_files().get(label), str(work_order))
            return original_run(argv, capture_output=capture_output, text=text,
                                check=check, timeout=timeout)

        with patch.object(subprocess, "run", side_effect=check_dispatch_order):
            self.assert_success(self.invoke("work", "slug-unit", "impl", str(work_order)))
        self.assertFalse(report.exists())
        self.assertEqual(self.assigned_files(), {
            "count-unit-review": str(other_file), label: str(work_order),
        })
        self.assertEqual(self.calls(), [herdr_args("pane", "list"), expected])

    def test_work_and_wait_use_role_labels_and_refuse_invalid_roles_without_input(self) -> None:
        for action in ("work", "wait"):
            arguments = [action, "slug-unit", "director"]
            if action == "work":
                arguments.append(str(self.root / "order.md"))
            with self.subTest(action=action):
                result = self.invoke(*arguments)
                self.assertEqual(result.code, 2, result.stderr)
                self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.scratch.exists())

    def test_wait_in_separate_process_uses_recorded_assignment_and_prints_settled_report_path(self) -> None:
        label = "clamp-unit-review"
        work_order = self.root / "review.md"
        report = Path(str(work_order) + ".done")
        _ = work_order.write_text("review the tiny change\n")
        self.pane_list([{"pane_id": "w38:p82", "label": label}])
        self.assert_success(self.invoke("work", "clamp-unit", "review", str(work_order)))
        _ = report.write_text("Review complete: the change passes.\n")
        for state in ("idle", "done"):
            with self.subTest(state=state):
                self.settled_response("w38:p82", state)
                result = self.invoke_cli("wait", "clamp-unit", "review")
                self.assert_success(result)
                self.assertEqual(result.stdout, str(report) + "\n")
        prompt_calls = [call for call in self.calls() if call[2:4] == ["agent", "prompt"]]
        self.assertEqual(len(prompt_calls), 1)
        self.assertEqual(self.calls()[-2:], [
            herdr_args("pane", "list"),
            herdr_args("agent", "wait", "w38:p82", "--timeout", "1800000"),
        ])

    def test_wait_without_assignment_refuses_before_contacting_herdr(self) -> None:
        for has_map in (False, True):
            with self.subTest(has_map=has_map):
                if has_map:
                    self.assignment("count-unit-impl", self.root / "other.md")
                self.assert_refusal(self.invoke("wait", "slug-unit", "impl"), "slug-unit-impl")
                self.assertEqual(self.calls(), [])

    def test_relative_work_file_remains_resolvable_when_wait_runs_from_another_directory(self) -> None:
        work_order = self.project / "relative.md"
        report = Path(str(work_order) + ".done")
        _ = work_order.write_text("work from the worker checkout\n")
        self.pane_list([{"pane_id": "w38:p93", "label": "count-unit-impl"}])
        self.assert_success(self.invoke_cli("work", "count-unit", "impl", "relative.md"))
        self.assertEqual(self.assigned_files().get("count-unit-impl"), str(work_order))
        _ = report.write_text("finished\n")
        self.settled_response("w38:p93", "done")
        result = self.invoke("wait", "count-unit", "impl")
        self.assert_success(result)
        self.assertEqual(result.stdout, str(report) + "\n")
        self.assertEqual(sum(call[2:4] == ["agent", "prompt"] for call in self.calls()), 1)

    def test_wait_keeps_waiting_after_initial_idle_until_report_or_deadline_without_resubmitting(self) -> None:
        work_order = self.root / "order.md"
        report = Path(str(work_order) + ".done")
        _ = work_order.write_text("work that has not started its turn yet\n")
        self.pane_list([{"pane_id": "w38:p93", "label": "count-unit-impl"}])
        self.assert_success(self.invoke("work", "count-unit", "impl", str(work_order)))
        states = iter(("idle", "working", "done"))
        waits: list[list[str]] = []
        original_run = subprocess.run

        def finish_after_start(argv: list[str], *, capture_output: bool, text: bool,
                               check: bool, timeout: float) -> subprocess.CompletedProcess[str]:
            if argv[3:5] == ["agent", "wait"]:
                state = next(states)
                waits.append(argv[1:])
                self.assertEqual("--until" in argv, state == "working")
                if state == "working":
                    self.assertEqual(argv[argv.index("--until") + 1], "working")
                self.assertFalse(report.exists())
                response: SettledAgentResponse = {"ok": True, "result": {"agent": {
                    "pane_id": "w38:p93", "agent": "codex", "agent_status": state,
                }}}
                self.configure(argv[1:], json.dumps(response))
                _ = self.response_file.write_text(json.dumps(self.responses))
                result = original_run(argv, capture_output=capture_output, text=text,
                                      check=check, timeout=timeout)
                if state == "done":
                    _ = report.write_text("report arrives after the turn starts\n")
                return result
            return original_run(argv, capture_output=capture_output, text=text,
                                check=check, timeout=timeout)

        with patch.object(subprocess, "run", side_effect=finish_after_start):
            result = self.invoke("wait", "count-unit", "impl")
        self.assert_success(result)
        self.assertEqual(result.stdout, str(report) + "\n")
        self.assertEqual(len(waits), 3)
        remaining = [int(call[call.index("--timeout") + 1]) for call in waits]
        self.assertTrue(all(0 < milliseconds <= 1800000 for milliseconds in remaining))
        self.assertEqual(remaining, sorted(remaining, reverse=True))
        report.unlink()
        self.settled_response("w38:p93", "idle", timeout=0)
        with patch.dict(os.environ, {"HERDR_TRIAL_WAIT_TIMEOUT": "0"}):
            expired = self.invoke("wait", "count-unit", "impl")
        self.assert_refusal(expired)
        self.assertRegex(expired.stderr.casefold(), "deadline|timed out|timeout|report missing")
        self.assertEqual(sum(call[2:4] == ["agent", "prompt"] for call in self.calls()), 1)

    def test_wait_deadline_exceeds_ten_seconds_and_prompt_is_not_resubmitted(self) -> None:
        work_order = self.root / "long-order.md"
        _ = work_order.write_text("takes longer than ten seconds\n")
        self.pane_list([{"pane_id": "w38:p71", "label": "slug-unit-impl"}])
        self.assert_success(self.invoke("work", "slug-unit", "impl", str(work_order)))
        _ = Path(str(work_order) + ".done").write_text("long work finished\n")
        original_run = subprocess.run
        deadlines: list[float] = []

        def emulate_long_wait(argv: list[str], *, capture_output: bool, text: bool,
                              check: bool, timeout: float) -> subprocess.CompletedProcess[str]:
            if argv[3:5] == ["agent", "wait"]:
                milliseconds = int(argv[-1])
                deadlines.append(timeout)
                # Model the long wait at its process boundary without sleeping.
                if timeout <= milliseconds / 1000:
                    raise subprocess.TimeoutExpired(argv, timeout)
            return original_run(argv, capture_output=capture_output, text=text,
                                check=check, timeout=timeout)

        for milliseconds in (1800000, 25000):
            with self.subTest(milliseconds=milliseconds):
                self.settled_response("w38:p71", "done", milliseconds)
                with patch.dict(os.environ, {"HERDR_TRIAL_WAIT_TIMEOUT": str(milliseconds)}):
                    with patch.object(subprocess, "run", side_effect=emulate_long_wait):
                        self.assert_success(self.invoke("wait", "slug-unit", "impl"))
        self.assertEqual(len(deadlines), 2)
        self.assertGreater(deadlines[0], 1800)
        self.assertGreater(deadlines[1], 25)
        self.assertEqual(sum(call[2:4] == ["agent", "prompt"] for call in self.calls()), 1)
        self.assertFalse(any("--until" in call for call in self.calls()))

    def test_work_agent_blocked_refusal_logs_one_issue_without_raw_input(self) -> None:
        label = "slug-unit-review"
        work_order = self.root / "review.md"
        _ = work_order.write_text("review\n")
        self.pane_list([{"pane_id": "w38:p72", "label": label}])
        response: AgentErrorResponse = {"ok": False, "error": {
            "code": "agent_blocked", "message": "agent is blocked by a permission dialog",
        }}
        prompt = (f"Read and carry out {work_order}. When done, write your report to "
                  f"{work_order}.done.")
        arguments = herdr_args("agent", "prompt", "w38:p72", prompt)
        self.configure(arguments, code=1, stderr=json.dumps(response))
        self.assert_blocked_issue(self.invoke("work", "slug-unit", "review", str(work_order)), label)
        self.assertEqual(self.calls(), [herdr_args("pane", "list"), arguments])

    def test_wait_settled_blocked_logs_issue_even_when_a_report_exists(self) -> None:
        label = "count-unit-review"
        work_order = self.root / "review.md"
        self.assignment(label, work_order)
        _ = Path(str(work_order) + ".done").write_text("do not accept while blocked\n")
        self.pane_list([{"pane_id": "w38:p94", "label": label}])
        self.settled_response("w38:p94", "blocked")
        result = self.invoke("wait", "count-unit", "review")
        self.assert_blocked_issue(result, label)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.calls(), [
            herdr_args("pane", "list"),
            herdr_args("agent", "wait", "w38:p94", "--timeout", "1800000"),
        ])

    def test_wait_agent_blocked_error_logs_one_issue_and_never_submits_input(self) -> None:
        label = "clamp-unit-impl"
        self.assignment(label, self.root / "order.md")
        self.pane_list([{"pane_id": "w38:p81", "label": label}])
        response: AgentErrorResponse = {"ok": False, "error": {
            "code": "agent_blocked", "message": "blocked while waiting",
        }}
        arguments = herdr_args("agent", "wait", "w38:p81", "--timeout", "1800000")
        self.configure(arguments, code=1, stderr=json.dumps(response))
        self.assert_blocked_issue(self.invoke("wait", "clamp-unit", "impl"), label)
        self.assertEqual(self.calls(), [herdr_args("pane", "list"), arguments])

    def test_wait_timeout_refuses_without_resending_the_prompt(self) -> None:
        label = "clamp-unit-review"
        work_order = self.root / "review.md"
        self.assignment(label, work_order)
        self.pane_list([{"pane_id": "w38:p82", "label": label}])
        response: AgentErrorResponse = {"ok": False, "error": {
            "code": "timeout", "message": "agent wait timed out",
        }}
        arguments = herdr_args("agent", "wait", "w38:p82", "--timeout", "1800000")
        self.configure(arguments, code=1, stderr=json.dumps(response))
        result = self.invoke("wait", "clamp-unit", "review")
        self.assert_refusal(result)
        self.assertIn("timed out", result.stderr)
        self.assertEqual(self.assigned_files(), {label: str(work_order)})
        self.assertEqual(self.calls(), [herdr_args("pane", "list"), arguments])

    def test_pane_id_finds_exact_label_and_uses_opaque_id(self) -> None:
        self.pane_list([
            {"pane_id": "w4:p9", "label": "smoke-other"},
            {"pane_id": "w27:p61", "label": "smoke"},
            {"pane_id": "w4:p10", "label": None},
        ])
        result = self.invoke("pane-id", "smoke")
        self.assert_success(result)
        self.assertEqual(result.stdout, "w27:p61\n")
        self.assertEqual(self.calls(), [herdr_args("pane", "list")])

    def test_pane_id_zero_matches_names_label(self) -> None:
        self.pane_list([{"pane_id": "w4:p9", "label": "another"}])
        result = self.invoke("pane-id", "missing-label")
        self.assert_refusal(result, "missing-label")
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.calls(), [herdr_args("pane", "list")])

    def test_pane_id_two_matches_refuses_ambiguity(self) -> None:
        self.pane_list([
            {"pane_id": "w1:p1", "label": "smoke"},
            {"pane_id": "w9:p8", "label": "smoke"},
        ])
        result = self.invoke("pane-id", "smoke")
        self.assert_refusal(result, "smoke")
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.calls(), [herdr_args("pane", "list")])

    def test_open_refuses_an_existing_label_without_creating_or_running_anything(self) -> None:
        cases: tuple[list[PaneDetails], ...] = (
            [{"pane_id": "w27:p61", "label": "smoke"}],
            [{"pane_id": "w27:p61", "label": "smoke"},
             {"pane_id": "w8:p9", "label": "smoke"}],
        )
        for panes in cases:
            with self.subTest(matches=len(panes)):
                if self.log.exists():
                    self.log.unlink()
                self.pane_list(panes)
                result = self.invoke("open", "smoke", "--command", "never run")
                self.assert_refusal(result, "smoke")
                self.assertEqual(result.stdout, "")
                self.assertEqual(self.calls(), [herdr_args("pane", "list")])

    def test_open_labels_returned_root_pane_and_runs_only_requested_command(self) -> None:
        cases: tuple[tuple[NewRootPane, bool], ...] = (
            ({"pane_id": "w27:p61"}, True),
            ({"pane_id": "w27:p61", "label": None}, True),
            ({"pane_id": "w27:p61", "label": "smoke"}, False),
        )
        for root_pane, explicit_options in cases:
            with self.subTest(root_pane=root_pane, explicit_options=explicit_options):
                if self.log.exists():
                    self.log.unlink()
                self.pane_list([{"pane_id": "w4:p9", "label": "smoke-other"}])
                cwd = str(self.project) if explicit_options else os.getcwd()
                create = herdr_args("workspace", "create", "--label", "smoke",
                                    "--cwd", cwd, "--no-focus")
                response: WorkspaceCreationResponse = {"ok": True, "result": {
                    "workspace": {"workspace_id": "w27", "label": "smoke"},
                    "tab": {"tab_id": "w27:t42"}, "root_pane": root_pane,
                }}
                self.configure(create, json.dumps(response))
                arguments = ["open", "smoke"]
                expected = [herdr_args("pane", "list"), create]
                if root_pane.get("label") != "smoke":
                    expected.append(herdr_args("pane", "rename", "w27:p61", "smoke"))
                if explicit_options:
                    command = "zsh -ic 'echo $HOME ; café'"
                    arguments.extend(["--cwd", cwd, "--command", command])
                    expected.append(herdr_args("pane", "run", "w27:p61", command))
                result = self.invoke(*arguments)
                self.assert_success(result)
                self.assertEqual(result.stdout, "w27:p61\n")
                self.assertEqual(self.calls(), expected)

    def test_close_targets_labelled_pane_and_preserves_workspace_with_other_panes(self) -> None:
        target: PaneDetails = {
            "pane_id": "w27:p61", "label": "smoke", "workspace_id": "w27",
        }
        sibling: PaneDetails = {
            "pane_id": "w27:p84", "label": "keep", "workspace_id": "w27",
        }
        unrelated: PaneDetails = {
            "pane_id": "w9:p2", "label": "elsewhere", "workspace_id": "w9",
        }
        for remaining in ([], [sibling]):
            with self.subTest(other_panes=len(remaining)):
                if self.log.exists():
                    self.log.unlink()
                self.pane_list([unrelated, target, *remaining])
                response: PaneCloseResponse = {"ok": True, "result": {"type": "ok"}}
                self.configure(herdr_args("pane", "close", "w27:p61"), json.dumps(response))
                result = self.invoke("close", "smoke")
                self.assert_success(result)
                self.assertEqual(result.stdout, "")
                # Live 0.9.0 removes the workspace when its last pane closes;
                # an explicit workspace close would also destroy a surviving sibling.
                self.assertEqual(self.calls(), [
                    herdr_args("pane", "list"), herdr_args("pane", "close", "w27:p61"),
                ])

    def test_read_prints_plain_pane_text_from_resolved_id_and_visible_source(self) -> None:
        self.pane_list([{"pane_id": "w27:p61", "label": "smoke"}])
        self.configure(herdr_args("pane", "read", "w27:p61", "--source", "visible"),
                       "echo $HOME ; ok\n/home/example\n")
        result = self.invoke("read", "smoke")
        self.assert_success(result)
        self.assertEqual(result.stdout.rstrip("\n"), "echo $HOME ; ok\n/home/example")
        self.assertEqual(self.calls(), [
            herdr_args("pane", "list"),
            herdr_args("pane", "read", "w27:p61", "--source", "visible"),
        ])

    def test_type_preserves_literal_text_and_sends_enter_after_text(self) -> None:
        self.pane_list([{"pane_id": "w27:p61", "label": "smoke"}])
        text = "echo $HOME ; ok\n'quoted' café $(never-run)"
        self.assert_success(self.invoke("type", "smoke", text))
        self.assertEqual(self.calls(), [
            herdr_args("pane", "list"),
            herdr_args("pane", "send-text", "w27:p61", text),
            herdr_args("pane", "send-keys", "w27:p61", "enter"),
        ])

    def test_read_and_type_refuse_missing_or_ambiguous_labels_before_input(self) -> None:
        pane_cases: list[list[PaneDetails]] = [[], [
            {"pane_id": "w1:p1", "label": "smoke"},
            {"pane_id": "w9:p8", "label": "smoke"},
        ]]
        for panes in pane_cases:
            for arguments in (("read", "smoke"), ("type", "smoke", "do not submit")):
                with self.subTest(panes=panes, arguments=arguments):
                    if self.log.exists():
                        self.log.unlink()
                    self.pane_list(panes)
                    self.assert_refusal(self.invoke(*arguments), "smoke")
                    self.assertEqual(self.calls(), [herdr_args("pane", "list")])

    def test_type_does_not_press_enter_if_sending_text_fails(self) -> None:
        self.pane_list([{"pane_id": "w27:p61", "label": "smoke"}])
        self.configure(herdr_args("pane", "send-text", "w27:p61", "hello"), code=1,
                       stderr='{"error":{"code":"pane_closed","message":"pane closed"}}\n')
        result = self.invoke("type", "smoke", "hello")
        self.assert_refusal(result)
        self.assertEqual(self.calls(), [
            herdr_args("pane", "list"),
            herdr_args("pane", "send-text", "w27:p61", "hello"),
        ])

    def test_agents_joins_labels_by_id_and_prints_kind_and_state(self) -> None:
        self.pane_list([
            {"pane_id": "w8:p7", "label": "director"},
            {"pane_id": "w8:p9", "label": "worker"},
        ])
        response: AgentListResponse = {"ok": True, "result": {"agents": [
            {"pane_id": "w8:p9", "agent": "codex", "agent_status": "blocked"},
            {"pane_id": "w8:p7", "agent": "claude", "agent_status": "idle"},
        ]}}
        self.configure(herdr_args("agent", "list"), json.dumps(response))
        result = self.invoke("agents")
        self.assert_success(result)
        self.assertCountEqual(result.stdout.splitlines(), [
            "worker\tcodex\tblocked", "director\tclaude\tidle",
        ])
        self.assertCountEqual(self.calls(), [herdr_args("pane", "list"),
                                            herdr_args("agent", "list")])

    def test_agents_empty_list_prints_no_lines(self) -> None:
        self.pane_list([])
        response: AgentListResponse = {"ok": True, "result": {"agents": []}}
        self.configure(herdr_args("agent", "list"), json.dumps(response))
        result = self.invoke("agents")
        self.assert_success(result)
        self.assertEqual(result.stdout, "")

    def test_fake_recorder_exact_argv_default_and_exit_code(self) -> None:
        arguments = ["pane", "send-text", "w9:p4", "echo '$HOME'; café\nsecond line"]
        self.configure(arguments, "configured output\n", 7)
        _ = self.response_file.write_text(json.dumps(self.responses))
        result = subprocess.run([str(self.fake), *arguments], capture_output=True,
                                text=True, check=False)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "configured output\n")
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.calls(), [arguments])
        self.responses["default"] = {"stdout": "default output", "code": 3}
        _ = self.response_file.write_text(json.dumps(self.responses))
        result = subprocess.run([str(self.fake), "unconfigured"], capture_output=True,
                                text=True, check=False)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "default output")
        self.assertEqual(self.calls(), [arguments, ["unconfigured"]])

    def test_server_start_launches_named_unit_then_records_it(self) -> None:
        status = herdr_args("status", "server")
        self.responses[" ".join(status)] = [
            {"stdout": "", "code": 1},
            {"stdout": "session: herdr-trial\nstatus: running\n", "code": 0},
        ]
        result = self.invoke("server", "start")
        self.assert_success(result)
        launch = ["--user", "--unit=herdr-trial-server", str(self.fake),
                  *herdr_args("server")]
        calls = self.calls()
        self.assertEqual(calls[0], status)
        self.assertEqual(calls.count(launch), 1)
        self.assertGreaterEqual(calls.count(status), 2)
        self.assertGreater(calls.index(launch), calls.index(status))
        self.assertEqual(self.started_units(), {"systemd_units": ["herdr-trial-server"]})

    def test_unreadable_started_record_refuses_before_launching_any_unit(self) -> None:
        self.scratch.mkdir()
        path = self.scratch / "started.json"
        self.configure(herdr_args("status", "server"), "status: not running\n")
        for unreadable in (False, True):
            if unreadable:
                path.unlink()
                path.mkdir()
            else:
                _ = path.write_text("{")
            for arguments in (("client", "open"), ("server", "start")):
                with self.subTest(unreadable=unreadable, arguments=arguments):
                    if self.log.exists():
                        self.log.unlink()
                    result = self.invoke(*arguments)
                    self.assert_refusal(result, "started.json")
                    self.assertFalse(any("--unit=herdr-trial-client" in call
                                         or "--unit=herdr-trial-server" in call
                                         for call in self.calls()), self.calls())
                    if not unreadable:
                        self.assertEqual(path.read_text(), "{")
                    else:
                        self.assertTrue(path.is_dir())

    def test_up_starts_server_before_client_and_reuses_recorded_active_client(self) -> None:
        status = herdr_args("status", "server")
        self.responses[" ".join(status)] = [
            {"stdout": "status: not running\n", "code": 0},
            {"stdout": "status: running\n", "code": 0},
        ]
        self.configure(["--user", "is-active", "herdr-trial-client"], "active\n")
        server_launch = ["--user", "--unit=herdr-trial-server", str(self.fake),
                         *herdr_args("server")]
        client_launch = ["--user", "--unit=herdr-trial-client", "konsole", "-e",
                         str(self.fake), *herdr_args()]
        self.assert_success(self.invoke("up"))
        first_calls = self.calls()
        self.assertEqual(first_calls.count(server_launch), 1)
        self.assertEqual(first_calls.count(client_launch), 1)
        self.assertLess(first_calls.index(server_launch), first_calls.index(client_launch))
        self.assertGreaterEqual(first_calls[:first_calls.index(client_launch)].count(status), 2)
        self.assertEqual(self.started_units(), {
            "systemd_units": ["herdr-trial-server", "herdr-trial-client"],
        })
        before = (self.scratch / "started.json").read_bytes()
        self.assert_success(self.invoke("up"))
        repeated_calls = self.calls()[len(first_calls):]
        self.assertNotIn(server_launch, repeated_calls)
        self.assertNotIn(client_launch, repeated_calls)
        self.assertIn(["--user", "is-active", "herdr-trial-client"], repeated_calls)
        self.assertEqual((self.scratch / "started.json").read_bytes(), before)

    def test_server_stop_timeout_still_stops_active_unit_and_reports_failure(self) -> None:
        stop = [str(self.fake), *herdr_args("server", "stop")]
        original_run = subprocess.run

        def run_with_stop_timeout(argv: list[str], *, capture_output: bool,
                                  text: bool, check: bool,
                                  timeout: float) -> subprocess.CompletedProcess[str]:
            if argv == stop:
                with self.log.open("a") as output:
                    _ = output.write(json.dumps(argv[1:]) + "\n")
                raise subprocess.TimeoutExpired(argv, timeout)
            return original_run(argv, capture_output=capture_output, text=text,
                                check=check, timeout=timeout)

        self.configure(["--user", "is-active", "herdr-trial-server"], "active\n")
        with patch.object(subprocess, "run", side_effect=run_with_stop_timeout):
            result = self.invoke("server", "stop")
        self.assert_refusal(result, "herdr-trial")
        self.assertIn("timed out", result.stderr.casefold())
        self.assertEqual(self.calls(), [
            herdr_args("server", "stop"),
            ["--user", "is-active", "herdr-trial-server"],
            ["--user", "stop", "herdr-trial-server"],
        ])

    def test_server_start_when_answering_changes_nothing(self) -> None:
        self.configure(herdr_args("status", "server"), "session: herdr-trial\nstatus: running\n")
        result = self.invoke("server", "start")
        self.assert_success(result)
        self.assertEqual(self.calls(), [herdr_args("status", "server")])
        self.assertFalse(self.scratch.exists())

    def test_server_status_exit_zero_without_running_still_launches_and_waits(self) -> None:
        # Live 0.9.0 reports "not running" with a successful process exit.
        status = herdr_args("status", "server")
        self.responses[" ".join(status)] = [
            {"stdout": "session: herdr-trial\nstatus: not running\n", "code": 0},
            {"stdout": "session: herdr-trial\nstatus: not running\n", "code": 0},
            {"stdout": "session: herdr-trial\nstatus: running\n", "code": 0},
        ]
        self.assert_success(self.invoke("server", "start"))
        self.assertEqual(self.calls(), [
            status,
            ["--user", "--unit=herdr-trial-server", str(self.fake), *herdr_args("server")],
            status,
            status,
        ])
        self.assertEqual(self.started_units(), {"systemd_units": ["herdr-trial-server"]})

    def test_repeated_server_start_preserves_started_file(self) -> None:
        self.scratch.mkdir()
        before = '{"systemd_units": ["herdr-trial-server", "herdr-trial-client"]}\n'
        _ = (self.scratch / "started.json").write_text(before)
        self.configure(herdr_args("status", "server"), "session: herdr-trial\nstatus: running\n")
        self.assert_success(self.invoke("server", "start"))
        self.assertEqual((self.scratch / "started.json").read_text(), before)
        self.assertEqual(self.calls(), [herdr_args("status", "server")])

    def test_server_start_timeout_names_server_and_records_started_unit(self) -> None:
        self.configure(herdr_args("status", "server"), "status: not running\n")
        with patch.dict(os.environ, {"HERDR_TRIAL_START_TIMEOUT": "0"}):
            result = self.invoke("server", "start")
        self.assert_refusal(result, "server")
        self.assertIn(["--user", "--unit=herdr-trial-server", str(self.fake),
                       *herdr_args("server")], self.calls())
        self.assertEqual(self.started_units(), {"systemd_units": ["herdr-trial-server"]})

    def test_server_stop_stops_only_the_named_active_unit(self) -> None:
        self.configure(["--user", "is-active", "herdr-trial-server"], "active\n")
        self.assert_success(self.invoke("server", "stop"))
        self.assertEqual(self.calls(), [
            herdr_args("server", "stop"),
            ["--user", "is-active", "herdr-trial-server"],
            ["--user", "stop", "herdr-trial-server"],
        ])

    def test_server_stop_leaves_inactive_unit_alone(self) -> None:
        self.configure(["--user", "is-active", "herdr-trial-server"], "inactive\n", 3)
        self.assert_success(self.invoke("server", "stop"))
        self.assertEqual(self.calls(), [
            herdr_args("server", "stop"),
            ["--user", "is-active", "herdr-trial-server"],
        ])

    def test_client_open_launches_konsole_with_named_session_and_records_unit(self) -> None:
        self.assert_success(self.invoke("client", "open"))
        self.assertEqual(self.calls(), [[
            "--user", "--unit=herdr-trial-client", "konsole", "-e", str(self.fake),
            *herdr_args(),
        ]])
        self.assertEqual(self.started_units(), {"systemd_units": ["herdr-trial-client"]})

    def test_client_open_adds_unit_once_and_preserves_existing_units(self) -> None:
        self.scratch.mkdir()
        _ = (self.scratch / "started.json").write_text(json.dumps({
            "systemd_units": ["herdr-trial-server", "herdr-trial-client"],
        }))
        self.assert_success(self.invoke("client", "open"))
        self.assertEqual(self.started_units(), {
            "systemd_units": ["herdr-trial-server", "herdr-trial-client"],
        })

    def test_client_close_stops_only_client_unit(self) -> None:
        self.assert_success(self.invoke("client", "close"))
        self.assertEqual(self.calls(), [["--user", "stop", "herdr-trial-client"]])

    def test_issue_appends_utc_records_preserving_arguments(self) -> None:
        before = datetime.now(timezone.utc)
        self.assert_success(self.invoke("issue", "smoke", "one line\nnext", "quote: ' café"))
        self.assert_success(self.invoke("issue", "probe", "trusted", "blocked"))
        after = datetime.now(timezone.utc)
        lines = (self.scratch / "issues.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        records = [cast(IssueRecord, json.loads(line)) for line in lines]
        for record in records:
            self.assertEqual(set(record), {"time", "session", "expected", "happened"})
            instant = datetime.fromisoformat(record["time"].replace("Z", "+00:00"))
            self.assertEqual(instant.utcoffset(), timedelta(0))
            self.assertGreaterEqual(instant, before - timedelta(seconds=1))
            self.assertLessEqual(instant, after)
        self.assertEqual(records[0]["session"], "smoke")
        self.assertEqual(records[0]["expected"], "one line\nnext")
        self.assertEqual(records[0]["happened"], "quote: ' café")
        self.assertEqual(records[1]["session"], "probe")
        self.assertEqual(records[1]["expected"], "trusted")
        self.assertEqual(records[1]["happened"], "blocked")
        self.assertEqual(self.calls(), [])

    def test_trust_check_accepts_directory_itself_and_reads_only(self) -> None:
        self.trust_configs({str(self.project): True}, {str(self.project): "trusted"})
        paths = [self.claude / ".claude.json", self.codex / "config.toml"]
        before = [path.read_bytes() for path in paths]
        self.assert_success(self.invoke("trust-check", str(self.project)))
        self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertFalse(self.scratch.exists())
        self.assertEqual(self.calls(), [])

    def test_trust_check_accepts_parents_independently(self) -> None:
        self.trust_configs({str(self.project.parent): True}, {str(self.root): "trusted"})
        self.assert_success(self.invoke("trust-check", str(self.project)))
        self.assertEqual(self.calls(), [])

    def test_trust_check_requires_both_accounts(self) -> None:
        cases: tuple[tuple[dict[str, bool], dict[str, str], str], ...] = (
            ({str(self.project): False}, {str(self.project): "trusted"}, "Claude"),
            ({str(self.project): True}, {str(self.project): "untrusted"}, "Codex"),
            ({}, {}, ""),
        )
        for claude_projects, codex_projects, name in cases:
            with self.subTest(missing=name):
                self.trust_configs(claude_projects, codex_projects)
                result = self.invoke("trust-check", str(self.project))
                self.assert_refusal(result, str(self.project))
                if name:
                    self.assertIn(name.casefold(), result.stderr.casefold())
        self.assertEqual(self.calls(), [])

    def test_trust_check_does_not_accept_a_sibling_with_same_prefix(self) -> None:
        sibling = self.project.parent.with_name(self.project.parent.name + "-other")
        self.trust_configs({str(sibling): True}, {str(sibling): "trusted"})
        self.assert_refusal(self.invoke("trust-check", str(self.project)), str(self.project))

    def test_trust_check_missing_files_is_a_named_refusal(self) -> None:
        self.assert_refusal(self.invoke("trust-check", str(self.project)), str(self.project))
        self.assertFalse((self.claude / ".claude.json").exists())
        self.assertFalse((self.codex / "config.toml").exists())
        self.assertEqual(self.calls(), [])

    def test_usage_errors_are_single_lines_and_have_no_effects(self) -> None:
        for arguments in ([], ["unknown"], ["server"], ["client", "bad"],
                          ["pane-id"], ["read"], ["type", "smoke"],
                          ["issue", "smoke", "expected"], ["trust-check"]):
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertEqual(result.code, 2, result.stderr)
                self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
                self.assertTrue(result.stderr.strip())
                self.assertEqual(self.calls(), [])
                self.assertFalse(self.scratch.exists())


if __name__ == "__main__":
    _ = unittest.main()
