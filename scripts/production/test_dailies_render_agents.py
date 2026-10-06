"""The report's agent lines use temporary notes, readings, and a fixed zone."""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import override
from unittest.mock import patch

import dailies_render


AT = "2026-10-05T12:45"
ZONE = "America/Los_Angeles"


class DailiesAgentsTests(unittest.TestCase):
    root: Path = Path()
    agents: Path = Path()
    readings: Path = Path()
    run_outs: Path = Path()
    holders: Path = Path()
    original_tz: str | None = None
    original_hold_dir: str | None = None

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.agents = self.root / "agents"
        self.agents.mkdir()
        self.readings = self.root / "readings.jsonl"
        self.run_outs = self.root / "run_outs.jsonl"
        self.holders = self.root / "holders"
        self.holders.mkdir()
        self.original_tz = os.environ.get("TZ")
        self.original_hold_dir = os.environ.get("BUILD_HOLD_DIR")
        os.environ["TZ"] = ZONE
        os.environ["BUILD_HOLD_DIR"] = str(self.holders)
        time.tzset()
        _ = self.enterContext(patch.object(dailies_render, "AGENTS_DIR", self.agents, create=True))
        _ = self.enterContext(patch.object(dailies_render, "READINGS_LOG", self.readings, create=True))
        _ = self.enterContext(patch.object(dailies_render, "RUN_OUTS_LOG", self.run_outs, create=True))
        _ = self.enterContext(patch.object(dailies_render, "CHART_CONF", self.root / "chart.conf"))

    @override
    def tearDown(self) -> None:
        if self.original_tz is None:
            _ = os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.original_tz
        if self.original_hold_dir is None:
            _ = os.environ.pop("BUILD_HOLD_DIR", None)
        else:
            os.environ["BUILD_HOLD_DIR"] = self.original_hold_dir
        time.tzset()

    def note(
        self,
        name: str,
        *,
        usage: str | None = "40",
        resets: str | None = "2026-10-11T23:00:00",
        checked: str = "2026-10-05T19:45:00+00:00",
        count: str | None = "1",
        limit: str | None = "2026-10-22T12:00:00",
        state: str = "active",
    ) -> None:
        fields = [f"state: {state}", f"weekly_usage_checked_at: {checked}"]
        if resets is not None:
            fields.append(f"resets: {resets}")
        if usage is not None:
            fields.append(f"weekly_remaining_usage: {usage}")
        if count is not None:
            fields.append(f"limit_reset_count: {count}")
        if limit is not None:
            fields.append(f"limit_reset: {limit}")
        _ = (self.agents / f"{name}.md").write_text("---\n" + "\n".join(fields) + "\n---\nBody.\n")

    def log(self, *readings: tuple[str, str, float]) -> None:
        _ = self.readings.write_text("".join(
            json.dumps({"account": account, "at": at, "remaining": remaining}) + "\n"
            for account, at, remaining in readings
        ))

    def run_report(self, *, at: str = AT, waiting: bool = False, held: bool = False) -> list[str]:
        unit: dict[str, object] = {
            "unit": "widgets", "label": "widget", "project": "clear controls",
            "phase": "Phase 1 of 2: refine controls", "started": "2026-10-05T11:00",
            "held": None, "update": "refining controls", "eta": {"time": "15:00", "percent": 50},
        }
        if held:
            unit["build_hold"] = True
            _ = (self.holders / "other").write_text(json.dumps({
                "holder": "other", "since": "2026-10-05T12:00:00-07:00",
                "for": "the shared check", "release_eta": "2026-10-05T13:00:00-07:00",
            }) + "\n")
        source = self.root / "input.json"
        _ = source.write_text(json.dumps({"length": "simple", "zone": ZONE, "units": [unit]}))
        args = [str(source), "--at", at]
        if waiting:
            outstanding = self.root / "outstanding.json"
            _ = outstanding.write_text(json.dumps([{"since": "2026-10-05T12:00", "text": "choose the icon"}]))
            args.extend(["--outstanding", str(outstanding)])
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = dailies_render.main(args)
        self.assertEqual(code, 0, errors.getvalue())
        return output.getvalue().splitlines()

    def agent_lines(self, lines: list[str]) -> list[str]:
        return [line for line in lines if line.startswith(("* claude ", "* codex ")) or line == "* none active"]

    def run_footer(self, at: str) -> str:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = dailies_render.main(["--footer", "--zone", ZONE, "--at", at])
        self.assertEqual(code, 0, errors.getvalue())
        return output.getvalue()

    def test_reset_starts_a_new_trailing_pace(self) -> None:
        self.note("claude 1", usage="81", checked="2026-10-06T17:11:00+00:00")
        self.log(
            ("claude 1", "2026-10-05T19:25:00+00:00", 58),
            ("claude 1", "2026-10-06T13:15:00+00:00", 1),
            ("claude 1", "2026-10-06T13:17:00+00:00", 100),
            ("claude 1", "2026-10-06T17:11:00+00:00", 81),
        )
        line = self.agent_lines(self.run_report(at="2026-10-06T10:11"))[0]
        self.assertIn("19%; runs out about", line)
        self.assertIn("before its Sun 23:00 refill", line)
        self.assertNotIn("lasts to its", line)

    def test_week_pace_fallback_counts_from_redeemed_reset(self) -> None:
        self.note("claude 1", usage="20", checked="2026-10-06T13:47:00+00:00")
        self.log(
            ("claude 1", "2026-10-06T13:15:00+00:00", 1),
            ("claude 1", "2026-10-06T13:17:00+00:00", 100),
            ("claude 1", "2026-10-06T13:47:00+00:00", 20),
        )
        line = self.agent_lines(self.run_report(at="2026-10-06T06:47"))[0]
        self.assertIn("80%; runs out about 06:55 PDT today", line)

    def test_stamped_footer_uses_readings_through_end_of_stamped_minute(self) -> None:
        stamp = "2026-10-06T10:11"
        self.note("claude 1", usage="81", checked="2026-10-06T17:11:30+00:00")
        self.log(
            ("claude 1", "2026-10-06T15:11:00+00:00", 91),
            ("claude 1", "2026-10-06T17:11:30+00:00", 81),
        )
        before = self.run_footer(stamp)
        self.assertIn("* claude 1: 19%", before)
        self.note("claude 1", usage="80", checked="2026-10-06T17:12:10+00:00")
        self.log(
            ("claude 1", "2026-10-06T15:11:00+00:00", 91),
            ("claude 1", "2026-10-06T17:11:30+00:00", 81),
            ("claude 1", "2026-10-06T17:12:10+00:00", 80),
        )
        self.assertEqual(self.run_footer(stamp), before)
        self.assertIn("* claude 1: 20%", self.run_footer("2026-10-06T10:12"))

    def test_stamped_minute_uses_its_own_repeated_hour_reading(self) -> None:
        """Each fall-back minute ends 60 seconds after its own instant."""
        self.note("claude 1", usage="79", resets="2026-11-07T12:00:00-08:00",
                  checked="2026-11-01T09:31:05+00:00", count=None, limit=None)
        self.log(
            ("claude 1", "2026-11-01T08:59:30+00:00", 81),
            ("claude 1", "2026-11-01T09:30:30+00:00", 80),
            ("claude 1", "2026-11-01T09:31:05+00:00", 79),
        )
        self.assertIn("* claude 1: 19%;", self.run_footer("2026-11-01T01:59-07:00"))
        self.assertIn("* claude 1: 20%;", self.run_footer("2026-11-01T01:30-08:00"))

    def test_approved_lines_order_and_footer_position(self) -> None:
        self.note("claude 1", usage="60", checked="2026-10-05T08:10:00+00:00")
        self.note("codex 2", usage="22", resets="2026-10-11T02:25:00",
                  count="1", limit="2026-10-29T03:00:00-04:00")
        self.note("codex 3", state="inactive")
        self.log(
            ("claude 1", "2026-10-05T06:10:00+00:00", 64),
            ("claude 1", "2026-10-05T08:10:00+00:00", 60),
            ("codex 2", "2026-10-05T15:45:00+00:00", 33),
            ("codex 2", "2026-10-05T19:45:00+00:00", 22),
        )
        _ = self.readings.write_text("{bad json}\n" + self.readings.read_text())
        lines = self.run_report(waiting=True, held=True)
        self.assertEqual(self.agent_lines(lines), [
            "* claude 1: 40%; runs out about Tue 07:10 PDT, before its Sun 23:00 refill; 1 reset available until Oct 22",
            "* codex 2: 78%; runs out about 20:45 PDT today, before its Sun 02:25 refill; 1 reset available until Oct 29",
        ])
        chart_end = len(lines) - 1 - lines[::-1].index("```")
        hold = next(i for i, line in enumerate(lines) if line.startswith("* build hold:"))
        agents = lines.index(self.agent_lines(lines)[0])
        schedule = lines.index("* no dailies scheduled")
        self.assertLess(chart_end, hold)
        self.assertLess(hold, agents)
        self.assertEqual(lines[schedule - 1], self.agent_lines(lines)[-1])
        self.assertFalse(any("waiting on you:" in line for line in lines))
        self.assertNotIn("codex 3", "\n".join(lines))

        output = io.StringIO()
        with redirect_stdout(output):
            code = dailies_render.main(["--footer", "--zone", ZONE, "--at", AT])
        self.assertEqual(code, 0)
        footer_lines = output.getvalue().splitlines()
        self.assertEqual(self.agent_lines(footer_lines), self.agent_lines(lines))
        self.assertEqual(footer_lines[:4], ["", "---", "12:45 PDT update:", ""])
        self.assertEqual(footer_lines[-1], "* no dailies scheduled")

    def test_agent_lines_use_run_out_words_for_every_usage_state(self) -> None:
        """All four known-usage states use the specified full line."""
        self.note("claude 1", usage="60", checked="2026-10-05T08:10:00+00:00")
        self.note("claude 2", usage="60", count="0", limit=None)
        self.note("codex 1", usage="99", resets="2026-10-11T12:45:00", count=None, limit=None)
        self.note("codex 2", usage="0", count=None, limit=None)
        self.log(
            ("claude 1", "2026-10-05T06:10:00+00:00", 64),
            ("claude 1", "2026-10-05T08:10:00+00:00", 60),
            ("claude 2", "2026-10-05T17:45:00+00:00", 60),
            ("claude 2", "2026-10-05T19:45:00+00:00", 60),
        )
        lines = self.agent_lines(self.run_report())
        self.assertEqual(lines, [
            "* claude 1: 40%; runs out about Tue 07:10 PDT, before its Sun 23:00 refill; 1 reset available until Oct 22",
            "* claude 2: 40%; does not run out at this pace, so it hits its Sun 23:00 refill first; no resets available",
            "* codex 1: 1%; runs out about Tue 11:45 PST, so it hits its Sun 12:45 refill first; resets unknown",
            "* codex 2: 100%; ran out, back at its Sun 23:00 refill; resets unknown",
        ])
        self.assertFalse(any("of the week used" in line or "lasts to" in line for line in lines))

    def test_repeated_hour_run_out_shows_pst(self) -> None:
        self.note("claude 1", usage="20", resets="2026-11-07T12:00:00-08:00",
                  checked="2026-11-01T07:30:00+00:00", count=None, limit=None)
        self.log(
            ("claude 1", "2026-11-01T06:30:00+00:00", 30),
            ("claude 1", "2026-11-01T07:30:00+00:00", 20),
        )
        self.assertEqual(self.agent_lines(self.run_report(at="2026-11-01T00:45")), [
            "* claude 1: 80%; runs out about 01:30 PST today, before its Sat 12:00 refill; resets unknown",
        ])

    def test_run_out_rounds_across_fall_back(self) -> None:
        self.note("claude 1", usage="10", resets="2026-11-07T12:00:00-08:00",
                  checked="2026-11-01T07:59:45+00:00", count=None, limit=None)
        self.log(
            ("claude 1", "2026-11-01T06:59:45+00:00", 20),
            ("claude 1", "2026-11-01T07:59:45+00:00", 10),
        )
        self.assertEqual(self.agent_lines(self.run_report(at="2026-11-01T01:00")), [
            "* claude 1: 90%; runs out about 01:00 PST today, before its Sat 12:00 refill; resets unknown",
        ])

    def test_agents_follow_multiple_holds_before_schedule(self) -> None:
        _ = (self.holders / "third").write_text(json.dumps({
            "holder": "third", "since": "2026-10-05T12:10:00-07:00",
            "for": "the review", "release_eta": "2026-10-05T13:10:00-07:00",
        }) + "\n")
        lines = self.run_report(waiting=True, held=True)
        holds = [index for index, line in enumerate(lines) if line.startswith("* build hold:")]
        agents = lines.index(self.agent_lines(lines)[0])
        schedule = lines.index("* no dailies scheduled")
        self.assertEqual(len(holds), 2)
        self.assertLess(holds[-1], agents)
        self.assertLess(agents, schedule)
        self.assertEqual(lines[agents - 1], lines[holds[-1]])
        self.assertEqual(lines[schedule - 1], self.agent_lines(lines)[-1])

    def test_short_span_uses_week_pace(self) -> None:
        self.note("claude 1", usage="60", resets="2026-10-11T12:45:00", count=None, limit=None)
        self.log(
            ("claude 1", "2026-10-05T19:15:00+00:00", 70),
            ("claude 1", "2026-10-05T19:45:00+00:00", 60),
        )
        self.assertEqual(self.agent_lines(self.run_report()), [
            "* claude 1: 40%; runs out about Wed 00:45 PDT, before its Sun 12:45 refill; resets unknown",
        ])

    def test_past_run_outs_lean_the_prediction_early(self) -> None:
        self.note("claude 1", usage="60", count=None, limit=None)
        self.log(
            ("claude 1", "2026-10-05T18:45:00+00:00", 70),
            ("claude 1", "2026-10-05T19:45:00+00:00", 60),
        )
        self.assertEqual(self.agent_lines(self.run_report()), [
            "* claude 1: 40%; runs out about 18:45 PDT today, before its Sun 23:00 refill; resets unknown",
        ])
        _ = self.run_outs.write_text("".join(json.dumps(record) + "\n" for record in [
            {"account": "codex 2", "ended": "2026-10-06T02:18:47+00:00", "ratios": [0.5, 0.5]},
            {"account": "claude 1", "ended": "2026-10-06T13:15:56+00:00", "ratios": [0.5, 0.5]},
        ]))
        self.assertEqual(self.agent_lines(self.run_report()), [
            "* claude 1: 40%; runs out about 15:45 PDT today, before its Sun 23:00 refill; resets unknown",
        ])

    def test_readings_before_last_refill_are_ignored(self) -> None:
        self.note("claude 1", usage="60", resets="2026-10-12T11:45:00", count=None, limit=None)
        self.log(
            ("claude 1", "2026-10-05T17:45:00+00:00", 90),
            ("claude 1", "2026-10-05T19:15:00+00:00", 70),
            ("claude 1", "2026-10-05T19:45:00+00:00", 60),
        )
        self.assertEqual(self.agent_lines(self.run_report()), [
            "* claude 1: 40%; runs out about 14:15 PDT today, before its Mon 11:45 refill; resets unknown",
        ])

    def test_missing_or_unreadable_log_uses_week_pace(self) -> None:
        self.note("claude 1", usage="60", resets="2026-10-11T12:45:00", count=None, limit=None)
        expected = [
            "* claude 1: 40%; runs out about Wed 00:45 PDT, before its Sun 12:45 refill; resets unknown",
        ]
        self.assertEqual(self.agent_lines(self.run_report()), expected)
        self.readings.mkdir()
        self.assertEqual(self.agent_lines(self.run_report()), expected)

    def test_zero_rate_late_runout_and_refill_today(self) -> None:
        self.note("claude 1", usage="60", resets="2026-10-11T12:45:00", count="0")
        self.note("claude 2", usage="99", resets="2026-10-11T12:45:00", count="2")
        self.note("codex 1", usage="60", resets="2026-10-05T23:00:00", count="1", limit=None)
        self.log(
            ("claude 1", "2026-10-05T17:45:00+00:00", 60),
            ("claude 1", "2026-10-05T19:45:00+00:00", 60),
            ("codex 1", "2026-10-05T17:45:00+00:00", 60),
            ("codex 1", "2026-10-05T19:45:00+00:00", 60),
        )
        self.assertEqual(self.agent_lines(self.run_report()), [
            "* claude 1: 40%; does not run out at this pace, so it hits its Sun 12:45 refill first; no resets available",
            "* claude 2: 1%; runs out about Tue 11:45 PST, so it hits its Sun 12:45 refill first; 2 resets available until Oct 22",
            "* codex 1: 40%; does not run out at this pace, so it hits its 23:00 today refill first; 1 reset available",
        ])

    def test_unknown_usage_and_no_active_note(self) -> None:
        self.note("claude 1", usage="null", count=None, limit=None)
        self.note("codex 1", resets="2026-10-04T23:00:00", count="0")
        self.assertEqual(self.agent_lines(self.run_report()), [
            "* claude 1: week's usage unknown; refills Sun 23:00; resets unknown",
            "* codex 1: week's usage unknown; refill time unknown; no resets available",
        ])
        (self.agents / "claude 1.md").unlink()
        (self.agents / "codex 1.md").unlink()
        self.assertEqual(self.agent_lines(self.run_report()), ["* none active"])

    def test_exhausted_and_missing_usage(self) -> None:
        self.note("claude 1", usage="0", count="1")
        self.note("codex 1", usage=None, resets=None, count=None, limit=None)
        self.assertEqual(self.agent_lines(self.run_report()), [
            "* claude 1: 100%; ran out, back at its Sun 23:00 refill; 1 reset available until Oct 22",
            "* codex 1: week's usage unknown; refill time unknown; resets unknown",
        ])


if __name__ == "__main__":
    _ = unittest.main()
