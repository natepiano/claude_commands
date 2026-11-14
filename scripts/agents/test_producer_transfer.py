"""A producer transfer gives the role to the calling conversation of either family."""
from __future__ import annotations

import io
import os
import unittest
from collections.abc import Generator
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast
from unittest.mock import Mock, patch

from scripts.accounts import accounts
from scripts.agents import pane_record, producer_transfer, remote, restart
from scripts.agents.test_stop_resume import ProducerAdmissionWorld, producer_first_fixture
from scripts.herdr import herdr, pane_launch
from scripts.lib import models_registry
from scripts.lib.test_models_registry import write_registry
from scripts.production import staff
from scripts.production.staff_test_support import fixture_member, fixture_runtime_id

OLD_PANE = "w1:p7"
OLD_SESSION = staff.native_id(fixture_runtime_id("previous-dwight"))


class TransferFixture:
    """One live caller in its pane, and the calls that reach outside the transfer."""

    def __init__(self, world: ProducerAdmissionWorld) -> None:
        self.world: ProducerAdmissionWorld = world
        self.steps: list[str] = []
        self.old_panes: list[str] = [OLD_PANE]
        self.stops: bool = True
        self.closes: bool = True
        self.locked_at_restart: bool = False

    @property
    def registry(self) -> Path:
        return self.world.case.root / "configuration/models.conf"

    @property
    def staff_file(self) -> Path:
        return self.world.case.root / "staff.json"

    def state(self) -> tuple[bytes, bytes]:
        """The staff record and the registry: a refused or repeated transfer leaves both unchanged."""
        return (self.staff_file.read_bytes() if self.staff_file.exists() else b"", self.registry.read_bytes())

    def settings(self) -> str:
        return models_registry.parse_registry(self.registry.read_text())["producers"]["dwight"]

    def row_panes(self, rows: list[staff.StaffRow], client: herdr.Herdr) -> list[tuple[staff.StaffRow, str]]:
        del client
        self.steps.append("panes")
        return [(rows[0], pane) for pane in self.old_panes]

    def stop_row(self, row: staff.StaffRow, client: herdr.Herdr) -> bool:
        del client
        self.steps.append(f"stop {row.current_session}")
        return self.stops

    def close(self, pane: str, *, terminal: str = "") -> bool:
        del terminal
        self.steps.append(f"close {pane}")
        if self.closes:
            self.old_panes.remove(pane)
        return self.closes

    def title(self, row: str) -> bool:
        owner = staff.initial_snapshot().by_id(staff.StaffId(row))
        assert owner is not None
        self.steps.append(f"title {owner.name}")
        return True

    def restart(self, *, stop_all: bool, only: tuple[str, ...], why: str) -> int:
        row = staff.initial_snapshot().by_id(staff.StaffId(only[0]))
        assert stop_all and row is not None and "transfer dwight" in why
        self.locked_at_restart = "STAFF_ROW_FD_" + row.id in os.environ
        self.steps.append(f"restart {row.current_session}")
        return 0

    def producer(self, session: str = OLD_SESSION) -> staff.StaffRow:
        return fixture_member("producer", name="dwight", runtime_id=session,
                              doc=staff.LiveDoc(remote.local_machine(), self.world.folder / "current.md"))

    def transfer(self, model: str = "", effort: str = "") -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            code = producer_transfer.transfer("dwight", model=model, effort=effort)
        assert code == 0
        return output.getvalue()


@contextmanager
def transfer_fixture(kind: Literal["claude", "codex"], settings: str) -> Generator[TransferFixture]:
    with producer_first_fixture(kind) as world, ExitStack() as scope:
        case = world.case
        fixture = TransferFixture(world)
        _ = write_registry(fixture.registry, {"producers": {"dwight": settings}}, update=True)
        world.active = True
        world.environment = {"HERDR_SESSION": "agents", "HERDR_PANE_ID": world.pane,
                             "CLAUDE_CONFIG_DIR" if kind == "claude" else "CODEX_HOME": str(world.home)}
        case.client.rows.append({"pane_id": world.pane, "terminal_id": "caller-terminal", "tab_id": "caller-tab",
                                 "workspace_id": "w1", "label": "caller",
                                 "agent_session": {"kind": "id", "value": world.identity}})
        case.client.native_ids[world.pane] = world.identity
        if kind == "codex":
            cast(Mock, herdr.codex_conversations).return_value = herdr.CodexConversations({world.identity: (456,)}, ())
        for boundary in (patch.object(staff, "runtime_id", return_value=world.identity),
                         patch.dict(os.environ, {"HERDR_PANE_ID": world.pane}),
                         patch.object(restart, "row_panes", side_effect=fixture.row_panes),
                         patch.object(restart, "stop_row", side_effect=fixture.stop_row),
                         patch.object(case.client, "close_retained_pane", side_effect=fixture.close),
                         patch.object(case.client, "retitle_row", side_effect=fixture.title),
                         patch.object(restart, "restart", side_effect=fixture.restart)):
            _ = cast(object, scope.enter_context(boundary))
        yield fixture


class ProducerTransferTests(unittest.TestCase):
    def test_a_codex_producer_moves_to_a_claude_session_and_back(self) -> None:
        cases: tuple[tuple[Literal["claude", "codex"], str, str, str], ...] = (
            ("claude", "codex:gpt-test:high", "opus", "high"), ("codex", "claude:opus:high", "gpt-test", "medium"))
        for kind, settings, model, effort in cases:
            with self.subTest(to=kind), transfer_fixture(kind, settings) as fixture:
                world = fixture.world
                if kind == "claude":
                    row = fixture.producer()
                else:
                    # The Codex caller is the producer's first conversation: its id is the row id.
                    row = staff.transfer_producer(fixture.producer(world.identity).id, OLD_SESSION,
                        staff.SessionHome(world.case.root / ".claude9"),
                        staff.PresentWorktree(remote.local_machine(), world.case.root))
                output = fixture.transfer(model, effort)
                moved = replace(row, current_session=staff.native_id(world.identity), home=staff.SessionHome(world.home),
                                worktree=staff.PresentWorktree(remote.local_machine(), world.folder))
                self.assertEqual(staff.initial_snapshot().by_id(row.id), moved)
                self.assertEqual(fixture.steps, ["panes", f"stop {OLD_SESSION}", f"close {OLD_PANE}", "title dwight",
                                                 f"restart {world.identity}"])
                self.assertFalse(fixture.locked_at_restart)
                self.assertEqual(fixture.settings(), f"{kind}:{model}:{effort}")
                binding = pane_record.read("agents")[world.pane]
                self.assertEqual((binding.get("staff_id"), binding.get("producer_binding", False)), (row.id, True))
                self.assertEqual([tab["staff_id"] for space in herdr.saved_layout("agents")["workspaces"]
                                  for tab in space["tabs"]], [row.id])
                self.assertEqual(output, "dwight: the old conversation stopped; read it with: "
                                 + f"~/.claude/scripts/lib/py ~/.claude/scripts/agents/catch_up.py {OLD_SESSION}\n"
                                 + f"dwight: session {world.identity} holds the role; {kind}:{model}:{effort}, folder {world.folder}\n")
                before = fixture.state()
                fixture.steps.clear()
                _ = fixture.transfer()
                self.assertEqual(fixture.steps, ["title dwight", f"restart {world.identity}"])
                self.assertEqual(fixture.state(), before)

    def test_a_refused_transfer_changes_nothing(self) -> None:
        other = accounts.NamedAccount("claude9", Path("/fixture/.claude9"))
        cases: tuple[tuple[str, str, str, str, list[str]], ...] = (
            ("member", "opus", "high", "a staff member cannot take a producer role", []),
            ("absent", "opus", "high", "no producer row", []),
            ("family", "", "", "give --model and --effort for claude", []),
            ("one option", "opus", "", "give --model and --effort together", []),
            ("model", "missing-agent", "high", "missing-agent", []),
            ("account", "opus", "high", "agents move", []),
            ("stop", "opus", "high", "did not stop", ["panes", f"stop {OLD_SESSION}"]),
            ("close", "opus", "high", "did not close", ["panes", f"stop {OLD_SESSION}", f"close {OLD_PANE}"]),
        )
        for case, model, effort, reason, steps in cases:
            with self.subTest(case=case), transfer_fixture("claude", "codex:gpt-test:high") as fixture:
                world = fixture.world
                if case != "absent":
                    _ = fixture.producer()
                if case == "member":
                    _ = fixture_member("producer", name="brain", runtime_id=world.identity)
                fixture.stops, fixture.closes = case != "stop", case != "close"
                before = fixture.state()
                with ExitStack() as scope:
                    if case == "account":
                        _ = scope.enter_context(patch.object(accounts, "role_account", return_value=other))
                    with self.assertRaisesRegex(ValueError, reason):
                        _ = fixture.transfer(model, effort)
                self.assertEqual(fixture.steps, steps)
                self.assertEqual(fixture.state(), before)
                self.assertNotIn(world.pane, pane_record.read("agents"))

    def test_a_failed_transfer_completes_when_it_runs_again(self) -> None:
        for stage in ("close", "staff", "publication"):
            with self.subTest(stage=stage), transfer_fixture("claude", "codex:gpt-test:high") as fixture:
                world = fixture.world
                row = fixture.producer()
                if stage == "close":
                    fixture.closes = False
                    boundary = nullcontext()
                elif stage == "staff":
                    boundary = patch.object(staff, "transfer_producer", side_effect=staff.StaffUnreadable("fixture write failure"))
                else:
                    boundary = patch.object(pane_launch, "write", side_effect=ValueError("fixture publication failure"))
                with boundary, self.assertRaises((ValueError, staff.StaffUnreadable)):
                    _ = fixture.transfer("opus", "high")
                self.assertNotIn(f"restart {world.identity}", fixture.steps)
                first = list(fixture.steps)
                fixture.steps.clear()
                fixture.closes = True
                _ = fixture.transfer("opus", "high")
                self.assertEqual(first + fixture.steps, {
                    "close": ["panes", f"stop {OLD_SESSION}", f"close {OLD_PANE}",
                              "panes", f"stop {OLD_SESSION}", f"close {OLD_PANE}", "title dwight", f"restart {world.identity}"],
                    "staff": ["panes", f"stop {OLD_SESSION}", f"close {OLD_PANE}",
                              "panes", f"stop {OLD_SESSION}", "title dwight", f"restart {world.identity}"],
                    "publication": ["panes", f"stop {OLD_SESSION}", f"close {OLD_PANE}", "title dwight", f"restart {world.identity}"],
                }[stage])
                moved = staff.initial_snapshot().by_id(row.id)
                assert moved is not None
                self.assertEqual((moved.current_session, fixture.settings()), (world.identity, "claude:opus:high"))
                self.assertTrue(pane_record.read("agents")[world.pane].get("producer_binding", False))


if __name__ == "__main__":
    _ = unittest.main()
