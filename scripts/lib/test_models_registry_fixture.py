"""Assignment scopes share the complete registry fixture's row-override API."""
from __future__ import annotations

import io
import os
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from typing import override
from collections.abc import Generator
from contextlib import contextmanager, redirect_stderr

from scripts.lib import models_registry, test_models_registry as fixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class RegistryOverrideTests(unittest.TestCase):
    path: Path = Path()

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = root / "models.conf"
        self.enterContext(patch.dict(os.environ, {
            "HOME": str(root), "MODELS_CONFIG_FILE": str(self.path),
            "MODELS_LAUNCH_CONFIGURATION": "", "MODELS_REGISTRY_EDIT_BASE": "",
            "MODELS_CONFIG_TIER_ONLY": "1",
        }))

    def test_shell_assignments_preserve_parsed_values_without_executing_them(self) -> None:
        text = "[roles]\nrunner = quoted ' value $(exit 9) # comment\nbare\n[empty]\n"
        parsed = models_registry.parse_registry(text)
        _ = self.path.write_text(text)
        result = subprocess.run([
            "bash", "-c", models_registry.configuration_shell(models_registry.edit_base(os.environ))
            + 'printf "%s\\n" "${_models_table_sections[@]}" "${_models_table_keys[@]}" '
            + '"${_models_table_values[@]}"',
        ], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(parsed, {"roles": {"runner": "quoted ' value $(exit 9)", "bare": ""}, "empty": {}})
        self.assertEqual(result.stdout.splitlines(), [
            "roles", "roles", "roles", "empty", "", "runner", "bare", "",
            "", "quoted ' value $(exit 9)", "", "",
        ])

    def _catalog_sync_environment(self, body: str) -> dict[str, str]:
        root = self.path.parent
        script = root / "scripts/agents/sync_codex_catalog.sh"
        script.parent.mkdir(parents=True)
        _ = script.write_text(body)
        script.chmod(0o755)
        paths = {"CODEX_CONFIG_FILE": root / "codex.toml",
                 "CODEX_MODELS_CACHE_FILE": root / "models.json",
                 "CODEX_CATALOG_SYNC_STATE_FILE": root / "sync-state"}
        for key, path in paths.items():
            _ = path.write_text("fixture")
            os.utime(path, (1, 1) if key == "CODEX_CATALOG_SYNC_STATE_FILE" else (2, 2))
        return {**{key: str(path) for key, path in paths.items()}, "MODELS_CONFIG_TIER_ONLY": "0"}

    @contextmanager
    def _catalog_sync_scope(self, environment: dict[str, str]) -> Generator[None]:
        library = self.path.parent / "scripts/lib/models_registry.py"
        with patch.object(models_registry, "__file__", str(library)), patch.dict(os.environ, environment):
            yield

    def test_fresh_operations_sync_before_reading_and_inherited_or_tier_only_operations_skip_sync(self) -> None:
        root = self.path.parent
        log = root / "sync-log"
        sync_environment = self._catalog_sync_environment("#!" + sys.executable + "\n" + '\n'.join([
            "import os", "from pathlib import Path",
            "path = Path(os.environ['MODELS_CONFIG_FILE'])",
            f"with Path({str(log)!r}).open('a') as output: output.write(str(path) + '\\n')",
            "path.write_text(path.read_text().replace('source=before', 'source=synced'))",
            "Path(os.environ['CODEX_CATALOG_SYNC_STATE_FILE']).touch()",
        ]) + "\n")
        state = Path(sync_environment["CODEX_CATALOG_SYNC_STATE_FILE"])
        config = Path(sync_environment["CODEX_CONFIG_FILE"])
        cache = Path(sync_environment["CODEX_MODELS_CACHE_FILE"])
        original = "[catalog]\nsource=before\n"
        for factory in (models_registry.launch_configuration, models_registry.edit_base):
            for mode in ("missing-state", "new-config", "new-cache", "current-state", "inherited", "tier-only"):
                with self.subTest(factory=factory.__name__, mode=mode):
                    _ = self.path.write_text(original)
                    _ = log.write_text("")
                    state.touch()
                    os.utime(state, (20, 20))
                    os.utime(config, (10, 10))
                    os.utime(cache, (10, 10))
                    if mode == "missing-state":
                        state.unlink()
                    elif mode != "current-state":
                        os.utime(cache if mode == "new-cache" else config, (30, 30))
                    inherited = (models_registry.LaunchModelConfiguration(self.path, original, ())
                                 if factory is models_registry.launch_configuration
                                 else models_registry.RegistryEditBase(self.path, original, ()))
                    environment = inherited.environment({}) if mode == "inherited" else {}
                    synchronize = mode not in ("inherited", "tier-only", "current-state")
                    with self._catalog_sync_scope({**sync_environment,
                        "MODELS_CONFIG_TIER_ONLY": "1" if mode == "tier-only" else "0",
                    }):
                        configuration = factory(environment)
                    self.assertEqual(log.read_text().splitlines(), [str(self.path)] if synchronize else [])
                    self.assertEqual(configuration.path, self.path)
                    self.assertEqual(configuration.text, original.replace("before", "synced") if synchronize else original)
                    self.assertEqual(next(row.value for row in configuration.rows if row.key == "source"),
                                     "synced" if synchronize else "before")

    def test_failed_catalog_sync_warns_and_returns_the_current_configuration(self) -> None:
        environment = self._catalog_sync_environment("#!/bin/sh\nexit 9\n")
        original = "# Keep the current configuration.\n[assignments]\ndirect=codex\n"
        _ = self.path.write_text(original)
        for factory in (models_registry.launch_configuration, models_registry.edit_base):
            with self.subTest(factory=factory.__name__), \
                    self._catalog_sync_scope(environment), redirect_stderr(io.StringIO()) as errors:
                configuration = factory({})
            self.assertEqual(errors.getvalue(),
                             "WARNING: Codex catalog sync failed; using the current model registry.\n")
            self.assertEqual(configuration.path, self.path)
            self.assertEqual(configuration.text, original)

    def test_shell_operations_use_unexported_registry_paths_and_explicit_paths_take_precedence(self) -> None:
        selected = fixture.write_registry(self.path, {"assignments": {"direct": "claude"}})
        _ = fixture.write_registry(self.path.parent / ".claude/config/models.conf", {"assignments": {"direct": "codex"}})
        other = fixture.write_registry(self.path.parent / "other.conf", {"assignments": {"direct": "codex"}})
        source = Path(models_registry.__file__).parents[1] / "agents/models_config.sh"
        for operation, expected in (("models_launch_configuration; emit", selected),
                                    ("models_registry_edit emit", selected),
                                    ('_models_registry_parse --registry "$3"; emit', other),
                                    ('_models_registry_parse --registry="$3"; emit', other)):
            with self.subTest(operation=operation):
                result = subprocess.run(["bash", "-c", 'source "$1"; export -n MODELS_CONFIG_FILE; '
                    + 'MODELS_CONFIG_FILE="$2"; emit() { printf "%s\\n" "$MODELS_CONFIG_FILE"; '
                    + '_models_table_section_values assignments; }; ' + operation,
                    "fixture", str(source), str(selected), str(other)],
                    env={**os.environ, "MODELS_CONFIG_FILE": str(other)},
                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines()[0], str(expected))
                self.assertIn("direct=" + ("claude" if expected == selected else "codex"), result.stdout.splitlines())

    def test_plain_row_overrides_keep_other_default_assignments(self) -> None:
        text = fixture.registry_text({'assignments': {'direct': 'claude'}})
        self.assertIn('direct=claude\n', text)
        self.assertIn('design_roles=codex\n', text)

    def test_path_resolution_uses_the_default_or_the_current_override(self) -> None:
        default = self.path.parent / "default registry.conf"
        for configured, expected in (("", default), (str(self.path), self.path),
                                     (str(self.path.parent / "later.conf"), self.path.parent / "later.conf")):
            with self.subTest(configured=configured), patch.object(models_registry, "DEFAULT_PATH", default), \
                    patch.dict(os.environ, {"MODELS_CONFIG_FILE": configured}):
                self.assertEqual(models_registry.registry_path(), expected)

    def test_shell_cli_resolves_its_path_without_reading_stdin(self) -> None:
        text = "# Keep this comment.\n[assignments]\ndirect = codex # active family\n[empty]\n"
        for override in (False, True):
            with self.subTest(override=override):
                selected = self.path if override else self.path.parent / ".claude/config/models.conf"
                selected.parent.mkdir(parents=True, exist_ok=True)
                _ = selected.write_text(text)
                environment = {**os.environ, "HOME": str(self.path.parent),
                               "MODELS_CONFIG_FILE": str(selected) if override else ""}
                result = subprocess.run(
                    ["python3", str(Path(models_registry.__file__)), "shell"], input="not a registry\n",
                    env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stderr)
                shell = subprocess.run(["bash", "-c", result.stdout +
                                        'printf "%s\\0" "$MODELS_CONFIG_FILE" "$MODELS_REGISTRY_BASE" '
                                        + '"${_models_table_sections[@]}" "${_models_table_keys[@]}" '
                                        + '"${_models_table_values[@]}"'],
                                       capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(shell.returncode, 0, shell.stderr)
                self.assertEqual(shell.stdout.split("\0")[:-1],
                                 [str(selected), text, "assignments", "assignments", "empty",
                                  "", "direct", "", "", "codex", ""])

    def test_python_and_bash_share_the_same_spaced_assignment_and_launch_generation(self) -> None:
        original = fixture.registry_text().replace("direct=codex", "direct = codex # chosen family")
        _ = self.path.write_text(original)
        configuration = models_registry.launch_configuration(os.environ)
        _ = self.path.write_text(original.replace("direct = codex", "direct = claude"))
        environment = configuration.environment(os.environ)
        bash = subprocess.run(["bash", "-c", 'source "$1"; models_launch_configuration; '
                               + 'agents_resolve direct.impl; printf "%s\\n" "$AGENT_FAMILY" "$AGENT_MODEL" "$AGENT_EFFORT"',
                               "fixture", str(Path(models_registry.__file__).parents[1] / "agents/models_config.sh")],
                              env=environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(bash.returncode, 0, bash.stderr)
        assignment = next(row.value for row in configuration.rows if row.section == "assignments" and row.key == "direct")
        self.assertEqual(bash.stdout.splitlines(), [assignment, "gpt-test", "high"])
        self.assertEqual(configuration.text, original)

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
