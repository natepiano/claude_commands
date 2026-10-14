#!/usr/bin/env python3
"""Add a Claude or Codex account and wire it into everything that names accounts.

`new_account.py claude claude4 [login]` or `new_account.py codex codex3 [login]`.
Each step prints `done:` or `for you:`. Run it again after a `for you:` step;
steps already done stay done. ~/.claude/docs/accounts.md is the guide.

Everything else that lists accounts discovers them: account folders by
~/.claudeN and ~/.codexN, readings and the status line by the account note.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.accounts import accounts  # noqa: E402
from scripts.lib.user_zone import user_zone  # noqa: E402
import agent_accounts  # noqa: E402
import note_file  # noqa: E402

NAME = re.compile(r"(claude|codex)([1-9][0-9]*)")
# The alias loop in the nixos shell module reads the notes folder; its path is the marker.
ALIAS_LOOP_MARKER = "hanadocs/agents/claude"


@dataclass(frozen=True)
class Step:
    done: bool
    text: str


@dataclass(frozen=True)
class NewAccount:
    tool: Literal["claude", "codex"]
    number: int
    name: str
    label: str


def account_label(name: str) -> str:
    """The label accounts.py accepts today: claude4 is "claude 4". The no-space rename deletes this."""
    return re.sub(r"([a-z])([0-9])", r"\1 \2", name, count=1)


def parse_account(tool: str, name: str) -> NewAccount:
    match = NAME.fullmatch(name)
    if tool not in ("claude", "codex"):
        raise accounts.AccountsError(f"new_account: the type is claude or codex, not {tool}")
    if match is None or match.group(1) != tool:
        raise accounts.AccountsError(f"new_account: name a {tool} account {tool}N, such as {tool}4, not {name}")
    return NewAccount(tool, int(match.group(2)), name, account_label(name))


def notes_directory() -> Path:
    return Path(os.environ.get("AGENT_NOTES_DIR", str(accounts.accounts_home() / "rust" / "hanadocs" / "agents")))


def note_path(account: NewAccount) -> Path:
    return notes_directory() / f"{account.label}.md"


def home_login(account: NewAccount) -> str | None:
    """The login the account's own folder already holds, if any."""
    home = accounts.accounts_home() / f".{account.name}"
    if account.tool == "codex":
        return agent_accounts.codex_email_on_disk(home) if (home / "auth.json").is_file() else None
    return agent_accounts.claude_on_disk(home).email if home.is_dir() else None


def note_step(account: NewAccount, login: str | None) -> Step:
    path = note_path(account)
    if path.exists():
        return Step(True, f"account note {path}")
    if login is None:
        then = (f"log in (below), then run /new_account {account.tool} {account.name} again"
                if account.tool == "claude" else f"run /new_account codex {account.name} <its login>")
        return Step(False, f"{account.label} needs its login in an account note: {then}")
    for other in sorted(notes_directory().glob("*.md")):
        note = note_file.read_note(other)
        held = note.get("login") if note is not None else None
        if held is not None and held.casefold() == login.casefold():
            return Step(False, f"{other.name} already holds that login; one login, one account")
    today = datetime.now(user_zone()).strftime("%Y-%m-%d")
    text = "\n".join((
        "---", f'date_created: "[[{today}]]"', f'date_modified: "[[{today}]]"', f"login: {login}",
        "return:", '  - "[[agents.base]]"', "state: inactive",
        "weekly_remaining_usage: null", "weekly_usage_checked_at: null", "---", "",
    ))
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".new-account-",
                                     suffix=".md", delete=False) as handle:
        _ = handle.write(text)
    _ = Path(handle.name).replace(path)
    return Step(True, f"wrote account note {path}")


def claude_login_instruction(account: NewAccount) -> Step:
    directory = shlex.quote(str(accounts.account_dir(account.label)))
    return Step(False, f"in a terminal, log in as {account.label}:\n"
                + f"    env -u CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR={directory} claude\n    /login")


def claude_login_step(account: NewAccount) -> Step:
    readiness = accounts.account_directory_readiness(account.label)
    if isinstance(readiness, accounts.AccountDirectoryReady):
        return Step(True, f"~/.{account.name} is logged in")
    if isinstance(readiness, accounts.AccountDirectoryLoggedOut):
        return claude_login_instruction(account)
    return Step(False, "\n    ".join(readiness.problems))


def codex_steps(account: NewAccount) -> list[Step]:
    home = accounts.codex_setup_home(account.label)
    if home_login(account) is None:
        problems = accounts.setup(account.label)
        if problems:
            return [Step(False, "\n    ".join(problems))]
        return [Step(True, f"set up ~/.{account.name}"),
                Step(False, f"in a terminal, sign in as {account.label} (keep CODEX_HOME on it):\n"
                     + f"    CODEX_HOME=~/{home.name} codex login")]
    problems = accounts.check(account.label)
    if problems:
        return [Step(False, "\n    ".join(problems))]
    return [Step(True, f"~/.{account.name} is set up and signed in; it is a spare until "
                 + f"/showrunner:accounts swap seats \"{account.label}\"")]


def settings_entries(account: NewAccount) -> list[tuple[str, str]]:
    """Each settings.json list a Claude account folder needs, with its entry."""
    folder = f"~/.{account.name}"
    return [("permissions.allow", f"Read({folder}/projects/**/tool-results/**)"),
            ("permissions.allow", f"Edit({folder}/**)"),
            ("sandbox.filesystem.allowWrite", folder)]


def _settings_list(settings: object, dotted: str) -> list[str]:
    value: object = settings
    for key in dotted.split("."):
        value = cast(dict[str, object], value).get(key) if isinstance(value, dict) else None
    return [str(item) for item in cast(list[object], value)] if isinstance(value, list) else []


def _covers(where: str, rule: str, entry: str) -> bool:
    """Read and Edit rules are gitignore globs; the Linux sandbox skips a write entry with a wildcard."""
    if where.startswith("sandbox.") and sys.platform != "darwin":
        return rule == entry
    return fnmatch.fnmatchcase(entry, rule)


def settings_step(account: NewAccount) -> Step:
    path = accounts.main_config_dir() / "settings.json"
    try:
        settings = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        return Step(False, f"could not read {path}: {error}")
    missing = [(where, entry) for where, entry in settings_entries(account)
               if not any(_covers(where, rule, entry) for rule in _settings_list(settings, where))]
    if not missing:
        return Step(True, f"~/.claude/settings.json covers ~/.{account.name}")
    lines = "\n".join(f"    {where}: \"{entry}\"," for where, entry in missing)
    return Step(False, f"add to ~/.claude/settings.json (agents may not edit it):\n{lines}")


def nixos_shell_module() -> Path | None:
    for repository in (Path("/etc/nixos"), accounts.accounts_home() / "nixos"):
        module = repository / "modules" / "linux" / "shell.nix"
        if module.is_file():
            return module
    return None


def alias_step(account: NewAccount) -> Step | None:
    """The `claudeN` shell alias; None on a machine whose shell has no account aliases."""
    if sys.platform == "darwin":
        return None
    module = nixos_shell_module()
    if module is None:
        return Step(False, f"no nixos shell module found to define the {account.name} alias")
    text = module.read_text(encoding="utf-8")
    if ALIAS_LOOP_MARKER in text:
        return Step(True, f"{account.name} alias: comes from the account note; open a new terminal")
    if re.search(rf"^\s*{account.name}\s*=", text, re.MULTILINE):
        return Step(True, f"{account.name} alias in {module} (live after a rebuild)")
    return Step(False, f"add to shellAliases in {module}, then rebuild:\n    {account.name} = "
                + f"\"~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py exec '{account.label}'\";")


def new_account(account: NewAccount, login: str | None) -> list[Step]:
    if account.tool == "codex":
        note = note_step(account, login or home_login(account))
        return [note, *codex_steps(account)] if note.done else [note]
    problems = accounts.setup(account.label)
    if problems:
        return [Step(False, "\n    ".join(problems))]
    steps = [Step(True, f"set up ~/.{account.name}"), note_step(account, login or home_login(account))]
    steps.append(claude_login_step(account) if steps[-1].done else claude_login_instruction(account))
    steps.append(settings_step(account))
    if (alias := alias_step(account)) is not None:
        steps.append(alias)
    return steps


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="new_account.py")
    _ = parser.add_argument("tool")
    _ = parser.add_argument("name")
    _ = parser.add_argument("login", nargs="?")
    options = parser.parse_args(argv)
    try:
        account = parse_account(cast(str, options.tool), cast(str, options.name))
        steps = new_account(account, cast(str | None, options.login))
    except (accounts.AccountsError, OSError) as error:
        print(error, file=sys.stderr)
        return 1
    for step in steps:
        print(f"{'done' if step.done else 'for you'}: {step.text}")
    if all(step.done for step in steps):
        print(f"{account.name} is ready; /showrunner:accounts shows its row.")
        return 0
    print(f"Then run /new_account {account.tool} {account.name} again.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
