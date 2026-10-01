"""The pacer's plan, its measurements, and the rollout reader feeding them."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from collections.abc import Mapping
from pathlib import Path
from typing import override

import codex_pacer as pacer


class FakeNote:
    def __init__(self, tool: str, fields: dict[str, str]) -> None:
        self.tool: str = tool
        self.fields: dict[str, str] = fields

    def get(self, key: str) -> str | None:
        return self.fields.get(key)


NOW = datetime(2026, 10, 1, 17, 0, tzinfo=timezone.utc)


def local(hours: float) -> str:
    return (NOW + timedelta(hours=hours)).astimezone().replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")


class PlanTests(unittest.TestCase):
    def test_share_spends_exactly_the_affordable_rate(self) -> None:
        share = pacer.fast_share(affordable=2.0, demand_rate=1.0, k=3.0)
        self.assertAlmostEqual(1.0 * (1 + share * (3.0 - 1)), 2.0)

    def test_no_fast_when_default_alone_outspends(self) -> None:
        self.assertEqual(pacer.fast_share(affordable=1.0, demand_rate=1.5, k=2.3), 0.0)
        self.assertEqual(pacer.fast_share(affordable=None, demand_rate=1.0, k=2.3), 0.0)
        self.assertEqual(pacer.fast_share(affordable=5.0, demand_rate=0.0, k=2.3), 0.0)

    def test_share_is_capped_at_all_fast(self) -> None:
        self.assertEqual(pacer.fast_share(affordable=10.0, demand_rate=1.0, k=2.0), 1.0)

    def test_fast_takes_the_front_of_each_half_hour(self) -> None:
        start = NOW.replace(minute=30)
        self.assertEqual(pacer.slot_tier(0.5, start + timedelta(minutes=14)), "fast")
        self.assertEqual(pacer.slot_tier(0.5, start + timedelta(minutes=16)), "default")
        self.assertEqual(pacer.slot_tier(0.0, start), "default")

    def test_checkpoints_count_resets_refreshes_and_buffers(self) -> None:
        notes = [
            FakeNote("codex", {"weekly_remaining_usage": "77", "resets": local(164),
                               "limit_reset_count": "2", "limit_reset": "2026-10-22T16:55:26-04:00"}),
            FakeNote("codex", {"weekly_remaining_usage": "0", "resets": local(140), "limit_reset_count": "0"}),
            FakeNote("claude", {"weekly_remaining_usage": "90", "resets": local(10)}),
        ]
        points = pacer.checkpoints(pacer.accounts(notes, NOW), NOW)
        self.assertEqual([round(p["hours"]) for p in points], [140, 164])
        # 77 + 2 limit resets, less the buffer; the second also has the first refresh,
        # less the leftover the first account loses when it resets.
        self.assertEqual([p["budget"] for p in points], [272.0, 367.0])

    def test_a_closed_window_has_refreshed(self) -> None:
        notes = [FakeNote("codex", {"weekly_remaining_usage": "null", "resets": local(-2)})]
        self.assertEqual(pacer.accounts(notes, NOW), [pacer.Account(100.0, None, 0)])

    def test_a_lapsed_limit_reset_is_not_counted(self) -> None:
        notes = [FakeNote("codex", {"weekly_remaining_usage": "50", "resets": local(5),
                                    "limit_reset_count": "1", "limit_reset": "2026-09-30T10:00:00-04:00"})]
        self.assertEqual(pacer.accounts(notes, NOW)[0].limit_resets, 0)


def rollout_line(kind: str, at: datetime, payload: Mapping[str, object]) -> str:
    return json.dumps({"timestamp": at.isoformat(), "type": kind, "payload": payload})


class MeasurementTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()

    @override
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def rollout(self, name: str, lines: list[str], tail: str = "") -> Path:
        path = self.root / name
        _ = path.write_text("\n".join(lines) + "\n" + tail, encoding="utf-8")
        return path

    def test_tokens_take_the_tier_in_force_at_thread_start(self) -> None:
        state = pacer.empty_state()
        state["history"] = [[(NOW - timedelta(hours=2)).isoformat(), "fast"], [(NOW - timedelta(hours=1)).isoformat(), "default"]]
        usage = {"usage": {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 0}}
        early = self.rollout("a.jsonl", [
            rollout_line("session_meta", NOW, {"timestamp": (NOW - timedelta(minutes=90)).isoformat()}),
            rollout_line("turn_context", NOW, {"model": "gpt-test"}),
            rollout_line("token_usage_record", NOW, usage),
        ])
        late = self.rollout("b.jsonl", [
            rollout_line("session_meta", NOW, {"timestamp": (NOW - timedelta(minutes=30)).isoformat()}),
            rollout_line("turn_context", NOW, {"model": "gpt-test"}),
            rollout_line("token_usage_record", NOW, usage),
        ], tail='{"timestamp": "partial')
        pacer.ingest(state, iter([early, late]))
        tokens = state["buckets"][pacer.hour_key(NOW)]["tokens"]
        self.assertEqual(tokens, {"fast gpt-test": 1000.0, "default gpt-test": 1000.0})
        # The unfinished last line waits for the next pass.
        self.assertEqual(state["files"][str(late)]["offset"], late.stat().st_size - len('{"timestamp": "partial'))

    def test_spend_ignores_account_switches_and_limit_resets(self) -> None:
        def reading(minutes: int, used: float, window: int) -> str:
            limits = {"limit_id": "codex", "primary": {"used_percent": used, "resets_at": window}}
            return rollout_line("event_msg", NOW + timedelta(minutes=minutes), {"type": "token_count", "rate_limits": limits})

        week = 1_800_000_000
        path = self.rollout("c.jsonl", [
            reading(0, 10, week), reading(1, 11, week), reading(2, 12, week),
            reading(3, 60, week + 999_999),        # another account: a new window, no spend
            reading(4, 0, week), reading(5, 1, week),  # a limit reset, then spend again
            reading(6, 40, week),                  # an impossible jump is not spend
        ])
        state = pacer.empty_state()
        pacer.ingest(state, iter([path]))
        self.assertEqual(state["buckets"][pacer.hour_key(NOW)]["spent"], 3.0)

    def test_fit_recovers_the_fast_premium(self) -> None:
        state = pacer.empty_state()
        for hour in range(60):
            default = 1e6 * (1 + hour % 3)
            fast = 1e6 * (hour % 4)
            state["buckets"][pacer.hour_key(NOW - timedelta(hours=hour))] = {
                "spent": 0.1e-6 * default + 0.25e-6 * fast,
                "tokens": {"default gpt-test": default, "fast gpt-test": fast},
            }
        costs = pacer.fit(state, NOW)
        self.assertAlmostEqual(costs.k, 2.5)
        self.assertEqual(costs.model, "gpt-test")
        # Spend less its fast premium is every token priced at default.
        windows = pacer.demand(state, costs.k, NOW)
        self.assertAlmostEqual(windows["24h"], sum(0.1 * (1 + h % 3 + h % 4) for h in range(24)) / 24)


if __name__ == "__main__":
    _ = unittest.main()
