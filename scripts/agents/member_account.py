"""Select the account for a member's resumed session."""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast
from importlib import import_module
from pathlib import Path
import subprocess
import sys

from scripts.accounts import accounts
from scripts.agents.stop_results import SessionWithoutStaffRow, SESSION_WITHOUT_STAFF_ROW
from scripts.whoami import agent_accounts
from scripts.whoami import account
from scripts.herdr import pane_launch, herdr_types

if TYPE_CHECKING:
    from scripts.production.staff import StaffRow


def resume_account(kind: Literal["claude", "codex"], row: StaffRow | SessionWithoutStaffRow = SESSION_WITHOUT_STAFF_ROW,
                   *, to: str | accounts.AccountSelection = "") -> accounts.AccountSelection:
    """Keep an account selection or select the requested label or role account."""
    if isinstance(to, (accounts.NamedAccount, accounts.CodexWorkersAccount)):
        return to
    if to:
        return (accounts.CodexWorkersAccount(to, agent_accounts.codex_account_home(to).home)
                if kind == "codex" else accounts.NamedAccount(to, accounts.account_dir(to)))
    if kind == "codex":
        return accounts.role_account("workers")
    if not isinstance(row, SessionWithoutStaffRow) and row.kind == "producer":
        return accounts.role_account("showrunner")
    if not isinstance(row, SessionWithoutStaffRow) and row.kind in ("unit", "showrunner"):
        return accounts.role_account(row.kind)
    return accounts.role_account("unit")


def repair_home(row: StaffRow, kind: str, pid: int) -> None:
    """Repair the member's home from its unchanged current process."""
    from scripts.production import staff

    try:
        reader = cast(pane_launch.NativeProcessReader, cast(object, import_module("scripts.herdr.herdr")))
        observed = reader.native_process(pid, kind)
        if observed.current_identity != row.current_session:
            return
        if kind == "codex":
            codex = account.process_codex_home(pid)
            if isinstance(codex, account.ProcessEnvironmentUnread):
                return
            folder = codex.home if isinstance(codex, account.ProcessCodexHome) else Path.home() / ".codex"
        else:
            claude = account.process_config_dir(pid)
            if not isinstance(claude, account.ConfiguredClaudeFolder):
                return
            folder = claude.path
        current = reader.native_process(pid, kind)
        if current.current_identity != observed.current_identity or current.started != observed.started:
            return
        home = staff.SessionHome(folder.absolute())
        if row.home != home:
            _ = staff.set_home(row.id, home)
    except (OSError, ValueError, subprocess.SubprocessError, herdr_types.HerdrFailure,
            staff.StaffUnreadable, staff.StaffNotFound) as error:
        print(f"{row.name}: home repair is unavailable: {error}", file=sys.stderr)
