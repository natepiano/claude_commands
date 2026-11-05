"""Check launch refusal before backend access and verify the actual parent route."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from scripts.agents import test_codex_mesh as mesh_fixture
from scripts.astra import design_roles
from scripts.direct import assign, assign_gate
from scripts.direct.assign_launcher_test_support import ASSIGN, AssignLauncherFixture, read_object
from scripts.lib.test_models_registry import write_registry
from scripts.usage import agents_log
from scripts.production import staff
from scripts.production.staff_test_support import fixture_runtime_id, fixture_session, fixture_worker

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / 'scripts/lib/py'
setUpModule = mesh_fixture.setUpModule


class AssignGateTests(unittest.TestCase):
    def test_direct_mesh_verbs_refuse_before_argument_validation(self) -> None:
        for verb in ('start', 'follow', 'reopen', 'fork', 'adopt', 'prime', 'resume-account-move'):
            for command in ([str(PYTHON), str(ROOT / 'scripts/agents/codex_mesh.py'), verb],
                            [str(PYTHON), '-c',
                             f'from scripts.agents import codex_mesh; codex_mesh.main([{verb!r}])']):
                with self.subTest(verb=verb, command=command):
                    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stderr.strip(), assign_gate.REFUSAL)
                    self.assertEqual(result.stdout, '')

    def test_direct_shell_launchers_refuse_before_argument_validation(self) -> None:
        for script in ('direct/implement.sh', 'direct/review.sh', 'agents/agent_bg.sh',
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

    def test_worker_fill_and_exec_hops_reach_the_assign_parent(self) -> None:
        for launcher, interpreter in ((ROOT / 'scripts/agents/agent_exec.sh', 'bash'),
                                      (ROOT / 'scripts/production/worker_fill.py', 'python3')):
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
    def capture_operation(self, handler: str) -> Path:
        """Observe a real process after its door and before backend work."""
        trace = self.session_dir.parent / 'operation.json'
        checks = self.session_dir.parent / 'checks.txt'
        expected_checks = ['admit'] if handler in {'command_adopt', 'command_resume_account_move'} else ['check-launch', 'admit']
        self.instrument_launcher('\n'.join((
            'from pathlib import Path',
            'from scripts.direct import assign',
            'from scripts.astra import design_roles',
            'from scripts.production import staff',
            'original_call = assign.call',
            'def call(command, env, **options):',
            '    result = original_call(command, env, **options)',
            '    if "check-launch" in command and result.returncode == 0:',
            f'        with Path({str(checks)!r}).open("a") as output: output.write("check-launch\\n")',
            '    return result',
            'assign.call = call',
            'original_admit = assign.admit_assignment',
            'def admit(*arguments):',
            '    result = original_admit(*arguments)',
            f'    with Path({str(checks)!r}).open("a") as output: output.write("admit\\n")',
            '    return result',
            'assign.admit_assignment = admit',
        )))
        startup = self.session_dir.parent / 'instrument/sitecustomize.py'
        with startup.open('a') as output:
            _ = output.write('\n' + '\n'.join((
                'from pathlib import Path',
                f'if Path(sys.argv[0]).resolve() == Path({str(ROOT / "scripts/agents/codex_mesh.py")!r}):',
                '    def observe(frame, event, argument):',
                f'        if event == "call" and frame.f_code.co_name == {handler!r}:',
                '            sys.setprofile(None)',
                '            import json',
                '            from pathlib import Path',
                '            from scripts.direct import assign_gate',
                '            assert assign_gate.called_by_assign()',
                f'            assert Path({str(checks)!r}).read_text().splitlines() == {expected_checks!r}',
                f'            Path({str(trace)!r}).write_text(json.dumps({{key: str(value) for key, value in vars(frame.f_locals["args"]).items()}}))',
                '            raise SystemExit(0)',
                '    sys.setprofile(observe)',
            )) + '\n')
        return trace

    def held_form(self, operation: str, held_role: str = 'designer', *, claim: str = 'creation-token') -> list[str]:
        design_id = fixture_runtime_id('assign-held-design')
        name = 'held-designer' if held_role == 'designer' else 'held-reviewer'
        owner = staff.by_current_session(os.environ['CODEX_THREAD_ID'])
        assert owner is not None
        worker = fixture_worker(name, owner.id, runtime_id=mesh_fixture.THREAD_ID) if operation == 'follow' else None
        lifecycle = ({'kind': 'RoleNotOpened', 'claim': claim} if worker is None
                     else {'kind': 'RoleOpen', 'staff_id': worker.id})
        directory = design_roles.design_directory(design_id)
        directory.mkdir(parents=True)
        fields = {
            'schema': 1, 'placement': {'design_id': design_id, 'host': design_roles.local_host(), 'generation': 1},
            'repository_id': 'fixture-repository', 'repository': str(self.project),
            'document': str(self.project / 'design.md'), 'area': 'assignment', 'created_at_utc': '2026-11-04T00:00:00Z',
            'binding': {'kind': 'DesignBoundToPhase', 'run_directory': str(self.session_dir), 'phase': '7',
                        'repository': str(self.project), 'output_paths': ['src/worker.py']},
            'roles': [{'role': role, 'name': 'held-' + role,
                       'lifecycle': lifecycle if role == held_role else {'kind': 'RoleNotOpened', 'claim': ''}}
                      for role in ('designer', 'reviewer')],
        }
        _ = (directory / 'design.json').write_text(json.dumps(fields))
        mesh = design_roles.mesh_directory(design_id)
        mesh.mkdir()
        _ = (mesh / 'progress_history_state.json').write_text((self.session_dir / 'progress_history_state.json').read_text())
        if operation == 'follow':
            assert worker is not None
            _ = (mesh / 'mesh_roster.json').write_text(json.dumps({name: {'staff_id': worker.id}}))
        return ['--role', 'implement' if held_role == 'designer' else 'review', '--operation', operation,
                '--session-dir', str(mesh), '--phase', '7', '--cwd', str(self.project), '--prompt', str(self.prompt),
                '--new-worker' if operation == 'prime' else '--to', name,
                '--held-design', design_id, '--held-role', held_role]

    def test_held_prime_passes_identity_claim_and_policy_through_the_door(self) -> None:
        trace = self.capture_operation('command_prime')
        arguments = self.held_form('prime')
        result = self.run_launcher(['bash', str(ASSIGN), *arguments])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        operation = read_object(trace)
        self.assertEqual(operation['held_role'], 'designer')
        self.assertEqual(operation['sandbox'], 'danger-full-access')
        design = design_roles.read_design(str(operation['held_design']))
        self.assertEqual(design.roles[0].lifecycle, design_roles.RoleNotOpened('creation-token'))
        args = assign.Arguments()
        args.role, args.operation = 'implement', assign.AssignmentOperation.PRIME
        args.held_design, args.held_role = design.placement.design_id, 'designer'
        args.new_worker, args.cwd, args.session_dir = 'held-designer', str(self.project), str(operation['session_dir'])
        held = assign.held_assignment(args)
        self.assertIsInstance(held, assign.HeldAssignment)
        assert isinstance(held, assign.HeldAssignment)
        self.assertEqual(held.claim, assign.ReservedHeldCreation('creation-token'))
        self.assertIsInstance(held.access, design_roles.PhaseBoundDesigner)

    def test_held_follow_keeps_the_read_only_policy(self) -> None:
        trace = self.capture_operation('command_follow')
        arguments = self.held_form('follow', 'reviewer')
        result = self.run_launcher(['bash', str(ASSIGN), *arguments])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        operation = read_object(trace)
        self.assertEqual(operation['to'], 'held-reviewer')
        self.assertEqual(operation['read_only'], 'True')

    def test_held_prime_without_its_creation_claim_starts_nothing(self) -> None:
        trace = self.capture_operation('command_prime')
        result = self.run_launcher(['bash', str(ASSIGN), *self.held_form('prime', claim='')])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('reserved creation claim', result.stderr)
        self.assertFalse(trace.exists())

    def test_resident_consultation_reaches_start_through_the_door(self) -> None:
        trace = self.capture_operation('command_start')
        _ = write_registry(self.registry, {'ask_a_friend.codex': {'consultation': 'gpt-kept-sol:high'}}, update=True)
        result = self.launch('review', '--operation', 'resident', '--new-worker', 'consultation', '--family', 'codex')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        operation = read_object(trace)
        self.assertEqual(operation['resident'], 'True')
        self.assertEqual(operation['task'], 'ask_a_friend.consultation')

    def recovery_record(self) -> dict[str, object]:
        owner = staff.by_current_session(os.environ['CODEX_THREAD_ID'])
        assert owner is not None
        worker = fixture_worker('existing', owner.id, runtime_id=mesh_fixture.THREAD_ID,
                                backend=staff.CodexBackend(Path(os.environ['CODEX_HOME'])))
        agents_log.append(worker.current_session, 'review', None, datetime.now(timezone.utc))
        return {'staff_id': worker.id, 'instructions_file': str(self.prompt), 'cwd': str(self.project)}

    def test_recovery_operations_reach_the_door_with_the_existing_identity(self) -> None:
        for operation, handler in (('adopt', 'command_adopt'), ('resume-account-move', 'command_resume_account_move')):
            with self.subTest(operation=operation):
                trace = self.capture_operation(handler)
                source = self.session_dir.parent / (operation + '-source')
                source.mkdir()
                record = self.recovery_record()
                _ = (source / 'mesh_roster.json').write_text(json.dumps({'existing': record}))
                options = ['--from-session-dir', str(source)] if operation == 'adopt' else []
                if operation != 'adopt':
                    _ = (self.session_dir / 'mesh_roster.json').write_text(json.dumps({'existing': record}))
                result = self.launch('review', '--operation', operation, '--to', 'existing', *options)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                arguments = read_object(trace)
                self.assertEqual(arguments['name' if operation == 'adopt' else 'to'], 'existing')
                trace.unlink()
                (self.session_dir.parent / 'checks.txt').unlink()

    def test_recovery_uses_the_recorded_context_without_extra_launch_arguments(self) -> None:
        trace = self.capture_operation('command_resume_account_move')
        record = self.recovery_record()
        worker = staff.by_id(staff.StaffId(str(record['staff_id'])))
        assert worker is not None
        current = staff.native_id(fixture_runtime_id('recovery-cleared-session'))
        fixture_session(current, staff.CodexBackend(Path(os.environ['CODEX_HOME'])))
        _ = staff.update_current_session(worker.current_session, current)
        state = read_object(self.session_dir / 'progress_history_state.json')
        state['pass'] = {'review': {'worker_name': 'existing', 'kind': 'review', 'started_at': 1}}
        _ = (self.session_dir / 'progress_history_state.json').write_text(json.dumps(state))
        _ = (self.session_dir / 'mesh_roster.json').write_text(json.dumps({'existing': record}))
        result = self.run_launcher(['bash', str(ASSIGN), '--role', 'review', '--operation', 'resume-account-move',
                                    '--session-dir', str(self.session_dir), '--to', 'existing'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(read_object(trace)['to'], 'existing')

    def test_closed_phase_refuses_before_a_resident_worker_starts(self) -> None:
        trace = self.capture_operation('command_start')
        result = self.launch('review', '--operation', 'resident', '--new-worker', 'consultation', '--phase', 'closed')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('not open', result.stderr)
        self.assertFalse(trace.exists())

    def test_removed_transport_routes_refuse(self) -> None:
        for route in ('--mesh', '--legacy'):
            with self.subTest(route=route):
                result = self.run_launcher(['bash', str(ASSIGN), route, 'start'])
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(self.methods('thread/start'), [])

    def test_refused_admission_starts_no_resident_worker(self) -> None:
        trace = self.capture_operation('command_start')
        _ = write_registry(self.registry, {'ask_a_friend.codex': {'consultation': 'gpt-kept-sol:high'}}, update=True)
        with (self.session_dir.parent / 'instrument/injection.py').open('a') as injection:
            _ = injection.write('\nfrom scripts.production import worker_admission\n' +
                                'def refuse(*args, **kwargs): raise worker_admission.AdmissionRefused("fixture admission refusal")\n' +
                                'assign.worker_admission.admit = refuse\n')
        result = self.launch('review', '--operation', 'resident', '--new-worker', 'consultation')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('fixture admission refusal', result.stderr)
        self.assertFalse(trace.exists())


if __name__ == '__main__':
    _ = unittest.main()
