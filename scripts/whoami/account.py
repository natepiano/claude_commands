#!/usr/bin/env python3
"""Resolve the Claude or Codex login behind a config directory or process."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
from typing import Literal, NamedTuple, TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.whoami.agent_accounts import claude_config_dir, claude_config_value, claude_on_disk, codex_email_on_disk, codex_home  # noqa: E402
from scripts.whoami import note_file  # noqa: E402


@dataclass(frozen=True)
class Account:
    tool: Literal["claude", "codex"]
    login: str
    label: str


@dataclass(frozen=True)
class ResolvedHistoryLabel:
    label: str


@dataclass(frozen=True)
class HistoricalAccountUnresolved:
    """This record contains no evidence of its home's numbered account."""


class UnreadableAccount(Exception):
    """The current process has no readable Claude login."""


class UnknownAccountName(ValueError):
    """Text does not identify a Claude note label or login."""


class AccountIdentity(TypedDict):
    login: str
    label: str


class AccountListing(TypedDict):
    claude: AccountIdentity | None
    codex: AccountIdentity | None


class CommandLine(argparse.Namespace):
    as_json: bool = False
    pid: int | None = None
    write_label: bool = False


def label_for(tool: str, login: str) -> str:
    """The matching hanadocs account label, or the login when no note matches."""
    folded_tool = tool.casefold()
    folded_login = login.casefold()
    for note in note_file.read_notes():
        if note.tool == folded_tool and (note.get("login") or "").casefold() == folded_login:
            return note.label
    return login


def claude_account(config_dir: Path) -> Account | None:
    """The Claude login stored for one configuration directory."""
    report = claude_on_disk(config_dir)
    if report.email is None:
        return None
    return Account("claude", report.email, label_for("claude", report.email))


def own_claude_account() -> Account:
    """Return this process's Claude account or explain why it cannot be used."""
    resolved = claude_account(claude_config_dir())
    if resolved is None:
        raise UnreadableAccount("this process's Claude account is unreadable")
    return resolved


def named_claude_account(text: str) -> Account:
    """Resolve a Claude note label or accept an explicit login."""
    parsed = note_file.parse_account_label(text.casefold())
    for note in note_file.read_notes():
        if note.tool != "claude" or not isinstance(parsed, note_file.NumberedAccountLabel) or note.label != parsed.label:
            continue
        login = note.get("login")
        if login:
            return Account("claude", login, note.label)
    if (
        "@" in text
        and "/" not in text
        and "\0" not in text
        and not any(character.isspace() for character in text)
        and not text.startswith(".")
    ):
        return Account("claude", text, label_for("claude", text))
    raise UnknownAccountName(
        f"unknown account {text}: give a note label such as claude2, or a login"
    )


def codex_account(home: Path) -> Account | None:
    """The Codex login stored in one Codex home."""
    login = codex_email_on_disk(home)
    if login is None:
        return None
    return Account("codex", login, label_for("codex", login))


def _default_claude_config_dir() -> Path:
    return Path.home() / ".claude"


# Claude Code saves a memory file without asking only when no link sits in the
# file's path. A named account's `projects` is a link into the default
# directory, so its sessions are pointed at the memory under that directory.
MEMORY_BASE_VARIABLE = "CLAUDE_CODE_REMOTE_MEMORY_DIR"
NAMED_ACCOUNT_VARIABLES = ("CLAUDE_CONFIG_DIR", MEMORY_BASE_VARIABLE)


def named_account_environment(config_dir: Path) -> dict[str, str]:
    """The variables that start Claude Code on a named account's directory."""
    return {
        "CLAUDE_CONFIG_DIR": claude_config_value(config_dir),
        MEMORY_BASE_VARIABLE: str(_default_claude_config_dir()),
    }


def named_account_prefix(config_dir: Path) -> str:
    """The same variables as shell assignments placed before a command."""
    return "".join(
        f"{name}={shlex.quote(value)} "
        for name, value in named_account_environment(config_dir).items()
    )


@dataclass(frozen=True)
class ConfiguredClaudeFolder:
    path: Path


@dataclass(frozen=True)
class NoAccountSetting:
    """The readable environment does not select a Claude account folder."""


def parse_darwin_config_dir(output: str, expected_uid: int) -> ConfiguredClaudeFolder | NoAccountSetting | ProcessEnvironmentUnread:
    """Extract a config dir when `ps eww` exposed our process environment."""
    fields = output.split(maxsplit=1)
    if len(fields) != 2 or fields[0] != str(expected_uid):
        return ProcessEnvironmentUnread("process environment could not be read")

    process_text = fields[1]
    if not any(token.startswith("HOME=") for token in process_text.split()):
        return ProcessEnvironmentUnread("process environment could not be read")

    prefix = "CLAUDE_CONFIG_DIR="
    for token in reversed(process_text.split()):
        if token.startswith(prefix):
            return ConfiguredClaudeFolder(Path(token.removeprefix(prefix))) if token.removeprefix(prefix) else NoAccountSetting()
    return NoAccountSetting()


def process_config_dir(pid: int) -> ConfiguredClaudeFolder | NoAccountSetting | ProcessEnvironmentUnread:
    """Keep an absent account setting distinct from an unreadable environment."""
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ("ps", "eww", "-o", "uid=,command=", "-p", str(pid)),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode:
                return ProcessEnvironmentUnread("process environment could not be read")
            return parse_darwin_config_dir(result.stdout, os.getuid())
        except (OSError, subprocess.TimeoutExpired):
            return ProcessEnvironmentUnread("process environment could not be read")

    try:
        environment = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return ProcessEnvironmentUnread("process environment could not be read")
    prefix = b"CLAUDE_CONFIG_DIR="
    for entry in environment.split(b"\0"):
        if entry.startswith(prefix):
            return ConfiguredClaudeFolder(Path(os.fsdecode(entry.removeprefix(prefix)))) if entry.removeprefix(prefix) else NoAccountSetting()
    return NoAccountSetting()



@dataclass(frozen=True)
class ProcessCodexHome:
    home: Path


@dataclass(frozen=True)
class ProcessWithoutCodexHome:
    """A readable environment has no CODEX_HOME; it uses the default home."""


@dataclass(frozen=True)
class ProcessEnvironmentUnread:
    reason: str


@dataclass(frozen=True)
class MacProcessRecord:
    arguments: tuple[str, ...]
    setting: ProcessCodexHome | ProcessWithoutCodexHome
    executable: Path


def kern_procargs2(pid: int) -> bytes:
    """Read one kernel argument record, without exposing it in diagnostics."""
    query = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    buffer = ctypes.create_string_buffer(1024 * 1024)
    size = ctypes.c_size_t(ctypes.sizeof(buffer))
    try:
        sysctl = cast(Callable[[object, int, object, object, object, int], int],
                      ctypes.CDLL(None, use_errno=True).sysctl)
    except AttributeError as error:
        raise OSError("kernel process reader unavailable") from error
    if sysctl(query, 3, buffer, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), "kernel process record unavailable")
    if size.value > ctypes.sizeof(buffer):
        raise OSError("kernel process record exceeds buffer")
    return buffer.raw[:size.value]


class ProcessArguments(NamedTuple):
    executable: Path
    arguments: tuple[str, ...]
    # Only the names the caller keeps.
    environment: dict[str, str]


def procargs2_parts(payload: bytes, keep: Callable[[str], bool]) -> ProcessArguments:
    """Separate exactly argc arguments from the following environment strings.

    The record is argc, the executable path, NUL padding, the arguments, then the environment and
    the kernel's own `key=value` strings, each NUL-terminated. Only a value whose name `keep`
    accepts is decoded; every other value is dropped as it is read. A malformed record raises
    ValueError, which names no part of the record.
    """
    malformed = ValueError("process argument record is malformed")
    if len(payload) < 5 or not payload.endswith(b"\0"):
        raise malformed
    count = int.from_bytes(payload[:4], sys.byteorder, signed=True)
    executable_end = payload.find(b"\0", 4)
    if count <= 0 or executable_end <= 4:
        raise malformed
    offset = executable_end + 1
    while offset < len(payload) and payload[offset] == 0:
        offset += 1
    words = payload[offset:].split(b"\0")
    if len(words) <= count or not words[0]:
        raise malformed
    environment: dict[str, str] = {}
    for entry in words[count:]:
        if not entry:
            continue
        key, separator, value = entry.partition(b"=")
        if not separator or not key:
            raise malformed
        if keep(name := os.fsdecode(key)):
            environment[name] = os.fsdecode(value)
    return ProcessArguments(Path(os.fsdecode(payload[4:executable_end])),
                            tuple(os.fsdecode(word) for word in words[:count]), environment)


def mac_process_record(pid: int) -> MacProcessRecord | ProcessEnvironmentUnread:
    """The arguments, the CODEX_HOME setting and the executable of a Mac process."""
    try:
        parts = procargs2_parts(kern_procargs2(pid), lambda name: name == "CODEX_HOME")
    except (OSError, ValueError):
        return ProcessEnvironmentUnread("process argument record could not be read")
    home = parts.environment.get("CODEX_HOME")
    setting = ProcessCodexHome(Path(home)) if home else ProcessWithoutCodexHome()
    return MacProcessRecord(parts.arguments, setting, parts.executable)


def process_codex_home(pid: int) -> ProcessCodexHome | ProcessWithoutCodexHome | ProcessEnvironmentUnread:
    """Distinguish an absent variable from an unreadable process environment."""
    if sys.platform == "darwin":
        record = mac_process_record(pid)
        return record.setting if isinstance(record, MacProcessRecord) else record
    try:
        environment = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError as error:
        return ProcessEnvironmentUnread(str(error))
    for entry in environment.split(b"\0"):
        if entry.startswith(b"CODEX_HOME="):
            return ProcessCodexHome(Path(os.fsdecode(entry.removeprefix(b"CODEX_HOME="))))
    return ProcessWithoutCodexHome()


@dataclass(frozen=True)
class LegacyHomeLogin:
    """The shared home still holds this login for a process-attribution pass."""

    account: Account


@dataclass(frozen=True)
class ConfigOnlyHome:
    """The shared configuration home has no readable account login."""


@dataclass(frozen=True)
class HomeAccountUnchecked:
    """A single-process caller has not read the shared home's state yet."""


_HOME_ACCOUNT_UNCHECKED = HomeAccountUnchecked()


def home_account_state() -> LegacyHomeLogin | ConfigOnlyHome:
    """Read the legacy home once for a pass that attributes several processes."""
    identity = claude_account(_default_claude_config_dir())
    return LegacyHomeLogin(identity) if identity is not None else ConfigOnlyHome()


def process_account_directory(setting: ConfiguredClaudeFolder | NoAccountSetting | ProcessEnvironmentUnread, *,
                              home_state: LegacyHomeLogin | ConfigOnlyHome | HomeAccountUnchecked = _HOME_ACCOUNT_UNCHECKED
                              ) -> ConfiguredClaudeFolder | NoAccountSetting | ProcessEnvironmentUnread:
    """Retain a legacy home session only until this machine has cut over."""
    if isinstance(setting, NoAccountSetting):
        state = home_account_state() if isinstance(home_state, HomeAccountUnchecked) else home_state
        if isinstance(state, LegacyHomeLogin):
            return ConfiguredClaudeFolder(_default_claude_config_dir())
    return setting


def account_of(pid: int, *, home_state: LegacyHomeLogin | ConfigOnlyHome | HomeAccountUnchecked = _HOME_ACCOUNT_UNCHECKED) -> Account | None:
    """The login selected by a readable process, including pre-cutover sessions."""
    setting = process_config_dir(pid)
    state = home_account_state() if isinstance(setting, NoAccountSetting) and isinstance(home_state, HomeAccountUnchecked) else home_state
    setting = process_account_directory(setting, home_state=state)
    if isinstance(setting, ConfiguredClaudeFolder) and setting.path == _default_claude_config_dir() and isinstance(state, LegacyHomeLogin):
        return state.account
    return claude_account(setting.path) if isinstance(setting, ConfiguredClaudeFolder) else None


def write_label(config_dir: Path, account: Account) -> None:
    """Atomically refresh a config directory's cheap status-line label cache."""
    expected = note_file.canonical_label(account.label) + "\n"
    destination = config_dir / "account-label"
    try:
        if destination.read_text(encoding="utf-8") == expected:
            return
    except OSError:
        pass

    config_dir.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config_dir,
            prefix=".account-label-",
            delete=False,
        ) as output:
            temporary = output.name
            _ = output.write(expected)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _identity(account: Account | None) -> AccountIdentity | None:
    if account is None:
        return None
    return {"login": account.login, "label": account.label}


def _plain(account: Account | None) -> str:
    if account is None:
        return "unavailable"
    return f"{account.label} ({account.login})"


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--json", action="store_true", dest="as_json")
    _ = parser.add_argument("--pid", type=int)
    _ = parser.add_argument("--write-label", action="store_true")
    options = cast(CommandLine, parser.parse_args(arguments))

    current_config_dir = claude_config_dir()
    current_claude = claude_account(current_config_dir)
    claude = current_claude if options.pid is None else account_of(options.pid)
    codex = codex_account(codex_home()) if options.pid is None else None

    if options.write_label:
        if current_claude is None:
            try:
                (current_config_dir / "account-label").unlink()
            except FileNotFoundError:
                pass
        else:
            write_label(current_config_dir, current_claude)

    listing: AccountListing = {"claude": _identity(claude), "codex": _identity(codex)}
    if options.as_json:
        print(json.dumps(listing))
    elif options.pid is not None:
        print(_plain(claude))
    else:
        print(f"{_plain(claude)} · {_plain(codex)}")
    return 0 if claude is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
