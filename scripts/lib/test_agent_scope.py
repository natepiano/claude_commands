"""Test live configuration for the shared agent scope."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.lib import agent_scope


class AgentScopeTests(unittest.TestCase):
    def test_probe_uses_each_live_slice_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stub = root / "systemd-run"
            _ = stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CONF_DIR/probe"\n')
            stub.chmod(0o755)
            with patch.dict(os.environ, {"PATH": directory, "CONF_DIR": directory}, clear=True):
                for slice_name in ("app-first.slice", "app-second.slice"):
                    _ = (root / "codex_daemon.conf").write_text(f"agents_slice={slice_name}\n")
                    prefix, reason = agent_scope.agent_scope_prefix()
                    self.assertEqual(reason, "")
                    self.assertEqual((root / "probe").read_text().splitlines(), [*prefix[1:], "true"])
                    self.assertIn(f"--slice={slice_name}", prefix)
