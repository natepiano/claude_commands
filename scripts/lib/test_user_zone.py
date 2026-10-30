#!/usr/bin/env python3
"""user_zone.py: the one setting for the zone the user's times are written in."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import override

from scripts.lib.code_kind import is_test_code

SCRIPT = Path(__file__).with_name("user_zone.py")
PROBE = ("import sys; sys.path.insert(0, sys.argv[1]); import user_zone; from datetime import datetime, timezone; "
         "print(user_zone.user_zone().key, user_zone.zone_label(datetime(2026, 7, 1, 12, tzinfo=timezone.utc)), "
         "user_zone.zone_label(datetime(2026, 1, 1, 12, tzinfo=timezone.utc)))")

# A zone written into a script: a named zone handed to ZoneInfo, a TZ= prefix, or a label after a time format.
HARDCODED = re.compile(r"""ZoneInfo\(\s*["'](?!UTC)|TZ=["']?[A-Z][a-z]+/|%[HMS] ?[PE][DS]T\b""")
# The scripts that may name a zone, each because the zone there is a fact and never the user's setting.
NAMES_A_ZONE = {
    "build_queue/queue_fixture.py": "one fixed zone, so a golden render is the same in every user zone",
    "model_study/turns.py": "the zone the roster's stored times were recorded in",
    "prioritize/renumber.py": "one fixed zone, so a date is the same on both machines",
    "whoami/agent_accounts.py": "the offset a stored reset time is written with",
}
# A time put in the machine's zone: `astimezone()` with no zone.
MACHINE_ZONE = re.compile(r"\.astimezone\(\)")
# The scripts that may use the machine's zone, each because the time there is stored or compared, never shown.
USES_THE_MACHINE_ZONE = {
    "agents/codex_mesh.py": "log stamps written with their offset",
    "hooks/banned_words_lib.py": "a log stamp written with its offset",
    "notify/pushover.py": "a log stamp written with its offset",
    "notify/review.py": "a log stamp written with its offset",
    "production/dailies_render.py": "reads reset times stored with no offset, in the machine's zone",
    "production/review_regime.py": "a stamp written with its offset",
    "shutdown/inventory.py": "Codex names its session folders by the machine's date",
    "whoami/agent_notes.py": "reset times written with their offset, and the note's date as the vault's linter writes it",
    "whoami/codex_pacer.py": "Codex names its session folders by the machine's date",
    "whoami/note_file.py": "reads a reset time stored before the offset was, in the machine's zone",
}
SKIPPED_DIRS = {"__pycache__", "fixtures", "tests", "testdata", "node_modules"}


class UserZoneTests(unittest.TestCase):
    conf: Path = Path()

    @override
    def setUp(self) -> None:
        self.conf = Path(self.enterContext(tempfile.TemporaryDirectory())) / "user_zone.conf"

    def run_script(self, *, pinned: str = "") -> subprocess.CompletedProcess[str]:
        env = {key: value for key, value in os.environ.items() if key != "USER_ZONE"}
        env["USER_ZONE_CONF"] = str(self.conf)
        if pinned:
            env["USER_ZONE"] = pinned
        return subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, check=False)

    def test_the_file_names_the_zone(self) -> None:
        _ = self.conf.write_text("# a comment\nzone=Europe/Paris\n", encoding="utf-8")
        result = self.run_script()
        self.assertEqual((result.returncode, result.stdout), (0, "Europe/Paris\n"), result.stderr)

    def test_no_file_and_no_zone_line_both_give_the_default(self) -> None:
        self.assertEqual(self.run_script().stdout, "America/New_York\n")
        _ = self.conf.write_text("# nothing set\n", encoding="utf-8")
        self.assertEqual(self.run_script().stdout, "America/New_York\n")

    def test_the_environment_wins_over_the_file(self) -> None:
        _ = self.conf.write_text("zone=Europe/Paris\n", encoding="utf-8")
        self.assertEqual(self.run_script(pinned="America/Los_Angeles").stdout, "America/Los_Angeles\n")

    def test_an_unknown_zone_is_refused_and_its_source_named(self) -> None:
        _ = self.conf.write_text("zone=Mars/Phobos\n", encoding="utf-8")
        from_file = self.run_script()
        self.assertEqual(from_file.returncode, 2)
        self.assertIn(f"user_zone: {self.conf}: 'Mars/Phobos' is not a zone name", from_file.stderr)
        pinned = self.run_script(pinned="Nowhere")
        self.assertEqual(pinned.returncode, 2)
        self.assertIn("user_zone: USER_ZONE: 'Nowhere' is not a zone name", pinned.stderr)

    def test_the_label_comes_from_the_zone_and_the_date(self) -> None:
        env = {**os.environ, "USER_ZONE_CONF": str(self.conf), "USER_ZONE": "America/New_York"}
        result = subprocess.run([sys.executable, "-c", PROBE, str(SCRIPT.parent)], env=env, capture_output=True,
                                text=True, check=False)
        self.assertEqual(result.stdout, "America/New_York EDT EST\n", result.stderr)

    def test_no_script_writes_the_users_zone_into_its_own_code(self) -> None:
        scripts = SCRIPT.parents[1]
        found: set[str] = set()
        for path in scripts.rglob("*"):
            relative = path.relative_to(scripts)
            if (path.suffix not in (".py", ".sh") or is_test_code(path)
                    or SKIPPED_DIRS.intersection(relative.parts) or not path.is_file()):
                continue
            if HARDCODED.search(path.read_text(encoding="utf-8", errors="replace")):
                found.add(relative.as_posix())
        self.assertEqual(found, set(NAMES_A_ZONE), "a script names a zone itself; read it from user_zone.py, "
                         + "or add the script to NAMES_A_ZONE with the reason its zone is a fact")

    def test_no_script_shows_a_time_in_the_machines_zone(self) -> None:
        scripts = SCRIPT.parents[1]
        found: set[str] = set()
        for path in scripts.rglob("*.py"):
            relative = path.relative_to(scripts)
            if is_test_code(path) or SKIPPED_DIRS.intersection(relative.parts) or not path.is_file():
                continue
            if MACHINE_ZONE.search(path.read_text(encoding="utf-8", errors="replace")):
                found.add(relative.as_posix())
        self.assertEqual(found, set(USES_THE_MACHINE_ZONE), "a script puts a time in the machine's zone; pass "
                         + "user_zone() to astimezone(), or add the script to USES_THE_MACHINE_ZONE with the reason")

    def test_the_shipped_setting_is_a_real_zone(self) -> None:
        env = {key: value for key, value in os.environ.items() if key not in ("USER_ZONE", "USER_ZONE_CONF")}
        result = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip())


if __name__ == "__main__":
    _ = unittest.main()
