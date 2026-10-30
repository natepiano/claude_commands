"""Give lint-cache tests private pass records without an unrelated Cargo token."""

from __future__ import annotations

from pathlib import Path
from typing import override

from scripts.direct.verify_lint_scope_test_support import VerifyLintScopeFixture


class VerifyLintCacheFixture(VerifyLintScopeFixture):
    @override
    def enable_cache(self) -> Path:
        # RUN_DIR enables the same cache without SESSION_DIR's director token
        # and progress window. These tests have no peer competing for Cargo.
        session = self.root / "session"
        session.mkdir()
        self.environment["UNIT_RUN_DIR"] = str(session)
        return session / "verify_cache"
