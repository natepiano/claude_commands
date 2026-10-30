"""Assignment scopes share the complete registry fixture's row-override API."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override

from scripts.lib import test_agents_registry as fixture


class RegistryOverrideTests(unittest.TestCase):
    path: Path = Path()

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = root / "agents.conf"

    def test_plain_row_overrides_keep_other_default_assignments(self) -> None:
        text = fixture.registry_text({'assignments': {'direct': 'claude'}})
        self.assertIn('direct=claude\n', text)
        self.assertIn('team_review=codex\n', text)

    def test_exact_assignment_rows_keep_the_complete_catalog_and_role_defaults(self) -> None:
        text = fixture.registry_text({'assignments': fixture.ExactRows({
            'direct': 'codex', 'module_review': 'codex', 'removed': None})})
        self.assertEqual(text.partition('[assignments]\n')[2].partition('\n\n')[0],
                         'direct=codex\nmodule_review=codex')
        self.assertIn('[direct.codex]\nimpl=gpt-test:high\n', text)
        self.assertIn('[codex.agents]\n', text)
        self.assertIn('[claude.agents]\n', text)

    def test_exact_empty_assignments_remove_the_section(self) -> None:
        text = fixture.registry_text({'assignments': fixture.ExactRows({})})
        self.assertNotIn('[assignments]', text)
        self.assertIn('[direct.codex]', text)

    def test_exact_role_rows_do_not_inherit_other_default_tasks(self) -> None:
        text = fixture.registry_text({'direct.codex': fixture.ExactRows({'impl': 'gpt-test:high'})})
        self.assertEqual(text.partition('[direct.codex]\n')[2].partition('\n\n')[0],
                         'impl=gpt-test:high')
        self.assertIn('[direct.claude]\n', text)

    def test_updating_a_fixture_keeps_its_explicit_assignment_scope(self) -> None:
        _ = fixture.write_registry(self.path, {'assignments': fixture.ExactRows({'direct': 'codex'})})
        _ = fixture.write_registry(self.path, {'assignments': {'direct': 'claude'}}, update=True)
        self.assertEqual(self.path.read_text().partition('[assignments]\n')[2].partition('\n\n')[0],
                         'direct=claude')

    def test_shell_replacement_uses_the_same_exact_section_semantics(self) -> None:
        result = subprocess.run([
            'bash', '-c', 'source "$1"; write_test_registry "$2" --replace-section assignments assignments/direct=codex',
            'fixture', str(Path(fixture.__file__).with_suffix('.sh')), str(self.path),
        ], capture_output=True, text=True, env={**os.environ, 'AGENTS_CONFIG_PUBLISH': '0'}, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.path.read_text(), fixture.registry_text({
            'assignments': fixture.ExactRows({'direct': 'codex'})}))


if __name__ == '__main__':
    _ = unittest.main()
