#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Read live configuration, complete missing default values, and recover invalid values from Git."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from typing import TYPE_CHECKING, TypeVar

from scripts.lib import conf_reads


if TYPE_CHECKING:
    from decimal import Decimal


_warning_sink: ContextVar[Callable[[str], None] | None] = ContextVar("configuration_warning_sink", default=None)


def _print_warning(message: str) -> None:
    print(message, file=sys.stderr)


def _warning(message: str) -> None:
    sink = _warning_sink.get()
    if sink is None:
        _print_warning(message)
    else:
        sink(message)


@contextmanager
def diagnostics(*, report: bool = True) -> Generator[list[str]]:
    """Collect reader warnings for a reply, with optional local output."""
    messages: list[str] = []

    def collect(message: str) -> None:
        messages.append(message)
        if report:
            _print_warning(message)

    token = _warning_sink.set(collect)
    try:
        yield messages
    finally:
        _warning_sink.reset(token)


class Unusable(ValueError):
    """The named configuration key has no usable value."""


@dataclass(frozen=True)
class Setting:
    value: str
    limit: str
    line: int


Result = TypeVar("Result")


@dataclass(frozen=True)
class UsableLiveValue[Value]:
    value: Value


@dataclass(frozen=True)
class RecoveredValue[Value]:
    value: Value
    reason: str


@dataclass(frozen=True)
class UnavailableValue:
    reason: str


class _MissingKey(ValueError):
    """The file has no effective entry for the requested key."""


@dataclass(frozen=True)
class _UnreadableFile:
    reason: str
    missing: bool


class UseConfiguredValue(Enum):
    """Read current configuration when the call starts."""

    CURRENT = "current configuration"


@dataclass(frozen=True)
class ConfigRead:
    """Keep one live file read for the settings used in one pass."""

    file: str | Path
    source: Path
    settings: dict[str, Setting] | _UnreadableFile
    repository_allowed: bool

    def _resolve(self, key: str, convert: Callable[[str], Result], *, warn: bool
                 ) -> UsableLiveValue[Result] | RecoveredValue[Result] | UnavailableValue:
        bad = "<missing>"
        repository_allowed = False
        try:
            if isinstance(self.settings, _UnreadableFile):
                repository_allowed = self.settings.missing
                raise ValueError(self.settings.reason)
            setting = _setting(self.settings, key)
            bad = setting.value
            return UsableLiveValue(_converted(setting, convert))
        except (OSError, UnicodeError, ValueError, ArithmeticError) as error:
            repository_allowed = repository_allowed or isinstance(error, _MissingKey)
            reason = f"{self.source}: {key}={bad!r}: {error}"
        if repository_allowed and self.repository_allowed and isinstance(self.file, str):
            repository = _repository_path(self.file)
            if repository != self.source:
                try:
                    setting = _setting(_parse(repository.read_text(encoding="utf-8")), key)
                    return RecoveredValue(_converted(setting, convert), reason)
                except (OSError, UnicodeError, ValueError, ArithmeticError):
                    pass
        for committed in _committed_files(self.source):
            try:
                value = _converted(_setting(_parse(committed), key), convert)
            except (ValueError, ArithmeticError):
                continue
            if warn:
                _warning(f"warning: {reason}")
            return RecoveredValue(value, reason)
        if warn:
            _warning(f"warning: {reason}")
        return UnavailableValue(reason)

    def origin(self, key: str, convert: Callable[[str], Result]
               ) -> UsableLiveValue[Result] | RecoveredValue[Result] | UnavailableValue:
        """Distinguish live values from recovered values without a warning."""
        return self._resolve(key, convert, warn=False)

    def _value(self, key: str, convert: Callable[[str], Result]) -> Result:
        result = self._resolve(key, convert, warn=True)
        if isinstance(result, UnavailableValue):
            raise Unusable(f"{result.reason}; no usable committed value")
        return result.value

    def text(self, key: str) -> str:
        return self._value(key, str)

    def number(self, key: str) -> float:
        return self._value(key, _number)

    def whole(self, key: str) -> int:
        return self._value(key, _whole)

    def words(self, key: str) -> tuple[str, ...]:
        return self._value(key, lambda value: tuple(value.split()))

    def values(self) -> dict[str, str]:
        """Return the map, including empty entries that have no limits."""
        keys = self.settings
        if isinstance(keys, _UnreadableFile):
            fault = keys
            keys = self._recover_file(fault)
        result: dict[str, str] = {}
        for key in keys:
            setting = _setting(keys, key)
            if not setting.value and not setting.limit.startswith(("# whole:", "# range:", "# one of:")):
                result[key] = setting.value
                continue
            try:
                _limits(setting)
                result[key] = setting.value
            except ValueError:
                result[key] = self.text(key)
        return result

    def _recover_file(self, fault: _UnreadableFile) -> dict[str, Setting]:
        if fault.missing and self.repository_allowed and isinstance(self.file, str):
            try:
                return _parse(_repository_path(self.file).read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                pass
        for contents in _committed_files(self.source):
            _warning(f"warning: {self.source}: keys: {fault.reason}")
            return _parse(contents)
        _warning(f"warning: {self.source}: keys: {fault.reason}")
        raise Unusable(f"{self.source}: keys: {fault.reason}; no usable committed file")


def read(file: str | Path) -> ConfigRead:
    """Read and parse the live file once; resolve faults when a setting is used."""
    source = path(file)
    try:
        settings = _parse(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        return ConfigRead(file, source, _UnreadableFile(str(error), isinstance(error, FileNotFoundError)),
                          "CONF_DIR" not in os.environ)
    return ConfigRead(file, source, settings, "CONF_DIR" not in os.environ)


def read_or_repository(stem: str) -> ConfigRead:
    """Use the repository file when the fixture has no copy."""
    if "CONF_DIR" in os.environ and not path(stem).exists():
        return read(_repository_path(stem))
    return read(stem)


def directory() -> Path:
    """Return the live configuration folder, or the test fixture override."""
    return Path(os.environ.get("CONF_DIR") or "~/.claude/config").expanduser()


def path(file: str | Path) -> Path:
    conf_reads.record(file if isinstance(file, str) else file.stem)
    if isinstance(file, Path):
        return file.expanduser()
    if not file or Path(file).name != file or file.endswith(".conf"):
        raise Unusable(f"{file}: file: expected a configuration stem")
    return directory() / f"{file}.conf"


def _repository_path(stem: str) -> Path:
    """Return the repository configuration file for a validated stem."""
    conf_reads.record(stem)
    return Path(__file__).resolve().parents[2] / "config" / f"{stem}.conf"


def _parse(source: str) -> dict[str, Setting]:
    settings: dict[str, Setting] = {}
    previous = ""
    for index, raw in enumerate(source.splitlines()):
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("["):
            key, separator, value = line.partition("=")
            if separator:
                _ = settings.setdefault(key.strip(), Setting(value.strip(), previous, index))
        previous = raw.strip()
    return settings


def _setting(settings: dict[str, Setting], key: str) -> Setting:
    host = socket.gethostname().split(".", 1)[0]
    for candidate in (f"{key}.{host}", key):
        if candidate in settings:
            return settings[candidate]
    raise _MissingKey("key is missing")


def _limits(setting: Setting) -> None:
    if not setting.value.strip():
        raise ValueError("value is empty")
    limit = setting.limit
    if limit.startswith("# one of:"):
        choices = tuple(word.strip() for word in limit.removeprefix("# one of:").split(","))
        if setting.value not in choices:
            raise ValueError(f"expected one of {', '.join(choices)}")
    elif limit.startswith(("# whole:", "# range:")):
        form = "# whole:" if limit.startswith("# whole:") else "# range:"
        bounds = limit.removeprefix(form).strip().split("..")
        if len(bounds) != 2:
            raise ValueError(f"invalid limit {limit}")
        convert = _whole if form == "# whole:" else decimal
        value = convert(setting.value)
        low, high = (bound.strip() for bound in bounds)
        if (low and value < convert(low)) or (high and value > convert(high)):
            raise ValueError(f"outside range {low}..{high}")


def decimal(value: str) -> Decimal:
    from decimal import Decimal, InvalidOperation

    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("expected a finite number") from error
    if not result.is_finite() or not math.isfinite(float(result)):
        raise ValueError("expected a finite number")
    return result


def _number(value: str) -> float:
    return float(decimal(value))


def _whole(value: str) -> int:
    if not re.fullmatch(r"[+-]?[0-9]+", value):
        raise ValueError("expected a whole number")
    return int(value)


def _git(folder: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", "-C", str(folder), *arguments], capture_output=True, text=True, check=False)
    except (OSError, UnicodeError) as error:
        return subprocess.CompletedProcess(["git", *arguments], 1, "", str(error))


def _committed_files(source: Path) -> Iterator[str]:
    root = _git(source.parent, "rev-parse", "--show-toplevel")
    if root.returncode:
        return
    checkout = Path(root.stdout.strip())
    try:
        relative = source.resolve().relative_to(checkout.resolve()).as_posix()
    except ValueError:
        return
    history = _git(checkout, "log", "--format=%H", "--", relative)
    for revision in history.stdout.splitlines() if history.returncode == 0 else ():
        committed = _git(checkout, "show", f"{revision}:{relative}")
        if committed.returncode == 0:
            yield committed.stdout


def _converted(setting: Setting, convert: Callable[[str], Result]) -> Result:
    if convert is _whole and not setting.limit.startswith("# whole:"):
        raise ValueError("expected a # whole: limit")
    if convert is _number and not setting.limit.startswith("# range:"):
        raise ValueError("expected a # range: limit")
    result = convert(setting.value)
    _limits(setting)
    return result


def effective_line(file: str | Path, key: str) -> tuple[int, str]:
    """Return the effective source line index and its value without a write."""
    source = path(file)
    try:
        setting = _setting(_parse(source.read_text(encoding="utf-8")), key)
    except (OSError, UnicodeError, ValueError) as error:
        raise Unusable(f"{source}: {key}: {error}") from error
    return setting.line, setting.value


def editable_line(file: str | Path, key: str) -> tuple[int, str]:
    """Return an exact stored key's line and value when no host override exists."""
    source = path(file)
    try:
        settings = _parse(source.read_text(encoding="utf-8"))
        if key not in settings:
            raise ValueError("exact stored key is missing")
        setting = settings[key]
        for candidate, override in settings.items():
            if candidate.startswith(f"{key}."):
                raise ValueError(
                    f"{key} at line {setting.line + 1} has host override "
                    + f"{candidate} at line {override.line + 1}"
                )
    except (OSError, UnicodeError, ValueError) as error:
        raise Unusable(f"{source}: {key}: {error}") from error
    return setting.line, setting.value


def check(file: str | Path, key: str, value: str) -> None:
    source = path(file)
    try:
        setting = _setting(_parse(source.read_text(encoding="utf-8")), key)
        _limits(Setting(value, setting.limit, setting.line))
    except (OSError, UnicodeError, ValueError) as error:
        raise Unusable(f"{source}: {key}={value!r}: {error}") from error


def main(arguments: list[str] | None = None) -> int:
    args = sys.argv[1:] if arguments is None else arguments
    if len(args) != 3 or args[0] != "get":
        print("usage: conf.py get FILE KEY", file=sys.stderr)
        return 2
    try:
        print(read(args[1]).text(args[2]))
    except Unusable as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
