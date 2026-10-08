"""Tests for configured unit discovery with retired production rows."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import override


SCRIPT = Path(__file__).with_name("live_units.py")


class LiveUnitsTests(unittest.TestCase):
    root: Path = Path()
    config: Path = Path()
    notifier: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.root / "showrunners.json"
        self.notifier = self.root / "notifier"
        self.notifier.mkdir()
        self.environment = {
            **os.environ,
            "SHOWRUNNERS_CONFIG": str(self.config),
            "NOTIFIER_STATE_DIR": str(self.notifier),
        }
        self.write_config("director", [
            ("alpha", "running"),
            ("old", "running"),
            ("standby", "standing-by"),
            ("finished", "run-finished"),
        ])

    def write_config(self, session: str, units: list[tuple[str, str]]) -> None:
        _ = self.config.write_text(json.dumps({
            "threshold_percent": 2,
            "repeat_minutes": 30,
            "stall_minutes": 5,
            "faults_to": "natedev",
            "always": ["natedev"],
            "showrunners": [{
                "session": session,
                "zone": "America/Los_Angeles",
                "units": [{"session": unit, "status": status} for unit, status in units],
            }],
        }), encoding="utf-8")

    def production_doc(self, name: str, showrunner: str, retired_session: str) -> Path:
        doc = self.root / f"{name}-production.md"
        _ = doc.write_text("\n".join((
            "# Production",
            f"- **Showrunner session:** {showrunner}",
            "## Units",
            "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            f"| old-unit | (retired by the user; run done) follow-up | /tmp/old | old | `{retired_session}` (resumed elsewhere) | — | — |",
        )), encoding="utf-8")
        return doc

    def instance(self, name: str, doc: Path) -> None:
        directory = self.notifier / f"showrunner-{name}"
        directory.mkdir()
        check = shlex.join(["zsh", "/opt/tools/production_check.sh", str(doc)])
        _ = (directory / "conf").write_text(f"CHECK={check}\n", encoding="utf-8")

    def run_script(self, session: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), session], env=self.environment,
                              capture_output=True, text=True, check=False)

    def test_registry_order_and_every_live_status_survive_retirement_filter(self) -> None:
        self.instance("current", self.production_doc("current", "director", "old"))
        result = self.run_script("director")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["alpha", "standby", "finished"])

    def test_doc_for_another_showrunner_retires_nothing(self) -> None:
        self.instance("other", self.production_doc("other", "other-director", "old"))
        result = self.run_script("director")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["alpha", "old", "standby", "finished"])

    def test_no_checked_doc_keeps_every_configured_unit(self) -> None:
        directory = self.notifier / "showrunner-current"
        directory.mkdir()
        _ = (directory / "conf").write_text("TARGET=session:abc\n", encoding="utf-8")
        result = self.run_script("director")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["alpha", "old", "standby", "finished"])

    def test_absent_showrunner_exits_one(self) -> None:
        result = self.run_script("missing")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.strip(), "showrunner absent from config: missing")


if __name__ == "__main__":
    _ = unittest.main()
