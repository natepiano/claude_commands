"""Assignment scopes share the complete registry fixture's row-override API."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override

from scripts.lib import models_registry, test_models_registry as fixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class RegistryOverrideTests(unittest.TestCase):
    path: Path = Path()

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = root / "models.conf"

    def test_shell_assignments_preserve_parsed_values_without_executing_them(self) -> None:
        text = "[roles]\nrunner = quoted ' value $(exit 9) # comment\nbare\n[empty]\n"
        parsed = models_registry.parse_registry(text)
        result = subprocess.run([
            "bash", "-c", models_registry.shell_assignments(text)
            + 'printf "%s\\n" "${_models_table_sections[@]}" "${_models_table_rows[@]}"',
        ], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(parsed, {"roles": {"runner": "quoted ' value $(exit 9)", "bare": ""}, "empty": {}})
        self.assertEqual(result.stdout.splitlines(), [
            "roles", "roles", "roles", "empty", "", "runner = quoted ' value $(exit 9)", "bare", "",
        ])

    def test_plain_row_overrides_keep_other_default_assignments(self) -> None:
        text = fixture.registry_text({'assignments': {'direct': 'claude'}})
        self.assertIn('direct=claude\n', text)
        self.assertIn('design_roles=codex\n', text)

    def test_exact_assignment_rows_keep_the_complete_catalog_and_role_defaults(self) -> None:
        text = fixture.registry_text({'assignments': fixture.ExactRows({
            'direct': 'codex', 'cli': 'codex', 'removed': None})})
        self.assertEqual(text.partition('[assignments]\n')[2].partition('\n\n')[0],
                         'direct=codex\ncli=codex')
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
        ], capture_output=True, text=True, env={**os.environ, 'MODELS_CONFIG_PUBLISH': '0'}, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.path.read_text(), fixture.registry_text({
            'assignments': fixture.ExactRows({'direct': 'codex'})}))


if __name__ == '__main__':
    _ = unittest.main()
