#!/usr/bin/env python3
"""Tests for top_level.py: who counts as a top-level session."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast, override
from unittest import mock

import top_level


class TopLevelTests(unittest.TestCase):
    folder: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        meminfo = self.folder / "meminfo"
        _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.folder),
            "CLAUDE_CODE_SESSION_ID": "natedev",
            "BUILD_HOLD_DIR": str(self.folder / "holders"),
            "BUILD_HOLD_RELEASE_DIR": str(self.folder / "release"),
            "BUILDLOG_MEMINFO": str(meminfo),
        }))

    def add(self, name: str, pid: int | None = None, start: str | None = None, tmux: str | None = None,
            session_id: str = "", file_name: str = "") -> None:
        pid = os.getpid() if pid is None else pid
        session: dict[str, object] = {"pid": pid, "name": name, "procStart": start or top_level.proc_start(pid)}
        session["sessionId"] = session_id or name
        if tmux:
            session["tmux"] = tmux
        _ = (self.folder / f"{file_name or name}.json").write_text(json.dumps(session))

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

    def test_forwarded_sessions_preserve_two_ids_with_one_name(self) -> None:
        self.add("shared", session_id="session-a", file_name="first")
        self.add("shared", session_id="session-b", file_name="second")
        self.add("natedev", session_id="natedev")
        self.add("unit", tmux="unit-trunk:@0.%0", session_id="director")
        forwarded = top_level.forwarded_sessions(
            "natedev", self.folder, lambda target: target.startswith("unit-")
        )
        self.assertEqual(forwarded, [("shared", "session-a"), ("shared", "session-b")])
        self.assertEqual(self.names(), ["shared"])

    def test_main_records_each_forwarded_id_while_printing_one_name(self) -> None:
        script = Path(__file__).resolve().parent.parent / "build_hold" / "build_hold.py"
        held = subprocess.run(
            ["python3", str(script), "hold", "--holder", "holder", "--for", "the test"],
            capture_output=True, text=True, check=False, env=os.environ.copy(),
        )
        self.assertEqual(held.returncode, 0, held.stderr)
        output = io.StringIO()
        with mock.patch.object(top_level, "forwarded_sessions", return_value=[
            ("shared", "session-a"), ("shared", "session-b"),
        ]), redirect_stdout(output):
            self.assertEqual(top_level.main(["--self", "natedev"]), 0)
        self.assertEqual(output.getvalue().splitlines(), ["shared"])
        release = self.folder / "release"
        cycle_id = (release / "current").read_text().strip()
        cycle = cast(dict[str, object], json.loads((release / cycle_id / "cycle.json").read_text()))
        self.assertEqual(cast(dict[str, str], cycle["recipients"]), {
            "session-a": "shared", "session-b": "shared",
        })

    def test_usage(self) -> None:
        self.assertEqual(top_level.main(["natedev"]), 2)


if __name__ == "__main__":
    _ = unittest.main()
