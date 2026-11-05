"""Test terminal encryption and administrator subprocess boundaries without network calls."""
from __future__ import annotations

from collections.abc import Sequence
from configparser import ConfigParser
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from typing import cast, final, override
from unittest import mock

from scripts.aws_target import admin_secrets, box, deploy


VALUES = admin_secrets.AdminCredentials('test-admin-id', 'test-admin-secret', 'test-client-id', 'test-client-secret')


@final
class AdminSecretTests(unittest.TestCase):
    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.root = Path()
        self.home = Path()
        self.path = Path()
        self.settings = Path()
        self.credentials = Path()

    @override
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'gpg'
        self.home.mkdir(mode=0o700)
        self.path = self.root / 'private/admin.gpg'
        self.settings = self.root / 'aws_target.conf'
        self.credentials = self.root / 'credentials'
        _ = self.credentials.write_text('[build-scaler]\naws_access_key_id=scaler-id\naws_secret_access_key=scaler-secret\n')
        self.credentials.chmod(0o600)
        source = (Path(__file__).resolve().parents[2] / 'config/aws_target.conf').read_text()
        replacements = {'admin_secret_file': str(self.path), 'deployment_price_file': str(self.root / 'price')}
        _ = self.settings.write_text('\n'.join(key + '=' + replacements[key] if '=' in line and
            (key := line.partition('=')[0]) in replacements else line for line in source.splitlines()) + '\n')
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {'GNUPGHOME': str(self.home),
            'AWS_SHARED_CREDENTIALS_FILE': str(self.credentials), 'AWS_SESSION_TOKEN': '', 'AWS_SECURITY_TOKEN': ''})))

    def save(self) -> None:
        self.path.parent.mkdir(mode=0o700)
        _ = self.path.write_bytes(admin_secrets.crypt(json.dumps(asdict(VALUES)).encode(), 'test-passphrase', encrypt=True))
        self.path.chmod(0o600)

    def test_symmetric_round_trip_uses_the_supplied_passphrase_without_agent_cache(self) -> None:
        self.save()
        ciphertext = self.path.read_bytes()
        for value in VALUES.secret_values():
            self.assertNotIn(value.encode(), ciphertext)
        self.assertEqual(admin_secrets.AdminCredentials.decode(admin_secrets.crypt(ciphertext, 'test-passphrase', encrypt=False)), VALUES)
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            with self.assertRaisesRegex(admin_secrets.SecretsRefused, 'decryption failed'):
                _ = admin_secrets.crypt(ciphertext, 'wrong-passphrase', encrypt=False)
        self.assertEqual(output.getvalue() + errors.getvalue(), '')

    def test_missing_terminal_refuses_without_using_stdin_or_gpg(self) -> None:
        self.save()
        with mock.patch.object(os, 'open', side_effect=OSError), \
             mock.patch.object(subprocess, 'run') as command:
            with self.assertRaisesRegex(admin_secrets.SecretsRefused, 'run deploy.py in a terminal'):
                _ = admin_secrets.AdminSecretSession(self.settings).get()
            command.assert_not_called()

    def test_one_prompt_serves_the_price_and_tailscale_stages_without_a_profile(self) -> None:
        self.save()
        calls: list[list[str]] = []
        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                    check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            self.assertTrue(capture_output and text)
            self.assertGreater(timeout, 0)
            self.assertFalse(check)
            self.assertIsNotNone(env)
            if env is None:
                self.fail('administrator subprocess has no credentials')
            self.assertEqual(env['AWS_ACCESS_KEY_ID'], VALUES.aws_access_key_id)
            self.assertEqual(env['AWS_SECRET_ACCESS_KEY'], VALUES.aws_secret_access_key)
            self.assertEqual(env['AWS_CONFIG_FILE'], '/dev/null')
            self.assertEqual(env['AWS_PAGER'], '')
            self.assertNotIn('--profile', arguments)
            self.assertNotIn(VALUES.aws_secret_access_key, arguments)
            calls.append(list(arguments))
            return subprocess.CompletedProcess(arguments, 0, '{"SpotPriceHistory":[{"SpotPrice":"0.1"}]}', '')
        subject = deploy.Deployment(self.settings, self.root / 'state', self.root / 'bundle', execute=execute)
        requests: list[str] = []
        def transport(method: str, endpoint: str, body: bytes, headers: dict[str, str], timeout: float) -> box.PolicyReply:
            self.assertGreater(timeout, 0)
            self.assertTrue(method and headers)
            requests.append(endpoint)
            if endpoint == 'oauth/token':
                self.assertIn(VALUES.tailscale_client_secret.encode(), body)
                return box.PolicyReply({'access_token': 'test-token'})
            return box.PolicyReply({})
        output, errors = StringIO(), StringIO()
        with mock.patch.object(admin_secrets, 'read_hidden', return_value='test-passphrase') as prompt, \
             redirect_stdout(output), redirect_stderr(errors):
            _ = subject.price()
            _ = box.Tailnet(self.settings, subject.admin, transport).call('GET', 'acl')
            _ = subject.price()
            prompt.assert_called_once()
        self.assertEqual(len(calls), 2)
        self.assertEqual(requests, ['oauth/token', 'tailnet/-/acl'])
        for value in VALUES.secret_values():
            self.assertNotIn(value, output.getvalue() + errors.getvalue())
            for path in self.root.rglob('*'):
                if path.is_file():
                    self.assertNotIn(value.encode(), path.read_bytes())

    def test_plain_admin_or_oauth_refuses_before_a_prompt_or_aws_request(self) -> None:
        for name in ('profile', 'oauth'):
            with self.subTest(source=name):
                if name == 'profile':
                    _ = self.credentials.write_text('[build-admin]\naws_access_key_id=import-id\naws_secret_access_key=import-secret\n')
                else:
                    _ = self.credentials.write_text('')
                    self.path.parent.mkdir(exist_ok=True)
                    _ = (self.path.parent / 'tailscale-oauth').write_text('{}')
                with mock.patch.object(admin_secrets, 'read_hidden') as prompt:
                    with self.assertRaisesRegex(admin_secrets.SecretsRefused, 'deploy.py secrets'):
                        _ = deploy.Deployment(self.settings, self.root / 'state', self.root / 'bundle').up(approve_resources=True)
                    prompt.assert_not_called()

    def test_secrets_import_removes_only_admin_and_keeps_scaler_after_encryption(self) -> None:
        _ = self.credentials.write_text('[build-admin]\naws_access_key_id=test-admin-id\naws_secret_access_key=test-admin-secret\n'
                                       + '[build-scaler]\naws_access_key_id=scaler-id\naws_secret_access_key=scaler-secret\n')
        output, errors = StringIO(), StringIO()
        descriptor = os.open('/dev/null', os.O_RDONLY)
        with mock.patch.object(admin_secrets, 'terminal', return_value=descriptor), \
             mock.patch.object(admin_secrets, 'read_hidden', side_effect=['', '', 'test-client-id', 'test-client-secret', 'test-passphrase', 'test-passphrase']), \
             redirect_stdout(output), redirect_stderr(errors):
            admin_secrets.configure(self.settings)
        profiles = ConfigParser()
        _ = profiles.read(self.credentials)
        self.assertEqual(profiles.sections(), ['build-scaler'])
        self.assertEqual(profiles.get('build-scaler', 'aws_secret_access_key'), 'scaler-secret')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.credentials.stat().st_mode & 0o777, 0o600)
        self.assertEqual(admin_secrets.AdminCredentials.decode(admin_secrets.crypt(self.path.read_bytes(), 'test-passphrase', encrypt=False)), VALUES)
        for value in VALUES.secret_values():
            self.assertNotIn(value, output.getvalue() + errors.getvalue())

    def test_secret_replacement_keeps_the_other_encrypted_values(self) -> None:
        self.save()
        descriptor = os.open('/dev/null', os.O_RDONLY)
        with mock.patch.object(admin_secrets, 'terminal', return_value=descriptor), \
             mock.patch.object(admin_secrets, 'read_hidden', side_effect=['test-passphrase', '', '', '', 'replacement-client-secret', 'new-passphrase', 'new-passphrase']), \
             redirect_stdout(StringIO()):
            admin_secrets.configure(self.settings)
        values = admin_secrets.AdminCredentials.decode(admin_secrets.crypt(self.path.read_bytes(), 'new-passphrase', encrypt=False))
        self.assertEqual(values.aws_access_key_id, VALUES.aws_access_key_id)
        self.assertEqual(values.aws_secret_access_key, VALUES.aws_secret_access_key)
        self.assertEqual(values.tailscale_client_id, VALUES.tailscale_client_id)
        self.assertEqual(values.tailscale_client_secret, 'replacement-client-secret')

    def test_admin_timeout_keeps_its_type_without_command_output_or_credentials(self) -> None:
        self.save()
        session = admin_secrets.AdminSecretSession(self.settings)
        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                    check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            self.assertTrue(capture_output and text)
            self.assertFalse(check)
            self.assertIsNotNone(env)
            raise subprocess.TimeoutExpired(arguments, timeout, output=VALUES.aws_secret_access_key,
                                            stderr=VALUES.tailscale_client_secret)
        with mock.patch.object(admin_secrets, 'read_hidden', return_value='test-passphrase'):
            with self.assertRaises(subprocess.TimeoutExpired) as failure:
                _ = session.executor(execute)(['aws', '--profile', 'build-admin'], capture_output=True,
                                             text=True, timeout=1, check=False)
        self.assertIsNone(cast(object, failure.exception.output))
        self.assertIsNone(failure.exception.stderr)
        self.assertEqual(cast(object, failure.exception.cmd), 'administrator AWS command')

    def test_admin_errors_redact_values_and_reject_inherited_session_tokens(self) -> None:
        self.save()
        session = admin_secrets.AdminSecretSession(self.settings)
        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                    check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            self.assertTrue(capture_output and text and timeout > 0)
            self.assertFalse(check)
            self.assertIsNotNone(env)
            return subprocess.CompletedProcess(arguments, 1, '', VALUES.aws_secret_access_key)
        with mock.patch.object(admin_secrets, 'read_hidden', return_value='test-passphrase'):
            result = session.executor(execute)(['aws', '--profile', 'build-admin'], capture_output=True, text=True, timeout=1, check=False)
            self.assertEqual(result.stderr, '[redacted]')
            with mock.patch.dict(os.environ, {'AWS_SESSION_TOKEN': 'test-session-token'}):
                with self.assertRaisesRegex(admin_secrets.SecretsRefused, 'without a session token'):
                    _ = session.environment()


if __name__ == '__main__':
    _ = unittest.main()
