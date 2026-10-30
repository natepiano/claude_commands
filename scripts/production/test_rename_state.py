"""State moves performed for a renamed showrunner unit."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import cast, override

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rename_state
import tidy_wrapped


class RenameStateTests(unittest.TestCase):
    root: Path = Path()
    scratch: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()

    def write_json(self, relative: str, value: object) -> Path:
        path = self.scratch / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        return path

    def test_each_scratch_store_moves_and_second_call_changes_no_bytes(self) -> None:
        eta = self.write_json("dailies_input_state/eta_seen.json", {
            "old|move": {"requested": True},
            "old|same": {"source": "old"},
            "new|same": {"source": "new"},
            "other|phase": {"requested": True},
        })
        decisions = self.scratch / "unit_status" / "decisions_seen"
        decisions.parent.mkdir()
        _ = decisions.write_text("old|move\nold|same\nnew|same\nother|stay\n")
        blocks = self.scratch / "unit_status" / "blocks_open"
        _ = blocks.write_text("old\tmove\nold\tsame\nnew\tsame\nother\tstay\n")
        showrunner = self.write_json("showrunner_state.json", {
            "units": [{"unit": "old", "phase": "build"}, {"unit": "other", "phase": "test"}],
            "merges_held": [],
        })
        judgment = self.write_json("dailies_judgment.json", {
            "units": [{"unit": "old", "activity": "discard"}, {"unit": "new", "activity": "keep"}],
        })
        rendered = self.write_json("dailies_state.json", {
            "old": {"phase": "build"}, "other": {"phase": "test"},
        })
        paths = [eta, decisions, blocks, showrunner, judgment, rendered]

        changed = rename_state.rename_scratch("old", "new", self.scratch)

        self.assertEqual(changed, [
            "dailies_input_state/eta_seen.json",
            "unit_status/decisions_seen",
            "unit_status/blocks_open",
            "showrunner_state.json",
            "dailies_judgment.json",
            "dailies_state.json",
        ])
        eta_fields = cast(dict[str, object], json.loads(eta.read_text()))
        self.assertNotIn("old|move", eta_fields)
        self.assertEqual(eta_fields["new|move"], {"requested": True})
        self.assertEqual(eta_fields["new|same"], {"source": "new"})
        self.assertEqual(decisions.read_text(), "new|move\nnew|same\nother|stay\n")
        self.assertEqual(blocks.read_text(), "new\tsame\nother\tstay\n")
        showrunner_fields = cast(dict[str, object], json.loads(showrunner.read_text()))
        showrunner_units = cast(list[dict[str, object]], showrunner_fields["units"])
        self.assertEqual([row["unit"] for row in showrunner_units], ["new", "other"])
        judgment_fields = cast(dict[str, object], json.loads(judgment.read_text()))
        judgment_units = cast(list[dict[str, object]], judgment_fields["units"])
        self.assertEqual(judgment_units, [{"unit": "new", "activity": "keep"}])
        rendered_fields = cast(dict[str, object], json.loads(rendered.read_text()))
        self.assertEqual(set(rendered_fields), {"new", "other"})

        after_first = {path: path.read_bytes() for path in paths}
        self.assertEqual(rename_state.rename_scratch("old", "new", self.scratch), [])
        self.assertEqual({path: path.read_bytes() for path in paths}, after_first)

    def test_missing_file_and_missing_directory_are_skipped(self) -> None:
        self.assertEqual(rename_state.rename_scratch("old", "new", self.scratch), [])
        self.assertEqual(rename_state.rename_scratch("old", "new", self.root / "absent"), [])

    def test_blocks_open_keeps_existing_new_row_when_old_row_differs(self) -> None:
        blocks = self.scratch / "unit_status" / "blocks_open"
        blocks.parent.mkdir()
        _ = blocks.write_text("old\told block\t10:00\nnew\tkept block\t10:05\nother\tother block\t10:10\n")

        self.assertIn("unit_status/blocks_open", rename_state.rename_scratch("old", "new", self.scratch))

        self.assertEqual(blocks.read_text(), "new\tkept block\t10:05\nother\tother block\t10:10\n")
        after_first = blocks.read_bytes()
        self.assertEqual(rename_state.rename_scratch("old", "new", self.scratch), [])
        self.assertEqual(blocks.read_bytes(), after_first)

    def test_the_live_lists_take_the_new_names_as_whole_words(self) -> None:
        words = (("demo-old", "demo-new"), ("/rust/claude-demo-old", "/rust/claude-demo-new"))
        path = self.write_json("showrunner_state.json", {
            "units": [{"unit": "old-unit", "phase": "2"}],
            "next": ["merge old-unit once demo-old is green", "keep the per-unit gate", "old-unitary stays"],
            "open_for_user": ["answer old-unit", {"text": "old-unit asks", "question": "Does old-unit ship?"}]})
        self.assertEqual(rename_state.rename_scratch("old-unit", "new-unit", self.scratch, words),
                         ["showrunner_state.json"])
        state = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(state["units"], [{"unit": "new-unit", "phase": "2"}])
        self.assertEqual(state["next"], ["merge new-unit once demo-new is green", "keep the per-unit gate",
                                         "old-unitary stays"])
        self.assertEqual(state["open_for_user"], ["answer new-unit", {"text": "new-unit asks",
                                                                      "question": "Does new-unit ship?"}])
        self.assertEqual(rename_state.rename_scratch("old-unit", "new-unit", self.scratch, words), [])

    def test_outstanding_takes_the_same_words_as_open_for_user(self) -> None:
        path = self.root / "outstanding" / "demo.json"
        path.parent.mkdir()
        _ = path.write_text(json.dumps([{"since": "2026-10-21T09:00", "text": "old-unit asks",
                                         "question": "Does old-unit ship?", "reviewed": "Ship old-unit?"}]))
        with unittest.mock.patch.object(tidy_wrapped, "STATE_DIR", self.root):
            self.assertTrue(rename_state.rename_outstanding("demo", (("old-unit", "new-unit"),)))
            self.assertFalse(rename_state.rename_outstanding("demo", (("old-unit", "new-unit"),)))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [{
            "since": "2026-10-21T09:00", "text": "new-unit asks", "question": "Does new-unit ship?",
            "reviewed": "Ship new-unit?"}])

    def test_unparsable_json_is_refused_by_file_name(self) -> None:
        path = self.scratch / "dailies_input_state" / "eta_seen.json"
        path.parent.mkdir(parents=True)
        _ = path.write_text("{not json\n")
        with self.assertRaisesRegex(rename_state.RenameRefused, "eta_seen.json"):
            _ = rename_state.rename_scratch("old", "new", self.scratch)

    def test_outstanding_rename_keeps_answered_jsonl_records_unchanged(self) -> None:
        path = self.root / "outstanding/demo.json"
        path.parent.mkdir()
        _ = path.write_text(json.dumps([{"text": "old-unit asks", "question": "Ship old-unit?"}]))
        answered = path.with_suffix(".answered.jsonl")
        _ = answered.write_text("".join(json.dumps({"text": text, "asked_by": "old-unit"}) + "\n"
                                        for text in ("first answer", "second answer")))
        before = answered.read_bytes()
        with unittest.mock.patch.object(tidy_wrapped, "STATE_DIR", self.root):
            self.assertTrue(rename_state.rename_outstanding("demo", (("old-unit", "new-unit"),)))
            self.assertFalse(rename_state.rename_outstanding("demo", (("old-unit", "new-unit"),)))
        self.assertEqual(json.loads(path.read_text()), [{"text": "new-unit asks", "question": "Ship new-unit?"}])
        self.assertEqual(answered.read_bytes(), before)


if __name__ == "__main__":
    _ = unittest.main()
