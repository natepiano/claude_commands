#!/usr/bin/env python3
"""zone.py: resolve a place to a zone, write and push the setting, pull or queue the Mac, tell every session."""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import override
from unittest import mock

from scripts.lib import zone, user_zone
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

SCRIPT = Path(__file__).with_name("zone.py")
CONF_TEXT = "# The zone every time shown to the user is written in.\nzone=America/New_York\n"
# Each fake logs its arguments as one JSON line; the fake ssh is offline while its OFFLINE file exists.
FAKE_SSH = """import json, pathlib, sys
here = pathlib.Path(__file__).parent
with open(here / "ssh.log", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if (here / "OFFLINE").exists():
    print("ssh: connect to host mac port 22: No route to host", file=sys.stderr)
    sys.exit(255)
print("mac: m1 - unit director - sent" if "broadcast.py" in sys.argv[-1] else "rc=0")
"""
FAKE_BROADCAST = """import json, pathlib, sys
with open(pathlib.Path(__file__).parent / "broadcast.log", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
print("s1 - showrunner - sent")
print("s2 - unit director - sent")
"""


class ResolveTests(unittest.TestCase):
    NOW: datetime = datetime(2026, 10, 21, 22, tzinfo=timezone.utc)

    def test_an_iana_name_a_city_and_a_one_zone_label_resolve(self) -> None:
        for name, expected in (("America/Curacao", "America/Curacao"), ("america/curacao", "America/Curacao"),
                               ("Curacao", "America/Curacao"), ("Curaçao", "America/Curacao"),
                               ("new york", "America/New_York"), ("UTC", "UTC")):
            with self.subTest(name=name):
                self.assertEqual(zone.resolve(name, self.NOW), expected)

    def test_a_label_many_zones_use_is_refused_by_offset(self) -> None:
        with self.assertRaises(zone.Refusal) as refused:
            _ = zone.resolve("AST", self.NOW)
        message = str(refused.exception)
        self.assertIn("'AST' is the label of", message)
        self.assertIn("at UTC-04:00 (such as America/", message)
        self.assertIn("name the place or the IANA zone", message)

    def test_a_place_that_is_not_a_zone_is_refused(self) -> None:
        with self.assertRaises(zone.Refusal) as refused:
            _ = zone.resolve("Seattle", self.NOW)
        self.assertIn("no zone is named 'Seattle'; give the IANA zone of the place", str(refused.exception))


@unittest.skipIf(platform.system() == "Darwin", "/zone set runs on natedev")
class SetTests(unittest.TestCase):
    root: Path = Path()
    checkout: Path = Path()
    fakes: Path = Path()
    env: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.checkout, origin, self.fakes = self.root / "checkout", self.root / "origin.git", self.root / "fakes"
        self.fakes.mkdir()
        _ = (self.fakes / "ssh.py").write_text(FAKE_SSH, encoding="utf-8")
        ssh = self.fakes / "ssh"
        _ = ssh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{self.fakes / "ssh.py"}" "$@"\n', encoding="utf-8")
        ssh.chmod(0o755)
        _ = (self.fakes / "broadcast.py").write_text(FAKE_BROADCAST, encoding="utf-8")
        self.env = {key: value for key, value in os.environ.items() if key not in ("USER_ZONE", "XDG_STATE_HOME")}
        self.env.update({
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "USER_ZONE_CONF": str(self.checkout / "config/user_zone.conf"),
            "ZONE_SSH": str(ssh), "ZONE_BROADCAST": str(self.fakes / "broadcast.py"),
            "ZONE_STATE": str(self.root / "state"),
        })
        _ = self.git(self.root, "init", "-q", "--bare", str(origin))
        _ = self.git(self.root, "init", "-q", "-b", "main", str(self.checkout))
        for arguments in (("config", "user.name", "tester"), ("config", "user.email", "tester@example.invalid"),
                          ("remote", "add", "origin", str(origin))):
            _ = self.git(self.checkout, *arguments)
        (self.checkout / "config").mkdir()
        _ = (self.checkout / "config/user_zone.conf").write_text(CONF_TEXT, encoding="utf-8")
        _ = (self.checkout / "other.txt").write_text("work in progress\n", encoding="utf-8")
        _ = self.git(self.checkout, "add", "config/user_zone.conf")
        _ = self.git(self.checkout, "commit", "-q", "-m", "start")
        _ = self.git(self.checkout, "push", "-q", "origin", "main")

    def git(self, cwd: Path, *arguments: str) -> str:
        return subprocess.run(["git", *arguments], cwd=cwd, env=self.env, capture_output=True, text=True,
                              check=True).stdout

    def run_zone(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env=self.env, capture_output=True,
                              text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

    def log(self, name: str) -> list[list[str]]:
        path = self.fakes / name
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        return [[str(part) for part in json.loads(line)] for line in lines]  # pyright: ignore[reportAny]

    def queued(self) -> Path:
        return self.root / "state/mac.json"

    def test_set_writes_commits_and_pushes_the_zone_pulls_the_mac_and_tells_both_machines(self) -> None:
        result = self.run_zone("set", "Curacao")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("zone: America/Curacao (AST, UTC-04:00); now ", result.stdout)
        self.assertIn("was: America/New_York", result.stdout)
        self.assertIn("mac: pulled", result.stdout)
        self.assertIn("broadcast: 2 sent", result.stdout)
        conf = (self.checkout / "config/user_zone.conf").read_text(encoding="utf-8")
        self.assertEqual(conf, CONF_TEXT.replace("America/New_York", "America/Curacao"))
        self.assertEqual(self.git(self.checkout, "log", "-1", "--format=%s", "origin/main").strip(),
                         "user zone: America/Curacao (AST), set with /zone")
        self.assertEqual(self.git(self.checkout, "status", "--short").strip(), "?? other.txt")
        (pull,) = self.log("ssh.log")
        self.assertIn(f"git pull -q --ff-only natedev:{self.checkout} main", pull[-1])
        (sent,) = self.log("broadcast.log")
        self.assertEqual(sent[:4], ["--from", "zone", "--all", sent[3]])
        self.assertIn("now America/Curacao (AST, UTC-04:00); it was America/New_York", sent[3])
        self.assertFalse(self.queued().exists())

    def test_an_offline_mac_is_queued_and_a_bare_zone_sends_it_later(self) -> None:
        _ = (self.fakes / "OFFLINE").write_text("", encoding="utf-8")
        result = self.run_zone("set", "America/Curacao")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("mac: queued (ssh: connect to host mac port 22: No route to host)", result.stdout)
        (sent,) = self.log("broadcast.log")
        self.assertEqual(sent[:3], ["--here", "--from", "zone"])
        self.assertTrue(self.queued().exists())
        still = self.run_zone()
        self.assertIn("mac: the America/Curacao change still waits since", still.stdout)
        (self.fakes / "OFFLINE").unlink()
        shown = self.run_zone()
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("zone: America/Curacao (AST", shown.stdout)
        self.assertIn("mac: took the America/Curacao change queued at", shown.stdout)
        calls = self.log("ssh.log")
        self.assertIn("git pull", calls[-2][-1])
        self.assertIn('broadcast.py" --here --from zone --all', calls[-1][-1])
        self.assertFalse(self.queued().exists())

    def test_a_promotion_that_pulled_the_mac_sends_only_the_message(self) -> None:
        _ = (self.fakes / "OFFLINE").write_text("", encoding="utf-8")
        _ = self.run_zone("set", "Curacao")
        (self.fakes / "OFFLINE").unlink()
        before = len(self.log("ssh.log"))
        drained = self.run_zone("drain", "--pulled")
        self.assertIn("mac: took the America/Curacao change", drained.stdout)
        calls = self.log("ssh.log")[before:]
        self.assertEqual(len(calls), 1)
        self.assertIn("broadcast.py", calls[0][-1])
        self.assertEqual(self.run_zone("drain", "--pulled").stdout, "")

    def test_an_ambiguous_name_changes_nothing(self) -> None:
        result = self.run_zone("set", "AST")
        self.assertEqual(result.returncode, 2)
        self.assertIn("zone: 'AST' is the label of", result.stderr)
        self.assertEqual((self.checkout / "config/user_zone.conf").read_text(encoding="utf-8"), CONF_TEXT)
        self.assertEqual(self.log("ssh.log") + self.log("broadcast.log"), [])

    def test_set_retries_a_zone_written_before_its_commit(self) -> None:
        conf = self.checkout / "config/user_zone.conf"
        head = self.git(self.checkout, "rev-parse", "HEAD")
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(user_zone, "CONF", conf), \
                mock.patch.object(zone, "_commit", side_effect=InterruptedError("before commit")), \
                self.assertRaises(InterruptedError):
            _ = zone.set_zone("Curacao")
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), head)
        self.assertIn("zone=America/Curacao", conf.read_text())
        retried = self.run_zone("set", "Curacao")
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertIn("mac: pulled", retried.stdout)
        self.assertIn("broadcast: 2 sent", retried.stdout)
        self.assertEqual(self.git(self.checkout, "show", "origin/main:config/user_zone.conf"),
                         CONF_TEXT.replace("America/New_York", "America/Curacao"))
        self.assertEqual(self.git(self.checkout, "log", "-1", "--format=%s").strip(),
                         "user zone: America/Curacao (AST), set with /zone")
        self.assertNotIn("config/user_zone.conf", self.git(self.checkout, "status", "--short"))

    def test_the_same_zone_again_makes_no_commit(self) -> None:
        head = self.git(self.checkout, "rev-parse", "HEAD")
        result = self.run_zone("set", "New York")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("was: the same; nothing to commit", result.stdout)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), head)

    def test_a_checkout_off_main_is_refused(self) -> None:
        _ = self.git(self.checkout, "switch", "-q", "-c", "work")
        result = self.run_zone("set", "Curacao")
        self.assertEqual(result.returncode, 2)
        self.assertIn("is on work, not main", result.stderr)
        self.assertEqual(self.log("ssh.log"), [])


if __name__ == "__main__":
    _ = unittest.main()
