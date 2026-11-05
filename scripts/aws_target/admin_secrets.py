"""Keep administrator credentials encrypted between terminal deployment runs."""
from __future__ import annotations

from collections.abc import Sequence
from configparser import ConfigParser, Error as ConfigError
from dataclasses import asdict, dataclass
from io import StringIO
import json
import os
from pathlib import Path
import stat
import subprocess
import termios
from typing import Protocol, cast

from scripts.lib import conf
from scripts.lib.atomic_file import write_bytes_atomic


class SecretsRefused(ValueError):
    """The terminal or encrypted credentials do not permit administration."""


@dataclass(frozen=True, repr=False)
class AdminCredentials:
    aws_access_key_id: str
    aws_secret_access_key: str
    tailscale_client_id: str
    tailscale_client_secret: str

    def secret_values(self) -> tuple[str, ...]:
        return (self.aws_access_key_id, self.aws_secret_access_key, self.tailscale_client_id, self.tailscale_client_secret)

    @classmethod
    def decode(cls, content: bytes) -> AdminCredentials:
        try:
            value = cast(object, json.loads(content))
            if not isinstance(value, dict):
                raise ValueError
            values = cast(dict[str, object], value)
            names = tuple(cls.__dataclass_fields__)
            if set(values) != set(names) or any(not isinstance(values[name], str) or not values[name] for name in names):
                raise ValueError
            return cls(*(cast(str, values[name]) for name in names))
        except (ValueError, KeyError, TypeError):
            raise SecretsRefused('admin_secret_file: invalid credential fields') from None


@dataclass(frozen=True)
class LockedAdminCredentials:
    pass


def terminal() -> int:
    try:
        descriptor = os.open('/dev/tty', os.O_RDWR | os.O_NOCTTY)
        if not os.isatty(descriptor):
            os.close(descriptor)
            raise OSError
        return descriptor
    except OSError:
        raise SecretsRefused('run deploy.py in a terminal; no /dev/tty is available') from None


def read_hidden(prompt: str) -> str:
    descriptor = terminal()
    try:
        previous = termios.tcgetattr(descriptor)
        hidden = list(previous)
        hidden[3] = cast(int, hidden[3]) & ~termios.ECHO
        try:
            termios.tcsetattr(descriptor, termios.TCSAFLUSH, hidden)
            _ = os.write(descriptor, prompt.encode())
            with os.fdopen(os.dup(descriptor), 'r') as stream:
                value = stream.readline()
            if not value:
                raise SecretsRefused('terminal input ended; credentials were not changed')
            return value.rstrip('\r\n')
        finally:
            termios.tcsetattr(descriptor, termios.TCSAFLUSH, previous)
            _ = os.write(descriptor, b'\n')
    except termios.error:
        raise SecretsRefused('the terminal cannot hide input; credentials were not changed') from None
    finally:
        os.close(descriptor)


def crypt(content: bytes, passphrase: str, *, encrypt: bool) -> bytes:
    """Send the passphrase through a private descriptor and data through stdin."""
    if not passphrase or '\n' in passphrase or len(passphrase.encode()) > 4095:
        raise SecretsRefused('use a nonempty passphrase of at most 4095 bytes without a newline')
    reader, writer = os.pipe()
    try:
        _ = os.write(writer, (passphrase + '\n').encode())
        os.close(writer)
        writer = -1
        arguments = ['gpg', '--no-options', '--batch', '--no-tty', '--no-symkey-cache',
                     '--pinentry-mode', 'loopback', '--passphrase-fd', str(reader), '--output', '-']
        arguments += ['--symmetric', '--cipher-algo', 'AES256'] if encrypt else ['--decrypt']
        try:
            result = subprocess.run(arguments, input=content, capture_output=True, pass_fds=(reader,), check=False)
        except (OSError, subprocess.SubprocessError):
            raise SecretsRefused('admin_secret_file: gpg failed') from None
        if result.returncode:
            raise SecretsRefused('admin_secret_file: encryption failed' if encrypt else
                                 'admin_secret_file: decryption failed; check the passphrase')
        return result.stdout
    finally:
        os.close(reader)
        if writer != -1:
            os.close(writer)


def secret_path(settings: Path) -> Path:
    path = Path(conf.read(settings).text('admin_secret_file')).expanduser().resolve()
    if path.is_relative_to(Path(__file__).resolve().parents[2]):
        raise SecretsRefused(f'admin_secret_file: {path}: keep encrypted credentials outside the repository')
    return path


def credentials_file() -> Path:
    value = os.environ.get('AWS_SHARED_CREDENTIALS_FILE', '')
    if not value:
        raise SecretsRefused('AWS_SHARED_CREDENTIALS_FILE must name the private scaler credential file')
    return Path(value).expanduser()


def read_profiles(path: Path) -> ConfigParser:
    profiles = ConfigParser(interpolation=None)
    try:
        if path.exists():
            _ = profiles.read_string(path.read_text())
    except (OSError, ConfigError):
        raise SecretsRefused('the private credential file cannot be read') from None
    return profiles


def refuse_plain_admin(settings: Path) -> None:
    path = secret_path(settings)
    credential_path = credentials_file()
    profiles = read_profiles(credential_path)
    profile = conf.read(settings).text('deployment_profile')
    if profiles.has_section('build-admin') or profiles.has_section(profile) or (path.parent / 'tailscale-oauth').exists():
        raise SecretsRefused('plain administrator credentials remain; run deploy.py secrets in a terminal')


class AdminSecretSession:
    """Decrypt once and share credentials across this deployment's stages."""

    def __init__(self, settings: Path) -> None:
        self.settings: Path = settings
        self.credentials: LockedAdminCredentials | AdminCredentials = LockedAdminCredentials()

    def get(self) -> AdminCredentials:
        refuse_plain_admin(self.settings)
        if isinstance(self.credentials, LockedAdminCredentials):
            path = secret_path(self.settings)
            try:
                mode = path.stat().st_mode
                if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
                    raise OSError
                content = path.read_bytes()
            except OSError:
                raise SecretsRefused(f'admin_secret_file: {path}: require an encrypted file, mode 0600; run deploy.py secrets') from None
            self.credentials = AdminCredentials.decode(crypt(content, read_hidden('Admin passphrase: '), encrypt=False))
        return self.credentials

    def environment(self) -> dict[str, str]:
        values = self.get()
        environment = dict(os.environ)
        # Explicit long-lived keys prevent an inherited session token from changing IAM authorization.
        for name in ('AWS_SESSION_TOKEN', 'AWS_SECURITY_TOKEN'):
            if environment.get(name):
                raise SecretsRefused('administrator calls require long-lived keys without a session token')
        for name in ('AWS_PROFILE', 'AWS_DEFAULT_PROFILE'):
            if name in environment:
                del environment[name]
        environment['AWS_ACCESS_KEY_ID'] = values.aws_access_key_id
        environment['AWS_SECRET_ACCESS_KEY'] = values.aws_secret_access_key
        environment['AWS_EC2_METADATA_DISABLED'] = 'true'
        # Ignore CLI history and plugin settings while administrator responses contain secrets.
        environment['AWS_CONFIG_FILE'] = '/dev/null'
        environment['AWS_CLI_AUTO_PROMPT'] = 'off'
        environment['AWS_PAGER'] = ''
        return environment

    def redact(self, message: str) -> str:
        if isinstance(self.credentials, AdminCredentials):
            for value in self.credentials.secret_values():
                message = message.replace(value, '[redacted]')
        return message

    def executor(self, execute: AdminExecutor) -> AdminExecutor:
        def authorized(arguments: Sequence[str], *, capture_output: bool, text: bool,
                       timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            if env is not None:
                raise SecretsRefused('administrator credentials require their own subprocess environment')
            command = list(arguments)
            if '--profile' in command:
                index = command.index('--profile')
                del command[index:index + 2]
            environment = self.environment()
            try:
                result = execute(command, capture_output=capture_output, text=text, timeout=timeout,
                                 check=check, env=environment)
            except subprocess.TimeoutExpired:
                raise subprocess.TimeoutExpired('administrator AWS command', timeout) from None
            except subprocess.CalledProcessError as error:
                raise subprocess.CalledProcessError(error.returncode, 'administrator AWS command',
                                                     stderr=self.redact(str(cast(object, error.stderr)))) from None
            except (OSError, subprocess.SubprocessError):
                raise SecretsRefused('administrator AWS command failed') from None
            # AWS errors can contain caller input. Never publish their raw text.
            if result.returncode:
                return subprocess.CompletedProcess(command, result.returncode, '', self.redact(result.stderr))
            return result
        return authorized


# Keep the executor type local to avoid importing the controller through its callers.


class AdminExecutor(Protocol):
    def __call__(self, arguments: Sequence[str], *, capture_output: bool, text: bool,
                 timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]: ...


def configure(settings: Path) -> None:
    """Import or replace credentials without displaying their values."""
    descriptor = terminal()
    os.close(descriptor)
    path = secret_path(settings)
    credential_path = credentials_file()
    profiles = read_profiles(credential_path)
    profile = conf.read(settings).text('deployment_profile')
    if profiles.get(profile, 'aws_session_token', fallback=''):
        raise SecretsRefused('import requires long-lived administrator keys without a session token')
    previous: dict[str, str] = {}
    if path.exists():
        previous = asdict(AdminCredentials.decode(crypt(path.read_bytes(), read_hidden('Current admin passphrase: '), encrypt=False)))
    elif profiles.has_section(profile):
        previous['aws_access_key_id'] = profiles.get(profile, 'aws_access_key_id', fallback='')
        previous['aws_secret_access_key'] = profiles.get(profile, 'aws_secret_access_key', fallback='')
    legacy = path.parent / 'tailscale-oauth'
    if legacy.exists():
        try:
            value = cast(dict[str, str], json.loads(legacy.read_text()))
            for name in ('client_id', 'client_secret'):
                if 'tailscale_' + name not in previous:
                    previous['tailscale_' + name] = value[name]
        except (OSError, ValueError, KeyError, TypeError):
            raise SecretsRefused('the legacy OAuth file cannot be imported') from None
    values: dict[str, str] = {}
    for name in AdminCredentials.__dataclass_fields__:
        hint = ' (Enter keeps the stored or imported value)' if previous.get(name) else ''
        values[name] = read_hidden(name + hint + ': ') or previous.get(name, '')
    credentials = AdminCredentials.decode(json.dumps(values).encode())
    passphrase = read_hidden('New admin passphrase: ')
    if passphrase != read_hidden('Repeat admin passphrase: '):
        raise SecretsRefused('passphrases differ; credentials were not changed')
    encrypted = crypt(json.dumps(asdict(credentials)).encode(), passphrase, encrypt=True)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_bytes_atomic(path, encrypted, mode=0o600)
    removed = profiles.remove_section(profile)
    if removed or not credential_path.exists():
        content = StringIO()
        profiles.write(content)
        write_bytes_atomic(credential_path, content.getvalue().encode(), mode=0o600)
    legacy.unlink(missing_ok=True)
    print('Encrypted administrator credentials saved; plain administrator credentials removed.')
