"""Rename verification distinguishes notes from names and preserves saved tab positions."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import cast, final
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts.production.rename_unit_test_support import UnitRenameFixture
import herdr


@final
class UnitRenameVerifyTests(UnitRenameFixture):
    def test_verify_keeps_showrunner_notes_but_checks_names_and_unknown_fields(self) -> None:
        self.assert_ok(self.rename("apply"))
        path = self.state / "demo/session/showrunner_state.json"
        notes: dict[str, object] = {
            "units": [{"unit": "gamma-unit", "phase": "alpha-unit completed", "wait": "demo-alpha was green"}],
            "merges_held": ["gamma-unit"],
            "next": ["review alpha-unit in demo-alpha"],
            "open_for_user": ["alpha-unit asked", {"text": "alpha-unit asks", "question": "Ship demo-alpha?"}],
            "report_to_user": [{"text": "alpha-unit finished", "until": "2026-11-01T00:00"}],
        }
        original = json.dumps(notes)
        self.write(path, original)
        self.assert_ok(self.rename("verify"))
        self.assertEqual(path.read_text(), original, "verify must leave the notes intact")
        for key, value in (
            ("units", [{"unit": "alpha-unit"}]),
            ("merges_held", ["alpha-unit"]),
            ("future_names", ["alpha-unit"]),
            ("units", [{"unit": "gamma-unit", "future_name": "alpha-unit"}]),
            ("open_for_user", [{"text": "history", "future_name": "alpha-unit"}]),
            ("next", [{"future_name": "alpha-unit"}]),
        ):
            with self.subTest(field=key, value=value):
                self.write(path, json.dumps({**notes, key: value}))
                self.assert_refused(self.rename("verify"), f"unclassified: {path} still holds an old name")

    def test_move_state_rewrites_held_merge_names(self) -> None:
        path = self.root / "scratch/showrunner_state.json"
        self.write(path, json.dumps({"units": [{"unit": "alpha-unit"}],
                                    "merges_held": ["alpha-unit", "demo-alpha", "mul_add-unit"]}))
        self.assert_ok(self.rename("apply"))
        state = cast(dict[str, object], json.loads(path.read_text()))
        self.assertEqual(state["merges_held"], ["gamma-unit", "gamma", "mul_add-unit"])
        self.assert_ok(self.rename("verify"))

    def test_relaunch_renames_the_saved_tab_in_place_and_verify_checks_it(self) -> None:
        restarts = self.seed_record(name="demo-alpha")
        with patch.dict(os.environ, self.env):
            path = herdr.layout_path(herdr.shared_session())
        layout = herdr.SavedLayout(at=123.0, workspaces=[
            herdr.SavedSpace(name="demo", seen=120.0, tabs=[
                herdr.SavedName(name="demo", seen=100.0),
                herdr.SavedName(name="demo-alpha", seen=110.0),
                herdr.SavedName(name="mul_add", seen=115.0),
                herdr.SavedName(name="gamma", seen=119.0),
                herdr.SavedName(name="alpha-unit", seen=111.0),
            ]),
            herdr.SavedSpace(name="another-production", seen=121.0,
                             tabs=[herdr.SavedName(name="alpha", seen=118.0)]),
        ])
        self.write(path, json.dumps(layout))
        self.assert_refused(self.rename("apply"), "gamma-unit is not running")
        with patch.dict(os.environ, self.env):
            saved = herdr.saved_layout(herdr.shared_session())
        expected = herdr.SavedLayout(at=123.0, workspaces=[
            herdr.SavedSpace(name="demo", seen=120.0, tabs=[
                herdr.SavedName(name="demo", seen=100.0),
                herdr.SavedName(name="gamma", seen=119.0),
                herdr.SavedName(name="mul_add", seen=115.0),
            ]), layout["workspaces"][1],
        ])
        self.assertEqual(saved, expected)
        _ = self.mark_live("gamma-unit", "sid-alpha")
        self.assert_ok(self.rename("apply"))
        self.assert_ok(self.rename("verify"))
        self.assertEqual(json.loads(path.read_text()), expected)
        self.write(path, json.dumps(layout))
        self.assert_refused(self.rename("verify"), f"unclassified: {path} still holds an old name")
        expected["workspaces"][0]["tabs"][1]["name"] = "alpha"
        self.write(path, json.dumps(expected))
        self.assert_refused(self.rename("verify"), f"herdr {path}: demo tab is still labelled alpha")
        # The live incident left an older journal at step 10, after relaunch.
        journal = self.journal()
        journal["step"] = 10
        self.write(self.state / "demo/rename_unit.json", json.dumps(journal))
        launches = restarts.read_bytes()
        self.assert_ok(self.rename("apply"))
        self.assertEqual(restarts.read_bytes(), launches, "layout recovery must not restart the live unit")
        self.assertIs(self.journal()["done"], True)
        expected["workspaces"][0]["tabs"][1]["name"] = "gamma"
        self.assertEqual(json.loads(path.read_text()), expected)
        self.assert_ok(self.rename("verify"))
