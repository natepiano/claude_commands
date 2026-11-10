"""Print sessions cannot replace the director's retained pane launch."""

from __future__ import annotations

from collections.abc import Callable
import io
import json
import os
from pathlib import Path
import runpy
from typing import cast
from unittest import mock

from scripts.hooks import staff_session_identity
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_runtime_id
from scripts.agents import pane_record


class SessionStartPaneRecordTests(StaffTestCase):
    def record_start(self, arguments: list[str], session_id: str) -> None:
        namespace = runpy.run_path(str(Path(__file__).with_name('session-start-pane-record.py')))
        main = cast(Callable[[], None], namespace['main'])
        payload = {'session_id': session_id, 'cwd': str(self.root), 'source': 'startup'}
        with (mock.patch.dict(os.environ, {
                  'HERDR_ENV': '1', 'HERDR_PANE_ID': 'w16:p3', 'HERDR_SESSION': 'agents',
                  'CLAUDE_CONFIG_DIR': str(self.root / 'account'),
              }),
              mock.patch('sys.stdin', io.StringIO(json.dumps(payload))),
              mock.patch.object(pane_record, 'claude_arguments', return_value=arguments),
              mock.patch.object(os, 'getppid', return_value=1),
              mock.patch.object(staff, 'runtime_id', return_value='')):
            main()

    def test_print_launch_keeps_the_existing_director_record_unchanged(self) -> None:
        _, director, _ = self.tree()
        pane_record.write('agents', pane_record.launch(
            'w16:p3', director.id, str(self.root), {}, ['claude', '--model', 'sonnet']))
        path = pane_record.record_path('agents')
        before = path.read_bytes(), path.stat().st_mtime_ns
        for flag in ('-p', '--print'):
            with self.subTest(flag=flag):
                self.record_start(['claude', flag, 'Review the change'], fixture_runtime_id('print-child'))
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_failed_argument_read_keeps_the_recorded_plugin_flags(self) -> None:
        identity = fixture_runtime_id("postmaster")
        entry = pane_record.launch("w16:p3", identity, str(self.root), {},
                                   ["claude", "--plugin-dir", "../postmaster"])
        pane_record.write("agents", entry)
        path = pane_record.record_path("agents")
        before = path.read_bytes(), path.stat().st_mtime_ns
        output = io.StringIO()
        with mock.patch("sys.stderr", output):
            self.record_start([], identity)
        self.assertEqual(output.getvalue(), "cannot read Claude arguments; retained launch stays unchanged\n")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(pane_record.read_strict("agents")["w16:p3"].get("plugin_directories"),
                         [str((self.root / "../postmaster").resolve())])

    def test_interactive_launch_records_the_new_session_after_identity_handling(self) -> None:
        _, director, _ = self.tree()
        pane_record.write('agents', pane_record.launch('w16:p3', director.id, str(self.root), {}, ['claude']))
        session_id = fixture_runtime_id('interactive')
        with mock.patch.object(staff_session_identity, 'handle', wraps=staff_session_identity.handle) as handle:
            self.record_start(['claude', '--model', 'sonnet'], session_id)
        handle.assert_called_once()
        entry = pane_record.read_strict('agents')['w16:p3']
        self.assertEqual(entry['session_id'], session_id)
        self.assertEqual(entry['cwd'], str(self.root))
        self.assertEqual(entry['model'], 'sonnet')
