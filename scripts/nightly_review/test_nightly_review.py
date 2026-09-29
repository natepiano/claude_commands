"""Launch, quota gate and morning offer of the nightly review."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import override
from unittest import mock

import nightly_review

NIGHT = date(2026, 9, 29)
MORNING = datetime(2026, 9, 29, 7, 30)


class FakeNote:
    def __init__(self, stem: str, fields: dict[str, str]) -> None:
        self.path: Path = Path(f"/vault/agents/{stem}.md")
        self.tool: str = stem.split()[0]
        self.fields: dict[str, str] = fields

    def get(self, key: str) -> str | None:
        return self.fields.get(key)


def done(stdout: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout, "")


class NightlyReviewTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.live: set[str] = set()
        self.attached: set[str] = set()
        self.calls: list[list[str]] = []

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name, value in (("ROOT", self.root), ("tmux", self.tmux)):
            patcher = mock.patch.object(nightly_review, name, value)
            _ = patcher.start()
            self.addCleanup(patcher.stop)

    def tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        verb, name = args[0], args[-1]
        if verb == "has-session":
            return done(code=0 if name in self.live else 1)
        if verb == "list-clients":
            return done("/dev/pts/3: nightly-config\n" if name in self.attached else "")
        self.live.discard(name)
        return done()

    def launched(self, notes: list[FakeNote]) -> list[str]:
        def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(command)
            return done()

        with mock.patch("agent_notes.read_notes", return_value=notes), mock.patch("subprocess.run", run):
            return nightly_review.launch(NIGHT)

    def night(self, log: str) -> Path:
        directory = self.root / NIGHT.isoformat()
        directory.mkdir()
        _ = (directory / "launch.log").write_text(log)
        return directory

    def test_launch_starts_both_agents_with_the_prompt_last(self) -> None:
        self.assertEqual(self.launched([]), ["nightly-config: started", "nightly-rust: started"])
        command = self.calls[0]
        self.assertEqual(command[command.index("-s") + 1], "nightly-config")
        self.assertEqual(command[command.index("-c") + 1], "/etc/nixos")
        self.assertTrue(command[-1].endswith("'/nightly_review config'; exec zsh"))
        self.assertTrue((self.root / NIGHT.isoformat() / "launch.log").read_text().startswith("nightly-config: started"))

    def test_last_night_is_replaced_unless_the_user_is_attached(self) -> None:
        self.live = {"nightly-config", "nightly-rust"}
        self.attached = {"nightly-rust"}
        lines = self.launched([])
        self.assertEqual(lines[0], "nightly-config: started")
        self.assertIn("left running", lines[1])
        self.assertEqual(len(self.calls), 1)

    def test_low_quota_skips_the_night(self) -> None:
        resets = (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
        low = FakeNote("claude 2", {"state": "active", "weekly_remaining_usage": "4", "resets": resets})
        lines = self.launched([low])
        self.assertEqual(lines, ["skipped: claude 2 has 4% of its weekly usage left, under the 10% floor"])
        self.assertEqual(self.calls, [])

    def test_unknown_or_reset_usage_does_not_skip(self) -> None:
        past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        notes = [FakeNote("claude 1", {"state": "active", "weekly_remaining_usage": "null"}),
                 FakeNote("claude 2", {"state": "active", "weekly_remaining_usage": "2", "resets": past})]
        self.assertEqual(len(self.launched(notes)), 2)

    def test_offer_waits_for_five_and_fires_once(self) -> None:
        directory = self.night("nightly-config: started\nnightly-rust: started\n")
        _ = (directory / "config.md").write_text("# proposal\n")
        self.live = {"nightly-rust"}
        self.assertIsNone(nightly_review.offer(MORNING.replace(hour=4, minute=59), lambda: "natedev"))
        context = nightly_review.offer(MORNING, lambda: "natedev")
        self.assertIsNotNone(context)
        self.assertIn("config.md is ready", context or "")
        self.assertIn("rust is still running (tmux session nightly-rust)", context or "")
        self.assertIn("digest.md is not written yet", context or "")
        self.assertIsNone(nightly_review.offer(MORNING, lambda: "natedev"))

    def test_offer_is_for_natedev_only(self) -> None:
        directory = self.night("nightly-config: started\n")
        self.assertIsNone(nightly_review.offer(MORNING, lambda: "boss of bosses"))
        self.assertFalse((directory / "offered").exists())

    def test_offer_asks_and_carries_deferred_proposals(self) -> None:
        _ = self.night("nightly-config: started\n")
        _ = (self.root / "ledger.md").write_text(
            "- 2026-09-27 config: Old one — accepted, applied\n- 2026-09-28 rust/hana: Warm runners — deferred\n")
        context = nightly_review.offer(MORNING, lambda: "natedev") or ""
        self.assertIn("Deferred earlier: 2026-09-28 rust/hana: Warm runners.", context)
        self.assertNotIn("Old one", context)
        self.assertIn("see it and the deferred proposals now or defer it, and show it only on yes", context)
        self.assertIn("change its ledger line's `— proposed` to `— deferred`", context)

    def test_offer_asks_about_deferred_on_a_skipped_night(self) -> None:
        _ = self.night("skipped: claude 2 has 4% of its weekly usage left, under the 10% floor\n")
        _ = (self.root / "ledger.md").write_text("- 2026-09-28 config: Kwin keys — deferred\n")
        context = nightly_review.offer(MORNING, lambda: "natedev") or ""
        self.assertIn("Deferred earlier: 2026-09-28 config: Kwin keys.", context)
        self.assertIn("ask the user once whether they want to see the deferred proposals now or defer", context)

    def test_offer_reports_a_skipped_night(self) -> None:
        _ = self.night("skipped: claude 2 has 4% of its weekly usage left, under the 10% floor\n")
        context = nightly_review.offer(MORNING, lambda: "natedev") or ""
        self.assertIn("skipped: claude 2 has 4%", context)
        self.assertNotIn("ask the user", context)


if __name__ == "__main__":
    _ = unittest.main()
