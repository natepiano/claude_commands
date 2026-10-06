#!/usr/bin/env python3
"""Poll stamps describe completed GitHub contact, including capped polls."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast, override
from unittest import mock

import ci
from test_index import point_root_at


class CiPollTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    gh_calls: int = 0
    warm_code: int = 0
    gh_response: object = None
    gh_error: str | None = None

    @override
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary) / "buildlog"
        point_root_at(self, self.root)
        self.enterContext(mock.patch.dict(os.environ, {"HOME": temporary}))
        self.gh_calls = 0
        self.warm_code = 0
        self.gh_response = {"total_count": 0, "workflow_runs": []}
        self.gh_error = None
        _ = self.enterContext(mock.patch.object(ci, "gh_get", new=self.fake_gh_get))
        _ = self.enterContext(mock.patch.object(subprocess, "run", new=self.fake_run))

    def fake_gh_get(self, _path: str, _fields: dict[str, str]) -> object:
        self.gh_calls += 1
        if self.gh_error is not None:
            raise ci.GhError(self.gh_error)
        return self.gh_response

    def fake_run(self, args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.assertEqual(args, ["github-warm-status"])
        return subprocess.CompletedProcess(args, self.warm_code)

    def stamp(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads((self.root / "ci" / "polled.json").read_text()))

    def test_complete_poll_records_utc_time_and_completion(self) -> None:
        self.assertEqual(ci.ci(), 0)

        state = cast(dict[str, object], self.stamp()[ci.REPOS[0]])
        self.assertIs(state["complete"], True)
        at = datetime.fromisoformat(cast(str, state["polled_at"]).replace("Z", "+00:00"))
        self.assertEqual(at.utcoffset(), timedelta(0))
        self.assertEqual(self.gh_calls, 1)

    def test_capped_poll_records_incomplete_stamp(self) -> None:
        with mock.patch.object(ci, "REQUEST_CAP", 1):
            self.assertEqual(ci.ci(), 0)

        state = cast(dict[str, object], self.stamp()[ci.REPOS[0]])
        self.assertIs(state["complete"], False)
        self.assertIsInstance(state["polled_at"], str)

    def test_cold_credentials_leave_prior_stamp_unchanged(self) -> None:
        path = self.root / "ci" / "polled.json"
        path.parent.mkdir(parents=True)
        prior = b'{"prior":true}\n'
        _ = path.write_bytes(prior)
        self.warm_code = 1

        self.assertEqual(ci.ci(), 0)

        self.assertEqual(path.read_bytes(), prior)
        self.assertEqual(self.gh_calls, 0)

    def test_github_error_leaves_prior_stamp_unchanged(self) -> None:
        path = self.root / "ci" / "polled.json"
        path.parent.mkdir(parents=True)
        prior = b'{"prior":true}\n'
        _ = path.write_bytes(prior)
        self.gh_error = "unavailable"

        self.assertEqual(ci.ci(), 1)

        self.assertEqual(path.read_bytes(), prior)


if __name__ == "__main__":
    _ = unittest.main()
