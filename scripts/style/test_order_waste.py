"""order_waste counts stale findings and reworked lines by rule pair from a ledger and its repository."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import cast, final, override

import order_waste
import style_ledger
from order_waste import AnalysisOrder, FamilyRead, RuleFix

ALPHA = """pub fn alpha_configuration_value() -> u32 {
    let selected_mode_number = 1;
    selected_mode_number + 1
}

pub fn beta_registration_counter() -> u32 {
    let registration_counter_start = 10;
    registration_counter_start * 2
}
"""
GAMMA = """pub fn gamma_layout_width() -> u32 {
    let layout_width_pixels = 58;
    layout_width_pixels
}
"""
MODULES = [f"global:rust/{rule}.md" for rule in order_waste.MODULES_RULES]
IF_ELSE = "global:rust/if-else-chains-signal-missing-types.md"
INVALID_STATES = "global:rust/make-invalid-states-unrepresentable.md"
SPELL_OUT = "global:rust/spell-out-names.md"
TYPE_NAMED = "global:rust/prefer-type-named-fields-and-bindings.md"
NO_MAGIC = "global:rust/no-magic-values.md"


def at(hour: int, minute: int = 0) -> str:
    return f"2026-10-20T{hour:02}:{minute:02}:00+00:00"


@final
class OrderWasteTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    connection: sqlite3.Connection = sqlite3.connect(":memory:")

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.root / "sample"
        self.repo.mkdir()
        _ = self.git("init", "-q", "--initial-branch=main")
        _ = self.git("config", "user.name", "Order waste tests")
        _ = self.git("config", "user.email", "order-waste@example.invalid")
        _ = self.git("config", "commit.gpgsign", "false")
        self.connection = self.enterContext(closing(sqlite3.connect(":memory:")))
        for _, migration in sorted(style_ledger.MIGRATIONS.items()):
            _ = self.connection.executescript(migration)

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *arguments], check=True, capture_output=True, text=True).stdout.strip()

    def write(self, relative: str, content: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(content)

    def replace(self, relative: str, old: str, new: str) -> None:
        text = (self.repo / relative).read_text()
        self.assertIn(old, text)
        self.write(relative, text.replace(old, new, 1))

    def commit(self, message: str) -> str:
        _ = self.git("add", "--all")
        _ = self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def ticket(self, id: str, family: str, rules: list[str], base: str, checkpoint: str, merged_at: str,
               state: str = "merged") -> None:
        _ = self.connection.execute(
            "INSERT INTO tickets(id,unit,kind,family,rule,crate,files,sites,rules,rule_blobs,state,branch,checkpoint,"
            + "created_at,updated_at,base) VALUES (?,?,'read',?,'','.','[]','[]',?,'{}',?,'',?,?,?,?)",
            (id, f"{family}-unit", family, json.dumps(rules), state, checkpoint, at(0), merged_at, base))

    def judgment(self, rule: str, path: str, verdict: str, ticket: str, recorded_at: str) -> int:
        cursor = self.connection.execute(
            'INSERT INTO judgments(rule,rule_blob,input_scope,input_fingerprint,path,"commit",verdict,verdict_detail,ticket,worker,'
            + "recorded_at) VALUES (?,'blob','file','print',?,'c',?,'',?,'worker',?)", (rule, path, verdict, ticket, recorded_at))
        return cast(int, cursor.lastrowid)

    def finding(self, id: str, rule: str, path: str, state: str, created_at: str, updated_at: str, *, ticket: str = "names-0",
                proposal: str = "", withdrawn_by: int = 0, duplicate_of: str = "", gone_at: str = "", followed_from: str = "",
                provenance: str = "unrecorded") -> None:
        _ = self.connection.execute(
            "INSERT INTO held_findings(finding_id,rule,path,line,text,kind,before,after,rule_blob,ticket,state,proposal,created_at,"
            + "updated_at,withdrawn_by,duplicate_of,declaration_gone_at,followed_from,waive_provenance) "
            + "VALUES (?,?,?,2,'text','rename','b','a','blob',?,?,?,?,?,?,?,?,?,?)",
            (id, f"global:rust/{rule}.md", path, ticket, state, proposal, created_at, updated_at, withdrawn_by, duplicate_of,
             gone_at, followed_from, provenance))

    def build_history(self) -> None:
        """types-1 writes lines, types-2 changes one of them, modules-1 moves a function, names-1 changes moved and older lines."""
        self.write("src/a.rs", ALPHA)
        self.write("src/b.rs", "pub fn delta() {}\n")
        self.write("src/c.rs", GAMMA)
        self.write("notes.md", "first note\n")
        start = self.commit("start")
        self.replace("src/a.rs", "selected_mode_number = 1;", "selected_mode_number = Mode::One as u32;")
        self.replace("src/c.rs", "layout_width_pixels = 58;", "layout_width_pixels = Width::Toast as u32;")
        self.write("notes.md", "first note changed\n")
        types_1 = self.commit("types-1")
        self.replace("src/a.rs", "Mode::One as u32;", "SelectedMode::One.number();")
        self.replace("src/a.rs", "registration_counter_start = 10;", "registration_counter_start = RegistrationStart::Ten.number();")
        self.write("notes.md", "first note changed again\n")
        types_2 = self.commit("types-2")
        alpha = (self.repo / "src/a.rs").read_text()
        beta = alpha[alpha.index("\npub fn beta"):]
        self.write("src/a.rs", alpha.replace(beta, ""))
        self.write("src/b.rs", "pub fn delta() {}\n" + beta)
        modules_1 = self.commit("modules-1")
        self.replace("src/b.rs", "registration_counter_start = Registration", "registration_counter_first = Registration")
        self.replace("src/c.rs", "Width::Toast as u32;", "Width::Toast.pixels();")
        names_1 = self.commit("names-1")
        self.ticket("types-1", "types", [IF_ELSE], start, types_1, at(1))
        self.ticket("types-2", "types", [INVALID_STATES], types_1, types_2, at(2))
        self.ticket("modules-1", "modules", MODULES, types_2, modules_1, at(3))
        self.ticket("names-1", "names", [SPELL_OUT, "global:rust/forbidden-words.md"], modules_1, names_1, at(4))
        self.ticket("values-0", "values", ["global:rust/no-magic-values.md"], names_1, names_1, at(5))
        # A dropped ticket's commits are not merged work, even where they hold another ticket's commit.
        self.ticket("dropped-1", "values", ["global:rust/no-magic-values.md", "global:rust/prefer-functional-patterns.md"],
                    start, types_1, at(1), state="dropped")
        _ = self.judgment(SPELL_OUT, "src/b.rs", "fixed", "names-1", at(4))
        _ = self.judgment(SPELL_OUT, "src/c.rs", "clean", "names-1", at(4))

    def build_findings(self) -> None:
        judged_again = self.judgment("global:rust/prefer-type-named-fields-and-bindings.md", "src/c.rs", "clean", "names-2", at(1, 30))
        judged_early = self.judgment("global:rust/prefer-type-named-fields-and-bindings.md", "src/c.rs", "clean", "names-3", at(0, 45))
        self.finding("judged-again", "prefer-type-named-fields-and-bindings", "src/c.rs", "withdrawn", at(0, 30), at(6),
                     withdrawn_by=judged_again)
        self.finding("judged-before-change", "prefer-type-named-fields-and-bindings", "src/c.rs", "withdrawn", at(0, 30), at(6),
                     withdrawn_by=judged_early)
        self.finding("person-waive", "no-magic-values", "src/a.rs", "waived", at(0, 30), at(6), provenance="person")
        self.finding("ledger-waive", "no-magic-values", "src/a.rs", "waived", at(0, 30), at(2, 30), provenance="ledger")
        self.finding("exception", "no-magic-values", "src/a.rs", "withdrawn", at(0, 30), at(6), proposal="decision.md",
                     withdrawn_by=judged_again)
        self.finding("gone", "dont-repeat-type-name-in-fields", "src/z.rs", "withdrawn", at(0, 30), at(2, 30), ticket="types-2",
                     gone_at="abc1234", followed_from="src/a.rs")
        self.finding("duplicate", "spell-out-names", "src/a.rs", "withdrawn", at(0, 30), at(6), duplicate_of="qualified")

    def measured(self) -> order_waste.Report:
        return order_waste.report(order_waste.measure(self.connection, self.repo))

    def test_counts_stale_findings_and_reworked_hunks_by_rule_pair(self) -> None:
        self.build_history()
        self.build_findings()
        data = self.measured()
        pairs = {(row["changed_by"], row["redone"]): (row["stale_findings"], row["reworked_hunks"], row["analysis"])
                 for row in data["pairs"]}
        self.assertEqual(pairs, {
            (INVALID_STATES, IF_ELSE): (0, 1, "A first, position"),
            (SPELL_OUT, INVALID_STATES): (0, 1, "B first, position"),
            ("names read", IF_ELSE): (0, 1, "B first, position"),
            (IF_ELSE, TYPE_NAMED): (1, 0, "A first, position"),
            (IF_ELSE, NO_MAGIC): (1, 0, "A first, edge"),
            (INVALID_STATES, NO_MAGIC): (1, 0, "A first, position"),
            (IF_ELSE, "global:rust/dont-repeat-type-name-in-fields.md"): (1, 0, "A first, position"),
        })
        self.assertEqual((data["changing_tickets"], data["counted_findings"]), (4, 4))
        self.assertEqual(data["not_counted"], {
            "bare rename duplicate": 1, "exception under a newer rule text": 1, "no other rule's merged fix changed its file": 1,
            "person waive": 1,
        })

    def test_moved_lines_are_not_rework_and_keep_their_writer(self) -> None:
        self.build_history()
        data = self.measured()
        self.assertFalse([row for row in data["pairs"] if row["changed_by"] == "modules read"])
        moved = next(row for row in data["pairs"] if row["changed_by"] == SPELL_OUT)
        self.assertEqual(moved["files"], ["src/b.rs"])

    def test_lists_analysis_edges_without_waste(self) -> None:
        self.build_history()
        self.build_findings()
        edges = {row["edge"]: row["seen"] for row in self.measured()["edges_without_waste"]}
        self.assertTrue(edges["spell-out-names > prefer-type-named-fields-and-bindings"])
        self.assertFalse(edges["dont-repeat-type-name-in-fields > avoid-repeated-field-affixes"])
        self.assertNotIn("if-else-chains-signal-missing-types > no-magic-values", edges)

    def test_rework_counts_only_the_net_change_of_a_ticket(self) -> None:
        self.write("src/a.rs", ALPHA)
        start = self.commit("start")
        self.replace("src/a.rs", "selected_mode_number = 1;", "selected_mode_number = Mode::One as u32;")
        types_1 = self.commit("types-1")
        self.replace("src/a.rs", "Mode::One as u32;", "SelectedMode::One.number();")
        _ = self.commit("types-2 edit")
        self.replace("src/a.rs", "SelectedMode::One.number();", "Mode::One as u32;")
        self.replace("src/a.rs", "registration_counter_start = 10;", "registration_counter_start = 20;")
        types_2 = self.commit("types-2 restore")
        self.ticket("types-1", "types", [IF_ELSE], start, types_1, at(1))
        self.ticket("types-2", "types", [INVALID_STATES], types_1, types_2, at(2))
        data = self.measured()
        self.assertEqual((data["pairs"], data["changing_tickets"]), ([], 2))

    def test_work_a_ticket_merged_in_is_not_its_change(self) -> None:
        self.write("src/a.rs", ALPHA)
        self.write("src/c.rs", GAMMA)
        start = self.commit("start")
        self.replace("src/a.rs", "selected_mode_number = 1;", "selected_mode_number = Mode::One as u32;")
        types_1 = self.commit("types-1")
        _ = self.git("checkout", "-q", "-b", "other")
        self.replace("src/a.rs", "Mode::One as u32;", "Mode::First as u32;")
        _ = self.commit("other")
        _ = self.git("checkout", "-q", "main")
        self.replace("src/c.rs", "layout_width_pixels = 58;", "layout_width_pixels = 60;")
        _ = self.commit("types-2")
        _ = self.git("merge", "--no-ff", "-q", "--no-edit", "other")
        types_2 = self.git("rev-parse", "HEAD")
        _ = self.git("checkout", "-q", "-b", "merge-only", types_1)
        _ = self.git("merge", "--no-ff", "-q", "--no-edit", "other")
        merge_only = self.git("rev-parse", "HEAD")
        self.ticket("types-1", "types", [IF_ELSE], start, types_1, at(1))
        self.ticket("types-2", "types", [INVALID_STATES], types_1, types_2, at(2))
        self.ticket("names-1", "names", [SPELL_OUT], types_1, merge_only, at(2))
        judged_again = self.judgment(TYPE_NAMED, "src/a.rs", "clean", "names-2", at(3))
        self.finding("merged-in", "prefer-type-named-fields-and-bindings", "src/a.rs", "withdrawn", at(1, 30), at(3),
                     withdrawn_by=judged_again)
        data = self.measured()
        self.assertEqual((data["pairs"], data["changing_tickets"]), ([], 2))

    def test_own_rework_counts_when_a_merge_brought_in_the_base(self) -> None:
        self.write("src/a.rs", ALPHA)
        start = self.commit("start")
        self.replace("src/a.rs", "selected_mode_number = 1;", "selected_mode_number = Mode::One as u32;")
        types_1 = self.commit("types-1")
        _ = self.git("checkout", "-q", "-b", "late", start)
        _ = self.git("merge", "--no-ff", "-q", "--no-edit", "main")
        self.replace("src/a.rs", "Mode::One as u32;", "Mode::First as u32;")
        names_1 = self.commit("names-1")
        self.ticket("types-1", "types", [IF_ELSE], start, types_1, at(1))
        self.ticket("names-1", "names", [SPELL_OUT], types_1, names_1, at(2))
        rows = [(row["changed_by"], row["redone"], row["reworked_hunks"]) for row in self.measured()["pairs"]]
        self.assertEqual(rows, [(SPELL_OUT, IF_ELSE, 1)])

    def test_own_rework_counts_when_a_merge_brought_in_the_base_under_an_own_edit(self) -> None:
        self.write("src/a.rs", ALPHA)
        self.write("src/c.rs", GAMMA)
        start = self.commit("start")
        self.replace("src/a.rs", "selected_mode_number = 1;", "selected_mode_number = Mode::One as u32;")
        types_1 = self.commit("types-1")
        _ = self.git("checkout", "-q", "-b", "late", start)
        self.replace("src/c.rs", "layout_width_pixels = 58;", "layout_width_pixels = 60;")
        _ = self.commit("names-1 first")
        _ = self.git("merge", "--no-ff", "-q", "--no-edit", "main")
        self.replace("src/a.rs", "Mode::One as u32;", "Mode::First as u32;")
        names_1 = self.commit("names-1")
        self.ticket("types-1", "types", [IF_ELSE], start, types_1, at(1))
        self.ticket("names-1", "names", [SPELL_OUT], types_1, names_1, at(2))
        rows = [(row["changed_by"], row["redone"], row["reworked_hunks"]) for row in self.measured()["pairs"]]
        self.assertEqual(rows, [(SPELL_OUT, IF_ELSE, 1)])

    def test_merged_in_change_to_a_file_the_ticket_put_back_is_not_its_change(self) -> None:
        self.write("src/a.rs", ALPHA)
        self.write("src/c.rs", GAMMA)
        start = self.commit("start")
        self.replace("src/a.rs", "selected_mode_number = 1;", "selected_mode_number = Mode::One as u32;")
        types_1 = self.commit("types-1")
        _ = self.git("checkout", "-q", "-b", "other")
        self.replace("src/c.rs", "layout_width_pixels = 58;", "layout_width_pixels = 60;")
        _ = self.commit("other")
        _ = self.git("checkout", "-q", "main")
        self.replace("src/c.rs", "layout_width_pixels = 58;", "layout_width_pixels = 59;")
        _ = self.commit("names-1 edit")
        self.replace("src/c.rs", "layout_width_pixels = 59;", "layout_width_pixels = 58;")
        self.replace("src/a.rs", "registration_counter_start = 10;", "registration_counter_start = 20;")
        _ = self.commit("names-1 restore")
        _ = self.git("merge", "--no-ff", "-q", "--no-edit", "other")
        names_1 = self.git("rev-parse", "HEAD")
        self.ticket("types-1", "types", [IF_ELSE], start, types_1, at(1))
        self.ticket("names-1", "names", [SPELL_OUT], types_1, names_1, at(2))
        judged_again = self.judgment(TYPE_NAMED, "src/c.rs", "clean", "names-2", at(3))
        self.finding("put-back", "prefer-type-named-fields-and-bindings", "src/c.rs", "withdrawn", at(1, 30), at(3),
                     withdrawn_by=judged_again)
        data = self.measured()
        self.assertEqual((data["pairs"], data["changing_tickets"]), ([], 2))

    def test_reorder_inside_one_commit_changes_its_file(self) -> None:
        self.write("src/a.rs", ALPHA)
        start = self.commit("start")
        alpha, beta = ALPHA.split("\n\n")
        self.write("src/a.rs", f"{beta}\n{alpha}\n")
        modules_1 = self.commit("modules-1")
        self.ticket("modules-1", "modules", ["global:rust/module-roots-as-table-of-contents.md"], start, modules_1, at(2))
        judged_again = self.judgment(TYPE_NAMED, "src/a.rs", "clean", "names-2", at(3))
        self.finding("reordered", "prefer-type-named-fields-and-bindings", "src/a.rs", "withdrawn", at(1), at(3),
                     withdrawn_by=judged_again)
        rows = [(row["changed_by"], row["stale_findings"]) for row in self.measured()["pairs"]]
        self.assertEqual(rows, [("global:rust/module-roots-as-table-of-contents.md", 1)])

    def test_same_named_global_and_local_rules_stay_apart(self) -> None:
        local = "local:docs/style/spell-out-names.md"
        self.write("src/a.rs", ALPHA)
        start = self.commit("start")
        self.replace("src/a.rs", "selected_mode_number = 1;", "selected_number = 1;")
        local_1 = self.commit("local-1")
        self.replace("src/a.rs", "selected_number = 1;", "selected_mode_number = 1;")
        names_1 = self.commit("names-1")
        self.ticket("local-1", "names", [local], start, local_1, at(1))
        self.ticket("names-1", "names", [SPELL_OUT + "#uncovered"], local_1, names_1, at(2))
        rows = [(row["changed_by"], row["redone"], row["reworked_hunks"], row["analysis"]) for row in self.measured()["pairs"]]
        self.assertEqual(rows, [(SPELL_OUT, local, 1, "unordered")])

    def test_renamed_file_counts_a_followed_finding_once(self) -> None:
        self.write("src/a.rs", ALPHA)
        start = self.commit("start")
        _ = self.git("mv", "src/a.rs", "src/alpha.rs")
        modules_1 = self.commit("modules-1")
        self.ticket("modules-1", "modules", ["global:rust/split-by-type-ownership.md"], start, modules_1, at(2))
        judged_again = self.judgment(TYPE_NAMED, "src/alpha.rs", "clean", "names-2", at(3))
        self.finding("followed", "prefer-type-named-fields-and-bindings", "src/alpha.rs", "withdrawn", at(1), at(3),
                     withdrawn_by=judged_again, followed_from="src/a.rs")
        rows = [(row["stale_findings"], row["files"]) for row in self.measured()["pairs"]]
        self.assertEqual(rows, [(1, ["src/a.rs"])])

    def test_markdown_only_ticket_changes_no_code(self) -> None:
        self.write("src/a.rs", ALPHA)
        self.write("notes.md", "first note\n")
        start = self.commit("start")
        self.write("notes.md", "first note changed\n")
        docs_1 = self.commit("docs-1")
        self.replace("src/a.rs", "selected_mode_number = 1;", "selected_mode_number = Mode::One as u32;")
        types_1 = self.commit("types-1")
        self.ticket("docs-1", "names", [SPELL_OUT], start, docs_1, at(1))
        self.ticket("types-1", "types", [IF_ELSE], docs_1, types_1, at(2))
        self.assertEqual(self.measured()["changing_tickets"], 1)

    def test_analysis_order_reads_edges_then_positions(self) -> None:
        def order(first: str, then: str) -> AnalysisOrder:
            return order_waste.analysis_order(RuleFix(f"global:rust/{first}.md"), RuleFix(f"global:rust/{then}.md"))

        self.assertEqual(order("enums-over-option-when-none-has-meaning", "prefer-functional-patterns"), AnalysisOrder.A_FIRST_BY_EDGE)
        self.assertEqual(order("prefer-functional-patterns", "enums-over-option-when-none-has-meaning"), AnalysisOrder.B_FIRST_BY_EDGE)
        self.assertEqual(order("forbidden-words", "dont-create-traits-for-single-implementations"), AnalysisOrder.B_FIRST_BY_POSITION)
        self.assertEqual(order("always-use-nextest", "no-magic-values"), AnalysisOrder.UNORDERED)
        self.assertEqual(order_waste.analysis_order(FamilyRead("values"), RuleFix(SPELL_OUT)), AnalysisOrder.UNORDERED)
        self.assertEqual(order_waste.analysis_order(RuleFix(IF_ELSE), RuleFix("local:docs/style/no-magic-values.md")),
                         AnalysisOrder.UNORDERED)
        self.assertEqual(order_waste.canonical_rule(NO_MAGIC + "#uncovered"), NO_MAGIC)
        self.assertEqual(order_waste.rule_slug(NO_MAGIC + "#uncovered"), "no-magic-values")
        self.assertIsNone(order_waste.rule_slug("local:docs/style/no-magic-values.md"))

    def test_command_reads_a_ledger_file_and_prints_the_table(self) -> None:
        self.build_history()
        self.build_findings()
        self.connection.commit()
        ledger = self.root / "ledger.sqlite"
        with closing(sqlite3.connect(ledger)) as copy:
            self.connection.backup(copy)
        command = [sys.executable, str(Path(__file__).with_name("order_waste.py")), "--ledger", str(ledger), "--repo", str(self.repo)]
        table = subprocess.run(command, check=True, capture_output=True, text=True).stdout
        self.assertIn(f"| {INVALID_STATES} | {IF_ELSE} | 0 | 1 | 1 | 1 | A first, position |", table)
        printed = cast(order_waste.Report, json.loads(subprocess.run([*command, "--json"], check=True, capture_output=True,
                                                                     text=True).stdout))
        self.assertEqual(len(printed["pairs"]), 7)
        missing = subprocess.run([*command[:3], str(self.root / "none.sqlite"), *command[4:]], check=False, capture_output=True, text=True)
        self.assertEqual((missing.returncode, missing.stdout), (2, ""))


if __name__ == "__main__":
    _ = unittest.main()
