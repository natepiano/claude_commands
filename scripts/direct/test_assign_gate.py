"""Check launch refusal before backend access and verify the actual parent route."""
from __future__ import annotations

from pathlib import Path
import os
import subprocess
import unittest
from unittest.mock import patch

from scripts.agents import codex_test_support as codex_fixture
from scripts.direct import assign_gate
from scripts.direct.assign_launcher_test_support import ASSIGN, AssignLauncherFixture
from scripts.production import staff
from scripts.production.staff_test_support import fixture_worker

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / 'scripts/lib/py'
setUpModule = codex_fixture.setUpModule


class AssignGateTests(unittest.TestCase):

    def test_direct_shell_launchers_refuse_before_argument_validation(self) -> None:
        for script in ('direct/review.sh',
                       'agents/agent_exec.sh'):
            with self.subTest(script=script):
                result = subprocess.run(['bash', str(ROOT / 'scripts' / script)],
                                        text=True, capture_output=True, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr.strip(), assign_gate.REFUSAL)
                self.assertEqual(result.stdout, '')

    def test_assign_parent_passes(self) -> None:
        result = subprocess.CompletedProcess([], 0, f'1 python3 {assign_gate.ASSIGN}\n', '')
        with patch.object(subprocess, 'run', return_value=result):
            self.assertTrue(assign_gate.called_by_assign())

    def test_apostrophe_in_launch_argument_passes_the_gate(self) -> None:
        result = subprocess.CompletedProcess([], 0, f"1 python3 {assign_gate.ASSIGN} --fix-cause it's the launch argument\n", '')
        with patch.object(subprocess, 'run', return_value=result):
            self.assertTrue(assign_gate.called_by_assign())

    def test_exec_hop_reaches_the_assign_parent(self) -> None:
        for launcher, interpreter in ((ROOT / 'scripts/agents/agent_exec.sh', 'bash'),):
            with self.subTest(launcher=launcher):
                replies = [subprocess.CompletedProcess([], 0, f'2 {interpreter} {launcher}\n', ''),
                           subprocess.CompletedProcess([], 0, f'1 python3 {assign_gate.ASSIGN}\n', '')]
                with patch.object(subprocess, 'run', side_effect=replies):
                    self.assertTrue(assign_gate.called_by_assign())

    def test_worker_between_assign_and_caller_refuses(self) -> None:
        result = subprocess.CompletedProcess([], 0, '1 codex exec worker\n', '')
        with patch.object(subprocess, 'run', return_value=result):
            self.assertFalse(assign_gate.called_by_assign())


class AssignmentFormsTests(AssignLauncherFixture):




    def test_managed_astra_review_requires_a_name_a_review_role_and_a_run(self) -> None:
        required = ["--role", "review", "--operation", "astra-review", "--new-worker", "astra-reviewer",
                    "--session-dir", str(self.session_dir), "--prompt", str(self.prompt)]
        for option, reason in (("--new-worker", "astra-review requires --new-worker"),
                               ("--role", "astra-review requires --role review"),
                               ("--session-dir", "astra-review requires --session-dir")):
            with self.subTest(option=option):
                arguments = required.copy()
                index = arguments.index(option)
                if option == "--role":
                    arguments[index + 1] = "implement"
                else:
                    del arguments[index:index + 2]
                result = self.run_launcher(["bash", str(ASSIGN), *arguments])
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(reason, result.stderr)
                self.assertEqual(self.native.fixture.world.launches, [])

    def test_removed_transport_routes_refuse(self) -> None:
        for route in ('--transport', '--legacy'):
            with self.subTest(route=route):
                result = self.run_launcher(['bash', str(ASSIGN), route, 'start'])
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(self.native.fixture.world.launches, [])

    def test_refused_admission_starts_no_worker(self) -> None:
        row = fixture_worker("not-a-director", self.native.fixture.parent.id,
                             backend=staff.CodexBackend(self.native.fixture.root / "codex"))
        with patch.dict(os.environ, {"CODEX_THREAD_ID": row.current_session, "CLAUDE_CODE_SESSION_ID": ""}):
            result = self.launch("review", "--new-worker", "consultation")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("caller is not a unit or showrunner", (self.session_dir / "phases/7/consultation_agent_1.log").read_text())
        self.assertEqual(self.native.fixture.world.launches, [])


if __name__ == '__main__':
    _ = unittest.main()
