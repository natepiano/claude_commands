"""Check feed configuration and publication with private queue readings."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from typing import cast, override
from unittest import mock

from scripts.build_queue import admission_client, admission_protocol as wire, memory_feed
from scripts.lib import conf, test_hang_guard
from scripts.message import unit_labels
from scripts.lint import memory_admit as gate
from scripts.lint.memory_test_support import admission_policy_fixture


class FeedStopped(Exception):
    """The fixture stops the feed after its recorded cycles."""


def captured_reading() -> gate.QueueRead:
    inputs = gate.QueueInputs([], (), (), gate.HostMemory(30 * gate.GIB, 60 * gate.GIB),
        gate.NO_PRESSURE_HOLD, gate.ReadyToStart(), gate.NoHeadAdmissionOverride(), 100)
    return gate.queue_read_from(inputs, admission_policy_fixture())


class MemoryFeedTests(unittest.TestCase):
    root: Path = Path()
    output: Path = Path()
    configuration: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.output = self.root / "page"
        self.configuration = self.root / "memory_feed.conf"
        self.configure()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "CONF_DIR": str(self.root),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "STAFF_FILE": str(self.root / "staff.json"),
        })))
        _ = self.enterContext(mock.patch.object(admission_client, "view",
            side_effect=AssertionError("The test must supply a private queue reading")))
        _ = self.enterContext(mock.patch.object(unit_labels, "unit_labels_for", return_value={}))
        _ = self.enterContext(mock.patch.object(memory_feed, "host_name", return_value="fixture"))
        _ = self.enterContext(mock.patch.object(memory_feed, "PROC", self.root / "proc"))
        _ = self.enterContext(mock.patch.object(memory_feed, "CI_CGROUP", self.root / "ci"))

    def configure(self, *, seconds: float = 0.125) -> None:
        _ = self.configuration.write_text(
            f"output_folder={self.output}\n# range: 5e-324..\ninterval_seconds={seconds}\n")

    def expected_page(self, seconds: float = 0.125) -> str:
        return Path(memory_feed.__file__).with_name("memory_live.html").read_text().replace(
            "__MEMORY_FEED_INTERVAL_SECONDS__", json.dumps(seconds))

    def test_configuration_reads_both_keys_from_the_feed_config(self) -> None:
        with mock.patch.object(conf, "read", wraps=conf.read) as read:
            configuration = memory_feed.read_configuration()
        read.assert_called_once_with("memory_feed")
        self.assertEqual(configuration, memory_feed.FeedConfiguration(self.output, 0.125))

    def test_configuration_expands_the_output_folder(self) -> None:
        _ = self.configuration.write_text("output_folder=~/page\n# range: 5e-324..\ninterval_seconds=7\n")
        self.assertEqual(memory_feed.read_configuration().output_folder, Path.home() / "page")
        _ = self.configuration.write_text("output_folder=$FEED_TEST_ROOT/page\n# range: 5e-324..\ninterval_seconds=7\n")
        with mock.patch.dict(os.environ, {"FEED_TEST_ROOT": str(self.root)}):
            self.assertEqual(memory_feed.read_configuration().output_folder, self.output)

    def test_a_missing_key_fails_with_its_name(self) -> None:
        for key, remaining in (("output_folder", "# range: 5e-324..\ninterval_seconds=3\n"),
                               ("interval_seconds", f"output_folder={self.output}\n")):
            with self.subTest(key=key):
                _ = self.configuration.write_text(remaining)
                with conf.diagnostics(report=False), self.assertRaisesRegex(conf.Unusable, key):
                    _ = memory_feed.read_configuration(conf.read(self.configuration))

    def test_start_publishes_the_page_before_the_first_queue_reading(self) -> None:
        def view() -> gate.QueueRead:
            self.assertEqual((self.output / "memory_live.html").read_text(), self.expected_page())
            return captured_reading()

        with mock.patch.object(admission_client, "view", side_effect=view) as read:
            self.assertEqual(memory_feed.main(["--once"]), 0)
        read.assert_called_once_with()
        page = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
        self.assertIs(page["ok"], True)
        self.assertEqual(page["read_at"], 100)
        self.assertEqual(page["total"], 60)
        self.assertEqual(page["available"], 30)
        self.assertEqual(page["walk"], [])
        self.assertEqual(set(path.name for path in self.output.iterdir()), {"memory_live.html", "memory.json"})
        for name in ("memory_live.html", "memory.json"):
            with self.subTest(name=name):
                self.assertEqual((self.output / name).stat().st_mode & 0o777, 0o644)

    def test_restart_replaces_the_served_page_with_configured_timing(self) -> None:
        self.output.mkdir()
        executable = shutil.which("node") or str(next(Path("/nix/store").glob("*nodejs*/bin/node")))
        for seconds, stale in ((0.125, 15), (60.0, 300)):
            with self.subTest(seconds=seconds):
                self.configure(seconds=seconds)
                _ = (self.output / "memory_live.html").write_text("previous page")
                with mock.patch.object(admission_client, "view", return_value=captured_reading()):
                    self.assertEqual(memory_feed.main(["--once"]), 0)
                page = (self.output / "memory_live.html").read_text()
                self.assertEqual(page, self.expected_page(seconds))
                constants = page[page.index("const SECONDS ="):page.index("const $ =")]
                result = subprocess.run(
                    [executable, "-e", constants + "\nprocess.stdout.write(String(STALE));"],
                    text=True, capture_output=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(float(result.stdout), stale)

    def test_an_interrupted_page_publication_preserves_the_previous_page(self) -> None:
        self.output.mkdir()
        target = self.output / "memory_live.html"
        _ = target.write_text("previous page")
        with mock.patch("scripts.lib.atomic_file.os.replace", side_effect=OSError("publication interrupted")):
            with self.assertRaisesRegex(OSError, "publication interrupted"):
                memory_feed.publish_page(memory_feed.read_configuration())
        self.assertEqual(target.read_text(), "previous page")
        self.assertEqual(list(self.output.iterdir()), [target])

    def test_the_first_failed_reading_publishes_an_error(self) -> None:
        with mock.patch.object(admission_client, "view",
                               side_effect=wire.AdmissionUnavailable("private socket unavailable")):
            self.assertEqual(memory_feed.main(["--once"]), 0)
        page = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
        self.assertIs(page["ok"], False)
        self.assertEqual(page["error"], "private socket unavailable")

    def test_a_failed_cycle_retains_the_last_successful_reading(self) -> None:
        with (mock.patch.object(admission_client, "view",
                                side_effect=[captured_reading(), RuntimeError("queue unavailable")]),
              mock.patch.object(time, "sleep", side_effect=[None, FeedStopped]) as sleep):
            with self.assertRaises(FeedStopped):
                _ = memory_feed.main([])
        self.assertEqual(sleep.call_args_list, [mock.call(0.125), mock.call(0.125)])
        page = cast(dict[str, object], json.loads((self.output / "memory.json").read_text()))
        self.assertIs(page["ok"], True)
        self.assertEqual(page["read_at"], 100)
        self.assertEqual(page["read_error"], "queue unavailable")

    def test_missing_pressure_has_a_named_state(self) -> None:
        pressure = memory_feed.read_stalls()
        self.assertIsInstance(pressure["some"], memory_feed.PressureNotRecorded)
        self.assertIsInstance(pressure["full"], memory_feed.PressureNotRecorded)

    def test_the_json_boundary_preserves_the_page_null_contract(self) -> None:
        with mock.patch.object(admission_client, "view", return_value=captured_reading()):
            payload = memory_feed.reading()
        payload["cap"] = gate.CapUnavailable(())
        page = cast(dict[str, object], json.loads(memory_feed.page_json(payload)))
        self.assertIsNone(page["cap"])
        self.assertEqual(page["psi"], {"some": None, "full": None})
