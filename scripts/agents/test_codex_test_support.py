"""Check reuse of the private fixture's model defaults."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts.agents import codex_test_support
from scripts.agents.codex_test_support import FixtureModelDefaults
from scripts.lib import models_registry
from scripts.lib.test_models_registry import write_registry


def setUpModule() -> None:
    codex_test_support.setUpModule()


class FixtureModelDefaultsTests(unittest.TestCase):
    def test_only_unchanged_baseline_results_are_reused(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            registry = write_registry(Path(root) / "models.conf")
            with patch.dict(os.environ, {"MODELS_CONFIG_FILE": str(registry)}):
                resolve = MagicMock(return_value=("gpt-test", "high"))
                defaults = FixtureModelDefaults(resolve)
                configuration = models_registry.launch_configuration({})
                args = argparse.Namespace(role="impl", task="", model="gpt-test", effort="high")
                self.assertEqual(defaults(args, configuration), ("gpt-test", "high"))
                self.assertEqual(defaults(args, configuration), ("gpt-test", "high"))
                self.assertEqual(resolve.call_count, 1)
                registry = write_registry(registry, {"direct.codex": {"impl": "gpt-test:max"}}, update=True)
                resolve.return_value = ("gpt-test", "max")
                self.assertEqual(defaults(args, configuration), ("gpt-test", "max"))
                self.assertEqual(defaults(args, configuration), ("gpt-test", "max"))
                self.assertEqual(resolve.call_count, 3, "changed registries must resolve on every turn")

    def test_custom_registry_and_subprocess_faults_bypass_baseline_reuse(self) -> None:
        resolve = MagicMock(return_value=("gpt-test", "high"))
        defaults = FixtureModelDefaults(resolve)
        configuration = models_registry.launch_configuration({})
        args = argparse.Namespace(role="review", task="", model="gpt-test", effort="high")
        self.assertEqual(defaults(args, configuration), ("gpt-test", "high"))
        with patch.dict(os.environ, {"MODELS_CONFIG_FILE": "/tmp/custom-codex-registry"}):
            self.assertEqual(defaults(args, configuration), ("gpt-test", "high"))
            self.assertEqual(defaults(args, configuration), ("gpt-test", "high"))
        with patch.object(subprocess, "run"):
            self.assertEqual(defaults(args, configuration), ("gpt-test", "high"))
        self.assertEqual(resolve.call_count, 4)
