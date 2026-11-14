"""Verify preparation, explicit approval, and isolated stack setup with private fakes."""
from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.aws_target import aws_cli, deploy, hana_box, hana_spend, ledger, remote
from scripts.aws_target.control_test_support import TestClock
from scripts.aws_target.config import DeploymentSettings
from scripts.aws_target.ledger_test_support import CostFixture, text_setting
from scripts.aws_target.test_hana_provider import FakeAws, IDENTITY, REPOSITORY
from scripts.lib import conf


class HanaSetupTests(CostFixture):
    workload: Path = Path()
    clock: TestClock = TestClock()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.clock = TestClock()
        self.workload = self.root / 'hana_aws.conf'
        _ = self.workload.write_text((REPOSITORY / 'config/hana_aws.conf').read_text())
        for key, value in {'ami_id': 'fixture-image', 'ami_owner_id': '000000000000',
                           'vpc_id': 'fixture-vpc', 'subnet_id': 'fixture-subnet'}.items():
            text_setting(self.workload, key, value)
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(self.clock.now, (), 'private setup inventory', True))
        book.observe(ledger.InstanceObservation(self.clock.now, 'terminated', 'private setup account'))
        original = conf.read_or_repository
        def configuration(name: str) -> conf.ConfigRead:
            return conf.read(self.workload) if name == 'hana_aws' else original(name)
        _ = self.enterContext(mock.patch.object(conf, 'read_or_repository', side_effect=configuration))
        original_path = conf.path
        def config_path(name: str) -> Path:
            return self.workload if name == 'hana_aws' else original_path(name)
        _ = self.enterContext(mock.patch.object(conf, 'path', side_effect=config_path))

    def subject(self, fake: FakeAws) -> deploy.Deployment:
        return deploy.Deployment(self.settings, self.state, self.root / 'output', execute=fake, clock=self.clock, notify=lambda _event: True)

    def test_prepare_is_local_and_saves_separate_configuration_evidence(self) -> None:
        fake = FakeAws(self.clock)
        subject = self.subject(fake)
        # The existing validator reads account metadata before Hana replaces resource inputs.
        with mock.patch.object(deploy, 'read_deployment_settings') as read:
            read.return_value = self.base_settings()
            prepared = subject.prepare_hana()
            again = subject.prepare_hana()
        self.assertEqual(prepared.fingerprint, again.fingerprint)
        self.assertEqual(fake.calls, [])
        settings = hana_spend.read_settings(conf.read(self.workload))
        self.assertEqual(prepared.parameters['RootGiB'], str(settings.root_gib))
        self.assertEqual(prepared.parameters['DataGiB'], str(settings.data_gib))
        self.assertEqual(prepared.parameters['SpotMaxPrice'], str(settings.spot_max_usd_per_hour))
        self.assertTrue((subject.output / 'hana/preflight.json').exists())
        self.assertFalse((subject.output / 'preflight.json').exists())
        self.assertFalse(settings.enabled)

    def base_settings(self) -> DeploymentSettings:
        return DeploymentSettings('us-east-2', 'fixture-admin', 'fixture-gates', 'fixture-type', 'fixture-image',
            '000000000000', 'fixture-release', 'us-east-2b', 'fixture-vpc', 'fixture-subnet', '', '/dev/xvda',
            1, 1, 1, 1, hana_spend.read_settings(conf.read(self.workload)).spot_max_usd_per_hour,
            1, 2, 3, conf.read(self.workload).whole('aws_timeout_s'),
            conf.read(self.settings).whole('preflight_max_age_s'), 'fixture-scaler',
            'https://example.invalid/scripts', 'fixture-commit', 'fixture-hash', self.root / 'bootstrap')

    def test_setup_requires_current_preparation_before_any_aws_call(self) -> None:
        fake = FakeAws(self.clock)
        subject = self.subject(fake)
        with mock.patch.object(deploy, 'read_deployment_settings', return_value=self.base_settings()), \
                mock.patch.object(subject, '_authorize'), mock.patch('sys.stdout', new_callable=StringIO):
            _ = subject.prepare_hana()
            data_gib = conf.read(self.workload).whole('data_gib')
            _ = self.workload.write_text(self.workload.read_text().replace(f'data_gib={data_gib}\n', f'data_gib={data_gib + 1}\n'))
            with self.assertRaisesRegex(deploy.DeploymentRefused, 'preparation expired|inputs changed'):
                _ = subject.hana_setup(approve_resources=True)
        self.assertEqual(fake.calls, [])

    def test_setup_reserves_costs_before_creating_resources_and_registers_only_after_stop(self) -> None:
        fake = FakeAws(self.clock, state='running')
        subject = self.subject(fake)
        original_call = fake.__call__
        calls: list[str] = []
        def api_call(operation: str, *arguments: str, service: str = 'ec2', timeout_s: float | None = None) -> dict[str, object]:
            calls.append(operation)
            if operation == 'deploy':
                evidence = ledger.CostLedger(self.state).read_snapshot()
                self.assertTrue(any(isinstance(row.state, ledger.HanaExposureOpen) for row in evidence.hana_compute))
                self.assertTrue(evidence.hana_storage)
                self.assertEqual(service, 'cloudformation')
                self.assertIn('hana-build', arguments)
                return {}
            if operation == 'describe-stacks':
                outputs = {'Instance': IDENTITY.instance, 'ControlBucket': 'fixture-bucket', 'DataVolume': 'fixture-volume'}
                return {'Stacks': [{'StackStatus': 'CREATE_COMPLETE', 'Outputs': [
                    {'OutputKey': key, 'OutputValue': value} for key, value in outputs.items()]}]}
            import subprocess
            result = original_call(['aws', '--profile', 'fixture-admin', '--region', IDENTITY.region,
                service, operation, *arguments, '--output', 'json'], capture_output=True, text=True,
                timeout=conf.read(self.workload).whole('aws_timeout_s') if timeout_s is None else timeout_s, check=False)
            if result.returncode:
                raise subprocess.CalledProcessError(result.returncode, 'fake')
            return ledger.mapping(cast(object, json.loads(result.stdout)))
        with mock.patch.object(deploy, 'read_deployment_settings', return_value=self.base_settings()), \
                mock.patch.object(subject, '_authorize'), mock.patch.object(subject, '_validate_inputs') as validate, \
                mock.patch.object(aws_cli.AwsCli, 'call', side_effect=api_call), \
                mock.patch('sys.stdout', new_callable=StringIO):
            prepared = subject.prepare_hana()
            saved_path = self.state / 'hana/setup.json'
            saved = remote.read_record(saved_path)
            saved['deployment'] = IDENTITY.deployment
            _ = saved_path.write_text(json.dumps(saved))
            # Refresh preparation for the chosen fake deployment identity.
            _ = subject.prepare_hana()
            self.assertTrue(subject.hana_setup(approve_resources=True))
            validate.assert_called_once()
        record = remote.read_record(self.state / 'hana/lifecycle.json')
        self.assertEqual(record['state'], hana_box.Lifecycle.IDLE_STOPPED)
        self.assertEqual(ledger.mapping(record['identity'])['spot_request'], IDENTITY.spot_request)
        self.assertIn('stop-instances', calls)
        self.assertNotIn('start-instances', calls)
        self.assertFalse((self.state / 'gate-deployment.json').exists())
        self.assertEqual(conf.read(self.workload).text('enabled'), 'off')
        self.assertEqual(prepared.parameters['InstanceType'], conf.read(self.workload).text('instance_type'))

    def test_setup_reservation_does_not_enable_start_or_command_permission(self) -> None:
        settings = hana_spend.read_settings(conf.read(self.workload))
        spending = hana_spend.HanaSpending(ledger.CostLedger(self.state), self.settings, settings)
        reservation = spending.reserve_setup(self.clock.now, self.clock.now + settings.permit_max_seconds)
        self.assertIsInstance(reservation, hana_spend.StartReserved)
        assert isinstance(reservation, hana_spend.StartReserved)
        self.assertIsInstance(spending.check_start(self.clock.now, reservation.exposure.generation), hana_spend.PermitRefused)
        self.assertIsInstance(spending.bind_start(self.clock.now, reservation.exposure.generation, 'fixture-boot'), hana_spend.PermitRefused)

    def test_template_uses_retained_disks_persistent_spot_and_no_guest_credentials(self) -> None:
        template = remote.read_record(deploy.HANA_TEMPLATE)
        resources = ledger.mapping(template['Resources'])
        launch = ledger.mapping(ledger.mapping(ledger.mapping(resources['LaunchTemplate'])['Properties'])['LaunchTemplateData'])
        market = ledger.mapping(ledger.mapping(launch['InstanceMarketOptions'])['SpotOptions'])
        self.assertEqual(market['SpotInstanceType'], 'persistent')
        self.assertEqual(market['InstanceInterruptionBehavior'], 'stop')
        self.assertNotIn('IamInstanceProfile', launch)
        self.assertNotIn('KeyName', launch)
        self.assertEqual(ledger.mapping(resources['DataVolume'])['DeletionPolicy'], 'Retain')
        self.assertNotIn('AWS::IAM::User', deploy.HANA_TEMPLATE.read_text())
