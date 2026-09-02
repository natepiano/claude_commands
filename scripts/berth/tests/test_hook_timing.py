#!/usr/bin/env python3
"""The installed PostToolUse timing measurement, run on its own."""

from __future__ import annotations

import sys
import unittest
from typing import override

from berth.tests.installed_front_end import (
    InstalledFrontEndFixture,
    run_explicit_engine_install,
)


class HookTimingTests(unittest.TestCase):
    """Run the intentionally separate installed-engine timing measurement."""

    runner: InstalledFrontEndFixture

    @classmethod
    @override
    def setUpClass(cls) -> None:
        InstalledFrontEndFixture.setUpClass()

    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.runner = InstalledFrontEndFixture()

    @override
    def setUp(self) -> None:
        self.runner.setUp()

    @override
    def tearDown(self) -> None:
        self.runner.tearDown()

    def test_complete_post_tool_use_outcome_matrix_stays_inside_published_bound(
        self,
    ) -> None:
        self.runner.run_complete_post_tool_use_outcome_matrix_measurement()

if __name__ == "__main__":
    if sys.argv[1:2] == ["--install-engine"]:
        raise SystemExit(run_explicit_engine_install(sys.argv[2:]))
    _ = unittest.main()
