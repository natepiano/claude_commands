"""Keep saved guild evidence and aliases while checking every live rename route."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.production import unit_rename_sweep as sweep  # noqa: E402


class GuildRenameSweepTests(unittest.TestCase):
    root: Path = Path()
    state: Path = Path()
    design: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.state = self.root / "state"
        self.design = self.state / "guilds/roles/design-one"
        self.design.mkdir(parents=True)
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"GUILD_ROLES_STATE": str(self.design.parent)})))

    def write(self, relative: str, value: object) -> Path:
        path = self.design / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def failures(self, live_files: tuple[Path, ...] = ()) -> list[str]:
        return sweep.sweep(("demo-old-unit", "demo-old", "/fixture/old-checkout"),
                           sweep.Roots(self.state, self.state / "showrunner/demo", (), (), live_files))

    def test_saved_prompts_and_replies_keep_old_holder_and_checkout_as_history(self) -> None:
        _ = self.write("prompts/first.md", {"member": "demo-old", "document": "/fixture/old-checkout/design.md"})
        _ = self.write("replies/first.md", "Evidence from demo-old-unit")
        self.assertEqual(self.failures(), [])

    def test_pane_snapshots_deletion_history_and_idle_registrations_keep_their_owners_names(self) -> None:
        for relative, value in (
            ("herdr-panes/agents.json", {"w1:p1": {"cwd": "/fixture/old-checkout",
                "name": "demo-old", "environment": {"SHOWRUNNER_UNIT_ID": "demo-old-unit"}}}),
            ("lint-sweep/target-deletes.json", {"/fixture/old-checkout/target": 1793300000.0}),
            ("mac-idle/calls/6b748ec1.json", {"call_id": "demo-old:check", "pid": 42, "start": "10",
                "repo": "/fixture/old-checkout", "package": "demo-old", "session": "sid"}),
        ):
            with self.subTest(path=relative):
                path = self.state / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                original = json.dumps(value)
                _ = path.write_text(original, encoding="utf-8")
                self.assertEqual(self.failures(), [])
                self.assertEqual(path.read_text(), original)
                # An explicitly live file still takes precedence over the snapshot classification.
                self.assertTrue(any(str(path) in failure for failure in self.failures((path,))))
                path.unlink()
        unknown = self.state / "mac-idle/unclassified.json"
        _ = unknown.write_text(json.dumps({"repo": "/fixture/old-checkout"}))
        self.assertTrue(any(str(unknown) in failure for failure in self.failures()))

    def test_mesh_aliases_keep_identity_without_exempting_live_fields(self) -> None:
        record = self.write("design.json", {"aliases": ["demo-old"], "holder": {
            "kind": "UnitHolder", "production_slug": "demo", "unit_name": "new-unit"},
            "document": "/fixture/new-checkout/design.md"})
        self.assertEqual(self.failures(), [])
        _ = self.write("design.json", {"aliases": ["demo-old"], "holder": {
            "kind": "UnitHolder", "production_slug": "demo", "unit_name": "demo-old-unit"},
            "document": "/fixture/new-checkout/design.md"})
        self.assertTrue(any(str(record) in failure for failure in self.failures()))

    def test_live_binding_and_launch_cwd_are_checked_even_beside_saved_history(self) -> None:
        _ = self.write("prompts/first.md", "demo-old-unit")
        for relative, value in (
            ("design.json", {"binding": {"repository": "/fixture/old-checkout",
                                         "run_directory": "/fixture/run"}}),
            ("mesh/mesh_roster.json", {"demo-new": {"cwd": "/fixture/old-checkout"}}),
            ("mesh/mesh_server.json", {"cwd": "/fixture/old-checkout"}),
        ):
            with self.subTest(path=relative):
                path = self.write(relative, value)
                self.assertTrue(any(str(path) in failure for failure in self.failures()))
                path.unlink()


if __name__ == "__main__":
    _ = unittest.main()
