"""Tests for setting every unit director's effort and the launch row together."""
from __future__ import annotations

from scripts.lib.test_agents_registry import write_registry

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest import mock

import fake_unit_host
import unit_effort
import unit_lookup
import herdr
from fake_herdr import CommandResponse
from fake_unit_host import HostedUnit
from unit_effort import Ready, Skipped

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.lib import agents_registry  # noqa: E402
from scripts.production.staff_test_support import isolated_staff_file, unit_membership

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
    records: Path = Path()
    registry: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        self.records = self.root / "sessions"
        self.records.mkdir()
        self.registry = self.root / "agents.conf"
        _ = write_registry(self.registry, {'assignments': {'production': 'claude'}, 'production.claude': {'director': 'opus:xhigh'}, 'claude.agents': {'opus': 'low,medium,high,xhigh,max'}})
        _ = (self.root / "herdr-responses.json").write_text(json.dumps({"default": {"code": 0, "stdout": '{"sessions": []}'}}))
        synced = self.root / "catalog-sync-success"
        synced.touch()
        self.enterContext(mock.patch.dict(os.environ, {
            "HERDR_BIN": str(Path(__file__).parents[1] / "herdr/fake_herdr.py"),
            "HERDR_PROC": str(self.root / "proc"),
            "HERDR_PLATFORM": "linux",
            "CONVERSATION_PAUSE_STATE_DIR": str(self.root / "conversation-pause"),
            "FAKE_HERDR_LOG": str(self.root / "herdr.log"),
            "FAKE_HERDR_RESPONSES": str(self.root / "herdr-responses.json"),
            "NOTIFIER_SESSIONS_DIR": str(self.records), "AGENTS_CONFIG_FILE": str(self.registry),
            "CODEX_CONFIG_FILE": str(self.root / "codex.toml"),
            "CODEX_MODELS_CACHE_FILE": str(self.root / "models.json"),
            "CODEX_CATALOG_SYNC_STATE_FILE": str(synced)}))
        _ = self.enterContext(mock.patch.object(unit_lookup, "live_session", return_value=True))
        units = [
            HostedUnit("review-pool", "w1:p1", {"SHOWRUNNER_UNIT": "build-followups", "SHOWRUNNER_UNIT_ID": "review-pool-unit"}, EMPTY),
            HostedUnit("widget", "w2:p1", {"SHOWRUNNER_UNIT": "tool-based-ui", "SHOWRUNNER_UNIT_ID": "widget-unit"}, screen("❯ my draft")),
            HostedUnit("trunk", "w3:p1", {"SHOWRUNNER_UNIT": "tool-based-ui", "SHOWRUNNER_UNIT_ID": "trunk-unit"}, screen("❯ \x1b[2mtry this\x1b[22m")),
            HostedUnit("notes", "w4:p1", {}, EMPTY),
        ]
        for unit in units:
            if "SHOWRUNNER_UNIT_ID" in unit.marks:
                _ = unit_membership(unit.marks["SHOWRUNNER_UNIT"], unit.marks["SHOWRUNNER_UNIT_ID"])
        fake_unit_host.write(os.environ, units)
        for index, unit in enumerate(units):
            _ = (self.records / f"{unit.session}.json").write_text(json.dumps({
                "pid": 991001 + index * 2, "sessionId": f"id-{unit.session}", "name": unit.session,
                "messagingSocketPath": str(self.root / f"{unit.session}.sock"), "updatedAt": 1,
                "tmux": ""}), encoding="utf-8")

    def run_main(self, *arguments: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = unit_effort.main(list(arguments))
        return code, out.getvalue(), err.getvalue()

    def sent(self) -> list[list[str]]:
        return [call for call in fake_unit_host.calls(os.environ) if call[2:4] == ["agent", "prompt"]]

    def director_row(self) -> str:
        return next(line for line in agents_registry.registry_text(self.registry).splitlines()
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
        self.assertEqual(self.sent(), [["--session", "review-pool", "agent", "prompt", "w1:p1", "/effort high"],
                                       ["--session", "trunk", "agent", "prompt", "w3:p1", "/effort high"]])
        self.assertEqual(self.director_row(), "director=opus:high")

    def test_a_level_the_model_does_not_take_is_refused_before_anything_happens(self) -> None:
        code, _, err = self.run_main("extreme")
        self.assertEqual(code, 2)
        self.assertIn("opus takes low, medium, high, xhigh, max, not extreme", err)
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.director_row(), "director=opus:xhigh")
        self.assertEqual(self.run_main("--check")[0], 2)

    def test_a_herdr_director_reads_ansi_and_receives_effort_once(self) -> None:
        place = herdr.HerdrPlace("native", "w1:p1")
        marked = unit_lookup.MarkedUnit("native-unit", unit_lookup.LiveClaude("native", "id", "/sock", 1), place)
        responses = self.root / "herdr-responses.json"
        replies: dict[str, CommandResponse] = {
            "--session native pane read w1:p1 --source recent --lines 60 --format ansi": {
                "code": 0, "stdout": screen("❯ \x1b[2mtry this\x1b[22m")},
            "default": {"code": 0, "stdout": ""},
        }
        _ = responses.write_text(json.dumps(replies))
        with mock.patch.object(unit_lookup, "every_marked_unit", return_value=unit_lookup.MarkedProductions(
                {"show": {"native-unit": marked}}, {})):
            code, out, err = self.run_main("high")
        self.assertEqual(code, 0, err)
        self.assertIn("native: sent", out)
        self.assertEqual((self.root / "herdr.log").read_text().splitlines(), [
            json.dumps(["--session", "native", "pane", "read", "w1:p1", "--source", "recent", "--lines", "60",
                        "--format", "ansi"]),
            json.dumps(["--session", "native", "agent", "prompt", "w1:p1", "/effort high"]),
        ])
        self.assertEqual(self.sent(), [["--session", "native", "agent", "prompt", "w1:p1", "/effort high"]])

    def test_a_herdr_prompt_refusal_sends_no_enter(self) -> None:
        refusal: herdr.HerdrRefusal = {"error": {"code": "agent_blocked", "message": "blocked at a prompt"}}
        _ = (self.root / "herdr-responses.json").write_text(json.dumps({"default": {
            "code": 1, "stdout": json.dumps(refusal)}}))
        problem = unit_effort.send(herdr.HerdrPlace("native", "w1:p1"), "high")
        self.assertIn("agent_blocked", problem)
        self.assertEqual(len((self.root / "herdr.log").read_text().splitlines()), 1)


if __name__ == "__main__":
    _ = unittest.main()
