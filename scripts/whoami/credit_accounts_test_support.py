"""Give a test its own list of Codex accounts that may spend paid credits."""
from __future__ import annotations

import unittest
from unittest import mock

from scripts.whoami import agent_accounts

TEST_CREDIT_ACCOUNTS = ("codex1", "codex2", "codex3", "codex4")


def hold_credit_accounts(test: unittest.TestCase, labels: tuple[str, ...] = TEST_CREDIT_ACCOUNTS) -> set[str]:
    """Return the test's credit accounts; add or remove a label to change the setting."""
    held = set(labels)
    _ = test.enterContext(mock.patch.object(agent_accounts, "codex_credit_accounts", side_effect=lambda: frozenset(held)))
    return held


def set_credits(held: set[str], label: str, setting: str) -> None:
    """Apply a test's "on" or "off" for one account."""
    if setting == "off":
        held.discard(label)
    else:
        held.add(label)
