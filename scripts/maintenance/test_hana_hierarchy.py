"""The migrated hierarchy renderer preserves its note and refusal behavior."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast, override, final
from unittest import mock
from scripts.maintenance import hana_hierarchy



def entry(role: str, name: str, **fields: str) -> dict[str, str]:
    base = {"machine": "natedev", "kind": "claude" if role != "worker" else "codex", "role": role, "name": name,
            "status": "idle", "cwd": "", "production": "", "showrunner": "", "unit": "", "director": "",
            "slot_role": ""}
    return {**base, **fields}


HOME = os.path.expanduser("~")

ROSTER = {
    "entries": [
        entry("showrunner", "hana", cwd=f"{HOME}/rust/hana_catalyst", production="tool-based-ui"),
        entry("unit director", "trunk", cwd=f"{HOME}/rust/tool-based-ui-trunk", showrunner="hana", unit="trunk-unit"),
        entry("unit director", "merge-gate", cwd=f"{HOME}/rust/hana_catalyst-merge-gate", showrunner="hana",
              unit="merge-gate-unit"),
        # The same director read twice: kept once.
        entry("unit director", "trunk", cwd=f"{HOME}/rust/tool-based-ui-trunk", showrunner="hana", unit="trunk-unit"),
        entry("worker", "tool-based-ui-trunk-test", showrunner="hana", director="trunk", slot_role="test",
              status="running"),
        entry("worker", "tool-based-ui-trunk-impl", showrunner="hana", director="trunk", slot_role="fix"),
        entry("worker", "tool-based-ui-trunk-odd", showrunner="hana", director="trunk", slot_role="scout",
              status="failed"),
        # From a director's earlier run: no director, so left out.
        entry("worker", "hana_catalyst-cables-impl", showrunner="hana", slot_role="impl"),
        # Another showrunner's tree: left out.
        entry("showrunner", "hanadocs", cwd=f"{HOME}/rust/hanadocs", production="hanadocs-info"),
        entry("unit director", "diagrams", showrunner="hanadocs", unit="diagrams-unit"),
        entry("worker", "hanadocs-diagrams-impl", showrunner="hanadocs", director="diagrams", slot_role="impl"),
    ],
    "problems": [],
}

EXPECTED = """---
date_created: "[[2026-10-01]]"
date_modified: "[[2026-10-14]]"
---

# Hana hierarchy

Snapshot taken 2026-10-14 14:05 EDT.

Showrunner: hana · Production file: [tool-based-ui-production.md](https://github.com/natepiano/hana/blob/init/catalyst/docs/hana/tool-based-ui-production.md)

The boxes run from the showrunner to its unit directors to the workers each one launched. Each box reads name — role. Showrunner and director boxes add their folder; worker boxes add their state.

```mermaid
flowchart TD
  hana["hana — showrunner<br/>~/rust/hana_catalyst"]

  merge_gate["merge-gate — unit director<br/>~/rust/hana_catalyst-merge-gate"]
  hana --> merge_gate

  trunk["trunk — unit director<br/>~/rust/tool-based-ui-trunk"]
  hana --> trunk

  tool_based_ui_trunk_impl["tool-based-ui-trunk-impl — fixer<br/>idle"]
  trunk --> tool_based_ui_trunk_impl

  tool_based_ui_trunk_odd["tool-based-ui-trunk-odd — scout<br/>failed"]
  trunk --> tool_based_ui_trunk_odd

  tool_based_ui_trunk_test["tool-based-ui-trunk-test — tester<br/>working"]
  trunk --> tool_based_ui_trunk_test

  classDef working fill:#d3f2d8,stroke:#2e7d32,color:#1b3d20
  classDef idle fill:#ececec,stroke:#8a8a8a,color:#3a3a3a
  classDef waiting fill:#fff1cc,stroke:#b8860b,color:#4a3b00
  class tool_based_ui_trunk_test working
  class tool_based_ui_trunk_impl idle
```

Workers: green is working, grey is idle.

| Unit director | Unit | Workers |
| --- | --- | --- |
| merge-gate | merge-gate-unit | 0 |
| trunk | trunk-unit | 3 |

- A worker keeps its opening name when its job changes. The role shown is its current job.
- Closed workers and workers from a director's earlier run are left out.
- The workers shown are Codex workers. A director's short-lived Claude helpers are not recorded.
- The hana showrunner has no showrunner under it today.

## Rebuilding it

The agent-notes job rewrites this note from `scripts/maintenance/hana_hierarchy.py`, and commits it when a box, state, link or count changes.
"""

NOW = datetime(2026, 10, 14, 14, 5, tzinfo=timezone(timedelta(hours=-4), "EDT"))


def sh(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout


@final
class Hierarchy(unittest.TestCase):
    """The generator against a fake roster.py and showrunners.py, and a real git checkout."""

    maxDiff: int | None = None
    module = hana_hierarchy
    root: str = ""
    note: str = ""
    roster: dict[str, object] = {}
    children: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        checkout = os.path.join(self.root, "hana_catalyst")
        os.makedirs(os.path.join(checkout, "docs", "hana"))
        _ = sh(checkout, "init", "-q", "-b", "init/catalyst")
        _ = sh(checkout, "remote", "add", "origin", "git@github.com:natepiano/hana")
        with open(os.path.join(checkout, "docs", "hana", "tool-based-ui-production.md"), "w", encoding="utf-8") as h:
            _ = h.write("# tool-based-ui\n")
        vault = os.path.join(self.root, "vault")
        os.makedirs(vault)
        self.note = os.path.join(vault, "hana hierarchy.md")
        with open(self.note, "w", encoding="utf-8") as h:
            _ = h.write('---\ndate_created: "[[2026-10-01]]"\n---\n\nhand-written\n')
        self.roster = cast(dict[str, object], json.loads(json.dumps(ROSTER)))
        self.children = {"tool-based-ui": ""}
        doc = os.path.join(checkout, "docs", "hana", "tool-based-ui-production.md")
        listing = f"tool-based-ui\thana\tAmerica/New_York\t{doc}\nhanadocs-info\thanadocs\tAmerica/New_York\t/x\n"

        def call(script: str, *args: str) -> str:
            if script.endswith("roster.py"):
                self.assertEqual(args, ("--here", "--json"))
                return json.dumps(self.roster)
            if args == ("list",):
                return listing
            return self.children[args[1]]

        _ = self.enterContext(mock.patch.object(self.module, "call", side_effect=call))

    def text(self) -> str:
        gather = self.module.gather
        render = self.module.render
        return render(gather(self.note, NOW))

    def run_main(self) -> tuple[int, str]:
        main = self.module.main
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main([self.note])
        return code, err.getvalue()

    def read_note(self) -> str:
        with open(self.note, encoding="utf-8") as handle:
            return handle.read()

    def test_fixture_roster_gives_the_expected_note(self) -> None:
        self.assertEqual(self.text(), EXPECTED)

    def worker(self, name: str) -> dict[str, str]:
        entries = cast(list[dict[str, str]], self.roster["entries"])
        return next(e for e in entries if e["name"] == name)

    def test_worker_state_follows_roster_status(self) -> None:
        for status, shown in (("running", "working"), ("idle", "idle"),
                              ("waiting for capacity", "waiting for capacity")):
            with self.subTest(status=status):
                self.worker("tool-based-ui-trunk-test")["status"] = status
                self.assertIn(f'["tool-based-ui-trunk-test — tester<br/>{shown}"]', self.text())

    def test_class_lines_list_sorted_ids_for_drawn_states_only(self) -> None:
        # Drawn before trunk's workers, sorted after them.
        entries = cast(list[dict[str, str]], self.roster["entries"])
        entries.append(entry("worker", "zeta-impl", showrunner="hana", director="merge-gate", slot_role="impl"))
        self.worker("tool-based-ui-trunk-test")["status"] = "idle"
        text = self.text()
        self.assertLess(text.index('  zeta_impl["'), text.index('  tool_based_ui_trunk_impl["'))
        classes = [line for line in text.splitlines() if line.startswith("  class ")]
        self.assertEqual(classes, ["  class tool_based_ui_trunk_impl,tool_based_ui_trunk_test,zeta_impl idle"])
        self.assertIn("  classDef working ", text)
        self.assertIn("\nWorkers: green is working, grey is idle.\n", text)

    def test_waiting_worker_is_amber_and_named_in_the_legend(self) -> None:
        self.worker("tool-based-ui-trunk-odd")["status"] = "waiting for capacity"
        text = self.text()
        self.assertIn("  class tool_based_ui_trunk_odd waiting\n", text)
        self.assertIn("\nWorkers: green is working, grey is idle, amber is waiting.\n", text)

    def test_main_writes_the_note(self) -> None:
        code, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn('date_created: "[[2026-10-01]]"', self.read_note())
        self.assertIn("trunk --> tool_based_ui_trunk_test", self.read_note())

    def test_problems_leave_the_note_untouched(self) -> None:
        self.roster["problems"] = ["units in /x: unreadable"]
        code, err = self.run_main()
        self.assertEqual((code, self.read_note()), (1, '---\ndate_created: "[[2026-10-01]]"\n---\n\nhand-written\n'))
        self.assertIn("left as it stands: roster.py reports problems: units in /x", err)

    def test_no_hana_showrunner_leaves_the_note_untouched(self) -> None:
        entries = cast(list[dict[str, str]], self.roster["entries"])
        self.roster["entries"] = [e for e in entries if e["name"] != "hana"]
        code, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("hand-written", self.read_note())
        self.assertIn("no hana showrunner", err)

    def test_no_unit_director_leaves_the_note_untouched(self) -> None:
        # What roster.py answers, problems empty, when tmux is not on PATH.
        entries = cast(list[dict[str, str]], self.roster["entries"])
        self.roster["entries"] = [e for e in entries if e["showrunner"] != "hana"]
        code, err = self.run_main()
        self.assertEqual((code, self.read_note()), (1, '---\ndate_created: "[[2026-10-01]]"\n---\n\nhand-written\n'))
        self.assertIn("left as it stands: roster.py lists no unit director under hana", err)

    def test_a_director_under_a_lower_showrunner_is_enough_to_draw(self) -> None:
        entries = cast(list[dict[str, str]], self.roster["entries"])
        self.roster["entries"] = [e for e in entries if e["showrunner"] != "hana"]
        self.children = {"tool-based-ui": "hanadocs-info\n", "hanadocs-info": ""}
        code, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("  hanadocs --> diagrams", self.read_note())

    def test_git_that_cannot_run_leaves_the_note_untouched(self) -> None:
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        with mock.patch.dict(os.environ, {"PATH": empty.name}):
            code, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("hand-written", self.read_note())
        self.assertIn("left as it stands: git rev-parse --show-toplevel: ", err)

    def test_lower_showrunner_and_its_subtree_are_drawn(self) -> None:
        self.children = {"tool-based-ui": "hanadocs-info\n", "hanadocs-info": ""}
        text = self.text()
        self.assertIn('  hanadocs["hanadocs — showrunner<br/>~/rust/hanadocs"]\n  hana --> hanadocs', text)
        self.assertIn("  hanadocs --> diagrams", text)
        self.assertIn("  diagrams --> hanadocs_diagrams_impl", text)
        self.assertIn("- Showrunners under hana: hanadocs (hanadocs-info).", text)

    def test_production_file_in_the_vault_is_a_wiki_link(self) -> None:
        link = self.module.production_file_link
        vault = os.path.join(self.root, "vault")
        _ = sh(vault, "init", "-q", "-b", "main")
        doc = Path(vault, "productions", "hanadocs-info-production.md")
        doc.parent.mkdir()
        _ = doc.write_text("# hanadocs-info\n", encoding="utf-8")
        self.assertEqual(link(doc, Path(vault)), "[[productions/hanadocs-info-production]]")

    def test_production_file_follows_the_first_promote_checkout(self) -> None:
        link = self.module.production_file_link
        main = os.path.join(self.root, "main")
        os.makedirs(main)
        _ = sh(main, "init", "-q", "-b", "main")
        _ = sh(main, "remote", "add", "origin", "https://github.com/natepiano/hana.git")
        doc = Path(self.root, "hana_catalyst", "docs", "hana", "tool-based-ui-production.md")
        _ = doc.write_text(f"- **Promote:** `{main}`, mac ~/rust/hana — wrap pushes\n", encoding="utf-8")
        self.assertEqual(
            link(doc, Path(self.root, "vault")),
            "https://github.com/natepiano/hana/blob/main/docs/hana/tool-based-ui-production.md",
        )
