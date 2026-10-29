"""Blind preference sheets conceal source assignments and score human picks."""
from __future__ import annotations

from collections import Counter
import contextlib
import io
import json
from pathlib import Path
from typing import cast, override
import unittest
from unittest import mock

from scripts.issue_score import blind as preference
from scripts.issue_score import evaluate, questions, records, score_issues
from scripts.issue_score import test_evaluate as support


class BlindTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture: support.CompareTests = support.CompareTests()

    @override
    def setUp(self) -> None:
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def disagreements(self, count: int = 28) -> str:
        paths: list[Path] = []
        for number in range(200):
            title = f"Blind disagreement {number:03}"
            if evaluate.split_for(title + ".md") != "holdout":
                continue
            path = self.fixture.issue(title, impact=1, body=f"Context for {title}.")
            _ = path.write_text(path.read_text().replace("backlog_alignment: ⭐⭐⭐⭐", "backlog_alignment: ⭐")
                                .replace('backlog_goal: "1 - Ship Hana"', 'backlog_goal: "2 - Find Collaborators"')
                                .replace("backlog_urgency: ⭐\n", "backlog_urgency: ⭐⭐\n")
                                .replace("backlog_effort: ⭐⭐", "backlog_effort: ⭐")
                                .replace("category: feature", "category: bug")
                                .replace('area: "[[rendering]]"', 'area: tooling'))
            paths.append(path)
            if len(paths) == count:
                break
        self.assertEqual(len(paths), count)
        entries = [self.fixture.record(path) for path in paths]
        tune = self.fixture.issue("Target A", impact=1)
        example_stem = next(stem for stem in sorted(questions.HISTORICAL_EFFORT_EXAMPLE_STEMS)
                            if evaluate.split_for(stem + ".md") == "holdout")
        example = self.fixture.issue(example_stem, impact=1)
        _ = self.fixture.run_file("blind-fixture", [*entries, self.fixture.record(tune), self.fixture.record(example)])
        return "blind-fixture"

    def blind(self, run_id: str, count: int = 20, *, seed: int = 17) -> records.BlindFiles:
        return preference.blind(run_id, count=count, seed=seed,
                                  issues_dir=self.fixture.issues, state_dir=self.fixture.state)

    def read(self, files: records.BlindFiles) -> tuple[records.BlindSheet, records.BlindKey]:
        return (cast(records.BlindSheet, json.loads(files.sheet_path.read_text())),
                cast(records.BlindKey, json.loads(files.key_path.read_text())))

    def test_seed_reproduces_assignments_without_disclosing_the_key_in_the_sheet(self) -> None:
        run = self.disagreements()
        files = self.blind(run)
        sheet, key = self.read(files)
        original_sheet, original_key = files.sheet_path.read_bytes(), files.key_path.read_bytes()
        repeated = self.blind(run)
        self.assertEqual(repeated.sheet_path.read_bytes(), original_sheet)
        self.assertEqual(repeated.key_path.read_bytes(), original_key)
        self.assertEqual(key["seed"], 17)
        self.assertNotEqual(files.sheet_path.parent, files.key_path.parent)
        self.assertEqual(set(sheet), {"run_id", "instructions", "items"})
        self.assertNotIn(str(files.key_path), files.sheet_path.read_text())
        for item in sheet["items"]:
            self.assertEqual(set(item), {"id", "field", "title", "body", "A", "B", "pick"})
            self.assertEqual(item["pick"], "")
            self.assertNotEqual(item["A"], item["B"])
        assignments = {entry["id"]: entry for entry in key["entries"]}
        self.assertEqual(set(assignments), {item["id"] for item in sheet["items"]})
        self.assertEqual({entry["jev_option"] for entry in key["entries"]}, {"A", "B"})
        _, alternate_key = self.read(self.blind(run, seed=91))
        self.assertNotEqual([entry["jev_option"] for entry in key["entries"]],
                            [entry["jev_option"] for entry in alternate_key["entries"]])

    def test_disagreements_span_fields_and_use_distinct_heldout_nonexample_issues(self) -> None:
        sheet, key = self.read(self.blind(self.disagreements()))
        self.assertEqual(len(sheet["items"]), 20)
        counts = Counter(item["field"] for item in sheet["items"])
        self.assertEqual(set(counts), set(questions.THRESHOLDS))
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)
        self.assertEqual(len({entry["issue_path"] for entry in key["entries"]}), 20)
        for entry in key["entries"]:
            path = Path(entry["issue_path"])
            self.assertEqual(evaluate.split_for(path), "holdout")
            self.assertNotIn(path.stem, questions.HISTORICAL_EFFORT_EXAMPLE_STEMS)

    def test_sparse_overlapping_disagreements_reserve_a_row_for_every_available_field(self) -> None:
        paths: list[Path] = []
        for number in range(200):
            title = f"Overlapping blind issue {number:03}"
            if evaluate.split_for(title + ".md") == "holdout":
                paths.append(self.fixture.issue(title))
                if len(paths) == 20:
                    break
        entries = [self.fixture.record(path) for path in paths]
        for index, path in enumerate(paths):
            text = path.read_text().replace("backlog_alignment: ⭐⭐⭐⭐", "backlog_alignment: ⭐")
            if index < 6:
                text = (text.replace('backlog_goal: "1 - Ship Hana"', 'backlog_goal: "2 - Find Collaborators"')
                        .replace("backlog_impact: ⭐⭐⭐", "backlog_impact: ⭐")
                        .replace("backlog_urgency: ⭐\n", "backlog_urgency: ⭐⭐\n")
                        .replace("backlog_effort: ⭐⭐", "backlog_effort: ⭐")
                        .replace('area: "[[rendering]]"', 'area: tooling'))
            if index == 0:
                text = text.replace("category: feature", "category: bug")
            _ = path.write_text(text)
        _ = self.fixture.run_file("overlapping-fields", entries)
        # Six fields compete for the first six issues; category has only the
        # first issue, while alignment can use any of the other fourteen.
        for seed in range(20):
            for count in (7, 20):
                with self.subTest(seed=seed, count=count):
                    sheet, key = self.read(self.blind("overlapping-fields", count=count, seed=seed))
                    self.assertEqual(len(sheet["items"]), count)
                    self.assertEqual({item["field"] for item in sheet["items"]}, set(questions.THRESHOLDS))
                    self.assertEqual(len({entry["issue_path"] for entry in key["entries"]}), count)
                    category = [entry for entry in key["entries"] if entry["field"] == "category"]
                    self.assertEqual([entry["issue_path"] for entry in category], [str(paths[0])])

    def test_blind_score_tallies_jev_vault_same_and_unanswered_per_field(self) -> None:
        files = self.blind(self.disagreements(), count=28)
        sheet, key = self.read(files)
        assignments = {entry["id"]: entry["jev_option"] for entry in key["entries"]}
        seen: Counter[str] = Counter()
        for item in sheet["items"]:
            option = assignments[item["id"]]
            index = seen[item["field"]]
            item["pick"] = (option, "B" if option == "A" else "A", "same", "")[index]
            seen[item["field"]] += 1
        _ = files.sheet_path.write_text(json.dumps(sheet))
        result = preference.blind_score(files.sheet_path)
        self.assertEqual(result.unanswered, 7)
        self.assertEqual(set(result.counts), set(questions.THRESHOLDS))
        for field, tally in result.counts.items():
            with self.subTest(field=field):
                self.assertEqual((tally.jev, tally.vault, tally.same), (1, 1, 1))
        rows = support.table(result.result_path.read_text(), "Field")
        self.assertEqual(len(rows), 7)
        for row in rows:
            for name in ("Jev share", "Vault share", "Same share"):
                self.assertAlmostEqual(float(row[name]), 1 / 3, delta=.00006)

    def test_invalid_pick_is_rejected_instead_of_silently_changing_the_denominator(self) -> None:
        files = self.blind(self.disagreements(count=7), count=7)
        sheet, _ = self.read(files)
        sheet["items"][0]["pick"] = "neither"
        _ = files.sheet_path.write_text(json.dumps(sheet))
        with self.assertRaises(ValueError):
            _ = preference.blind_score(files.sheet_path)

    def test_blind_cli_preserves_seed_count_and_scores_the_completed_sheet(self) -> None:
        run = self.disagreements(count=7)
        expected = self.blind(run, count=7, seed=123)
        expected_sheet = expected.sheet_path.read_bytes()
        original_blind = preference.blind

        def local_blind(run_id: str, count: int = 20, *, seed: int = 0) -> records.BlindFiles:
            return original_blind(run_id, count=count, seed=seed,
                                  issues_dir=self.fixture.issues, state_dir=self.fixture.state)

        output = io.StringIO()
        with mock.patch.object(score_issues, "blind", side_effect=local_blind), contextlib.redirect_stdout(output):
            code = score_issues.main(["blind", run, "--count", "7", "--seed", "123"])
        self.assertEqual(code, 0)
        path = Path(output.getvalue().strip())
        self.assertEqual(path.read_bytes(), expected_sheet)
        sheet = cast(records.BlindSheet, json.loads(path.read_text()))
        self.assertEqual(len(sheet["items"]), 7)
        for item in sheet["items"]:
            item["pick"] = "same"
        _ = path.write_text(json.dumps(sheet))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = score_issues.main(["blind-score", str(path)])
        self.assertEqual(code, 0)
        rows = support.table(Path(output.getvalue().strip()).read_text(), "Field")
        self.assertTrue(all(float(row["Same share"]) == 1. for row in rows))


if __name__ == "__main__":
    _ = unittest.main()
