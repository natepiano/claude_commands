"""Large decision batches retain every finding and release dropped slices."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import approved_renames
import style_plan
import style_proposals

import berth_board
import rules
import style_ledger

from style_plan_test_support import StylePlanFixture


@final
class StylePlanTests(StylePlanFixture):
    def test_decision_refill_slices_forty_restructure_files_and_releases_dropped_slice(self) -> None:
        rule = self.rule("decision", "tier: C\nfamily: names\ninput_scope: file\nmode: propose\n")
        files = tuple(f"crates/alpha/src/piece_{index:02}.rs" for index in range(40))
        for path in files:
            self.write(path, "fn work() {}\n")
        self.tip = self.commit()
        _ = self.git("merge", "--ff-only", "main", cwd=self.unit)
        _ = self.git("merge", "--ff-only", "main", cwd=self.style)
        self.sync()
        folder = self.root / "issues"
        folder.mkdir()
        issue = folder / "decision.md"
        ledger = self.ledger()
        blob = next(item.blob for item in rules.load_rules(self.repo) if item.rule_id == rule)
        for path in files:
            finding = ledger.hold(rule, path, 1, "split", "restructure", "before", "after", blob, "outside")
            ledger.file_held(finding.finding_id, issue)
        _ = issue.write_text("---\nstatus: open\ntags: [proposal, style]\nunit: names-unit\nplan: approved\n"
                             + f"style_rule: {rule}\nstyle_crate: crates/alpha\nstyle_rule_blob: {blob}\n---\n")
        _ = self.doc.write_text(self.doc.read_text().replace("## Units", f"- **Proposals:** {folder}\n\n## Units"))
        first_slice = style_plan.read_slices(self.repo, self.tip, files, style_plan.BatchLimits(6, 3000, 5))[0]
        with (mock.patch.object(berth_board, "read_board", return_value=self.board(*first_slice)),
              mock.patch.object(style_proposals, "proposal_command")):
            code, _, errors = self.run_next()
        self.assertEqual(code, 0, errors)
        first_ticket = ledger.ticket_open("names-unit")[0]
        self.assertLessEqual(len(first_ticket["files"]), 6)
        self.assertFalse(set(first_ticket["files"]) & set(first_slice))
        ledger.ticket_drop(first_ticket["id"])
        self.assertTrue(all(isinstance(finding.build_ticket, style_ledger.NoBuildTicket)
                            for finding in ledger.proposal_findings(issue)))
        built: set[str] = set()
        with mock.patch.object(style_proposals, "proposal_command"):
            for _ in range(40):
                code, _, errors = self.run_next()
                self.assertEqual(code, 0, errors)
                ticket = ledger.ticket_open("names-unit")[0]
                self.assertEqual(ticket["kind"], "proposal")
                self.assertLessEqual(len(ticket["files"]), 6)
                self.assertFalse(built & set(ticket["files"]))
                built.update(ticket["files"])
                ledger.ticket_checkpoint(ticket["id"], self.tip)
                not_built = ledger.ticket_merge(ticket["id"])
                self.assertEqual(not_built, tuple(style_ledger.FindingNotBuilt(identity, approved_renames.NoAppliedCheck("restructure"))
                                                  for identity in sorted(ticket["finding_ids"])))
                for identity in ticket["finding_ids"]:
                    ledger.mark_built(identity)
                if len(built) == 40:
                    break
        self.assertEqual(built, set(files))
        self.assertTrue(all(isinstance(finding.state, style_ledger.Built) for finding in ledger.proposal_findings(issue)))

if __name__ == "__main__":
    _ = unittest.main()
