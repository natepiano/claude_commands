"""Regression tests for the delegate run's terminal cleanup."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import override


END_SESSION = Path(__file__).with_name("end_session.sh")
ACTIVE_ROOT = Path("/tmp/claude/delegate/active")


class EndSessionTests(unittest.TestCase):
    """A roster remains terminal even when its app-server record is gone."""

    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    session_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    marker: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    script: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    calls: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    session_id: str  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.session_dir = self.root / "run"
        self.session_dir.mkdir()
        session_id = f"end-session-test-{os.getpid()}-{self.root.name}"
        ACTIVE_ROOT.mkdir(parents=True, exist_ok=True)
        self.marker = ACTIVE_ROOT / session_id
        _ = self.marker.write_text(f"{self.session_dir}\n", encoding="utf-8")
        self.calls = self.root / "calls"

        scripts = self.root / "scripts"
        delegate = scripts / "delegate"
        agents = scripts / "agents"
        message = scripts / "message"
        delegate.mkdir(parents=True)
        agents.mkdir()
        message.mkdir()
        self.script = delegate / "end_session.sh"
        _ = shutil.copy2(END_SESSION, self.script)
        _ = (delegate / "remove_seats.py").write_text("", encoding="utf-8")
        _ = (agents / "codex_mesh.py").write_text("", encoding="utf-8")
        notifier = message / "notifier.sh"
        _ = notifier.write_text("#!/usr/bin/env zsh\nexit 0\n", encoding="utf-8")
        notifier.chmod(0o755)

        python_shim = self.root / ".claude" / "scripts" / "lib" / "py"
        python_shim.parent.mkdir(parents=True)
        _ = python_shim.write_text(
            "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"${END_SESSION_CALLS}\"\n",
            encoding="utf-8",
        )
        python_shim.chmod(0o755)
        self.session_id = session_id

    @override
    def tearDown(self) -> None:
        self.marker.unlink(missing_ok=True)
        self.temporary.cleanup()

    def test_roster_without_server_record_still_runs_mesh_stop(self) -> None:
        _ = (self.session_dir / "mesh_roster.json").write_text(json.dumps({
            "project-impl": {"thread_id": "thread-1", "status": "done"},
        }), encoding="utf-8")
        environment = os.environ.copy()
        environment.update({
            "HOME": str(self.root),
            "CLAUDE_CODE_SESSION_ID": self.session_id,
            "END_SESSION_CALLS": str(self.calls),
        })

        result = subprocess.run(
            ["bash", str(self.script)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 2)
        self.assertIn("remove_seats.py --session-dir", calls[0])
        self.assertIn("codex_mesh.py stop --session-dir", calls[1])
        self.assertIn(str(self.session_dir), calls[1])
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    _ = unittest.main()
