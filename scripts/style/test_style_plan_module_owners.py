"""Busy module declarations protect restructure decisions."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import final

sys.path.insert(0, str(Path(__file__).resolve().parent))

import style_ledger

from style_plan_test_support import StylePlanFixture


@final
class StylePlanTests(StylePlanFixture):
    def test_decision_refill_holds_a_restructure_whose_owner_declares_it_and_is_busy(self) -> None:
        issue, finding = self.owned_module_decision("crates/alpha/src/terminal/startup_tests.rs", "crates/alpha/src/terminal.rs",
                                                    "#[cfg(test)]\nmod startup_tests;\n")
        output = self.refill_with_busy("crates/alpha/src/terminal.rs")
        self.assertIn(f"waiting {issue}: {self.ledger().named_held([finding])[0].path} edits crates/alpha/src/terminal.rs, busy (1 finding)\n", output)
        self.assertIsInstance(self.ledger().proposal_findings(issue)[0].build_ticket, style_ledger.NoBuildTicket)

    def test_decision_refill_holds_a_restructure_whose_owner_declares_it_by_a_path_attribute(self) -> None:
        issue, finding = self.owned_module_decision(
            "crates/alpha/src/census/hierarchy_snapshot_tests.rs", "crates/alpha/src/census/hierarchy.rs",
            '#[cfg(test)]\n#[path = "hierarchy_snapshot_tests.rs"]\nmod snapshot_tests;\n')
        output = self.refill_with_busy("crates/alpha/src/census/hierarchy.rs")
        self.assertIn(f"waiting {issue}: {self.ledger().named_held([finding])[0].path} edits crates/alpha/src/census/hierarchy.rs, busy (1 finding)\n", output)

    def test_decision_refill_holds_a_restructure_whose_owner_declares_it_only_at_the_unit_base(self) -> None:
        issue, finding = self.owned_module_decision("crates/alpha/src/terminal/startup_tests.rs", "crates/alpha/src/terminal.rs", "")
        # The unit's branch adds the declaration after the synced tip.
        _ = self.unit_commit("crates/alpha/src/terminal.rs", "#[cfg(test)]\nmod startup_tests;\npub fn open() {}\n")
        output = self.refill_with_busy("crates/alpha/src/terminal.rs")
        self.assertIn(f"waiting {issue}: {self.ledger().named_held([finding])[0].path} edits crates/alpha/src/terminal.rs, busy (1 finding)\n", output)

    def test_decision_refill_holds_a_restructure_whose_owner_has_a_path_attribute_in_an_inline_module(self) -> None:
        issue, finding = self.owned_module_decision("crates/alpha/src/terminal/startup_tests.rs", "crates/alpha/src/lib.rs",
                                                    'mod terminal {\n    #[path = "startup_tests.rs"]\n    mod startup_tests;\n}\n')
        output = self.refill_with_busy("crates/alpha/src/lib.rs")
        self.assertIn(f"waiting {issue}: {self.ledger().named_held([finding])[0].path} edits crates/alpha/src/lib.rs, busy (1 finding)\n", output)

    def test_decision_refill_holds_a_restructure_whose_owner_has_a_cfg_attr_path(self) -> None:
        issue, finding = self.owned_module_decision("crates/alpha/src/terminal/startup_tests.rs", "crates/alpha/src/terminal.rs",
                                                    '#[cfg_attr(test, path = "terminal/startup_tests.rs")]\nmod startup_tests;\n')
        output = self.refill_with_busy("crates/alpha/src/terminal.rs")
        self.assertIn(f"waiting {issue}: {self.ledger().named_held([finding])[0].path} edits crates/alpha/src/terminal.rs, busy (1 finding)\n", output)

    def test_decision_refill_holds_a_restructure_whose_owner_mounts_its_folder_on_an_inline_module(self) -> None:
        issue, finding = self.owned_module_decision("crates/alpha/src/terminal/startup_tests.rs", "crates/alpha/src/lib.rs",
                                                    '#[path = "terminal"]\nmod shell {\n    mod startup_tests;\n}\n')
        output = self.refill_with_busy("crates/alpha/src/lib.rs")
        self.assertIn(f"waiting {issue}: {self.ledger().named_held([finding])[0].path} edits crates/alpha/src/lib.rs, busy (1 finding)\n", output)

    def test_decision_refill_holds_a_restructure_whose_owner_mounts_its_folder_by_a_raw_path_attribute(self) -> None:
        issue, finding = self.owned_module_decision("crates/alpha/src/terminal/startup_tests.rs", "crates/alpha/src/lib.rs",
                                                    '#[r#path = "terminal"]\nmod shell {\n    mod startup_tests;\n}\n')
        output = self.refill_with_busy("crates/alpha/src/lib.rs")
        self.assertIn(f"waiting {issue}: {self.ledger().named_held([finding])[0].path} edits crates/alpha/src/lib.rs, busy (1 finding)\n", output)

    def test_decision_refill_holds_a_restructure_whose_owner_declares_it_by_a_raw_path_attribute(self) -> None:
        issue, finding = self.owned_module_decision("crates/alpha/src/terminal/startup_tests.rs", "crates/alpha/src/lib.rs",
                                                    '#[r#path = "terminal/startup_tests.rs"]\nmod probe;\n')
        output = self.refill_with_busy("crates/alpha/src/lib.rs")
        self.assertIn(f"waiting {issue}: {self.ledger().named_held([finding])[0].path} edits crates/alpha/src/lib.rs, busy (1 finding)\n", output)

    def test_decision_refill_holds_a_restructure_whose_owner_declares_it_by_a_raw_module_name(self) -> None:
        issue, finding = self.owned_module_decision("crates/alpha/src/terminal/startup_tests.rs", "crates/alpha/src/terminal.rs",
                                                    "#[cfg(test)]\nmod r#startup_tests;\n")
        output = self.refill_with_busy("crates/alpha/src/terminal.rs")
        self.assertIn(f"waiting {issue}: {self.ledger().named_held([finding])[0].path} edits crates/alpha/src/terminal.rs, busy (1 finding)\n", output)

    def test_decision_refill_holds_a_restructure_whose_owner_declares_it_by_an_escaped_path_string(self) -> None:
        # The string names startup_tests.rs, but the owner does not hold the word startup_tests.
        issue, finding = self.owned_module_decision("crates/alpha/src/terminal/startup_tests.rs", "crates/alpha/src/terminal.rs",
                                                    '#[cfg(test)]\n#[path = "terminal/startup\\u{5f}tests.rs"]\nmod tests;\n')
        output = self.refill_with_busy("crates/alpha/src/terminal.rs")
        self.assertIn(f"waiting {issue}: {self.ledger().named_held([finding])[0].path} edits crates/alpha/src/terminal.rs, busy (1 finding)\n", output)

    def test_decision_refill_holds_a_restructure_of_a_mod_rs_file_whose_declaring_root_is_busy(self) -> None:
        issue, finding = self.owned_module_decision("crates/alpha/src/census/mod.rs", "crates/alpha/src/terminal.rs", "")
        output = self.refill_with_busy("crates/alpha/src/lib.rs")
        self.assertIn(f"waiting {issue}: {self.ledger().named_held([finding])[0].path} edits crates/alpha/src/lib.rs, busy (1 finding)\n", output)

    def test_decision_refill_builds_a_restructure_whose_busy_file_names_its_module_without_declaring_it(self) -> None:
        _, finding = self.owned_module_decision("crates/alpha/src/terminal/startup_tests.rs", "crates/alpha/src/terminal.rs",
                                                "#[cfg(test)]\nmod startup_tests;\n")
        # The busy file holds the word startup_tests as a function name, and the owner is free.
        self.write(self.sources[1], "pub fn startup_tests() {}\n")
        self.commit_to_every_checkout()
        output = self.refill_with_busy(self.sources[1])
        self.assertNotIn("waiting", output)
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["files"], ticket["finding_ids"]), (["crates/alpha/src/terminal/startup_tests.rs"], [finding]))

    def test_decision_refill_builds_a_restructure_whose_busy_file_mounts_another_file_by_path(self) -> None:
        _, finding = self.owned_module_decision("crates/alpha/src/terminal/startup_tests.rs", "crates/alpha/src/terminal.rs",
                                                "#[cfg(test)]\nmod startup_tests;\n")
        # The busy file's `path` attribute is read with certainty, and names another file.
        self.write(self.sources[1], '#[path = "probe/startup_tests.rs"]\nmod startup_tests;\n')
        self.commit_to_every_checkout()
        output = self.refill_with_busy(self.sources[1])
        self.assertNotIn("waiting", output)
        ticket = self.ledger().ticket_open("names-unit")[0]
        self.assertEqual((ticket["files"], ticket["finding_ids"]), (["crates/alpha/src/terminal/startup_tests.rs"], [finding]))

if __name__ == "__main__":
    _ = unittest.main()
