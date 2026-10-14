"""Tests for setting every unit director's effort and the launch row together."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest import mock

import fake_tmux
import unit_effort
import unit_lookup
from fake_tmux import FakeSession
from unit_effort import Ready, Skipped

FAKE = str(Path(__file__).with_name("fake_tmux.py"))
RULE_TOP = "──── review-pool ──────────"
RULE = "───────────────────────────"
STATUS = "  natedev | claude3 | review-pool | 189,709 | Opus 5.5 xhigh"
EMPTY = f"{RULE_TOP}\n\x1b[39m❯ \n{RULE}\n{STATUS}"


def screen(*box: str, above: str = "✻ Churned for 6s · done 21:41\n") -> str:
    return f"{above}\n" + "\n".join((RULE_TOP, *box, RULE, STATUS))


class VerdictTests(unittest.TestCase):
    def test_an_empty_input_holding_only_a_non_breaking_space_is_ready(self) -> None:
        self.assertEqual(unit_effort.verdict(EMPTY), Ready())

    def test_a_dim_suggestion_is_no_draft_even_under_the_cursor(self) -> None:
        self.assertEqual(unit_effort.verdict(screen("❯ \x1b[2mrun the tests\x1b[22m")), Ready())
        cursor = "❯ \x1b[7mr\x1b[27m\x1b[2mun the tests\x1b[0m"
        self.assertEqual(unit_effort.verdict(screen(cursor)), Ready())

    def test_typed_text_is_a_draft_with_or_without_the_cursor_on_it(self) -> None:
        draft = Skipped("a draft is in its input line")
        self.assertEqual(unit_effort.verdict(screen("❯ half a message")), draft)
        self.assertEqual(unit_effort.verdict(screen("❯ \x1b[7mx\x1b[27m")), draft)
        # A true-colour foreground carries a 2 that is no dim code.
        self.assertEqual(unit_effort.verdict(screen("❯ \x1b[38;2;200;2;2mred text\x1b[39m")), draft)
        self.assertEqual(unit_effort.verdict(screen("❯ first line", "  second line")), draft)

    def test_a_form_prompt_or_menu_is_skipped(self) -> None:
        form = Skipped("a form, prompt or menu is open")
        permission = "Bash command\n  rm -rf build\nDo you want to proceed?\n❯ 1. Yes\n  2. No\nEsc to cancel"
        self.assertEqual(unit_effort.verdict(f"{RULE_TOP}\n{permission}"), form)
        self.assertEqual(unit_effort.verdict("☐ Scope\nWhich one?\n❯ 1. This\n  2. That\nEnter to select"), form)
        self.assertEqual(unit_effort.verdict(screen("❯ /effo") + "\n  Enter to confirm"), form)

    def test_a_pane_with_no_input_box_is_skipped(self) -> None:
        self.assertEqual(unit_effort.verdict("$ ls\nfile\n$ "), Skipped("no input line on screen"))


class UnitEffortTests(unittest.TestCase):
    root: Path = Path()
    state: Path = Path()
    records: Path = Path()
    registry: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.root / "tmux.json"
        self.records = self.root / "sessions"
        self.records.mkdir()
        self.registry = self.root / "agents.conf"
        _ = self.registry.write_text(
            "[assignments]\nproduction=claude\n\n[production.claude]\ndirector=opus:xhigh\n\n"
            + "[claude.agents]\nopus=low,medium,high,xhigh,max\n", encoding="utf-8")
        synced = self.root / "catalog-sync-success"
        synced.touch()
        self.enterContext(mock.patch.dict(os.environ, {
            "FAKE_TMUX_STATE": str(self.state), "UNIT_LOOKUP_TMUX": FAKE,
            "NOTIFIER_SESSIONS_DIR": str(self.records), "AGENTS_CONFIG_FILE": str(self.registry),
            "CODEX_CONFIG_FILE": str(self.root / "codex.toml"),
            "CODEX_MODELS_CACHE_FILE": str(self.root / "models.json"),
            "CODEX_CATALOG_SYNC_STATE_FILE": str(synced)}))
        _ = self.enterContext(mock.patch.object(unit_lookup, "live_session", return_value=True))
        sessions = {
            "$1": self.unit("review-pool", "%1", "build-followups", "review-pool-unit", EMPTY),
            "$2": self.unit("widget", "%2", "tool-based-ui", "widget-unit", screen("❯ my draft")),
            "$3": self.unit("trunk", "%3", "tool-based-ui", "trunk-unit", screen("❯ \x1b[2mtry this\x1b[22m")),
            "$4": FakeSession(label="notes", panes=["%4"], env={}, screens={"%4": EMPTY}),
        }
        fake_tmux.write(self.state, sessions)
        for pane, name in (("%1", "review-pool"), ("%2", "widget"), ("%3", "trunk"), ("%4", "notes")):
            _ = (self.records / f"{name}.json").write_text(json.dumps({
                "pid": os.getpid(), "sessionId": f"id-{name}", "name": name,
                "messagingSocketPath": str(self.root / f"{name}.sock"), "updatedAt": 1,
                "tmux": f"{name}:@1.{pane}"}), encoding="utf-8")

    @staticmethod
    def unit(label: str, pane: str, slug: str, unit: str, shown: str) -> FakeSession:
        return FakeSession(label=label, panes=[pane], env={"SHOWRUNNER_UNIT": slug, "SHOWRUNNER_UNIT_ID": unit},
                           screens={pane: shown})

    def run_main(self, *arguments: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = unit_effort.main(list(arguments))
        return code, out.getvalue(), err.getvalue()

    def sent(self) -> list[list[str]]:
        return [keys for session in fake_tmux.read(self.state).values() for keys in session.get("sent", [])]

    def director_row(self) -> str:
        return next(line for line in self.registry.read_text(encoding="utf-8").splitlines()
                    if line.startswith("director="))

    def test_the_dry_run_sends_nothing_and_changes_nothing(self) -> None:
        code, out, _ = self.run_main("high", "--check")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "review-pool: would send",
            "trunk: would send",
            "widget: skipped — a draft is in its input line",
            "registry: production.director would be opus:high (now opus:xhigh)",
        ])
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.director_row(), "director=opus:xhigh")

    def test_every_productions_directors_get_effort_and_the_row_keeps_its_model(self) -> None:
        code, out, _ = self.run_main("high")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[-1], "registry: production.director is now opus:high (was opus:xhigh)")
        self.assertIn("widget: skipped — a draft is in its input line", out)
        self.assertEqual(self.sent(), [["%1", "-l", "/effort high"], ["%1", "Enter"],
                                       ["%3", "-l", "/effort high"], ["%3", "Enter"]])
        self.assertEqual(self.director_row(), "director=opus:high")

    def test_a_level_the_model_does_not_take_is_refused_before_anything_happens(self) -> None:
        code, _, err = self.run_main("extreme")
        self.assertEqual(code, 2)
        self.assertIn("opus takes low, medium, high, xhigh, max, not extreme", err)
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.director_row(), "director=opus:xhigh")
        self.assertEqual(self.run_main("--check")[0], 2)


if __name__ == "__main__":
    _ = unittest.main()
