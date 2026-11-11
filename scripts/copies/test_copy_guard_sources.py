"""Check copy policy in scans of the repository's own scripts."""
from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import ClassVar
import unittest

from scripts.copies import copies, copy_guard


class CopyGuardTests(unittest.TestCase):
    transitional: ClassVar[frozenset[str]] = frozenset({"mac_test/offload.py", "production/mac_run.sh", "direct/verify.sh"})

    @classmethod
    @cache
    def repository_findings(cls) -> tuple[copy_guard.TreeCreation, ...]:
        """Share the strict scan of the unchanged repository between both policy tests."""
        scripts = Path(__file__).resolve().parents[1]
        settings = copies.load_config()
        return tuple(copy_guard.scan_scripts(scripts, allow_list={
            path: reason for path, reason in settings.allow_list.items() if path not in cls.transitional}))

    def test_repository_scripts_use_the_single_checkout_service(self) -> None:
        scripts = Path(__file__).resolve().parents[1]
        # The shared service loader owns both gate settings and the allow-list data.
        settings = copies.load_config()
        findings = [finding for finding in self.repository_findings()
                    if finding.path.relative_to(scripts).as_posix() not in settings.allow_list]
        self.assertEqual(findings, [], "\n".join(str(finding) for finding in findings))

    def test_repository_build_copy_makers_have_no_transitional_exemptions(self) -> None:
        settings = copies.load_config()
        transitional = self.transitional
        findings = list(self.repository_findings())
        self.assertEqual(findings, [], "\n".join(str(finding) for finding in findings))
        self.assertEqual(set(settings.allow_list) & transitional, set())


if __name__ == "__main__":
    _ = unittest.main()
