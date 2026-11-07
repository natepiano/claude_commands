"""Probe identity comes from the live process, including unreadable environments."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import override
from unittest.mock import patch

import inventory
import test_inventory as fixtures
from test_support import ShutdownTestCase


class ProbeInventoryTests(ShutdownTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        read_environment = inventory._process_environment  # pyright: ignore[reportPrivateUsage]
        fixture = fixtures.InventoryTests(methodName="runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        _ = self.enterContext(patch.object(inventory, "_process_environment", new=read_environment))
        _ = self.enterContext(patch.object(sys, "platform", "linux"))

    def test_only_exact_one_in_the_process_environment_marks_a_probe(self) -> None:
        read_bytes = Path.read_bytes
        for marker in (None, "", "0", "true", "01", "1 ", "1"):
            with self.subTest(marker=marker):
                environment = b"UNRELATED=1\0"
                if marker is not None:
                    environment += f"CLAUDE_PROBE_SESSION={marker}\0".encode()
                def read_environment(path: Path) -> bytes:
                    if path.parent.parent == Path("/proc") and path.name == "environ":
                        self.assertEqual(path, Path(f"/proc/{fixtures.DESKTOP_PID}/environ"))
                        return environment
                    return read_bytes(path)
                with (patch.dict(os.environ, {"CLAUDE_PROBE_SESSION": "1"}),
                      patch.object(Path, "read_bytes", autospec=True, side_effect=read_environment)):
                    found = inventory.inventory(fixtures.TARGET_LOGIN, only=frozenset({fixtures.DESKTOP_ID}))
                self.assertEqual(len(found["sessions"]), 1)
                session = found["sessions"][0]
                self.assertEqual(session["session_id"], fixtures.DESKTOP_ID)
                self.assertEqual(session["probe"], marker == "1")
                decoded = inventory.parse_inventory(json.dumps(found))
                self.assertEqual(decoded["sessions"][0]["probe"], marker == "1")

    def test_unreadable_process_environment_leaves_the_session_non_probe(self) -> None:
        with (patch.dict(os.environ, {"CLAUDE_PROBE_SESSION": "1"}),
              patch.object(Path, "read_bytes", side_effect=PermissionError("environment denied"))):
            found = inventory.inventory(fixtures.TARGET_LOGIN, only=frozenset({fixtures.DESKTOP_ID}))
        self.assertEqual(len(found["sessions"]), 1)
        self.assertFalse(found["sessions"][0]["probe"])
