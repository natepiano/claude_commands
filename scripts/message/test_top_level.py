#!/usr/bin/env python3
"""Tests for top_level.py: who counts as a top-level session."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import override

import top_level


class TopLevelTests(unittest.TestCase):
    folder: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def add(self, name: str, pid: int | None = None, start: str | None = None, tmux: str | None = None) -> None:
        pid = os.getpid() if pid is None else pid
        session: dict[str, object] = {"pid": pid, "name": name, "procStart": start or top_level.proc_start(pid)}
        if tmux:
            session["tmux"] = tmux
        _ = (self.folder / f"{name}.json").write_text(json.dumps(session))

    def names(self, me: str = "natedev") -> list[str]:
        return top_level.top_level(me, self.folder, lambda target: target.startswith("unit-"))

    def test_keeps_live_sessions_and_drops_self(self) -> None:
        self.add("natedev")
        self.add("showrunner")
        self.add("berth-fix", tmux="berth-fix:@3.%3")
        self.assertEqual(self.names(), ["berth-fix", "showrunner"])

    def test_drops_unit_directors(self) -> None:
        self.add("showrunner")
        self.add("trunk", tmux="unit-trunk:@0.%0")
        self.assertEqual(self.names(), ["showrunner"])

    def test_drops_ended_and_reused_pids(self) -> None:
        self.add("ended", pid=2**22 + 1, start="1")
        self.add("reused", start="1")
        self.assertEqual(self.names(), [])

    def test_usage(self) -> None:
        self.assertEqual(top_level.main(["natedev"]), 2)


if __name__ == "__main__":
    _ = unittest.main()
