"""Cache rewrites are unreadable snapshots, not evidence of a missing model."""
from __future__ import annotations

from scripts.lib.test_hang_guard import LauncherHangGuard

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import final, override
from unittest.mock import Mock, patch

from scripts.lib.codex_models_cache import ModelsCacheUnreadable, read_models_cache
from scripts.lib.test_agents_registry import ExactRows, write_registry


ROOT = Path(__file__).resolve().parents[2]
COMPLETE = '{"models":[{"slug":"gpt-6.1-sol","visibility":"list"}]}'


@final
class ModelsCacheTests(unittest.TestCase):
    root: Path = Path()
    cache: Path = Path()

    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / "models_cache.json"

    def test_a_partial_write_resolves_after_the_retry_reads_a_complete_file(self) -> None:
        _ = self.cache.write_text('{"models":[')
        pauses: list[float] = []

        def finish_write(delay: float) -> None:
            pauses.append(delay)
            _ = self.cache.write_text(COMPLETE)

        snapshot = read_models_cache(self.cache, retry_delays=(0.01,), pause=finish_write)

        self.assertEqual(snapshot, json.loads(COMPLETE))
        self.assertEqual(pauses, [0.01])
        self.assertEqual(self.resolve().stdout, "gpt-6.1-sol")

    def test_missing_empty_malformed_and_invalid_snapshots_have_a_bounded_retry(self) -> None:
        for text in (None, "", "{broken", "null", "{}", '{"models":null}'):
            with self.subTest(text=text):
                if text is not None:
                    _ = self.cache.write_text(text)
                pause = Mock()
                with patch.object(Path, "read_text", wraps=self.cache.read_text) as reads:
                    with self.assertRaisesRegex(ModelsCacheUnreadable, "the models cache could not be read") as raised:
                        _ = read_models_cache(self.cache, retry_delays=(0.01, 0.01), pause=pause)
                self.assertIn(str(self.cache), str(raised.exception))
                self.assertNotIn("no Codex model", str(raised.exception))
                self.assertEqual(reads.call_count, 3)
                self.assertEqual(pause.call_count, 2)

    def test_a_complete_cache_without_the_model_does_not_retry(self) -> None:
        _ = self.cache.write_text('{"models":[]}')
        pause = Mock()
        self.assertEqual(read_models_cache(self.cache, pause=pause), {"models": []})
        pause.assert_not_called()
        result = self.resolve()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no Codex model for 'sol'", result.stderr)
        self.assertNotIn("could not be read", result.stderr)

    def test_a_persistently_broken_cache_reports_a_read_error_to_the_shell(self) -> None:
        _ = self.cache.write_text('{"models":[')
        result = self.resolve()
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"the models cache could not be read: {self.cache}", result.stderr)
        self.assertNotIn("no Codex model", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_sync_uses_one_snapshot_when_the_live_file_changes_between_queries(self) -> None:
        _ = self.cache.write_text(COMPLETE)
        configuration = self.root / "config.toml"
        _ = configuration.write_text('model = "gpt-6.1-sol"\n')
        real_jq = shutil.which("jq")
        self.assertIsNotNone(real_jq)
        bin_directory = self.root / "bin"
        bin_directory.mkdir()
        jq = bin_directory / "jq"
        _ = jq.write_text(
            "#!/usr/bin/env python3\nimport os, sys\nfrom pathlib import Path\n" +
            f"_ = Path({str(self.cache)!r}).write_text('{{broken')\n" +
            f"os.execv({real_jq!r}, [{real_jq!r}, *sys.argv[1:]])\n")
        jq.chmod(0o755)
        environment = {**self.environment(), "PATH": str(bin_directory) + os.pathsep + os.environ["PATH"],
                       "CODEX_CONFIG_FILE": str(configuration),
                       "CODEX_CATALOG_SYNC_STATE_FILE": str(self.root / "last-success")}
        result = subprocess.run(["bash", str(ROOT / "scripts/agents/sync_codex_catalog.sh")],
                                env=environment, text=True, capture_output=True, check=False, timeout=LauncherHangGuard(self.id()).remaining())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.cache.read_text(), "{broken")

    def environment(self) -> dict[str, str]:
        registry = write_registry(self.root / "agents.conf", {
            "assignments": ExactRows({"direct": "codex"}),
            "direct.codex": {"impl": "sol:high"},
            "codex.agents": {"gpt-6.1-sol": "high"},
            "codex.families": {"sol": "gpt-6.1-sol"},
        })
        return {**os.environ, "AGENTS_CONFIG_FILE": str(registry),
                       "AGENTS_CONFIG_TIER_ONLY": "1", "AGENTS_CONFIG_PUBLISH": "0",
                "CODEX_MODELS_CACHE_FILE": str(self.cache)}

    def resolve(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", "-c", 'source "$1"; _agents_model_slug codex sol',
                               "cache-test", str(ROOT / "scripts/agents/agents_config.sh")],
                              env=self.environment(), capture_output=True, text=True, check=False, timeout=LauncherHangGuard(self.id()).remaining())


if __name__ == "__main__":
    _ = unittest.main()
