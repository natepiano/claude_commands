"""Mac usage credentials belong to the exact folder selected at launch."""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from collections.abc import Callable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import override
from unittest.mock import patch

from scripts.whoami import agent_accounts


def folder_service(folder: Path) -> str:
    """Independent vector from the service-name contract supplied by the director."""
    value = unicodedata.normalize("NFC", str(folder)).encode("utf-8")
    return "Claude Code-credentials-" + hashlib.sha256(value).hexdigest()[:8]


class ServiceKeychain:
    """A synthetic keychain that rejects every selector except a service name."""

    def __init__(self) -> None:
        self.entries: dict[str, str] = {}
        self.calls: list[tuple[str, ...]] = []
        self.after_delete: Callable[[], None] = lambda: None

    def run(self, arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
        argv = tuple(arguments)
        if Path(argv[0]).name == "ps":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if Path(argv[0]).name != "security":
            raise AssertionError("unexpected external command in isolated keychain test")
        if len(argv) < 4 or argv[2] != "-s":
            raise AssertionError("keychain item must be selected by service name only")
        action, service = argv[1], argv[3]
        if (action == "find-generic-password" and argv[4:] not in ((), ("-w",))
                or action == "delete-generic-password" and argv[4:]
                or action not in ("find-generic-password", "delete-generic-password")):
            raise AssertionError("unexpected keychain action or extra item selector")
        if action == "delete-generic-password" or not argv[4:]:
            if (_options.get("stdout") != subprocess.DEVNULL
                    or _options.get("stderr") != subprocess.DEVNULL):
                raise AssertionError("keychain mutations and presence probes must discard both streams")
        self.calls.append(argv)
        if service not in self.entries:
            return subprocess.CompletedProcess(argv, 44, "", "synthetic private diagnostic")
        if action == "find-generic-password":
            return subprocess.CompletedProcess(argv, 0, self.entries[service] if "-w" in argv else "", "")
        del self.entries[service]
        self.after_delete()
        return subprocess.CompletedProcess(argv, 0, "", "")


class NamedKeychainCredentialsTests(unittest.TestCase):
    home: Path = Path()
    keychain: ServiceKeychain = ServiceKeychain()
    output: io.StringIO = io.StringIO()
    errors: io.StringIO = io.StringIO()

    @override
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}, clear=True))
        _ = self.enterContext(patch.object(sys, "platform", "darwin"))
        self.keychain = ServiceKeychain()
        _ = self.enterContext(patch.object(subprocess, "run", side_effect=self.keychain.run))
        self.output, self.errors = io.StringIO(), io.StringIO()
        _ = self.enterContext(redirect_stdout(self.output))
        _ = self.enterContext(redirect_stderr(self.errors))
        self.addCleanup(self.assert_private_output)

    def assert_private_output(self) -> None:
        output = self.output.getvalue() + self.errors.getvalue()
        self.assertFalse(any(value in output for value in (
            "synthetic-named-token", "synthetic-home-token", "synthetic private diagnostic")))

    def test_named_folder_reads_only_its_hashed_service_with_unicode_normalization(self) -> None:
        # Decomposed Unicode distinguishes NFC hashing from hashing raw path bytes.
        folder = self.home / "cafe\u0301" / ".claude2"
        service = folder_service(folder)
        self.keychain.entries[service] = json.dumps({"claudeAiOauth": {"accessToken": "synthetic-named-token"}})
        self.keychain.entries["Claude Code-credentials"] = json.dumps({"claudeAiOauth": {"accessToken": "synthetic-home-token"}})
        credentials = agent_accounts.claude_credentials(folder)
        self.assertTrue(credentials.get("claudeAiOauth", {}).get("accessToken") == "synthetic-named-token")
        self.assertEqual([call[3] for call in self.keychain.calls], [service])

    def test_missing_named_entry_raises_without_reading_legacy_credentials(self) -> None:
        folder = self.home / ".claude2"
        self.keychain.entries["Claude Code-credentials"] = json.dumps({"claudeAiOauth": {"accessToken": "synthetic-home-token"}})
        with self.assertRaisesRegex(RuntimeError, "usage credentials unavailable"):
            _ = agent_accounts.claude_credentials(folder)
        self.assertEqual([call[3] for call in self.keychain.calls], [folder_service(folder)])

    def test_empty_config_setting_reads_the_legacy_keychain_service(self) -> None:
        self.keychain.entries["Claude Code-credentials"] = json.dumps({
            "claudeAiOauth": {"accessToken": "synthetic-home-token"}})
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ""}):
            credentials = agent_accounts.claude_credentials()
        self.assertTrue(credentials.get("claudeAiOauth", {}).get("accessToken") == "synthetic-home-token")
        self.assertEqual([call[3] for call in self.keychain.calls], ["Claude Code-credentials"])

    def test_implicit_folder_uses_the_callers_exact_config_setting(self) -> None:
        folder = self.home / ".claude3"
        service = folder_service(folder)
        self.keychain.entries[service] = json.dumps({"claudeAiOauth": {"accessToken": "synthetic-named-token"}})
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(folder)}):
            credentials = agent_accounts.claude_credentials()
        self.assertTrue(credentials.get("claudeAiOauth", {}).get("accessToken") == "synthetic-named-token")
        self.assertEqual([call[3] for call in self.keychain.calls], [service])


if __name__ == "__main__":
    _ = unittest.main()
