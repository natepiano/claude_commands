#!/usr/bin/env python3
"""Tests for the agent registry's local overlay: readers merge it, writers store only it, migrate fills it."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import final, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import agents_registry  # noqa: E402

AGENTS = Path(__file__).resolve().parent
DEFAULTS = """[assignments]
direct=codex

[direct.codex]
impl=gpt-a:xhigh   # the committed row
review=gpt-a:max

[direct.options]
codex_mesh=1

[codex.agents]
gpt-a=low,high,xhigh,max
gpt-b=low,high,xhigh,max
"""


@final
class OverlayTests(unittest.TestCase):
    root: Path = Path()
    registry: Path = Path()
    overlay: Path = Path()

    @override
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.registry = self.root / "agents.conf"
        self.overlay = self.root / "agents.local.conf"
        _ = self.registry.write_text(DEFAULTS, encoding="utf-8")

    def bash(self, script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            "AGENTS_CONFIG_FILE": str(self.registry),
            "AGENTS_CONFIG_TIER_ONLY": "1",
            "CODEX_CONFIG_FILE": str(self.root / "codex.toml"),
            "CODEX_PACER_TIER_FILE": str(self.root / "tier"),
        }
        _ = environment.pop("AGENTS_LOCAL_CONFIG_FILE", None)
        return subprocess.run(["bash", "-c", script, "_", *arguments], capture_output=True, text=True,
                              env=environment, check=False)

    def resolve(self, task: str) -> str:
        done = self.bash(f'source "{AGENTS}/agents_config.sh" && agents_resolve {task} && '
                         + 'printf "%s:%s" "$AGENT_MODEL" "$AGENT_EFFORT"')
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_an_overlay_row_wins_and_an_overlay_only_row_and_section_are_read(self) -> None:
        overlay = "[direct.codex]\nimpl=gpt-b:high\nfix=gpt-b:low\n\n[design_roles.codex]\ndesigner=gpt-b:xhigh\n"
        _ = self.overlay.write_text(overlay + "\n[assignments]\ndesign_roles=codex\n", encoding="utf-8")
        self.assertEqual(self.resolve("direct.impl"), "gpt-b:high")
        self.assertEqual(self.resolve("direct.review"), "gpt-a:max")
        self.assertEqual(self.resolve("direct.fix"), "gpt-b:low")
        self.assertEqual(self.resolve("design_roles.designer"), "gpt-b:xhigh")
        environment = {"AGENTS_CONFIG_FILE": str(self.registry)}
        with mock.patch.dict(os.environ, environment):
            bash_view = self.bash(f'source "{AGENTS}/agents_overlay.sh" && agents_registry_text').stdout
            self.assertEqual(agents_registry.registry_text(), bash_view)

    def test_a_writer_stores_only_the_differing_rows_and_never_the_committed_file(self) -> None:
        done = self.bash(f'source "{AGENTS}/agents_config.sh" && agents_set_row direct.impl gpt-b:high')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.registry.read_text(encoding="utf-8"), DEFAULTS)
        rows = [line for line in self.overlay.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#")]
        self.assertEqual(rows, ["[direct.codex]", "impl=gpt-b:high"])
        self.assertEqual(self.resolve("direct.impl"), "gpt-b:high")
        # Setting the committed value back leaves no row that differs, so the overlay goes.
        done = self.bash(f'source "{AGENTS}/agents_config.sh" && agents_set_row direct.impl gpt-a:xhigh')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(self.overlay.exists())
        self.assertEqual(self.registry.read_text(encoding="utf-8"), DEFAULTS)

    def test_migrate_moves_a_pre_rename_registrys_local_rows_into_the_overlay(self) -> None:
        copy = self.root / "premigrate.conf"
        old = DEFAULTS.replace("direct=codex", "delegate=codex").replace("[direct.", "[delegate.")
        _ = copy.write_text(old.replace("impl=gpt-a:xhigh   # the committed row", "impl=gpt-b:high"),
                            encoding="utf-8")
        done = self.bash(f'bash "{AGENTS}/agents_overlay.sh" migrate --from "$1"', str(copy))
        self.assertEqual(done.returncode, 0, done.stderr)
        rows = [line for line in self.overlay.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#")]
        self.assertEqual(rows, ["[direct.codex]", "impl=gpt-b:high"])
        self.assertEqual(self.resolve("direct.impl"), "gpt-b:high")

    def test_migrate_refuses_the_committed_file_and_writes_nothing_when_nothing_differs(self) -> None:
        refused = self.bash(f'bash "{AGENTS}/agents_overlay.sh" migrate --from "$1"', str(self.registry))
        self.assertEqual(refused.returncode, 2, refused.stderr)
        copy = self.root / "same.conf"
        _ = copy.write_text(DEFAULTS, encoding="utf-8")
        done = self.bash(f'bash "{AGENTS}/agents_overlay.sh" migrate --from "$1"', str(copy))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("no overlay written", done.stdout)
        self.assertFalse(self.overlay.exists())


if __name__ == "__main__":
    _ = unittest.main()
