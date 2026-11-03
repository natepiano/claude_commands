#!/usr/bin/env python3
"""Read live configuration, complete missing default values, and recover invalid values from Git."""
from __future__ import annotations

from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from typing import TypeVar


_warning_sink: ContextVar[Callable[[str], None] | None] = ContextVar("configuration_warning_sink", default=None)


def _warning(message: str) -> None:
    sink = _warning_sink.get()
    if sink is None:
        print(message, file=sys.stderr)
    else:
        sink(message)


@contextmanager
def diagnostics(*, report: bool = True) -> Generator[list[str]]:
    """Collect reader warnings for a reply, with optional local output."""
    messages: list[str] = []

    def collect(message: str) -> None:
        messages.append(message)
        if report:
            print(message, file=sys.stderr)

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


def value_origin(file: str | Path, key: str, convert: Callable[[str], Result]
                 ) -> UsableLiveValue[Result] | RecoveredValue[Result] | UnavailableValue:
    """Expose live validation without granting recovered values the same guarantee."""
    return _resolve(file, key, convert, warn=False)


class _MissingKey(ValueError):
    """The file has no effective entry for the requested key."""


def _resolve(file: str | Path, key: str, convert: Callable[[str], Result], *, warn: bool
             ) -> UsableLiveValue[Result] | RecoveredValue[Result] | UnavailableValue:
    """Read repository defaults before Git when a default file or key is absent."""
    source = path(file)
    bad = "<missing>"
    repository_allowed = False
    try:
        setting = _setting(_parse(source.read_text(encoding="utf-8")), key)
        bad = setting.value
        return UsableLiveValue(_converted(setting, convert))
    except (OSError, UnicodeError, ValueError, ArithmeticError) as error:
        repository_allowed = isinstance(error, (FileNotFoundError, _MissingKey))
        reason = f"{source}: {key}={bad!r}: {error}"
    if repository_allowed and isinstance(file, str) and "CONF_DIR" not in os.environ:
        repository = _repository_path(file)
        if repository != source:
            try:
                setting = _setting(_parse(repository.read_text(encoding="utf-8")), key)
                return RecoveredValue(_converted(setting, convert), reason)
            except (OSError, UnicodeError, ValueError, ArithmeticError):
                pass
    for committed in _committed_files(source):
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


def directory() -> Path:
    """Return the live configuration folder, or the test fixture override."""
    return Path(os.environ.get("CONF_DIR") or "~/.claude/config").expanduser()


def path(file: str | Path) -> Path:
    if isinstance(file, Path):
        return file.expanduser()
    if not file or Path(file).name != file or file.endswith(".conf"):
        raise Unusable(f"{file}: file: expected a configuration stem")
    return directory() / f"{file}.conf"


def _repository_path(stem: str) -> Path:
    """Return the repository configuration file for a validated stem."""
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
        convert = _whole if form == "# whole:" else _number
        value = convert(setting.value)
        low, high = (bound.strip() for bound in bounds)
        if (low and value < convert(low)) or (high and value > convert(high)):
            raise ValueError(f"outside range {low}..{high}")


def _number(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("expected a finite number")
    return result


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


def _read(file: str | Path, key: str, convert: Callable[[str], Result]) -> Result:
    result = _resolve(file, key, convert, warn=True)
    if isinstance(result, UnavailableValue):
        raise Unusable(f"{result.reason}; no usable committed value")
    return result.value


def text(file: str | Path, key: str) -> str:
    return _read(file, key, str)


def number(file: str | Path, key: str) -> float:
    return _read(file, key, _number)


def whole(file: str | Path, key: str) -> int:
    return _read(file, key, _whole)


def words(file: str | Path, key: str) -> tuple[str, ...]:
    return _read(file, key, lambda value: tuple(value.split()))


def values(file: str | Path) -> dict[str, str]:
    """Read a variable-key map; explicit empty entries without limits remain optional."""
    source = path(file)
    try:
        contents = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        contents = ""
        recovered_from_repository = False
        if isinstance(error, FileNotFoundError) and isinstance(file, str) and "CONF_DIR" not in os.environ:
            try:
                contents = _repository_path(file).read_text(encoding="utf-8")
                recovered_from_repository = True
            except (OSError, UnicodeError):
                pass
        if not recovered_from_repository:
            for contents in _committed_files(source):
                break
            else:
                _warning(f"warning: {source}: keys: {error}")
                raise Unusable(f"{source}: keys: {error}; no usable committed file") from error
            _warning(f"warning: {source}: keys: {error}")
    keys = _parse(contents)
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
            result[key] = text(file, key)
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
        print(text(args[1], args[2]))
    except Unusable as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
