"""State moves performed for a renamed showrunner unit."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import cast, override

import rename_state


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

    def test_unparsable_json_is_refused_by_file_name(self) -> None:
        path = self.scratch / "dailies_input_state" / "eta_seen.json"
        path.parent.mkdir(parents=True)
        _ = path.write_text("{not json\n")
        with self.assertRaisesRegex(rename_state.RenameRefused, "eta_seen.json"):
            _ = rename_state.rename_scratch("old", "new", self.scratch)


if __name__ == "__main__":
    _ = unittest.main()
