"""Type decisions preserve both read-limit batches."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style_proposals

import style_ledger

from style_plan_test_support import StylePlanFixture


@final
class StylePlanTests(StylePlanFixture):
    def test_decision_refill_makes_two_read_limit_tickets_for_twenty_six_type_files(self) -> None:
        rule = self.rule("types-decision")
        files = tuple(f"crates/alpha/src/piece_{index:02}.rs" for index in range(26))
        for path in files:
            self.write(path, "fn work() -> u32 { 1 }\n")
        self.commit_to_every_checkout()
        issue = self.approved_decision(rule, files, "type")
        expected = (files[:25], files[25:])
        ledger = self.ledger()
        tickets: list[list[str]] = []
        with mock.patch.object(style_proposals, "proposal_command"):
            for _ in expected:
                code, _, errors = self.run_next()
                self.assertEqual(code, 0, errors)
                ticket = ledger.ticket_open("names-unit")[0]
                self.assertEqual(ticket["kind"], "proposal")
                tickets.append(ticket["files"])
                ledger.ticket_checkpoint(ticket["id"], self.tip)
                _ = ledger.ticket_merge(ticket["id"])
                for identity in ticket["finding_ids"]:
                    ledger.mark_built(identity)
            self.assertEqual(self.run_next()[0], 0)
        self.assertEqual(sorted(tuple(files) for files in tickets), sorted(expected))
        self.assertEqual(len(ledger.proposal_tickets()), 2)
        self.assertTrue(all(isinstance(finding.state, style_ledger.Built) for finding in ledger.proposal_findings(issue)))

if __name__ == "__main__":
    _ = unittest.main()
