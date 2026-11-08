"""Select the account for a member's resumed session."""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from scripts.accounts import accounts
from scripts.shutdown.stop_results import SessionWithoutStaffRow, SESSION_WITHOUT_STAFF_ROW
from scripts.whoami import agent_accounts

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
    if not isinstance(row, SessionWithoutStaffRow) and row.kind in ("unit", "showrunner"):
        return accounts.role_account(row.kind)
    return accounts.role_account("unit")
