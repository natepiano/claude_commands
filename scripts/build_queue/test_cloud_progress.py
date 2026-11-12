"""Cloud progress uses private files and named missing states."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.build_queue import cloud_progress as cloud, progress_profile as progress
from scripts.build_queue import snapshot_codec as codec


class CloudProgressTests(unittest.TestCase):
    def test_stage_and_location_round_trip_with_the_existing_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sidecar = Path(temporary) / 'gate.cgroup'
            expected = cloud.CloudProgress('running', 32, 0, 100, stage='round 2 of 3 base',
                                           region='us-east-2', zone='us-east-2b')
            cloud.publish(sidecar, expected)
            self.assertEqual(cloud.read(sidecar), expected)

    def test_older_files_use_empty_defaults_for_the_new_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sidecar = Path(temporary) / 'gate.cgroup'
            for names in (('stage', 'region', 'zone'), ('region', 'zone')):
                with self.subTest(missing=names):
                    record = dict(kind='CloudProgress', state='running', size_vcpus=32, failed_modules=0,
                                  updated_at=100, stage='round 2 of 3 base', region='us-east-2', zone='us-east-2b')
                    for name in names:
                        _ = record.pop(name)
                    _ = Path(str(sidecar) + '.cloud.json').write_text(json.dumps(record))
                    self.assertEqual(cloud.read(sidecar), cloud.CloudProgress('running', 32, 0, 100,
                        stage='' if 'stage' in names else 'round 2 of 3 base'))

    def test_the_previous_field_list_accepts_new_record_fields(self) -> None:
        @dataclass(frozen=True)
        class CloudProgress:
            state: cloud.CloudState
            size_vcpus: int
            failed_modules: int
            updated_at: float
        record = codec.snapshot_value(cloud.CloudProgress('running', 32, 0, 100, stage='round 2 of 3 base',
                                                         region='us-east-2', zone='us-east-2b'))
        self.assertEqual(codec.parse_snapshot_value(record, CloudProgress), CloudProgress('running', 32, 0, 100))

    def test_each_state_round_trips_beside_the_existing_module_counter(self) -> None:
        states: tuple[cloud.CloudState, ...] = ("requesting", "setup", "running", "uploading")
        with tempfile.TemporaryDirectory() as temporary:
            sidecar = Path(temporary) / "gate.cgroup"
            _ = sidecar.write_text("private.scope\n")
            counter = progress.ProgressCounter("modules", 7, 12)
            progress.publish_counter(sidecar, counter)
            for state in states:
                with self.subTest(state=state):
                    expected = cloud.CloudProgress(state, 32, 2, 1700000000.25)
                    cloud.publish(sidecar, expected)
                    self.assertEqual(cloud.read(sidecar), expected)
                    self.assertTrue(Path(str(sidecar) + ".cloud.json").is_file())
                    self.assertEqual(progress.read_counter(sidecar), counter)
                    self.assertEqual(sidecar.read_text(), "private.scope\n")

    def test_missing_file_returns_the_named_missing_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = cloud.read(Path(temporary) / "absent.cgroup")
            self.assertEqual(result, cloud.NoCloudProgress("missing"))

    def test_broken_file_returns_the_named_unreadable_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sidecar = Path(temporary) / "gate.cgroup"
            path = Path(str(sidecar) + ".cloud.json")
            for content in (b"{", b"null", b"[]", b"{}", b"\xff", b'{"state":"unknown"}'):
                with self.subTest(content=content):
                    _ = path.write_bytes(content)
                    self.assertEqual(cloud.read(sidecar), cloud.NoCloudProgress("unreadable"))

    def test_unreadable_path_returns_the_named_unreadable_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sidecar = Path(temporary) / "gate.cgroup"
            Path(str(sidecar) + ".cloud.json").mkdir()
            self.assertEqual(cloud.read(sidecar), cloud.NoCloudProgress("unreadable"))

    def test_huge_update_time_returns_the_named_unreadable_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sidecar = Path(temporary) / "gate.cgroup"
            _ = Path(str(sidecar) + ".cloud.json").write_text(json.dumps({
                "kind": "CloudProgress", "state": "running", "size_vcpus": 32,
                "failed_modules": 0, "updated_at": 10 ** 1000}))
            self.assertEqual(cloud.read(sidecar), cloud.NoCloudProgress("unreadable"))

    def test_publish_replaces_a_complete_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sidecar = Path(temporary) / "gate.cgroup"
            path = Path(str(sidecar) + ".cloud.json")
            first = cloud.CloudProgress("setup", 16, 0, 100)
            second = cloud.CloudProgress("running", 32, 1, 105)
            cloud.publish(sidecar, first)
            replace = os.replace

            def replace_complete(source: Path, destination: Path) -> None:
                self.assertEqual(destination, path)
                self.assertEqual(cloud.read(sidecar), first)
                self.assertEqual(json.loads(source.read_text())["state"], "running")
                replace(source, destination)

            with mock.patch("scripts.lib.atomic_file.os.replace", side_effect=replace_complete) as publication:
                cloud.publish(sidecar, second)
            publication.assert_called_once()
            self.assertEqual(cloud.read(sidecar), second)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_failed_replace_preserves_the_previous_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sidecar = Path(temporary) / "gate.cgroup"
            first = cloud.CloudProgress("running", 16, 0, 100)
            cloud.publish(sidecar, first)
            with mock.patch("scripts.lib.atomic_file.os.replace", side_effect=OSError("private replace failure")):
                with self.assertRaises(OSError):
                    cloud.publish(sidecar, cloud.CloudProgress("uploading", 16, 0, 105))
            self.assertEqual(cloud.read(sidecar), first)
            self.assertEqual([path.name for path in sidecar.parent.iterdir()], [sidecar.name + ".cloud.json"])


if __name__ == "__main__":
    _ = unittest.main()
