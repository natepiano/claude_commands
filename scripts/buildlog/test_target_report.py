"""Keep AWS and local steps outside the Mac report section."""

from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from typing import cast, override
import unittest
from unittest import mock

from scripts.buildlog import index
from scripts.buildlog import report
from scripts.buildlog.test_index import Record, STAMP, call, encode, local_day, point_root_at, step

from scripts.lint.memory_test_support import write_config_fixture


class TargetReportTests(unittest.TestCase):
    root: Path = Path()
    registry: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "buildlog"
        _ = write_config_fixture(self.root.parent / "buildlog.conf")
        self.registry = self.root.parent / "build_targets.conf"
        _ = self.registry.write_text(
            "targets=mac,natedev\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac.conf\n"
            + "kind.natedev=local\nplatform.natedev=linux\n"
            + "sync.hosts.mac=Mac,Nates-MacBook-Pro-2023\nsync.hosts.aws=aws-build\n"
            + "sync.hosts.natedev=natedev\n"
            + "# whole: 0..10\nvault.lock_retries=5\n"
            + "# range: 0..5\nvault.lock_retry_delay_s=3\n"
        )
        _ = (self.root.parent / "mac.conf").write_text("host=fake-mac\nci_repo=\n")
        point_root_at(self, self.root)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "USER_ZONE": "UTC", "BUILD_TARGETS_CONFIG": str(self.registry),
            "CONF_DIR": str(self.root.parent),
        })))
        _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=AssertionError("Unexpected external command")))

    def write(self, *records: Record) -> None:
        for row in records:
            path = self.root / str(row["host"]) / "2026-10.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab") as output:
                _ = output.write(encode(row))
        _ = index.update()

    def mac_section(self) -> str:
        with closing(index.read_only()) as connection:
            return "\n".join(report.mac_section(connection, local_day(STAMP)))

    def test_mixed_day_counts_only_mac_steps_and_legacy_mac_aliases(self) -> None:
        self.write(
            step("mac", host="Mac", target="mac", step="nextest", duration_s=11),
            step("legacy", host="Nates-MacBook-Pro-2023", step="doc", duration_s=13),
            step("aws", host="aws-build", target="aws", step="check", duration_s=500, status=1),
            step("local", host="natedev", target="natedev", step="clippy", duration_s=700, status=1),
            step("unknown", host="other-host", step="build", duration_s=900, status=1),
        )

        text = self.mac_section()

        self.assertIn("Mac total: 2 steps, 24.0 s; 0 failed.", text)
        self.assertIn("| nextest |", text)
        self.assertIn("| doc |", text)
        self.assertNotIn("| check |", text)
        self.assertNotIn("| clippy |", text)
        self.assertNotIn("| build |", text)

    def test_target_identity_selects_mac_on_a_new_host(self) -> None:
        self.write(step("renamed", host="new-mac-host", target="mac", step="doc", duration_s=17))

        self.assertIn("Mac total: 1 steps, 17.0 s; 0 failed.", self.mac_section())

    def test_explicit_aws_target_overrides_a_legacy_mac_host(self) -> None:
        self.write(step("moved", host="Mac", target="aws", step="check", duration_s=91))

        self.assertIn("No steps recorded on the Mac.", self.mac_section())

    def test_day_with_aws_and_local_steps_has_an_empty_mac_section(self) -> None:
        self.write(
            step("aws", host="aws-build", target="aws", step="doc"),
            step("local", host="natedev", target="natedev", step="nextest"),
        )

        self.assertIn("No steps recorded on the Mac.", self.mac_section())

    def test_explicit_local_target_overrides_a_legacy_mac_host(self) -> None:
        self.write(step("moved", host="Nates-MacBook-Pro-2023", target="natedev", step="check"))

        self.assertIn("No steps recorded on the Mac.", self.mac_section())

    def test_empty_target_uses_the_legacy_mac_host_alias(self) -> None:
        self.write(step("legacy", host="Mac", target="", step="doc", duration_s=19))

        self.assertIn("Mac total: 1 steps, 19.0 s; 0 failed.", self.mac_section())

    def test_configured_host_mapping_reloads_for_old_records(self) -> None:
        self.write(step("renamed", host="replacement-mac", step="doc", duration_s=23))
        self.assertIn("No steps recorded on the Mac.", self.mac_section())
        _ = self.registry.write_text(self.registry.read_text().replace(
            "sync.hosts.mac=Mac,Nates-MacBook-Pro-2023", "sync.hosts.mac=Mac,Nates-MacBook-Pro-2023,replacement-mac",
        ))

        self.assertIn("Mac total: 1 steps, 23.0 s; 0 failed.", self.mac_section())

    def test_index_rebuild_preserves_explicit_target_selection(self) -> None:
        self.write(
            step("mac", host="new-mac-host", target="mac", step="doc", duration_s=29),
            step("aws", host="Mac", target="aws", step="check", duration_s=500),
        )
        with closing(sqlite3.connect(index.index_path())) as connection:
            _ = connection.execute("PRAGMA user_version = 26")
            _ = connection.execute("UPDATE steps SET target = NULL")
            connection.commit()

        _ = index.update()

        text = self.mac_section()
        self.assertIn("Mac total: 1 steps, 29.0 s; 0 failed.", text)
        self.assertNotIn("| check |", text)

    def test_call_report_uses_configured_mac_and_local_target_names(self) -> None:
        _ = self.registry.write_text(
            "targets=laptop,builder\nkind.laptop=ssh\nplatform.laptop=macos\nsettings.laptop=mac.conf\n"
            + "kind.builder=local\nplatform.builder=linux\n"
            + "sync.hosts.laptop=Mac\nsync.hosts.builder=natedev\n"
            + "# whole: 0..10\nvault.lock_retries=5\n"
            + "# range: 0..5\nvault.lock_retry_delay_s=3\n"
        )
        self.write(
            call("remote", target="laptop", mac="passed", mac_s=5),
            call("local", target="builder", mac="passed", mac_s=5),
            call("aws", target="aws", mac="passed", mac_s=5),
        )

        with closing(index.read_only()) as connection:
            selected = report.reported_mac_calls(connection, local_day(STAMP))

        self.assertEqual({item.call_id for item in selected}, {"remote", "local"})


if __name__ == "__main__":
    _ = unittest.main()
