"""Check region configuration and deployment migration with private files."""
from __future__ import annotations

import json
from pathlib import Path
from unittest import TestCase
import tempfile

from scripts.aws_target.config import gate_deployment, gate_regions
from scripts.lib import conf


class GateRegionTests(TestCase):
    def test_region_order_starts_at_home_without_repeated_entries(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'cloud_gate.conf'
            for value, home, valid in (
                ('us-east-2 us-west-2', 'us-east-2', True), ('us-east-2', 'us-east-2', True),
                ('us-west-2 us-east-2', 'us-east-2', False), ('us-east-2 us-east-2', 'us-east-2', False),
                ('us-east-2 us-east-1', 'us-east-2', True), ('us-east-2 us-west-2', 'us-west-2', False),
                ('us-west-2 us-east-2', 'us-west-2', True), ('eu-west-1 ap-south-1', 'eu-west-1', True),
                ('eu-west-1', 'eu-west-1', True), ('', 'us-east-2', False)):
                with self.subTest(value=value, home=home):
                    _ = path.write_text('regions=' + value + '\n')
                    if valid:
                        self.assertEqual(gate_regions(home, conf.read(path)), tuple(value.split()))
                    else:
                        with self.assertRaises(ValueError):
                            _ = gate_regions(home, conf.read(path))

    def test_old_deployment_migrates_once_and_preserves_shared_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'gate-deployment.json'
            old: dict[str, object] = {'region': 'us-east-2', 'image': 'ami-fixture', 'subnets': [], 'template': 'lt-fixture',
                   'version': '1', 'security_group': 'sg-fixture', 'bucket': 'fixture-bucket',
                   'revision': 'fixture-revision', 'artifact_prefix': 'artifacts/', 'deployment_id': 'fixture'}
            _ = path.write_text(json.dumps(old))
            migrated = gate_deployment(path, 'us-east-2')
            self.assertNotIn('image', migrated)
            self.assertEqual(migrated['bucket'], old['bucket'])
            self.assertEqual(migrated['regions'], [{key: old[key] for key in
                ('region', 'image', 'subnets', 'template', 'version', 'security_group')}])
            stamp = path.stat().st_mtime_ns
            self.assertEqual(gate_deployment(path, 'us-east-2'), migrated)
            self.assertEqual(path.stat().st_mtime_ns, stamp)
