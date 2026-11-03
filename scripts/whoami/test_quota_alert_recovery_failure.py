"""Quota alerts: recovery failure."""

from __future__ import annotations

import json
import unittest
from datetime import timedelta
from typing import cast
from unittest import mock
from scripts.whoami import quota_alert_test_support as support
import quota_alert
from scripts.accounts import moves

setUpModule = support.setUpModule


class QuotaAlertTests(support.SeededQuotaReminderFixture):
    def test_partial_routing_restoration_records_failed_keys_and_never_claims_full_recovery(self) -> None:
        _ = self.block([self.note("codex1.md", "active", "0")], self.now)
        edit = quota_alert.edit_registry
        def fail_one(function: str, *arguments: str) -> str | None:
            if function == "agents_set_assignment" and arguments[0] == "module_review":
                return "registry locked"
            return edit(function, *arguments)
        with mock.patch.object(quota_alert, "edit_registry", side_effect=fail_one):
            _ = self.alert([self.note("codex1.md", "active", "100")], self.now + timedelta(hours=1))
        entries = self.routing_entries()
        self.assertEqual([entry["kind"] for entry in entries], ["routed to claude", "routing not restored"])
        self.assertEqual(entries[1]["outcome"], "partial")
        self.assertEqual(entries[1]["restored"], ["direct"])
        self.assertEqual(entries[1]["failed"], ["module_review (registry locked)"])
        self.assertEqual(entries[1]["fallback_at"], entries[0]["fallback_at"])
        self.assertEqual(self.assignments()["module_review"], "claude")

    def test_unreadable_registry_records_failed_recovery_for_every_owed_key(self) -> None:
        _ = self.block([self.note("codex1.md", "active", "0")], self.now)
        with mock.patch.object(quota_alert, "registry_assignments", side_effect=OSError("registry unreadable")):
            _ = self.alert([self.note("codex1.md", "active", "100")], self.now + timedelta(hours=1))
        entries = self.routing_entries()
        self.assertEqual([entry["kind"] for entry in entries], ["routed to claude", "routing not restored"])
        self.assertEqual(entries[1]["outcome"], "failed")
        self.assertEqual(entries[1]["restored"], [])
        self.assertEqual(set(cast(list[str], entries[1]["failed"])),
                         {"direct (registry unreadable)", "module_review (registry unreadable)"})

    def test_unknown_fallback_owner_stays_unknown_after_reload_and_recovery_on_a_noted_home(self) -> None:
        _ = self.block([self.note("codex1.md", "active", "0")], self.now)
        with quota_alert.state_file() as state:
            prior = state["fallback"]
            assert isinstance(prior, quota_alert.AccountCodexQuotaFallback)
            state["fallback"] = quota_alert.UnknownOwnerCodexQuotaFallback(prior.at, prior.moved)
        later = self.now + timedelta(hours=1)
        _ = self.alert([self.note("codex1.md", "active", "100")], later)
        recovered = self.routing_entries()[-1]
        self.assertEqual(recovered["kind"], "routed back")
        self.assertEqual(recovered["owner"], "unknown")
        self.assertEqual(recovered["fallback_at"], prior.at)

    def test_mixed_routing_journal_closes_legacy_worker_wait_and_no_room_only_once(self) -> None:
        for kind in ("waited", "no room"):
            common = moves.MoveCommon(at=self.now.isoformat(), tool="codex", subject="workers", move_id=kind)
            entry: moves.MoveEntry = (moves.WaitedMove(**common, kind="waited", window="five_hour", reset=self.now.isoformat())
                if kind == "waited" else moves.NoRoomMove(**common, kind="no room", earliest_reset=self.now.isoformat()))
            path = moves.store_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as output:
                _ = output.write(json.dumps({**entry, "subject": "seats"}) + "\n")
        moves.append_routing(moves.RoutedToClaude(kind="routed to claude", at=self.now.isoformat(), tool="codex",
            fallback_at=self.now.isoformat(), owner="codex1", cause="refusal", why="Codex refused work for quota", moved=["direct"]))
        _ = self.note("codex1.md", "active", "100")
        for _ in range(2):
            quota_alert._finish_workers_transitions(self.now)  # pyright: ignore[reportPrivateUsage]
            quota_alert._room_again(self.now)  # pyright: ignore[reportPrivateUsage]
        closing = [entry for entry in moves.read_moves() if entry["kind"] in ("wait ended", "room again")]
        self.assertCountEqual([(entry["kind"], entry["move_id"]) for entry in closing],
                              [("wait ended", "waited"), ("room again", "no room")])


if __name__ == "__main__":
    _ = unittest.main()
